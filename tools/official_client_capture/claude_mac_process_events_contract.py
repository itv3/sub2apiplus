"""Darwin 原生进程投影的内部闭集合同；结构通过不等于实际内核来源。

旧 v1 只验证离线原件；owned-v2 增加生产管线的未资格化内部结构合同。
唯一 boot、mute、原生屏障及排空尚未现场资格化，
因此所有结果保持无正式来源、无产品启动权、无执行映像证明、无完整事件结论。
序号在范围筛选之前校验；生成帧永远不能转为真实原生窗口。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA = "claude-mac-native-process-projection/internal-v1"
BUILD_SCHEMA = "claude-mac-native-process-build/internal-v1"
CAPTURE_SCHEMA = "claude-mac-native-process-capture/internal-v1"
EVENT_TYPES = ("notify_fork", "notify_exec", "notify_exit")
INCOMPLETE_REASONS = frozenset({
    "boot_unavailable", "boot_changed", "abi_unknown", "message_version_insufficient",
    "scope_muted", "mute_unknown", "global_sequence_gap", "type_sequence_gap",
    "queue_overflow", "record_limit", "byte_limit", "relation_unknown",
    "native_path_truncated", "observer_terminated", "control_channel_failed",
    "wal_truncated", "closing_barrier_missing", "drain_unverified", "deadline_exceeded",
})
NATIVE_RESULTS = frozenset({
    "native_entry_disabled", "boot_representation_unqualified", "abi_unknown",
    "es_internal_error", "es_invalid_argument", "es_not_entitled", "es_not_permitted",
    "es_not_privileged", "es_too_many_clients", "es_unknown_result",
    "client_creation_succeeded_no_subscription",
})
FRAME_FIELDS = frozenset({
    "schema_version", "record_type", "collector_run_id", "context_sha256", "message_version",
    "global_seq_num", "event_type", "event_seq_num", "kernel_mach_time", "kernel_time", "payload",
})
PROCESS_FIELDS = frozenset({
    "boot_session_uuid", "pid", "pidversion", "fork_start_time", "parent_audit_identity", "executable",
})
EXECUTABLE_FIELDS = frozenset({
    "path", "path_truncated", "device", "inode", "size", "mtime", "cdhash",
    "codesigning_flags", "signing_id", "team_id",
})
PROJECTION_DESCRIPTOR = {
    "schema_version": SCHEMA, "frame_fields": sorted(FRAME_FIELDS),
    "process_fields": sorted(PROCESS_FIELDS), "executable_fields": sorted(EXECUTABLE_FIELDS),
    "events": list(EVENT_TYPES), "message_version_min": 4, "message_version_max": 8,
    "source_scope": "internal_native_probe", "probe_program_id": "fork-exec-exit-v1",
    "incomplete_reasons": sorted(INCOMPLETE_REASONS), "native_results": sorted(NATIVE_RESULTS),
    "live_window_qualified": False,
}


class NativeProcessSourceError(ValueError):
    """原生材料、参数或闭集合同非法。"""


class NativeProcessSourceUnavailable(NativeProcessSourceError):
    """缺少本版尚未资格化的原生前提；禁止猜测成功。"""


class NativeProcessObservationIncomplete(NativeProcessSourceError):
    """序号、关系或窗口不闭合；原件不得补造。"""


def require(condition: Any, message: str) -> None:
    """所有类型与边界显式检查，不能以布尔值冒充整数。"""
    if not condition:
        raise NativeProcessSourceError(message)


def _uint(value: Any, bits: int = 64) -> None:
    require(type(value) is int and 0 <= value < 2 ** bits, "无符号整数越界或类型非法")


def _sha(value: Any) -> None:
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "SHA256 格式非法")


def _id(value: Any) -> None:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value), "原生上下文 ID 非法")


def utc(value: Any) -> datetime:
    """UTC 必须来自明确字段，不用本机当前时刻补齐缺失事件。"""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise NativeProcessSourceError("时间须为明确 UTC") from error
    require(moment.tzinfo is not None and moment.utcoffset().total_seconds() == 0, "时间须为明确 UTC")
    return moment


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def canonical_json(value: Any) -> bytes:
    """原件采用固定 UTF-8／排序键／紧凑 JSON；拒绝 NaN 等扩展。"""
    return json.dumps(_thaw(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        require(key not in result, "JSON 存在重复键")
        result[key] = value
    return result


def strict_json(raw: bytes) -> Any:
    """拒绝重复键、无效 UTF-8、非 JSON 常量与不可闭合原件。"""
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(NativeProcessSourceError("JSON 常量非法")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise NativeProcessSourceError("JSON 编码或结构非法") from error


def freeze(value: Any) -> Any:
    """逐层复制后冻结，调用方不能在重验之后改变嵌套字段。"""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def exact(value: Any, fields: set[str] | frozenset[str]) -> None:
    require(isinstance(value, Mapping) and set(value) == fields, "字段不属于合同闭集")


@dataclass(frozen=True)
class MaterialBinding:
    path: str
    sha256: str
    bytes: int

    def __post_init__(self) -> None:
        require(isinstance(self.path, str) and Path(self.path).is_absolute()
                and ".." not in Path(self.path).parts and str(Path(self.path)) == self.path, "物料路径须为规范绝对路径")
        _sha(self.sha256)
        _uint(self.bytes)


@dataclass(frozen=True)
class NativeLimits:
    max_record_bytes: int
    max_total_bytes: int
    max_records: int
    max_queue_records: int
    max_pending_relations: int
    deadline_utc: str

    def __post_init__(self) -> None:
        for name, ceiling in (("max_record_bytes", 1_048_576), ("max_total_bytes", 67_108_864),
                              ("max_records", 65_536), ("max_queue_records", 8_192), ("max_pending_relations", 4_096)):
            value = getattr(self, name)
            require(type(value) is int and 0 < value <= ceiling, "原生窗口上界非法")
        require(self.max_total_bytes >= self.max_record_bytes, "总字节上界小于单记录上界")
        utc(self.deadline_utc)


@dataclass(frozen=True)
class NativeBuildIdentity:
    build_receipt: MaterialBinding
    source_files_sha256: str
    projection_contract_sha256: str
    helper: MaterialBinding
    helper_mode: int
    helper_arch: str
    probe_fixture: MaterialBinding
    probe_fixture_mode: int
    sdk_identity_sha256: str
    toolchain_identity_sha256: str
    signing_identity_sha256: str
    signing_status: str

    def __post_init__(self) -> None:
        for name in ("build_receipt", "helper", "probe_fixture"):
            require(isinstance(getattr(self, name), MaterialBinding), "构建物料类型非法")
        for name in ("source_files_sha256", "projection_contract_sha256", "sdk_identity_sha256",
                     "toolchain_identity_sha256", "signing_identity_sha256"):
            _sha(getattr(self, name))
        require(type(self.helper_mode) is int and type(self.probe_fixture_mode) is int
                and self.helper_mode == self.probe_fixture_mode == 0o755, "原生构建物权限须为 0755")
        require(self.helper_arch == "arm64", "本版原生构建只接受 arm64")
        require(self.signing_status in {"unsigned", "ad_hoc_unqualified"}, "本版没有合格 ES 签名来源")


@dataclass(frozen=True)
class NativeContext:
    task_sha256: str
    task_nonce: str
    producer_run_id: str
    collector_run_id: str
    deployment_id: str
    tool_identity_sha256: str
    build_identity_sha256: str
    source_scope: str
    probe_program_id: str
    limits: NativeLimits

    def __post_init__(self) -> None:
        for name in ("task_sha256", "tool_identity_sha256", "build_identity_sha256"):
            _sha(getattr(self, name))
        for name in ("task_nonce", "producer_run_id", "collector_run_id", "deployment_id"):
            _id(getattr(self, name))
        require(self.source_scope == "internal_native_probe" and self.probe_program_id == "fork-exec-exit-v1", "禁止真实产品或任意探针范围")
        require(isinstance(self.limits, NativeLimits), "窗口上界类型非法")


@dataclass(frozen=True)
class NativeReadiness:
    status: str
    native_result: str
    build_identity_sha256: str
    boot_session_uuid: str | None
    missing_prerequisites: tuple[str, ...]
    diagnostic: MaterialBinding
    formal_ready: bool
    launch_allowed: bool
    registration_verified: bool

    def __post_init__(self) -> None:
        require(self.status == "unavailable" and self.native_result in NATIVE_RESULTS, "本版不授原生可用结论")
        _sha(self.build_identity_sha256)
        require(type(self.missing_prerequisites) is tuple and all(isinstance(x, str) for x in self.missing_prerequisites), "缺件须为冻结元组")
        require(isinstance(self.diagnostic, MaterialBinding), "诊断绑定类型非法")
        require(self.formal_ready is False and self.launch_allowed is False and self.registration_verified is False, "诊断不能授正式权")


@dataclass(frozen=True)
class NativeBarrier:
    collector_run_id: str
    phase: str
    observed_at_utc: str
    global_seq_num: int
    fork_seq_num: int
    exec_seq_num: int
    exit_seq_num: int
    event_prefix: MaterialBinding

    def __post_init__(self) -> None:
        _id(self.collector_run_id)
        require(self.phase in {"ready", "closing"}, "屏障阶段非法")
        utc(self.observed_at_utc)
        for name in ("global_seq_num", "fork_seq_num", "exec_seq_num", "exit_seq_num"):
            _uint(getattr(self, name))
        require(isinstance(self.event_prefix, MaterialBinding), "屏障原件绑定类型非法")


@dataclass(frozen=True)
class NativeCaptureResult:
    status: str
    origin: str
    context_sha256: str
    build_identity_sha256: str
    collector_instance: MaterialBinding
    ready: MaterialBinding | None
    closed: MaterialBinding | None
    native_projection: MaterialBinding
    capture_manifest: MaterialBinding
    unknown_reasons: tuple[str, ...]
    process_events_complete: bool
    executed_image_verified: bool
    formal_ready: bool
    launch_allowed: bool
    registration_verified: bool

    def __post_init__(self) -> None:
        require(self.status in {"structurally_valid_fixture", "incomplete", "unavailable"}, "本版不能签发真实完整窗口")
        require(self.origin in {"regression_fixture", "supplied_unverified"}, "外部材料不能自报实际原生来源")
        _sha(self.context_sha256)
        _sha(self.build_identity_sha256)
        for name in ("collector_instance", "native_projection", "capture_manifest"):
            require(isinstance(getattr(self, name), MaterialBinding), "结果原件类型非法")
        for name in ("ready", "closed"):
            require(getattr(self, name) is None or isinstance(getattr(self, name), MaterialBinding), "结果屏障类型非法")
        require(type(self.unknown_reasons) is tuple and all(x in INCOMPLETE_REASONS for x in self.unknown_reasons), "结果未知原因非法")
        for name in ("process_events_complete", "executed_image_verified", "formal_ready", "launch_allowed", "registration_verified"):
            require(getattr(self, name) is False, "离线重放不能授原生完整、执行映像、正式来源或启动权")


def context_digest(context: NativeContext) -> str:
    require(isinstance(context, NativeContext), "上下文类型非法")
    return fingerprint(asdict(context))


def _time(value: Any, fraction: str, ceiling: int) -> None:
    exact(value, {"seconds", fraction})
    require(type(value["seconds"]) is int and 0 < value["seconds"] < 2 ** 63, "内核秒值非法")
    require(type(value[fraction]) is int and 0 <= value[fraction] < ceiling, "内核分秒值非法")


def _binding(value: Any) -> MaterialBinding:
    exact(value, {"path", "sha256", "bytes"})
    return MaterialBinding(**value)


def _process(value: Any) -> tuple[Any, ...]:
    exact(value, PROCESS_FIELDS)
    boot = value["boot_session_uuid"]
    require(isinstance(boot, str) and re.fullmatch(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", boot)
            and boot.lower() != "00000000-0000-0000-0000-000000000000", "唯一 boot UUID 缺失或非法")
    require(type(value["pid"]) is int and 0 < value["pid"] < 2 ** 31, "内核 PID 非法")
    _uint(value["pidversion"], 32)
    _time(value["fork_start_time"], "microseconds", 1_000_000)
    parent = value["parent_audit_identity"]
    if parent is not None:
        exact(parent, {"pid", "pidversion"})
        require(type(parent["pid"]) is int and 0 < parent["pid"] < 2 ** 31, "父 audit PID 非法")
        _uint(parent["pidversion"], 32)
    executable = value["executable"]
    exact(executable, EXECUTABLE_FIELDS)
    require(isinstance(executable["path"], str) and executable["path"].startswith("/")
            and len(executable["path"].encode("utf-8")) <= 4096
            and not any(ord(c) < 32 for c in executable["path"]), "原生路径非法或越界")
    require(executable["path_truncated"] is False, "截短路径不能闭合")
    for name in ("device", "inode", "size"):
        _uint(executable[name])
    _uint(executable["codesigning_flags"], 32)
    _time(executable["mtime"], "nanoseconds", 1_000_000_000)
    require(isinstance(executable["cdhash"], str) and re.fullmatch(r"[0-9a-f]{40}", executable["cdhash"]), "cdhash 格式非法")
    for name in ("signing_id", "team_id"):
        require(isinstance(executable[name], str) and len(executable[name].encode("utf-8")) <= 4096
                and not any(ord(c) < 32 for c in executable[name]), "签名 token 非法")
    start = value["fork_start_time"]
    return boot.lower(), value["pid"], start["seconds"], start["microseconds"]


def validate_native_projection(context: NativeContext, raw: bytes) -> tuple[Mapping[str, Any], ...]:
    """严格重验结构、全量序号与生命周期；返回冻结帧，不签发可信来源。

    第一个序号是实际帧给出的基线，不推测起始为零；屏障必须引用实际前缀。
    即使全部结构闭合，调用方也只能将它标为夹具或未核实材料。
    """
    context_sha = context_digest(context)
    require(type(raw) is bytes and 0 < len(raw) <= context.limits.max_total_bytes, "投影为空或超过总字节上界")
    if not raw.endswith(b"\n"):
        raise NativeProcessObservationIncomplete("wal_truncated：原件缺少实际末尾换行")
    lines = raw.splitlines(keepends=True)
    require(len(lines) <= context.limits.max_records, "record_limit：原件记录数越界")
    frames, per_type, states, active_pids, exec_counts = [], {}, {}, {}, {}
    probe_forks, probe_exits = 0, 0
    last_global, boot, offset = None, None, 0
    ready, closing, ended, incomplete, collector_binding = False, False, False, False, None
    for line in lines:
        require(0 < len(line) <= context.limits.max_record_bytes, "byte_limit：单记录越界")
        value = strict_json(line)
        exact(value, FRAME_FIELDS)
        require(canonical_json(value) + b"\n" == line, "投影不是规范 JSONL")
        require(not ended and value["schema_version"] == SCHEMA and value["collector_run_id"] == context.collector_run_id
                and value["context_sha256"] == context_sha, "记录上下文变化、schema 非法或关闭后继续记录")
        kind, payload = value["record_type"], value["payload"]
        if kind in {"process_event", "counter_only"}:
            require(type(value["message_version"]) is int and 4 <= value["message_version"] <= 8, "message_version_insufficient／abi_unknown")
            event = value["event_type"]
            require(event in EVENT_TYPES, "原生事件不属于订阅闭集")
            for name in ("global_seq_num", "event_seq_num", "kernel_mach_time"):
                _uint(value[name])
            _time(value["kernel_time"], "nanoseconds", 1_000_000_000)
            if last_global is not None and value["global_seq_num"] != last_global + 1:
                raise NativeProcessObservationIncomplete("global_sequence_gap：过滤前全局序号不连续")
            if event in per_type and value["event_seq_num"] != per_type[event] + 1:
                raise NativeProcessObservationIncomplete("type_sequence_gap：事件型序号不连续")
            last_global, per_type[event] = value["global_seq_num"], value["event_seq_num"]
            if kind == "counter_only":
                exact(payload, set())
            else:
                require(not closing, "收尾屏障之后不能再出现受控探针事件")
                exact(payload, {"actor", "target", "exit_stat"})
                actor = payload["actor"]
                actor_key = _process(actor)
                if boot is None:
                    boot = actor_key[0]
                require(actor_key[0] == boot, "boot_changed：原生窗口 boot 改变")
                if actor_key not in states:
                    require(event == "notify_fork", "relation_unknown：缺少实例形成事件")
                    require(actor["pid"] not in active_pids, "PID 复用的旧实例尚未收口")
                    states[actor_key], active_pids[actor["pid"]] = actor, actor_key
                require(active_pids.get(actor["pid"]) == actor_key and states[actor_key] == actor, "实例已经退出或未记录执行代次改变")
                if event in {"notify_fork", "notify_exec"}:
                    require(payload["exit_stat"] is None, "非 exit 事件不能携带退出码")
                    target = payload["target"]
                    target_key = _process(target)
                    require(target_key[0] == boot, "boot_changed：目标 boot 改变")
                    if event == "notify_fork":
                        require(target["pid"] != actor["pid"] and target_key not in states
                                and target["pid"] not in active_pids, "fork 子实例重复或 PID 冲突")
                        require(target["parent_audit_identity"] == {"pid": actor["pid"], "pidversion": actor["pidversion"]}, "fork 父子 audit 关系不闭合")
                        require(len(active_pids) < context.limits.max_pending_relations, "relation_unknown：关系数量越界")
                        states[target_key], active_pids[target["pid"]] = target, target_key
                        probe_forks += 1
                    else:
                        require(target_key == actor_key and target["pidversion"] > actor["pidversion"], "exec 必须保留 fork 实例并增加执行代次")
                        states[actor_key] = target
                        exec_counts[actor_key] = exec_counts.get(actor_key, 0) + 1
                else:
                    require(payload["target"] is None and type(payload["exit_stat"]) is int
                            and -(2 ** 31) <= payload["exit_stat"] < 2 ** 31, "exit 投影非法")
                    del active_pids[actor["pid"]]
                    probe_exits += 1
        elif kind in {"barrier", "closed"}:
            require(all(value[x] is None for x in ("message_version", "event_type", "event_seq_num", "kernel_mach_time", "kernel_time"))
                    and last_global is not None and value["global_seq_num"] == last_global, "屏障须引用已见水位，不得伪造内核消息")
            watermarks = {name.removeprefix("notify_"): seq for name, seq in per_type.items()}
            require(set(watermarks) == {"fork", "exec", "exit"}, "屏障没有三个事件型的实际水位")
            require(isinstance(payload, Mapping) and "type_watermarks" in payload, "屏障缺水位字段")
            exact(payload["type_watermarks"], {"fork", "exec", "exit"})
            for seq in payload["type_watermarks"].values():
                _uint(seq)
            if kind == "barrier":
                exact(payload, {"phase", "type_watermarks", "collector_instance", "event_prefix"})
                require(payload["type_watermarks"] == watermarks, "屏障型水位与原件不符")
                binding = _binding(payload["collector_instance"])
                prefix = _binding(payload["event_prefix"])
                if payload["phase"] == "ready":
                    require(not ready and not incomplete and not closing, "ready 重复或在错误后发布")
                    ready, collector_binding = True, binding
                elif payload["phase"] == "closing":
                    require(ready and not closing and not active_pids and binding == collector_binding, "收尾屏障缺 ready、实例未退出或采集器改变")
                    closing = True
                else:
                    raise NativeProcessSourceError("屏障阶段不属于闭集")
            else:
                exact(payload, {"type_watermarks", "unclosed_instances", "drain_status", "event_material"})
                require(payload["type_watermarks"] == watermarks and type(payload["unclosed_instances"]) is int
                        and payload["unclosed_instances"] == len(active_pids), "关闭记录水位或实例数不符")
                require(payload["drain_status"] in {"verified", "unknown"}, "排空状态非法")
                if payload["drain_status"] == "verified":
                    require(ready and closing and not incomplete and not active_pids, "不能以 closed 覆盖缺屏障或不完整窗口")
                    require(probe_forks > 0 and probe_exits >= 2 and any(count >= 2 for count in exec_counts.values()), "固定探针缺少 fork、多次 exec 或自然 exit")
                prefix = _binding(payload["event_material"])
                ended = True
            require(prefix.bytes == offset and prefix.sha256 == hashlib.sha256(raw[:offset]).hexdigest(), "屏障原件前缀摘要不符")
        elif kind in {"diagnostic", "incomplete"}:
            require(all(value[x] is None for x in ("message_version", "global_seq_num", "event_type", "event_seq_num", "kernel_mach_time", "kernel_time")), "诊断不得补零或自造内核时刻")
            if kind == "diagnostic":
                exact(payload, {"native_result", "missing_prerequisites"})
                require(payload["native_result"] in NATIVE_RESULTS and type(payload["missing_prerequisites"]) is list
                        and 0 < len(payload["missing_prerequisites"]) <= 16
                        and all(isinstance(x, str) and x and len(x) <= 128 for x in payload["missing_prerequisites"]), "原生诊断码或缺件非法")
            else:
                exact(payload, {"reason", "first_error_sequence"})
                require(payload["reason"] in INCOMPLETE_REASONS, "不完整原因不属于闭集")
                if payload["first_error_sequence"] is not None:
                    _uint(payload["first_error_sequence"])
                incomplete = True
        else:
            raise NativeProcessSourceError("记录型不属于合同闭集")
        frames.append(freeze(value))
        offset += len(line)
    return tuple(frames)


# 独立内部 v2 不改变上述 v1 描述符、摘要或历史解释。
OWNED_PLAN_SCHEMA = "claude-mac-owned-process-plan/internal-v2"
OWNED_REQUEST_SCHEMA = "claude-mac-owned-process-request/internal-v2"
OWNED_CONTROL_SCHEMA = "claude-mac-owned-process-control/internal-v2"
OWNED_FRAME_SCHEMA = "claude-mac-owned-process-projection/internal-v2"
OWNED_CAPTURE_SCHEMA = "claude-mac-owned-process-capture/internal-v2"
OWNED_RESULT_SCHEMA = "claude-mac-owned-process-read-result/internal-v2"
OWNED_PLAN_FIELDS = frozenset({"schema_version", "context", "context_sha256", "build_receipt",
    "build_identity_sha256", "helper", "probe_fixture", "bundle_root", "bundle_instance", "plan_sha256"})
OWNED_FRAME_FIELDS = FRAME_FIELDS | {"plan_sha256"}
OWNED_FILE_ROLES = frozenset({"plan", "context", "build_receipt", "projection", "collector_instance",
    "boot_before", "boot_after", "mute_before", "mute_after", "ready", "closing", "closed", "first_error"})
OWNED_MANIFEST_FIELDS = frozenset({"schema_version", "plan_sha256", "context_sha256", "collector_run_id",
    "build_identity_sha256", "origin", "status", "files", "unknown_reasons"})
OWNED_FLAGS = ("process_events_complete", "executed_image_verified", "formal_ready", "launch_allowed", "registration_verified")
OWNED_RESULT_FIELDS = frozenset({"schema_version", "status", "origin", "plan_sha256", "context_sha256",
    "build_identity_sha256", "collector_run_id", "projection", "ready", "closing", "closed", "first_error",
    "unknown_reasons", "record_count", "observed_event_counts", *OWNED_FLAGS})
OWNED_STATUSES = frozenset({"unavailable", "incomplete", "observed_unqualified", "structurally_valid_fixture"})
OWNED_ORIGINS = frozenset({"regression_fixture", "supplied_unverified", "native_unqualified"})
OWNED_KINDS = frozenset({"process_event", "counter_only", "collector_instance", "diagnostic",
    "state_observation", "incomplete", "barrier", "closed"})
OWNED_MISSING = frozenset({"boot_representation_unqualified", "mute_unqualified", "closing_barrier_unqualified"})
OWNED_PROJECTION_DESCRIPTOR = {
    "schema_version": OWNED_FRAME_SCHEMA, "frame_fields": sorted(OWNED_FRAME_FIELDS),
    "process_fields": sorted(PROCESS_FIELDS), "executable_fields": sorted(EXECUTABLE_FIELDS),
    "record_types": sorted(OWNED_KINDS), "events": list(EVENT_TYPES),
    "message_version_min": 4, "message_version_max": 8, "live_window_qualified": False,
    "source_scope": "internal_native_probe", "capabilities": {name: False for name in OWNED_FLAGS},
}


def owned_context(value: Mapping[str, Any]) -> NativeContext:
    """保留原类型，仅施加更小的 v2 限额，不放宽历史上限。"""
    exact(value, {"task_sha256", "task_nonce", "producer_run_id", "collector_run_id", "deployment_id",
        "tool_identity_sha256", "build_identity_sha256", "source_scope", "probe_program_id", "limits"})
    limits = value["limits"]
    exact(limits, {"max_record_bytes", "max_total_bytes", "max_records", "max_queue_records",
        "max_pending_relations", "deadline_utc"})
    for name, bound in (("max_record_bytes", 65536), ("max_total_bytes", 67108864),
                       ("max_records", 65536), ("max_queue_records", 4096), ("max_pending_relations", 256)):
        require(type(limits[name]) is int and 0 < limits[name] <= bound, "owned-v2 限额非法")
    return NativeContext(**{key: item for key, item in value.items() if key != "limits"}, limits=NativeLimits(**limits))


def owned_role(value: Mapping[str, Any], role: str) -> MaterialBinding:
    """固定输出角色仍为未资格化物料，不接受签名授信字段。"""
    exact(value, {"role", "material", "mode", "architecture", "code_signature_present"})
    binding = _binding(value["material"])
    require(value["role"] == role and Path(binding.path).name == role and type(value["mode"]) is int
            and value["mode"] == 0o755 and value["architecture"] == "arm64"
            and type(value["code_signature_present"]) is bool, "owned-v2 输出角色非法")
    return binding


def owned_plan_shape(value: Mapping[str, Any]) -> NativeContext:
    """仅核闭集与冻结摘要；运行前另复核实际文件和目录实例。"""
    exact(value, OWNED_PLAN_FIELDS)
    require(value["schema_version"] == OWNED_PLAN_SCHEMA, "owned-v2 plan 版本非法")
    context = owned_context(value["context"])
    require(value["context_sha256"] == context_digest(context)
            and value["build_identity_sha256"] == context.build_identity_sha256, "plan context 或构建身份变化")
    _sha(value["build_identity_sha256"])
    receipt = _binding(value["build_receipt"])
    helper, probe = owned_role(value["helper"], "collector"), owned_role(value["probe_fixture"], "probe_fixture")
    require(Path(helper.path).parent == Path(probe.path).parent == Path(receipt.path).parent
            and len({helper.path, probe.path, receipt.path}) == 3, "两个角色不在同一构建根")
    root = value["bundle_root"]
    require(isinstance(root, str) and Path(root).is_absolute() and ".." not in Path(root).parts
            and str(Path(root)) == root, "bundle 路径不规范")
    exact(value["bundle_instance"], {"dev", "ino", "uid", "mode"})
    for item in value["bundle_instance"].values():
        _uint(item)
    require(value["bundle_instance"]["mode"] == 0o700, "bundle 权限须为 0700")
    _sha(value["plan_sha256"])
    require(value["plan_sha256"] == fingerprint({key: item for key, item in value.items() if key != "plan_sha256"}),
            "plan 摘要不符")
    return context


@dataclass(frozen=True)
class OwnedProbePlan:
    """递归冻结计划；构造类型本身不获得正式来源或启动资格。"""
    document: Mapping[str, Any]

    def __post_init__(self) -> None:
        frozen = freeze(self.document)
        owned_plan_shape(frozen)
        object.__setattr__(self, "document", frozen)


def owned_relative_binding(value: Any) -> Mapping[str, Any]:
    """清单仅引用本 bundle 单层文件，不混用绝对物料路径。"""
    exact(value, {"path", "sha256", "bytes"})
    path = value["path"]
    require(isinstance(path, str) and path not in {"", ".", ".."} and "/" not in path
            and "\\" not in path and not any(ord(char) < 32 for char in path), "bundle 引用非法")
    _sha(value["sha256"])
    _uint(value["bytes"])
    return value


@dataclass(frozen=True)
class OwnedProbeReadResult:
    """未资格化结果的五能力字段在类型边界固定为 false。"""
    document: Mapping[str, Any]

    def __post_init__(self) -> None:
        value = freeze(self.document)
        exact(value, OWNED_RESULT_FIELDS)
        require(value["schema_version"] == OWNED_RESULT_SCHEMA and value["status"] in OWNED_STATUSES
                and value["origin"] in OWNED_ORIGINS, "owned-v2 结果状态非法")
        for name in ("plan_sha256", "context_sha256", "build_identity_sha256"):
            _sha(value[name])
        _id(value["collector_run_id"])
        for name in OWNED_FLAGS:
            require(value[name] is False, "owned-v2 不授完整事件、映像、来源、启动或注册权")
        for name in ("projection", "ready", "closing", "closed", "first_error"):
            if value[name] is not None:
                owned_relative_binding(value[name])
        require(value["projection"] is not None, "结果不能缺 projection 原件")
        _uint(value["record_count"])
        exact(value["observed_event_counts"], {"fork", "exec", "exit"})
        for count in value["observed_event_counts"].values():
            _uint(count)
        reasons = value["unknown_reasons"]
        require(isinstance(reasons, (list, tuple)) and len(set(reasons)) == len(reasons)
                and all(reason in INCOMPLETE_REASONS for reason in reasons), "结果原因非法")
        object.__setattr__(self, "document", value)


def _owned_boot(value: Any) -> str | None:
    """有界 sysctl 原始返回只认候选 UUID+NUL，未知布局保持未知。"""
    import base64
    exact(value, {"attempts", "representation", "boot_session_uuid"})
    require(isinstance(value["attempts"], (list, tuple)) and 0 < len(value["attempts"]) <= 2, "boot 查询次数非法")
    decoded = []
    for attempt in value["attempts"]:
        exact(attempt, {"returncode", "errno", "returned_bytes", "raw_base64"})
        require(type(attempt["returncode"]) is int and attempt["returncode"] in {-1, 0}
                and type(attempt["errno"]) is int and 0 <= attempt["errno"] < 2 ** 31, "boot 原码非法")
        _uint(attempt["returned_bytes"])
        if attempt["raw_base64"] is None:
            require(attempt["returncode"] != 0, "成功 boot 不得缺缓冲")
            continue
        try:
            raw = base64.b64decode(attempt["raw_base64"], validate=True)
        except (ValueError, TypeError) as error:
            raise NativeProcessSourceError("boot base64 非法") from error
        require(len(raw) == attempt["returned_bytes"] and len(raw) <= 64, "boot 长度不符")
        if attempt["returncode"] == 0:
            require(attempt["errno"] == 0, "成功 boot errno 须为零")
            decoded.append(raw)
    boot = value["boot_session_uuid"]
    require(value["representation"] in {"canonical_uuid_string", "unknown"}, "boot 表示非法")
    if value["representation"] == "unknown":
        require(boot is None, "未知 boot 不得填 UUID")
        return None
    require(isinstance(boot, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot)
            and boot != "00000000-0000-0000-0000-000000000000", "boot UUID 非法")
    require(decoded and all(len(raw) == 37 and raw[-1:] == b"\0"
            and raw[:-1].decode("ascii", errors="replace").lower() == boot for raw in decoded), "UUID 未对应原缓冲")
    return boot


def _owned_mute(value: Any) -> bool:
    """扫描计数与摘要仅为未资格化原件，不代表平台无遗漏。"""
    exact(value, {"processes", "paths", "inversion", "query_status"})
    statuses = {"observed_unqualified", "unknown", "failed"}
    require(value["query_status"] in statuses, "mute 状态非法")
    for role in ("processes", "paths"):
        query = value[role]
        exact(query, {"returncode", "reported_count", "examined_count", "table_sha256", "relevant_match_count", "status"})
        require(query["returncode"] is None or type(query["returncode"]) is int, "mute 原码非法")
        require(query["status"] in statuses, "mute 表状态非法")
        for name in ("reported_count", "examined_count", "relevant_match_count"):
            if query[name] is not None:
                _uint(query[name])
        if query["table_sha256"] is not None:
            _sha(query["table_sha256"])
        if query["status"] == "observed_unqualified":
            require(query["returncode"] == 0 and type(query["reported_count"]) is int
                    and query["reported_count"] < 4096 and query["examined_count"] == query["reported_count"]
                    and type(query["relevant_match_count"]) is int
                    and query["relevant_match_count"] <= query["reported_count"]
                    and query["table_sha256"] is not None, "mute 扫描不闭合")
    exact(value["inversion"], {"process", "path", "target_path"})
    require(all(item is None or type(item) is int for item in value["inversion"].values()), "mute inversion 原码非法")
    clear = all(value[name]["status"] == "observed_unqualified" and value[name]["relevant_match_count"] == 0
                for name in ("processes", "paths")) and all(item == 1 for item in value["inversion"].values())
    require(value["query_status"] != "observed_unqualified" or clear, "mute 原码/相关命中与声明矛盾")
    return value["query_status"] == "observed_unqualified" and clear


def validate_owned_probe_projection(plan: OwnedProbePlan, raw: bytes) -> tuple[Mapping[str, Any], ...]:
    """重验原始前缀、过滤前序号、owned 生命周期；结构通过不授信。"""
    require(isinstance(plan, OwnedProbePlan), "plan 类型非法")
    document, context = plan.document, owned_plan_shape(plan.document)
    require(type(raw) is bytes and len(raw) <= context.limits.max_total_bytes, "byte_limit：总字节越界")
    if raw and not raw.endswith(b"\n"):
        raise NativeProcessObservationIncomplete("wal_truncated：原件末尾半行")
    lines = raw.splitlines(keepends=True)
    require(len(lines) <= context.limits.max_records, "record_limit：记录越界")
    frames, watermarks, instances, active = [], {}, {}, {}
    offset, last_global, collector, collector_sha, before_boot = 0, None, None, None, None
    before_observation = None
    before_clear, after_clear = False, False
    phases, observations = set(), set()
    first_error, ended, diagnostic, root_fork_seen = False, False, False, False
    for line in lines:
        require(0 < len(line) <= context.limits.max_record_bytes, "byte_limit：单行越界")
        frame = strict_json(line)
        exact(frame, OWNED_FRAME_FIELDS)
        require(canonical_json(frame) + b"\n" == line and not ended
                and frame["schema_version"] == OWNED_FRAME_SCHEMA
                and frame["collector_run_id"] == context.collector_run_id
                and frame["context_sha256"] == document["context_sha256"]
                and frame["plan_sha256"] == document["plan_sha256"], "规范、上下文或末态变化")
        kind, payload = frame["record_type"], frame["payload"]
        require(kind in OWNED_KINDS, "记录类型非法")
        if kind in {"process_event", "counter_only"}:
            require(type(frame["message_version"]) is int and 4 <= frame["message_version"] <= 8
                    and frame["event_type"] in EVENT_TYPES, "message_version_insufficient／abi_unknown")
            for name in ("global_seq_num", "event_seq_num", "kernel_mach_time"):
                _uint(frame[name])
            _time(frame["kernel_time"], "nanoseconds", 1_000_000_000)
            event = frame["event_type"]
            require(last_global is None or frame["global_seq_num"] == last_global + 1, "global_sequence_gap：全局缺口")
            require(event not in watermarks or frame["event_seq_num"] == watermarks[event] + 1, "type_sequence_gap：事件型缺口")
            last_global, watermarks[event] = frame["global_seq_num"], frame["event_seq_num"]
            if kind == "counter_only":
                exact(payload, set())
            else:
                require(collector is not None and before_clear and not first_error and "closing" not in phases,
                        "relation_unknown：owned 根/准入缺失、首错后或关闭后事件")
                exact(payload, {"actor", "target", "exit_stat"})
                actor, target = payload["actor"], payload["target"]
                key = _process(actor)
                require(key in instances and active.get(actor["pid"]) == key and key[0] == before_boot,
                        "relation_unknown：缺 fork、PID 重用、boot 改变或退出后事件")
                if event != "notify_exec":
                    require(instances[key] == actor, "relation_unknown：未记录执行代次")
                if event == "notify_fork":
                    child = _process(target)
                    require(payload["exit_stat"] is None and child[0] == before_boot and child not in instances
                            and target["pid"] not in active and target["parent_audit_identity"] == {
                                "pid": actor["pid"], "pidversion": actor["pidversion"]}, "relation_unknown：fork 不闭合")
                    require(len(active) < context.limits.max_pending_relations, "relation_unknown：关系达到上界")
                    instances[child], active[target["pid"]] = target, child
                    root_fork_seen |= key == collector
                elif event == "notify_exec":
                    require(payload["exit_stat"] is None and _process(target) == key
                            and target["pidversion"] == actor["pidversion"], "relation_unknown：exec 改实例")
                    instances[key] = target
                else:
                    require(target is None and type(payload["exit_stat"]) is int
                            and -(2 ** 31) <= payload["exit_stat"] < 2 ** 31 and key != collector, "exit 结构非法")
                    del active[actor["pid"]]
                for process in (actor, target):
                    if process is not None:
                        require(process["executable"]["path"] in {document["helper"]["material"]["path"],
                            document["probe_fixture"]["material"]["path"]}, "owned 执行非固定映像")
        else:
            require(all(frame[name] is None for name in ("message_version", "global_seq_num", "event_type",
                "event_seq_num", "kernel_mach_time", "kernel_time")), "非内核帧伪造序号或时间")
            if kind == "diagnostic":
                require(not frames and not diagnostic, "diagnostic 必须独立且唯一")
                diagnostic = True
                exact(payload, {"native_result", "create_result", "delete_result", "boot_observation", "missing_prerequisites"})
                require(payload["native_result"] in NATIVE_RESULTS and payload["native_result"] != "native_entry_disabled",
                        "owned-v2 不接受未接线诊断")
                for name in ("create_result", "delete_result"):
                    require(payload[name] is None or type(payload[name]) is int, "诊断原码非法")
                require(isinstance(payload["missing_prerequisites"], (list, tuple))
                        and all(item in OWNED_MISSING for item in payload["missing_prerequisites"]), "缺件非法")
                _owned_boot(payload["boot_observation"])
            elif kind == "state_observation":
                exact(payload, {"phase", "boot_observation", "mute_observation", "create_result", "subscribe_result"})
                phase = payload["phase"]
                require(phase in {"before", "after"} and phase not in observations and not diagnostic
                        and ((phase == "before" and not phases) or (phase == "after" and "closing" in phases)), "观测阶段非法")
                for name in ("create_result", "subscribe_result"):
                    require(payload[name] is None or type(payload[name]) is int, "观测原码非法")
                boot = _owned_boot(payload["boot_observation"])
                clear = _owned_mute(payload["mute_observation"])
                if phase == "before":
                    before_boot = boot
                    before_observation = payload["boot_observation"]
                    before_clear = clear
                else:
                    require(before_boot == boot or first_error, "boot_changed：变化但无首错")
                    require(clear or first_error, "mute_unknown：关闭观测缺件但没有首错")
                    after_clear = clear
                observations.add(phase)
            elif kind == "collector_instance":
                exact(payload, {"collector_process", "helper", "probe_fixture", "boot_observation"})
                require(collector is None and before_boot is not None and "before" in observations
                        and payload["helper"] == document["helper"] and payload["probe_fixture"] == document["probe_fixture"]
                        and _owned_boot(payload["boot_observation"]) == before_boot
                        and canonical_json(payload["boot_observation"]) == canonical_json(before_observation), "实例前观测或物料非法")
                process = payload["collector_process"]
                collector = _process(process)
                require(collector[0] == before_boot and process["executable"]["path"] == document["helper"]["material"]["path"],
                        "collector 映像非法")
                instances[collector], active[process["pid"]] = process, collector
                collector_sha = fingerprint(payload)
            elif kind == "incomplete":
                exact(payload, {"reason", "first_error_sequence"})
                require(not first_error and payload["reason"] in INCOMPLETE_REASONS, "首错重复或原因非法")
                if payload["first_error_sequence"] is not None:
                    _uint(payload["first_error_sequence"])
                first_error = True
            elif kind in {"barrier", "closed"}:
                fields = {"phase", "type_watermarks", "collector_instance_sha256", "event_prefix"} if kind == "barrier" else {
                    "type_watermarks", "unclosed_instances", "drain_status", "event_prefix", "unsubscribe_result", "delete_result"}
                exact(payload, fields)
                exact(payload["event_prefix"], {"sha256", "bytes", "frame_count"})
                _sha(payload["event_prefix"]["sha256"])
                _uint(payload["event_prefix"]["bytes"])
                _uint(payload["event_prefix"]["frame_count"])
                exact(payload["type_watermarks"], {"fork", "exec", "exit"})
                for number in payload["type_watermarks"].values():
                    if number is not None:
                        _uint(number)
                require(payload["event_prefix"] == {"sha256": hashlib.sha256(raw[:offset]).hexdigest(),
                    "bytes": offset, "frame_count": len(frames)}, "前缀摘要或水位变化")
                require(payload["type_watermarks"] == {name.removeprefix("notify_"): watermarks.get(name)
                    for name in EVENT_TYPES}, "水位不对应原始消息")
                if kind == "barrier":
                    phase = payload["phase"]
                    require(phase in {"ready", "closing"} and phase not in phases and not first_error
                            and collector_sha is not None and root_fork_seen and before_clear and len(watermarks) == 3
                            and payload["collector_instance_sha256"] == collector_sha
                            and ((phase == "ready" and not phases) or (phase == "closing" and "ready" in phases))
                            and set(active.values()) == {collector}, "屏障缺原生事件或子实例未收口")
                    phases.add(phase)
                else:
                    _uint(payload["unclosed_instances"])
                    known_unclosed = len(set(active.values()) - {collector})
                    require(payload["unclosed_instances"] >= known_unclosed
                            and (payload["unclosed_instances"] == known_unclosed or first_error)
                            and payload["drain_status"] in {"unknown", "observed_unqualified", "failed"}, "关闭状态非法")
                    for name in ("unsubscribe_result", "delete_result"):
                        require(payload[name] is None or type(payload[name]) is int, "关闭原码非法")
                    if payload["drain_status"] == "observed_unqualified":
                        require("closing" in phases and "after" in observations and after_clear and not first_error
                                and payload["unclosed_instances"] == 0
                                and payload["unsubscribe_result"] == 0 and payload["delete_result"] == 0, "关闭阶段不闭合")
                    ended = True
        require(not diagnostic or kind in {"diagnostic", "incomplete"}, "诊断不得混入采集帧")
        frames.append(freeze(frame))
        offset += len(line)
    return tuple(frames)
