#!/usr/bin/env python3
"""对已 finalized 的单个抓包运行目录执行最终精确秘密扫描。

该工具不修改 run_dir。它会扫描其中包括最终 `manifest.json`
在内的全部普通文件，并把回执排他写入 run_dir 之外的新文件。
秘密只能通过进程环境传入：

    python3 post_run_secret_scan_receipt.py \
      --run-dir /capture/runs/official-client/oauth/<run-id> \
      --output /capture/post-run-secret-scan-receipts/<run-id>.json \
      --secret-env claude_oauth_runtime_access_token_value=CLAUDE_CAPTURE_OAUTH_TOKEN \
      --secret-env operator_scan_env:CLAUDE_CAPTURE_REFRESH_TOKEN=CLAUDE_CAPTURE_REFRESH_TOKEN

`--secret-env NAME` 是 `NAME=NAME` 的简写。回执只记录等号左边的
label，永不记录环境变量名与值的映射、秘密值或秘密值摘要。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "official-client-post-run-secret-scan-receipt/v1"
ALGORITHM = "exact-byte-match/v1"
CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,191}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReceiptError(RuntimeError):
    """不能安全生成回执的结构性错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TreeEntry:
    """首次枚举时固定的普通文件元数据。"""

    path: Path
    relative: str
    device: int
    inode: int
    size: int
    mode: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class ScannedFile:
    """一个已完整读取的文件。"""

    inventory: dict[str, Any]
    secret_labels: tuple[str, ...]
    content: bytes | None


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 键，避免 finalized 判定出现二义性。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-key")
        result[key] = value
    return result


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _validate_run_dir(raw_run_dir: Path) -> tuple[Path, Path]:
    """固定非符号链接运行根，返回词法路径与解析路径。"""
    run_dir = _absolute(raw_run_dir)
    try:
        metadata = run_dir.lstat()
    except OSError as error:
        raise ReceiptError("run_dir_missing", "运行目录不存在") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ReceiptError("run_dir_symlink", "运行目录不得是符号链接")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReceiptError("run_dir_not_directory", "运行根必须是目录")
    return run_dir, run_dir.resolve(strict=True)


def _validate_output_path(
    raw_output: Path,
    *,
    run_dir_resolved: Path,
) -> Path:
    """要求精确输出文件位于 run_dir 外，且尚不存在。"""
    output = _absolute(raw_output)
    if os.path.lexists(output):
        raise ReceiptError("output_exists", "输出文件已存在，拒绝覆盖")
    parent = output.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise ReceiptError("output_parent_missing", "输出父目录不存在") from error
    if stat.S_ISLNK(parent_metadata.st_mode):
        raise ReceiptError("output_parent_symlink", "输出父目录不得是符号链接")
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ReceiptError("output_parent_not_directory", "输出父路径必须是目录")
    candidate_resolved = parent.resolve(strict=True) / output.name
    if candidate_resolved.is_relative_to(run_dir_resolved):
        raise ReceiptError("output_inside_run", "输出文件必须位于运行目录之外")
    return output


def _parse_secret_specs(
    specifications: Sequence[str],
    environ: Mapping[str, str],
) -> dict[str, bytes]:
    """解析 LABEL=ENV_NAME，只从环境读值并只保留 label。"""
    if not specifications:
        raise ReceiptError("secret_spec_missing", "至少需要一个秘密环境映射")
    secrets: dict[str, bytes] = {}
    for specification in specifications:
        if "=" in specification:
            label, env_name = specification.split("=", 1)
        else:
            label = env_name = specification
        if not LABEL_RE.fullmatch(label):
            raise ReceiptError("secret_label_invalid", "秘密 label 格式非法")
        if not ENV_NAME_RE.fullmatch(env_name):
            raise ReceiptError("secret_env_name_invalid", "秘密环境变量名格式非法")
        if label in secrets:
            raise ReceiptError("secret_label_duplicate", "秘密 label 不得重复")
        value = environ.get(env_name)
        if value is None or value == "":
            raise ReceiptError("secret_value_empty", "秘密环境值缺失或为空")
        secrets[label] = value.encode("utf-8")
    return secrets


def _path_contains_secret(relative: str, secrets: Mapping[str, bytes]) -> bool:
    encoded = relative.encode("utf-8", errors="surrogateescape")
    return any(secret in encoded for secret in secrets.values())


def _enumerate_tree(run_dir: Path, secrets: Mapping[str, bytes]) -> list[TreeEntry]:
    """确定性枚举全树；任何符号链接或非普通节点都立即拒绝。"""
    files: list[TreeEntry] = []

    def visit(directory: Path, prefix: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise ReceiptError("tree_enumeration_failed", "无法枚举运行目录") from error
        for entry in entries:
            relative_path = prefix / entry.name
            relative = relative_path.as_posix()
            if _path_contains_secret(relative, secrets):
                raise ReceiptError(
                    "secret_in_path",
                    "运行目录存在包含秘密值的路径名",
                )
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ReceiptError("tree_stat_failed", "无法读取运行产物元数据") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ReceiptError("tree_symlink", "运行目录不得包含符号链接")
            path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                visit(path, relative_path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ReceiptError("tree_non_regular", "运行目录不得包含非普通文件")
            files.append(
                TreeEntry(
                    path=path,
                    relative=relative,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    size=metadata.st_size,
                    mode=metadata.st_mode,
                    mtime_ns=metadata.st_mtime_ns,
                    ctime_ns=metadata.st_ctime_ns,
                )
            )

    visit(run_dir, PurePosixPath())
    files.sort(key=lambda item: item.relative)
    return files


def _metadata_identity(entry: TreeEntry) -> tuple[int, ...]:
    return (
        entry.device,
        entry.inode,
        entry.size,
        entry.mode,
        entry.mtime_ns,
        entry.ctime_ns,
    )


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _safe_errno(error: OSError) -> str:
    return errno.errorcode.get(error.errno or 0, "EUNKNOWN")


def _scan_file(entry: TreeEntry, secrets: Mapping[str, bytes]) -> ScannedFile:
    """以 O_NOFOLLOW 逐字节读取一个文件，支持跨 chunk 命中。"""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(entry.path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReceiptError("file_not_regular", "扫描对象已不是普通文件")
        if _stat_identity(before) != _metadata_identity(entry):
            raise ReceiptError("file_changed_before_read", "产物在枚举后、读取前发生变化")
        digest = hashlib.sha256()
        found: set[str] = set()
        byte_count = 0
        max_secret_size = max(len(secret) for secret in secrets.values())
        overlap = b""
        manifest_chunks: list[bytes] | None = (
            [] if entry.relative == "manifest.json" else None
        )
        while True:
            chunk = os.read(descriptor, CHUNK_SIZE)
            if not chunk:
                break
            byte_count += len(chunk)
            digest.update(chunk)
            if manifest_chunks is not None:
                if byte_count > MAX_MANIFEST_BYTES:
                    raise ReceiptError("manifest_too_large", "最终 manifest 超过安全大小上限")
                manifest_chunks.append(chunk)
            window = overlap + chunk
            for label, secret in secrets.items():
                if label not in found and secret in window:
                    found.add(label)
            overlap = window[-(max_secret_size - 1):] if max_secret_size > 1 else b""
        after = os.fstat(descriptor)
        if _stat_identity(after) != _stat_identity(before) or byte_count != before.st_size:
            raise ReceiptError("file_changed_during_read", "产物在扫描期间发生变化")
        return ScannedFile(
            inventory={
                "path": entry.relative,
                "size": byte_count,
                "sha256": digest.hexdigest(),
            },
            secret_labels=tuple(sorted(found)),
            content=b"".join(manifest_chunks) if manifest_chunks is not None else None,
        )
    finally:
        os.close(descriptor)


def _parse_final_manifest(content: bytes, run_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ReceiptError("manifest_invalid_json", "最终 manifest 不是唯一可解释的 JSON") from error
    if not isinstance(manifest, dict):
        raise ReceiptError("manifest_not_object", "最终 manifest 必须是 JSON 对象")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id or run_id != run_dir.name:
        raise ReceiptError("manifest_run_id_invalid", "最终 manifest run_id 与目录不一致")
    if manifest.get("status") != "complete":
        raise ReceiptError("manifest_not_complete", "只接受 status=complete 的最终 manifest")
    if not isinstance(manifest.get("ended_at"), str) or not manifest["ended_at"]:
        raise ReceiptError("manifest_not_finalized", "最终 manifest 缺少 ended_at")
    cleanup = manifest.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("attempted") is not True or cleanup.get("successful") is not True:
        raise ReceiptError("manifest_cleanup_incomplete", "最终 manifest 没有成功完成清理")
    secret_scan = manifest.get("secret_scan")
    if not isinstance(secret_scan, dict) or secret_scan.get("performed") is not True or secret_scan.get("passed") is not True:
        raise ReceiptError("manifest_secret_scan_incomplete", "最终 manifest 内部秘密扫描未通过")
    return manifest


def _manifest_secret_scope(manifest: Mapping[str, Any]) -> list[str]:
    scope = manifest["secret_scan"].get("scope")
    if (
        not isinstance(scope, list)
        or not scope
        or any(not isinstance(label, str) or not LABEL_RE.fullmatch(label) for label in scope)
        or len(set(scope)) != len(scope)
    ):
        raise ReceiptError("manifest_secret_scope_invalid", "最终 manifest 的秘密 scope 非法")
    return sorted(scope)


def _manifest_artifact_inventory(manifest: Mapping[str, Any]) -> dict[str, tuple[int, str]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReceiptError("manifest_artifacts_invalid", "最终 manifest artifacts 非法")
    result: dict[str, tuple[int, str]] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            raise ReceiptError("manifest_artifacts_invalid", "最终 manifest artifacts 非法")
        relative = item.get("path")
        size = item.get("size")
        sha256 = item.get("sha256")
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ReceiptError("manifest_artifact_path_invalid", "manifest artifact 路径非法")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or pure.as_posix() != relative:
            raise ReceiptError("manifest_artifact_path_invalid", "manifest artifact 路径非法")
        if relative == "manifest.json" or relative in result:
            raise ReceiptError("manifest_artifact_duplicate", "manifest artifact 重复或包含 manifest")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ReceiptError("manifest_artifact_size_invalid", "manifest artifact size 非法")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise ReceiptError("manifest_artifact_sha_invalid", "manifest artifact SHA-256 非法")
        result[relative] = (size, sha256)
    return result


def _artifact_integrity_errors(
    manifest: Mapping[str, Any],
    files: Sequence[dict[str, Any]],
) -> list[str]:
    expected = _manifest_artifact_inventory(manifest)
    actual = {
        item["path"]: (item["size"], item["sha256"])
        for item in files
        if item["path"] != "manifest.json"
    }
    errors: list[str] = []
    for relative in sorted(set(expected) - set(actual)):
        errors.append(f"artifact_missing:{relative}")
    for relative in sorted(set(actual) - set(expected)):
        errors.append(f"artifact_unexpected:{relative}")
    for relative in sorted(set(actual) & set(expected)):
        actual_size, actual_sha = actual[relative]
        expected_size, expected_sha = expected[relative]
        if actual_size != expected_size:
            errors.append(f"artifact_size_mismatch:{relative}")
        if actual_sha != expected_sha:
            errors.append(f"artifact_sha256_mismatch:{relative}")
    return errors


def _tree_identity(entries: Sequence[TreeEntry]) -> list[tuple[str, tuple[int, ...]]]:
    return [(entry.relative, _metadata_identity(entry)) for entry in entries]


def _exclusive_write_receipt(output: Path, receipt: Mapping[str, Any]) -> None:
    """以 0600 排他写入精确目标，任何已存在文件都不覆盖。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = json.dumps(
        receipt,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    try:
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ReceiptError("receipt_write_failed", "无法以排他方式写入回执") from error


def generate_receipt(
    *,
    run_dir: Path,
    output: Path,
    secret_specs: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """扫描一个 finalized run_dir，并排他写入外部回执文件。"""
    secrets = _parse_secret_specs(secret_specs, environ if environ is not None else os.environ)
    run_alias, run_resolved = _validate_run_dir(run_dir)
    validated_output = _validate_output_path(
        output,
        run_dir_resolved=run_resolved,
    )
    initial_tree = _enumerate_tree(run_alias, secrets)
    if not any(entry.relative == "manifest.json" for entry in initial_tree):
        raise ReceiptError("manifest_missing", "运行目录缺少最终 manifest.json")

    inventory: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    scan_errors: list[str] = []
    manifest_content: bytes | None = None
    for entry in initial_tree:
        try:
            scanned = _scan_file(entry, secrets)
        except ReceiptError as error:
            scan_errors.append(f"{error.code}:{entry.relative}")
            continue
        except OSError as error:
            scan_errors.append(f"read_error:{entry.relative}:{_safe_errno(error)}")
            continue
        inventory.append(scanned.inventory)
        if scanned.secret_labels:
            matches.append({
                "path": entry.relative,
                "secret_labels": list(scanned.secret_labels),
            })
        if entry.relative == "manifest.json":
            manifest_content = scanned.content

    try:
        final_tree = _enumerate_tree(run_alias, secrets)
    except ReceiptError as error:
        raise ReceiptError("tree_changed_invalid", "扫描后的运行目录结构非法") from error
    if _tree_identity(initial_tree) != _tree_identity(final_tree):
        scan_errors.append("tree_changed_during_scan")
    inventory.sort(key=lambda item: item["path"])
    matches.sort(key=lambda item: item["path"])

    if manifest_content is None:
        raise ReceiptError("manifest_read_failed", "无法完整读取最终 manifest")
    manifest = _parse_final_manifest(manifest_content, run_alias)
    secret_labels = sorted(secrets)
    if _manifest_secret_scope(manifest) != secret_labels:
        raise ReceiptError("secret_scope_mismatch", "回执 label 与最终 manifest scope 不一致")
    if any(secret.decode("utf-8") in manifest["run_id"] for secret in secrets.values()):
        raise ReceiptError("secret_in_run_id", "run_id 不得包含秘密值")

    artifact_errors = _artifact_integrity_errors(manifest, inventory)
    scan_errors.extend(artifact_errors)
    scan_errors = sorted(set(scan_errors))
    manifest_entry = next(
        (item for item in inventory if item["path"] == "manifest.json"),
        None,
    )
    if manifest_entry is None:
        raise ReceiptError("manifest_inventory_missing", "最终 manifest 未进入文件清单")
    inventory_sha256 = _sha256_bytes(_canonical_json_bytes(inventory))
    passed = not matches and not scan_errors
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "scan_root": ".",
        "algorithm": ALGORITHM,
        "secret_labels": secret_labels,
        "final_manifest_sha256": manifest_entry["sha256"],
        "files": inventory,
        "file_count": len(inventory),
        "byte_count": sum(item["size"] for item in inventory),
        "inventory_sha256": inventory_sha256,
        "manifest_artifact_inventory_verified": not artifact_errors,
        "matches": matches,
        "scan_errors": scan_errors,
        "passed": passed,
        "tool_sha256": _file_sha256(Path(__file__)),
    }
    serialized_receipt = _canonical_json_bytes(receipt)
    for secret in secrets.values():
        if secret in serialized_receipt:
            raise ReceiptError("secret_in_receipt", "待写回执含有秘密值")
    _exclusive_write_receipt(validated_output, receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--secret-env",
        action="append",
        required=True,
        metavar="LABEL=ENV_NAME",
        help="从 ENV_NAME 读取秘密，回执仅记录 LABEL；NAME 是 NAME=NAME 简写",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        receipt = generate_receipt(
            run_dir=arguments.run_dir,
            output=arguments.output,
            secret_specs=arguments.secret_env,
        )
    except ReceiptError as error:
        json.dump(
            {
                "schema_version": SCHEMA_VERSION,
                "passed": False,
                "error_code": error.code,
            },
            sys.stderr,
            ensure_ascii=False,
            sort_keys=True,
        )
        sys.stderr.write("\n")
        return 2
    json.dump(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": receipt["run_id"],
            "passed": receipt["passed"],
            "file_count": receipt["file_count"],
            "byte_count": receipt["byte_count"],
        },
        sys.stdout,
        ensure_ascii=False,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
