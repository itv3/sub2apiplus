"""本机独立采集的原件、固定配置和不可变导出合同。

本合同只授予 regression_fixture 能力，不登记正式来源，不生成 Mac 产品参与、
transport-map 或完整 v2 capture 的成功证明。所有网络和预算操作均在调用层执行。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

CONFIG_SCHEMA = "claude-upgrade-native-capture-configuration/v1"
NATIVE_SCHEMA = "claude-upgrade-native-capture/v1"
INVENTORY_SCHEMA = "claude-upgrade-native-capture-export/v1"
COMMIT_SCHEMA = "claude-upgrade-native-capture-commit/v1"
RPC_PROTOCOL = "claude-upgrade-native-capture-rpc/v1"
SCOPE = "regression_fixture"
MAX_RPC_BYTES = 262144
MAX_CHUNK_BYTES = 65536
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def contract_versions(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """从受管配置形成期选择整套版本，禁止失败后跨版本回退。"""
    schema = configuration.get("schema_version")
    require(schema in {CONFIG_SCHEMA, "claude-upgrade-native-capture-configuration/v2"}, "未知 native 配置版本")
    version = 1 if schema == CONFIG_SCHEMA else 2
    return {"version": version, "configuration": schema,
            "native": f"claude-upgrade-native-capture/v{version}",
            "inventory": f"claude-upgrade-native-capture-export/v{version}",
            "commit": f"claude-upgrade-native-capture-commit/v{version}",
            "rpc": f"claude-upgrade-native-capture-rpc/v{version}",
            "sync": f"claude-upgrade-native-capture-sync/v{version}"}


class CaptureError(ValueError):
    """受管采集合同失败；不包含请求正文或凭据。"""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise CaptureError(message)


def exact(value: Any, names: set[str], label: str) -> dict[str, Any]:
    require(isinstance(value, dict) and set(value) == names, f"{label} 字段闭集非法")
    return value


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp(value: Any) -> datetime:
    require(isinstance(value, str) and value.endswith("Z"), "时间必须为 UTC 原件")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CaptureError("UTC 时间非法") from error
    return result


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in values:
        require(name not in result, "JSON 原件含重复键")
        result[name] = value
    return result


def parse_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _value: require(False, "JSON 非有限数字"))
    except (ValueError, UnicodeError) as error:
        raise CaptureError("JSON 原件非法") from error
    require(isinstance(value, dict), "JSON 顶层必须为对象")
    return value


def plain_path(path: Path) -> Path:
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "受管路径必须为无回退的绝对路径")
    require(all(not item.is_symlink() for item in (path, *path.parents)), "受管路径包含符号链接")
    return path


def private_directory(path: Path) -> Path:
    path = plain_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "私有目录所有者或权限非法")
    return path


def managed_path(root: Path, relative: str) -> Path:
    root = plain_path(root)
    require(isinstance(relative, str) and relative and not relative.endswith("/"), "相对路径非法")
    parts = PurePosixPath(relative).parts
    require(not PurePosixPath(relative).is_absolute() and all(part not in {"", ".", ".."} for part in parts) and "/".join(parts) == relative, "相对路径越界")
    return plain_path(root.joinpath(*parts))


def read_bytes(path: Path) -> bytes:
    path = plain_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1, "原件类型、所有者或硬链接非法")
        require(stat.S_IMODE(info.st_mode) & 0o077 == 0, "原件必须为当前用户私有")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict[str, Any]:
    return parse_json(read_bytes(path))


def fsync_directory(path: Path) -> None:
    descriptor = os.open(plain_path(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_once(path: Path, raw: bytes) -> None:
    path = plain_path(path)
    private_directory(path.parent)
    if path.exists():
        require(read_bytes(path) == raw, "不可变原件已存在且字节冲突")
        return
    temporary = path.parent / (".pending-" + secrets.token_hex(16))
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(descriptor)
        # 独占根锁保护写者；使用 hard-link 的不覆盖提交消除外部同名抢占窗口。
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def write_json_once(path: Path, value: Any) -> None:
    write_once(path, canonical(value) + b"\n")


def binding(root: Path, path: Path) -> dict[str, Any]:
    path = plain_path(path)
    require(path.is_relative_to(root), "原件不在冻结根内")
    raw = read_bytes(path)
    return {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def check_binding(root: Path, value: Any) -> Path:
    reference = exact(value, {"path", "sha256", "bytes"}, "原件绑定")
    require(isinstance(reference["sha256"], str) and SHA_RE.fullmatch(reference["sha256"]), "原件摘要非法")
    require(type(reference["bytes"]) is int and reference["bytes"] >= 0, "原件长度非法")
    path = managed_path(root, reference["path"])
    require(binding(root, path) == reference, "原件摘要或长度漂移")
    return path


def _loopback(value: Any) -> None:
    require(isinstance(value, str), "监听或上游地址必须为回环 IP")
    try:
        require(ipaddress.ip_address(value).is_loopback, "本机后端仅接受回环地址")
    except ValueError as error:
        raise CaptureError("监听或上游地址必须为回环 IP") from error


def validate_configuration(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and value.get("schema_version") == "claude-upgrade-native-capture-configuration/v2":
        from tools.official_client_capture import claude_mac_capture_h2_contract as h2_contract
        return h2_contract.validate_configuration(value)
    result = exact(value, {"schema_version", "source_scope", "data_root", "capture_relative", "task_binding", "deployment", "control_socket_relative", "channel_key_binding", "listener", "upstream", "limits"}, "采集配置")
    require(result["schema_version"] == CONFIG_SCHEMA and result["source_scope"] == SCOPE, "本机 capture 尚不接受正式来源用途")
    root = private_directory(Path(result["data_root"]))
    capture = managed_path(root, result["capture_relative"])
    control = managed_path(root, result["control_socket_relative"])
    require(not control.is_relative_to(capture) and not capture.is_relative_to(control.parent), "控制通道不能位于导出原件根内")
    check_binding(root, result["task_binding"])
    key = read_bytes(check_binding(root, result["channel_key_binding"]))
    require(32 <= len(key) <= 128, "采集通道 key 必须为 32 至 128 字节")
    require(not check_binding(root, result["channel_key_binding"]).is_relative_to(capture), "通道 key 不能位于导出原件根内")
    deployment = exact(result["deployment"], {"deployment_id", "tool_identity_sha256"}, "采集部署")
    require(isinstance(deployment["deployment_id"], str) and bool(deployment["deployment_id"]), "采集部署 ID 缺失")
    require(isinstance(deployment["tool_identity_sha256"], str) and SHA_RE.fullmatch(deployment["tool_identity_sha256"]), "采集部署摘要非法")
    listener = exact(result["listener"], {"host", "port", "tls"}, "数据监听")
    upstream = exact(result["upstream"], {"host", "port", "tls"}, "固定上游")
    for endpoint in (listener, upstream):
        _loopback(endpoint["host"])
        require(type(endpoint["port"]) is int and 0 < endpoint["port"] <= 65535, "端口必须预先冻结")
    require((listener["host"], listener["port"]) != (upstream["host"], upstream["port"]), "监听与上游不能形成自环")
    if listener["tls"] is not None:
        exact(listener["tls"], {"certificate", "private_key"}, "监听 TLS")
        for reference in listener["tls"].values():
            check_binding(root, reference)
    if upstream["tls"] is not None:
        exact(upstream["tls"], {"ca", "server_hostname"}, "上游 TLS")
        check_binding(root, upstream["tls"]["ca"])
        require(isinstance(upstream["tls"]["server_hostname"], str) and bool(upstream["tls"]["server_hostname"]), "上游 TLS hostname 缺失")
    limits = exact(result["limits"], {"drain_seconds", "socket_timeout_seconds", "max_connections", "max_header_bytes", "max_body_bytes", "max_wire_bytes"}, "采集上界")
    for name in ("drain_seconds", "socket_timeout_seconds"):
        require(type(limits[name]) in {int, float} and 0 < limits[name] <= 30, "采集等待上界非法")
    for name in ("max_connections", "max_header_bytes", "max_body_bytes", "max_wire_bytes"):
        require(type(limits[name]) is int and limits[name] > 0, "采集大小上界非法")
    require(limits["max_connections"] <= 64 and limits["max_header_bytes"] <= 65536 and limits["max_wire_bytes"] <= 67108864, "本机采集配置超过有界能力")
    return result


def verify_task_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    from tools.official_client_capture import claude_mac_client_source as source
    task = source.validate_task_v2(read_json(check_binding(Path(configuration["data_root"]), configuration["task_binding"])))
    execution = task["execution"]
    require(execution["source_scope"] == SCOPE, "本机 capture 不能接正式 task")
    root = managed_path(Path(configuration["data_root"]), configuration["capture_relative"])
    require(root == Path(execution["capture_root"]), "capture 根与任务冻结根不符")
    require(configuration["deployment"] == execution["source_deployments"]["capture"], "配置部署与任务不符")
    endpoint = urlsplit(execution["endpoint"])
    listener = configuration["listener"]
    require(endpoint.scheme == ("https" if listener["tls"] is not None else "http") and endpoint.hostname == listener["host"] and endpoint.port == listener["port"] and endpoint.path in {"", "/"} and not endpoint.query and not endpoint.fragment, "产品 endpoint 必须为冻结回环 listener")
    return task


def runtime_bindings(configuration: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    root = Path(__file__).parent
    # 记录本后端直接调用的关键代码字节；这份原件不代替整套正式工具身份和来源登记。
    names = ("claude_mac_capture_contract.py", "claude_mac_capture_wire.py", "claude_mac_capture_engine_core.py", "claude_mac_capture_fixture_policy.py", "claude_upgrade_mac_capture_service.py", "claude_upgrade_mac_capture_client.py", "upstream_byte_relay.py", "claude_client_receipt_contract.py", "claude_mac_client_source.py", "claude_upgrade_mac_budget.py", "codex_upgrade_project_ledger.py", "claude_upgrade_delivery_recovery_guard.py")
    if configuration is not None and contract_versions(configuration)["version"] == 2:
        names += ("claude_mac_capture_h2.py", "claude_mac_capture_h2_dependencies.py",
                  "claude_mac_capture_h2_contract.py", "claude_mac_capture_h2_runtime.py",
                  "claude_mac_capture_h2_core.py", "claude_mac_capture_dependency_core.py",
                  "claude_mac_capture_runtime_core.py")
    return [{"file": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(), "bytes": (root / name).stat().st_size} for name in names]


def validate_inventory(value: Any, *, task_sha256: str, expected_version: int | None = None) -> dict[str, Any]:
    version = 2 if isinstance(value, Mapping) and value.get("schema_version") == "claude-upgrade-native-capture-export/v2" else 1
    require(expected_version is None or expected_version == version, "inventory 与形成期版本不符")
    if version == 2:
        from tools.official_client_capture import claude_mac_capture_h2_contract as h2_contract
        return h2_contract.validate_inventory(value, task_sha256=task_sha256)
    inventory = exact(value, {"schema_version", "source_scope", "task_sha256", "capture_id", "producer_run_id", "configuration_sha256", "deployment", "native_manifest", "files", "export_id"}, "采集导出清单")
    require(inventory["schema_version"] == INVENTORY_SCHEMA and inventory["source_scope"] == SCOPE and inventory["task_sha256"] == task_sha256, "导出用途或任务不符")
    require(inventory["export_id"] == digest({key: item for key, item in inventory.items() if key != "export_id"}), "不可变 export ID 不符")
    require(isinstance(inventory["files"], list), "导出原件清单非法")
    names: set[str] = set()
    for item in inventory["files"]:
        exact(item, {"path", "sha256", "bytes"}, "导出原件")
        managed_path(Path("/private/tmp"), item["path"])
        require(item["path"] not in names and type(item["bytes"]) is int and item["bytes"] >= 0 and isinstance(item["sha256"], str) and SHA_RE.fullmatch(item["sha256"]), "导出原件重复或摘要非法")
        names.add(item["path"])
    require(inventory["native_manifest"] in inventory["files"] and inventory["native_manifest"]["path"] == "native-capture-manifest.json", "导出未绑定 native 终态原件")
    return inventory


def verify_export(root: Path, inventory: Mapping[str, Any], *, task_sha256: str, expected_version: int | None = None) -> dict[str, Any]:
    inventory = validate_inventory(dict(inventory), task_sha256=task_sha256, expected_version=expected_version)
    if inventory["schema_version"] == "claude-upgrade-native-capture-export/v2":
        from tools.official_client_capture import claude_mac_capture_h2_contract as h2_contract
        return h2_contract.verify_export(root, inventory, task_sha256=task_sha256)
    for item in inventory["files"]:
        check_binding(root, item)
    native = read_json(check_binding(root, inventory["native_manifest"]))
    exact(native, {"schema_version", "source_scope", "task_sha256", "candidate", "capture_id", "producer_run_id", "endpoint", "deployment", "configuration_sha256", "runtime_bindings", "status", "reasons", "closed_at_utc", "reservation", "ready", "connections", "requests", "outcomes", "journal", "raw_files"}, "native 终态")
    require(native["schema_version"] == NATIVE_SCHEMA and native["source_scope"] == SCOPE and native["status"] in {"complete", "failed", "truncated"}, "native 终态版本、用途或状态非法")
    for name in ("task_sha256", "capture_id", "producer_run_id", "configuration_sha256", "deployment"):
        require(native[name] == inventory[name], "native 终态与认证导出不符")
    timestamp(native["closed_at_utc"])
    references = [native[name] for name in ("reservation", "connections", "requests", "outcomes", "journal")]
    if native["ready"] is not None:
        references.append(native["ready"])
    require(isinstance(native["raw_files"], list), "native 原始字节清单非法")
    references.extend(native["raw_files"])
    for reference in references:
        require(reference in inventory["files"], "native 必需原件不在导出闭包")
        check_binding(root, reference)
    for name in ("requests", "outcomes"):
        for line in read_bytes(check_binding(root, native[name])).splitlines():
            row = parse_json(line)
            require(row.get("protocol") != "h2" and "stream_id" not in row, "H2 行不能包装为 native v1")
    # 这里验证字节闭包，不声称真实产品进程／模块、transport-map 或正式来源已完成。
    return native


def decode_chunk(value: Any, *, export_id: str, relative: str, offset: int, limit: int) -> bytes:
    chunk = exact(value, {"export_id", "path", "offset", "bytes", "sha256", "data_base64"}, "导出分块")
    require(chunk["export_id"] == export_id and chunk["path"] == relative and chunk["offset"] == offset, "导出分块坐标不符")
    try:
        raw = base64.b64decode(chunk["data_base64"], validate=True)
    except (ValueError, TypeError) as error:
        raise CaptureError("导出分块编码非法") from error
    require(type(chunk["bytes"]) is int and len(raw) == chunk["bytes"] <= limit and hashlib.sha256(raw).hexdigest() == chunk["sha256"], "导出分块长度或摘要不符")
    return raw
