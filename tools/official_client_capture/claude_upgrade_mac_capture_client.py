"""只连接冻结私有 daemon 的认证同步客户端。

导出中断只续取同一 export 的真实字节，不再预约、不再监听、不启动产品。
正式来源和 v2 产品联合证明由后续独立实施负责。
"""

from __future__ import annotations

import hashlib
import hmac
import fcntl
import os
import secrets
import socket
import stat
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture.claude_upgrade_mac_capture_service import _mac, receive


class CaptureClient:
    """socket、key 与接收根只取受管调用配置，不能由证据指定。"""

    def __init__(self, configuration: Mapping[str, Any], sync_root: Path, *, timeout_seconds: float = 40):
        self.configuration = c.validate_configuration(dict(configuration))
        self.task = c.verify_task_configuration(self.configuration)
        self.task_sha = self.task["task_sha256"]
        self.data_root = Path(self.configuration["data_root"])
        self.path = c.managed_path(self.data_root, self.configuration["control_socket_relative"])
        self.key = c.read_bytes(c.check_binding(self.data_root, self.configuration["channel_key_binding"]))
        self.root = c.private_directory(sync_root)
        capture = c.managed_path(self.data_root, self.configuration["capture_relative"])
        c.require(not self.root.is_relative_to(capture) and not capture.is_relative_to(self.root), "同步根必须与独立原件根分离")
        c.require(type(timeout_seconds) in {int, float} and 0 < timeout_seconds <= 60, "IPC 等待上界非法")
        self.timeout = timeout_seconds

    def call(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        c.require(action in {"status", "reserve", "ready", "close", "inventory", "export", "recover", "shutdown"}, "未定义的 capture 调用")
        c.plain_path(self.path)
        info = self.path.lstat()
        c.require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, "采集 socket 类型、所有者或权限非法")
        nonce = secrets.token_hex(24)
        envelope = {"protocol": c.RPC_PROTOCOL, "nonce": nonce, "action": action, "payload": dict(payload)}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(str(self.path))
            connection.sendall(c.canonical({**envelope, "mac": _mac(self.key, envelope)}) + b"\n")
            response = c.exact(c.parse_json(receive(connection)), {"protocol", "nonce", "body", "mac"}, "采集 RPC 响应")
        core = {name: response[name] for name in ("protocol", "nonce", "body")}
        c.require(response["protocol"] == c.RPC_PROTOCOL and response["nonce"] == nonce and isinstance(response["mac"], str) and hmac.compare_digest(response["mac"], _mac(self.key, core)), "采集来源响应鉴权失败")
        body = c.exact(response["body"], {"ok", "value"}, "采集响应体")
        c.require(body["ok"] is True, str(body["value"]))
        c.require(isinstance(body["value"], dict), "采集返回值必须为对象")
        return body["value"]

    def _task(self, task: Mapping[str, Any]) -> None:
        c.require(c.canonical(dict(task)) == c.canonical(self.task), "客户端操作必须绑定固定完整 task")

    def reserve(self, task: Mapping[str, Any], hold: Mapping[str, Any]) -> dict[str, Any]:
        self._task(task)
        return self.call("reserve", {"task": dict(task), "hold": dict(hold)})

    def wait_ready(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._task(task)
        return self.call("ready", {"task_sha256": self.task_sha})

    def _inventory(self, value: Mapping[str, Any]) -> dict[str, Any]:
        inventory = c.validate_inventory(dict(value), task_sha256=self.task_sha)
        c.require(inventory["configuration_sha256"] == c.digest(self.configuration) and inventory["deployment"] == self.configuration["deployment"], "认证 export 与受管配置或部署不符")
        for name in ("capture_id", "producer_run_id"):
            c.require(inventory[name] == self.task["execution"][name], "认证 export 与原 producer 不符")
        return inventory

    def close_and_sync(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._task(task)
        inventory = self._inventory(self.call("close", {"task_sha256": self.task_sha, "reason": "normal"}))
        return self.sync_committed_export(task, inventory["export_id"])

    def recover(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._task(task)
        inventory = self._inventory(self.call("recover", {"task_sha256": self.task_sha}))
        return self.sync_committed_export(task, inventory["export_id"])

    def _chunk(self, export_id: str, relative: str, offset: int, limit: int) -> bytes:
        value = self.call("export", {"task_sha256": self.task_sha, "export_id": export_id, "path": relative, "offset": offset, "limit": limit})
        return c.decode_chunk(value, export_id=export_id, relative=relative, offset=offset, limit=limit)

    def sync_committed_export(self, task: Mapping[str, Any], export_id: str) -> dict[str, Any]:
        descriptor = os.open(self.root / "sync-owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            c.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, "同步锁类型、所有者或权限非法")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return self._sync_committed_export(task, export_id)
        finally:
            os.close(descriptor)

    def _sync_committed_export(self, task: Mapping[str, Any], export_id: str) -> dict[str, Any]:
        self._task(task)
        c.require(isinstance(export_id, str) and c.SHA_RE.fullmatch(export_id), "export ID 非法")
        inventory = self._inventory(self.call("inventory", {"task_sha256": self.task_sha, "export_id": export_id}))
        exports = c.private_directory(self.root / "exports")
        destination = c.managed_path(exports, export_id)
        if destination.exists():
            native = c.verify_export(destination, inventory, task_sha256=self.task_sha)
            c.require(c.read_json(destination / "authenticated-inventory.json") == inventory, "已同步 inventory 漂移")
            expected_commit = {"schema_version": "claude-upgrade-native-capture-sync/v1", "source_scope": c.SCOPE, "task_sha256": self.task_sha, "export_id": export_id, "inventory": c.binding(destination, destination / "authenticated-inventory.json")}
            c.require(c.read_json(destination / "sync-commit.json") == expected_commit, "已同步提交凭证缺失或漂移")
            return {"status": native["status"], "source_scope": c.SCOPE, "export_id": export_id, "root": str(destination), "native_manifest": inventory["native_manifest"]}
        staging = c.private_directory(self.root / "pending" / export_id)
        c.write_json_once(staging / "authenticated-inventory.json", inventory)
        for reference in inventory["files"]:
            relative, expected = reference["path"], reference["bytes"]
            target = c.managed_path(staging, relative)
            c.private_directory(target.parent)
            if target.exists():
                c.check_binding(staging, reference)
                continue
            part = c.managed_path(staging / ".parts", relative)
            c.private_directory(part.parent)
            existing = c.read_bytes(part) if part.exists() else b""
            c.require(len(existing) <= expected, "同步 partial 超出认证文件长度")
            # 续传先认证已有前缀，不能仅相信本地 offset 或把坏前缀当成功。
            for offset in range(0, len(existing), c.MAX_CHUNK_BYTES):
                amount = min(c.MAX_CHUNK_BYTES, len(existing) - offset)
                c.require(self._chunk(export_id, relative, offset, amount) == existing[offset:offset + amount], "同步已有前缀与受管原件不符")
            descriptor = os.open(part, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "ab", closefd=False) as handle:
                    offset = len(existing)
                    while offset < expected:
                        amount = min(c.MAX_CHUNK_BYTES, expected - offset)
                        raw = self._chunk(export_id, relative, offset, amount)
                        c.require(len(raw) == amount, "受管 export 提前结束，保持同步未提交")
                        handle.write(raw)
                        handle.flush()
                        os.fsync(descriptor)
                        offset += len(raw)
                    handle.flush()
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            raw = c.read_bytes(part)
            c.require(len(raw) == expected and hashlib.sha256(raw).hexdigest() == reference["sha256"], "同步完整文件长度或 SHA 不符")
            c.write_once(target, raw)
            part.unlink()
            c.fsync_directory(part.parent)
        native = c.verify_export(staging, inventory, task_sha256=self.task_sha)
        commit = {"schema_version": "claude-upgrade-native-capture-sync/v1", "source_scope": c.SCOPE, "task_sha256": self.task_sha, "export_id": export_id, "inventory": c.binding(staging, staging / "authenticated-inventory.json")}
        c.write_json_once(staging / "sync-commit.json", commit)
        for directory in sorted((item for item in staging.rglob("*") if item.is_dir()), reverse=True):
            c.fsync_directory(directory)
        c.fsync_directory(staging)
        c.require(not destination.exists(), "同步提交目的已被并发占用")
        staging.rename(destination)
        c.fsync_directory(exports)
        c.fsync_directory(staging.parent)
        return {"status": native["status"], "source_scope": c.SCOPE, "export_id": export_id, "root": str(destination), "native_manifest": inventory["native_manifest"]}
