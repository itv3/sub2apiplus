"""从明确的 Mac 产品安装根冻结真实物料，供离线诊断计划消费。

只读应用、扩展和显式指定的 Desktop CLI 安装目录，不启动产品、不扫描
用户状态、不读取环境或凭据。产品树允许终点仍在本树内的链接；归档原件
继续采用现有证据合同的普通文件规则。冻结与派生均不能授予正式资格。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import plistlib
import re
import stat
import struct
from pathlib import Path, PurePosixPath
from typing import Any

from tools.official_client_capture import claude_client_receipt_contract as c

MATERIALS_SCHEMA = "claude-mac-product-materials/v1"
_FLAGS = {"source_scope": "readonly_diagnostic", "registration_verified": False,
          "formal_ready": False, "launch_allowed": False}
_CHUNK = 1024 * 1024
_SOURCE_LIMIT = 32 * _CHUNK
_VERSION_LINE = re.compile(rb"// Version: ([0-9]+\.[0-9]+\.[0-9]+)\r?\n")
_CONFIG_SOURCE = re.compile(rb"(?:`|[\"'])\.claude[^;\n]{0,96}[\"'`];return [^;\n]{0,64}process\.env\.CLAUDE_CONFIG_DIR\|\|[^;\n]{0,64}?\}")


class ProductMaterialsError(ValueError):
    """安装树、公开原件、版本或冻结收据不一致。"""


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ProductMaterialsError(message)


def _relative(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    _require(isinstance(value, str) and bool(value) and not path.is_absolute()
             and path.as_posix() == value and all(part not in {".", ".."} for part in path.parts)
             and not any(ord(char) < 32 for char in value), "产品相对路径非法")
    return path.parts


def _root(value: Path) -> Path:
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "产品根须为明确的规范绝对目录")
    c.require_plain_path(path)
    _require(path.is_dir(), "产品根不存在或不是目录")
    return path


def _signature(info: os.stat_result) -> tuple[int, ...]:
    """实例、权限、长度和修改时间都须稳定，同字节替换也不能穿过读中检查。"""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _directory_fd(path: Path) -> int:
    """从文件系统根逐级拒绝链接，返回所选目录实例的描述符。"""
    _require(path.is_absolute() and ".." not in path.parts, "目录须为规范绝对路径")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise ProductMaterialsError("目录缺失、含链接或实例已漂移") from error
    except BaseException:
        os.close(descriptor)
        raise


def _open(root: Path, relative: str) -> int:
    """以目录描述符逐级打开，不能通过被替换的父链接读到产品根以外。"""
    parts = _relative(relative)
    directory = _directory_fd(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        raise ProductMaterialsError("物料文件缺失、含链接或路径实例已漂移") from error
    finally:
        os.close(directory)


def _read(root: Path, relative: str, *, limit: int = _SOURCE_LIMIT) -> bytes:
    descriptor = _open(root, relative)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_size <= limit,
                 "公开原件不是有界普通文件")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        _require(len(raw) == before.st_size and _signature(before) == _signature(os.fstat(descriptor)),
                 "公开原件读取过程中发生漂移")
        return raw
    finally:
        os.close(descriptor)


def _tree(root: Path) -> tuple[dict[str, Any], dict[str, tuple[int, ...]]]:
    """不遍历链接目录；链接原文、树内终点和普通文件原字节共同进入树摘要。"""
    rows: list[dict[str, Any]] = []
    signatures: dict[str, tuple[int, ...]] = {}
    root_fd = _directory_fd(root)
    device = os.fstat(root_fd).st_dev

    def visit(directory: int, prefix: str) -> None:
        before_directory = os.fstat(directory)
        signatures[prefix or "."] = _signature(before_directory)
        rows.append({"path": prefix or ".", "kind": "directory",
                     "mode": stat.S_IMODE(before_directory.st_mode)})
        for name in sorted(os.listdir(directory)):
            _relative(name)
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            _require(info.st_dev == device, "产品树不得跨越其他文件系统")
            signatures[relative] = _signature(info)
            row = {"path": relative, "mode": stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=directory)
                try:
                    endpoint = (root / relative).resolve(strict=True)
                except (OSError, RuntimeError) as error:
                    raise ProductMaterialsError("产品链接悬空或成环") from error
                _require(endpoint.is_relative_to(root), "产品链接终点越出产品根")
                row.update(kind="symlink", target=target, resolved=endpoint.relative_to(root).as_posix())
                rows.append(row)
            elif stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    _require(_signature(os.fstat(child)) == _signature(info), "产品目录打开时漂移")
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    _require(_signature(os.fstat(descriptor)) == _signature(info), "产品文件打开时漂移")
                    digest, size = hashlib.sha256(), 0
                    while raw := os.read(descriptor, _CHUNK):
                        digest.update(raw)
                        size += len(raw)
                    _require(size == info.st_size and _signature(os.fstat(descriptor)) == _signature(info),
                             "产品文件读取过程中漂移")
                    row.update(kind="file", sha256=digest.hexdigest(), bytes=size)
                    rows.append(row)
                finally:
                    os.close(descriptor)
            else:
                raise ProductMaterialsError("产品树含特殊文件")
            _require(_signature(os.stat(name, dir_fd=directory, follow_symlinks=False)) == _signature(info),
                     "产品条目读取后漂移")
        _require(_signature(os.fstat(directory)) == _signature(before_directory), "产品目录遍历时漂移")

    try:
        visit(root_fd, "")
        _require(_signature(root.lstat()) == signatures["."], "产品根实例发生替换")
    finally:
        os.close(root_fd)
    rows.sort(key=lambda row: row["path"])
    return {"root": str(root), "tree_sha256": c.digest(rows),
            "bytes": sum(row.get("bytes", 0) for row in rows), "entries": rows}, signatures


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ProductMaterialsError(f"{label}不是合法JSON原件") from error
    _require(isinstance(value, dict), f"{label}须为对象")
    return value


def _plist(raw: bytes, identity: str, executable: str) -> dict[str, Any]:
    try:
        value = plistlib.loads(raw)
    except (ValueError, plistlib.InvalidFileException) as error:
        raise ProductMaterialsError("产品Info.plist原件非法") from error
    _require(isinstance(value, dict) and value.get("CFBundleIdentifier") == identity
             and value.get("CFBundleExecutable") == executable
             and isinstance(value.get("CFBundleShortVersionString"), str)
             and bool(value["CFBundleShortVersionString"]), "产品身份、executable或独立版本不符")
    return value


def _asar(root: Path) -> dict[str, bytes]:
    """只提取普通ASAR成员的公开源码，不执行脚本或读取unpacked／链接成员。"""
    descriptor = _open(root, "Contents/Resources/app.asar")
    try:
        before = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            header = stream.read(16)
            _require(len(header) == 16 and stat.S_ISREG(before.st_mode), "ASAR头不完整")
            a, length, payload, json_length = struct.unpack("<4I", header)
            _require(a == 4 and 4 <= length <= _SOURCE_LIMIT and payload == length - 4
                     and 0 < json_length <= payload - 4 and 8 + length <= before.st_size,
                     "ASAR pickle边界非法")
            tree = _json(stream.read(json_length), "ASAR索引")
            start = 8 + length

            def member(relative: str) -> bytes:
                row = tree
                for part in _relative(relative):
                    _require(isinstance(row.get("files"), dict) and part in row["files"], "ASAR公开成员缺失")
                    row = row["files"][part]
                _require(isinstance(row, dict) and not row.get("unpacked") and "link" not in row
                         and type(row.get("size")) is int and 0 <= row["size"] <= _SOURCE_LIMIT
                         and isinstance(row.get("offset"), str) and row["offset"].isdigit(),
                         "ASAR公开成员不是有界内置普通文件")
                offset = start + int(row["offset"])
                _require(offset + row["size"] <= before.st_size, "ASAR成员越出归档")
                stream.seek(offset)
                raw = stream.read(row["size"])
                _require(len(raw) == row["size"], "ASAR成员截断")
                return raw

            package_raw = member("package.json")
            package = _json(package_raw, "Desktop package")
            main_raw = member(str(package.get("main", "")))
            build = tree.get("files", {}).get(".vite", {}).get("files", {}).get("build", {}).get("files", {})
            originals = {"app-package.json": package_raw, "desktop-main.js": main_raw}
            pins = []
            for name in sorted(build):
                if name.startswith("index") and name.endswith(".js"):
                    raw = member(".vite/build/" + name)
                    for match in re.finditer(rb"JSON\.parse\('([^'\\]*)'\)", raw):
                        try:
                            pin = json.loads(match[1])
                        except (ValueError, UnicodeError):
                            continue
                        # Desktop还包含按commit命名的其他工具manifest，不能与CLI的
                        # 独立语义版本／SDK wrapper pin混为一谈。
                        if (isinstance(pin, dict) and isinstance(pin.get("manifest"), dict)
                                and "platforms" in pin["manifest"]
                                and isinstance(pin.get("version"), str)
                                and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pin["version"])
                                and isinstance(pin.get("sdkWrapperVersion"), str) and pin["sdkWrapperVersion"]):
                            pins.append((pin, raw, match[1]))
            _require(len(pins) == 1, "Desktop须有唯一可静态解析的CLI pin，未知源码形态必须拒绝")
            originals["desktop-cli-pin.js"] = pins[0][1]
            # 该JSON是源码中的原字节片段；完整JS同时归档，不用重序列化投影冒充原件。
            originals["desktop-cli-pin.json"] = pins[0][2]
        _require(_signature(before) == _signature(os.fstat(descriptor)), "ASAR读取时漂移")
        return originals
    finally:
        os.close(descriptor)


def _binary_facts(root: Path, relative: str) -> tuple[bytes, bytes, str, bytes, int]:
    descriptor = _open(root, relative)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_mode & 0o111, "客户端可执行制品缺执行权限")
        header = os.read(descriptor, 32)
        _require(len(header) == 32 and header[:4] == b"\xcf\xfa\xed\xfe"
                 and struct.unpack_from("<I", header, 4)[0] == 0x0100000C,
                 "仅接受已实读的Mach-O ARM64可执行物料")
        banners, tail, consumed = {}, header, len(header)
        config_source, config_offset = b"", -1
        while raw := os.read(descriptor, _CHUNK):
            combined = tail + raw
            for match in _VERSION_LINE.finditer(combined):
                banners.setdefault(match[1].decode("ascii"), match[0])
            if not config_source and (match := _CONFIG_SOURCE.search(combined)):
                config_source, config_offset = match[0], consumed - len(tail) + match.start()
            consumed += len(raw)
            tail = combined[-512:]
        _require(_signature(before) == _signature(os.fstat(descriptor)), "可执行制品读取时漂移")
        _require(len(banners) <= 1, "CLI本体版本注释存在冲突")
        version = next(iter(banners), "")
        return header, banners.get(version, b""), version, config_source, config_offset
    finally:
        os.close(descriptor)


def _inspect(client_id: str, *, application_root: Path, extension_root: Path | None = None,
             desktop_cli_root: Path | None = None) -> tuple[dict[str, Any], dict[str, bytes]]:
    _require(client_id in c.CLIENTS, "客户端非法")
    _require((client_id == "claude-desktop" and desktop_cli_root is not None and extension_root is None)
             or (client_id == "claude-vscode" and extension_root is not None and desktop_cli_root is None),
             "须显式指定本客户端的独立物料根，不能猜测CLI或扩展目录")
    roots = {"application": _root(application_root)}
    roots["desktop_cli" if client_id == "claude-desktop" else "extension"] = _root(
        desktop_cli_root if client_id == "claude-desktop" else extension_root)
    _require(all(not a.is_relative_to(b) and not b.is_relative_to(a)
                 for i, a in enumerate(roots.values()) for b in list(roots.values())[i + 1:]),
             "独立产品根不得相互重叠")
    # 先核明确的公开产品入口再遍历；被篡改收据不能把普通用户状态目录当产品根扫描。
    _require(roots["application"].suffix == ".app", "应用根须为明确的.app产品包")
    _plist(_read(roots["application"], "Contents/Info.plist"),
           "com.anthropic.claudefordesktop" if client_id == "claude-desktop" else "com.microsoft.VSCode",
           "Claude" if client_id == "claude-desktop" else "Code")
    if client_id == "claude-desktop":
        _plist(_read(roots["desktop_cli"], "claude.app/Contents/Info.plist"), "com.anthropic.claude-code", "claude")
    else:
        extension = _json(_read(roots["extension"], "package.json"), "Claude扩展入口")
        _require(extension.get("name") == "claude-code" and str(extension.get("publisher", "")).lower() == "anthropic",
                 "扩展根缺明确的Claude公开产品入口")
    snapshots = {name: _tree(root) for name, root in roots.items()}
    originals: dict[str, bytes] = {}

    def public(name: str, root_name: str, relative: str) -> bytes:
        raw = _read(roots[root_name], relative)
        entry = next((row for row in snapshots[root_name][0]["entries"] if row["path"] == relative), None)
        _require(entry is not None and entry.get("sha256") == hashlib.sha256(raw).hexdigest(),
                 "公开原件与产品库存摘要不一致")
        originals[name] = raw
        return raw

    desktop = client_id == "claude-desktop"
    executable_name = "Claude" if desktop else "Code"
    info = _plist(public("app-info.plist", "application", "Contents/Info.plist"),
                  "com.anthropic.claudefordesktop" if desktop else "com.microsoft.VSCode", executable_name)
    versions = {"product": info["CFBundleShortVersionString"]}
    roles = {"application": ("application", ".", "app_bundle", "product"),
             "application_executable": ("application", "Contents/MacOS/" + executable_name, "host_executable", "host")}
    facts: dict[str, Any] = {}
    if desktop:
        originals.update(_asar(roots["application"]))
        package = _json(originals["app-package.json"], "Desktop package")
        _require(package.get("name") == "@ant/desktop" and package.get("version") == versions["product"],
                 "Desktop应用与ASAR package版本不一致")
        pin = _json(originals["desktop-cli-pin.json"], "Desktop CLI pin")
        try:
            platform = pin["manifest"]["platforms"]["darwin-arm64"]
            cli_version = pin["version"]
            package_sha, bundle_sha = platform["checksum"], platform["bundle"]["checksum"]
            _require(pin["manifest"]["version"] == cli_version and c.SHA_RE.fullmatch(package_sha)
                     and c.SHA_RE.fullmatch(bundle_sha), "Desktop pin版本或下载摘要非法")
        except (KeyError, TypeError) as error:
            raise ProductMaterialsError("Desktop pin缺ARM64二进制／bundle下载摘要") from error
        cli_root = roots["desktop_cli"]
        _require(cli_root.name == bundle_sha[:12] and cli_root.parent.name == cli_version,
                 "Desktop CLI根须恰为pin版本／bundle摘要前12位，不能挑最高目录或旧CLI")
        cli_info = _plist(public("embedded-cli-info.plist", "desktop_cli", "claude.app/Contents/Info.plist"),
                          "com.anthropic.claude-code", "claude")
        _require(cli_info["CFBundleShortVersionString"] == cli_version
                 and cli_info.get("CFBundleVersion") == cli_version, "Desktop CLI plist与pin版本不一致")
        verified = public("embedded-cli-verification.txt", "desktop_cli", ".verified").decode("ascii").strip()
        _require(verified == bundle_sha, "Desktop CLI .verified与pin的bundle安装包摘要不一致")
        _require(b"process.env.CLAUDE_USER_DATA_DIR" in originals["desktop-main.js"]
                 and b'setPath("userData",' in originals["desktop-main.js"]
                 and b'getPath("userData"),"claude-code"' in originals["desktop-cli-pin.js"],
                 "Desktop公开源码未给出所需私有userData与CLI缓存路径线索")
        facts["desktop_pin"] = {"version": cli_version, "commit": pin["manifest"].get("commit"),
                                 "base_url": pin.get("baseUrl"), "sdk_wrapper_version": pin.get("sdkWrapperVersion"),
                                 "download_binary_sha256": package_sha, "download_bundle_sha256": bundle_sha}
        roles["embedded_cli"] = ("desktop_cli", "claude.app/Contents/MacOS/claude", "embedded_cli", "embedded_cli")
    else:
        package = _json(public("app-package.json", "application", "Contents/Resources/app/package.json"), "Code package")
        _require(package.get("name") == "Code" and package.get("version") == versions["product"],
                 "Code宿主与package独立版本不一致")
        product = _json(public("app-product.json", "application", "Contents/Resources/app/product.json"), "Code product")
        _require(product.get("applicationName") == "code" and product.get("version") == versions["product"],
                 "Code产品元数据与独立版本不一致")
        portable_name = product.get("portable") or "code-portable-data"
        _require(isinstance(portable_name, str) and len(_relative(portable_name)) == 1, "portable数据目录名非法")
        portable = roots["application"].parent / portable_name
        _require(not portable.exists() and not portable.is_symlink(),
                 "Code存在portable数据目录，不能沿用普通安装参数")
        cli_raw = public("code-cli.js", "application", "Contents/Resources/app/out/cli.js")
        _require(all(token in cli_raw for token in (b"user-data-dir", b"extensions-dir", b"inspect-extensions")),
                 "Code公开CLI解析器缺私有目录参数")
        extension = _json(public("extension-package.json", "extension", "package.json"), "Claude扩展package")
        _require(extension.get("name") == "claude-code" and str(extension.get("publisher", "")).lower() == "anthropic"
                 and isinstance(extension.get("version"), str) and bool(extension["version"]), "Claude扩展身份或独立版本不符")
        entry = str(extension.get("main", ""))
        entry = entry[2:] if entry.startswith("./") else entry
        _relative(entry)
        public("extension-entry.js", "extension", entry)
        properties = extension.get("contributes", {}).get("configuration", {}).get("properties", {})
        env = properties.get("claudeCode.environmentVariables", {})
        _require(env.get("type") == "array" and env.get("items", {}).get("required") == ["name", "value"],
                 "扩展未声明固定名称／值的环境设置形态")
        versions["extension_package"] = extension["version"]
        cli_version = extension["version"]
        roles.update(extension_host=("application", "Contents/Frameworks/Code Helper (Plugin).app/Contents/MacOS/Code Helper (Plugin)", "host_executable", "host"),
                     extension_package=("extension", ".", "extension_package", "extension_package"),
                     extension_entry=("extension", entry, "extension_entry", "extension_package"),
                     embedded_cli=("extension", "resources/native-binary/claude", "embedded_cli", "embedded_cli"))
    versions["host"] = versions["product"]
    versions["embedded_cli"] = cli_version
    for role in ("application_executable", "embedded_cli", *(("extension_host",) if not desktop else ())):
        root_name, relative, _, _ = roles[role]
        header, banner, binary_version, config_source, config_offset = _binary_facts(roots[root_name], relative)
        originals[role + "-header.bin"] = header
        if role == "embedded_cli":
            _require(binary_version == cli_version, "内置CLI本体版本注释与pin／扩展package不一致")
            _require(bool(config_source), "内置CLI公开源码没有明确的CLAUDE_CONFIG_DIR配置路径线索")
            originals["embedded-cli-version.txt"] = banner
            originals["embedded-cli-config-source.js"] = config_source
            facts["embedded_cli_config_source"] = {"product_relative_path": relative,
                                                    "byte_offset": config_offset, "bytes": len(config_source)}
    originals = {"public/" + name: raw for name, raw in originals.items()}
    bindings = [{"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                for name, raw in sorted(originals.items())]
    support = {"client_id": client_id, **_FLAGS, "observation": "public_materials_only",
               "versions": versions, "originals": bindings, "facts": facts}
    originals["isolation-support.json"] = c.canonical(support)
    artifacts = []
    for role, (root_name, relative, kind, version_kind) in sorted(roles.items()):
        tree = snapshots[root_name][0]
        if relative == ".":
            content = {"kind": "directory", "path": str(roots[root_name]),
                       "sha256": tree["tree_sha256"], "bytes": tree["bytes"]}
        else:
            row = next((row for row in tree["entries"] if row["path"] == relative), {})
            _require(row.get("kind") == "file", "真实制品角色须绑定普通文件")
            content = {"kind": "file", "path": str(roots[root_name] / relative), "sha256": row["sha256"], "bytes": row["bytes"]}
        version = {"kind": version_kind, "value": versions[version_kind]}
        origin_path = "origins/" + role + ".json"
        origin = {"artifact_id": role, "content": content, "version": version,
                  **_FLAGS, "observation": "public_materials_only", "originals": bindings}
        originals[origin_path] = c.canonical(origin)
        artifacts.append({"artifact_id": role, "role": role, "kind": kind,
                          "product": "claude-desktop" if desktop else "claude-code-vscode",
                          "version": version, "platform": "darwin/arm64", "content": content,
                          "official_source": {"path": origin_path, "sha256": hashlib.sha256(originals[origin_path]).hexdigest(),
                                              "bytes": len(originals[origin_path])}})
    inventories = []
    for name, (tree, signatures) in sorted(snapshots.items()):
        current_tree, current_signatures = _tree(roots[name])
        _require(current_tree == tree and current_signatures == signatures, "产品树读取前后实例或字节发生漂移")
        path = "inventories/" + name + ".json"
        originals[path] = c.canonical(tree)
        inventories.append({"path": path, "sha256": hashlib.sha256(originals[path]).hexdigest(), "bytes": len(originals[path])})
    result = {"schema_version": MATERIALS_SCHEMA, "client_id": client_id, **_FLAGS,
              "roots": {name: str(root) for name, root in roots.items()}, "versions": versions,
              "client_artifacts": artifacts, "inventories": inventories, "public_originals": bindings,
              "official_support": {"path": "isolation-support.json", "sha256": hashlib.sha256(originals["isolation-support.json"]).hexdigest(),
                                   "bytes": len(originals["isolation-support.json"])}, "facts": facts}
    result["receipt_sha256"] = c.digest(result)
    return result, originals


def inspect_product_materials(client_id: str, *, application_root: Path,
                              extension_root: Path | None = None, desktop_cli_root: Path | None = None) -> dict[str, Any]:
    """只读实测目录库存、公开源码、精确角色与各自版本，不创建物料归档。"""
    result, _ = _inspect(client_id, application_root=application_root, extension_root=extension_root,
                         desktop_cli_root=desktop_cli_root)
    return copy.deepcopy(result)


def _write_once(root_fd: int, relative: str, raw: bytes) -> None:
    """所有发布操作都相对同一私有根实例，父目录被替换为链接也不会越界写入。"""
    parts = _relative(relative)
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            if os.fstat(child).st_mode & 0o077:
                os.close(child)
                raise ProductMaterialsError("归档子目录须保持私有")
            os.fsync(parent)
            os.close(parent)
            parent = child
        descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)
    finally:
        os.close(parent)


def freeze_product_materials(client_id: str, *, application_root: Path, output_root: Path,
                             extension_root: Path | None = None, desktop_cli_root: Path | None = None) -> dict[str, Any]:
    """独占创建私有归档，最后发布收据；相同原字节重入只核对，失败残件不覆盖。"""
    result, originals = _inspect(client_id, application_root=application_root, extension_root=extension_root,
                                  desktop_cli_root=desktop_cli_root)
    output = Path(output_root)
    c.require_plain_path(output)
    _require(output.is_absolute() and output.parent.is_dir(), "冻结根的普通父目录必须已存在")
    _require(all(not output.is_relative_to(Path(value)) and not Path(value).is_relative_to(output)
                 for value in result["roots"].values()), "物料归档不得与只读产品根重叠")
    receipt_raw = c.canonical(result)
    parent_fd = _directory_fd(output.parent)
    try:
        try:
            os.mkdir(output.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            _require(output.is_dir() and output.stat().st_mode & 0o077 == 0, "已有物料根不是私有普通目录")
            for name, raw in {**originals, "product-materials.json": receipt_raw}.items():
                _require(_read(output, name) == raw, "已发布物料原字节不同或残件缺失，禁止覆盖")
            return copy.deepcopy(result)
        root_fd = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            root_instance = os.fstat(root_fd)
            for name, raw in originals.items():
                _write_once(root_fd, name, raw)
            # 普通原件和目录先持久化；完成收据最后发布，残件从不成为可承接结果。
            _write_once(root_fd, "product-materials.json", receipt_raw)
            os.fsync(root_fd)
            c.require_plain_path(output)
            _require((output.stat().st_dev, output.stat().st_ino) == (root_instance.st_dev, root_instance.st_ino),
                     "归档根实例在发布时被替换")
        finally:
            os.close(root_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return copy.deepcopy(result)


def read_product_materials(materials_receipt: Path) -> dict[str, Any]:
    """重读冻结原件并复测明确产品根；JSON旗标或调用方自报verified不参与资格。"""
    receipt = Path(materials_receipt)
    _require(receipt.name == "product-materials.json", "须读取准确物料冻结收据")
    _root(receipt.parent)
    actual = _json(_read(receipt.parent, receipt.name), "物料收据")
    _require(actual.get("schema_version") == MATERIALS_SCHEMA
             and actual.get("receipt_sha256") == c.digest({key: value for key, value in actual.items() if key != "receipt_sha256"})
             and all(actual.get(key) == value for key, value in _FLAGS.items()), "物料收据版本、摘要或只读用途不符")
    roots = actual.get("roots", {})
    current, originals = _inspect(actual.get("client_id"), application_root=Path(roots.get("application", "")),
                                  extension_root=Path(roots["extension"]) if "extension" in roots else None,
                                  desktop_cli_root=Path(roots["desktop_cli"]) if "desktop_cli" in roots else None)
    _require(current == actual, "冻结收据与当前实读产品物料不一致")
    for name, raw in originals.items():
        _require(_read(receipt.parent, name) == raw, "冻结原件与实读物料不一致")
    return copy.deepcopy(actual)
