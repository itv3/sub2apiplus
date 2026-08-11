#!/usr/bin/env python3
"""生成可字节比较的状态快照，并对候选证据执行秘密泄漏扫描。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    # 允许从仓库根目录按文档直接执行本文件。
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.capturelib.security import secure_write_text


STATE_SCHEMA_VERSION = "codex-candidate-normalized-state/v1"
GUARD_REPORT_SCHEMA_VERSION = "codex-candidate-evidence-guard-report/v1"
CAPTURE_MANIFEST_SCHEMA_VERSION = "codex-candidate-capture-manifest/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SENSITIVE_STATE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "session_token",
    }
)
SENSITIVE_STATE_KEY_RE = re.compile(
    r"(?:^|_)(?:access_token|api_key|authorization|cookie|credential|id_token|"
    r"password|refresh_token|secret|session_token)(?:$|_)"
)
SAFE_SENSITIVE_SUFFIXES = (
    "_count",
    "_digest",
    "_present",
    "_sha256",
    "_status",
)
VOLATILE_STATE_KEYS = frozenset(
    {
        "captured_at",
        "created_at",
        "ended_at",
        "generated_at",
        "restart_count",
        "started_at",
        "timestamp",
        "updated_at",
        "uptime",
        "uptime_seconds",
    }
)

# 规则只命中具有凭据语义的稳定前缀。随机二进制偶然含短 ASCII 的概率不应导致
# 大量误报，因此所有通用 token 形态均要求足够长度。
HEURISTIC_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "authorization-value",
        re.compile(
            rb"(?i)authorization\s*[:=]\s*(?!<(?:redacted|secret))"
            rb"(?:bearer\s+)?[a-z0-9._~+/=-]{12,}"
        ),
    ),
    (
        "cookie-value",
        re.compile(
            rb"(?i)(?:^|[\r\n{,])\s*(?:cookie|set-cookie)\s*[:=]\s*"
            rb"(?!<(?:redacted|secret))[a-z0-9._~+/=;:%-]{12,}"
        ),
    ),
    (
        "json-secret-field",
        # 白名单必须同时认 `relay_extract.shape_value` 的长度占位符：body 摘要对
        # >24 字符的串一律降成 `str:<len=N>`，refresh_token 这类长凭据只剩长度。
        # 原先的负向前瞻只认 `<redacted`／`<secret`，会把已脱敏的
        # `"refresh_token":"str:<len=211>"` 判成明文凭据（k40 的 A13 派生观测即
        # 因此在 seal 处失败关闭）。这里只放行「`str:<len=` ＋纯数字 ＋ `>`」这一种
        # 确定形态——`str:` 后跟原文的短值（≤24 字符，如短 api_key）仍然照常命中。
        re.compile(
            rb'(?i)"(?:access_token|refresh_token|id_token|api_key|password|secret)"'
            rb'\s*:\s*"(?!<(?:redacted|secret)|str:<len=\d+>")[^"\r\n]{8,}"'
        ),
    ),
    (
        "openai-key-shape",
        re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    ),
    (
        "jwt-shape",
        re.compile(
            rb"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\."
            rb"[A-Za-z0-9_-]{8,}\b"
        ),
    ),
)
SCAN_CHUNK_SIZE = 1024 * 1024
SCAN_OVERLAP = 64 * 1024


class EvidenceGuardError(ValueError):
    """状态快照或证据归档不满足安全约束。"""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝会造成状态或证据字段覆盖的 JSON 重复键。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceGuardError(f"JSON 对象包含重复字段：{key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class SecretFinding:
    """不含秘密原文的扫描命中。"""

    path: str
    rule: str
    offset: int

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "rule": self.rule, "offset": self.offset}


def file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, description: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise EvidenceGuardError(f"{description}必须是非符号链接普通文件：{path}")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceGuardError(f"{description}不是合法 UTF-8 JSON：{error}") from error


def _state_key_is_sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized.endswith(SAFE_SENSITIVE_SUFFIXES):
        return False
    return normalized in SENSITIVE_STATE_KEYS or bool(
        SENSITIVE_STATE_KEY_RE.search(normalized)
    )


def _validate_state_value(value: Any, path: str = "state") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise EvidenceGuardError(f"{path} 包含空或非字符串字段名")
            normalized = raw_key.lower().replace("-", "_")
            if normalized in VOLATILE_STATE_KEYS:
                raise EvidenceGuardError(
                    f"规范化状态不得包含自然波动字段：{path}.{raw_key}"
                )
            if _state_key_is_sensitive(raw_key):
                raise EvidenceGuardError(
                    f"规范化状态不得保存凭据原文，请改存 *_sha256 或 *_present："
                    f"{path}.{raw_key}"
                )
            _validate_state_value(item, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_state_value(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise EvidenceGuardError(f"{path} 包含 NaN 或 Infinity，不能稳定序列化")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise EvidenceGuardError(f"{path} 包含不能稳定序列化的值")


def normalize_state(value: Any) -> bytes:
    """验证并生成可逐字节比较的规范化状态 JSON。"""

    if not isinstance(value, dict) or not value:
        raise EvidenceGuardError("规范化状态必须是包含实际字段的非空对象")
    _validate_state_value(value)
    normalized = {
        "schema_version": STATE_SCHEMA_VERSION,
        "state": value,
    }
    return (
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_state_snapshot(input_path: Path, output_path: Path) -> dict[str, Any]:
    """从不含凭据的原始状态 JSON 生成 0600 规范化快照。"""

    value = _load_json(input_path, "状态输入")
    payload = normalize_state(value)
    secure_write_text(output_path, payload.decode("utf-8"))
    return {
        "path": str(output_path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _validate_snapshot(path: Path, description: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise EvidenceGuardError(f"{description}必须是非符号链接普通文件：{path}")
    payload = path.read_bytes()
    if not payload:
        raise EvidenceGuardError(f"{description}不能为空")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceGuardError(f"{description}不是合法 UTF-8 JSON：{error}") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "state"}:
        raise EvidenceGuardError(f"{description}不是 guard 生成的封闭状态结构")
    if value.get("schema_version") != STATE_SCHEMA_VERSION:
        raise EvidenceGuardError(f"{description} schema_version 不匹配")
    expected = normalize_state(value.get("state"))
    if payload != expected:
        raise EvidenceGuardError(f"{description}不是稳定紧凑 JSON 字节格式")
    return payload


def verify_restoration(before: Path, after: Path) -> dict[str, Any]:
    """验证 before/after 为不同 inode 且内容逐字节一致。"""

    before_payload = _validate_snapshot(before, "before 快照")
    after_payload = _validate_snapshot(after, "after 快照")
    try:
        different_inode = not os.path.samestat(before.stat(), after.stat())
    except OSError as error:
        raise EvidenceGuardError(f"无法比较快照 inode：{error}") from error
    if not different_inode:
        raise EvidenceGuardError("before 与 after 不能是同一文件或硬链接")
    if before_payload != after_payload:
        raise EvidenceGuardError("环境未恢复：before 与 after 字节不一致")
    return {
        "passed": True,
        "different_inode": True,
        "byte_identical": True,
        "sha256": hashlib.sha256(before_payload).hexdigest(),
        "bytes": len(before_payload),
    }


def _relative_path(value: Any, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise EvidenceGuardError(f"{description}必须是 POSIX 相对路径")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value in {".", ".."}
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise EvidenceGuardError(f"{description}不能逃逸证据根目录")
    return path


def _resolve_artifact(evidence_root: Path, relative: PurePosixPath) -> Path:
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise EvidenceGuardError("证据根目录必须是非符号链接目录")
    root = evidence_root.resolve(strict=True)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceGuardError(f"证据路径包含符号链接：{relative}")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise EvidenceGuardError(f"证据不存在或逃逸根目录：{relative}") from error
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise EvidenceGuardError(f"证据必须是非空普通文件：{relative}")
    return resolved


def _archive_relative_file(
    path: Path, evidence_root: Path, description: str
) -> tuple[str, Path]:
    """要求 guard 输入位于证据根内，且根内路径不经过符号链接。"""

    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise EvidenceGuardError("证据根目录必须是非符号链接目录")
    root = evidence_root.resolve(strict=True)
    try:
        relative = path.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise EvidenceGuardError(
            f"{description}必须位于证据根目录内：{path}"
        ) from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceGuardError(f"{description}路径包含符号链接：{path}")
    if not current.is_file():
        raise EvidenceGuardError(f"{description}必须是普通文件：{path}")
    return str(PurePosixPath(*relative.parts)), current


def manifest_artifacts(
    capture_manifest: Path,
    evidence_root: Path,
    expected_codex_version: str,
) -> list[tuple[str, Path]]:
    """解析统一 capture manifest，并绑定每份 artifact 的实际 SHA。"""

    if not VERSION_RE.fullmatch(expected_codex_version):
        raise EvidenceGuardError("期望 Codex 版本必须是完整的 x.y.z 版本")
    value = _load_json(capture_manifest, "capture manifest")
    if not isinstance(value, dict):
        raise EvidenceGuardError("capture manifest 顶层必须是对象")
    if value.get("schema_version") != CAPTURE_MANIFEST_SCHEMA_VERSION:
        raise EvidenceGuardError("capture manifest schema_version 不匹配")
    if value.get("codex_version") != expected_codex_version:
        raise EvidenceGuardError("capture manifest Codex 版本与 Campaign 目标不一致")
    if not isinstance(value.get("capture_id"), str) or not value[
        "capture_id"
    ].strip():
        raise EvidenceGuardError("capture manifest capture_id 不能为空")
    if value.get("status") != "complete":
        raise EvidenceGuardError("capture manifest status 必须是 complete")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise EvidenceGuardError("capture manifest artifacts 必须是非空数组")
    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise EvidenceGuardError(f"artifacts[{index}] 必须是对象")
        raw_path = artifact.get("path")
        relative = _relative_path(raw_path, f"artifacts[{index}].path")
        if raw_path in seen:
            raise EvidenceGuardError(f"重复证据路径：{raw_path}")
        seen.add(raw_path)
        expected_sha = artifact.get("sha256")
        if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
            raise EvidenceGuardError(f"artifacts[{index}].sha256 格式非法")
        resolved = _resolve_artifact(evidence_root, relative)
        if file_sha256(resolved) != expected_sha:
            raise EvidenceGuardError(f"证据 SHA-256 不匹配：{raw_path}")
        result.append((raw_path, resolved))
    return result


def _known_secrets(secret_env_names: Sequence[str]) -> list[tuple[str, bytes]]:
    secrets: list[tuple[str, bytes]] = []
    for name in secret_env_names:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise EvidenceGuardError(f"秘密环境变量名非法：{name!r}")
        value = os.environ.get(name)
        if value is None:
            raise EvidenceGuardError(f"秘密环境变量未设置：{name}")
        encoded = value.encode("utf-8")
        if len(encoded) < 8:
            raise EvidenceGuardError(
                f"秘密环境变量 {name} 少于 8 字节，拒绝产生高误报扫描"
            )
        secrets.append((name, encoded))
    return secrets


def scan_files_for_secrets(
    files: Sequence[tuple[str, Path]],
    *,
    secret_env_names: Sequence[str] = (),
) -> dict[str, Any]:
    """扫描全部文本和二进制文件；报告不保存秘密原文。"""

    secrets = _known_secrets(secret_env_names)
    overlap_size = max(
        SCAN_OVERLAP,
        max((len(secret) - 1 for _, secret in secrets), default=0),
    )
    findings: list[SecretFinding] = []
    scanned_bytes = 0
    for relative, path in files:
        overlap = b""
        file_offset = 0
        with path.open("rb") as stream:
            while chunk := stream.read(SCAN_CHUNK_SIZE):
                scanned_bytes += len(chunk)
                payload = overlap + chunk
                payload_base = file_offset - len(overlap)
                for name, secret in secrets:
                    start = 0
                    while True:
                        offset = payload.find(secret, start)
                        if offset < 0:
                            break
                        findings.append(
                            SecretFinding(
                                path=relative,
                                rule=f"known-secret-env:{name}",
                                offset=payload_base + offset,
                            )
                        )
                        start = offset + len(secret)
                for rule, pattern in HEURISTIC_PATTERNS:
                    for match in pattern.finditer(payload):
                        findings.append(
                            SecretFinding(
                                path=relative,
                                rule=rule,
                                offset=payload_base + match.start(),
                            )
                        )
                overlap = payload[-overlap_size:]
                file_offset += len(chunk)
    deduplicated = {
        (finding.path, finding.rule, finding.offset): finding
        for finding in findings
    }
    ordered = [
        deduplicated[key].as_dict() for key in sorted(deduplicated)
    ]
    return {
        "passed": not ordered,
        "known_secret_env_names": sorted(set(secret_env_names)),
        "file_count": len(files),
        "scanned_bytes": scanned_bytes,
        "findings": ordered,
    }


def verify_evidence_guard(
    *,
    before: Path,
    after: Path,
    capture_manifest: Path,
    evidence_root: Path,
    expected_codex_version: str,
    secret_env_names: Sequence[str] = (),
) -> dict[str, Any]:
    """执行 restoration 和全 artifact secret scan。"""

    before_relative, before_path = _archive_relative_file(
        before, evidence_root, "before 快照"
    )
    after_relative, after_path = _archive_relative_file(
        after, evidence_root, "after 快照"
    )
    manifest_relative, manifest_path = _archive_relative_file(
        capture_manifest, evidence_root, "capture manifest"
    )
    restoration = verify_restoration(before_path, after_path)
    artifacts = manifest_artifacts(
        manifest_path,
        evidence_root,
        expected_codex_version,
    )
    state_files = [
        (before_relative, before_path),
        (after_relative, after_path),
        (manifest_relative, manifest_path),
    ]
    secret_scan = scan_files_for_secrets(
        [*artifacts, *state_files], secret_env_names=secret_env_names
    )
    return {
        "schema_version": GUARD_REPORT_SCHEMA_VERSION,
        "codex_version": expected_codex_version,
        "status": "pass" if secret_scan["passed"] else "fail",
        "restoration": restoration,
        "secret_scan": secret_scan,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    secure_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="候选抓包状态快照、字节恢复与秘密泄漏门禁"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot = commands.add_parser("snapshot", help="生成稳定状态快照")
    snapshot.add_argument("--input", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)

    verify = commands.add_parser("verify", help="验证恢复并扫描证据")
    verify.add_argument("--before", type=Path, required=True)
    verify.add_argument("--after", type=Path, required=True)
    verify.add_argument("--capture-manifest", type=Path, required=True)
    verify.add_argument("--evidence-root", type=Path, required=True)
    verify.add_argument("--expected-codex-version", required=True)
    verify.add_argument("--secret-env", action="append", default=[])
    verify.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            result = write_state_snapshot(args.input, args.output)
            payload: Mapping[str, Any] = {
                "schema_version": STATE_SCHEMA_VERSION,
                "status": "pass",
                "snapshot": result,
            }
            exit_code = 0
        else:
            report = verify_evidence_guard(
                before=args.before,
                after=args.after,
                capture_manifest=args.capture_manifest,
                evidence_root=args.evidence_root,
                expected_codex_version=args.expected_codex_version,
                secret_env_names=args.secret_env,
            )
            _write_json(args.report, report)
            payload = report
            exit_code = 0 if report["status"] == "pass" else 1
    except (EvidenceGuardError, OSError) as error:
        payload = {
            "schema_version": GUARD_REPORT_SCHEMA_VERSION,
            "status": "fail",
            "error": str(error),
        }
        if args.command == "verify":
            payload["codex_version"] = args.expected_codex_version
            _write_json(args.report, payload)
        exit_code = 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
