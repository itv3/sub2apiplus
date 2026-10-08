"""Claude 隔离候选交付的独立恢复守卫。

守卫管理单次物理操作，不替代四阶段或 dr 编排。正式服务通过固定私有
Unix 通道接收冻结请求；物理操作始终由守卫执行。工作进程只持有租约，
强杀、通道丢失、非法观测或复核失败都会触发固定可信回退。

日志、执行意图、物理终态及终态前缀分别只写一次。提交后的 Store 发布
由调用方 outbox 处理，不能重新执行物理动作。正式登记仍由受管源码另行
完成；本模块不会根据 JSON 中的 formal 字样授予来源资格。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, parse_json_bytes
from tools.official_client_control import profile_observation_bindings as profile_observations

PROTOCOL = "claude-upgrade-delivery-guard-rpc/v1"
JOURNAL_SCHEMA = "claude-upgrade-delivery-journal/v1"
INDEX_SCHEMA = "claude-upgrade-delivery-operations-index/v1"
PREPARED_SCHEMA = "claude-upgrade-delivery-prepared/v1"
INTENT_SCHEMA = "claude-upgrade-delivery-physical-intent/v1"
TERMINAL_SCHEMA = "claude-upgrade-delivery-guard-terminal/v1"
CORE_SCHEMA = "claude-delivery-stage-execution/v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
STAGE_CHECKS = {
    "candidate_active": ("entry-routing", "health", "image-identity", "smoke"),
    "rollback_verified": ("auth", "data", "dependencies", "health", "image-identity"),
    "candidate_restored": ("entry-routing", "health", "image-identity", "profile"),
    "stable_observed": ("data", "identity", "release-isolation", "safety", "state"),
}
OBSERVATION_FIELDS = frozenset({
    "stage", "target", "actual", "environment", "checks", "observed_seconds",
    "live_request_count", "request_records", "before_bindings", "during_bindings",
    "after_bindings", "result",
})
PREPARE_FIELDS = frozenset({
    "stage", "run_nonce", "plan_ref", "configuration_ref", "stable_projection_ref",
    "probe_recipe_ref", "environment",
})
TERMINAL_STATES = {"execution_committed", "restored", "restore_failed", "recovery_pending"}
MAX_RPC_BYTES = 64 * 1024 * 1024


class GuardError(RuntimeError):
    """守卫拒绝、现场未决或原件不完整。"""


def exact(value: Any, fields: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise GuardError(f"{label} 字段不闭合")
    return value


def relative_path(value: Any, label: str = "路径") -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise GuardError(f"{label} 非法")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise GuardError(f"{label} 必须是规范受管相对路径")
    if any(part.lower() in {"production", "production_active", "production_candidate", "production_previous", "releases", "release"} for part in path.parts):
        raise GuardError(f"{label} 不得指向生产 selector 或 Release 路径")
    return value


def managed_path(root: Path, relative: Any) -> Path:
    relative_path(relative)
    if not root.is_absolute() or root.is_symlink():
        raise GuardError("受管根必须为非符号链接的绝对路径")
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise GuardError(f"受管路径包含符号链接：{relative}")
    return path


def private_directory(path: Path) -> None:
    """创建私有目录并核实际所有者，不能修权限掩盖外部目录漂移。"""
    if not path.is_absolute():
        raise GuardError("私有目录必须是绝对路径")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise GuardError("私有目录祖先包含符号链接")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        sync_directory(directory.parent)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise GuardError(f"私有目录所有者或权限非法：{path}")


def read_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise GuardError("原件不得为符号链接")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise GuardError(f"原件不可读：{path}") from error
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise GuardError(f"原件不是当前所有者的普通文件：{path}")
        return stream.read()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = parse_json_bytes(read_bytes(path), str(path))
    except Exception as error:
        raise GuardError(f"守卫原件 JSON 非法：{path}") from error
    if not isinstance(value, dict):
        raise GuardError(f"守卫原件必须是对象：{path}")
    return value


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_once(path: Path, content: bytes) -> None:
    private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if read_bytes(path) != content:
            raise GuardError(f"不可变原件发生漂移：{path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def write_json_once(path: Path, value: Any) -> None:
    write_once(path, canonical_json_bytes(value))


def atomic_json(path: Path, value: Any) -> None:
    """只用于权威索引；操作日志与终态始终不可覆盖。"""
    private_directory(path.parent)
    temporary = path.parent / (".index-" + secrets.token_hex(12))
    write_json_once(temporary, value)
    try:
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def binding(root: Path, path: Path) -> dict[str, Any]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as error:
        raise GuardError("绑定原件越出 data_root") from error
    managed_path(root, relative)
    content = read_bytes(path)
    return {"path": relative, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def check_binding(root: Path, value: Any) -> Path:
    parsed = exact(value, {"path", "sha256", "bytes"}, "文件绑定")
    if not isinstance(parsed["sha256"], str) or not SHA256.fullmatch(parsed["sha256"]) or parsed["sha256"] == "0" * 64:
        raise GuardError("文件绑定摘要非法")
    if not isinstance(parsed["bytes"], int) or isinstance(parsed["bytes"], bool) or parsed["bytes"] < 0:
        raise GuardError("文件绑定长度非法")
    path = managed_path(root, parsed["path"])
    if binding(root, path) != parsed:
        raise GuardError(f"文件绑定漂移：{parsed['path']}")
    return path


def object_ref(value: Any, kind: str, label: str) -> dict[str, str]:
    parsed = exact(value, {"object_kind", "sha256"}, label)
    if parsed["object_kind"] != kind or not isinstance(parsed["sha256"], str) or not SHA256.fullmatch(parsed["sha256"]) or parsed["sha256"] == "0" * 64:
        raise GuardError(f"{label} 引用非法")
    return parsed


def all_bindings(value: Any) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if set(node) == {"path", "sha256", "bytes"}:
                previous = found.get(str(node["path"]))
                if previous is not None and previous != node:
                    raise GuardError("同一原件绑定了不同字节")
                found[str(node["path"])] = dict(node)
            else:
                for child in node.values():
                    visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
    visit(value)
    return [found[key] for key in sorted(found)]


def process_identity(pid: int) -> dict[str, Any]:
    """PID 必须同时绑定启动标记和本次宿主启动标记。"""
    try:
        proc = Path("/proc")
        if proc.is_dir():
            boot = (proc / "sys/kernel/random/boot_id").read_text().strip()
            fields = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                raise OSError("进程已退出")
            start = fields[19]
            group = int(fields[2])
        else:
            output = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid="], check=True, capture_output=True, text=True).stdout.strip()
            parts = output.rsplit(None, 1)
            if len(parts) != 2:
                raise OSError("进程不存在")
            start, group = parts[0], int(parts[1])
            boot = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], check=True, capture_output=True, text=True).stdout.strip()
        return {"boot_id": boot, "pid": pid, "process_start": start, "process_group_id": group}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise GuardError("进程实例已消失或启动身份不可读") from error


def owner_alive(owner: Mapping[str, Any]) -> bool:
    try:
        return process_identity(int(owner["pid"])) == dict(owner)
    except (GuardError, KeyError, TypeError, ValueError):
        return False


def validate_observation(raw: Any, prepared: Mapping[str, Any], data_root: Path) -> dict[str, Any]:
    value = exact(raw, OBSERVATION_FIELDS, "阶段观测")
    if value["stage"] != prepared["request"]["stage"] or value["target"] != prepared["target"] or value["result"] not in {"pass", "failed"}:
        raise GuardError("阶段观测坐标或结果非法")
    if not isinstance(value["observed_seconds"], int) or isinstance(value["observed_seconds"], bool) or value["observed_seconds"] < 0:
        raise GuardError("阶段实测时长非法")
    if value["stage"] != "stable_observed" and value["observed_seconds"] != 0:
        raise GuardError("非观察窗阶段observed_seconds必须为零")
    if value["environment"] is None and value["result"] == "pass":
        raise GuardError("通过阶段不能缺实测环境")
    if value["live_request_count"] != 0 or isinstance(value["live_request_count"], bool):
        raise GuardError("交付只允许零模型请求")
    actual = exact(value["actual"], {"image", "profile"}, "实测身份")
    for name, expected in (("image", prepared["target"]["image_digest"]), ("profile", prepared["target"]["runtime_profile_sha256"])):
        item = actual[name]
        if isinstance(item, dict) and item.get("state") == "known":
            exact(item, {"state", "value", "direct_binding"}, "已知实测身份")
            check_binding(data_root, item["direct_binding"])
            if value["result"] == "pass" and item["value"] != expected:
                raise GuardError("通过观测的实测身份与目标不符")
        elif isinstance(item, dict) and item.get("state") == "unknown":
            exact(item, {"state", "reason"}, "未知实测身份")
            if value["result"] == "pass" or not isinstance(item["reason"], str) or not item["reason"]:
                raise GuardError("未知实测身份不能通过")
        else:
            raise GuardError("实测身份必须为 known 或 unknown")
    rows = value["checks"]
    if not isinstance(rows, list) or {row.get("id") for row in rows if isinstance(row, dict)} != set(STAGE_CHECKS[value["stage"]]) or len(rows) != len(STAGE_CHECKS[value["stage"]]):
        raise GuardError("阶段检查项不闭合")
    for row in rows:
        exact(row, {"id", "result", "facts"}, "阶段检查")
        if row["result"] not in {"pass", "failed"} or not isinstance(row["facts"], dict):
            raise GuardError("阶段检查结果非法")
    if value["result"] == "pass" and (value["environment"] != prepared["request"]["environment"] or any(row["result"] != "pass" for row in rows)):
        raise GuardError("通过观测存在环境漂移或检查失败")
    if not isinstance(value["request_records"], list):
        raise GuardError("请求记录必须为数组")
    for row in value["request_records"]:
        exact(row, {"method", "path", "request_binding", "response_binding"}, "请求记录")
        if row["method"] != "GET" or row["path"] not in {"/health", "/v1/models"}:
            raise GuardError("出现未冻结的 HTTP 请求")
    for item in all_bindings(value):
        check_binding(data_root, item)
    return value


class GuardEngine:
    """持有现场锁的服务侧执行引擎；runtime 只能由固定受管构造器提供。"""

    def __init__(self, configuration: dict[str, Any], context: dict[str, Any], runtime: Any, identity: dict[str, Any]):
        self.configuration = configuration
        self.context = context
        self.runtime = runtime
        self.identity = identity
        if hasattr(runtime, "bind_profile_producer"):
            runtime.bind_profile_producer(identity)
        self.data_root = Path(context["data_root"])
        self.root = managed_path(self.data_root, configuration["paths"]["guard_root_relative"])
        private_directory(self.root)
        self.operations = self.root / "operations"
        private_directory(self.operations)
        self.lock = threading.RLock()
        self.owners: dict[str, tuple[dict[str, Any], float]] = {}
        self.cancelled: set[str] = set()
        self._file_lock = os.open(self.root / ".service.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self._file_lock)
            raise GuardError("该隔离实例已有恢复守卫") from error
        self.stable_context = {key: value for key, value in context.items() if key not in {"segment", "run_nonce"}}
        frozen = {"configuration": configuration, "context": self.stable_context}
        write_json_once(self.root / "configuration.json", frozen)
        if not (self.root / "index.json").exists():
            atomic_json(self.root / "index.json", {"schema_version": INDEX_SCHEMA, "configuration_sha256": canonical_sha256(configuration), "operations": []})
        self._stop = threading.Event()

    def close(self) -> None:
        self._stop.set()
        fcntl.flock(self._file_lock, fcntl.LOCK_UN)
        os.close(self._file_lock)

    def _operation(self, operation_id: str) -> Path:
        if not isinstance(operation_id, str) or not SAFE_ID.fullmatch(operation_id):
            raise GuardError("操作 ID 非法")
        return managed_path(self.operations, operation_id)

    def _index(self) -> dict[str, Any]:
        value = exact(read_json(self.root / "index.json"), {"schema_version", "configuration_sha256", "operations"}, "全操作索引")
        if value["schema_version"] != INDEX_SCHEMA or value["configuration_sha256"] != canonical_sha256(self.configuration) or not isinstance(value["operations"], list):
            raise GuardError("全操作索引版本或配置漂移")
        ids = []
        for row in value["operations"]:
            exact(row, {"operation_id", "prepared_binding"}, "操作索引条目")
            ids.append(row["operation_id"])
            check_binding(self.data_root, row["prepared_binding"])
            prepared = self._prepared(row["operation_id"])
            if prepared["operation_id"] != row["operation_id"]:
                raise GuardError("索引与 prepared 操作 ID 不符")
        if ids != sorted(set(ids)):
            raise GuardError("操作索引重复或未排序")
        actual = []
        for path in self.operations.iterdir():
            if path.is_symlink() or not path.is_dir() or not SAFE_ID.fullmatch(path.name):
                raise GuardError("操作目录存在未知或损坏原件")
            actual.append(path.name)
        if sorted(actual) != ids:
            raise GuardError("全操作索引遗漏或引用了不存在的操作；先恢复索引原件再续跑")
        return value

    def _prepared(self, operation_id: str) -> dict[str, Any]:
        prepared = read_json(self._operation(operation_id) / "prepared.json")
        required = {"schema_version", "operation_id", "configuration_sha256", "context", "request", "target", "rollback", "before_bindings", "guard_identity", "owner", "prepared_at_utc"}
        exact(prepared, required, "prepared")
        if prepared["schema_version"] != PREPARED_SCHEMA or prepared["operation_id"] != operation_id or prepared["configuration_sha256"] != canonical_sha256(self.configuration):
            raise GuardError("prepared 配置或坐标漂移")
        if prepared["context"] != self.stable_context:
            raise GuardError("prepared 执行上下文漂移")
        for item in all_bindings(prepared):
            check_binding(self.data_root, item)
        self._journal(operation_id)
        return prepared

    def _repair_unarmed_index_gap(self) -> None:
        """修复prepared落盘后、索引fsync前的唯一安全断点。

        无armed及execution_started才允许补登记；带动作或损坏原件的遗漏
        不推测、不忽略，仍阻断并要求修复原件。
        """
        index = read_json(self.root / "index.json")
        exact(index, {"schema_version", "configuration_sha256", "operations"}, "全操作索引")
        if index["schema_version"] != INDEX_SCHEMA or index["configuration_sha256"] != canonical_sha256(self.configuration) or not isinstance(index["operations"], list):
            raise GuardError("全操作索引无法安全恢复")
        known = {row["operation_id"] for row in index["operations"]}
        gaps = []
        for path in self.operations.iterdir():
            if path.name in known:
                continue
            if path.is_symlink() or not path.is_dir() or not SAFE_ID.fullmatch(path.name):
                raise GuardError("未知操作目录不能自动恢复")
            prepared = self._prepared(path.name)
            records = self._journal(path.name)
            if (path / "armed.json").exists() or any(row["event"] not in {"prepared"} for row in records):
                raise GuardError("索引遗漏了armed或已执行操作，不能隐去后继续")
            gaps.append({"operation_id": prepared["operation_id"], "prepared_binding": binding(self.data_root, path / "prepared.json")})
        if gaps:
            index["operations"] = sorted([*index["operations"], *gaps], key=lambda row: row["operation_id"])
            atomic_json(self.root / "index.json", index)
            for row in gaps:
                self._append(row["operation_id"], "unarmed_index_recovered", {"prepared_binding": row["prepared_binding"]})

    def _journal(self, operation_id: str) -> list[dict[str, Any]]:
        directory = self._operation(operation_id) / "journal"
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise GuardError("journal 目录非法")
        records = []
        previous = None
        for index, path in enumerate(sorted(directory.iterdir()), 1):
            if path.is_symlink() or not path.is_file() or path.name != f"{index:06d}.json":
                raise GuardError("journal 序号缺失或存在未知原件")
            value = exact(read_json(path), {"schema_version", "operation_id", "sequence", "event", "payload", "previous_sha256", "record_sha256"}, "journal记录")
            expected = canonical_sha256({key: item for key, item in value.items() if key != "record_sha256"})
            if value["schema_version"] != JOURNAL_SCHEMA or value["operation_id"] != operation_id or value["sequence"] != index or value["previous_sha256"] != previous or value["record_sha256"] != expected:
                raise GuardError("journal 链断裂或摘要漂移")
            for item in all_bindings(value):
                check_binding(self.data_root, item)
            records.append(value)
            previous = value["record_sha256"]
        return records

    def _append(self, operation_id: str, event: str, payload: dict[str, Any]) -> None:
        records = self._journal(operation_id)
        value = {"schema_version": JOURNAL_SCHEMA, "operation_id": operation_id, "sequence": len(records) + 1, "event": event, "payload": payload, "previous_sha256": records[-1]["record_sha256"] if records else None}
        value["record_sha256"] = canonical_sha256(value)
        write_json_once(self._operation(operation_id) / "journal" / f"{value['sequence']:06d}.json", value)

    def _latest_terminal(self, operation_id: str) -> tuple[dict[str, Any], Path] | None:
        directory = self._operation(operation_id) / "terminals"
        if not directory.exists():
            return None
        files = sorted(directory.iterdir())
        for number, path in enumerate(files, 1):
            if path.is_symlink() or not path.is_file() or path.name != f"{number:06d}.json":
                raise GuardError("物理终态序号非法")
            value = read_json(path)
            if value.get("operation_id") != operation_id or value.get("schema_version") != TERMINAL_SCHEMA or value.get("status") not in TERMINAL_STATES:
                raise GuardError("物理终态合同非法")
            for item in all_bindings(value):
                check_binding(self.data_root, item)
        return (read_json(files[-1]), files[-1]) if files else None

    def _snapshot_index(self) -> dict[str, Any]:
        index = self._index()
        operations = []
        for row in index["operations"]:
            operation_id = row["operation_id"]
            terminal = self._latest_terminal(operation_id)
            root = self._operation(operation_id)
            originals = []
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise GuardError("操作原件存在符号链接")
                if path.is_file() and not path.name.startswith(("evidence-", "journal-prefix-")):
                    originals.append(binding(self.data_root, path))
            operations.append({"operation_id": operation_id, "status": terminal[0]["status"] if terminal else "armed", "prepared_binding": row["prepared_binding"], "terminal_binding": binding(self.data_root, terminal[1]) if terminal else None, "bindings": originals})
        snapshot = {"schema_version": INDEX_SCHEMA, "configuration_sha256": canonical_sha256(self.configuration), "operations": operations}
        path = self.root / "index-snapshots" / (canonical_sha256(snapshot) + ".json")
        write_json_once(path, snapshot)
        return binding(self.data_root, path)

    def _origin(self) -> dict[str, Any]:
        return {"authority": self.identity["authority"], "source_scope": self.identity["source_scope"], "configuration_sha256": canonical_sha256(self.configuration), "service_instance_id": self.identity["service_instance_id"], "operations_index_binding": self._snapshot_index()}

    def status(self) -> dict[str, Any]:
        with self.lock:
            summary = self.recover_pending({"reason": "status"}, startup=False)
            return {"guard_identity": self.identity, "configuration_sha256": canonical_sha256(self.configuration), "execution_context": self.stable_context, "recovery": summary}

    def inspect(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            summary = self.recover_pending({"reason": "freeze"}, startup=False)
            if summary["blocked"]:
                raise GuardError("存在未验证恢复的操作，禁止新的 freeze")
            # 每次重新只读核现场，旧snapshot不能代替实时准入，原件不能覆盖。
            parent = self.root / "freezes" / canonical_sha256(request)
            private_directory(parent)
            root = parent / f"{len(list(parent.iterdir())) + 1:06d}"
            private_directory(root)
            path = root / "snapshot.json"
            result = self.runtime.freeze(request, root)
            exact(result, {"environment", "rollback", "gates", "cores"}, "现场freeze")
            origin = self._origin()
            for core in result["cores"].values():
                core["origin"] = origin
            result["cores"]["freeze"]["guard_identity"] = self.identity
            write_json_once(path, result)
            return result

    def prepare(self, request: dict[str, Any], owner: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            exact(request, PREPARE_FIELDS, "prepare请求")
            if request["stage"] not in STAGE_CHECKS or not isinstance(request["run_nonce"], str) or not SAFE_ID.fullmatch(request["run_nonce"]):
                raise GuardError("prepare阶段或run_nonce非法")
            for name in ("plan_ref", "configuration_ref", "stable_projection_ref", "probe_recipe_ref"):
                object_ref(request[name], "candidate_delivery_plan" if name == "plan_ref" else "operational_evidence", name)
            if not owner_alive(owner) or owner["process_group_id"] == self.identity["process_group_id"]:
                raise GuardError("worker 身份不存在或与守卫共用进程组")
            operation_id = "op-" + canonical_sha256({"configuration": canonical_sha256(self.configuration), "context": self.stable_context, "request": request})[:32]
            root = self._operation(operation_id)
            if root.exists():
                self._index()
                prepared = self._prepared(operation_id)
                if prepared["request"] != request:
                    raise GuardError("同一操作重入改变了冻结请求")
                terminal = self._latest_terminal(operation_id)
                if terminal and terminal[0]["status"] in {"restore_failed", "recovery_pending"}:
                    raise GuardError("该操作恢复未决，禁止重入")
                if terminal is None:
                    current = self.owners.get(operation_id)
                    if current is None or not owner_alive(current[0]) or time.monotonic() - current[1] > self.configuration["limits"]["lease_seconds"]:
                        self._abort(operation_id, "重入前恢复已失效的旧租约")
                    elif current[0] != owner:
                        raise GuardError("同一操作仍由另一个有效worker持有")
                    else:
                        self.owners[operation_id] = (dict(owner), time.monotonic())
                return self._ticket(operation_id)
            summary = self.recover_pending({"reason": "prepare"}, startup=False)
            if summary["blocked"] or any(self._latest_terminal(row["operation_id"]) is None for row in self._index()["operations"]):
                raise GuardError("当前实例仍有未收口操作，禁止新变更")
            self.runtime.check_qualification()
            private_directory(root)
            target_material = self.configuration["rollback" if request["stage"] == "rollback_verified" else "candidate"]
            target = {key: target_material[key] for key in ("image_digest", "runtime_profile_sha256")}
            rollback = {key: self.configuration["rollback"][key] for key in ("image_digest", "runtime_profile_sha256")}
            try:
                before = self.runtime.before(root, request["environment"])
            except Exception:
                # armed之前没有物理变更；保留检查原件，但不留下假操作阻塞续跑。
                failed = self.root / "preparation-failures"
                private_directory(failed)
                destination = failed / (operation_id + f"-{len(list(failed.iterdir())) + 1:06d}")
                os.rename(root, destination)
                sync_directory(self.operations)
                sync_directory(failed)
                raise
            prepared = {"schema_version": PREPARED_SCHEMA, "operation_id": operation_id, "configuration_sha256": canonical_sha256(self.configuration), "context": self.stable_context, "request": request, "target": target, "rollback": rollback, "before_bindings": before, "guard_identity": self.identity, "owner": owner, "prepared_at_utc": datetime.now(timezone.utc).isoformat()}
            for item in all_bindings(prepared):
                check_binding(self.data_root, item)
            write_json_once(root / "prepared.json", prepared)
            self._append(operation_id, "prepared", {"prepared_binding": binding(self.data_root, root / "prepared.json")})
            index = read_json(self.root / "index.json")
            index["operations"].append({"operation_id": operation_id, "prepared_binding": binding(self.data_root, root / "prepared.json")})
            index["operations"].sort(key=lambda item: item["operation_id"])
            atomic_json(self.root / "index.json", index)
            armed = {"operation_id": operation_id, "prepared_binding": binding(self.data_root, root / "prepared.json"), "guard_identity": self.identity}
            write_json_once(root / "armed.json", armed)
            self._append(operation_id, "armed", {"armed_binding": binding(self.data_root, root / "armed.json")})
            self.owners[operation_id] = (dict(owner), time.monotonic())
            return self._ticket(operation_id)

    def _ticket(self, operation_id: str) -> dict[str, Any]:
        prepared = self._prepared(operation_id)
        return {"operation_id": operation_id, "prepared_binding": binding(self.data_root, self._operation(operation_id) / "prepared.json"), "guard_identity": prepared["guard_identity"]}

    def _check_ticket(self, ticket: dict[str, Any]) -> str:
        exact(ticket, {"operation_id", "prepared_binding", "guard_identity"}, "ticket")
        operation_id = ticket["operation_id"]
        self._index()
        if ticket != self._ticket(operation_id):
            raise GuardError("ticket 与已落盘 prepared 不一致")
        return operation_id

    def heartbeat(self, ticket: dict[str, Any], owner: dict[str, Any]) -> dict[str, Any]:
        operation_id = ticket["operation_id"]
        current = self.owners.get(operation_id)
        if current is None or current[0] != owner or not owner_alive(owner):
            raise GuardError("租约 owner 已改变或消失")
        self.owners[operation_id] = (current[0], time.monotonic())
        return {"operation_id": operation_id, "alive": True}

    def _lease_check(self, operation_id: str) -> None:
        current = self.owners.get(operation_id)
        if operation_id in self.cancelled or current is None or not owner_alive(current[0]) or time.monotonic() - current[1] > self.configuration["limits"]["lease_seconds"]:
            raise GuardError("worker 消失、通道丢失或恢复租约超时")

    def _failed_observation(self, prepared: dict[str, Any], reason: str) -> dict[str, Any]:
        unknown = {"state": "unknown", "reason": reason}
        value = {"stage": prepared["request"]["stage"], "target": prepared["target"], "actual": {"image": dict(unknown), "profile": dict(unknown)}, "environment": None, "checks": [{"id": name, "result": "failed", "facts": {"reason": reason}} for name in STAGE_CHECKS[prepared["request"]["stage"]]], "observed_seconds": 0, "live_request_count": 0, "request_records": [], "before_bindings": prepared["before_bindings"], "during_bindings": [], "after_bindings": [], "result": "failed"}
        if hasattr(self.runtime, "failed_observation"):
            try:
                candidate = self.runtime.failed_observation(copy.deepcopy(value), self._operation(prepared["operation_id"]))
                value = validate_observation(candidate, prepared, self.data_root)
            except Exception:
                pass
        return value

    def run(self, ticket: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            operation_id = self._check_ticket(ticket)
            root = self._operation(operation_id)
            prepared = self._prepared(operation_id)
            terminal = self._latest_terminal(operation_id)
            if terminal:
                return read_json(root / "observation.json") if (root / "observation.json").exists() else self._failed_observation(prepared, "物理操作已经收口")
            if (root / "observation.json").exists():
                return validate_observation(read_json(root / "observation.json"), prepared, self.data_root)
            if any(row["event"] == "execution_started" for row in self._journal(operation_id)):
                self._abort(operation_id, "检测到物理动作已开始的断点；不能重复动作")
                return read_json(root / "observation.json")
            try:
                self._lease_check(operation_id)
                self._append(operation_id, "execution_started", {"prepared_binding": ticket["prepared_binding"]})
                raw = self.runtime.execute(prepared["request"]["stage"], prepared["target"], prepared["request"]["environment"], root, lambda: self._lease_check(operation_id))
                raw["before_bindings"] = prepared["before_bindings"]
                write_json_once(root / "raw-observation.json", raw)
                value = validate_observation(raw, prepared, self.data_root)
                self._lease_check(operation_id)
                write_json_once(root / "observation.json", value)
                self._append(operation_id, "observation_written", {"observation_binding": binding(self.data_root, root / "observation.json")})
                if value["result"] != "pass":
                    self._abort(operation_id, "实测阶段检查失败")
                return value
            except Exception as error:
                reason = f"物理执行或观测复核失败：{type(error).__name__}"
                write_json_once(root / "execution-error.json", {"operation_id": operation_id, "reason": reason})
                if not (root / "observation.json").exists():
                    write_json_once(root / "observation.json", self._failed_observation(prepared, reason))
                self._abort(operation_id, reason)
                return read_json(root / "observation.json")

    def _terminal(self, operation_id: str, status: str, restoration: dict[str, Any], intent: dict[str, Any] | None) -> dict[str, Any]:
        root = self._operation(operation_id)
        directory = root / "terminals"
        private_directory(directory)
        number = len(list(directory.iterdir())) + 1
        terminal = {"schema_version": TERMINAL_SCHEMA, "operation_id": operation_id, "status": status, "prepared_sha256": binding(self.data_root, root / "prepared.json")["sha256"], "physical_intent_sha256": intent["sha256"] if intent else None, "physical_intent_binding": intent, "restoration": restoration, "guard_identity": self.identity}
        path = directory / f"{number:06d}.json"
        write_json_once(path, terminal)
        self._append(operation_id, status, {"terminal_binding": binding(self.data_root, path)})
        prefix = {"schema_version": JOURNAL_SCHEMA, "operation_id": operation_id, "records": self._journal(operation_id)}
        write_json_once(root / f"journal-prefix-{number:06d}.json", prefix)
        self.owners.pop(operation_id, None)
        self.cancelled.discard(operation_id)
        core = self._core(operation_id, number, terminal, path)
        if status in {"restored", "restore_failed", "recovery_pending"}:
            self._restoration_core(operation_id, number, terminal, path)
        return core

    def _core(self, operation_id: str, number: int, terminal: dict[str, Any], terminal_path: Path) -> dict[str, Any]:
        root = self._operation(operation_id)
        # 首次执行证据固定；后续恢复只追加restore core，不偷换已发布失败事实。
        path = root / "evidence-000001.json"
        if path.exists():
            existing = read_json(path)
            for item in all_bindings(existing):
                check_binding(self.data_root, item)
            return existing
        prepared = self._prepared(operation_id)
        observation = read_json(root / "observation.json") if (root / "observation.json").exists() else self._failed_observation(prepared, "动作未完成，由守卫恢复")
        result = "pass" if terminal["status"] == "execution_committed" else "failed"
        core = {"contract_version": CORE_SCHEMA, "campaign_id": self.context["campaign_id"], "candidate_id": self.context["candidate_id"], "delivery_id": self.context["delivery_id"], "stage": prepared["request"]["stage"], "run_nonce": prepared["request"]["run_nonce"], "operation_id": operation_id, **{key: prepared["request"][key] for key in ("plan_ref", "configuration_ref", "stable_projection_ref", "probe_recipe_ref")}, **{key: observation[key] for key in OBSERVATION_FIELDS - {"stage", "result"}}, "guard_identity": terminal["guard_identity"], "prepared_binding": binding(self.data_root, root / "prepared.json"), "physical_intent_binding": terminal["physical_intent_binding"], "guard_terminal_binding": binding(self.data_root, terminal_path), "journal_binding": binding(self.data_root, root / f"journal-prefix-{number:06d}.json"), "restoration": terminal["restoration"], "origin": self._origin(), "result": result}
        write_json_once(path, core)
        return core

    def _restoration_core(self, operation_id: str, number: int, terminal: dict[str, Any], terminal_path: Path) -> dict[str, Any]:
        root = self._operation(operation_id)
        path = root / f"restoration-core-{number:06d}.json"
        if path.exists():
            value = read_json(path)
            for item in all_bindings(value):
                check_binding(self.data_root, item)
            return value
        prepared = self._prepared(operation_id)
        successful = terminal["status"] == "restored"
        restore_bindings = terminal["restoration"]["bindings"]
        if successful:
            restored = read_json(check_binding(self.data_root, restore_bindings[0]))
            actual, checks = restored["actual"], restored["checks"]
        else:
            unknown = {"state": "unknown", "reason": "恢复尚未验证"}
            actual = {"image": dict(unknown), "profile": dict(unknown)}
            checks = [{"id": name, "result": "failed", "facts": {"reason": terminal["restoration"]["reason"]}} for name in STAGE_CHECKS["rollback_verified"]]
        value = {"contract_version": "claude-delivery-restore/v1", "result": "pass" if successful else "failed", "campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"], "operation_id": operation_id, "target": prepared["rollback"], "actual": actual, "checks": checks, "journal_binding": binding(self.data_root, root / f"journal-prefix-{number:06d}.json"), "terminal_binding": binding(self.data_root, terminal_path), "bindings": restore_bindings, "origin": self._origin()}
        write_json_once(path, value)
        return value

    def _current_core(self, operation_id: str) -> dict[str, Any]:
        terminal = self._latest_terminal(operation_id)
        if terminal is None:
            raise GuardError("物理操作尚未收口")
        number = int(terminal[1].stem)
        root = self._operation(operation_id)
        prefix_path = root / f"journal-prefix-{number:06d}.json"
        if not prefix_path.exists():
            records = self._journal(operation_id)
            matching = [row for row in records if row["payload"].get("terminal_binding") == binding(self.data_root, terminal[1])]
            if not matching:
                self._append(operation_id, terminal[0]["status"], {"terminal_binding": binding(self.data_root, terminal[1])})
                records = self._journal(operation_id)
            write_json_once(prefix_path, {"schema_version": JOURNAL_SCHEMA, "operation_id": operation_id, "records": records})
        core = self._core(operation_id, number, terminal[0], terminal[1])
        if terminal[0]["status"] in {"restored", "restore_failed", "recovery_pending"}:
            self._restoration_core(operation_id, number, terminal[0], terminal[1])
        return core

    def _abort(self, operation_id: str, reason: str) -> dict[str, Any]:
        latest = self._latest_terminal(operation_id)
        if latest and latest[0]["status"] in {"execution_committed", "restored"}:
            return self._current_core(operation_id)
        self._append(operation_id, "restoration_started", {"reason": reason})
        root = self._operation(operation_id)
        prepared = self._prepared(operation_id)
        attempt = len([row for row in self._journal(operation_id) if row["event"] == "restoration_started"])
        restore_root = root / "restorations" / f"{attempt:06d}"
        private_directory(restore_root)
        try:
            restored = self.runtime.restore(prepared["rollback"], prepared["request"]["environment"], restore_root)
            exact(restored, {"target", "actual", "checks", "bindings", "result"}, "恢复实测")
            for item in all_bindings(restored):
                check_binding(self.data_root, item)
            if restored["target"] != prepared["rollback"] or restored["result"] != "pass" or not isinstance(restored["checks"], list) or {row.get("id") for row in restored["checks"] if isinstance(row, dict)} != set(STAGE_CHECKS["rollback_verified"]) or len(restored["checks"]) != len(STAGE_CHECKS["rollback_verified"]) or any(row["result"] != "pass" for row in restored["checks"]):
                raise GuardError("恢复未通过完整真实检查")
            for name, expected in (("image", prepared["rollback"]["image_digest"]), ("profile", prepared["rollback"]["runtime_profile_sha256"])):
                if restored["actual"][name].get("state") != "known" or restored["actual"][name].get("value") != expected:
                    raise GuardError("恢复实测镜像或 Profile 不匹配")
            write_json_once(restore_root / "result.json", restored)
            restoration = {"state": "verified", "bindings": [binding(self.data_root, restore_root / "result.json"), *all_bindings(restored)], "reason": reason}
            status = "restored"
        except Exception as error:
            write_json_once(restore_root / "error.json", {"operation_id": operation_id, "reason": f"恢复失败：{type(error).__name__}"})
            restoration = {"state": "restore_failed", "bindings": [binding(self.data_root, restore_root / "error.json")], "reason": f"{reason}；恢复尚未验证"}
            status = "restore_failed"
        intent_path = root / "physical-intent.json"
        return self._terminal(operation_id, status, restoration, binding(self.data_root, intent_path) if intent_path.exists() else None)

    def finish(self, ticket: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            operation_id = self._check_ticket(ticket)
            terminal = self._latest_terminal(operation_id)
            if terminal:
                return self._current_core(operation_id)
            root = self._operation(operation_id)
            try:
                exact(decision, {"result", "observation_sha256", "verifier_binding", "reason"}, "编排复核")
                if decision["result"] not in {"pass", "failed"} or not isinstance(decision["reason"], str):
                    raise GuardError("编排复核结果非法")
                check_binding(self.data_root, decision["verifier_binding"])
                self._lease_check(operation_id)
                observation = validate_observation(read_json(root / "observation.json"), self._prepared(operation_id), self.data_root)
                if decision["observation_sha256"] != canonical_sha256(observation) or decision["result"] != "pass" or observation["result"] != "pass":
                    return self._abort(operation_id, "编排复核拒绝、摘要漂移或实测失败")
                write_json_once(root / "verified-decision.json", decision)
                intent = {"schema_version": INTENT_SCHEMA, "operation_id": operation_id, "prepared_sha256": ticket["prepared_binding"]["sha256"], "prepared_binding": ticket["prepared_binding"], "observation_binding": binding(self.data_root, root / "observation.json"), "verified_decision_binding": binding(self.data_root, root / "verified-decision.json"), "result": "pass"}
                write_json_once(root / "physical-intent.json", intent)
                self._append(operation_id, "intent_written", {"physical_intent_binding": binding(self.data_root, root / "physical-intent.json")})
                self._lease_check(operation_id)
                return self._terminal(operation_id, "execution_committed", {"state": "not_required", "bindings": [], "reason": "物理阶段已提交"}, binding(self.data_root, root / "physical-intent.json"))
            except Exception as error:
                return self._abort(operation_id, f"复核或提交预写失败：{type(error).__name__}")

    def recover_pending(self, request: dict[str, Any], *, startup: bool = False) -> dict[str, Any]:
        with self.lock:
            exact(request, {"reason"}, "恢复请求")
            rows = []
            self._repair_unarmed_index_gap()
            index = self._index()
            for row in index["operations"]:
                operation_id = row["operation_id"]
                self._prepared(operation_id)
                terminal = self._latest_terminal(operation_id)
                if terminal and terminal[0]["status"] in {"execution_committed", "restored"}:
                    core = self._current_core(operation_id)
                elif terminal and not startup and request["reason"] in {"watchdog", "status", "freeze", "prepare"}:
                    core = self._current_core(operation_id)
                elif startup or terminal or operation_id not in self.owners or not owner_alive(self.owners[operation_id][0]) or time.monotonic() - self.owners[operation_id][1] > self.configuration["limits"]["lease_seconds"]:
                    core = self._abort(operation_id, "独立守卫启动恢复" if startup else "租约丢失或显式恢复未决操作")
                else:
                    core = None
                rows.append({"operation_id": operation_id, "status": self._latest_terminal(operation_id)[0]["status"] if self._latest_terminal(operation_id) else "armed", "execution": core})
            blocked = any(row["status"] in {"restore_failed", "recovery_pending", "armed"} for row in rows)
            origin = self._origin()
            return {"schema_version": "claude-upgrade-delivery-recovery-summary/v1", "blocked": blocked, "operations": rows, "operations_index_binding": origin["operations_index_binding"], "origin": origin}

    def inventory(self) -> dict[str, Any]:
        with self.lock:
            self._index()
            executions, restorations = [], []
            for row in self._index()["operations"]:
                root = self._operation(row["operation_id"])
                prepared = self._prepared(row["operation_id"])
                if prepared["context"] != self.stable_context:
                    raise GuardError("操作索引跨Campaign或delivery")
                for pattern, collection in (("evidence-*.json", executions), ("restoration-core-*.json", restorations)):
                    for path in sorted(root.glob(pattern)):
                        value = read_json(path)
                        for item in all_bindings(value):
                            check_binding(self.data_root, item)
                        collection.append(value)
            origin = self._origin()
            return {"executions": executions, "restorations": restorations, "operations_index_binding": origin["operations_index_binding"], "origin": origin}

    def watchdog(self) -> None:
        while not self._stop.wait(self.configuration["limits"]["poll_seconds"]):
            for operation_id, current in list(self.owners.items()):
                if not owner_alive(current[0]) or time.monotonic() - current[1] > self.configuration["limits"]["lease_seconds"]:
                    self.cancelled.add(operation_id)
            if self.lock.acquire(blocking=False):
                try:
                    self.recover_pending({"reason": "watchdog"})
                except Exception:
                    # 损坏原件保持阻断；服务仍可回报错误，不忽略操作继续变更。
                    pass
                finally:
                    self.lock.release()

    def _profile_source_materials(self, origin: dict[str, Any], freeze_path: Path | None) -> list[dict[str, Any]]:
        """完整 core 已匹配后，只从其原 snapshot 和显式全操作索引选择 producer。"""
        def read_original(item: Mapping[str, Any]) -> bytes:
            return read_bytes(check_binding(self.data_root, dict(item)))

        materials = []

        def add(phase: str, producer: dict[str, Any], observation: dict[str, Any] | None, document: Any, prefix: str) -> None:
            references = profile_observations.select_profile_proof_references(document, prefix, read_original, configuration=self.configuration)
            # 也保留无可授域的 Docker 观测：校验器继续核完整来源而不倒填角色。
            materials.append({"collection_phase": phase, "producer_binding": producer,
                              "observation_binding": observation, "proof_references": references})

        if freeze_path is not None:
            snapshot = read_json(freeze_path)
            add("freeze", binding(self.data_root, freeze_path), None, snapshot, "/cores/freeze/before_bindings/")
        index_binding = origin.get("operations_index_binding")
        if index_binding is not None:
            index = read_json(check_binding(self.data_root, index_binding))
            if index.get("schema_version") != INDEX_SCHEMA or index.get("configuration_sha256") != canonical_sha256(self.configuration):
                raise GuardError("Profile producer 的索引不是同一冻结配置")
            current = {row["operation_id"]: row["prepared_binding"] for row in self._index()["operations"]}
            for row in index["operations"]:
                exact(row, {"operation_id", "status", "prepared_binding", "terminal_binding", "bindings"}, "Profile producer操作索引")
                if current.get(row["operation_id"]) != row["prepared_binding"]:
                    raise GuardError("Profile producer 的原操作不在受管索引")
                prepared = self._prepared(row["operation_id"])
                add("before", row["prepared_binding"], None, prepared, "/before_bindings/")
                operation = self._operation(row["operation_id"])
                observation_path = operation / "observation.json"
                if observation_path.exists():
                    observed_binding = binding(self.data_root, observation_path)
                    if observed_binding in row["bindings"]:
                        add("execution", row["prepared_binding"], observed_binding, read_json(observation_path), "/during_bindings/")
                for terminal_binding in row["bindings"]:
                    terminal_path = PurePosixPath(terminal_binding["path"])
                    if terminal_path.parent.as_posix() != operation.relative_to(self.data_root).as_posix() + "/terminals":
                        continue
                    terminal = read_json(check_binding(self.data_root, terminal_binding))
                    if terminal.get("status") == "restored":
                        restoration_binding = terminal["restoration"]["bindings"][0]
                        if restoration_binding not in row["bindings"]:
                            raise GuardError("Profile recovery 的原观测不在完整操作索引")
                        restored = read_json(check_binding(self.data_root, restoration_binding))
                        add("recovery", terminal_binding, restoration_binding, restored, "/bindings/")
        return sorted(materials, key=canonical_sha256)

    def export(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            exact(request, {"content", "origin"}, "来源导出")
            origin = request["origin"]
            if origin.get("authority") != self.identity["authority"] or origin.get("configuration_sha256") != canonical_sha256(self.configuration) or origin.get("source_scope") != self.identity["source_scope"]:
                raise GuardError("来源配置或 authority 不匹配")
            self._index()
            # 只能导出守卫曾生成的完整内容；不能给客户端任意自摘要 JSON 背书。
            expected_core = {**request["content"], "origin": origin}
            matched = False
            matched_freeze_path = None
            for path in [*self.operations.glob("*/evidence-*.json"), *self.operations.glob("*/restoration-core-*.json")]:
                if read_json(path) == expected_core:
                    matched = True
                    break
            if not matched:
                for path in (self.root / "freezes").rglob("snapshot.json") if (self.root / "freezes").exists() else ():
                    snapshot = read_json(path)
                    if expected_core in snapshot.get("cores", {}).values():
                        matched = True
                        matched_freeze_path = path
                        break
            content = request["content"]
            if not matched and set(content) == {"campaign_id", "delivery_id", "operation_index_binding"}:
                current = self._origin()
                derived = {"campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"], "operation_index_binding": current["operations_index_binding"]}
                if content == derived and origin == current:
                    matched = True
            if not matched:
                raise GuardError("请求内容不是该受管守卫生成的冻结原件")
            profile_materials = self._profile_source_materials(origin, matched_freeze_path)
            proof_bindings = {reference["proof_binding"]["path"]: reference["proof_binding"]
                              for material in profile_materials for reference in material["proof_references"]}
            profile_projection = profile_observations.validate_profile_observation_projection(
                self.configuration, self.context, [proof_bindings[key] for key in sorted(proof_bindings)],
                lambda item: read_bytes(check_binding(self.data_root, dict(item))),
                purpose="formed_replay", producer_materials=profile_materials)
            originals: dict[str, str] = {}
            seen: dict[str, dict[str, Any]] = {}
            pending = all_bindings({"content": content, "origin": origin, "profile_producer_materials": profile_materials,
                                    "configuration_binding": self.context["configuration_ref"]})
            total = 0
            while pending:
                item = pending.pop()
                previous = seen.get(item["path"])
                if previous is not None:
                    if previous != item:
                        raise GuardError("来源闭包中同一原件存在不同绑定")
                    continue
                path = check_binding(self.data_root, item)
                data = read_bytes(path)
                seen[item["path"]] = item
                total += len(data)
                if total > MAX_RPC_BYTES // 2:
                    raise GuardError("来源原件闭包超出固定通道上限，不能裁掉原件继续")
                originals[item["path"]] = base64.b64encode(data).decode("ascii")
                projected = profile_projection.project(item, data)
                if projected is not None:
                    pending.extend(projected)
                    continue
                # 展开受管新协议的原件引用；旧资格图仍由Store形成期重放，
                # 不把旧文件里的任意路径当作guard新的读取权限。
                if path.suffix == ".json" and (path.is_relative_to(self.root) or item == self.context["configuration_ref"]):
                    pending.extend(all_bindings(parse_json_bytes(data, "受管来源闭包")))
            return {"content_sha256": canonical_sha256(content), "deployment_sha256": self.identity["deployment_receipt_binding"]["sha256"], "configuration_sha256": canonical_sha256(self.configuration), "service_instance_id": expected_core["origin"]["service_instance_id"], "trusted_data_root": str(self.data_root), "bytes_by_path": originals, "guard_identity": self.identity,
                    "profile_configuration_binding": self.context["configuration_ref"],
                    "profile_producer_materials": profile_materials, "profile_proof_bindings": [proof_bindings[key] for key in sorted(proof_bindings)]}


def _mac(key: bytes, value: Any) -> str:
    return hmac.new(key, canonical_json_bytes(value), hashlib.sha256).hexdigest()


def _receive(connection: socket.socket) -> bytes:
    content = bytearray()
    while not content.endswith(b"\n"):
        block = connection.recv(min(65536, MAX_RPC_BYTES + 1 - len(content)))
        if not block:
            raise GuardError("守卫通道提前关闭")
        content.extend(block)
        if len(content) > MAX_RPC_BYTES:
            raise GuardError("守卫通道消息超出限制")
    return bytes(content)


class GuardClient:
    """只连接显式冻结的私有 Unix socket，协议和鉴权不可由证据指定。"""

    def __init__(self, data_root: Path, socket_relative: str, channel_key_binding: dict[str, Any], timeout_seconds: float = 300):
        self.path = managed_path(data_root, socket_relative)
        self.key = read_bytes(check_binding(data_root, channel_key_binding))
        if len(self.key) < 32:
            raise GuardError("守卫通道密钥至少为 32 字节")
        self.timeout = timeout_seconds

    def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in {"status", "inspect", "prepare", "run", "finish", "heartbeat", "recover", "inventory", "export"}:
            raise GuardError("未定义的守卫操作")
        if self.path.is_symlink() or not self.path.exists() or not stat.S_ISSOCK(self.path.stat().st_mode) or self.path.stat().st_uid != os.getuid() or stat.S_IMODE(self.path.stat().st_mode) != 0o600:
            raise GuardError("守卫 socket 所有者、类型或权限非法")
        nonce = secrets.token_hex(24)
        envelope = {"protocol": PROTOCOL, "nonce": nonce, "action": action, "payload": payload}
        request = {**envelope, "mac": _mac(self.key, envelope)}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(str(self.path))
            connection.sendall(canonical_json_bytes(request))
            response = parse_json_bytes(_receive(connection))
        exact(response, {"protocol", "nonce", "body", "mac"}, "守卫响应")
        core = {key: response[key] for key in ("protocol", "nonce", "body")}
        if response["protocol"] != PROTOCOL or response["nonce"] != nonce or not hmac.compare_digest(response["mac"], _mac(self.key, core)):
            raise GuardError("守卫来源通道鉴权失败")
        body = exact(response["body"], {"ok", "value"}, "守卫响应体")
        if body["ok"] is not True:
            raise GuardError(str(body["value"]))
        if not isinstance(body["value"], dict):
            raise GuardError("守卫返回值必须为对象")
        return body["value"]


def serve(engine: GuardEngine, socket_path: Path, key: bytes, ready: Callable[[], None] | None = None) -> None:
    """独立服务入口。重启先恢复全部日志，再监听，不能只重放最新一条。"""
    private_directory(socket_path.parent)
    engine.recover_pending({"reason": "startup"}, startup=True)
    if socket_path.exists():
        if socket_path.is_symlink() or not stat.S_ISSOCK(socket_path.stat().st_mode) or socket_path.stat().st_uid != os.getuid():
            raise GuardError("已有 socket 不可信")
        socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    socket_path.chmod(0o600)
    server.listen(16)
    server.settimeout(0.25)
    watchdog = threading.Thread(target=engine.watchdog, daemon=True)
    watchdog.start()

    def handle(connection: socket.socket) -> None:
        nonce = ""
        try:
            connection.settimeout(30)
            request = parse_json_bytes(_receive(connection))
            exact(request, {"protocol", "nonce", "action", "payload", "mac"}, "守卫请求")
            core = {name: request[name] for name in ("protocol", "nonce", "action", "payload")}
            nonce = request["nonce"]
            if request["protocol"] != PROTOCOL or not hmac.compare_digest(request["mac"], _mac(key, core)):
                raise GuardError("守卫请求鉴权失败")
            action, payload = request["action"], request["payload"]
            if action == "status":
                exact(payload, set(), "status请求")
                value = engine.status()
            elif action == "inspect":
                value = engine.inspect(payload)
            elif action == "prepare":
                exact(payload, {"request", "owner"}, "prepare RPC")
                value = engine.prepare(payload["request"], payload["owner"])
            elif action == "run":
                value = engine.run(payload)
            elif action == "finish":
                exact(payload, {"ticket", "decision"}, "finish RPC")
                value = engine.finish(payload["ticket"], payload["decision"])
            elif action == "heartbeat":
                exact(payload, {"ticket", "owner"}, "heartbeat RPC")
                value = engine.heartbeat(payload["ticket"], payload["owner"])
            elif action == "recover":
                value = engine.recover_pending(payload)
            elif action == "inventory":
                exact(payload, set(), "inventory请求")
                value = engine.inventory()
            elif action == "export":
                value = engine.export(payload)
            else:
                raise GuardError("未定义的守卫操作")
            body = {"ok": True, "value": value}
        except Exception as error:
            body = {"ok": False, "value": str(error) if isinstance(error, GuardError) else f"守卫执行异常：{type(error).__name__}"}
        response = {"protocol": PROTOCOL, "nonce": nonce, "body": body}
        try:
            connection.sendall(canonical_json_bytes({**response, "mac": _mac(key, response)}))
        except OSError:
            # 对端丢失不改变提交权；未提交操作按 owner/lease 路径恢复。
            pass
        finally:
            connection.close()

    if ready:
        ready()
    try:
        while not engine._stop.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(connection,), daemon=True).start()
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
        engine.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude ARM64 独立恢复守卫（不自动安装／登记）")
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--execution-context", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.configuration.is_absolute() or not args.execution_context.is_absolute():
        raise GuardError("守卫启动配置与上下文必须使用显式绝对路径")
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import DockerComposeRuntime, guard_identity, validate_configuration, validate_execution_context
    configuration = validate_configuration(read_json(args.configuration))
    if canonical_sha256(configuration) != args.configuration_sha256:
        raise GuardError("启动配置摘要不匹配")
    context = validate_execution_context(read_json(args.execution_context), configuration)
    raw_configuration = check_binding(Path(context["data_root"]), context["configuration_ref"])
    if raw_configuration != args.configuration or read_json(raw_configuration) != configuration:
        raise GuardError("守卫启动配置不是上下文冻结的raw配置原件")
    if configuration["source_scope"] != "formal":
        raise GuardError("正式服务入口不接受本机夹具")
    identity = guard_identity(configuration, context, formal=True)
    runtime = DockerComposeRuntime(configuration, context)
    engine = GuardEngine(configuration, context, runtime, identity)
    root = Path(context["data_root"])
    key = read_bytes(check_binding(root, configuration["guard"]["channel_key_binding"]))
    serve(engine, managed_path(root, configuration["guard"]["socket_relative"]), key)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuardError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(3)
