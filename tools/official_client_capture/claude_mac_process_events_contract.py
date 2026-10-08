"""Darwin 原生进程投影的内部闭集合同；结构通过不等于实际内核来源。

本版只验证离线原件。唯一 boot、mute、原生屏障及排空尚未现场资格化，
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
