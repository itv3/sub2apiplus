"""严格 JSON、规范化摘要与安全的只写一次文件操作。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import ControlError


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
SPEC_ID_RE = re.compile(r"^SPEC-[A-Z0-9][A-Z0-9._-]{0,95}$")
PAIR_ID_RE = re.compile(r"^PAIR-(SPEC-[A-Z0-9][A-Z0-9._-]{0,95})$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlError(f"JSON 存在重复键：{key}")
        result[key] = value
    return result


def parse_json_bytes(content: bytes, label: str = "JSON") -> Any:
    """严格解析 UTF-8 JSON，并拒绝重复键。"""

    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError(f"{label} 无法解析：{error}") from error


def load_json_file(path: Path, label: str = "JSON") -> Any:
    """只读取可信普通文件。"""

    if path.is_symlink() or not path.is_file():
        raise ControlError(f"{label} 不是可信普通文件：{path}")
    return parse_json_bytes(path.read_bytes(), label)


def canonical_json_bytes(value: Any) -> bytes:
    """返回稳定、单行、带换行的 UTF-8 JSON。"""

    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        raise ControlError(f"值不能规范化为 JSON：{error}") from error


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ControlError(f"摘要目标不是可信普通文件：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expect_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlError(f"{label} 必须是对象")
    return value


def expect_exact_keys(
    value: dict[str, Any],
    required: Iterable[str],
    label: str,
    *,
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    optional_set = set(optional)
    actual = set(value)
    missing = sorted(required_set - actual)
    extra = sorted(actual - required_set - optional_set)
    if missing or extra:
        raise ControlError(f"{label} 字段不闭合：missing={missing}, extra={extra}")


def expect_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ControlError(f"{label} 必须是无首尾空白的非空字符串")
    return value


def expect_safe_id(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if not SAFE_ID_RE.fullmatch(text):
        raise ControlError(f"{label} 不是安全标识符")
    return text


def expect_sha256(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if not SHA256_RE.fullmatch(text):
        raise ControlError(f"{label} 不是小写 SHA-256")
    return text


def expect_image_digest(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if not IMAGE_DIGEST_RE.fullmatch(text):
        raise ControlError(f"{label} 不是固定 OCI image digest")
    return text


def expect_rfc3339(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if not RFC3339_RE.fullmatch(text):
        raise ControlError(f"{label} 不是 RFC3339 时间")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ControlError(f"{label} 不是有效时间") from error
    if parsed.tzinfo is None:
        raise ControlError(f"{label} 必须带时区")
    return text


def expect_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ControlError(f"{label} 必须是布尔值")
    return value


def expect_non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlError(f"{label} 必须是非负整数")
    return value


def expect_string_list(
    value: Any,
    label: str,
    *,
    non_empty: bool = True,
    sorted_unique: bool = True,
) -> list[str]:
    if not isinstance(value, list) or (non_empty and not value):
        raise ControlError(f"{label} 必须是{'非空' if non_empty else ''}字符串数组")
    result = [expect_string(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if sorted_unique and result != sorted(set(result)):
        raise ControlError(f"{label} 必须严格排序且不得重复")
    return result


def validate_relative_path(value: Any, label: str) -> str:
    text = expect_string(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ControlError(f"{label} 必须是根内规范相对路径")
    if path.as_posix() != text:
        raise ControlError(f"{label} 不是规范 POSIX 相对路径")
    return text


def validate_external_binding(value: Any, label: str) -> dict[str, Any]:
    binding = expect_object(value, label)
    expect_exact_keys(binding, {"path", "sha256", "bytes"}, label)
    validate_relative_path(binding["path"], f"{label}.path")
    expect_sha256(binding["sha256"], f"{label}.sha256")
    expect_non_negative_int(binding["bytes"], f"{label}.bytes")
    return binding


def resolve_relative(root: Path, relative: str) -> Path:
    validate_relative_path(relative, "relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ControlError(f"路径越过 Store 根：{relative}")
    return resolved


def ensure_directory(path: Path, *, mode: int = 0o700) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ControlError(f"目录路径不可信：{path}")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    path.chmod(mode)


def write_once(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """以 O_EXCL 原子创建文件；任何现有目标都视为覆盖尝试。"""

    ensure_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as error:
        raise ControlError(f"禁止覆盖已存在的不可变文件：{path}") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(mode)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def atomic_replace_untrusted(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """只用于派生缓存或锁信息；不可变事实不得调用本函数。"""

    ensure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        temporary.unlink(missing_ok=True)


def verify_mode(path: Path, expected: int, label: str) -> None:
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != expected:
        raise ControlError(f"{label} 权限必须是 {oct(expected)}，实际为 {oct(actual)}")
