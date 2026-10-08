"""固定私有 wheel 的完整字节闭包与受管导入。

本模块只读取调用者已冻结的任务私有目录，不下载、不安装、不加入 sys.path。
生产解码只允许官方 hpack 4.1.0；h2／hyperframe 仅由测试 peer 合同启用。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import io
import os
import stat
import sys
import threading
import zipfile
from email.parser import BytesParser
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from tools.official_client_capture import claude_mac_capture_contract as c

SCHEMA = "claude-upgrade-h2-private-dependencies/v1"
PINS = {
    "hpack": ("4.1.0", "157ac792668d995c657d93111f46b4535ed114f0c9c8d672271bbec7eae1b496"),
    "h2": ("4.3.0", "c438f029a25f7945c69e0ccf0fb951dc3f73a5f6412981daee861431b70e2bdd"),
    "hyperframe": ("6.1.0", "b03380493a519fce58ea5af42e4a42317bf9bd425596f7a0835ffce80f1a42e5"),
}
_PACKAGE_FIELDS = {"name", "version", "root", "metadata_url", "metadata", "wheel_url", "wheel",
                   "files", "licenses", "requires_dist", "vulnerabilities"}
_IMPORT_LOCK = threading.RLock()


def _private_directory(path: Path) -> None:
    info = c.plain_path(path).stat()
    c.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
              and stat.S_IMODE(info.st_mode) == 0o700, "H2 依赖目录不属于当前用户或不是 0700")


class _FrozenLoader(importlib.abc.Loader):
    """只编译先验验过的 wheel 原字节；不读取或生成 pyc。"""

    def __init__(self, bundle: FrozenDependencies, name: str, path: Path, raw: bytes):
        self.bundle, self.name, self.path, self.raw = bundle, name, path, raw

    def create_module(self, spec):
        return None

    def exec_module(self, module: ModuleType) -> None:
        module.__file__ = str(self.path)
        exec(compile(self.raw, str(self.path), "exec", dont_inherit=True), module.__dict__)


class _FrozenFinder(importlib.abc.MetaPathFinder):
    """受管命名空间内找不到的模块直接拒绝，不能回退系统包或另一个路径。"""

    def __init__(self, bundle: FrozenDependencies):
        self.bundle = bundle

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname.split(".", 1)[0] not in self.bundle.names:
            return None
        source = self.bundle.sources.get(fullname)
        c.require(source is not None, "受管 H2 包不含请求的模块")
        file_path, raw, is_package = source
        loader = _FrozenLoader(self.bundle, fullname, file_path, raw)
        return importlib.util.spec_from_file_location(
            fullname, str(file_path), loader=loader,
            submodule_search_locations=[str(file_path.parent)] if is_package else None)


class FrozenDependencies:
    """以外部受管配置提供的 manifest 摘要为根，重验官方 wheel 和全部解包文件。"""

    source_scope = c.SCOPE
    formal_ready = False
    launch_allowed = False

    def __init__(self, root: Path, manifest_binding: Mapping[str, Any], *,
                 kind: str = "production_decoder", artifact_only: bool = False):
        self.root = c.plain_path(root)
        _private_directory(self.root)
        self.manifest_binding = dict(manifest_binding)
        self.kind = kind
        c.require(type(artifact_only) is bool, "依赖原件校验用途非法")
        self.artifact_only = artifact_only
        c.require(kind in {"production_decoder", "test_peer"}, "H2 依赖用途非法")
        self.manifest = c.read_json(c.check_binding(self.root, self.manifest_binding))
        c.exact(self.manifest, {"schema_version", "source_scope", "formal_ready", "launch_allowed",
                               "kind", "packages"}, "H2 私有依赖合同")
        c.require(self.manifest["schema_version"] == SCHEMA
                  and self.manifest["source_scope"] == c.SCOPE
                  and self.manifest["formal_ready"] is False
                  and self.manifest["launch_allowed"] is False
                  and self.manifest["kind"] == kind, "H2 依赖不能启用正式来源或另一种用途")
        c.require(isinstance(self.manifest["packages"], list), "H2 包清单非法")
        self.packages: dict[str, dict[str, Any]] = {}
        for item in self.manifest["packages"]:
            row = c.exact(item, _PACKAGE_FIELDS, "H2 包合同")
            name = row["name"]
            c.require(isinstance(name, str) and name in PINS and name not in self.packages,
                      "H2 依赖名称不在固定闭集或重复")
            self.packages[name] = row
        self.names = frozenset(self.packages)
        c.require(self.names == ({"hpack"} if kind == "production_decoder" else set(PINS)),
                  "生产只能读取 hpack，测试 peer 必须绑定固定三包")
        self.sources: dict[str, tuple[Path, bytes, bool]] = {}
        self.read_set: list[dict[str, Any]] = []
        self._finder: _FrozenFinder | None = None
        self.revalidate()
        for name, row in self.packages.items():
            package_root = c.managed_path(self.root, row["root"])
            for item in row["files"]:
                path = c.managed_path(self.root, item["path"])
                relative = path.relative_to(package_root)
                if not relative.parts or relative.parts[0] != name or path.suffix != ".py":
                    continue
                parts = list(relative.with_suffix("").parts)
                is_package = parts[-1] == "__init__"
                if is_package:
                    parts.pop()
                fullname = ".".join(parts)
                raw = c.read_bytes(path)
                c.require(len(raw) == item["bytes"] and hashlib.sha256(raw).hexdigest() == item["sha256"],
                          "H2 待编译源字节在读取时发生漂移")
                self.sources[fullname] = (path, raw, is_package)

    def revalidate(self) -> None:
        """ready、解码和收口均可调用；检查全包文件漂移及已经加载的实际 origin。"""
        _private_directory(self.root)
        c.require(c.read_json(c.check_binding(self.root, self.manifest_binding)) == self.manifest,
                  "H2 私有依赖 manifest 漂移")
        reads = [self.manifest_binding]
        for name, row in self.packages.items():
            version, wheel_sha = PINS[name]
            c.require(row["version"] == version
                      and row["root"] == f"packages/{name}-{version}"
                      and row["metadata_url"] == f"https://pypi.org/pypi/{name}/{version}/json",
                      "H2 固定包版本、根或官方来源不符")
            metadata_path = c.check_binding(self.root, row["metadata"])
            metadata = c.read_json(metadata_path)
            c.require(metadata.get("info", {}).get("name") == name
                      and metadata["info"].get("version") == version
                      and row["vulnerabilities"] == metadata.get("vulnerabilities", []),
                      "H2 官方元数据版本不符")
            expected_filename = f"{name}-{version}-py3-none-any.whl"
            releases = [item for item in metadata.get("urls", [])
                        if item.get("filename") == expected_filename]
            c.require(len(releases) == 1, "H2 官方发行物不存在或不唯一")
            release = releases[0]
            c.require(release.get("digests", {}).get("sha256") == wheel_sha
                      and row["wheel"]["sha256"] == wheel_sha
                      and row["wheel"]["bytes"] == release["size"]
                      and row["wheel_url"] == release["url"]
                      and row["wheel_url"].startswith("https://files.pythonhosted.org/packages/")
                      and release.get("yanked") is False, "H2 wheel 不符官方冻结摘要")
            wheel_path = c.check_binding(self.root, row["wheel"])
            package_root = c.managed_path(self.root, row["root"])
            _private_directory(package_root)
            with zipfile.ZipFile(io.BytesIO(c.read_bytes(wheel_path))) as archive:
                members = archive.infolist()
                c.require(len({item.filename for item in members}) == len(members),
                          "H2 wheel 含重复路径")
                expected = []
                for member in members:
                    c.require(not member.is_dir()
                              and stat.S_IFMT(member.external_attr >> 16) != stat.S_IFLNK,
                              "H2 wheel 含目录或符号链接项")
                    path = c.managed_path(package_root, member.filename)
                    raw = archive.read(member)
                    reference = {"path": path.relative_to(self.root).as_posix(),
                                 "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                    c.check_binding(self.root, reference)
                    expected.append(reference)
            expected.sort(key=lambda item: item["path"])
            c.require(row["files"] == expected, "H2 manifest 未闭合官方 wheel 全包文件")
            actual = set()
            for path in package_root.rglob("*"):
                c.plain_path(path)
                if path.is_dir():
                    _private_directory(path)
                else:
                    actual.add(path.relative_to(self.root).as_posix())
            c.require(actual == {item["path"] for item in expected}, "H2 包目录含额外或缺失文件")
            for item in expected:
                info = c.managed_path(self.root, item["path"]).stat()
                c.require(stat.S_IMODE(info.st_mode) == 0o600, "H2 包文件不是 0600")
            metadata_file = package_root / f"{name}-{version}.dist-info/METADATA"
            fields = BytesParser().parsebytes(c.read_bytes(metadata_file))
            c.require(fields["Name"] == name and fields["Version"] == version
                      and (fields.get_all("Requires-Dist") or []) == row["requires_dist"],
                      "H2 实际 distribution 元数据漂移")
            c.require(name != "hpack" or row["requires_dist"] == [], "生产 hpack 引入未冻结依赖")
            licenses = [item for item in expected
                        if item["path"].endswith(("/LICENSE", "/LICENSE.txt"))]
            c.require(bool(licenses) and row["licenses"] == licenses
                      and all(b"MIT" in c.read_bytes(c.managed_path(self.root, item["path"]))
                              for item in licenses), "H2 许可证不符固定发行物")
            reads.extend([row["metadata"], row["wheel"], *expected])
        self.read_set = sorted(reads, key=lambda item: item["path"])
        self._verify_loaded_origins()

    def _verify_loaded_origins(self) -> None:
        if self.artifact_only:
            return
        for fullname, module in tuple(sys.modules.items()):
            name = fullname.split(".", 1)[0]
            if name not in self.names:
                continue
            c.require(isinstance(module, ModuleType), "H2 已加载模块不是正常模块")
            loader = getattr(module, "__loader__", None)
            c.require(isinstance(loader, _FrozenLoader), "H2 检出系统包或未受管已加载模块")
            row = self.packages[name]
            expected_root = c.managed_path(self.root, row["root"]) / name
            path = loader.path
            c.require(loader.bundle.root == self.root
                      and loader.bundle.packages[name]["files"] == row["files"]
                      and path.is_relative_to(expected_root)
                      and getattr(module, "__file__", None) == str(path)
                      and getattr(getattr(module, "__spec__", None), "origin", None) == str(path),
                      "H2 实际加载 origin 不在冻结包闭包")
            expected = next((item for item in row["files"]
                             if item["path"] == path.relative_to(self.root).as_posix()), None)
            c.require(expected is not None and hashlib.sha256(loader.raw).hexdigest()
                      == expected["sha256"], "H2 实际编译字节不符冻结闭包")
            if fullname == name:
                c.require(getattr(module, "__version__", None) == PINS[name][0],
                          "H2 实际加载版本不符固定版本")

    def load_module(self, fullname: str) -> ModuleType:
        c.require(not self.artifact_only, "仅校验原件的依赖对象禁止执行或导入")
        c.require(fullname.split(".", 1)[0] in self.names, "H2 未授权的依赖命名空间")
        with _IMPORT_LOCK:
            self.revalidate()
            if self._finder is None:
                self._finder = _FrozenFinder(self)
                sys.meta_path.insert(0, self._finder)
            module = importlib.import_module(fullname)
            self._verify_loaded_origins()
            return module

    def new_hpack_decoder(self, max_header_list_size: int):
        c.require(self.kind == "production_decoder", "测试 peer 合同不能充当生产解码合同")
        c.require(type(max_header_list_size) is int and max_header_list_size > 0,
                  "HPACK 解压头部上界非法")
        module = self.load_module("hpack")
        return module.Decoder(max_header_list_size=max_header_list_size)

    def receipt(self) -> dict[str, Any]:
        c.require(not self.artifact_only, "仅校验原件不能签实际加载 origin 收据")
        self.revalidate()
        return {"schema_version": "claude-upgrade-h2-dependency-read-set/v1",
                "source_scope": c.SCOPE, "formal_ready": False, "launch_allowed": False,
                "kind": self.kind, "manifest": dict(self.manifest_binding),
                "packages": [{"name": name, "version": row["version"],
                              "wheel_sha256": row["wheel"]["sha256"]}
                             for name, row in sorted(self.packages.items())],
                "read_set": list(self.read_set),
                "loaded_origins": [{"module": name, "origin": module.__file__}
                                   for name, module in sorted(sys.modules.items())
                                   if name.split(".", 1)[0] in self.names]}
