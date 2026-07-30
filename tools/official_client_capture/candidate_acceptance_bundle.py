#!/usr/bin/env python3
"""生成 Codex CLI 0.145.0 候选侧正式 42 条验收包。

工具采用两个阶段，避免把环境恢复时间伪装成晚于断言执行：

1. ``assert`` 按冻结画像逐条执行候选断言，生成恰好 42 个结果；
2. 操作者采集恢复后的规范化状态；
3. ``finalize`` 只消费上述结果和既有证据，组装 submission 并执行最终门禁。

生成器不会运行提交中声明的抓包触发命令，也不会推断实现位置、部署身份或官方观察。
这些事实必须通过显式输入提供；缺失、摘要不符或集合不闭合时一律失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.candidate_42_acceptance import (
    ASSERTION_SCHEMA_VERSION,
    command_sha256,
)
from tools.official_client_capture.candidate_evidence_guard import (
    verify_evidence_guard,
    verify_restoration,
)
from tools.official_client_capture.candidate_rule_assertion import (
    CHECKER_RELATIVE_PATH,
    DEFAULT_PROFILE_RELATIVE_PATH,
    build_assertion_command,
    evaluate_rule,
    file_sha256,
    load_observations,
    load_profile,
)
from tools.official_client_capture.capturelib.security import secure_write_json


CODEX_VERSION = "0.145.0"
ASSERTION_INDEX_SCHEMA_VERSION = "codex-candidate-assertion-index/v1"
RULE_METADATA_SCHEMA_VERSION = "codex-candidate-bundle-rule-metadata/v1"
OFFICIAL_MAP_SCHEMA_VERSION = "codex-candidate-official-evidence-map/v1"
ACCEPTANCE_SCHEMA_VERSION = "codex-candidate-42-acceptance/v1"
REQUIRED_RULE_COUNT = 42

RULE_MANIFEST_RELATIVE_PATH = (
    "tools/official_client_capture/codex_upgrade_rules_0_145_0.json"
)
FINAL_CHECKER_RELATIVE_PATH = (
    "tools/official_client_capture/candidate_42_acceptance.py"
)
DEFAULT_ASSERTIONS_DIR = "assertions/candidate-42"
DEFAULT_SUBMISSION_PATH = "candidate-42-acceptance.json"
DEFAULT_REPORT_PATH = "candidate-42-acceptance-report.json"
DEFAULT_GUARD_REPORT_PATH = "restoration/candidate-evidence-guard-report.json"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RULE_ID_RE = re.compile(r"^SPEC-[A-Z0-9]+-[0-9]{3}$")

OFFICIAL_EVIDENCE_KINDS = frozenset(
    {
        "application_log",
        "database_trace",
        "filesystem_snapshot",
        "http_trace",
        "mitm_jsonl",
        "official_analysis",
        "official_index",
        "official_report",
        "pcap",
        "pcapng",
        "process_trace",
        "relay_binary",
        "source_excerpt",
        "stderr_log",
        "stdout_log",
        "tls_keylog",
        "websocket_trace",
        "wire_dump",
    }
)


class BundleConfigurationError(ValueError):
    """正式验收包的输入或归档不满足失败关闭约束。"""


def utc_now() -> str:
    """返回带时区的 UTC RFC 3339 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 字段，避免事实被后值静默覆盖。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleConfigurationError(f"JSON 对象包含重复字段：{key}")
        result[key] = value
    return result


def _load_json(path: Path, description: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise BundleConfigurationError(
            f"{description}必须是非符号链接普通文件：{path}"
        )
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BundleConfigurationError(
            f"{description}不是可读取的 UTF-8 JSON：{error}"
        ) from error


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    description: str,
) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - set(optional))
    if missing or extra:
        raise BundleConfigurationError(
            f"{description}字段不闭合：missing={missing} extra={extra}"
        )


def _require_text(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BundleConfigurationError(f"{description}必须是非空字符串")
    normalized = value.strip().lower().strip("<>[]{}：:。.!！")
    if normalized in {
        "placeholder",
        "todo",
        "tbd",
        "待填写",
        "待补充",
        "待采集",
        "占位",
        "示例",
    } or "待填写" in normalized or "待补充" in normalized:
        raise BundleConfigurationError(f"{description}不能是占位文本")
    return value


def _relative_path(value: Any, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise BundleConfigurationError(f"{description}必须是 POSIX 相对路径")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value in {".", ".."}
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise BundleConfigurationError(f"{description}不能逃逸根目录：{value}")
    return path


def _require_root(path: Path, description: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise BundleConfigurationError(
            f"{description}必须是非符号链接目录：{path}"
        )
    return path.resolve(strict=True)


def _resolve_existing_file(
    root: Path,
    relative: PurePosixPath,
    description: str,
) -> Path:
    root_resolved = _require_root(root, f"{description}根目录")
    current = root_resolved
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BundleConfigurationError(
                f"{description}路径包含符号链接：{relative}"
            )
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as error:
        raise BundleConfigurationError(
            f"{description}不存在或逃逸根目录：{relative}"
        ) from error
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise BundleConfigurationError(
            f"{description}必须是非空普通文件：{relative}"
        )
    return resolved


def _resolve_output_file(
    root: Path,
    relative: PurePosixPath,
    description: str,
) -> Path:
    root_resolved = _require_root(root, f"{description}根目录")
    current = root_resolved
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise BundleConfigurationError(
                f"{description}父路径包含符号链接：{relative}"
            )
    target = root_resolved.joinpath(*relative.parts)
    if target.exists() and target.is_symlink():
        raise BundleConfigurationError(
            f"{description}不能覆盖符号链接：{relative}"
        )
    return target


def _parse_timestamp(value: Any, description: str) -> datetime:
    if not isinstance(value, str):
        raise BundleConfigurationError(
            f"{description}必须是带时区的 RFC 3339 字符串"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BundleConfigurationError(
            f"{description}必须是带时区的 RFC 3339 字符串"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BundleConfigurationError(f"{description}必须显式包含时区")
    return parsed


def _require_command(value: Any, description: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise BundleConfigurationError(f"{description}必须是非空参数数组")
    if any(not isinstance(token, str) or not token.strip() for token in value):
        raise BundleConfigurationError(f"{description}包含空参数")
    if " ".join(value).strip().lower() in {":", "true", "/bin/true", "exit 0"}:
        raise BundleConfigurationError(f"{description}不能是空操作命令")
    return list(value)


def _source_paths(source_root: Path) -> tuple[Path, Path, Path]:
    profile = _resolve_existing_file(
        source_root,
        _relative_path(DEFAULT_PROFILE_RELATIVE_PATH, "冻结画像路径"),
        "冻结画像",
    )
    rule_manifest = _resolve_existing_file(
        source_root,
        _relative_path(RULE_MANIFEST_RELATIVE_PATH, "规则清单路径"),
        "规则清单",
    )
    checker = _resolve_existing_file(
        source_root,
        _relative_path(CHECKER_RELATIVE_PATH, "逐规则 checker 路径"),
        "逐规则 checker",
    )
    return profile, rule_manifest, checker


def _profile_and_rules(source_root: Path) -> tuple[dict[str, Any], list[str]]:
    profile_path, rule_manifest_path, _ = _source_paths(source_root)
    source_checker = source_root / CHECKER_RELATIVE_PATH
    local_checker = Path(__file__).resolve().with_name("candidate_rule_assertion.py")
    if file_sha256(source_checker) != file_sha256(local_checker):
        raise BundleConfigurationError(
            "当前生成器导入的逐规则 checker 与 source-root 快照不一致"
        )
    profile = load_profile(profile_path, rule_manifest_path)
    rule_ids = [rule["rule_id"] for rule in profile["rules"]]
    if len(rule_ids) != REQUIRED_RULE_COUNT:
        raise BundleConfigurationError(
            f"冻结画像必须恰好包含 {REQUIRED_RULE_COUNT} 条规则"
        )
    return profile, rule_ids


def _capture_manifest_relative(
    capture_manifest: Path, evidence_root: Path
) -> tuple[Path, str]:
    if capture_manifest.is_symlink() or not capture_manifest.is_file():
        raise BundleConfigurationError("capture manifest 必须是非符号链接普通文件")
    evidence_resolved = _require_root(evidence_root, "证据根目录")
    try:
        relative = capture_manifest.resolve(strict=True).relative_to(evidence_resolved)
    except (OSError, ValueError) as error:
        raise BundleConfigurationError(
            "capture manifest 必须位于 evidence-root 内"
        ) from error
    return capture_manifest.resolve(strict=True), relative.as_posix()


def _assertion_paths(
    evidence_root: Path,
    assertions_dir: str,
    rule_id: str,
) -> tuple[Path, str]:
    directory = _relative_path(assertions_dir, "assertions-dir")
    relative = directory / f"{rule_id}.result.json"
    return (
        _resolve_output_file(evidence_root, relative, "断言结果"),
        relative.as_posix(),
    )


def _assertion_index_path(evidence_root: Path, assertions_dir: str) -> Path:
    directory = _relative_path(assertions_dir, "assertions-dir")
    return _resolve_output_file(
        evidence_root,
        directory / "assertion-index.json",
        "断言索引",
    )


def run_assertions(
    *,
    source_root: Path,
    evidence_root: Path,
    capture_manifest: Path,
    assertions_dir: str = DEFAULT_ASSERTIONS_DIR,
) -> dict[str, Any]:
    """逐条运行冻结 checker，并持久化 42 条结果与绑定索引。"""

    source_resolved = _require_root(source_root, "源码根目录")
    evidence_resolved = _require_root(evidence_root, "证据根目录")
    capture_path, capture_relative = _capture_manifest_relative(
        capture_manifest, evidence_resolved
    )
    profile, rule_ids = _profile_and_rules(source_resolved)
    profile_path, rule_manifest_path, checker_path = _source_paths(source_resolved)
    del profile

    index_path = _assertion_index_path(evidence_resolved, assertions_dir)
    expected_outputs = [
        _assertion_paths(evidence_resolved, assertions_dir, rule_id)[0]
        for rule_id in rule_ids
    ]
    occupied = [path for path in [index_path, *expected_outputs] if path.exists()]
    if occupied:
        raise BundleConfigurationError(
            "断言输出已存在；为保留执行证据，请使用新的 assertions-dir："
            + ", ".join(str(path) for path in occupied[:3])
        )

    results: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        output_path, output_relative = _assertion_paths(
            evidence_resolved, assertions_dir, rule_id
        )
        command = build_assertion_command(
            rule_id=rule_id,
            capture_manifest=str(capture_path),
            evidence_root=str(evidence_resolved),
            output=str(output_path),
            profile=DEFAULT_PROFILE_RELATIVE_PATH,
            rule_manifest=RULE_MANIFEST_RELATIVE_PATH,
        )
        completed = subprocess.run(
            command,
            cwd=source_resolved,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        result_status = "missing"
        result_sha: str | None = None
        if output_path.is_file() and not output_path.is_symlink():
            result_sha = file_sha256(output_path)
            try:
                result_value = _load_json(output_path, f"{rule_id} 断言结果")
            except BundleConfigurationError:
                result_status = "invalid"
            else:
                if isinstance(result_value, dict):
                    result_status = str(result_value.get("status", "invalid"))
        results.append(
            {
                "rule_id": rule_id,
                "path": output_relative,
                "sha256": result_sha,
                "status": result_status,
                "exit_code": completed.returncode,
            }
        )

    index = {
        "schema_version": ASSERTION_INDEX_SCHEMA_VERSION,
        "codex_version": CODEX_VERSION,
        "generated_at": utc_now(),
        "capture_manifest": {
            "path": capture_relative,
            "sha256": file_sha256(capture_path),
        },
        "profile_sha256": file_sha256(profile_path),
        "rule_manifest_sha256": file_sha256(rule_manifest_path),
        "checker": {
            "path": CHECKER_RELATIVE_PATH,
            "sha256": file_sha256(checker_path),
        },
        "results": results,
    }
    secure_write_json(index_path, index)
    return index


def _rule_map(
    value: Any,
    *,
    schema_version: str,
    rule_ids: Sequence[str],
    description: str,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, dict):
        raise BundleConfigurationError(f"{description}顶层必须是对象")
    _require_exact_keys(
        value,
        required={"schema_version", "codex_version", "rules"},
        description=description,
    )
    if value.get("schema_version") != schema_version:
        raise BundleConfigurationError(f"{description} schema_version 不匹配")
    if value.get("codex_version") != CODEX_VERSION:
        raise BundleConfigurationError(f"{description} Codex 版本不匹配")
    rules = value.get("rules")
    if not isinstance(rules, list):
        raise BundleConfigurationError(f"{description} rules 必须是数组")
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(rules):
        if not isinstance(item, dict):
            raise BundleConfigurationError(f"{description} rules[{index}] 必须是对象")
        rule_id = item.get("rule_id")
        if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id):
            raise BundleConfigurationError(
                f"{description} rules[{index}].rule_id 格式非法"
            )
        if rule_id in by_id:
            raise BundleConfigurationError(f"{description}规则重复：{rule_id}")
        by_id[rule_id] = item
    expected = set(rule_ids)
    actual = set(by_id)
    if actual != expected:
        raise BundleConfigurationError(
            f"{description}必须精确覆盖 42 条规则："
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )
    return by_id


def _load_rule_metadata(
    path: Path,
    source_root: Path,
    rule_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    by_id = _rule_map(
        _load_json(path, "规则元数据"),
        schema_version=RULE_METADATA_SCHEMA_VERSION,
        rule_ids=rule_ids,
        description="规则元数据",
    )
    result: dict[str, dict[str, Any]] = {}
    for rule_id in rule_ids:
        item = by_id[rule_id]
        _require_exact_keys(
            item,
            required={"rule_id", "implementation", "trigger_command"},
            description=f"规则元数据 {rule_id}",
        )
        implementation = item.get("implementation")
        if not isinstance(implementation, dict):
            raise BundleConfigurationError(f"{rule_id} implementation 必须是对象")
        _require_exact_keys(
            implementation,
            required={"summary", "locations"},
            description=f"{rule_id} implementation",
        )
        summary = _require_text(
            implementation.get("summary"), f"{rule_id} implementation.summary"
        )
        locations = implementation.get("locations")
        if not isinstance(locations, list) or not locations:
            raise BundleConfigurationError(f"{rule_id} 至少需要一个实现位置")
        normalized_locations: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for index, location in enumerate(locations):
            if not isinstance(location, dict):
                raise BundleConfigurationError(
                    f"{rule_id} locations[{index}] 必须是对象"
                )
            _require_exact_keys(
                location,
                required={"path", "line_start", "line_end", "symbol"},
                description=f"{rule_id} locations[{index}]",
            )
            relative = _relative_path(
                location.get("path"), f"{rule_id} locations[{index}].path"
            )
            relative_text = relative.as_posix()
            if relative_text in seen_paths:
                raise BundleConfigurationError(
                    f"{rule_id} 实现位置路径重复：{relative_text}"
                )
            seen_paths.add(relative_text)
            source_path = _resolve_existing_file(
                source_root, relative, f"{rule_id} 实现位置"
            )
            line_start = location.get("line_start")
            line_end = location.get("line_end")
            symbol = _require_text(
                location.get("symbol"), f"{rule_id} locations[{index}].symbol"
            )
            if (
                not isinstance(line_start, int)
                or isinstance(line_start, bool)
                or not isinstance(line_end, int)
                or isinstance(line_end, bool)
                or line_start < 1
                or line_end < line_start
            ):
                raise BundleConfigurationError(f"{rule_id} 实现行号范围非法")
            try:
                source_lines = source_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as error:
                raise BundleConfigurationError(
                    f"{rule_id} 实现源码不是 UTF-8 文本：{error}"
                ) from error
            if line_end > len(source_lines):
                raise BundleConfigurationError(f"{rule_id} 实现行号超出文件范围")
            if symbol not in "\n".join(source_lines[line_start - 1 : line_end]):
                raise BundleConfigurationError(
                    f"{rule_id} 声明符号未出现在实现行号范围内：{symbol}"
                )
            normalized_locations.append(
                {
                    "path": relative_text,
                    "sha256": file_sha256(source_path),
                    "line_start": line_start,
                    "line_end": line_end,
                    "symbol": symbol,
                }
            )
        result[rule_id] = {
            "implementation": {
                "summary": summary,
                "locations": normalized_locations,
            },
            "trigger_command": _require_command(
                item.get("trigger_command"), f"{rule_id} trigger_command"
            ),
        }
    return result


def _copy_official_artifact(
    *,
    source: Path,
    destination: Path,
    expected_sha: str,
) -> None:
    if not SHA256_RE.fullmatch(expected_sha):
        raise BundleConfigurationError("官方证据 SHA-256 格式非法")
    actual_sha = file_sha256(source)
    if actual_sha != expected_sha:
        raise BundleConfigurationError(f"官方证据 SHA-256 不匹配：{source}")
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise BundleConfigurationError(
                f"官方证据目标必须是普通文件：{destination}"
            )
        if file_sha256(destination) != expected_sha:
            raise BundleConfigurationError(
                f"官方证据目标已存在且内容不同：{destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        if file_sha256(temporary) != expected_sha:
            raise BundleConfigurationError("官方证据归档后摘要发生变化")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_official_evidence(
    *,
    path: Path,
    official_root: Path,
    evidence_root: Path,
    bundle_prefix: str,
    rule_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    by_id = _rule_map(
        _load_json(path, "官方证据映射"),
        schema_version=OFFICIAL_MAP_SCHEMA_VERSION,
        rule_ids=rule_ids,
        description="官方证据映射",
    )
    prefix = _relative_path(bundle_prefix, "official-bundle-prefix")
    result: dict[str, dict[str, Any]] = {}
    pending_copies: dict[str, tuple[Path, Path, str]] = {}
    for rule_id in rule_ids:
        item = by_id[rule_id]
        _require_exact_keys(
            item,
            required={"rule_id", "observation", "artifacts"},
            description=f"官方证据映射 {rule_id}",
        )
        observation = _require_text(
            item.get("observation"), f"{rule_id} 官方观察"
        )
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise BundleConfigurationError(f"{rule_id} 至少需要一份官方证据")
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                raise BundleConfigurationError(
                    f"{rule_id} 官方证据 artifacts[{index}] 必须是对象"
                )
            _require_exact_keys(
                artifact,
                required={"path", "sha256", "kind"},
                description=f"{rule_id} 官方证据 artifacts[{index}]",
            )
            source_relative = _relative_path(
                artifact.get("path"), f"{rule_id} 官方证据 path"
            )
            source = _resolve_existing_file(
                official_root, source_relative, f"{rule_id} 官方证据"
            )
            expected_sha = artifact.get("sha256")
            if (
                not isinstance(expected_sha, str)
                or not SHA256_RE.fullmatch(expected_sha)
            ):
                raise BundleConfigurationError(f"{rule_id} 官方证据摘要格式非法")
            if file_sha256(source) != expected_sha:
                raise BundleConfigurationError(
                    f"官方证据 SHA-256 不匹配：{source}"
                )
            kind = artifact.get("kind")
            if kind not in OFFICIAL_EVIDENCE_KINDS:
                raise BundleConfigurationError(
                    f"{rule_id} 官方证据 kind 不受支持：{kind!r}"
                )
            destination_relative = prefix / source_relative
            destination_text = destination_relative.as_posix()
            if destination_text in seen:
                raise BundleConfigurationError(
                    f"{rule_id} 官方证据路径重复：{destination_text}"
                )
            seen.add(destination_text)
            destination = _resolve_output_file(
                evidence_root, destination_relative, f"{rule_id} 官方证据归档"
            )
            previous = pending_copies.get(destination_text)
            if previous is not None and previous[2] != expected_sha:
                raise BundleConfigurationError(
                    f"不同官方证据映射到同一归档路径：{destination_text}"
                )
            pending_copies[destination_text] = (
                source,
                destination,
                expected_sha,
            )
            normalized.append(
                {
                    "path": destination_text,
                    "sha256": expected_sha,
                    "kind": kind,
                }
            )
        result[rule_id] = {
            "observation": observation,
            "artifacts": normalized,
        }
    # 只有完整 42 条映射及全部输入摘要通过后才开始归档，避免留下半套官方证据。
    for source, destination, expected_sha in pending_copies.values():
        _copy_official_artifact(
            source=source,
            destination=destination,
            expected_sha=expected_sha,
        )
    return result


def _load_candidate_identity(path: Path) -> dict[str, str]:
    value = _load_json(path, "candidate identity")
    if not isinstance(value, dict):
        raise BundleConfigurationError("candidate identity 顶层必须是对象")
    required = {
        "git_commit",
        "source_tree_sha256",
        "image_reference",
        "image_digest",
        "deployed_version",
    }
    _require_exact_keys(
        value,
        required=required,
        description="candidate identity",
    )
    if not isinstance(value.get("git_commit"), str) or not GIT_COMMIT_RE.fullmatch(
        value["git_commit"]
    ):
        raise BundleConfigurationError("candidate identity git_commit 格式非法")
    if not isinstance(value.get("source_tree_sha256"), str) or not SHA256_RE.fullmatch(
        value["source_tree_sha256"]
    ):
        raise BundleConfigurationError(
            "candidate identity source_tree_sha256 格式非法"
        )
    if not isinstance(value.get("image_digest"), str) or not IMAGE_DIGEST_RE.fullmatch(
        value["image_digest"]
    ):
        raise BundleConfigurationError("candidate identity image_digest 格式非法")
    for field in ("image_reference", "deployed_version"):
        _require_text(value.get(field), f"candidate identity {field}")
    return {key: str(value[key]) for key in required}


def _load_assertion_index(
    *,
    source_root: Path,
    evidence_root: Path,
    capture_manifest: Path,
    assertions_dir: str,
    rule_ids: Sequence[str],
    profile: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Mapping[str, Any]]]:
    index_path = _assertion_index_path(evidence_root, assertions_dir)
    index = _load_json(index_path, "断言索引")
    if not isinstance(index, dict):
        raise BundleConfigurationError("断言索引顶层必须是对象")
    _require_exact_keys(
        index,
        required={
            "schema_version",
            "codex_version",
            "generated_at",
            "capture_manifest",
            "profile_sha256",
            "rule_manifest_sha256",
            "checker",
            "results",
        },
        description="断言索引",
    )
    if index.get("schema_version") != ASSERTION_INDEX_SCHEMA_VERSION:
        raise BundleConfigurationError("断言索引 schema_version 不匹配")
    if index.get("codex_version") != CODEX_VERSION:
        raise BundleConfigurationError("断言索引 Codex 版本不匹配")
    _parse_timestamp(index.get("generated_at"), "断言索引 generated_at")
    profile_path, rule_manifest_path, checker_path = _source_paths(source_root)
    if index.get("profile_sha256") != file_sha256(profile_path):
        raise BundleConfigurationError("断言索引绑定的冻结画像摘要不匹配")
    if index.get("rule_manifest_sha256") != file_sha256(rule_manifest_path):
        raise BundleConfigurationError("断言索引绑定的规则清单摘要不匹配")
    checker = index.get("checker")
    if not isinstance(checker, dict) or checker != {
        "path": CHECKER_RELATIVE_PATH,
        "sha256": file_sha256(checker_path),
    }:
        raise BundleConfigurationError("断言索引绑定的 checker 不匹配")
    capture_path, capture_relative = _capture_manifest_relative(
        capture_manifest, evidence_root
    )
    capture_ref = index.get("capture_manifest")
    if not isinstance(capture_ref, dict) or capture_ref != {
        "path": capture_relative,
        "sha256": file_sha256(capture_path),
    }:
        raise BundleConfigurationError("断言索引绑定的 capture manifest 不匹配")

    raw_results = index.get("results")
    if not isinstance(raw_results, list):
        raise BundleConfigurationError("断言索引 results 必须是数组")
    entries: dict[str, Mapping[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            raise BundleConfigurationError("断言索引结果条目必须是对象")
        _require_exact_keys(
            item,
            required={"rule_id", "path", "sha256", "status", "exit_code"},
            description="断言索引结果",
        )
        rule_id = item.get("rule_id")
        if not isinstance(rule_id, str) or rule_id in entries:
            raise BundleConfigurationError("断言索引包含非法或重复 rule_id")
        entries[rule_id] = item
    if set(entries) != set(rule_ids):
        raise BundleConfigurationError("断言索引必须精确覆盖 42 条规则")

    manifest_value = _load_json(capture_path, "capture manifest")
    artifacts = manifest_value.get("artifacts") if isinstance(manifest_value, dict) else None
    if not isinstance(artifacts, list):
        raise BundleConfigurationError("capture manifest artifacts 缺失")
    manifest_artifacts = {
        item.get("path"): item
        for item in artifacts
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    parsed_manifest, observations = load_observations(
        capture_path, evidence_root
    )

    loaded: dict[str, dict[str, Any]] = {}
    for rule_id in rule_ids:
        entry = entries[rule_id]
        expected_path, expected_relative = _assertion_paths(
            evidence_root, assertions_dir, rule_id
        )
        if entry.get("path") != expected_relative:
            raise BundleConfigurationError(f"{rule_id} 断言结果路径不匹配")
        result = _load_json(expected_path, f"{rule_id} 断言结果")
        if not isinstance(result, dict):
            raise BundleConfigurationError(f"{rule_id} 断言结果必须是对象")
        if entry.get("sha256") != file_sha256(expected_path):
            raise BundleConfigurationError(f"{rule_id} 断言结果摘要不匹配")
        if entry.get("status") != "pass" or entry.get("exit_code") != 0:
            raise BundleConfigurationError(f"{rule_id} 断言索引未记录通过")
        required_result_keys = {
            "schema_version",
            "rule_id",
            "status",
            "started_at",
            "finished_at",
            "exit_code",
            "checker_sha256",
            "command_sha256",
            "checks",
        }
        _require_exact_keys(
            result,
            required=required_result_keys,
            description=f"{rule_id} 断言结果",
        )
        if (
            result.get("schema_version") != ASSERTION_SCHEMA_VERSION
            or result.get("rule_id") != rule_id
            or result.get("status") != "pass"
            or result.get("exit_code") != 0
        ):
            raise BundleConfigurationError(f"{rule_id} 断言结果未通过")
        if result.get("checker_sha256") != file_sha256(checker_path):
            raise BundleConfigurationError(f"{rule_id} checker 摘要不匹配")
        _parse_timestamp(result.get("started_at"), f"{rule_id} started_at")
        _parse_timestamp(result.get("finished_at"), f"{rule_id} finished_at")
        command = build_assertion_command(
            rule_id=rule_id,
            capture_manifest=str(capture_path),
            evidence_root=str(evidence_root.resolve(strict=True)),
            output=str(expected_path),
            profile=DEFAULT_PROFILE_RELATIVE_PATH,
            rule_manifest=RULE_MANIFEST_RELATIVE_PATH,
        )
        if result.get("command_sha256") != command_sha256(command):
            raise BundleConfigurationError(f"{rule_id} 断言命令摘要不匹配")
        checks = result.get("checks")
        if not isinstance(checks, list) or not checks:
            raise BundleConfigurationError(f"{rule_id} 缺少语义检查")
        evidence_paths: set[str] = set()
        for check in checks:
            if not isinstance(check, dict) or check.get("passed") is not True:
                raise BundleConfigurationError(f"{rule_id} 包含未通过的语义检查")
            paths = check.get("evidence_paths")
            if not isinstance(paths, list) or not paths:
                raise BundleConfigurationError(f"{rule_id} 语义检查未绑定证据")
            for raw_path in paths:
                if not isinstance(raw_path, str) or raw_path not in manifest_artifacts:
                    raise BundleConfigurationError(
                        f"{rule_id} 引用了 manifest 外候选证据：{raw_path!r}"
                    )
                evidence_paths.add(raw_path)
        recomputed_checks = evaluate_rule(
            profile, rule_id, observations, parsed_manifest
        )
        if checks != recomputed_checks:
            raise BundleConfigurationError(
                f"{rule_id} 已归档 checks 与冻结画像现场复算结果不一致"
            )
        loaded[rule_id] = {
            "value": result,
            "path": expected_relative,
            "sha256": file_sha256(expected_path),
            "command": command,
            "evidence_paths": sorted(evidence_paths),
        }
    return loaded, manifest_artifacts


def _state_reference(
    *,
    path: Path,
    evidence_root: Path,
    captured_at: str,
    description: str,
) -> tuple[dict[str, str], datetime]:
    evidence_resolved = _require_root(evidence_root, "证据根目录")
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise BundleConfigurationError(f"{description}必须是非空普通文件")
    try:
        relative = path.resolve(strict=True).relative_to(evidence_resolved)
    except (OSError, ValueError) as error:
        raise BundleConfigurationError(f"{description}必须位于 evidence-root 内") from error
    parsed = _parse_timestamp(captured_at, f"{description} captured_at")
    return (
        {
            "path": relative.as_posix(),
            "sha256": file_sha256(path),
            "kind": "normalized_state",
            "captured_at": captured_at,
        },
        parsed,
    )


def _candidate_evidence_group(
    *,
    rule_id: str,
    rule_description: str,
    assertion: Mapping[str, Any],
    manifest_artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    artifacts: list[dict[str, str]] = []
    for path in assertion["evidence_paths"]:
        manifest_artifact = manifest_artifacts[path]
        kind = manifest_artifact.get("kind")
        sha256 = manifest_artifact.get("sha256")
        if not isinstance(kind, str) or not isinstance(sha256, str):
            raise BundleConfigurationError(f"{rule_id} manifest artifact 字段非法")
        artifacts.append({"path": path, "sha256": sha256, "kind": kind})
    return {
        "observation": f"冻结画像逐规则断言已通过：{rule_description}",
        "artifacts": artifacts,
    }


def finalize_bundle(
    *,
    source_root: Path,
    evidence_root: Path,
    capture_manifest: Path,
    assertions_dir: str,
    rule_metadata_path: Path,
    official_map_path: Path,
    official_root: Path,
    official_bundle_prefix: str,
    candidate_identity_path: Path,
    before_state: Path,
    before_captured_at: str,
    after_state: Path,
    after_captured_at: str,
    assessment_id: str,
    restoration_name: str,
    restoration_description: str,
    submission_path: str = DEFAULT_SUBMISSION_PATH,
    report_path: str = DEFAULT_REPORT_PATH,
    guard_report_path: str = DEFAULT_GUARD_REPORT_PATH,
    secret_env_names: Sequence[str] = (),
) -> dict[str, Any]:
    """消费真实归档，生成 submission，并调用最终 42 条门禁。"""

    source_resolved = _require_root(source_root, "源码根目录")
    evidence_resolved = _require_root(evidence_root, "证据根目录")
    submission_output = _resolve_output_file(
        evidence_resolved,
        _relative_path(submission_path, "submission-path"),
        "正式验收提交",
    )
    report_output = _resolve_output_file(
        evidence_resolved,
        _relative_path(report_path, "report-path"),
        "最终验收报告",
    )
    guard_output = _resolve_output_file(
        evidence_resolved,
        _relative_path(guard_report_path, "guard-report-path"),
        "证据守卫报告",
    )
    occupied_outputs = [
        path
        for path in (submission_output, report_output, guard_output)
        if path.exists()
    ]
    if occupied_outputs:
        raise BundleConfigurationError(
            "正式组包输出已存在；为保留验收历史，请改用新的输出路径："
            + ", ".join(str(path) for path in occupied_outputs)
        )
    profile, rule_ids = _profile_and_rules(source_resolved)
    profile_rules = {item["rule_id"]: item for item in profile["rules"]}
    scenario_profiles = {
        item["scenario_id"]: item for item in profile["scenarios"]
    }
    assertions, manifest_artifacts = _load_assertion_index(
        source_root=source_resolved,
        evidence_root=evidence_resolved,
        capture_manifest=capture_manifest,
        assertions_dir=assertions_dir,
        rule_ids=rule_ids,
        profile=profile,
    )
    metadata = _load_rule_metadata(
        rule_metadata_path, source_resolved, rule_ids
    )
    candidate_identity = _load_candidate_identity(candidate_identity_path)

    verify_restoration(before_state, after_state)
    before_ref, before_time = _state_reference(
        path=before_state,
        evidence_root=evidence_resolved,
        captured_at=before_captured_at,
        description="before 状态",
    )
    after_ref, after_time = _state_reference(
        path=after_state,
        evidence_root=evidence_resolved,
        captured_at=after_captured_at,
        description="after 状态",
    )
    started_times = [
        _parse_timestamp(item["value"]["started_at"], f"{rule_id} started_at")
        for rule_id, item in assertions.items()
    ]
    finished_times = [
        _parse_timestamp(item["value"]["finished_at"], f"{rule_id} finished_at")
        for rule_id, item in assertions.items()
    ]
    if before_time > min(started_times):
        raise BundleConfigurationError("before 状态必须早于全部逐规则断言")
    if after_time < max(finished_times):
        raise BundleConfigurationError(
            "after 状态必须在全部逐规则断言完成后真实采集；"
            "请先执行 assert 阶段，再采集恢复状态，最后执行 finalize"
        )

    guard_report = verify_evidence_guard(
        before=before_state,
        after=after_state,
        capture_manifest=capture_manifest,
        evidence_root=evidence_resolved,
        secret_env_names=secret_env_names,
    )
    secure_write_json(guard_output, guard_report)
    if guard_report.get("status") != "pass":
        raise BundleConfigurationError("证据守卫未通过，拒绝生成正式验收提交")

    official = _load_official_evidence(
        path=official_map_path,
        official_root=official_root,
        evidence_root=evidence_resolved,
        bundle_prefix=official_bundle_prefix,
        rule_ids=rule_ids,
    )

    _require_text(assessment_id, "assessment-id")
    _require_text(restoration_name, "restoration-name")
    _require_text(restoration_description, "restoration-description")
    _, rule_manifest_path, checker_path = _source_paths(source_resolved)
    rules: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        rule_profile = profile_rules[rule_id]
        scenarios = [
            scenario_profiles[scenario_id]
            for scenario_id in rule_profile["scenario_ids"]
        ]
        trigger_description = "；".join(
            f"{item['scenario_id']}：{item['trigger']}" for item in scenarios
        )
        preconditions = list(
            dict.fromkeys(
                condition
                for scenario in scenarios
                for condition in scenario["preconditions"]
            )
        )
        assertion = assertions[rule_id]
        rules.append(
            {
                "rule_id": rule_id,
                "implementation": metadata[rule_id]["implementation"],
                "trigger": {
                    "description": trigger_description,
                    "preconditions": preconditions,
                    "command": metadata[rule_id]["trigger_command"],
                    "expected_observation": rule_profile["description"],
                },
                "official_evidence": official[rule_id],
                "candidate_raw_evidence": _candidate_evidence_group(
                    rule_id=rule_id,
                    rule_description=rule_profile["description"],
                    assertion=assertion,
                    manifest_artifacts=manifest_artifacts,
                ),
                "assertion": {
                    "checker": {
                        "path": CHECKER_RELATIVE_PATH,
                        "sha256": file_sha256(checker_path),
                    },
                    "command": assertion["command"],
                    "result": {
                        "path": assertion["path"],
                        "sha256": assertion["sha256"],
                        "kind": "assertion_result",
                    },
                },
                "environment_restoration": {
                    "description": restoration_description,
                    "state_pairs": [
                        {
                            "name": restoration_name,
                            "before": dict(before_ref),
                            "after": dict(after_ref),
                        }
                    ],
                },
            }
        )

    submission = {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "codex_version": CODEX_VERSION,
        "assessment_id": assessment_id,
        "generated_at": utc_now(),
        "rule_manifest_sha256": file_sha256(rule_manifest_path),
        "candidate_identity": candidate_identity,
        "rules": rules,
    }
    secure_write_json(submission_output, submission)

    command = [
        "python3",
        FINAL_CHECKER_RELATIVE_PATH,
        "--submission",
        str(submission_output),
        "--manifest",
        str(rule_manifest_path),
        "--source-root",
        str(source_resolved),
        "--evidence-root",
        str(evidence_resolved),
        "--report",
        str(report_output),
    ]
    completed = subprocess.run(
        command,
        cwd=source_resolved,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if not report_output.is_file() or report_output.is_symlink():
        raise BundleConfigurationError(
            "最终门禁未生成结构化报告：" + completed.stderr.strip()
        )
    report = _load_json(report_output, "最终验收报告")
    if not isinstance(report, dict):
        raise BundleConfigurationError("最终验收报告必须是对象")
    if completed.returncode != 0 or report.get("accepted") is not True:
        raise BundleConfigurationError(
            "最终 42 条门禁未通过；请查看报告：" + str(report_output)
        )
    return {
        "submission": str(submission_output),
        "report": str(report_output),
        "guard_report": str(guard_output),
        "accepted": True,
        "required_rule_count": report.get("required_rule_count"),
        "submitted_rule_count": report.get("submitted_rule_count"),
        "error_count": report.get("error_count"),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "生成 Codex CLI 0.145.0 候选侧正式 42 条验收包；"
            "assert 与 finalize 必须按顺序执行。"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    assert_parser = subparsers.add_parser(
        "assert", help="逐条执行冻结断言并生成 42 个结果"
    )
    assert_parser.add_argument("--source-root", type=Path, required=True)
    assert_parser.add_argument("--evidence-root", type=Path, required=True)
    assert_parser.add_argument("--capture-manifest", type=Path, required=True)
    assert_parser.add_argument(
        "--assertions-dir", default=DEFAULT_ASSERTIONS_DIR
    )

    finalize_parser = subparsers.add_parser(
        "finalize", help="消费既有断言和恢复状态并生成最终验收包"
    )
    finalize_parser.add_argument("--source-root", type=Path, required=True)
    finalize_parser.add_argument("--evidence-root", type=Path, required=True)
    finalize_parser.add_argument("--capture-manifest", type=Path, required=True)
    finalize_parser.add_argument(
        "--assertions-dir", default=DEFAULT_ASSERTIONS_DIR
    )
    finalize_parser.add_argument("--rule-metadata", type=Path, required=True)
    finalize_parser.add_argument("--official-evidence-map", type=Path, required=True)
    finalize_parser.add_argument("--official-evidence-root", type=Path, required=True)
    finalize_parser.add_argument(
        "--official-bundle-prefix", default="official"
    )
    finalize_parser.add_argument("--candidate-identity", type=Path, required=True)
    finalize_parser.add_argument("--before-state", type=Path, required=True)
    finalize_parser.add_argument("--before-captured-at", required=True)
    finalize_parser.add_argument("--after-state", type=Path, required=True)
    finalize_parser.add_argument("--after-captured-at", required=True)
    finalize_parser.add_argument("--assessment-id", required=True)
    finalize_parser.add_argument(
        "--restoration-name", default="production-normalized-state"
    )
    finalize_parser.add_argument(
        "--restoration-description",
        default="代理、CA、hosts、keeper、账号调度和本轮临时状态均已恢复",
    )
    finalize_parser.add_argument(
        "--submission-path", default=DEFAULT_SUBMISSION_PATH
    )
    finalize_parser.add_argument("--report-path", default=DEFAULT_REPORT_PATH)
    finalize_parser.add_argument(
        "--guard-report-path", default=DEFAULT_GUARD_REPORT_PATH
    )
    finalize_parser.add_argument("--secret-env", action="append", default=[])
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "assert":
            result = run_assertions(
                source_root=args.source_root,
                evidence_root=args.evidence_root,
                capture_manifest=args.capture_manifest,
                assertions_dir=args.assertions_dir,
            )
            passed = sum(
                item.get("status") == "pass" and item.get("exit_code") == 0
                for item in result["results"]
            )
            payload = {
                "phase": "assert",
                "passed_rule_count": passed,
                "required_rule_count": REQUIRED_RULE_COUNT,
                "assertion_index": str(
                    _assertion_index_path(args.evidence_root, args.assertions_dir)
                ),
            }
            exit_code = 0 if passed == REQUIRED_RULE_COUNT else 1
        else:
            payload = finalize_bundle(
                source_root=args.source_root,
                evidence_root=args.evidence_root,
                capture_manifest=args.capture_manifest,
                assertions_dir=args.assertions_dir,
                rule_metadata_path=args.rule_metadata,
                official_map_path=args.official_evidence_map,
                official_root=args.official_evidence_root,
                official_bundle_prefix=args.official_bundle_prefix,
                candidate_identity_path=args.candidate_identity,
                before_state=args.before_state,
                before_captured_at=args.before_captured_at,
                after_state=args.after_state,
                after_captured_at=args.after_captured_at,
                assessment_id=args.assessment_id,
                restoration_name=args.restoration_name,
                restoration_description=args.restoration_description,
                submission_path=args.submission_path,
                report_path=args.report_path,
                guard_report_path=args.guard_report_path,
                secret_env_names=args.secret_env,
            )
            payload = {"phase": "finalize", **payload}
            exit_code = 0
    except (BundleConfigurationError, OSError, ValueError) as error:
        payload = {
            "phase": args.command,
            "accepted": False,
            "error": str(error),
        }
        exit_code = 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
