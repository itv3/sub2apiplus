"""真实 NE flow 原件的构建、固定安装、认证读取与闭合解析。

网络来源独立于 ES/libproc；只接受 sourceProcessAuditToken 和 FlowClosed。
原生流不含 HTTP ordinal，必须与独立 capture 原件联合映射后才能消费请求。
私有构建不安装、不创建签名密钥、不申请系统权限，也不登记正式来源。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import os
import platform
import plistlib
import select
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_capture_contract as wire
from tools.official_client_capture import claude_mac_product_authority as authority
from tools.official_client_capture import claude_mac_product_native as native

SCHEMA = "claude-mac-network-installation/v1"
PROVIDER_ID = "com.sub2apiplus.claude.network-observer"
BRIDGE_ID = "com.sub2apiplus.claude.network-client"
PROVIDER_RELATIVE = PROVIDER_ID + ".systemextension/Contents/MacOS/claude-network-observer"
_TOKEN = object()


class ProductFlowError(ValueError):
    """原生流、安装身份、签名或原始序列不符。"""


class ProductFlowUnavailable(ProductFlowError):
    """真实流来源缺少资格、权限、端点或完整关闭报告。"""


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ProductFlowError(message)


def _audit(value: Any, *, optional: bool = False) -> tuple[int, ...] | None:
    if optional and value is None:
        return None
    _require(isinstance(value, list) and len(value) == 8
             and all(type(n) is int and 0 <= n <= 0xFFFFFFFF for n in value)
             and value[5] > 0 and value[7] > 0, "flow audit token 不是完整内核实例")
    return tuple(value)


def _endpoint(value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    _require(isinstance(value, dict) and set(value) == {"host", "port"}
             and type(value["port"]) is int and 0 < value["port"] <= 65535, "flow 原生端点非法")
    try:
        address = ipaddress.ip_address(value["host"])
    except (ValueError, TypeError) as error:
        raise ProductFlowError("flow 地址必须来自原生数字地址，不能 DNS 猜映射") from error
    return (f"[{address}]" if address.version == 6 else str(address)) + ":" + str(value["port"])


def _context(task: Mapping[str, Any], *, user_id: int | None = None) -> dict[str, Any]:
    actual_user = os.geteuid() if user_id is None else user_id
    _require(type(actual_user) is int and 0 < actual_user <= 0xFFFFFFFF, "网络用户不是实际非 root 身份")
    return {"task_sha256": task["task_sha256"], "run_id": task["execution"]["producer_run_id"],
            "user_id": actual_user, "deadline_utc": task["execution"]["request_policy"]["deadline_utc"],
            "deployment": copy.deepcopy(task["execution"]["source_deployments"]["mac"])}


def verify_flow_attestation(raw: bytes, envelope: Mapping[str, Any], public_key: bytes,
                            context: Mapping[str, Any]) -> dict[str, Any]:
    """核实际 ECDSA 来源签名；公开 key 必须由固定安装读取器提供。

    X962 原始 public key 转为固定 P-256 SPKI，系统 openssl 只执行验签。
    不把“签名字段存在”或一个上传者自摘要当来源证明。
    """
    _require(isinstance(envelope, Mapping) and set(envelope)
             == {"payload_base64", "signature_base64", "public_key_sha256"}, "flow attestation 字段非法")
    _require(isinstance(public_key, bytes) and len(public_key) == 65 and public_key[0] == 4
             and envelope["public_key_sha256"] == hashlib.sha256(public_key).hexdigest(), "flow 受管 public key 不符")
    try:
        payload = base64.b64decode(envelope["payload_base64"], validate=True)
        signature = base64.b64decode(envelope["signature_base64"], validate=True)
        message = wire.parse_json(payload)
    except (ValueError, TypeError) as error:
        raise ProductFlowError("flow 签名原件编码非法") from error
    _require(0 < len(payload) <= 65536 and 0 < len(signature) <= 144, "flow 签名原件超限")
    _require(set(message) == {"schema_version", "context", "record_count", "raw_bytes", "stream_sha256", "first_error"}
             and message["schema_version"] == "claude-mac-ne-flow-attestation/v1"
             and message["context"] == dict(context) and type(message["record_count"]) is int
             and 2 <= message["record_count"] <= 65536 and message["raw_bytes"] == len(raw)
             and message["stream_sha256"] == hashlib.sha256(raw).hexdigest(), "flow 签名上下文或真实原件摘要不符")
    der = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200") + public_key
    encoded = base64.b64encode(der)
    pem = b"-----BEGIN PUBLIC KEY-----\n" + b"\n".join(encoded[i:i+64] for i in range(0, len(encoded), 64)) + b"\n-----END PUBLIC KEY-----\n"
    with tempfile.TemporaryDirectory(prefix="claude-flow-verify-") as name:
        root = Path(name)
        for relative, content in (("key.pem", pem), ("payload", payload), ("signature", signature)):
            native._new_file(root / relative, content)
        checked = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(root / "key.pem"),
            "-signature", str(root / "signature"), str(root / "payload")], capture_output=True, timeout=10, check=False)
        _require(checked.returncode == 0, "flow 实际 ECDSA 来源签名不通过")
    return message


def parse_owned_flows(raw: bytes, *, task: Mapping[str, Any],
                      process_frames: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从完整原件建立实际进程 flow，不猜闭连接、应用 PID 或模块调用。

    未知 actual process token、端点缺失、序列空洞、来源应用代办连接和缺 close
    都拒绝。其他用户进程的记录不会被重标为本轮产品进程。
    """
    _require(isinstance(raw, bytes) and raw.endswith(b"\n") and len(raw) <= 67_108_864, "flow 原件截断或超限")
    rows = [wire.parse_json(line) for line in raw.splitlines()]
    _require(2 <= len(rows) <= 65536 and rows[0].get("event") == "session_begin"
             and rows[-1].get("event") == "session_end", "flow 缺启动或结束屏障")
    _require(dict(context) == _context(task, user_id=context.get("user_id")), "flow context 不是原 task 与来源用户")
    lifecycles = native.owned_process_lifecycles(process_frames, task)
    processes = {token: row["start"] for token, row in lifecycles.items()}
    exits = {token: row["end"]["observed_at_utc"] for token, row in lifecycles.items() if row["end"] is not None}
    _require(all(token[1] == context["user_id"] for token in processes), "flow 与 ES 原件不是同一实际来源用户")
    _require(bool(processes), "flow 缺实际 ES 产品实例")
    facts_names = {"flow_id", "source_process_audit_token", "source_app_audit_token", "local_endpoint", "remote_endpoint",
                   "socket_family", "socket_type", "socket_protocol"}
    pending: dict[str, dict[str, Any]] = {}
    last_time = c.timestamp(task["issued_at_utc"])
    for index, row in enumerate(rows):
        _require(set(row) == {"schema_version", "event", "sequence", "task_sha256", "run_id", "observed_at_utc", "facts"}
                 and row["schema_version"] == "claude-mac-ne-flow-event/v1" and type(row["sequence"]) is int
                 and row["sequence"] == index and row["task_sha256"] == task["task_sha256"]
                 and row["run_id"] == context["run_id"], "flow 原件身份、顺序或字段不闭合")
        at = c.timestamp(row["observed_at_utc"])
        _require(last_time <= at, "flow 原件时间倒置")
        last_time = at
        event, facts = row["event"], row["facts"]
        _require(isinstance(facts, dict), "flow facts 非对象")
        if event == "session_begin":
            _require(index == 0 and facts == {"provider_class": "NEFilterDataProvider",
                "source_token_kind": "sourceProcessAuditToken", "close_report_kind": "NEFilterReportEventFlowClosed"},
                "flow 启动来源能力不符")
            continue
        if event == "session_end":
            _require(index == len(rows)-1 and set(facts) == {"first_error", "open_flow_ids"}
                     and facts["first_error"] is None and facts["open_flow_ids"] == sorted(
                         key for key, value in pending.items() if value["closed_at_utc"] is None), "flow 尾屏障或来源错误")
            continue
        _require(event in {"flow_open", "flow_endpoints", "flow_closed"}
                 and set(facts) == (facts_names | {"bytes_in", "bytes_out", "report_event"} if event == "flow_closed" else facts_names),
                 "不能用样本或 data-complete 冒充原生 flow 事件")
        try:
            identifier = str(uuid.UUID(facts["flow_id"]))
        except (ValueError, TypeError, AttributeError) as error:
            raise ProductFlowError("flow UUID 非法") from error
        _require(identifier == facts["flow_id"] and facts["socket_family"] in {2, 30}
                 and facts["socket_type"] == 1 and facts["socket_protocol"] == 6, "flow 不是真实 TCP flow")
        actual = _audit(facts["source_process_audit_token"], optional=True)
        application = _audit(facts["source_app_audit_token"], optional=True)
        local, remote = _endpoint(facts["local_endpoint"], optional=True), _endpoint(facts["remote_endpoint"], optional=True)
        if event == "flow_open":
            _require(identifier not in pending, "flow 打开重复")
            pending[identifier] = {"flow_id": identifier, "audit_token": actual, "app_audit_token": application,
                "client_endpoint": local, "server_endpoint": remote, "opened_at_utc": row["observed_at_utc"],
                "closed_at_utc": None, "bytes_in": None, "bytes_out": None}
        else:
            value = pending.get(identifier)
            _require(value is not None and value["closed_at_utc"] is None and value["audit_token"] == actual
                     and value["app_audit_token"] == application, "flow 更新／关闭缺打开或进程身份变化")
            for key, endpoint in (("client_endpoint", local), ("server_endpoint", remote)):
                _require(value[key] is None or endpoint == value[key], "同 flow 的端点漂移")
                value[key] = endpoint
            if event == "flow_closed":
                _require(facts["report_event"] == 3 and all(type(facts[key]) is int and facts[key] >= 0
                         for key in ("bytes_in", "bytes_out")), "flow 关闭不是实际 FlowClosed 报告")
                value.update(closed_at_utc=row["observed_at_utc"], bytes_in=facts["bytes_in"], bytes_out=facts["bytes_out"])
    result = []
    for value in pending.values():
        token, app = value["audit_token"], value["app_audit_token"]
        if token not in processes:
            _require(app not in processes, "系统代办／未知 actual process 不能换成来源应用 PID")
            continue
        process = processes[token]
        _require(value["closed_at_utc"] is not None and value["client_endpoint"] is not None
                 and value["server_endpoint"] is not None and token in exits, "本轮产品 flow 缺端点、真实关闭或进程 exit")
        opened, closed = c.timestamp(value["opened_at_utc"]), c.timestamp(value["closed_at_utc"])
        _require(c.timestamp(process["observed_at_utc"]) <= opened <= closed <= c.timestamp(exits[token]),
                 "flow 与实际 ES 进程生命周期不闭合")
        result.append({**value, "process": process["process"]})
    return result


@dataclass(frozen=True)
class NetworkInstallationAuthority:
    """固定受管读取器产物；普通 dataclass／自报旗标不能授来源权。"""
    installation: Mapping[str, Any]
    raw_sha256: str
    bridge: Path
    public_key: bytes
    _token: object

    def revalidate(self, deployment: Mapping[str, Any]) -> dict[str, Any]:
        _require(self._token is _TOKEN and platform.system() == "Darwin" and platform.machine() == "arm64",
                 "网络来源须为固定 Darwin ARM64 工厂")
        for path in (authority.INSTALL_ROOT, *authority.INSTALL_ROOT.parents):
            info = path.lstat()
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not path.is_symlink()
                     and stat.S_IMODE(info.st_mode) & 0o022 == 0, "网络安装祖先可被普通用户改写")
        raw = authority.read_original(authority.INSTALL_ROOT / "network-installation.json", root_owned=True)
        value = wire.parse_json(raw)
        _require(hashlib.sha256(raw).hexdigest() == self.raw_sha256 and value == dict(self.installation)
                 and value["deployment"] == dict(deployment), "网络受管安装或部署身份已经变化")
        identities = []
        for role, relative in (("bridge", "claude-network-client"), ("provider", PROVIDER_RELATIVE)):
            binding = value[role]
            _require(set(binding) == {"path", "sha256", "bytes"} and binding["path"] == relative, "网络受管映像路径不符")
            path = authority.INSTALL_ROOT / relative
            image = authority.read_original(path, root_owned=True)
            _require(len(image) == binding["bytes"] and hashlib.sha256(image).hexdigest() == binding["sha256"], "网络映像字节漂移")
            expected = value[role + "_signature"]
            _require(set(expected) == {"team_id", "identifier", "cdhash"}
                     and expected["identifier"] == (BRIDGE_ID if role == "bridge" else PROVIDER_ID), "网络代码身份不符")
            checked = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(path)],
                                     capture_output=True, timeout=30, check=False)
            read = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(path)],
                                  capture_output=True, timeout=30, check=False)
            _require(checked.returncode == read.returncode == 0, "网络原生映像实际签名无效")
            facts = dict(line.split("=", 1) for line in read.stderr.decode().splitlines() if "=" in line)
            _require(all(facts.get(key) == expected[name] for key, name in
                     (("Identifier", "identifier"), ("TeamIdentifier", "team_id"), ("CDHash", "cdhash"))), "网络实际签名不符")
            identities.append(expected)
        _require(identities[0]["team_id"] == identities[1]["team_id"]
                 and len(identities[0]["team_id"]) == 10 and all(ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for ch in identities[0]["team_id"]),
                 "网络 bridge 与 sysext 必须同一正式 Team ID")
        provider = authority.INSTALL_ROOT / PROVIDER_RELATIVE
        plist_path = provider.parent.parent / "Info.plist"
        plist_raw = authority.read_original(plist_path, root_owned=True)
        _require(hashlib.sha256(plist_raw).hexdigest() == value["provider_plist_sha256"], "网络 provider plist 漂移")
        info = plistlib.loads(plist_raw)
        _require(info["CFBundleIdentifier"] == PROVIDER_ID
                 and info["NetworkExtension"]["NEProviderClasses"] == {"com.apple.networkextension.filter-data": "ClaudeFlowDataProvider"}
                 and info["NetworkExtension"]["NEMachServiceName"] == value["mach_service"]
                 and info["RecorderClientRequirement"] == value["bridge_requirement"], "网络 provider 安装接线不符")
        for role, expected in zip(("bridge", "provider"), identities):
            requirement = value[role + "_requirement"]
            _require(isinstance(requirement, str) and f'identifier "{expected["identifier"]}"' in requirement
                     and expected["team_id"] in requirement and f'cdhash H"{expected["cdhash"]}"' in requirement,
                     "XPC peer requirement 必须冻结同 Team、独立 identifier 和实际 CDHash")
            path = authority.INSTALL_ROOT / value[role]["path"]
            checked = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", "-R", requirement, str(path)],
                                     capture_output=True, timeout=30, check=False)
            _require(checked.returncode == 0, "网络 peer requirement 不匹配实际签名")
        entitlements = subprocess.run(["/usr/bin/codesign", "--display", "--entitlements", ":-", str(provider)],
                                     capture_output=True, timeout=30, check=False)
        start = entitlements.stdout.find(b"<?xml")
        _require(entitlements.returncode == 0 and start >= 0, "网络 sysext 缺实际 entitlements")
        permissions = plistlib.loads(entitlements.stdout[start:])
        _require("content-filter-provider-systemextension" in permissions.get("com.apple.developer.networking.networkextension", [])
                 and any(value["mach_service"].startswith(group + ".") for group in
                         permissions.get("com.apple.security.application-groups", [])), "网络 sysext 权限与 Mach service 不符")
        return {"installation_sha256": self.raw_sha256, "deployment": dict(deployment), "public_key_sha256": hashlib.sha256(self.public_key).hexdigest()}


def load_network_installation() -> NetworkInstallationAuthority:
    """只读固定 root 安装，缺件时明确拒绝，不能用上传构建输出替代。"""
    try:
        raw = authority.read_original(authority.INSTALL_ROOT / "network-installation.json", root_owned=True)
        value = wire.parse_json(raw)
    except (ValueError, OSError) as error:
        raise ProductFlowUnavailable("真实 NetworkExtension 来源尚无受管安装") from error
    _require(set(value) == {"schema_version", "deployment", "bridge", "provider", "bridge_signature", "provider_signature",
             "bridge_requirement", "provider_requirement", "mach_service", "public_key_base64", "provider_plist_sha256"}
             and value["schema_version"] == SCHEMA, "网络安装合同非法")
    try:
        key = base64.b64decode(value["public_key_base64"], validate=True)
    except (ValueError, TypeError) as error:
        raise ProductFlowError("网络固定 public key 非法") from error
    _require(len(key) == 65 and key[0] == 4, "网络 public key 须为真实 P-256 X962 原件")
    result = NetworkInstallationAuthority(value, hashlib.sha256(raw).hexdigest(),
        authority.INSTALL_ROOT / "claude-network-client", key, _TOKEN)
    result.revalidate(value["deployment"])
    return result


def build_network_observer(output_root: Path) -> dict[str, Any]:
    """真实编译两份固定原生映像和 sysext plist；资格旗标始终 false。"""
    root = Path(output_root)
    native._private_directory(root)
    _require(not list(root.iterdir()), "网络私有构建根须为空")
    src = Path(__file__).with_name("claude_mac_product_flows_native.m")
    raw = authority.read_original(src)
    native._new_file(root / src.name, raw)
    sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, timeout=30, check=True).stdout.decode().strip()
    compiler = subprocess.run(["/usr/bin/xcrun", "--find", "clang"], capture_output=True, timeout=30, check=True).stdout.decode().strip()
    provider = root / PROVIDER_RELATIVE
    provider.parent.mkdir(mode=0o700, parents=True)
    plist = {"CFBundleIdentifier": PROVIDER_ID, "CFBundleExecutable": "claude-network-observer",
        "CFBundleVersion": "1", "CFBundlePackageType": "SYSX", "LSMinimumSystemVersion": "15.0",
        "NetworkExtension": {"NEMachServiceName": "UNQUALIFIED.com.sub2apiplus.claude.network-observer",
            "NEProviderClasses": {"com.apple.networkextension.filter-data": "ClaudeFlowDataProvider"}},
        "RecorderClientRequirement": ""}
    native._new_file(provider.parent.parent / "Info.plist", plistlib.dumps(plist))
    outputs = []
    for role, executable, define in (("provider", provider, []), ("bridge", root / "claude-network-client", ["-DCLAUDE_FLOW_CLIENT=1"])):
        argv = [compiler, "-isysroot", sdk, "-arch", "arm64", "-mmacosx-version-min=15.0", "-fobjc-arc", "-fblocks",
            "-framework", "Foundation", "-framework", "NetworkExtension", "-framework", "Network", "-framework", "Security",
            "-lbsm", "-O2", *define, str(root / src.name), "-o", str(executable)]
        result = subprocess.run(argv, capture_output=True, timeout=120, check=False)
        native._new_file(root / (role + "-build.stdout"), result.stdout)
        native._new_file(root / (role + "-build.stderr"), result.stderr)
        _require(result.returncode == 0, "网络原生映像编译失败；实际诊断已保留")
        signature = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(executable)],
                                   capture_output=True, timeout=30, check=False)
        native._new_file(root / (role + "-codesign.stderr"), signature.stderr)
        outputs.append({"role": role, "executable": c.binding(executable, root), "argv": argv,
            "code_signature_present": signature.returncode == 0,
            "formal_team_signature": False, "executed": False})
    _require(authority.read_original(src) == raw, "网络编译期间源码变化")
    receipt = {"schema_version": "claude-mac-network-build/v1", "source": c.binding(root / src.name, root),
        "outputs": outputs, "sdk": sdk, "compiler": compiler, "source_scope": "private_unsigned_build",
        "installed": False, "activated": False, "formal_ready": False, "launch_allowed": False, "registered": False}
    native._new_file(root / "build.json", c.canonical(receipt))
    return receipt


class DarwinFlowMonitor:
    """实启动固定签名 bridge，保存 XPC 原始响应与签名流；不启动官方产品。"""

    def __init__(self, task: Mapping[str, Any], *, bundle_root: Path, installed: NetworkInstallationAuthority):
        self.task, self.root = copy.deepcopy(dict(task)), Path(bundle_root)
        self.authority, self.context = installed, _context(task)
        self.process: subprocess.Popen | None = None
        self.offset = 0
        self._buffer = bytearray()
        self._calls = 0
        self._raw = bytearray()
        self._deadline = time.monotonic() + max(0, (c.timestamp(self.context["deadline_utc"])-datetime.now(timezone.utc)).total_seconds())
        self._cleanup_deadline: float | None = None
        self.attestation: dict[str, Any] | None = None
        self._stdout_fd: int | None = None
        self._stderr_fd: int | None = None
        self._stdout_bytes = 0
        self._failed = False

    def _remaining(self) -> float:
        remaining = (self._cleanup_deadline or self._deadline) - time.monotonic()
        if remaining <= 0:
            raise ProductFlowUnavailable("原网络 deadline 已耗尽，禁止重开窗口")
        return min(remaining, 30)

    def _abort_bridge(self) -> None:
        """只回收本实例持有的 bridge；保留服务端失败会话，不重开来源。

        真实 XPC 同步调用可被对端阻塞，因此关闭 stdin 不足以完成回收。
        有界终止自己的 Popen，不根据磁盘 PID 操作其他进程。
        """
        self._failed = True
        if self.process is not None:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.stdout is not None and not self.process.stdout.closed:
                self.process.stdout.close()
        self._finish_streams()

    def _finish_streams(self) -> None:
        for name in ("_stdout_fd", "_stderr_fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.fsync(fd)
                os.close(fd)
                setattr(self, name, None)

    def _call(self, operation: str, *, audit_tokens: list[list[int]] | None = None) -> dict[str, Any]:
        try:
            return self._call_raw(operation, audit_tokens=audit_tokens)
        except BaseException:
            self._abort_bridge()
            raise

    def _call_raw(self, operation: str, *, audit_tokens: list[list[int]] | None = None) -> dict[str, Any]:
        _require(self.process is not None and self.process.stdin is not None and self.process.stdout is not None
                 and self.process.poll() is None and not self._failed, "原网络 bridge 已退出，不能另开 producer")
        request = {"protocol": "claude-mac-ne-flow-rpc/v1", "operation": operation, "context": self.context, "offset": self.offset}
        if audit_tokens is not None:
            _require(operation == "quiesce", "只有原通道关闭允许 token 清单")
            request["audit_tokens"] = audit_tokens
        raw_request = c.canonical(request) + b"\n"
        _require(len(raw_request) <= 65536, "网络请求超出固定 bridge 原生帧上限")
        self._calls += 1
        _require(self._calls <= 262144, "网络读取次数超限")
        prefix = f"rpc-{self._calls:06d}-{operation}"
        native._new_file(self.root / "network-observations" / (prefix + ".request.json"), raw_request)
        fd = self.process.stdin.fileno()
        os.set_blocking(fd, False)
        offset = 0
        while offset < len(raw_request):
            _, writable, _ = select.select([], [fd], [], self._remaining())
            if not writable:
                raise ProductFlowUnavailable("原网络控制通道超过本次限时")
            try:
                count = os.write(fd, raw_request[offset:])
            except BlockingIOError:
                continue
            _require(count > 0, "原网络控制通道短写")
            offset += count
        while b"\n" not in self._buffer:
            readable, _, _ = select.select([self.process.stdout.fileno()], [], [], self._remaining())
            if not readable:
                raise ProductFlowUnavailable("原认证 XPC 读取超过本次限时")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            _require(bool(chunk), "认证 XPC 原件截断")
            # 先保存真实 stdout；坏 JSON、未成行、超限和截断前缀也必须保留。
            _require(self._stdout_fd is not None, "网络原件读流未初始化")
            self._stdout_bytes += len(chunk)
            view = memoryview(chunk)
            while view:
                count = os.write(self._stdout_fd, view)
                _require(count > 0, "网络原件读流短写")
                view = view[count:]
            os.fsync(self._stdout_fd)
            _require(self._stdout_bytes <= 134217728, "网络 RPC 原始输出总量超限")
            self._buffer.extend(chunk)
            _require(len(self._buffer) <= 524288, "认证 XPC 响应超限")
        line, _, tail = self._buffer.partition(b"\n")
        self._buffer = bytearray(tail)
        native._new_file(self.root / "network-observations" / (prefix + ".response.json"), bytes(line))
        response = wire.parse_json(bytes(line))
        _require(response.get("ok") is True, "真实网络来源拒绝：" + str(response.get("error", "unknown")))
        return response

    def start(self) -> dict[str, Any]:
        _require(self.process is None, "网络监测只能启动一次")
        self.authority.revalidate(self.context["deployment"])
        directory = self.root / "network-observations"
        directory.mkdir(mode=0o700)
        native._private_directory(directory)
        self._stdout_fd = os.open(directory / "bridge.stdout", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            self._stderr_fd = os.open(directory / "bridge.stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            self.process = subprocess.Popen([str(self.authority.bridge), "--flow-session-v1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_fd,
                bufsize=0, close_fds=True, start_new_session=True)
            return self._call("begin")
        except BaseException:
            self._abort_bridge()
            raise

    def read(self) -> dict[str, Any]:
        try:
            return self._read_response()
        except BaseException:
            self._abort_bridge()
            raise

    def _read_response(self) -> dict[str, Any]:
        response = self._call("read")
        _require(set(response) == {"ok", "offset", "records", "record_count", "sealed"}
                 and response["offset"] == self.offset and isinstance(response["records"], list)
                 and len(response["records"]) <= 128 and type(response["sealed"]) is bool, "网络读取闭集不符")
        for encoded in response["records"]:
            raw = base64.b64decode(encoded, validate=True)
            _require(0 < len(raw) <= 65536 and raw.endswith(b"\n"), "网络原始帧截断或超限")
            row = wire.parse_json(raw)
            _require(row.get("sequence") == self.offset, "网络来源丢帧或重放重复帧")
            native._new_file(self.root / "network-observations" / f"event-{self.offset:06d}.jsonl", raw)
            self.offset += 1
            self._raw.extend(raw)
            _require(len(self._raw) <= 67_108_864 and self.offset <= 65536, "网络原件总量超限")
        _require(type(response["record_count"]) is int and self.offset <= response["record_count"] <= 65536,
                 "网络实际记录数量非法")
        return response

    def close(self) -> dict[str, Any]:
        try:
            return self._close_original()
        except BaseException:
            self._abort_bridge()
            raise

    def _close_original(self) -> dict[str, Any]:
        if self.attestation is not None:
            verify_flow_attestation(bytes(self._raw), self.attestation, self.authority.public_key, self.context)
            return copy.deepcopy(self.attestation)
        if self._cleanup_deadline is None:
            self._cleanup_deadline = time.monotonic() + 30
        self.authority.revalidate(self.context["deployment"])
        value = self._call("close")
        _require(set(value) == {"ok", "attestation"}, "网络关闭缺真实签名原件")
        while True:
            read = self.read()
            if self.offset == read["record_count"]:
                _require(read["sealed"] is True, "网络尚未冻结尾部")
                break
        verify_flow_attestation(bytes(self._raw), value["attestation"], self.authority.public_key, self.context)
        native._new_file(self.root / "network-events.jsonl", bytes(self._raw))
        native._new_file(self.root / "network-attestation.json", c.canonical(value["attestation"]))
        released = self._call("release")
        _require(released == {"ok": True}, "网络原会话未释放")
        self.process.stdin.close()
        try:
            self.process.wait(timeout=self._remaining())
            # 原认证 bridge 已退出；仍核实际 EOF，不能静默丢弃最后一帧尾部。
            trailing = self.process.stdout.read()
            if trailing:
                _require(self._stdout_fd is not None, "网络原件读流缺失")
                view = memoryview(trailing)
                while view:
                    count = os.write(self._stdout_fd, view)
                    _require(count > 0, "网络尾部原件短写")
                    view = view[count:]
                os.fsync(self._stdout_fd)
            self.process.stdout.close()
            _require(self.process.returncode == 0 and not self._buffer and not trailing,
                     "网络 bridge 退出后还有尾部或失败")
            self._finish_streams()
        except BaseException:
            self._abort_bridge()
            raise
        self.attestation = copy.deepcopy(value["attestation"])
        return copy.deepcopy(self.attestation)

    def quiesce_owned(self, process_frames: Sequence[Mapping[str, Any]]) -> None:
        """产品仍存活时关闭本轮内核通道，并等真实 FlowClosed，不伪报关闭。

        这是回收动作，不能延长期限或再派发模型请求。产品已经退出导致无法
        闭合的来源仍拒绝；不能拿当前时间填成过去的关闭时刻。
        """
        try:
            self._quiesce_original(process_frames)
        except BaseException:
            self._abort_bridge()
            raise

    def _quiesce_original(self, process_frames: Sequence[Mapping[str, Any]]) -> None:
        if self._cleanup_deadline is None:
            self._cleanup_deadline = time.monotonic() + 30
        tokens = sorted({tuple(row["process"]["audit_token"]) for row in process_frames
                         if row.get("kind") == "process" and row.get("event") in {"exec", "fork"}})
        batches = [tokens[index:index+256] for index in range(0, len(tokens), 256)] or [[]]
        for batch in batches[:-1]:
            response = self._call("quiesce", audit_tokens=[list(token) for token in batch])
            _require(set(response) == {"ok", "pending_flow_ids"} and response["ok"] is True,
                     "原网络分批回收不符")
        while True:
            response = self._call("quiesce", audit_tokens=[list(token) for token in batches[-1]])
            _require(set(response) == {"ok", "pending_flow_ids"} and isinstance(response["pending_flow_ids"], list),
                     "原网络回收结果字段不符")
            self.read()
            if not response["pending_flow_ids"]:
                return
            time.sleep(min(self._remaining(), 0.01))

    def owned_flows(self, process_frames: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        _require(self.attestation is not None, "真实网络原件尚未签名收口")
        self.authority.revalidate(self.context["deployment"])
        verify_flow_attestation(bytes(self._raw), self.attestation, self.authority.public_key, self.context)
        return parse_owned_flows(bytes(self._raw), task=self.task, process_frames=process_frames, context=self.context)
