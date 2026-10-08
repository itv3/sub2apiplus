"""读取 Darwin 内核的进程身份快照，不授予产品启动或完整事件来源权。

本模块只调用 libproc 与只读 sysctl，不读取进程 argv、环境、应用状态或凭据。
同一次快照前后重读 PID、启动标记、父 PID、UID、路径及 kernel boot time；
读取不稳定、短读、权限不足或 ABI 不符均拒绝。快照不能证明中间没有 exec，
不能代替完整 exec/fork/exit 事件、实际执行映像、唯一 boot session 或来源登记。
"""

from __future__ import annotations

import ctypes
import os
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA = "claude-mac-process-snapshot/v1"
_PROC_PIDTBSDINFO = 3
_PROC_PIDPATHINFO_MAXSIZE = 4096
_SNAPSHOT_FIELDS = frozenset({
    "schema_version", "pid", "ppid", "uid", "start_time_seconds", "start_time_microseconds",
    "kernel_boot_time", "executable_path", "observed_at_utc", "source_scope",
    "registration_verified", "formal_ready", "launch_allowed", "process_events_complete",
    "executed_image_verified",
})
_IDENTITY_FIELDS = (
    "pid", "ppid", "uid", "start_time_seconds", "start_time_microseconds",
    "kernel_boot_time", "executable_path",
)


class ProcessSnapshotError(ValueError):
    """显式进程参数非法、快照格式非法或身份已经变化。"""


class ProcessSnapshotUnavailable(ProcessSnapshotError):
    """原生读取无法闭合；调用者应保持未知，不能用空值或启动权替代。"""


class _ProcBSDInfo(ctypes.Structure):
    """按 MacOSX15.5 SDK 的 proc_bsdinfo 固定 LP64 字段布局。"""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _Timeval(ctypes.Structure):
    """Darwin LP64 timeval：time_t 为 64 位，suseconds_t 为 32 位。"""

    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_usec", ctypes.c_int32)]


@dataclass(frozen=True)
class _NativeBindings:
    """仅保存三项固定只读系统函数，不接受调用方路径或动态 handler。"""

    proc_pidinfo: Any
    proc_pidpath: Any
    sysctlbyname: Any


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ProcessSnapshotError(message)


def _validate_abi() -> None:
    """先核宿主、字段偏移与长度；不能把未知布局传给原生函数。"""
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "x86_64"}:
        raise ProcessSnapshotUnavailable("只读进程快照仅支持已核布局的 Darwin arm64/x86_64")
    expected = {
        "pbi_pid": 12, "pbi_ppid": 16, "pbi_uid": 20,
        "pbi_comm": 48, "pbi_name": 64, "pbi_nice": 116,
        "pbi_start_tvsec": 120, "pbi_start_tvusec": 128,
    }
    if (ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_long) != 8
            or ctypes.sizeof(_ProcBSDInfo) != 136 or ctypes.alignment(_ProcBSDInfo) != 8
            or ctypes.sizeof(_Timeval) != 16 or _Timeval.tv_usec.offset != 8
            or any(getattr(_ProcBSDInfo, name).offset != offset for name, offset in expected.items())):
        raise ProcessSnapshotUnavailable("Darwin 原生结构 ABI 与已核 SDK 布局不符")


def _load_native() -> _NativeBindings:
    """从固定系统库取只读函数；不探测产品、不读取用户环境或配置。"""
    _validate_abi()
    try:
        proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        info, path, boot = proc.proc_pidinfo, proc.proc_pidpath, system.sysctlbyname
        info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        info.restype = ctypes.c_int
        path.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        path.restype = ctypes.c_int
        boot.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                         ctypes.c_void_p, ctypes.c_size_t]
        boot.restype = ctypes.c_int
        return _NativeBindings(info, path, boot)
    except (OSError, AttributeError) as error:
        raise ProcessSnapshotUnavailable("固定 Darwin 只读系统函数不可用") from error


def _native_failure(label: str) -> ProcessSnapshotUnavailable:
    """保留明确系统错误，不把 ESRCH／EPERM 等误解为零进程或零流量。"""
    code = ctypes.get_errno()
    return ProcessSnapshotUnavailable(f"{label}不可读，errno={code}：{os.strerror(code)}")


def _read_boot(bindings: _NativeBindings) -> dict[str, int]:
    value, length = _Timeval(), ctypes.c_size_t(ctypes.sizeof(_Timeval))
    ctypes.set_errno(0)
    returned = bindings.sysctlbyname(b"kern.boottime", ctypes.byref(value), ctypes.byref(length), None, 0)
    if returned != 0:
        raise _native_failure("内核启动时间")
    if length.value != ctypes.sizeof(_Timeval):
        raise ProcessSnapshotUnavailable("内核启动时间短读或 ABI 长度不符")
    if value.tv_sec <= 0 or not 0 <= value.tv_usec < 1_000_000:
        raise ProcessSnapshotUnavailable("内核启动时间值非法")
    return {"seconds": int(value.tv_sec), "microseconds": int(value.tv_usec)}


def read_kernel_boot_time() -> dict[str, int]:
    """只读 kernel boot time；它不是唯一 boot session ID，也不授正式权。"""
    return _read_boot(_load_native())


def _read_bsd(bindings: _NativeBindings, pid: int) -> dict[str, int]:
    value = _ProcBSDInfo()
    size = ctypes.sizeof(value)
    ctypes.set_errno(0)
    returned = bindings.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(value), size)
    if returned <= 0:
        raise _native_failure("进程内核身份")
    if returned != size:
        raise ProcessSnapshotUnavailable("进程内核身份短读或 ABI 长度不符")
    if (value.pbi_pid != pid or value.pbi_ppid > 2_147_483_647
            or value.pbi_start_tvsec <= 0 or not 0 <= value.pbi_start_tvusec < 1_000_000):
        raise ProcessSnapshotUnavailable("进程内核身份返回值非法或 PID 已变化")
    return {"pid": int(value.pbi_pid), "ppid": int(value.pbi_ppid), "uid": int(value.pbi_uid),
            "start_time_seconds": int(value.pbi_start_tvsec),
            "start_time_microseconds": int(value.pbi_start_tvusec)}


def _read_path(bindings: _NativeBindings, pid: int) -> str:
    # 非零哨兵使缺失 NUL 的短读可见，不能靠零初始化替内核补终止符。
    value = ctypes.create_string_buffer(b"\xff" * _PROC_PIDPATHINFO_MAXSIZE,
                                       _PROC_PIDPATHINFO_MAXSIZE)
    ctypes.set_errno(0)
    returned = bindings.proc_pidpath(pid, value, len(value))
    if returned <= 0:
        raise _native_failure("进程 executable 路径")
    if returned >= len(value):
        raise ProcessSnapshotUnavailable("进程 executable 路径截断或长度非法")
    raw = value.raw
    end = raw.find(b"\0")
    if end <= 0 or returned not in {end, end + 1}:
        raise ProcessSnapshotUnavailable("进程 executable 路径短读或缺少实际终止符")
    try:
        path = raw[:end].decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ProcessSnapshotUnavailable("进程 executable 路径编码非法") from error
    if not path.startswith("/") or any(ord(char) < 32 for char in path):
        raise ProcessSnapshotUnavailable("进程 executable 路径不是明确绝对路径")
    return path


def read_process_snapshot(pid: int) -> dict[str, Any]:
    """读取一个明确 PID 的稳定内核样本，不枚举产品或读取 argv／环境。

    两次内核身份和两次路径必须相等，并核两次 boot time。即使相等也仅是
    读时样本；不能声称全过程没有 exec、未知子进程、丢失 flow 或实际 POST。
    """
    _require(type(pid) is int and 0 < pid <= 2_147_483_647, "PID 须为正的有界整数，不能用布尔值")
    bindings = _load_native()
    boot_before = _read_boot(bindings)
    before = _read_bsd(bindings, pid)
    path_before = _read_path(bindings, pid)
    after = _read_bsd(bindings, pid)
    path_after = _read_path(bindings, pid)
    boot_after = _read_boot(bindings)
    if before != after or path_before != path_after or boot_before != boot_after:
        raise ProcessSnapshotUnavailable("进程身份、路径或内核启动时间在读取期间变化")
    return {"schema_version": SCHEMA, **after, "kernel_boot_time": boot_after,
            "executable_path": path_after,
            "observed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_scope": "readonly_diagnostic", "registration_verified": False,
            "formal_ready": False, "launch_allowed": False,
            "process_events_complete": False, "executed_image_verified": False}


def _validate_snapshot(value: Mapping[str, Any]) -> None:
    _require(isinstance(value, Mapping) and set(value) == _SNAPSHOT_FIELDS, "进程快照字段闭集非法")
    _require(value["schema_version"] == SCHEMA and value["source_scope"] == "readonly_diagnostic",
             "进程快照 schema／来源范围非法")
    for field in ("registration_verified", "formal_ready", "launch_allowed",
                  "process_events_complete", "executed_image_verified"):
        _require(value[field] is False, "只读快照不能自报来源、启动、事件或执行映像权")
    _require(type(value["pid"]) is int and 0 < value["pid"] <= 2_147_483_647, "快照 PID 非法")
    _require(type(value["ppid"]) is int and 0 <= value["ppid"] <= 2_147_483_647, "快照父 PID 非法")
    _require(type(value["uid"]) is int and 0 <= value["uid"] <= 4_294_967_295, "快照 UID 非法")
    _require(type(value["start_time_seconds"]) is int and value["start_time_seconds"] > 0,
             "快照启动秒非法")
    _require(type(value["start_time_microseconds"]) is int and 0 <= value["start_time_microseconds"] < 1_000_000,
             "快照启动微秒非法")
    boot = value["kernel_boot_time"]
    _require(isinstance(boot, Mapping) and set(boot) == {"seconds", "microseconds"}
             and type(boot["seconds"]) is int and boot["seconds"] > 0
             and type(boot["microseconds"]) is int and 0 <= boot["microseconds"] < 1_000_000,
             "快照内核启动时间非法")
    path = value["executable_path"]
    _require(isinstance(path, str) and path.startswith("/") and not any(ord(char) < 32 for char in path),
             "快照 executable 路径非法")
    try:
        at = datetime.fromisoformat(value["observed_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ProcessSnapshotError("快照观测时间非法") from error
    _require(at.tzinfo is not None and at.utcoffset().total_seconds() == 0, "快照观测时间须带 UTC 时区")


def verify_same_process(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> None:
    """核对两个严格只读样本；通过仍不授予 kill、启动或正式参与证明。"""
    _validate_snapshot(expected)
    _validate_snapshot(observed)
    _require(all(expected[field] == observed[field] for field in _IDENTITY_FIELDS),
             "进程 PID／父 PID／UID／启动／boot time／路径与原样本不同")
