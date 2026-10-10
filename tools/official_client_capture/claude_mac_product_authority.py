"""从固定受管安装核产品观察器身份；编译、路径或 JSON 旗标不能授正式权。

正式实例只读取 root 拥有且不可由普通用户改写的安装根，再实际运行系统
codesign 校验。外部上传的构建收据、ad-hoc 签名和未签名输出均不满足该门禁。
这里不执行签名、公证、安装、权限申请或 SOURCE_ADAPTERS 登记。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c


INSTALL_ROOT = Path("/Library/Application Support/sub2apiplus/claude-client-recorder")
AUTHORITY_SCHEMA = "claude-mac-product-installation/v1"
_TOKEN = object()


class ProductAuthorityError(ValueError):
    """安装来源、实际代码签名、身份或原件发生变化。"""


class ProductAuthorityUnavailable(ProductAuthorityError):
    """正式安装、签名或系统能力尚未就绪。"""


def _require(value: Any, message: str) -> None:
    if not value:
        raise ProductAuthorityError(message)


def read_original(path: Path, *, maximum: int = 67_108_864, root_owned: bool = False) -> bytes:
    """实际打开普通文件并重验路径实例，不把收据绑定当读取证明。"""
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts, "来源路径须为规范绝对路径")
    c.require_plain_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_size <= maximum, "来源须为有界普通单链接文件")
        if root_owned:
            _require(before.st_uid == 0 and stat.S_IMODE(before.st_mode) & 0o022 == 0,
                     "正式安装原件须由 root 拥有且禁止普通用户写入")
        chunks, size = [], 0
        while raw := os.read(fd, 65536):
            chunks.append(raw)
            size += len(raw)
            _require(size <= maximum, "来源读取超限")
        after = os.fstat(fd)
        current = path.lstat()
        names = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid", "st_nlink")
        _require(all(getattr(before, name) == getattr(after, name) == getattr(current, name)
                     for name in names), "来源读取时目录项或字节发生变化")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _signature(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    """系统工具只核冻结映像；不执行映像，不自动申请权限。"""
    _require(set(expected) == {"team_id", "identifier", "cdhash"}
             and isinstance(expected["team_id"], str) and re.fullmatch(r"[A-Z0-9]{10}", expected["team_id"])
             and isinstance(expected["identifier"], str) and bool(expected["identifier"])
             and isinstance(expected["cdhash"], str) and re.fullmatch(r"[0-9a-f]{40}", expected["cdhash"]),
             "正式代码签名冻结值非法")
    verify = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(path)],
                            capture_output=True, timeout=30, check=False)
    if verify.returncode:
        raise ProductAuthorityUnavailable("实际 codesign 校验失败，不能执行原生产品观察器")
    identity = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(path)],
                              capture_output=True, timeout=30, check=False)
    entitlement = subprocess.run(["/usr/bin/codesign", "--display", "--entitlements", ":-", str(path)],
                                 capture_output=True, timeout=30, check=False)
    _require(identity.returncode == entitlement.returncode == 0, "无法读取实际签名身份或权限声明")
    rows = dict(line.split("=", 1) for line in identity.stderr.decode("utf-8", "strict").splitlines()
                if "=" in line)
    _require(rows.get("TeamIdentifier") == expected["team_id"]
             and rows.get("Identifier") == expected["identifier"]
             and rows.get("CDHash", "").lower() == expected["cdhash"], "实际签名身份与受管冻结值不符")
    raw = entitlement.stdout
    start = raw.find(b"<?xml")
    _require(start >= 0, "正式观察器缺实际权限 plist")
    permissions = plistlib.loads(raw[start:])
    _require(permissions.get("com.apple.developer.endpoint-security.client") is True,
             "正式观察器缺实际 EndpointSecurity entitlement")
    return {"identity": dict(expected), "identity_output_sha256": hashlib.sha256(identity.stderr).hexdigest(),
            "entitlements_sha256": hashlib.sha256(raw).hexdigest()}


@dataclass(frozen=True)
class ProductInstallationAuthority:
    """由固定安装读取器构造的只读句柄；外部 dataclass 或旗标不能代替来源核验。"""

    installation: Mapping[str, Any]
    installation_sha256: str
    executable: Path
    _token: object

    def revalidate(self, deployment: Mapping[str, Any]) -> dict[str, Any]:
        _require(self._token is _TOKEN, "产品来源句柄不是固定安装工厂构造")
        _require(platform.system() == "Darwin" and platform.machine() == "arm64", "正式产品来源只支持 Darwin ARM64")
        for directory in (INSTALL_ROOT, *INSTALL_ROOT.parents):
            info = directory.lstat()
            _require(stat.S_ISDIR(info.st_mode) and not directory.is_symlink() and info.st_uid == 0
                     and stat.S_IMODE(info.st_mode) & 0o022 == 0, "受管安装目录可被普通用户改写")
        raw = read_original(INSTALL_ROOT / "installation.json", maximum=65536, root_owned=True)
        # 消费同一次 FD 实读的字节，不能在验完摘要后再从路径重读一份 JSON。
        actual = json.loads(raw)
        _require(hashlib.sha256(raw).hexdigest() == self.installation_sha256
                 and actual == dict(self.installation) and actual["deployment"] == dict(deployment),
                 "产品受管安装或任务部署身份已经变化")
        binding = actual["native_executable"]
        _require(set(binding) == {"path", "sha256", "bytes"} and binding["path"] == "claude-product-events",
                 "原生观察器不是固定安装映像")
        image = read_original(self.executable, root_owned=True)
        _require(len(image) == binding["bytes"] and hashlib.sha256(image).hexdigest() == binding["sha256"],
                 "原生观察器实际字节与安装冻结值不符")
        signature = _signature(self.executable, actual["code_signature"])
        return {"installation_sha256": self.installation_sha256, "deployment": dict(deployment),
                "executable": dict(binding), "signature": signature}


def load_product_installation() -> ProductInstallationAuthority:
    """只读加载固定受管安装；缺失时零启动，不读取用户提供的替代路径。"""
    try:
        raw = read_original(INSTALL_ROOT / "installation.json", maximum=65536, root_owned=True)
    except (OSError, ValueError) as error:
        raise ProductAuthorityUnavailable("产品原生观察器尚无可信受管安装") from error
    value = json.loads(raw)
    _require(isinstance(value, dict) and set(value) == {"schema_version", "deployment", "native_executable", "code_signature"}
             and value["schema_version"] == AUTHORITY_SCHEMA, "受管产品安装合同非法")
    authority = ProductInstallationAuthority(value, hashlib.sha256(raw).hexdigest(),
                                             INSTALL_ROOT / "claude-product-events", _TOKEN)
    authority.revalidate(value["deployment"])
    return authority
