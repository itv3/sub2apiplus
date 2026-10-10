"""固定 Darwin 产品 helper 的实际进程与原件流水。

编译只生成未签名私有产物。正式监测实例必须来自固定受管安装 authority，
并在启动前重验签名。ES 创建、队列丢失、尾屏障或 socket 样本不完整均保留
原始记录；不会把 helper 返回零、一个 ready 文件或安装收据当作完整来源。
"""

from __future__ import annotations

import hashlib
import copy
import json
import os
import select
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_product_authority as authority_tools


class ProductNativeError(ValueError):
    """原生产品上下文、实际帧或文件实例不符。"""


class ProductNativeUnavailable(ProductNativeError):
    """权限、实际观察或生命周期不完整，不能凭空签来源。"""


def _require(value: Any, message: str) -> None:
    if not value:
        raise ProductNativeError(message)


def _private_directory(path: Path) -> tuple[int, int]:
    c.require_plain_path(path)
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o700, "产品原件目录须为当前用户的 0700 普通目录")
    return info.st_dev, info.st_ino


def _new_file(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            _require(count > 0, "产品原件短写")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_native_frame(value: Any, task: Mapping[str, Any]) -> dict[str, Any]:
    """原生帧按类型核闭集；采样、输入和请求事实不能升级为完整来源。"""
    _require(isinstance(value, dict), "原生帧不是对象")
    kind = value.get("kind")
    identity = {"task_sha256": task["task_sha256"], "run_id": task["execution"]["producer_run_id"]}
    fields = {
        "ready": {"kind", *identity, "boot_id", "observed_at_utc", "accessibility_trusted",
                  "socket_events_complete", "module_activation_verified"},
        "unavailable": {"kind", "reason"},
        "incomplete": {"kind", "reason"},
        "process": {"kind", "event", "process", "observed_at_utc", "global_sequence", "event_sequence"},
        "product_file": {"kind", "process", "path", "device", "inode", "size", "observed_at_utc", "global_sequence", "event_sequence"},
        "product_module_open": {"kind", "process", "path", "device", "inode", "size", "mtime_nanoseconds", "ctime_nanoseconds", "observed_at_utc", "global_sequence", "event_sequence"},
        "socket_snapshot": {"kind", "snapshot"},
        "closed": {"kind", *identity, "observed_at_utc", "close_requested", "unsubscribe_result", "delete_result",
                   "unclosed_instances", "first_error", "drain_status", "socket_events_complete", "module_activation_verified"},
        "origin_attestation": {"kind", "payload_base64", "signature_base64", "public_key_sha256"},
        "product_scope": {"kind", "scope_sha256", "observed_at_utc"},
        "product_state": {"kind", "event", "process", "paths", "file", "requested", "owned", "observed_at_utc",
                          "global_sequence", "event_sequence"},
        "product_state_snapshot": {"kind", "states", "observed_at_utc"},
    }
    _require(kind in fields, "原生帧类型非法")
    names = fields[kind]
    if kind == "ready" and "state_paths" in value:
        names = names | {"state_paths", "state_observation_schema"}
        _require(value["state_observation_schema"] == "claude-mac-product-state-observation/v1"
                 and isinstance(value["state_paths"], list) and 1 <= len(value["state_paths"]) <= 64
                 and len(set(value["state_paths"])) == len(value["state_paths"])
                 and all(isinstance(path, str) and Path(path).is_absolute() and str(Path(path)) == path
                         for path in value["state_paths"]), "原生精确状态订阅清单非法")
    if kind == "process" and value.get("event") == "exec" and "previous_process" in value:
        names = names | {"previous_process"}
    if kind == "product_file" and {"mtime_nanoseconds", "ctime_nanoseconds"} <= set(value):
        names = names | {"mtime_nanoseconds", "ctime_nanoseconds"}
    if kind == "product_file" and "content_sha256" in value:
        names = names | {"content_sha256"}
        _require(isinstance(value["content_sha256"], str) and bool(c.SHA_RE.fullmatch(value["content_sha256"])),
                 "产品关闭原件字节摘要非法")
    if kind == "unavailable" and value.get("reason") == "es_create_failed":
        names = names | {"native_result"}
        _require(type(value["native_result"]) is int, "ES 返回码非法")
    _require(set(value) == names, "原生帧字段闭集不符")
    if kind == "origin_attestation":
        _require(all(isinstance(value[name], str) and 0 < len(value[name]) <= 90_000
                     for name in ("payload_base64", "signature_base64", "public_key_sha256"))
                 and bool(c.SHA_RE.fullmatch(value["public_key_sha256"])), "产品签名尾帧字段非法")
    if kind == "product_scope":
        _require(isinstance(value["scope_sha256"], str) and bool(c.SHA_RE.fullmatch(value["scope_sha256"])), "原生状态 scope 摘要非法")
        c.timestamp(value["observed_at_utc"])
    if kind == "product_state":
        events = {"create", "write", "close", "mmap", "truncate", "unlink", "rename", "setmode", "utimes", "link",
                  "setowner", "setflags", "setattrlist", "setacl", "setextattr", "deleteextattr", "exchangedata", "clone", "copyfile"}
        _require(value["event"] in events and type(value["owned"]) is bool and isinstance(value["paths"], list)
                 and len(value["paths"]) == (2 if value["event"] in {"rename", "link", "exchangedata", "clone", "copyfile"} else 1)
                 and all(isinstance(path, str) and Path(path).is_absolute() and str(Path(path)) == path
                         for path in value["paths"]), "产品状态变化坐标或类型非法")
        validate_native_frame({"kind": "process", "event": "fork", "process": value["process"],
            "observed_at_utc": value["observed_at_utc"], "global_sequence": value["global_sequence"],
            "event_sequence": value["event_sequence"]}, task)
        fact = value["file"]
        if fact is not None:
            _require(isinstance(fact, dict) and set(fact) == {"type", "mode", "mtime_ns", "bytes", "device", "inode", "sha256"}
                and fact["type"] in {"file", "directory", "unsupported"}
                and all(type(fact[name]) is int and fact[name] >= 0 for name in ("mode", "mtime_ns", "bytes", "device", "inode"))
                and (fact["sha256"] is None or isinstance(fact["sha256"], str) and bool(c.SHA_RE.fullmatch(fact["sha256"]))),
                "产品状态变化的内核对象事实非法")
        requested = value["requested"]
        expected = {"mode"} if value["event"] == "setmode" else {"mtime_ns"} if value["event"] == "utimes" else {"unapplied"}
        _require(requested is None or isinstance(requested, dict) and set(requested) == expected, "产品状态请求字段非法")
        if value["event"] in {"setmode", "utimes"}:
            name = next(iter(expected))
            _require(requested is not None and type(requested[name]) is int and requested[name] >= 0, "产品元数据请求值非法")
        elif requested is not None:
            _require(value["event"] == "create" and requested["unapplied"] is True, "未生效操作标记非法")
    if kind == "product_state_snapshot":
        c.timestamp(value["observed_at_utc"])
        _require(isinstance(value["states"], dict) and 1 <= len(value["states"]) <= 4096, "最终原生状态全集非法")
        total = 0
        for path, node in value["states"].items():
            _require(isinstance(path, str) and Path(path).is_absolute() and str(Path(path)) == path
                and isinstance(node, dict) and node.get("type") in {"missing", "file", "directory"}, "最终原生状态对象非法")
            if node["type"] == "missing":
                _require(set(node) == {"type"}, "最终缺失对象字段非法")
                continue
            names = {"type", "mode", "mtime_ns"} | ({"bytes", "sha256"} if node["type"] == "file" else set())
            _require(set(node) == names and type(node["mode"]) is int and 0 <= node["mode"] <= 0o777
                and type(node["mtime_ns"]) is int and node["mtime_ns"] >= 0, "最终原生状态权限或时间非法")
            if node["type"] == "file":
                _require(type(node["bytes"]) is int and 0 <= node["bytes"] <= 8*1024*1024
                         and isinstance(node["sha256"], str) and bool(c.SHA_RE.fullmatch(node["sha256"])), "最终原生状态文件超限或摘要非法")
                total += node["bytes"]
        _require(total <= 32*1024*1024, "最终原生状态总字节超限")
    if kind in {"ready", "closed"}:
        _require(all(value[name] == expected for name, expected in identity.items()), "原生帧任务身份不符")
        _require(value["socket_events_complete"] is False and value["module_activation_verified"] is False,
                 "此 helper 只提供样本，不能自行声明完整 flow 或模块激活")
        c.timestamp(value["observed_at_utc"])
    if kind in {"unavailable", "incomplete"}:
        _require(isinstance(value["reason"], str) and 0 < len(value["reason"]) <= 256, "原生失败原因非法")
    if kind == "ready":
        _require(isinstance(value["boot_id"], str) and bool(c.ID_RE.fullmatch(value["boot_id"]))
                 and type(value["accessibility_trusted"]) is bool, "原生 ready 能力或 boot 非法")
    elif kind in {"process", "product_file", "product_module_open"}:
        c.timestamp(value["observed_at_utc"])
        _require((kind != "process" or value["event"] in {"exec", "fork", "exit"})
                 and all(type(value[name]) is int and value[name] >= 0 for name in ("global_sequence", "event_sequence")),
                 "原生进程事件或序号非法")
        if kind != "process":
            _require(isinstance(value["path"], str) and value["path"].startswith("/")
                     and all(type(value[name]) is int and value[name] >= 0 for name in ("device", "inode", "size")),
                     "产品文件写入坐标非法")
            if "mtime_nanoseconds" in value:
                _require(all(type(value[name]) is int and value[name] >= 0
                             for name in ("mtime_nanoseconds", "ctime_nanoseconds")), "产品原件内核修改时间非法")
        process = value["process"]
        names = {"pid", "ppid", "pidversion", "boot_id", "start_seconds", "start_microseconds", "executable",
                 "cdhash", "device", "inode", "size", "audit_token", "parent_audit_token"}
        _require(isinstance(process, dict) and set(process) == names, "原生进程字段不符")
        _require(all(type(process[name]) is int and process[name] >= 0
                     for name in ("pid", "ppid", "pidversion", "start_seconds", "start_microseconds", "device", "inode", "size"))
                 and process["pid"] > 0 and process["start_seconds"] > 0 and process["start_microseconds"] < 1_000_000,
                 "原生进程启动实例非法")
        _require(isinstance(process["boot_id"], str) and bool(c.ID_RE.fullmatch(process["boot_id"]))
                 and isinstance(process["executable"], str) and process["executable"].startswith("/")
                 and isinstance(process["cdhash"], str) and len(process["cdhash"]) == 40
                 and all(char in "0123456789abcdef" for char in process["cdhash"]), "原生映像身份非法")
        audit = process["audit_token"]
        _require(isinstance(audit, list) and len(audit) == 8 and all(type(number) is int and 0 <= number <= 0xFFFFFFFF for number in audit)
                 and audit[5] == process["pid"] and audit[7] == process["pidversion"], "audit-token 与进程实例不符")
        parent = process["parent_audit_token"]
        _require(isinstance(parent, list) and len(parent) == 8
                 and all(type(number) is int and 0 <= number <= 0xFFFFFFFF for number in parent)
                 and parent[5] == process["ppid"], "父 audit-token 与父 PID 不符")
        if "previous_process" in value:
            previous = validate_native_frame({"kind": "process", "event": "fork", "process": value["previous_process"],
                "observed_at_utc": value["observed_at_utc"], "global_sequence": value["global_sequence"],
                "event_sequence": value["event_sequence"]}, task)["process"]
            _require(all(previous[key] == process[key] for key in
                         ("pid", "boot_id", "start_seconds", "start_microseconds"))
                     and previous["pidversion"] != process["pidversion"], "exec 前后不是同一实际进程的两个执行身份")
    elif kind == "socket_snapshot":
        item = value["snapshot"]
        names = {"pid", "ppid", "boot_id", "start_seconds", "start_microseconds", "executable", "observed_at_utc",
                 "flows", "scope", "socket_events_complete"}
        _require(isinstance(item, dict) and set(item) == names and item["scope"] == "kernel_socket_snapshot"
                 and item["socket_events_complete"] is False and isinstance(item["flows"], list), "socket 样本字段或用途非法")
        c.timestamp(item["observed_at_utc"])
        _require(all(type(item[name]) is int and item[name] >= 0 for name in ("pid", "ppid", "start_seconds", "start_microseconds"))
                 and item["pid"] > 0 and item["start_seconds"] > 0 and item["start_microseconds"] < 1_000_000
                 and isinstance(item["boot_id"], str) and bool(c.ID_RE.fullmatch(item["boot_id"]))
                 and isinstance(item["executable"], str) and item["executable"].startswith("/"), "socket 样本进程实例非法")
        _require(len(item["flows"]) <= 8192, "socket 样本过大")
        for flow in item["flows"]:
            _require(isinstance(flow, dict) and set(flow) == {"fd", "socket_handle", "family", "local_host", "local_port",
                     "peer_host", "peer_port", "tcp_state"}, "socket 样本连接字段不符")
            _require(all(type(flow[name]) is int and flow[name] >= 0 for name in ("fd", "socket_handle", "family", "local_port", "peer_port", "tcp_state"))
                     and flow["local_port"] <= 65535 and flow["peer_port"] <= 65535
                     and all(isinstance(flow[name], str) and 0 < len(flow[name]) <= 64 for name in ("local_host", "peer_host")),
                     "socket 连接样本非法")
    elif kind == "closed":
        _require(type(value["close_requested"]) is bool and value["drain_status"] == "observed_unqualified"
                 and all(type(value[name]) is int and value[name] >= 0 for name in ("unsubscribe_result", "delete_result", "unclosed_instances"))
                 and (value["first_error"] is None or isinstance(value["first_error"], str)), "原生收口事实非法")
    return copy.deepcopy(value)


def owned_process_lifecycles(frames: Any, task: Mapping[str, Any]) -> dict[tuple[int, ...], dict[str, Any]]:
    """按真实 exec replacement 闭合执行身份；不会把 exec 当成虚构 exit。

    fork 的旧 token 与 exec 的新 token 对应同一 PID、同一 fork 启动时刻。
    旧身份在实际 exec 边界停止当前执行；只有真正的 exit 能证明进程退出。
    """
    histories: dict[tuple[int, ...], dict[str, Any]] = {}
    current: dict[int, tuple[int, ...]] = {}
    for original in frames:
        if original.get("kind") != "process":
            continue
        frame = validate_native_frame(dict(original), task)
        value, event = frame["process"], frame["event"]
        token, pid = tuple(value["audit_token"]), value["pid"]
        if event == "exit":
            _require(current.get(pid) == token and token in histories, "真实 exit 没有相同执行身份的启动")
            histories[token].update(end=frame, end_reason="exit")
            current.pop(pid)
            continue
        previous = current.get(pid)
        if previous is not None and previous != token:
            _require(event == "exec" and "previous_process" in frame
                     and tuple(frame["previous_process"]["audit_token"]) == previous
                     and all(frame["previous_process"][key] == histories[previous]["start"]["process"][key]
                             for key in ("pid", "boot_id", "start_seconds", "start_microseconds", "executable")),
                     "执行身份替换没有内核 exec 前后原件，不能按 PID 猜")
            histories[previous].update(end=frame, end_reason="exec_replaced")
        if token in histories:
            _require(previous == token and event == "exec" and histories[token]["start"]["event"] == "fork",
                     "同执行身份重复启动或在结束后再次出现")
            histories[token]["start"] = frame
        else:
            histories[token] = {"start": frame, "end": None, "end_reason": None}
        current[pid] = token
    return histories


def build_product_observer(output_root: Path) -> dict[str, Any]:
    """实际编译固定源码；不签名、不启动、不授 ES／UI／正式来源资格。"""
    root = Path(output_root)
    _private_directory(root)
    _require(not list(root.iterdir()), "原生构建根须为空，不能覆盖旧产物")
    source_path = Path(__file__).with_suffix(".m")
    raw = authority_tools.read_original(source_path)
    _new_file(root / source_path.name, raw)
    sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, check=True, timeout=30).stdout.decode().strip()
    compiler = subprocess.run(["/usr/bin/xcrun", "--find", "clang"], capture_output=True, check=True, timeout=30).stdout.decode().strip()
    executable = root / "claude-product-events"
    argv = [compiler, "-isysroot", sdk, "-arch", "arm64", "-mmacosx-version-min=15.0", "-fobjc-arc", "-fblocks",
            "-framework", "Foundation", "-framework", "ApplicationServices", "-framework", "Security", "-lEndpointSecurity", "-lbsm", "-lproc",
            "-O2", str(root / source_path.name), "-o", str(executable)]
    built = subprocess.run(argv, capture_output=True, timeout=120, check=False)
    _new_file(root / "build.stdout", built.stdout)
    _new_file(root / "build.stderr", built.stderr)
    _require(built.returncode == 0, "产品原生 helper 编译失败，原始诊断已保留")
    image = authority_tools.read_original(executable)
    result = {"schema_version": "claude-mac-product-build/v1", "source": c.binding(root / source_path.name, root),
              "executable": c.binding(executable, root), "argv": argv, "compiler": compiler, "sdk": sdk,
              "returncode": built.returncode, "source_scope": "private_unsigned_build",
              "signed": False, "executed": False, "formal_ready": False, "launch_allowed": False,
              "registration_verified": False}
    _require(authority_tools.read_original(source_path) == raw and hashlib.sha256(image).hexdigest()
             == result["executable"]["sha256"], "原生构建期间源码或产物发生变化")
    _new_file(root / "build.json", c.canonical(result))
    return result


class DarwinProductMonitor:
    """真实固定 helper 会话；构造纯只读，start 才启动且先重验 authority。"""

    def __init__(self, task: Mapping[str, Any], *, bundle_root: Path,
                 authority: authority_tools.ProductInstallationAuthority, path_context: Any = None) -> None:
        self.task = copy.deepcopy(source.validate_task_v2(task))
        self.root = Path(bundle_root)
        self.instance = _private_directory(self.root)
        _require(isinstance(authority, authority_tools.ProductInstallationAuthority), "缺固定安装 authority")
        self.authority = authority
        self._path_context = path_context
        self.process: subprocess.Popen | None = None
        self.records: list[dict[str, Any]] = []
        self._condition = threading.Condition()
        self._failure: BaseException | None = None
        self._eof = False
        self._closed = False
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self._clock_deadline = 0.0
        self._cleanup_deadline: float | None = None
        self.origin: dict[str, Any] | None = None

    def _remaining(self) -> float:
        if self._cleanup_deadline is not None:
            value = self._cleanup_deadline - time.monotonic()
            if value <= 0:
                raise ProductNativeUnavailable("原生回收排空期限已耗尽；不允许重新派发")
            return value
        deadline = c.timestamp(self.task["execution"]["request_policy"]["deadline_utc"])
        value = min(self._clock_deadline - time.monotonic(), (deadline - datetime.now(timezone.utc)).total_seconds())
        if value <= 0:
            raise ProductNativeUnavailable("产品观察器期限已耗尽，不重算、不重启")
        return value

    def start(self) -> dict[str, Any]:
        _require(self.process is None and not self._closed, "同产品监测实例不能重启")
        deployment = self.task["execution"]["source_deployments"]["mac"]
        self.authority.revalidate(deployment)
        from tools.official_client_capture import claude_mac_product_broker as broker_tools
        broker = broker_tools.load_product_broker(self.authority)
        broker.revalidate(deployment)
        _require(_private_directory(self.root) == self.instance, "产品来源根实例变化")
        application = next(row for row in self.task["client_artifacts"] if row["role"] == "application_executable")
        module = next((row for row in self.task["client_artifacts"] if row["role"] == "extension_entry"), None)
        recipe = c.read_json(c.bound_file(self.root, self.task["execution"]["isolation_recipe"]))
        from tools.official_client_capture import claude_mac_product_recipe as recipes
        session_root = recipes.physical_session_root(self.task, recipe, self.root, self._path_context)
        self._clock_deadline = time.monotonic() + self.task["execution"]["timeout_seconds"]
        self._remaining()
        self._fd = os.open(self.root / "native-events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            self.process = subprocess.Popen([str(broker.executable), "--product-root-client-v1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=self.root, env={}, close_fds=True)
            request = {"schema_version": "claude-mac-product-native-request/v1", "task_sha256": self.task["task_sha256"],
                       "run_id": self.task["execution"]["producer_run_id"],
                       "deployment": self.task["execution"]["source_deployments"]["mac"],
                       "user_id": os.geteuid(),
                       "launcher_pid": os.getpid(),
                       "product_log_root": str(session_root / "config" / "projects"),
                       "product_user_data_root": str(session_root / "user_data"),
                       "product_module_path": module["content"]["path"] if module else None,
                       "application_executable": application["content"]["path"],
                       "state_paths": sorted(str(path) for path in recipes.material_state_paths(
                           self.task, recipe, self.root, self._path_context)),
                       "timeout_seconds": min(self.task["execution"]["timeout_seconds"], self._remaining()), "max_records": 65532}
            self._send(request)
            self._thread = threading.Thread(target=self._read, name="claude-product-observations", daemon=True)
            self._thread.start()
            ready = self.wait_for("ready")
            _require(ready["task_sha256"] == self.task["task_sha256"] and ready["run_id"] == request["run_id"],
                     "原生 ready 任务身份不符")
            return ready
        except BaseException:
            # 不用 kill 掩盖未收口状态；关闭控制 pipe 让 helper 自行落失败尾记录。
            if self.process is not None and self.process.stdin is not None:
                self.process.stdin.close()
            if self._thread is None and self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise

    def _send(self, value: Mapping[str, Any]) -> None:
        _require(self.process is not None and self.process.stdin is not None, "缺原控制通道")
        raw = c.canonical(value) + b"\n"
        fd = self.process.stdin.fileno()
        os.set_blocking(fd, False)
        offset = 0
        while offset < len(raw):
            _, writable, _ = select.select([], [fd], [], min(self._remaining(), 0.1))
            if writable:
                try:
                    count = os.write(fd, raw[offset:])
                except BlockingIOError:
                    continue
                _require(count > 0, "原生控制通道短写")
                offset += count

    def _read(self) -> None:
        size = 0
        pending = bytearray()
        try:
            _require(self.process is not None and self.process.stdout is not None and self._fd is not None,
                     "原生读侧未初始化")
            output_fd = self.process.stdout.fileno()
            os.set_blocking(output_fd, False)
            while True:
                readable, _, _ = select.select([output_fd], [], [], min(self._remaining(), 0.1))
                if not readable:
                    continue
                try:
                    raw = os.read(output_fd, 65536)
                except BlockingIOError:
                    continue
                if not raw:
                    _require(not pending, "原生输出尾行截断")
                    break
                size += len(raw)
                _require(size <= 67_108_864, "原生输出总量超限")
                # 原始字节先落盘；解析失败也保留真实失败帧，不能丢掉坏原件。
                view = memoryview(raw)
                while view:
                    count = os.write(self._fd, view)
                    _require(count > 0, "原生 WAL 短写")
                    view = view[count:]
                os.fsync(self._fd)
                pending.extend(raw)
                while b"\n" in pending:
                    position = pending.index(10)
                    _require(position < 1_048_576, "原生单帧超限")
                    line = bytes(pending[:position])
                    del pending[:position + 1]
                    value = validate_native_frame(json.loads(line), self.task)
                    with self._condition:
                        _require(len(self.records) < 65536, "原生观测记录超限")
                        _require(not self.records or self.records[-1]["kind"] != "origin_attestation", "原生签名后仍有帧")
                        _require(not self.records or self.records[-1]["kind"] != "closed"
                                 or value["kind"] == "origin_attestation", "原生关闭后仍有非签名帧")
                        self.records.append(value)
                        self._condition.notify_all()
                _require(len(pending) < 1_048_576, "原生未终止单帧超限")
        except BaseException as error:
            with self._condition:
                self._failure = error
            if self.process is not None and self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        finally:
            if self.process is not None and self.process.stdout is not None:
                self.process.stdout.close()
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            with self._condition:
                self._eof = True
                self._condition.notify_all()

    def wait_for(self, kind: str, *, pid: int | None = None) -> dict[str, Any]:
        with self._condition:
            while True:
                for row in self.records:
                    if row["kind"] in {"unavailable", "incomplete"}:
                        raise ProductNativeUnavailable("原生产品观察失败：" + str(row["reason"]))
                    if row["kind"] == kind and (pid is None or row.get("process", {}).get("pid") == pid):
                        return dict(row)
                if self._failure is not None:
                    raise self._failure
                if self._eof:
                    raise ProductNativeUnavailable("原生观察器已结束但缺所需 " + kind)
                self._condition.wait(min(self._remaining(), 0.1))

    def recheck(self) -> dict[str, Any]:
        self.authority.revalidate(self.task["execution"]["source_deployments"]["mac"])
        _require(_private_directory(self.root) == self.instance, "产品原件根被替换")
        self._remaining()
        _require(not self._closed and not self._eof and self._failure is None and self.process is not None
                 and self.process.poll() is None, "产品原生观察器已丢失")
        return self.wait_for("ready")

    def authorize_state_scope(self, scope_sha256: str) -> dict[str, Any]:
        """固定工厂已 fsync 事前 scope 后，取得原生签名流中的准确确认。"""
        self.recheck()
        _require(bool(c.SHA_RE.fullmatch(scope_sha256)) and not any(row["kind"] == "product_scope" for row in self.records),
                 "同一原监测不能替换状态 scope")
        self._send({"operation": "authorize_state", "task_sha256": self.task["task_sha256"], "scope_sha256": scope_sha256})
        value = self.wait_for("product_scope")
        _require(value["scope_sha256"] == scope_sha256, "原生 scope 确认不是事前准确摘要")
        return value

    def begin_cleanup(self) -> None:
        """回收期限只设一次；不能用此窗口再次派发产品或场景。"""
        if self._cleanup_deadline is None:
            self._cleanup_deadline = time.monotonic() + 30

    def close(self) -> dict[str, Any]:
        _require(self.process is not None, "从未启动的实例不能造关闭事实")
        if not self._closed:
            # 回收窗口只排空／关闭，不延长任务或请求 deadline。
            self.begin_cleanup()
            self._closed = True
            self._send({"operation": "close", "task_sha256": self.task["task_sha256"]})
            if self.process.stdin is not None:
                self.process.stdin.close()
        value = self.wait_for("closed")
        if self._thread is not None:
            self._thread.join(self._remaining())
            _require(not self._thread.is_alive(), "原生输出未排空")
        _require(self.process.wait(timeout=self._remaining()) == 0, "原生观察器失败退出，原件不能按成功消费")
        _require(value["task_sha256"] == self.task["task_sha256"]
                 and value["run_id"] == self.task["execution"]["producer_run_id"], "关闭事实任务不符")
        from tools.official_client_capture import claude_mac_product_origins as product_origins
        self.origin = product_origins.read_native_original(self.task, self.root, self.authority)
        path = self.root / "product-native-attestation.json"
        raw = c.canonical(self.origin["attestation"])
        if path.exists():
            _require(authority_tools.read_original(path) == raw, "原生签名收口重入发生字节冲突")
        else:
            _new_file(path, raw)
        return value
