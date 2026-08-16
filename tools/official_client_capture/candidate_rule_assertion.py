#!/usr/bin/env python3
"""基于冻结画像对候选抓包执行逐规则独立断言。

本工具只读取证据归档，不导入候选 Go 画像。冻结预期位于
``candidate_rule_expectations_0_145_0.json``，抓包事实来自统一 manifest 所引用的
pcap、relay 原始字节或结构化 trace。成功输出由统一升级编排器复算并纳入逐规则
正式验收。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit

if __package__ in {None, ""}:
    # 支持最终 submission 要求的显式 checker 文件命令：
    # python3 tools/official_client_capture/candidate_rule_assertion.py ...
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.pcap_clienthello import (
    iter_packets,
    parse_client_hello,
    tcp_payload,
)
from tools.official_client_capture.relay_extract import (
    parse_h1_stream,
    parse_ws_frames,
)
from tools.official_client_capture.acceptance_contract import (
    check_applies_to_side,
    derive_side_restricted_checks,
)


PROFILE_SCHEMA_VERSION = "codex-candidate-rule-expectations/v1"
CAPTURE_MANIFEST_SCHEMA_VERSION = "codex-candidate-capture-manifest/v1"
OBSERVATION_SCHEMA_VERSION = "codex-candidate-observation/v1"
ASSERTION_SCHEMA_VERSION = "codex-candidate-rule-assertion/v1"
CODEX_VERSION = "0.145.0"
CHECKER_RELATIVE_PATH = (
    "tools/official_client_capture/candidate_rule_assertion.py"
)
DEFAULT_PROFILE_RELATIVE_PATH = (
    "tools/official_client_capture/candidate_rule_expectations_0_145_0.json"
)
# 2026-08-11（R8）：合并 17 项 selector 修正、双轨 track selector、A04 压缩
# 分流与 Wham 原始字节 selector；逐项依据由主手册第二部分规则和批准画像承载。
# 同日补一项：BODY-006/nonlite-* 两条补 method=POST 与 responses 路径约束。原先
# 只按 A04＋mode=non_lite 选，会把 residency-us／runtime-metrics 里的启动 models
# GET 一并选中，断言 body 字段必然失败；R8 补出非 Lite 的 HTTP POST 样本后，这两条
# 约束不再造成「选不到」。验收契约载荷逐字未变（25／17 分组、42 条 validation_modes
# 与 expected_check_ids 全部不变），故 acceptance_contract 的冻结摘要不随之漂移。
# 2026-08-15 主文档 active 规则升级为 0.147；本文件继续保留 0.145 历史断言载荷，
# 仅重绑 source_spec 章节摘要供基线复算使用。
# 2026-08-16 主手册更新 §3.5 的 v0.1.177 台账路径后再次重绑第二部分摘要；
# 规则与判据载荷不变。
FROZEN_PROFILE_SHA256 = (
    "9e6220c90607b71ff3468ed3f8b904916cfc1a229a4cf2b7976d4dfac68ba685"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RULE_ID_RE = re.compile(r"^SPEC-[A-Z0-9]+-[0-9]{3}$")

STRUCTURED_TRACE_KINDS = frozenset(
    {
        "application_log",
        "database_trace",
        "filesystem_snapshot",
        "http_trace",
        "mitm_jsonl",
        "process_trace",
        "stderr_log",
        "stdout_log",
        "websocket_trace",
        "wire_dump",
    }
)
ALLOWED_PARSERS_BY_KIND = {
    "pcap": frozenset({"pcap_client_hello"}),
    "pcapng": frozenset({"opaque_bound_source"}),
    "relay_binary": frozenset({"h1_request_stream", "opaque_bound_source"}),
    "tls_keylog": frozenset({"opaque_bound_source"}),
    "wire_dump": frozenset(
        {
            "h1_request_stream",
            "observation_json",
            "observation_jsonl",
            "opaque_bound_source",
        }
    ),
    **{
        kind: (
            frozenset(
                {
                    "observation_json",
                    "observation_jsonl",
                    "opaque_bound_source",
                }
            )
            if kind in {"stdout_log", "stderr_log"}
            else frozenset({"observation_json", "observation_jsonl"})
        )
        for kind in STRUCTURED_TRACE_KINDS
        if kind != "wire_dump"
    },
}
ALLOWED_ASSERTION_OPERATORS = frozenset(
    {
        "all_absent",
        "all_contains",
        "all_equal",
        "all_fields_equal",
        "all_list_all_different",
        "all_list_all_same",
        "all_list_prefix",
        "all_list_suffix",
        "all_lowercase",
        "all_match",
        "all_not_contains",
        "all_ordered_subset_of",
        "all_subset",
        "any_equal",
        "any_subset",
        "count_at_least",
        "count_equal",
        "distinct_count_at_least",
        "distinct_values_equal",
        "none_subset",
        "same_set_distinct_order",
    }
)
MISSING = object()


class AssertionConfigurationError(ValueError):
    """画像、manifest 或证据归档不满足失败关闭约束。"""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝 JSON 重复键，避免后一个值静默覆盖证据事实。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssertionConfigurationError(f"JSON 对象包含重复字段：{key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class Observation:
    """一条由独立解析器从候选原始证据得到的事实。"""

    record_id: str
    scenario_id: str
    record_type: str
    artifact_path: str
    evidence_paths: tuple[str, ...]
    labels: Mapping[str, Any]
    data: Mapping[str, Any]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "scenario_id": self.scenario_id,
            "record_type": self.record_type,
            "artifact_path": self.artifact_path,
            "labels": dict(self.labels),
            "data": dict(self.data),
        }


def file_sha256(path: Path) -> str:
    """流式计算普通文件的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_sha256(command: Sequence[str]) -> str:
    """按最终门禁规定的紧凑 JSON 算法计算命令摘要。"""

    payload = json.dumps(
        list(command), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def utc_now() -> str:
    """返回带时区的 UTC RFC 3339 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _load_json(path: Path, description: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise AssertionConfigurationError(
            f"{description}必须是非符号链接普通文件：{path}"
        )
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AssertionConfigurationError(
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
        raise AssertionConfigurationError(
            f"{description}字段不闭合：missing={missing} extra={extra}"
        )


def _relative_path(value: Any, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise AssertionConfigurationError(f"{description}必须是 POSIX 相对路径")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value in {".", ".."}
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise AssertionConfigurationError(f"{description}不能逃逸证据根目录：{value}")
    return path


def _resolve_evidence_file(
    evidence_root: Path,
    relative: PurePosixPath,
    description: str,
) -> Path:
    root = evidence_root.resolve(strict=True)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AssertionConfigurationError(
                f"{description}路径包含符号链接：{relative}"
            )
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise AssertionConfigurationError(
            f"{description}不存在或逃逸证据根目录：{relative}"
        ) from error
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise AssertionConfigurationError(f"{description}必须是非空普通文件：{relative}")
    return resolved


def load_profile(
    profile_path: Path,
    rule_manifest_path: Path | None = None,
    *,
    verify_frozen_digest: bool = True,
    expected_codex_version: str = CODEX_VERSION,
    expected_profile_sha256: str | None = None,
) -> dict[str, Any]:
    """加载版本化断言画像，并可选核对规则全集与批准摘要。"""

    profile = _load_json(profile_path, "冻结规则画像")
    if expected_profile_sha256 is not None:
        if not SHA256_RE.fullmatch(expected_profile_sha256):
            raise AssertionConfigurationError("批准规则画像 SHA-256 格式非法")
        if file_sha256(profile_path) != expected_profile_sha256:
            raise AssertionConfigurationError("批准规则画像 SHA-256 不匹配")
    if verify_frozen_digest and file_sha256(profile_path) != FROZEN_PROFILE_SHA256:
        raise AssertionConfigurationError(
            "冻结规则画像 SHA-256 不匹配；修改预期必须经过独立审核，"
            "并同步更新 checker 固定摘要"
        )
    if not isinstance(profile, dict):
        raise AssertionConfigurationError("冻结规则画像顶层必须是对象")
    _require_exact_keys(
        profile,
        required={
            "schema_version",
            "codex_version",
            "source_spec",
            "source_spec_sha256",
            "scenarios",
            "rules",
        },
        optional={"status"},
        description="冻结规则画像",
    )
    # classify 批准后的权威画像会在原始断言载荷外补充批准态。compare/accept
    # 明确绑定这份批准文件及其完整摘要，因此 checker 必须接受该元数据；但只
    # 允许 approved，避免 draft、blocked 等中间态混入正式验收。
    if "status" in profile and profile["status"] != "approved":
        raise AssertionConfigurationError(
            "冻结规则画像 status 存在时必须为 approved"
        )
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise AssertionConfigurationError("冻结规则画像 schema_version 不匹配")
    if profile.get("codex_version") != expected_codex_version:
        raise AssertionConfigurationError("冻结规则画像 Codex 版本不匹配")
    source_spec_sha256 = profile.get("source_spec_sha256")
    if not isinstance(source_spec_sha256, str) or not SHA256_RE.fullmatch(
        source_spec_sha256
    ):
        raise AssertionConfigurationError("冻结规则画像 source_spec_sha256 格式非法")
    if verify_frozen_digest:
        source_spec = profile.get("source_spec")
        if not isinstance(source_spec, str):
            raise AssertionConfigurationError("冻结规则画像 source_spec 格式非法")
        source_path_text, _, source_fragment = source_spec.partition("#")
        source_relative = _relative_path(
            source_path_text, "冻结规则画像 source_spec"
        )
        try:
            repository_root = profile_path.resolve(strict=True).parent.parents[1]
            source_path = _resolve_evidence_file(
                repository_root,
                source_relative,
                "冻结规则画像来源规格",
            )
        except (IndexError, OSError) as error:
            raise AssertionConfigurationError(
                "无法从冻结画像位置解析来源规格"
            ) from error
        if source_spec_section_sha256(source_path, source_fragment) != source_spec_sha256:
            raise AssertionConfigurationError(
                "来源规格 SHA-256 与冻结画像声明不一致"
            )

    scenarios = profile.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise AssertionConfigurationError("冻结规则画像 scenarios 必须是非空数组")
    scenario_ids: list[str] = []
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise AssertionConfigurationError(f"scenarios[{index}] 必须是对象")
        _require_exact_keys(
            scenario,
            required={
                "scenario_id",
                "description",
                "trigger",
                "preconditions",
                "required_artifact_kinds",
            },
            description=f"scenarios[{index}]",
        )
        scenario_id = scenario.get("scenario_id")
        if not isinstance(scenario_id, str) or not re.fullmatch(r"A\d{2}", scenario_id):
            raise AssertionConfigurationError(
                f"scenarios[{index}].scenario_id 格式非法"
            )
        scenario_ids.append(scenario_id)
        for text_field in ("description", "trigger"):
            if not isinstance(scenario.get(text_field), str) or not scenario[
                text_field
            ].strip():
                raise AssertionConfigurationError(
                    f"scenarios[{index}].{text_field} 不能为空"
                )
        for list_field in ("preconditions", "required_artifact_kinds"):
            values = scenario.get(list_field)
            if not isinstance(values, list) or not values or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise AssertionConfigurationError(
                    f"scenarios[{index}].{list_field} 必须是非空字符串数组"
                )
    if len(set(scenario_ids)) != len(scenario_ids):
        raise AssertionConfigurationError("冻结规则画像存在重复 scenario_id")

    rules = profile.get("rules")
    if not isinstance(rules, list) or not rules:
        raise AssertionConfigurationError("冻结规则画像 rules 必须是非空数组")
    rule_ids: list[str] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise AssertionConfigurationError(f"rules[{index}] 必须是对象")
        _require_exact_keys(
            rule,
            required={"rule_id", "scenario_ids", "description", "checks"},
            description=f"rules[{index}]",
        )
        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id):
            raise AssertionConfigurationError(f"rules[{index}].rule_id 格式非法")
        rule_ids.append(rule_id)
        selected_scenarios = rule.get("scenario_ids")
        if not isinstance(selected_scenarios, list) or not selected_scenarios:
            raise AssertionConfigurationError(
                f"rules[{index}].scenario_ids 必须是非空数组"
            )
        unknown_scenarios = sorted(set(selected_scenarios) - set(scenario_ids))
        if unknown_scenarios:
            raise AssertionConfigurationError(
                f"rules[{index}] 引用了未知场景：{unknown_scenarios}"
            )
        checks = rule.get("checks")
        if not isinstance(checks, list) or not checks:
            raise AssertionConfigurationError(f"rules[{index}].checks 不能为空")
        check_ids: list[str] = []
        for check_index, check in enumerate(checks):
            if not isinstance(check, dict):
                raise AssertionConfigurationError(
                    f"rules[{index}].checks[{check_index}] 必须是对象"
                )
            _require_exact_keys(
                check,
                required={"id", "description", "select", "assertion"},
                description=f"rules[{index}].checks[{check_index}]",
            )
            check_id = check.get("id")
            if not isinstance(check_id, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9._-]*", check_id
            ):
                raise AssertionConfigurationError(
                    f"rules[{index}].checks[{check_index}].id 格式非法"
                )
            check_ids.append(check_id)
            selector = check.get("select")
            if not isinstance(selector, dict):
                raise AssertionConfigurationError("检查 select 必须是对象")
            _require_exact_keys(
                selector,
                required={"record_type"},
                optional={"where", "scenario_ids"},
                description=f"rules[{index}].checks[{check_index}].select",
            )
            assertion = check.get("assertion")
            if not isinstance(assertion, dict):
                raise AssertionConfigurationError("检查 assertion 必须是对象")
            operator = assertion.get("operator")
            if operator not in ALLOWED_ASSERTION_OPERATORS:
                raise AssertionConfigurationError(
                    f"不支持的断言 operator：{operator!r}"
                )
        if len(set(check_ids)) != len(check_ids):
            raise AssertionConfigurationError(f"{rule_id} 存在重复检查 ID")
    if len(set(rule_ids)) != len(rule_ids):
        raise AssertionConfigurationError("冻结规则画像存在重复 rule_id")

    if rule_manifest_path is not None:
        manifest = _load_json(rule_manifest_path, "目标版本规则清单")
        expected_rule_ids = manifest.get("required_rules") if isinstance(manifest, dict) else None
        if not isinstance(expected_rule_ids, list) or rule_ids != expected_rule_ids:
            raise AssertionConfigurationError(
                "冻结规则画像必须按相同顺序精确覆盖目标版本规则全集"
            )
    return profile


def source_spec_section_sha256(source_path: Path, fragment: str) -> str:
    """按 source_spec 的章节锚点计算稳定摘要，避免无关章节改动击穿冻结画像。"""

    if not fragment:
        return file_sha256(source_path)
    headings = {
        "第二章": "# 第二部分 Codex CLI 客户端规则画像",
        "第二部分": "# 第二部分 Codex CLI 客户端规则画像",
        "第二部分-规则": "# 第二部分 Codex CLI 客户端规则画像",
    }
    expected_heading = headings.get(fragment)
    if expected_heading is None:
        raise AssertionConfigurationError(
            f"冻结规则画像 source_spec 章节锚点不受支持：{fragment}"
        )
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as error:
        raise AssertionConfigurationError(f"无法读取来源规格章节：{error}") from error
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip("\r\n") == expected_heading),
        None,
    )
    if start is None:
        raise AssertionConfigurationError(f"来源规格缺少章节：{expected_heading}")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("# ")
        ),
        len(lines),
    )
    section = "".join(lines[start:end]).encode("utf-8")
    return hashlib.sha256(section).hexdigest()


def _validate_capture_manifest(
    value: Any,
    expected_codex_version: str = CODEX_VERSION,
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise AssertionConfigurationError("capture manifest 顶层必须是对象")
    _require_exact_keys(
        value,
        required={
            "schema_version",
            "codex_version",
            "capture_id",
            "status",
            "artifacts",
        },
        description="capture manifest",
    )
    if value.get("schema_version") != CAPTURE_MANIFEST_SCHEMA_VERSION:
        raise AssertionConfigurationError("capture manifest schema_version 不匹配")
    if value.get("codex_version") != expected_codex_version:
        raise AssertionConfigurationError("capture manifest Codex 版本不匹配")
    if value.get("status") != "complete":
        raise AssertionConfigurationError("capture manifest status 必须是 complete")
    if not isinstance(value.get("capture_id"), str) or not value["capture_id"].strip():
        raise AssertionConfigurationError("capture manifest capture_id 不能为空")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise AssertionConfigurationError("capture manifest artifacts 必须是非空数组")
    paths: list[str] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise AssertionConfigurationError(f"artifacts[{index}] 必须是对象")
        _require_exact_keys(
            artifact,
            required={
                "path",
                "sha256",
                "kind",
                "parser",
                "scenario_ids",
                "labels",
            },
            optional={"frame_labels"},
            description=f"artifacts[{index}]",
        )
        _relative_path(artifact.get("path"), f"artifacts[{index}].path")
        paths.append(artifact["path"])
        if not isinstance(artifact.get("sha256"), str) or not SHA256_RE.fullmatch(
            artifact["sha256"]
        ):
            raise AssertionConfigurationError(f"artifacts[{index}].sha256 格式非法")
        kind = artifact.get("kind")
        parser = artifact.get("parser")
        if kind not in ALLOWED_PARSERS_BY_KIND or parser not in ALLOWED_PARSERS_BY_KIND[kind]:
            raise AssertionConfigurationError(
                f"artifacts[{index}] 的 kind/parser 组合不受支持：{kind}/{parser}"
            )
        scenario_ids = artifact.get("scenario_ids")
        if not isinstance(scenario_ids, list) or not scenario_ids or any(
            not isinstance(item, str) or not re.fullmatch(r"A\d{2}", item)
            for item in scenario_ids
        ):
            raise AssertionConfigurationError(
                f"artifacts[{index}].scenario_ids 格式非法"
            )
        if len(set(scenario_ids)) != len(scenario_ids):
            raise AssertionConfigurationError(
                f"artifacts[{index}].scenario_ids 存在重复项"
            )
        labels = artifact.get("labels")
        if not isinstance(labels, dict):
            raise AssertionConfigurationError(f"artifacts[{index}].labels 必须是对象")
        if "frame_labels" in artifact:
            # 帧级标签只对能产出 websocket_frame 观测的两条路径有意义：
            # h1_request_stream 直接解析原始字节，或派生器产出的 observation_jsonl
            # （candidate 侧 relay 走 opaque+derive，帧观测在派生 jsonl 中）。
            if parser not in {"h1_request_stream", "observation_jsonl"}:
                raise AssertionConfigurationError(
                    f"artifacts[{index}].frame_labels 仅支持 h1_request_stream "
                    "或 observation_jsonl"
                )
            frame_labels = artifact["frame_labels"]
            if not isinstance(frame_labels, dict) or not frame_labels:
                raise AssertionConfigurationError(
                    f"artifacts[{index}].frame_labels 必须是非空对象"
                )
            for frame_index, values in frame_labels.items():
                if not isinstance(frame_index, str) or not re.fullmatch(
                    r"0|[1-9]\d*", frame_index
                ):
                    raise AssertionConfigurationError(
                        f"artifacts[{index}].frame_labels 索引非法：{frame_index!r}"
                    )
                if not isinstance(values, dict) or not values:
                    raise AssertionConfigurationError(
                        f"artifacts[{index}].frame_labels[{frame_index!r}] "
                        "必须是非空对象"
                    )
                conflicts = sorted(set(labels) & set(values))
                if conflicts:
                    raise AssertionConfigurationError(
                        f"artifacts[{index}].frame_labels[{frame_index!r}] "
                        f"不能覆盖 artifact labels：{conflicts}"
                    )
                for label_name, label_value in values.items():
                    if not isinstance(label_name, str) or not re.fullmatch(
                        r"[a-z][a-z0-9_]*", label_name
                    ):
                        raise AssertionConfigurationError(
                            f"artifacts[{index}].frame_labels[{frame_index!r}] "
                            f"标签名非法：{label_name!r}"
                        )
                    if not isinstance(label_value, str) or not label_value.strip():
                        raise AssertionConfigurationError(
                            f"artifacts[{index}].frame_labels[{frame_index!r}]"
                            f".{label_name} 必须是非空字符串"
                        )
    if len(set(paths)) != len(paths):
        raise AssertionConfigurationError("capture manifest 存在重复 artifact path")
    return artifacts


def _header_values(request: Mapping[str, Any]) -> dict[str, list[Any]]:
    values: dict[str, list[Any]] = {}
    for item in request.get("headers", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            values.setdefault(name.lower(), []).append(item.get("value"))
    return values


def _parse_request_line(line: Any) -> dict[str, Any]:
    if not isinstance(line, str):
        return {"method": None, "target": None, "path": None, "query_pairs": [], "protocol": None}
    parts = line.split(" ", 2)
    if len(parts) != 3:
        return {"method": None, "target": line, "path": None, "query_pairs": [], "protocol": None}
    method, target, protocol = parts
    parsed = urlsplit(target)
    return {
        "method": method,
        "target": target,
        "path": parsed.path,
        "query_pairs": [
            [key, value]
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        "protocol": protocol,
    }


def _pcap_observations(
    path: Path,
    artifact_path: str,
    scenario_ids: Sequence[str],
    labels: Mapping[str, Any],
) -> list[Observation]:
    observations: list[Observation] = []
    packet_index = 0
    for linktype, packet in iter_packets(path):
        packet_index += 1
        parsed_tcp = tcp_payload(linktype, packet)
        if parsed_tcp is None:
            continue
        destination, destination_port, payload = parsed_tcp
        parsed_hello = parse_client_hello(payload)
        if parsed_hello is None:
            continue
        sni, extension_types, cipher_suites, alpn_protocols = parsed_hello
        data = {
            "destination": destination,
            "destination_port": destination_port,
            "sni": sni,
            "extension_types": extension_types,
            "cipher_suites": cipher_suites,
            "cipher_suite_count": len(cipher_suites),
            "alpn_protocols": alpn_protocols,
            "has_alpn_extension": 16 in extension_types,
        }
        for scenario_id in scenario_ids:
            observations.append(
                Observation(
                    record_id=f"{artifact_path}#packet-{packet_index}",
                    scenario_id=scenario_id,
                    record_type="tls_client_hello",
                    artifact_path=artifact_path,
                    evidence_paths=(artifact_path,),
                    labels=dict(labels),
                    data=data,
                )
            )
    return observations


def _h1_observations(
    path: Path,
    artifact_path: str,
    scenario_ids: Sequence[str],
    labels: Mapping[str, Any],
    frame_labels: Mapping[str, Mapping[str, str]],
) -> list[Observation]:
    payload = path.read_bytes()
    try:
        requests = parse_h1_stream(payload)
    except SystemExit as error:
        raise AssertionConfigurationError(
            f"H1 原始字节解析依赖缺失或解析失败：{artifact_path}: {error}"
        ) from error
    observations: list[Observation] = []
    for request_index, request in enumerate(requests):
        line = _parse_request_line(request.get("request_line"))
        header_names = request.get("header_names_in_order", [])
        wire_protocol = (
            "websocket"
            if any(
                isinstance(name, str) and name.lower() == "upgrade"
                for name in header_names
            )
            else "http"
        )
        data = {
            **line,
            "connection_id": labels.get("connection_id", Path(artifact_path).name),
            "request_index": request_index,
            "wire_protocol": wire_protocol,
            "header_names_in_order": header_names,
            "remaining_header_names": header_names[5:],
            "header_values": _header_values(request),
            "body": request.get("body"),
        }
        for scenario_id in scenario_ids:
            observations.append(
                Observation(
                    record_id=f"{artifact_path}#request-{request_index}",
                    scenario_id=scenario_id,
                    record_type="http_request",
                    artifact_path=artifact_path,
                    evidence_paths=(artifact_path,),
                    labels=dict(labels),
                    data=data,
                )
            )

    parsed_frames: list[dict[str, Any]] = []
    if requests and any(
        name.lower() == "upgrade"
        for name in requests[0].get("header_names_in_order", [])
    ):
        header_end = payload.find(b"\r\n\r\n")
        if header_end >= 0:
            parsed_frames = parse_ws_frames(payload[header_end + 4 :])
            for frame_index, frame in enumerate(parsed_frames):
                # 帧级标签只叠加到派生事实；原始 relay 字节始终保持只读。
                observation_labels = dict(labels)
                observation_labels.update(frame_labels.get(str(frame_index), {}))
                for scenario_id in scenario_ids:
                    observations.append(
                        Observation(
                            record_id=f"{artifact_path}#frame-{frame_index}",
                            scenario_id=scenario_id,
                            record_type="websocket_frame",
                            artifact_path=artifact_path,
                            evidence_paths=(artifact_path,),
                            labels=observation_labels,
                            data={
                                **frame,
                                "connection_id": labels.get(
                                    "connection_id", Path(artifact_path).name
                                ),
                                "frame_index": frame_index,
                            },
                        )
                    )
    missing_frame_indexes = sorted(
        int(frame_index)
        for frame_index in frame_labels
        if int(frame_index) >= len(parsed_frames)
    )
    if missing_frame_indexes:
        raise AssertionConfigurationError(
            f"artifact frame_labels 引用了不存在的 WebSocket 帧："
            f"{artifact_path}: {missing_frame_indexes}"
        )
    return observations


def _structured_records(path: Path, parser: str) -> list[Any]:
    try:
        if parser == "observation_json":
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_object,
            )
            return payload if isinstance(payload, list) else [payload]
        # 按 \n 切分而非 splitlines()：后者会在 \x85／\u2028 等 Unicode 行
        # 分隔符处误切，而它们可合法出现在 JSON 字符串内部（真实 mitm 记录即含
        # \x85），会把一条记录截断成两条非法 JSON。
        return [
            json.loads(line, object_pairs_hook=_unique_json_object)
            for line in path.read_text(encoding="utf-8").split("\n")
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AssertionConfigurationError(
            f"结构化 trace 无法解析：{path}: {error}"
        ) from error


def _trace_observations(
    path: Path,
    artifact_path: str,
    parser: str,
    scenario_ids: Sequence[str],
    labels: Mapping[str, Any],
    declared_artifact_scenarios: Mapping[str, set[str]],
    frame_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> list[Observation]:
    frame_labels = frame_labels or {}
    seen_frame_indexes: set[int] = set()
    observations: list[Observation] = []
    for index, record in enumerate(_structured_records(path, parser)):
        if not isinstance(record, dict):
            raise AssertionConfigurationError(
                f"{artifact_path} 第 {index + 1} 条记录必须是对象"
            )
        _require_exact_keys(
            record,
            required={
                "schema_version",
                "record_id",
                "scenario_id",
                "record_type",
                "data",
            },
            optional={"source_artifacts"},
            description=f"{artifact_path} 第 {index + 1} 条记录",
        )
        if record.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
            raise AssertionConfigurationError(
                f"{artifact_path} 第 {index + 1} 条记录 schema_version 不匹配"
            )
        scenario_id = record.get("scenario_id")
        if scenario_id not in scenario_ids:
            raise AssertionConfigurationError(
                f"{artifact_path} 第 {index + 1} 条记录场景不属于 artifact"
            )
        record_id = record.get("record_id")
        record_type = record.get("record_type")
        data = record.get("data")
        if not isinstance(record_id, str) or not record_id.strip():
            raise AssertionConfigurationError("结构化 trace record_id 不能为空")
        if not isinstance(record_type, str) or not record_type.strip():
            raise AssertionConfigurationError("结构化 trace record_type 不能为空")
        if not isinstance(data, dict) or not data:
            raise AssertionConfigurationError("结构化 trace data 必须是非空对象")
        source_artifacts = record.get("source_artifacts", [])
        if not isinstance(source_artifacts, list) or any(
            not isinstance(item, str) for item in source_artifacts
        ):
            raise AssertionConfigurationError("source_artifacts 必须是字符串数组")
        unknown_sources = sorted(
            set(source_artifacts) - set(declared_artifact_scenarios)
        )
        if unknown_sources:
            raise AssertionConfigurationError(
                f"结构化 trace 引用了 manifest 外证据：{unknown_sources}"
            )
        cross_scenario_sources = sorted(
            source
            for source in source_artifacts
            if scenario_id not in declared_artifact_scenarios[source]
        )
        if cross_scenario_sources:
            raise AssertionConfigurationError(
                "结构化 trace 只能绑定同一场景的原始证据："
                f"{cross_scenario_sources}"
            )
        evidence_paths = tuple(dict.fromkeys([artifact_path, *source_artifacts]))
        # 帧级标签与 _h1_observations 同语义：只叠加到 websocket_frame 事实，
        # 帧索引取派生记录 data 内的 frame_index。
        observation_labels = dict(labels)
        if record_type == "websocket_frame":
            frame_index = data.get("frame_index")
            if isinstance(frame_index, int) and not isinstance(frame_index, bool):
                seen_frame_indexes.add(frame_index)
                observation_labels.update(frame_labels.get(str(frame_index), {}))
        observations.append(
            Observation(
                record_id=record_id,
                scenario_id=scenario_id,
                record_type=record_type,
                artifact_path=artifact_path,
                evidence_paths=evidence_paths,
                labels=observation_labels,
                data=data,
            )
        )
    missing_frame_indexes = sorted(
        int(frame_index)
        for frame_index in frame_labels
        if int(frame_index) not in seen_frame_indexes
    )
    if missing_frame_indexes:
        raise AssertionConfigurationError(
            f"artifact frame_labels 引用了不存在的 WebSocket 帧："
            f"{artifact_path}: {missing_frame_indexes}"
        )
    if not observations:
        raise AssertionConfigurationError(f"结构化 trace 为空：{artifact_path}")
    return observations


def load_observations(
    capture_manifest_path: Path,
    evidence_root: Path,
    expected_codex_version: str = CODEX_VERSION,
) -> tuple[dict[str, Any], list[Observation]]:
    """校验所有 manifest artifact 的路径与哈希，并解析可断言事实。"""

    manifest = _load_json(capture_manifest_path, "capture manifest")
    artifacts = _validate_capture_manifest(manifest, expected_codex_version)
    declared_artifact_scenarios = {
        artifact["path"]: set(artifact["scenario_ids"])
        for artifact in artifacts
    }
    resolved: dict[str, Path] = {}
    for index, artifact in enumerate(artifacts):
        relative = _relative_path(artifact["path"], f"artifacts[{index}].path")
        path = _resolve_evidence_file(
            evidence_root, relative, f"artifacts[{index}]"
        )
        actual_sha = file_sha256(path)
        if actual_sha != artifact["sha256"]:
            raise AssertionConfigurationError(
                f"artifact SHA-256 不匹配：{artifact['path']}"
            )
        resolved[artifact["path"]] = path

    observations: list[Observation] = []
    opaque_paths: set[str] = set()
    for artifact in artifacts:
        path = resolved[artifact["path"]]
        parser = artifact["parser"]
        if parser == "opaque_bound_source":
            opaque_paths.add(artifact["path"])
            continue
        if parser == "pcap_client_hello":
            parsed = _pcap_observations(
                path,
                artifact["path"],
                artifact["scenario_ids"],
                artifact["labels"],
            )
        elif parser == "h1_request_stream":
            parsed = _h1_observations(
                path,
                artifact["path"],
                artifact["scenario_ids"],
                artifact["labels"],
                artifact.get("frame_labels", {}),
            )
        else:
            parsed = _trace_observations(
                path,
                artifact["path"],
                parser,
                artifact["scenario_ids"],
                artifact["labels"],
                declared_artifact_scenarios,
                artifact.get("frame_labels"),
            )
        if not parsed:
            raise AssertionConfigurationError(
                f"artifact 未解析出任何事实：{artifact['path']}"
            )
        observations.extend(parsed)

    referenced_paths = {
        evidence_path
        for observation in observations
        for evidence_path in observation.evidence_paths
    }
    unbound_opaque = sorted(opaque_paths - referenced_paths)
    if unbound_opaque:
        raise AssertionConfigurationError(
            "opaque 原始证据必须由结构化 trace 的 source_artifacts 显式绑定："
            f"{unbound_opaque}"
        )

    record_ids = [item.record_id for item in observations]
    if len(record_ids) != len(set(record_ids)):
        raise AssertionConfigurationError("所有 artifact 的 record_id 必须全局唯一")
    return manifest, observations


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    if not isinstance(path, str) or not path:
        return MISSING
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def _is_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _is_subset(value, actual[key])
            for key, value in expected.items()
        )
    return expected == actual


def _where_matches(observation: Observation, conditions: Any) -> bool:
    if conditions is None:
        return True
    if not isinstance(conditions, list):
        raise AssertionConfigurationError("select.where 必须是数组")
    mapping = observation.as_mapping()
    for condition in conditions:
        if not isinstance(condition, dict):
            raise AssertionConfigurationError("where 条件必须是对象")
        _require_exact_keys(
            condition,
            required={"path", "operator"},
            optional={"value"},
            description="where 条件",
        )
        actual = _resolve_path(mapping, condition["path"])
        operator = condition["operator"]
        expected = condition.get("value")
        if operator == "equal":
            matched = actual is not MISSING and actual == expected
        elif operator == "not_equal":
            matched = actual is not MISSING and actual != expected
        elif operator == "present":
            matched = actual is not MISSING
        elif operator == "absent":
            matched = actual is MISSING
        elif operator == "contains":
            matched = actual is not MISSING and expected in actual
        elif operator == "in":
            matched = actual is not MISSING and isinstance(expected, list) and actual in expected
        elif operator == "match":
            matched = (
                isinstance(actual, str)
                and isinstance(expected, str)
                and re.search(expected, actual) is not None
            )
        elif operator == "subset":
            matched = actual is not MISSING and _is_subset(expected, actual)
        else:
            raise AssertionConfigurationError(f"不支持的 where operator：{operator!r}")
        if not matched:
            return False
    return True


def _select_observations(
    observations: Sequence[Observation],
    selector: Mapping[str, Any],
    default_scenarios: Sequence[str],
) -> list[Observation]:
    record_type = selector.get("record_type")
    if not isinstance(record_type, str) or not record_type:
        raise AssertionConfigurationError("select.record_type 不能为空")
    scenario_ids = selector.get("scenario_ids", list(default_scenarios))
    if not isinstance(scenario_ids, list) or not scenario_ids:
        raise AssertionConfigurationError("select.scenario_ids 必须是非空数组")
    return [
        item
        for item in observations
        if item.record_type == record_type
        and item.scenario_id in scenario_ids
        and _where_matches(item, selector.get("where"))
    ]


def _json_safe(value: Any) -> Any:
    return "<absent>" if value is MISSING else value


def _evaluate_assertion(
    matched: Sequence[Observation], assertion: Mapping[str, Any]
) -> tuple[bool, dict[str, Any]]:
    operator = assertion.get("operator")
    if operator not in ALLOWED_ASSERTION_OPERATORS:
        raise AssertionConfigurationError(f"不支持的断言 operator：{operator!r}")
    mappings = [item.as_mapping() for item in matched]
    path = assertion.get("path")
    values = [_resolve_path(item, path) for item in mappings] if isinstance(path, str) else []

    if operator == "count_at_least":
        expected = assertion.get("value")
        passed = (
            isinstance(expected, int)
            and not isinstance(expected, bool)
            and len(matched) >= expected
        )
        return passed, {"matched_count": len(matched)}
    if operator == "count_equal":
        expected = assertion.get("value")
        return len(matched) == expected, {"matched_count": len(matched)}
    if operator == "all_equal":
        expected = assertion.get("value")
        return bool(values) and all(value == expected for value in values), {
            "values": [_json_safe(value) for value in values]
        }
    if operator == "any_equal":
        expected = assertion.get("value")
        return any(value == expected for value in values), {
            "values": [_json_safe(value) for value in values]
        }
    if operator == "all_absent":
        return bool(mappings) and all(value is MISSING for value in values), {
            "values": [_json_safe(value) for value in values]
        }
    if operator in {"all_contains", "all_not_contains"}:
        expected = assertion.get("value")
        contained = [value is not MISSING and expected in value for value in values]
        passed = bool(contained) and (
            all(contained) if operator == "all_contains" else not any(contained)
        )
        return passed, {"values": [_json_safe(value) for value in values]}
    if operator == "all_lowercase":
        passed = bool(values) and all(
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item == item.lower() for item in value)
            for value in values
        )
        return passed, {"values": [_json_safe(value) for value in values]}
    if operator in {"all_list_prefix", "all_list_suffix"}:
        expected = assertion.get("value")
        if not isinstance(expected, list):
            raise AssertionConfigurationError(f"{operator}.value 必须是数组")
        comparisons = []
        for value in values:
            if not isinstance(value, list):
                comparisons.append(False)
            elif operator == "all_list_prefix":
                comparisons.append(value[: len(expected)] == expected)
            else:
                comparisons.append(value[-len(expected) :] == expected if expected else True)
        return bool(comparisons) and all(comparisons), {
            "values": [_json_safe(value) for value in values]
        }
    if operator == "all_match":
        pattern = assertion.get("value")
        if not isinstance(pattern, str):
            raise AssertionConfigurationError("all_match.value 必须是正则字符串")
        passed = bool(values) and all(
            isinstance(value, str) and re.fullmatch(pattern, value) is not None
            for value in values
        )
        return passed, {"values": [_json_safe(value) for value in values]}
    if operator == "all_ordered_subset_of":
        allowed = assertion.get("allowed")
        required = assertion.get("required", [])
        if not isinstance(allowed, list) or not isinstance(required, list):
            raise AssertionConfigurationError(
                "all_ordered_subset_of 需要 allowed/required 数组"
            )
        allowed_positions = {item: index for index, item in enumerate(allowed)}
        results: list[bool] = []
        for value in values:
            if not isinstance(value, list) or any(
                item not in allowed_positions for item in value
            ):
                results.append(False)
                continue
            positions = [allowed_positions[item] for item in value]
            results.append(
                positions == sorted(positions)
                and len(set(value)) == len(value)
                and all(item in value for item in required)
            )
        return bool(results) and all(results), {
            "values": [_json_safe(value) for value in values]
        }
    if operator in {"all_subset", "any_subset", "none_subset"}:
        expected = assertion.get("value")
        results = [value is not MISSING and _is_subset(expected, value) for value in values]
        if operator == "all_subset":
            passed = bool(results) and all(results)
        elif operator == "any_subset":
            passed = any(results)
        else:
            passed = bool(mappings) and not any(results)
        return passed, {"values": [_json_safe(value) for value in values]}
    if operator == "distinct_count_at_least":
        expected = assertion.get("value")
        serialized = {
            json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
            for value in values
        }
        passed = isinstance(expected, int) and len(serialized) >= expected
        return passed, {
            "distinct_count": len(serialized),
            "values": [_json_safe(value) for value in values],
        }
    if operator == "distinct_values_equal":
        expected = assertion.get("value")
        if not isinstance(expected, list):
            raise AssertionConfigurationError(
                "distinct_values_equal.value 必须是数组"
            )
        serialized_actual = {
            json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
            for value in values
        }
        serialized_expected = {
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            for value in expected
        }
        return bool(values) and serialized_actual == serialized_expected, {
            "distinct_values": [_json_safe(value) for value in values]
        }
    if operator == "same_set_distinct_order":
        minimum_records = assertion.get("minimum_records")
        minimum_artifacts = assertion.get("minimum_artifacts")
        minimum_orders = assertion.get("minimum_distinct_orders")
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in (minimum_records, minimum_artifacts, minimum_orders)
        ):
            raise AssertionConfigurationError(
                "same_set_distinct_order 的三个 minimum_* 必须是正整数"
            )

        # 同一个 selector 可能同时选中 native-tls 与 rustls 等多个 TLS 实现。
        # 规则要证明的是“存在至少一组扩展集合相同、但线序有扰动的独立样本”，
        # 不能要求 selector 命中的所有实现共享同一扩展集合。按集合分组后逐组核对
        # record、artifact 与排列数；只有同一组同时达到三个阈值才算通过。
        groups: dict[str, dict[str, Any]] = {}
        invalid_record_ids: list[str] = []
        for observation, sequence in zip(matched, values, strict=True):
            if not isinstance(sequence, list):
                invalid_record_ids.append(observation.record_id)
                continue
            try:
                set_key = json.dumps(
                    sorted(
                        {
                            json.dumps(
                                _json_safe(item),
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            for item in sequence
                        }
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as error:
                raise AssertionConfigurationError(
                    "same_set_distinct_order 的序列元素无法形成稳定集合"
                ) from error
            group = groups.setdefault(
                set_key,
                {
                    "values": [],
                    "artifacts": set(),
                    "orders": set(),
                    "record_ids": [],
                },
            )
            group["values"].append(sequence)
            group["artifacts"].add(observation.artifact_path)
            group["orders"].add(
                json.dumps(
                    _json_safe(sequence),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            group["record_ids"].append(observation.record_id)

        summaries = [
            {
                "extension_set": json.loads(set_key),
                "record_count": len(group["values"]),
                "artifact_count": len(group["artifacts"]),
                "distinct_order_count": len(group["orders"]),
                "record_ids": group["record_ids"],
                "values": group["values"],
            }
            for set_key, group in sorted(groups.items())
        ]
        qualifying = [
            group
            for group in summaries
            if group["record_count"] >= minimum_records
            and group["artifact_count"] >= minimum_artifacts
            and group["distinct_order_count"] >= minimum_orders
        ]
        passed = bool(qualifying) and not invalid_record_ids
        return passed, {
            "matching_group_count": len(qualifying),
            "selected_group": qualifying[0] if qualifying else None,
            "groups": summaries,
            "invalid_record_ids": invalid_record_ids,
        }
    if operator == "all_fields_equal":
        left_path = assertion.get("left_path")
        right_path = assertion.get("right_path")
        if not isinstance(left_path, str) or not isinstance(right_path, str):
            raise AssertionConfigurationError("all_fields_equal 需要 left_path/right_path")
        pairs = [
            (_resolve_path(item, left_path), _resolve_path(item, right_path))
            for item in mappings
        ]
        passed = bool(pairs) and all(
            left is not MISSING and right is not MISSING and left == right
            for left, right in pairs
        )
        return passed, {
            "pairs": [
                {"left": _json_safe(left), "right": _json_safe(right)}
                for left, right in pairs
            ]
        }
    if operator in {"all_list_all_same", "all_list_all_different"}:
        results: list[bool] = []
        for value in values:
            if not isinstance(value, list) or len(value) < 2:
                results.append(False)
                continue
            serialized = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if operator == "all_list_all_same":
                results.append(len(set(serialized)) == 1)
            else:
                results.append(len(set(serialized)) == len(serialized))
        return bool(results) and all(results), {
            "values": [_json_safe(value) for value in values]
        }
    raise AssertionConfigurationError(f"断言 operator 未实现：{operator}")


def _expected_payload(assertion: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in assertion.items()}


def _side_restriction_contract(profile: Mapping[str, Any]) -> dict[str, Any]:
    """只取侧别限定投影，不构造完整契约——本模块不承担契约冻结校验。"""

    return {"side_restricted_checks": derive_side_restricted_checks(profile)}


def evaluate_rule(
    profile: Mapping[str, Any],
    rule_id: str,
    observations: Sequence[Observation],
    capture_manifest: Mapping[str, Any],
    side: str | None = None,
) -> list[dict[str, Any]]:
    """执行一条冻结规则的全部检查并返回结构化结果。

    给出 ``side`` 时跳过验收契约登记为本侧不适用的 check——这类 check 依赖的实验
    条件在本侧结构性不可能成立（依据见 acceptance_contract.SIDE_RESTRICTED_CHECKS），
    强制执行只会把"造不出该条件"记成失败。不给 ``side`` 时按全集评估。
    """

    rules = {
        rule["rule_id"]: rule
        for rule in profile["rules"]
        if isinstance(rule, dict) and isinstance(rule.get("rule_id"), str)
    }
    if rule_id not in rules:
        raise AssertionConfigurationError(f"冻结画像不包含规则：{rule_id}")
    rule = rules[rule_id]
    scenario_profiles = {
        scenario["scenario_id"]: scenario for scenario in profile["scenarios"]
    }
    artifacts = capture_manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise AssertionConfigurationError("capture manifest artifacts 缺失")
    coverage_actual: dict[str, list[str]] = {}
    coverage_expected: dict[str, list[str]] = {}
    coverage_paths: set[str] = set()
    coverage_passed = True
    for scenario_id in rule["scenario_ids"]:
        required_kinds = scenario_profiles[scenario_id]["required_artifact_kinds"]
        actual_kinds = sorted(
            {
                artifact["kind"]
                for artifact in artifacts
                if scenario_id in artifact["scenario_ids"]
            }
        )
        scenario_paths = {
            artifact["path"]
            for artifact in artifacts
            if scenario_id in artifact["scenario_ids"]
        }
        coverage_paths.update(scenario_paths)
        coverage_expected[scenario_id] = sorted(required_kinds)
        coverage_actual[scenario_id] = actual_kinds
        if not set(required_kinds).issubset(actual_kinds):
            coverage_passed = False
    results: list[dict[str, Any]] = [
        {
            "id": "scenario-artifact-coverage",
            "description": (
                "本规则所需冻结场景均具有规定类型的已哈希原始证据"
            ),
            "passed": coverage_passed,
            "expected": coverage_expected,
            "actual": coverage_actual,
            "evidence_paths": sorted(coverage_paths),
        }
    ]
    side_contract = _side_restriction_contract(profile) if side else None
    for check in rule["checks"]:
        if side_contract is not None and not check_applies_to_side(
            side_contract, rule_id, check["id"], side
        ):
            continue
        matched = _select_observations(
            observations, check["select"], rule["scenario_ids"]
        )
        passed, actual = _evaluate_assertion(matched, check["assertion"])
        evidence_paths = sorted(
            {
                evidence_path
                for observation in matched
                for evidence_path in observation.evidence_paths
            }
        )
        if not evidence_paths:
            # 失败结果仍提供非空路径，便于定位本规则预期场景中
            # 已有但未命中的证据。
            scenario_ids = set(
                check["select"].get("scenario_ids", rule["scenario_ids"])
            )
            evidence_paths = sorted(
                {
                    evidence_path
                    for observation in observations
                    if observation.scenario_id in scenario_ids
                    for evidence_path in observation.evidence_paths
                }
            )
        results.append(
            {
                "id": check["id"],
                "description": check["description"],
                "passed": passed,
                "expected": _expected_payload(check["assertion"]),
                "actual": actual,
                "evidence_paths": evidence_paths,
            }
        )
    return results


def build_assertion_command(
    *,
    rule_id: str,
    capture_manifest: str,
    evidence_root: str,
    output: str,
    profile: str = DEFAULT_PROFILE_RELATIVE_PATH,
    rule_manifest: str = "tools/official_client_capture/codex_upgrade_rules_0_145_0.json",
    expected_codex_version: str | None = None,
    expected_profile_sha256: str | None = None,
    side: str | None = None,
) -> list[str]:
    """构造应写入验收 submission 的稳定 checker 参数数组。"""

    command = [
        "python3",
        CHECKER_RELATIVE_PATH,
        "--rule-id",
        rule_id,
        "--capture-manifest",
        capture_manifest,
        "--evidence-root",
        evidence_root,
        "--profile",
        profile,
        "--rule-manifest",
        rule_manifest,
    ]
    if expected_codex_version is not None:
        command.extend(["--expected-codex-version", expected_codex_version])
    if expected_profile_sha256 is not None:
        command.extend(["--expected-profile-sha256", expected_profile_sha256])
    if side is not None:
        command.extend(["--side", side])
    command.extend(["--output", output])
    return command


def build_assertion_result(
    *,
    rule_id: str,
    checks: Sequence[Mapping[str, Any]],
    command: Sequence[str],
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """生成最终门禁可读取的单规则断言结果。"""

    passed = bool(checks) and all(check.get("passed") is True for check in checks)
    return {
        "schema_version": ASSERTION_SCHEMA_VERSION,
        "rule_id": rule_id,
        "status": "pass" if passed else "fail",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": 0 if passed else 1,
        "checker_sha256": file_sha256(Path(__file__)),
        "command_sha256": command_sha256(command),
        "checks": [dict(check) for check in checks],
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    from tools.official_client_capture.capturelib.security import secure_write_json

    secure_write_json(path, payload)


def _build_parser() -> argparse.ArgumentParser:
    tool_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="按独立冻结画像执行 Codex CLI 0.145.0 候选侧单规则断言"
    )
    parser.add_argument("--rule-id", required=True)
    parser.add_argument("--capture-manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument(
        "--profile",
        type=Path,
        default=tool_root / "candidate_rule_expectations_0_145_0.json",
    )
    parser.add_argument(
        "--rule-manifest",
        type=Path,
        default=tool_root / "codex_upgrade_rules_0_145_0.json",
    )
    parser.add_argument("--expected-codex-version")
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument(
        "--side",
        choices=("official", "candidate"),
        help="验收侧；给出时跳过契约登记为本侧不适用的 check",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    started_at = utc_now()
    checks: list[dict[str, Any]]
    try:
        expected_version = args.expected_codex_version or CODEX_VERSION
        profile = load_profile(
            args.profile,
            args.rule_manifest,
            verify_frozen_digest=args.expected_profile_sha256 is None,
            expected_codex_version=expected_version,
            expected_profile_sha256=args.expected_profile_sha256,
        )
        capture_manifest, observations = load_observations(
            args.capture_manifest,
            args.evidence_root,
            expected_version,
        )
        checks = evaluate_rule(
            profile, args.rule_id, observations, capture_manifest, args.side
        )
    except (AssertionConfigurationError, OSError, ValueError) as error:
        checks = [
            {
                "id": "input-validation",
                "description": "画像、manifest 与原始证据必须完整且相互绑定",
                "passed": False,
                "expected": {"input_valid": True},
                "actual": {"input_valid": False, "error": str(error)},
                "evidence_paths": [str(args.capture_manifest)],
            }
        ]
    finished_at = utc_now()
    command = build_assertion_command(
        rule_id=args.rule_id,
        capture_manifest=str(args.capture_manifest),
        evidence_root=str(args.evidence_root),
        profile=str(args.profile),
        rule_manifest=str(args.rule_manifest),
        expected_codex_version=args.expected_codex_version,
        expected_profile_sha256=args.expected_profile_sha256,
        side=args.side,
        output=str(args.output),
    )
    result = build_assertion_result(
        rule_id=args.rule_id,
        checks=checks,
        command=command,
        started_at=started_at,
        finished_at=finished_at,
    )
    _write_json(args.output, result)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
