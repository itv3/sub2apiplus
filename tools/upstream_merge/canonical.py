"""严格 JSON、内容摘要和不可覆盖文件操作。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import UpstreamMergeError


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
MAX_JSON_BYTES = 32 * 1024 * 1024


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复键，避免同一文档存在两种解释。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpstreamMergeError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def canonical_bytes(value: Any) -> bytes:
    """返回稳定、紧凑且带结尾换行的 UTF-8 JSON。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def pretty_bytes(value: Any) -> bytes:
    """返回便于人工审阅且字节稳定的 UTF-8 JSON。"""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n"
    )


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"摘要目标不是可信普通文件：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_binding(path: Path) -> dict[str, Any]:
    """生成绝对路径文件绑定；外部事实移动后必须显式重新建计划。"""

    if not path.is_absolute():
        raise UpstreamMergeError(f"绑定路径必须是绝对路径：{path}")
    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"绑定路径不是可信普通文件：{path}")
    resolved = path.resolve(strict=True)
    raw_size = resolved.stat().st_size
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "bytes": raw_size,
    }


def validate_file_binding(value: Any, label: str) -> dict[str, Any]:
    binding = expect_object(value, label)
    expect_exact_fields(binding, {"path", "sha256", "bytes"}, label)
    raw_path = expect_string(binding.get("path"), f"{label}.path")
    path = Path(raw_path)
    if not path.is_absolute():
        raise UpstreamMergeError(f"{label}.path 必须是绝对路径")
    digest = expect_sha256(binding.get("sha256"), f"{label}.sha256")
    size = binding.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise UpstreamMergeError(f"{label}.bytes 必须是非负整数")
    actual = file_binding(path)
    if actual["sha256"] != digest or actual["bytes"] != size:
        raise UpstreamMergeError(f"{label} 内容摘要或字节数漂移：{path}")
    return binding


def identity_sha256(document: dict[str, Any]) -> str:
    """复算排除自摘要字段后的文档身份。"""

    payload = {key: value for key, value in document.items() if key != "identity_sha256"}
    return sha256_bytes(canonical_bytes(payload))


def bind_identity(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    result["identity_sha256"] = identity_sha256(result)
    return result


def validate_identity(document: dict[str, Any], label: str) -> str:
    digest = expect_sha256(document.get("identity_sha256"), f"{label}.identity_sha256")
    if identity_sha256(document) != digest:
        raise UpstreamMergeError(f"{label} identity_sha256 漂移")
    return digest


def load_json(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"{label} 不是可信普通文件：{path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise UpstreamMergeError(f"{label} 大小非法：{size}")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UpstreamMergeError(f"无法读取 {label}：{error}") from error


def expect_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UpstreamMergeError(f"{label} 必须是对象")
    return value


def expect_exact_fields(value: dict[str, Any], fields: set[str], label: str) -> None:
    actual = set(value)
    if actual != fields:
        raise UpstreamMergeError(
            f"{label} 字段不闭合：缺失={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )


def expect_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UpstreamMergeError(f"{label} 必须是无首尾空白的非空字符串")
    return value


def expect_safe_id(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if not SAFE_ID_RE.fullmatch(text):
        raise UpstreamMergeError(f"{label} 不是安全标识")
    return text


def expect_sha256(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if not SHA256_RE.fullmatch(text):
        raise UpstreamMergeError(f"{label} 不是小写 SHA-256")
    return text


def expect_git_object(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if not GIT_OBJECT_RE.fullmatch(text):
        raise UpstreamMergeError(f"{label} 不是完整 Git 对象 ID")
    return text


def expect_sorted_unique_strings(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise UpstreamMergeError(f"{label} 必须是{'可空' if allow_empty else '非空'}字符串数组")
    result = [expect_string(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if result != sorted(set(result)):
        raise UpstreamMergeError(f"{label} 必须严格排序且不得重复")
    return result


def safe_relative_path(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if "\\" in text:
        raise UpstreamMergeError(f"{label} 必须使用 POSIX 分隔符")
    path = PurePosixPath(text)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise UpstreamMergeError(f"{label} 必须是根内规范相对路径")
    if path.as_posix() != text:
        raise UpstreamMergeError(f"{label} 不是规范 POSIX 相对路径")
    return text


def resolve_within(root: Path, relative: str, label: str) -> Path:
    safe_relative_path(relative, label)
    resolved_root = root.resolve()
    candidate = (resolved_root / PurePosixPath(relative)).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise UpstreamMergeError(f"{label} 越过受控根：{relative}")
    current = resolved_root
    for part in PurePosixPath(relative).parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise UpstreamMergeError(f"{label} 父目录包含符号链接：{current}")
    return candidate


def ensure_private_directory(path: Path, *, create: bool) -> Path:
    """验证或创建 0700 私有目录，不接受符号链接。"""

    if not path.is_absolute():
        raise UpstreamMergeError(f"私有目录必须是绝对路径：{path}")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise UpstreamMergeError(f"私有目录路径不可信：{path}")
    elif create:
        path.mkdir(parents=True, mode=0o700)
    else:
        raise UpstreamMergeError(f"私有目录不存在：{path}")
    resolved = path.resolve(strict=True)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode != 0o700:
        raise UpstreamMergeError(f"私有目录权限必须是 0700：{resolved} 当前={mode:04o}")
    return resolved


def write_once(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    """使用 O_EXCL 创建不可变文件，并拒绝不可信父目录。"""

    if path.exists() or path.is_symlink():
        raise UpstreamMergeError(f"禁止覆盖已存在的不可变文件：{path}")
    parent = path.parent
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise UpstreamMergeError(f"输出父目录不可信：{parent}")
    parent_existed = parent.exists()
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise UpstreamMergeError(f"输出父目录不可信：{parent}")
    if not parent_existed:
        parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as error:
        raise UpstreamMergeError(f"禁止覆盖已存在的不可变文件：{path}") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(mode)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_json_once(path: Path, value: Any) -> None:
    write_once(path, pretty_bytes(value))


def artifact_binding(root: Path, path: Path) -> dict[str, Any]:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise UpstreamMergeError(f"制品不在 evidence root 内：{resolved}")
    relative = resolved.relative_to(resolved_root).as_posix()
    return {
        "path": relative,
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def validate_artifact_binding(
    root: Path,
    value: Any,
    label: str,
) -> dict[str, Any]:
    binding = expect_object(value, label)
    expect_exact_fields(binding, {"path", "sha256", "bytes"}, label)
    relative = safe_relative_path(binding.get("path"), f"{label}.path")
    digest = expect_sha256(binding.get("sha256"), f"{label}.sha256")
    size = binding.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise UpstreamMergeError(f"{label}.bytes 必须是非负整数")
    path = resolve_within(root, relative, f"{label}.path")
    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"{label} 指向的制品不存在或不可信：{path}")
    if path.stat().st_size != size or sha256_file(path) != digest:
        raise UpstreamMergeError(f"{label} 制品内容漂移：{relative}")
    return binding


def validate_string_enum(value: Any, allowed: Iterable[str], label: str) -> str:
    text = expect_string(value, label)
    allowed_set = set(allowed)
    if text not in allowed_set:
        raise UpstreamMergeError(f"{label} 非法：{text}，允许={sorted(allowed_set)}")
    return text
