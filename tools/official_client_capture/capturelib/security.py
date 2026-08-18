"""权限、脱敏和秘密泄漏检查。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import ConfigurationError


SENSITIVE_HEADER_RE = re.compile(
    r"(authorization|api[-_]?key|x-api-key|cookie|token|secret|password|credential)",
    re.I,
)
DYNAMIC_HEADER_RE = re.compile(
    r"(^date$|request[-_]?id|session[-_]?id|thread[-_]?id|conversation[-_]?id|"
    r"turn[-_]?(id|metadata|state)|window[-_]?id|installation[-_]?id|"
    r"trace[-_]?id|content-length)",
    re.I,
)
DYNAMIC_BODY_KEY_RE = re.compile(
    r"(^id$|_id$|session|thread|conversation|request_id|prompt_cache_key)", re.I
)
TEXT_BODY_KEY_RE = re.compile(
    r"(content|text|instructions|system|prompt|description|arguments|delta|output|"
    r"value|user|metadata)",
    re.I,
)
STABLE_BODY_STRING_KEYS = {
    "model",
    "name",
    "role",
    "status",
    "type",
}
SENSITIVE_ARG_RE = re.compile(
    r"(^|[-_])(api[-_]?key|authorization|cookie|token|secret|password|credential)($|[-_])",
    re.I,
)


def ensure_private_directory(path: Path, root: Path | None = None) -> Path:
    """创建 0700 目录，并拒绝通过符号链接逃离限定根目录。"""

    resolved_root = root.resolve() if root else None
    resolved_path = path.resolve(strict=False)
    if resolved_root and not resolved_path.is_relative_to(resolved_root):
        raise ConfigurationError(f"目录越过允许边界：{path}")
    if path.exists() and path.is_symlink():
        raise ConfigurationError(f"拒绝使用符号链接目录：{path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def secure_write_text(path: Path, content: str) -> None:
    """以 0600 权限原子写入文本。"""

    # 通用安全写入不能更改调用方已有父目录（例如 /tmp 或仓库）的权限。
    # 只有父目录不存在时，才创建一个新的 0700 目录。
    if path.parent.exists():
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise ConfigurationError(f"输出父目录非法：{path.parent}")
    else:
        ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def secure_write_json(path: Path, payload: Any) -> None:
    """以稳定格式写入 JSON。"""

    secure_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def canonical_json_sha256(value: Any) -> str:
    """计算脱敏 JSON 结构的稳定摘要。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def argv_manifest_view(
    argv: Sequence[str], known_secrets: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """生成实际 argv 的安全归档视图，不对原始秘密派生摘要。"""

    secrets = {
        source: value
        for source, value in (known_secrets or {}).items()
        if value
    }
    safe: list[str] = []
    redactions: list[dict[str, Any]] = []
    redact_next = False
    for index, raw_value in enumerate(argv):
        value = str(raw_value)
        reasons: list[str] = []
        if redact_next:
            value = "<redacted-sensitive-argument>"
            reasons.append("sensitive_option_value")
            redact_next = False
        else:
            for source, secret in secrets.items():
                if secret in value:
                    value = value.replace(secret, "<redacted-runtime-secret>")
                    reasons.append(source)
            option_name = value.partition("=")[0]
            if value.startswith("--") and SENSITIVE_ARG_RE.search(option_name):
                if "=" in value:
                    option, _separator, _option_value = value.partition("=")
                    value = f"{option}=<redacted-sensitive-argument>"
                    reasons.append("sensitive_option_inline_value")
                else:
                    redact_next = True
        safe.append(value)
        if reasons:
            redactions.append({"index": index, "reasons": sorted(set(reasons))})
    payload = {
        "schema_version": "official-client-invocation/v1",
        "shell": False,
        "argv_redacted": safe,
        "redactions": redactions,
        "redaction_policy": "known-secret-and-sensitive-option/v1",
    }
    payload["argv_sha256"] = canonical_json_sha256(safe)
    return payload


def redact_known_secret(text: str, secret: str | None) -> str:
    """在保存 CLI 输出前消除本轮已知访问 Key。"""

    if not secret:
        return text
    return text.replace(secret, "<redacted-runtime-secret>")


def redact_header_value(name: str, value: str) -> str:
    """规范化 Header 值，保留稳定画像并删除秘密与动态身份。"""

    if SENSITIVE_HEADER_RE.search(name):
        return "<secret>"
    if name.lower() == "host":
        return "<target-host>"
    if DYNAMIC_HEADER_RE.search(name):
        return "<dynamic>"
    return value


def normalize_json_shape(value: Any, key: str = "") -> Any:
    """保留 JSON 结构和稳定枚举，脱敏正文与动态身份值。"""

    if isinstance(value, dict):
        return {
            str(item_key): normalize_json_shape(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [normalize_json_shape(item, key) for item in value]
    if DYNAMIC_BODY_KEY_RE.search(key):
        return f"<dynamic:{type(value).__name__}>"
    if TEXT_BODY_KEY_RE.search(key) and isinstance(value, str):
        return f"<text:{len(value)}>"
    if isinstance(value, str) and key.lower() not in STABLE_BODY_STRING_KEYS:
        # 默认不信任未知字符串字段；只保留少量画像所需的稳定枚举。
        return f"<string:{len(value)}>"
    return value


def file_sha256(path: Path) -> str:
    """计算文件 SHA-256，不加载整个 pcap。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_artifacts(run_dir: Path) -> list[dict[str, Any]]:
    """列出产物的相对路径、大小、哈希、权限和敏感等级。"""

    artifacts: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(run_dir)
        parts = relative.parts
        # 默认按原始敏感材料处理。只有规范化分析和经过字段白名单约束的
        # 场景 summary 才可标为脱敏，避免把 SQLite、会话或 last-message
        # 仅因后缀不在名单中而误标为可公开材料。
        redacted = bool(parts and parts[0] == "analysis") or (
            len(parts) >= 2
            and parts[0] == "results"
            and path.name in {"invocation.json", "summary.json"}
        )
        artifacts.append(
            {
                "path": str(relative),
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
                "mode": oct(path.stat().st_mode & 0o777),
                "sensitivity": "redacted" if redacted else "raw_private",
            }
        )
    return artifacts


def scan_for_secret(root: Path, secret: str | None) -> list[str]:
    """递归扫描文本产物，返回包含运行时秘密的相对路径。"""

    report = scan_for_secrets(
        root,
        {"runtime_secret": secret} if secret else {},
    )
    return sorted(
        str(item["path"])
        for item in report["matches"]
        if isinstance(item, dict) and item.get("path")
    )


def scan_for_secrets(
    root: Path, known_secrets: Mapping[str, str] | None
) -> dict[str, Any]:
    """对运行目录全部普通文件做多秘密逐字节扫描并记录边界。"""

    needles = {
        source: value.encode("utf-8")
        for source, value in (known_secrets or {}).items()
        if value
    }
    matches: list[dict[str, Any]] = []
    file_count = 0
    byte_count = 0
    scan_errors: list[str] = []
    maximum_needle = max((len(value) for value in needles.values()), default=0)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = str(path.relative_to(root))
        file_count += 1
        try:
            pending = set(needles)
            overlap = b""
            observed_bytes = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    observed_bytes += len(chunk)
                    window = overlap + chunk
                    for source in tuple(pending):
                        if needles[source] in window:
                            pending.remove(source)
                    if not pending:
                        # 找齐当前文件中的全部秘密后仍读到 EOF，使扫描字节数可复算。
                        for remainder in iter(
                            lambda: stream.read(1024 * 1024), b""
                        ):
                            observed_bytes += len(remainder)
                        break
                    overlap = (
                        window[-(maximum_needle - 1) :]
                        if maximum_needle > 1
                        else b""
                    )
        except OSError:
            scan_errors.append(relative)
            continue
        byte_count += observed_bytes
        sources = sorted(set(needles) - pending)
        if sources:
            matches.append({"path": relative, "secret_sources": sources})
    performed = bool(needles)
    return {
        "performed": performed,
        "algorithm": "exact-byte-match/v1" if performed else None,
        "scope": sorted(needles),
        "included_root": ".",
        "excluded": [],
        "file_count": file_count,
        "byte_count": byte_count,
        "matches": matches,
        "scan_errors": scan_errors,
        "passed": performed and not matches and not scan_errors,
        "limitation": (
            None
            if performed
            else "没有把本轮实际凭据值交给编排器，无法执行精确值扫描。"
        ),
    }


def scrub_known_secret(root: Path, secret: str | None) -> list[str]:
    """将文本产物中的已知运行时秘密就地替换，并返回处理路径。"""

    if not secret:
        return []
    needle = secret.encode("utf-8")
    replacement = b"<redacted-runtime-secret>"
    binary_suffixes = {".pcap", ".pcapng", ".mitm", ".flow"}
    scrubbed: list[str] = []
    for path in root.rglob("*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix.lower() in binary_suffixes
        ):
            continue
        content = path.read_bytes()
        if needle not in content:
            continue
        flags = os.O_WRONLY | os.O_TRUNC
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise ConfigurationError(f"拒绝清理不可信文件：{path}")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content.replace(needle, replacement))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        scrubbed.append(str(path.relative_to(root)))
    return sorted(scrubbed)
