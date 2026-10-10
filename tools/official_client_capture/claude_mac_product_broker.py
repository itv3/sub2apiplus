"""固定 root ES broker 的构建与只读安装合同，不执行安装或提权。

普通用户只启动同一已签名 broker 的 client 模式，连接 launchd 的固定 socket。
root 服务实际核 peer audit token、冻结 launcher 用户和原生代码 requirement；
它只派生受管 ES worker。产品 UI 与回收继续保持登录用户，不因 root 旗标放行。
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_product_authority as authority

SCHEMA = "claude-mac-product-broker-installation/v1"
BROKER_ID = "com.sub2apiplus.claude.product-broker"
SOCKET = authority.INSTALL_ROOT / "product-events.sock"
PLIST = Path("/Library/LaunchDaemons/" + BROKER_ID + ".plist")
_TOKEN = object()


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise authority.ProductAuthorityError(message)


def launchd_plist() -> dict[str, Any]:
    """只生成可审阅内容；正式 socket 由现场已授权的 launchd 创建。"""
    return {"Label": BROKER_ID, "ProgramArguments": [str(authority.INSTALL_ROOT / "claude-product-broker"), "--product-broker-v1"],
        "UserName": "root", "KeepAlive": True,
        "Sockets": {"ProductEvents": {"SockPathName": str(SOCKET), "SockPathMode": 0o666, "SockType": "stream"}}}


@dataclass(frozen=True)
class ProductBrokerAuthority:
    """同一次固定读取构造的安装句柄；外部对象不具有来源或执行权。"""
    installation: Mapping[str, Any]
    installation_sha256: str
    executable: Path
    native: authority.ProductInstallationAuthority
    _token: object

    def revalidate(self, deployment: Mapping[str, Any]) -> dict[str, Any]:
        _require(self._token is _TOKEN, "ES broker 不是固定安装工厂构造")
        native_facts = self.native.revalidate(deployment)
        raw = authority.read_original(authority.INSTALL_ROOT / "broker-installation.json", maximum=65536, root_owned=True)
        value = json.loads(raw)
        _require(hashlib.sha256(raw).hexdigest() == self.installation_sha256
                 and c.canonical(value) == c.canonical(self.installation)
                 and value["deployment"] == dict(deployment)
                 and value["native_installation_sha256"] == native_facts["installation_sha256"], "ES broker 与原 native 部署不一致")
        image = authority.read_original(self.executable, root_owned=True)
        binding = value["broker_executable"]
        _require(set(binding) == {"path", "sha256", "bytes"} and binding["path"] == "claude-product-broker"
                 and len(image) == binding["bytes"] and hashlib.sha256(image).hexdigest() == binding["sha256"], "ES broker 映像字节漂移")
        signature = authority._signature(self.executable, value["code_signature"], require_es=False)
        _require(value["code_signature"]["identifier"] == BROKER_ID
                 and value["code_signature"]["team_id"] == self.native.installation["code_signature"]["team_id"], "ES broker 与原 worker 非同正式 Team")
        for path, requirement, identity in (
                (self.executable, value["client_requirement"], value["code_signature"]),
                (self.native.executable, value["worker_requirement"], self.native.installation["code_signature"])):
            _require(isinstance(requirement, str) and len(requirement) <= 4096
                     and f'identifier "{identity["identifier"]}"' in requirement and identity["team_id"] in requirement
                     and f'cdhash H"{identity["cdhash"]}"' in requirement, "ES peer requirement 缺正式 Team、独立身份或实际 CDHash")
            checked = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", "-R", requirement, str(path)],
                                     capture_output=True, timeout=30, check=False)
            _require(checked.returncode == 0, "ES peer requirement 与实际映像不符")
        raw_plist = authority.read_original(PLIST, maximum=65536, root_owned=True)
        _require(hashlib.sha256(raw_plist).hexdigest() == value["launchd_plist_sha256"]
                 and plistlib.loads(raw_plist) == launchd_plist(), "ES root 服务未按固定 launchd 合同安装")
        info = SOCKET.lstat()
        _require(stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o666,
                 "ES root 服务尚无真实受管 socket；不自动启动或提权")
        return {"deployment": dict(deployment), "installation_sha256": self.installation_sha256, "signature": signature}


def load_product_broker(native: authority.ProductInstallationAuthority) -> ProductBrokerAuthority:
    """缺正式安装、签名、真实 root 服务任一件都拒绝；不读调用方替代路径。"""
    import json
    try:
        raw = authority.read_original(authority.INSTALL_ROOT / "broker-installation.json", maximum=65536, root_owned=True)
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise authority.ProductAuthorityUnavailable("root ES broker 尚未完成受管现场安装") from error
    _require(set(value) == {"schema_version", "deployment", "native_installation_sha256", "broker_executable", "code_signature",
                 "client_requirement", "worker_requirement", "socket_path", "launchd_plist_sha256"}
             and value["schema_version"] == SCHEMA and value["socket_path"] == str(SOCKET), "ES broker 固定安装合同非法")
    result = ProductBrokerAuthority(value, hashlib.sha256(raw).hexdigest(),
             authority.INSTALL_ROOT / "claude-product-broker", native, _TOKEN)
    result.revalidate(value["deployment"])
    return result


def build_product_broker(output_root: Path) -> dict[str, Any]:
    """实际私有编译固定 broker，保存诊断与部署模板，不签名、安装或启动。"""
    from tools.official_client_capture import claude_mac_product_native as native
    root = Path(output_root)
    native._private_directory(root)
    _require(not list(root.iterdir()), "ES broker 私有构建根须为空")
    source = Path(__file__).with_name("claude_mac_product_broker_native.m")
    raw = authority.read_original(source)
    native._new_file(root / source.name, raw)
    sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, timeout=30, check=True).stdout.decode().strip()
    compiler = subprocess.run(["/usr/bin/xcrun", "--find", "clang"], capture_output=True, timeout=30, check=True).stdout.decode().strip()
    argv = [compiler, "-isysroot", sdk, "-arch", "arm64", "-mmacosx-version-min=15.0", "-fobjc-arc", "-fblocks",
            "-framework", "Foundation", "-framework", "Security", "-lbsm", "-O2", str(root / source.name), "-o", str(root / "claude-product-broker")]
    result = subprocess.run(argv, capture_output=True, timeout=120, check=False)
    native._new_file(root / "build.stdout", result.stdout)
    native._new_file(root / "build.stderr", result.stderr)
    _require(result.returncode == 0, "ES broker 编译失败；原始诊断已保存")
    native._new_file(root / (BROKER_ID + ".plist"), plistlib.dumps(launchd_plist()))
    _require(authority.read_original(source) == raw, "ES broker 编译期间源码变化")
    receipt = {"schema_version": "claude-mac-product-broker-build/v1", "argv": argv, "source": c.binding(root/source.name, root),
        "executable": c.binding(root/"claude-product-broker", root), "source_scope": "private_unsigned_build",
        "formal_team_signature": False, "executed": False, "installed": False, "registered": False, "launch_allowed": False}
    native._new_file(root / "build.json", c.canonical(receipt))
    return receipt
