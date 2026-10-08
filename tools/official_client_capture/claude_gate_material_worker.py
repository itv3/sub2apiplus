#!/usr/bin/env python3
"""在新私有进程中真实重放封存门禁，同时约束路径、描述符和元数据访问。

这里不改原 reader，不把材料里的路径当成读取权限。父进程只传已经取得
的精确能力清单。读阶段的所有拒绝留下不可复位标记，即使原 reader 捕获
异常也不能返回成功。该合同只执行已绑定的受管 Python 代码；不是任意
原生程序的操作系统沙箱。运行库和方法范围变化必须重新冻结读取合同。
"""

from __future__ import annotations

import sys

# 独立 worker 始终由源码启动，受管模块不消费树外遗留的 __pycache__。
# 这里只影响脚本子进程；将本模块导入测试或守卫不会改父进程设置。
if __name__ == "__main__":
    sys.pycache_prefix = "/__claude_gate_material_reader_no_bytecode__"

import argparse
import base64
import builtins
import hashlib
import importlib.util
import io
import _io
import json
import os
import posix
import stat
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


CONTRACT_PATH = Path(__file__).with_name("claude_gate_material_reader_contract.json")
IDENTITY_SCHEMA = "claude-gate-material-reader-identity/v1"
RUNTIME_SCHEMA = "claude-gate-material-python-runtime/v1"
_IDENTITY_TOKEN = object()
_ORIGINAL = {name: getattr(os, name) for name in ("open", "stat", "lstat", "access", "readlink", "scandir", "listdir", "fstat", "read", "lseek", "dup", "close")}
_OPEN = builtins.open
_FDOPEN = os.fdopen


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class ReaderError(RuntimeError):
    """来源、读取范围或实际重放失败。"""


@dataclass(frozen=True)
class VerifiedReaderIdentity:
    document: Mapping[str, Any]
    repository_root: Path
    token: object


def _plain_file(path: Path) -> tuple[bytes, os.stat_result]:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ReaderError("读取身份文件必须是规范绝对路径")
    for member in (path, *path.parents):
        if stat.S_ISLNK(_ORIGINAL["lstat"](member).st_mode):
            raise ReaderError("读取身份或材料路径包含符号链接")
    descriptor = _ORIGINAL["open"](path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = _ORIGINAL["fstat"](descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReaderError("读取身份或材料不是普通文件")
        chunks = []
        while True:
            data = _ORIGINAL["read"](descriptor, 1024 * 1024)
            if not data:
                break
            chunks.append(data)
        after = _ORIGINAL["fstat"](descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ReaderError("文件在实读期间漂移")
        return b"".join(chunks), before
    finally:
        _ORIGINAL["close"](descriptor)


def _file_identity(path: Path) -> dict[str, Any]:
    raw, metadata = _plain_file(path)
    return {"absolute_path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "mode": f"{stat.S_IMODE(metadata.st_mode):04o}"}


def _preload(repository: Path) -> Any:
    """固定启动序列；证据不能指定模块或代码路径。"""
    sys.path.insert(0, str(repository))
    from tools.official_client_capture import codex_upgrade_gate_receipt as gate
    # 五份固定实现都由原 reader 核钉住摘要；提前载入其纯历史依赖，避免
    # 读阶段从 HOME、site、证据目录或未知运行库按需加载。
    target = gate._target_gate_verifier()
    records = target.fs.records_module()
    records._audit_module()._runtime_module()
    # 身份校验也有固定的纯依赖；bootstrap 与真实读取须预载同一序列。
    # 不过滤运行库增量，随后仍逐文件严格比较完整实际清单。
    from tools.official_client_capture import claude_tool_identity_policy
    from tools.official_client_control import contracts
    import codecs
    import encodings.utf_8
    import encodings.ascii
    import encodings.latin_1
    import datetime
    import errno
    import locale
    datetime.datetime.strptime("2026-10-08T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ")
    return gate


def _runtime_manifest(repository: Path) -> dict[str, Any]:
    if sys.pycache_prefix and Path(sys.pycache_prefix).exists():
        raise ReaderError("独立源码读取器的禁用 bytecode 前缀必须不存在")
    _preload(repository)
    paths = {Path(sys.executable).resolve()}
    for module in tuple(sys.modules.values()):
        location = getattr(module, "__file__", None)
        if location and not str(location).startswith("<"):
            path = Path(location).resolve()
            if not path.is_relative_to(repository):
                paths.add(path)
        # 解释器早期启动的 encoding 等模块可能已消费现存字节码；源码和
        # 实际 cache 都绑定，不能把执行过的 .pyc 替换成 .py 摘要。
        cached = getattr(module, "__cached__", None)
        if cached and Path(cached).is_file():
            paths.add(Path(cached).resolve())
    library = Path(sysconfig.get_config_var("LIBDIR") or "/nonexistent") / (sysconfig.get_config_var("LDLIBRARY") or "nonexistent")
    if library.is_file():
        paths.add(library.resolve())
    # Linux 的实际映射表用于精确绑定已加载共享库，不能用整棵 lib 根授权。
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/maps").read_text().splitlines():
            location = line.split()[-1]
            if location.startswith("/") and Path(location).is_file():
                paths.add(Path(location).resolve())
    value = {"schema_version": RUNTIME_SCHEMA, "python_version": sys.version,
             "executable": str(Path(sys.executable).resolve()), "files": [_file_identity(path) for path in sorted(paths)],
             "bytecode_prefix": sys.pycache_prefix,
             "native_libraries_certified": sys.platform.startswith("linux")}
    value["manifest_sha256"] = _digest(value)
    return value


def reader_bytecode_probe(document: Mapping[str, Any], absolute_path: str) -> str:
    """源码 loader 的固定负向探测；不读取现存未知 bytecode。"""
    original = Path(importlib.util.cache_from_source(absolute_path))
    prefix = document["runtime_manifest"].get("bytecode_prefix")
    if prefix is None:
        return str(original)
    return str(Path(prefix) / Path(absolute_path).parent.relative_to("/") / original.name)


def compute_gate_reader_identity(repository_root: Path, *, tool_identity: Mapping[str, Any] | None = None,
                                 formed_context: Any, runtime_manifest: Mapping[str, Any] | None = None) -> VerifiedReaderIdentity:
    """绑定真实代码和固定解释器；正式来源另外核有效七项身份及安装凭证。"""
    repository = Path(repository_root).resolve(strict=True)
    from tools.official_client_capture import claude_tool_identity_policy as policy_module
    from tools.official_client_control.contracts import TOOL_IDENTITY_FIELDS
    policy = policy_module.load_policy(repository / "tools/official_client_capture" / policy_module.POLICY_FILENAME)
    memo = os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
    try:
        measured = policy_module.compute_identity(policy, repository)
    finally:
        if memo is not None:
            os.environ["CODEX_UPGRADE_IDENTITY_MEMO"] = memo
    actual_tool_identity = {key: measured[key] for key in TOOL_IDENTITY_FIELDS}
    if tool_identity is not None and dict(tool_identity) != actual_tool_identity:
        raise ReaderError("读取器实际双受管根身份与有效工具身份不同")
    worker_path = repository / "tools/official_client_capture/claude_gate_material_worker.py"
    contract_path = worker_path.with_name(CONTRACT_PATH.name)
    command = [str(Path(sys.executable).resolve()), "-I", "-B", "-S", str(worker_path), "--runtime-manifest", "--repository", str(repository)]
    outcome = subprocess.run(command, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=60, check=False)
    if outcome.returncode != 0:
        raise ReaderError("读取器固定运行库准备失败：" + outcome.stderr.decode("utf-8", "replace")[-2000:])
    actual_runtime = json.loads(outcome.stdout)
    if runtime_manifest is not None and dict(runtime_manifest) != actual_runtime:
        raise ReaderError("读取器实际 Python 运行库与安装准备清单不同")
    files = []
    for root_name in ("tools/official_client_capture", "tools/official_client_control"):
        for path in sorted((repository / root_name).rglob("*.py")):
            if "tests" not in path.relative_to(repository).parts and "__pycache__" not in path.parts:
                files.append(_file_identity(path))
    from tools.official_client_capture import codex_upgrade_gate_receipt as gate
    target_roots = (repository / "tools/ci", Path("/root/arm64-capture-driver/driver"))
    target_root = next((root for root in target_roots if all((root / name).is_file() and _file_identity(root / name)["sha256"] == digest for name, digest in gate.TARGET_VERIFIER_SHA256S.items())), None)
    if target_root is None:
        raise ReaderError("读取身份缺固定目录的五文件钉住实现")
    target_files = [_file_identity(target_root / name) for name in sorted(gate.TARGET_VERIFIER_SHA256S)]
    if any(row["sha256"] != gate.TARGET_VERIFIER_SHA256S[Path(row["absolute_path"]).name] for row in target_files):
        raise ReaderError("原目标验证器五文件与固定摘要不同")
    contract = _file_identity(contract_path)
    document = {"schema_version": IDENTITY_SCHEMA, "tool_identity": actual_tool_identity, "formed_context": formed_context,
                "contract_binding": contract, "contract_sha256": _digest(json.loads(_plain_file(contract_path)[0])),
                "code_bindings": files, "target_bindings": target_files, "runtime_manifest": actual_runtime,
                "io_version": "managed-path-fd-metadata/v1"}
    document["identity_sha256"] = _digest(document)
    return VerifiedReaderIdentity(document, repository, _IDENTITY_TOKEN)


def verify_reader_identity(document: Mapping[str, Any]) -> None:
    """只重验已受管冻结的精确清单；调用者先证明这份身份的来源。"""
    fields = {"schema_version", "tool_identity", "formed_context", "contract_binding", "contract_sha256", "code_bindings", "target_bindings", "runtime_manifest", "io_version", "identity_sha256"}
    if set(document) != fields or document["schema_version"] != IDENTITY_SCHEMA or document["io_version"] != "managed-path-fd-metadata/v1":
        raise ReaderError("读取身份合同不闭合")
    if document["identity_sha256"] != _digest({key: value for key, value in document.items() if key != "identity_sha256"}):
        raise ReaderError("读取身份内容摘要漂移")
    runtime = document["runtime_manifest"]
    if runtime.get("schema_version") != RUNTIME_SCHEMA or runtime.get("executable") != str(Path(sys.executable).resolve()) or runtime.get("python_version") != sys.version:
        raise ReaderError("读取身份不是实际固定 Python")
    if runtime.get("manifest_sha256") != _digest({key: value for key, value in runtime.items() if key != "manifest_sha256"}):
        raise ReaderError("运行库清单摘要漂移")
    for row in [document["contract_binding"], *document["code_bindings"], *document["target_bindings"], *runtime["files"]]:
        if set(row) != {"absolute_path", "sha256", "bytes", "mode"} or _file_identity(Path(row["absolute_path"])) != row:
            raise ReaderError("读取器代码或运行库实际字节／模式漂移")
    contract_path = Path(document["contract_binding"]["absolute_path"])
    repository = contract_path.parents[2]
    if contract_path != repository / "tools/official_client_capture" / CONTRACT_PATH.name or document["contract_sha256"] != _digest(json.loads(_plain_file(contract_path)[0])):
        raise ReaderError("读取合同不是当前受管合同原件")
    expected_code = sorted(str(path) for name in ("tools/official_client_capture", "tools/official_client_control")
        for path in (repository / name).rglob("*.py") if "tests" not in path.relative_to(repository).parts and "__pycache__" not in path.parts)
    if sorted(row["absolute_path"] for row in document["code_bindings"]) != expected_code:
        raise ReaderError("读取代码精确清单不闭合")
    from tools.official_client_capture import claude_tool_identity_policy as policy_module
    from tools.official_client_control.contracts import TOOL_IDENTITY_FIELDS
    policy = policy_module.load_policy(repository / "tools/official_client_capture" / policy_module.POLICY_FILENAME)
    memo = os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
    try:
        measured = policy_module.compute_identity(policy, repository)
    finally:
        if memo is not None:
            os.environ["CODEX_UPGRADE_IDENTITY_MEMO"] = memo
    if document["tool_identity"] != {key: measured[key] for key in TOOL_IDENTITY_FIELDS}:
        raise ReaderError("读取身份的七项工具身份不是实际双受管根字节")


class ManagedReadStream:
    """使用真实只读流；不能把缓存字节配上虚假的 fstat。"""

    def __init__(self, view: "GateMaterialReadView", handle: Any, descriptor: int, logical: str):
        self._view, self._handle, self._descriptor = view, handle, descriptor
        self.name = logical

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        self.close()

    def read(self, *arguments):
        self._view.consume(self._descriptor)
        return self._handle.read(*arguments)

    def readline(self, *arguments):
        self._view.consume(self._descriptor)
        return self._handle.readline(*arguments)

    def readlines(self, *arguments):
        self._view.consume(self._descriptor)
        return self._handle.readlines(*arguments)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def fileno(self):
        self._view.known_fd(self._descriptor)
        return self._descriptor

    def seek(self, *arguments):
        self._view.known_fd(self._descriptor)
        return self._handle.seek(*arguments)

    def tell(self):
        self._view.known_fd(self._descriptor)
        return self._handle.tell()

    def close(self):
        if self._descriptor in self._view.descriptors:
            self._view.descriptors.pop(self._descriptor)
            self._handle.close()

    @property
    def closed(self):
        return self._handle.closed

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True


class ManagedDirectoryEntry:
    def __init__(self, view: "GateMaterialReadView", logical: str):
        self._view = view
        self.path = logical
        self.name = logical.rsplit("/", 1)[-1]

    def stat(self, *, follow_symlinks=True):
        return self._view.stat(self.path, follow_symlinks=follow_symlinks)

    def is_dir(self, *, follow_symlinks=True):
        return stat.S_ISDIR(self.stat(follow_symlinks=follow_symlinks).st_mode)

    def is_file(self, *, follow_symlinks=True):
        return stat.S_ISREG(self.stat(follow_symlinks=follow_symlinks).st_mode)

    def is_symlink(self):
        self.stat(follow_symlinks=False)
        return False

    def __fspath__(self):
        return self.path


class ManagedDirectoryIterator:
    def __init__(self, members):
        self._members = iter(members)

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._members)

    def close(self):
        return None


class GateMaterialReadView:
    """逻辑旧坐标到唯一实际文件的精确映射；未登记路径不是普通 missing。"""

    def __init__(self, files: list[dict[str, Any]], directories: list[dict[str, Any]],
                 missing: list[str], code: list[dict[str, Any]]):
        self.files = {row["logical_path"]: row for row in files}
        self.directories = {row["logical_path"]: row for row in directories}
        self.missing = frozenset(missing)
        self.code = {row["absolute_path"]: row for row in code}
        self.descriptors: dict[int, str] = {}
        self.readset: set[str] = set()
        self.metadata_files: set[str] = set()
        self.accesses: dict[str, dict[str, Any]] = {}
        self.violations: list[str] = []
        self.ticket = None
        self.handles: set[int] = set()
        # namedtuple 等标准库在预载入时生成的 code object 以对象身份准入，
        # 不允许任意新代码使用同一个 <string> filename 冒充。
        generated = set()
        for module in tuple(sys.modules.values()):
            for member in tuple(vars(module).values()) if module is not None else ():
                values = tuple(vars(member).values()) if isinstance(member, type) else (member,)
                for function in values:
                    if isinstance(function, (staticmethod, classmethod)):
                        function = function.__func__
                    candidate = getattr(function, "__code__", None)
                    if candidate is not None and candidate.co_filename == "<string>":
                        generated.add(candidate)
        self.generated_codes = frozenset(generated)

    def deny(self, reason: str):
        self.violations.append(reason)
        raise ReaderError(reason)

    def logical(self, value, *, dir_fd=None):
        if dir_fd is not None or isinstance(value, int):
            return self.deny("路径操作不接受描述符或 dir_fd")
        result = os.fspath(value)
        if not isinstance(result, str) or not result.startswith("/") or ".." in result.split("/"):
            return self.deny("读取器路径不是规范绝对坐标")
        result = os.path.normpath(result)
        if result not in self.files and result not in self.directories and result not in self.missing and result not in self.code:
            return self.deny("未授权读取路径：" + result)
        return result

    def physical(self, logical):
        if logical in self.files:
            return self.files[logical]["physical_path"]
        if logical in self.directories:
            return self.directories[logical]["physical_path"]
        if logical in self.code:
            return logical
        raise FileNotFoundError(logical)

    def internal(self, name, *arguments, **keywords):
        previous = self.ticket
        self.ticket = (name, arguments)
        try:
            return _ORIGINAL[name](*arguments, **keywords)
        finally:
            self.ticket = previous

    def _record(self, operation, logical, metadata=None, *, members=None):
        if logical in self.code:
            return
        row = {"operation": operation, "logical_path": logical, "type": "missing"}
        if metadata is not None:
            row.update(type="directory" if stat.S_ISDIR(metadata.st_mode) else "regular", mode=f"{stat.S_IMODE(metadata.st_mode):04o}", size=metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0)
        if members is not None:
            row["members"] = members
        self.accesses[_digest(row)] = row

    def stat(self, value, *, dir_fd=None, follow_symlinks=True):
        logical = self.logical(value, dir_fd=dir_fd)
        operation = "stat" if follow_symlinks else "lstat"
        try:
            physical = self.physical(logical)
            for ancestor in Path(physical).parents:
                if stat.S_ISLNK(self.internal("lstat", str(ancestor)).st_mode):
                    return self.deny("读取实际路径的祖先含符号链接")
            actual = self.internal("lstat", physical)
        except FileNotFoundError:
            self._record(operation, logical)
            raise
        if stat.S_ISLNK(actual.st_mode):
            return self.deny("材料或代码元数据路径含符号链接")
        self._record(operation, logical, actual)
        if logical in self.files:
            self.metadata_files.add(logical)
            self._validate_file(logical, actual)
        return actual

    def _validate_file(self, logical, metadata):
        row = self.files.get(logical) or self.code[logical]
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != row["bytes"] or f"{stat.S_IMODE(metadata.st_mode):04o}" != row["mode"]:
            self.deny("材料实际类型、长度或模式漂移：" + logical)

    def access(self, value, mode, *, dir_fd=None, effective_ids=False, follow_symlinks=True):
        if mode != os.F_OK:
            return self.deny("读取合同不接受权限探测")
        try:
            metadata = self.stat(value, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        except FileNotFoundError:
            self._record("access", self.logical(value))
            return False
        self._record("access", self.logical(value), metadata)
        return True

    def readlink(self, value, *, dir_fd=None):
        logical = self.logical(value, dir_fd=dir_fd)
        self.stat(logical, follow_symlinks=False)
        raise OSError(22, "已验证路径不是符号链接", logical)

    def members(self, value, operation):
        logical = self.logical(value)
        metadata = self.stat(logical)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(logical)
        prefix = logical.rstrip("/") + "/"
        names = sorted({path[len(prefix):].split("/", 1)[0] for path in (*self.files, *self.directories) if path.startswith(prefix)})
        self._record(operation, logical, metadata, members=names)
        return [prefix + name for name in names]

    def listdir(self, value):
        return [path.rsplit("/", 1)[-1] for path in self.members(value, "listdir")]

    def scandir(self, value):
        return ManagedDirectoryIterator([ManagedDirectoryEntry(self, path) for path in self.members(value, "scandir")])

    def open_fd(self, value, flags, mode=0o777, *, dir_fd=None):
        permitted = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        if flags & ~permitted or (flags & os.O_ACCMODE) != os.O_RDONLY:
            return self.deny("读取合同拒绝写 flags 或未知 flags")
        logical = self.logical(value, dir_fd=dir_fd)
        metadata = self.stat(logical)
        self._validate_file(logical, metadata)
        physical = self.physical(logical)
        descriptor = self.internal("open", physical, flags | getattr(os, "O_NOFOLLOW", 0))
        self.descriptors[descriptor] = logical
        before = self.internal("fstat", descriptor)
        self._validate_file(logical, before)
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            return self.deny("实际打开对象与路径元数据不是同一个文件")
        chunks = []
        while True:
            data = self.internal("read", descriptor, 1024 * 1024)
            if not data:
                break
            chunks.append(data)
        after = self.internal("fstat", descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return self.deny("材料在真实描述符实读期间漂移")
        if hashlib.sha256(b"".join(chunks)).hexdigest() != (self.files.get(logical) or self.code[logical])["sha256"]:
            return self.deny("材料实际完整字节摘要漂移：" + logical)
        self.internal("lseek", descriptor, 0, os.SEEK_SET)
        self._record("open", logical, before)
        return descriptor

    def known_fd(self, descriptor):
        if type(descriptor) is not int or descriptor not in self.descriptors:
            return self.deny("未登记只读文件描述符")
        return self.descriptors[descriptor]

    def consume(self, descriptor):
        logical = self.known_fd(descriptor)
        if logical in self.files:
            self.readset.add(logical)

    def fdopen(self, descriptor, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True):
        logical = self.known_fd(descriptor)
        if mode not in {"r", "rt", "rb"} or not closefd:
            return self.deny("描述符流只支持登记的读模式与关闭所有权")
        previous = self.ticket
        self.ticket = ("fdopen", descriptor)
        try:
            handle = _OPEN(descriptor, mode, buffering, encoding, errors, newline, closefd)
        finally:
            self.ticket = previous
        self.handles.add(id(handle))
        return ManagedReadStream(self, handle, descriptor, logical)

    def open(self, value, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        if mode not in {"r", "rt", "rb"} or opener is not None or not closefd:
            return self.deny("文件流拒绝写入、自定义 opener 或匿名描述符")
        descriptor = self.open_fd(value, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        return self.fdopen(descriptor, mode, buffering, encoding, errors, newline)

    def fileio(self, value, mode="r", closefd=True, opener=None):
        return self.open(value, "rb" if mode in {"r", "rb"} else mode, buffering=0, closefd=closefd, opener=opener)

    def fstat(self, descriptor):
        logical = self.known_fd(descriptor)
        metadata = self.internal("fstat", descriptor)
        self._validate_file(logical, metadata)
        self._record("fstat", logical, metadata)
        return metadata

    def read_fd(self, descriptor, count):
        self.consume(descriptor)
        return self.internal("read", descriptor, count)

    def seek_fd(self, descriptor, offset, whence):
        self.known_fd(descriptor)
        return self.internal("lseek", descriptor, offset, whence)

    def dup_fd(self, descriptor):
        logical = self.known_fd(descriptor)
        copied = self.internal("dup", descriptor)
        self.descriptors[copied] = logical
        return copied

    def close_fd(self, descriptor):
        self.known_fd(descriptor)
        self.descriptors.pop(descriptor)
        return self.internal("close", descriptor)

    def audit(self, event, arguments):
        if event == "open":
            if self.ticket is None or self.ticket[0] not in {"open", "fdopen"}:
                self.deny("audit 拒绝未授权实际打开")
            if self.ticket[0] == "open":
                expected_path = self.ticket[1][0]
                if os.fspath(arguments[0]) != os.fspath(expected_path):
                    self.deny("audit 实际打开路径与一次性 ticket 不同")
            elif arguments[0] != self.ticket[1]:
                self.deny("audit 实际 fdopen 与登记描述符不同")
            flags = arguments[2]
            forbidden = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if isinstance(flags, int) and flags & forbidden:
                self.deny("audit 实际打开带写 flags")
        elif event in {"os.listdir", "os.scandir"}:
            if self.ticket is None:
                self.deny("audit 拒绝直接目录枚举")
        elif event == "import":
            name = arguments[0]
            if name not in sys.modules:
                self.deny("读阶段拒绝未预载入模块：" + name)
        elif event == "compile":
            raw, filename = arguments
            if filename not in self.code or not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != self.code[filename]["sha256"]:
                self.deny("读阶段拒绝未绑定源字节的编译")
        elif event == "exec":
            if arguments[0].co_filename not in self.code:
                self.deny("读阶段拒绝未绑定 code object 的执行")
        elif event.startswith(("subprocess.", "socket.", "ctypes.", "mmap.", "os.exec", "os.spawn", "os.fork", "os.system", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.chown", "os.link", "os.symlink", "os.truncate")):
            self.deny("audit 拒绝副作用：" + event)

    def profile(self, frame, event, argument):
        if event == "call":
            location = frame.f_code.co_filename
            if not location.startswith("<frozen ") and location not in self.code and frame.f_code not in self.generated_codes:
                self.deny("读阶段拒绝未绑定 Python 代码：" + location)
        elif event == "c_call":
            # ticket 只准入它选择的原生函数，不能让同一动态范围内任何 C
            # callable 都绕过检查。实际 open 事件另核精确坐标及只读 flags。
            if self.ticket is not None and ((self.ticket[0] in _ORIGINAL and argument is _ORIGINAL[self.ticket[0]]) or (self.ticket[0] == "fdopen" and argument is _OPEN)):
                return
            module = getattr(argument, "__module__", "") or ""
            name = getattr(argument, "__name__", "")
            owner = getattr(argument, "__self__", None)
            # 纯数据 callable 的有限准入。系统 IO 必须经上面的精确钩子；
            # _io 流只准入本 worker 登记的真实只读对象和无副作用方法。
            pure_builtin = {"len", "isinstance", "issubclass", "getattr", "setattr", "hasattr", "delattr", "all", "any", "sum", "sorted", "min", "max", "abs", "round", "repr", "ascii", "iter", "next", "hash", "id", "ord", "chr", "divmod", "callable", "vars", "locals", "globals", "__import__", "hex", "oct", "bin", "pow", "format"}
            pure_modules = {"_hashlib", "_sha256", "_sha512", "_blake2", "_json", "_codecs", "unicodedata", "math", "_sre", "_string", "binascii", "_stat", "_abc"}
            pure_types = (str, bytes, bytearray, dict, list, tuple, set, frozenset, int, float, range)
            if module == "sys" and name == "setprofile" and frame.f_code.co_filename == __file__:
                return
            if argument in {object.__new__, type.__new__}:
                return
            if module == "posix" and name in {"fspath", "_path_splitroot_ex", "_path_splitroot", "_path_normpath"}:
                return
            if module == "sys" and name == "intern":
                return
            if module == "_imp" and name in {"extension_suffixes", "source_hash"}:
                return
            if module == "builtins" and name in {"compile", "exec"} and (frame.f_code.co_filename, frame.f_code.co_name) in {("<frozen importlib._bootstrap_external>", "source_to_code"), ("<frozen importlib._bootstrap_external>", "exec_module"), ("<frozen importlib._bootstrap>", "_call_with_frames_removed")}:
                return
            if getattr(owner, "__module__", "") == "datetime" and name in {"fromisoformat", "fromtimestamp", "utcfromtimestamp", "combine", "strptime"}:
                return
            if module == "_locale" and name in {"setlocale", "nl_langinfo"}:
                return
            if module == "time" and name in {"strftime", "strptime"}:
                return
            if type(owner).__module__ == "_thread" and name in {"__enter__", "__exit__", "acquire", "release", "locked"}:
                return
            if module == "_io" and name == "text_encoding":
                return
            if isinstance(owner, type) and owner in pure_types and name in {"from_bytes", "fromhex", "maketrans", "fromkeys", "__new__"}:
                return
            if module == "builtins" and name in pure_builtin:
                return
            if module in pure_modules:
                return
            if isinstance(owner, pure_types) or type(owner).__module__ in {"re", "_sre", "datetime", "_hashlib", "_sha256", "_blake2"}:
                return
            if id(owner) in self.handles and name in {"read", "readline", "readlines", "fileno", "close", "seek", "tell", "readable", "writable", "seekable"}:
                return
            if module in {"_io", "io"} and name in {"TextIOWrapper", "BufferedReader"} and frame.f_code.co_name == "fdopen" and frame.f_code.co_filename == os.__file__:
                return
            self.deny("读阶段拒绝未知 C callable：" + module + "." + name + "；调用点 " + frame.f_code.co_filename + ":" + frame.f_code.co_name)

    def install(self):
        builtins.open = io.open = _io.open = self.open
        io.FileIO = _io.FileIO = self.fileio
        _io.open_code = lambda value: self.open(value, "rb")
        hooks = {"open": self.open_fd, "stat": self.stat,
                 "lstat": lambda value, **options: self.stat(value, follow_symlinks=False, **options),
                 "access": self.access, "readlink": self.readlink, "scandir": self.scandir, "listdir": self.listdir,
                 "fstat": self.fstat, "read": self.read_fd, "lseek": self.seek_fd, "dup": self.dup_fd, "close": self.close_fd,
                 "pread": lambda *_arguments, **_keywords: self.deny("读取合同拒绝 pread"),
                 "dup2": lambda *_arguments, **_keywords: self.deny("读取合同拒绝 dup2")}
        for module in (os, posix):
            for name, function in hooks.items():
                if hasattr(module, name):
                    setattr(module, name, function)
        os.fdopen = self.fdopen
        sys.addaudithook(self.audit)
        sys.setprofile(self.profile)


def _worker(specification: Mapping[str, Any], repository: Path) -> dict[str, Any]:
    gate = _preload(repository)
    identity = specification["reader_identity"]
    verify_reader_identity(identity)
    if _runtime_manifest(repository) != identity["runtime_manifest"]:
        raise ReaderError("私有进程实际预载运行库与固定清单不同")
    runtime = identity["runtime_manifest"]
    code = [*identity["code_bindings"], *identity["target_bindings"], *runtime["files"]]
    # 当前 worker 经脚本启动时 __main__ 的 code filename 也是实际绑定文件。
    view = GateMaterialReadView(specification["files"], specification["directories"], specification["missing"], code)
    sys.stdin.close()
    view.install()
    try:
        result = gate.replay(Path(specification["gate_root"]), specification["receipt_relative"])
        if result.get("phase") != "candidate_external" or result.get("status") != "passed" or result.get("subject") != specification["expected_subject"]:
            raise ReaderError("门禁原 reader 的阶段、结果或完整 subject 不一致")
    finally:
        sys.setprofile(None)
    if view.violations:
        raise ReaderError("读取越界不能被原 reader 吞掉：" + view.violations[0])
    accesses = sorted(view.accesses.values(), key=lambda row: (row["logical_path"], row["operation"]))
    # 收口后仍实读同一映射，核对所有文件没有在 replay 期间漂移。
    for row in specification["files"]:
        view.ticket = ("open", (row["physical_path"],))
        try:
            raw, metadata = _plain_file(Path(row["physical_path"]))
        finally:
            view.ticket = None
        if hashlib.sha256(raw).hexdigest() != row["sha256"] or len(raw) != row["bytes"] or f"{stat.S_IMODE(metadata.st_mode):04o}" != row["mode"]:
            raise ReaderError("replay 后材料字节／模式漂移")
    return {"result": result, "reader_readset": sorted(view.readset), "metadata_files": sorted(view.metadata_files),
            "reader_accesses": accesses, "reader_accesses_sha256": _digest(accesses), "violations": []}


def run_gate_material_reader(reader_identity: VerifiedReaderIdentity, files: list[dict[str, Any]],
                             directories: list[dict[str, Any]], missing: list[str], gate_root: str,
                             receipt_relative: str, *, view: str, expected_subject: Mapping[str, Any]) -> dict[str, Any]:
    """每次新进程、新 module cache；父进程不安装任何路径钩子。"""
    if not isinstance(reader_identity, VerifiedReaderIdentity) or reader_identity.token is not _IDENTITY_TOKEN or view not in {"original", "imported"}:
        raise ReaderError("读取器须由受管身份与固定视图启动")
    verify_reader_identity(reader_identity.document)
    specification = {"reader_identity": dict(reader_identity.document), "files": files, "directories": directories,
                     "missing": missing, "gate_root": gate_root, "receipt_relative": receipt_relative,
                     "expected_subject": dict(expected_subject)}
    command = [reader_identity.document["runtime_manifest"]["executable"], "-I", "-B", "-S", str(reader_identity.repository_root / "tools/official_client_capture/claude_gate_material_worker.py"), "--repository", str(reader_identity.repository_root)]
    result = subprocess.run(command, input=_canonical(specification), env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=60, check=False)
    if result.returncode != 0:
        raise ReaderError("独立原 reader 拒绝材料：" + result.stderr.decode("utf-8", "replace")[-4000:])
    if len(result.stdout) > 16 * 1024 * 1024:
        raise ReaderError("读取器结果超过固定上限")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--runtime-manifest", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.runtime_manifest:
            result = _runtime_manifest(arguments.repository.resolve(strict=True))
        else:
            raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ReaderError("读取器输入超过固定上限")
            result = _worker(json.loads(raw), arguments.repository.resolve(strict=True))
        sys.stdout.buffer.write(_canonical(result))
        return 0
    except Exception as error:
        sys.setprofile(None)
        sys.stderr.write(str(error) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
