#!/usr/bin/env python3
"""把部署源码快照的 ``go test -json`` 事实收口为候选结构化 trace。

本工具不从 Go 源码常量推导验收结论，也不生成 HTTP、TLS 或 WebSocket 原始线事实。
只有冻结映射中的精确测试在原始 ``go test -json`` 日志中实际通过，且测试输出了映射
允许的动态事实时，才会生成 observation。每条 observation 同时绑定测试日志和同场景
原始 relay，供独立 42 条断言器回放。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.candidate_rule_assertion import (
    ALLOWED_PARSERS_BY_KIND,
    _validate_capture_manifest,
)


MAP_SCHEMA_VERSION = "codex-candidate-test-fact-map/v1"
FACT_SCHEMA_VERSION = "codex-candidate-test-fact/v1"
OBSERVATION_SCHEMA_VERSION = "codex-candidate-observation/v1"
RECEIPT_SCHEMA_VERSION = "codex-candidate-test-trace-receipt/v1"
FACT_PREFIX = "CANDIDATE_TRACE_FACT "
DEFAULT_MAPPING_RELATIVE_PATH = (
    "tools/official_client_capture/candidate_test_fact_map_0_145_0.json"
)
DEFAULT_PROFILE_RELATIVE_PATH = (
    "tools/official_client_capture/candidate_rule_expectations_0_145_0.json"
)
# 冻结映射内容完成后由离线测试固定；任何修改都必须显式更新并重新审核。
FROZEN_MAPPING_SHA256 = (
	"13eb15f45c92f216b2eb023d859d91f02c70a6026c4a5b7c252f861239e65319"
)
# 2026-08-10（SCN-REALITY-01 §3.1）：随 A01／A15 的 required_artifact_kinds 对齐更新。
FROZEN_PROFILE_SHA256 = (
    "78a0ec3f69206e54ce8f5b7dda19c7db9abb0ff9cb03ad9a715cefe27e747f1e"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
TEST_NAME_RE = re.compile(r"^Test[A-Za-z0-9_]+$")
FACT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]+$")
SCENARIO_RE = re.compile(r"^A[0-9]{2}$")
DATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FORBIDDEN_RECORD_TYPES = frozenset(
    {"http_request", "tls_client_hello", "websocket_frame"}
)
ALLOWED_RECORD_TYPES = frozenset(
    {
        "alpha_search_flow",
        "compaction_decision",
        "conditional_header",
        "connection_lifecycle",
        "file_upload_chain",
        "header_assembly",
        "image_edit_encoding",
        "image_tool_flow",
        "lite_transform",
        "realtime_chain",
        "response_prefix_reuse",
        "serialization_boundary",
        "surface_identity",
        "transport_fallback",
        "turn_state_chain",
        "websocket_compression_context",
    }
)
ALLOWED_TRACE_KINDS = frozenset({"process_trace", "websocket_trace"})
ALLOWED_RAW_SOURCE_KINDS = frozenset(
    {"pcap", "pcapng", "relay_binary", "wire_dump"}
)


class CandidateTestTraceError(ValueError):
    """冻结映射、源码快照、测试日志或抓包证据不满足失败关闭约束。"""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateTestTraceError(f"JSON 对象包含重复字段：{key}")
        result[key] = value
    return result


def _loads_json(payload: str, description: str) -> Any:
    try:
        return json.loads(payload, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise CandidateTestTraceError(f"{description}不是合法 JSON：{error}") from error


def _load_json(path: Path, description: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise CandidateTestTraceError(f"{description}必须是非符号链接普通文件：{path}")
    try:
        return _loads_json(path.read_text(encoding="utf-8"), description)
    except OSError as error:
        raise CandidateTestTraceError(f"{description}无法读取：{error}") from error


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: Any, *, jsonl: bool = False) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return encoded + (b"\n" if jsonl else b"")


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    description: str,
) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing or extra:
        raise CandidateTestTraceError(
            f"{description}字段不闭合：missing={missing} extra={extra}"
        )


def _relative_path(value: Any, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise CandidateTestTraceError(f"{description}必须是 POSIX 相对路径")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value in {".", ".."}
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise CandidateTestTraceError(f"{description}不能逃逸根目录：{value}")
    return path


def _resolve_existing_file(
    root: Path,
    relative: PurePosixPath,
    description: str,
    *,
    require_nonempty: bool = True,
) -> Path:
    root_resolved = root.resolve(strict=True)
    current = root_resolved
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CandidateTestTraceError(f"{description}路径包含符号链接：{relative}")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as error:
        raise CandidateTestTraceError(
            f"{description}不存在或逃逸根目录：{relative}"
        ) from error
    if not resolved.is_file() or (require_nonempty and resolved.stat().st_size <= 0):
        raise CandidateTestTraceError(f"{description}必须是普通文件：{relative}")
    return resolved


def _relative_existing_file(root: Path, path: Path, description: str) -> PurePosixPath:
    if path.is_symlink() or not path.is_file():
        raise CandidateTestTraceError(f"{description}必须是非符号链接普通文件：{path}")
    try:
        relative = path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as error:
        raise CandidateTestTraceError(f"{description}必须位于指定根目录内：{path}") from error
    return _relative_path(relative.as_posix(), description)


@dataclass(frozen=True)
class FactSpec:
    fact_id: str
    scenario_id: str
    record_type: str
    trace_kind: str
    data_keys: tuple[str, ...]
    required_source_kinds: tuple[str, ...]


@dataclass(frozen=True)
class TestSpec:
    package: str
    name: str
    test_file: str
    test_file_sha256: str
    source_files: tuple[tuple[str, str], ...]
    facts: tuple[FactSpec, ...]

    @property
    def key(self) -> tuple[str, str]:
        return self.package, self.name


def load_mapping(
    path: Path,
    *,
    expected_codex_version: str,
    expected_sha256: str,
) -> tuple[dict[str, Any], tuple[TestSpec, ...]]:
    """加载并严格校验测试名到抽象事实的冻结映射。"""

    if not VERSION_RE.fullmatch(expected_codex_version):
        raise CandidateTestTraceError("期望 Codex 版本必须是完整的 x.y.z 版本")
    if not SHA256_RE.fullmatch(expected_sha256):
        raise CandidateTestTraceError("期望测试事实映射 SHA-256 非法")
    value = _load_json(path, "测试事实冻结映射")
    if file_sha256(path) != expected_sha256:
        raise CandidateTestTraceError("测试事实冻结映射 SHA-256 不匹配")
    if not isinstance(value, dict):
        raise CandidateTestTraceError("测试事实冻结映射顶层必须是对象")
    _require_exact_keys(
        value,
        required={
            "schema_version",
            "codex_version",
            "fact_prefix",
            "forbidden_record_types",
            "tests",
        },
        description="测试事实冻结映射",
    )
    if value.get("schema_version") != MAP_SCHEMA_VERSION:
        raise CandidateTestTraceError("测试事实冻结映射 schema_version 不匹配")
    if value.get("codex_version") != expected_codex_version:
        raise CandidateTestTraceError("测试事实冻结映射 Codex 版本与 Campaign 目标不匹配")
    if value.get("fact_prefix") != FACT_PREFIX:
        raise CandidateTestTraceError("测试事实冻结映射 fact_prefix 不匹配")
    if value.get("forbidden_record_types") != sorted(FORBIDDEN_RECORD_TYPES):
        raise CandidateTestTraceError("冻结映射必须精确禁止三类原始线事实")
    raw_tests = value.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise CandidateTestTraceError("测试事实冻结映射 tests 必须是非空数组")

    tests: list[TestSpec] = []
    seen_tests: set[tuple[str, str]] = set()
    seen_facts: set[str] = set()
    for test_index, raw_test in enumerate(raw_tests):
        description = f"tests[{test_index}]"
        if not isinstance(raw_test, dict):
            raise CandidateTestTraceError(f"{description}必须是对象")
        _require_exact_keys(
            raw_test,
            required={
                "package",
                "name",
                "test_file",
                "test_file_sha256",
                "source_files",
                "facts",
            },
            description=description,
        )
        package = raw_test.get("package")
        name = raw_test.get("name")
        if not isinstance(package, str) or not package.strip():
            raise CandidateTestTraceError(f"{description}.package 不能为空")
        if not isinstance(name, str) or not TEST_NAME_RE.fullmatch(name):
            raise CandidateTestTraceError(f"{description}.name 格式非法")
        key = (package, name)
        if key in seen_tests:
            raise CandidateTestTraceError(f"冻结映射存在重复测试：{key}")
        seen_tests.add(key)
        test_file = str(_relative_path(raw_test.get("test_file"), f"{description}.test_file"))
        if not test_file.endswith("_test.go"):
            raise CandidateTestTraceError(f"{description}.test_file 必须是 _test.go")
        test_sha = raw_test.get("test_file_sha256")
        if not isinstance(test_sha, str) or not SHA256_RE.fullmatch(test_sha):
            raise CandidateTestTraceError(f"{description}.test_file_sha256 格式非法")

        raw_sources = raw_test.get("source_files")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise CandidateTestTraceError(f"{description}.source_files 必须是非空数组")
        sources: list[tuple[str, str]] = []
        source_paths: set[str] = set()
        for source_index, raw_source in enumerate(raw_sources):
            source_description = f"{description}.source_files[{source_index}]"
            if not isinstance(raw_source, dict):
                raise CandidateTestTraceError(f"{source_description}必须是对象")
            _require_exact_keys(
                raw_source,
                required={"path", "sha256"},
                description=source_description,
            )
            source_path = str(
                _relative_path(raw_source.get("path"), f"{source_description}.path")
            )
            if not source_path.endswith(".go") or source_path.endswith("_test.go"):
                raise CandidateTestTraceError(
                    f"{source_description}.path 必须是生产 Go 源文件"
                )
            source_sha = raw_source.get("sha256")
            if not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha):
                raise CandidateTestTraceError(f"{source_description}.sha256 格式非法")
            if source_path in source_paths:
                raise CandidateTestTraceError(f"{description}.source_files 路径重复")
            source_paths.add(source_path)
            sources.append((source_path, source_sha))

        raw_facts = raw_test.get("facts")
        if not isinstance(raw_facts, list) or not raw_facts:
            raise CandidateTestTraceError(f"{description}.facts 必须是非空数组")
        facts: list[FactSpec] = []
        for fact_index, raw_fact in enumerate(raw_facts):
            fact_description = f"{description}.facts[{fact_index}]"
            if not isinstance(raw_fact, dict):
                raise CandidateTestTraceError(f"{fact_description}必须是对象")
            _require_exact_keys(
                raw_fact,
                required={
                    "fact_id",
                    "scenario_id",
                    "record_type",
                    "trace_kind",
                    "data_keys",
                    "required_source_kinds",
                },
                description=fact_description,
            )
            fact_id = raw_fact.get("fact_id")
            scenario_id = raw_fact.get("scenario_id")
            record_type = raw_fact.get("record_type")
            trace_kind = raw_fact.get("trace_kind")
            data_keys = raw_fact.get("data_keys")
            source_kinds = raw_fact.get("required_source_kinds")
            if not isinstance(fact_id, str) or not FACT_ID_RE.fullmatch(fact_id):
                raise CandidateTestTraceError(f"{fact_description}.fact_id 格式非法")
            if fact_id in seen_facts:
                raise CandidateTestTraceError(f"冻结映射存在重复 fact_id：{fact_id}")
            seen_facts.add(fact_id)
            if not isinstance(scenario_id, str) or not SCENARIO_RE.fullmatch(scenario_id):
                raise CandidateTestTraceError(f"{fact_description}.scenario_id 格式非法")
            if record_type in FORBIDDEN_RECORD_TYPES or record_type not in ALLOWED_RECORD_TYPES:
                raise CandidateTestTraceError(
                    f"{fact_description}.record_type 不是允许的抽象事实：{record_type}"
                )
            if trace_kind not in ALLOWED_TRACE_KINDS:
                raise CandidateTestTraceError(f"{fact_description}.trace_kind 非法")
            if (
                not isinstance(data_keys, list)
                or not data_keys
                or len(data_keys) != len(set(data_keys))
                or any(not isinstance(item, str) or not DATA_KEY_RE.fullmatch(item) for item in data_keys)
            ):
                raise CandidateTestTraceError(f"{fact_description}.data_keys 格式非法")
            if (
                not isinstance(source_kinds, list)
                or not source_kinds
                or len(source_kinds) != len(set(source_kinds))
                or any(item not in ALLOWED_RAW_SOURCE_KINDS for item in source_kinds)
                or "relay_binary" not in source_kinds
            ):
                raise CandidateTestTraceError(
                    f"{fact_description}.required_source_kinds 必须包含 relay_binary"
                )
            facts.append(
                FactSpec(
                    fact_id=fact_id,
                    scenario_id=scenario_id,
                    record_type=record_type,
                    trace_kind=trace_kind,
                    data_keys=tuple(data_keys),
                    required_source_kinds=tuple(source_kinds),
                )
            )
        tests.append(
            TestSpec(
                package=package,
                name=name,
                test_file=test_file,
                test_file_sha256=test_sha,
                source_files=tuple(sources),
                facts=tuple(facts),
            )
        )
    return value, tuple(tests)


def verify_source_snapshot(source_root: Path, tests: Sequence[TestSpec]) -> list[dict[str, str]]:
    """核对冻结测试文件和它们明确绑定的生产源码摘要。"""

    if source_root.is_symlink() or not source_root.is_dir():
        raise CandidateTestTraceError("source-root 必须是非符号链接目录")
    expected: dict[str, str] = {}
    for test in tests:
        for path, digest in ((test.test_file, test.test_file_sha256), *test.source_files):
            previous = expected.setdefault(path, digest)
            if previous != digest:
                raise CandidateTestTraceError(f"同一源码路径绑定了不同摘要：{path}")
    files: list[dict[str, str]] = []
    for relative_text in sorted(expected):
        relative = _relative_path(relative_text, "源码路径")
        path = _resolve_existing_file(source_root, relative, "部署源码快照文件")
        actual = file_sha256(path)
        if actual != expected[relative_text]:
            raise CandidateTestTraceError(f"部署源码快照 SHA-256 不匹配：{relative_text}")
        files.append({"path": relative_text, "sha256": actual})
    return files


def load_capture_manifest(
    path: Path,
    evidence_root: Path,
    expected_codex_version: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """校验基础 manifest 以及它声明的每一份原始文件。"""

    manifest_relative = _relative_existing_file(evidence_root, path, "基础 capture manifest")
    manifest = _load_json(path, "基础 capture manifest")
    try:
        artifacts = _validate_capture_manifest(manifest, expected_codex_version)
    except ValueError as error:
        raise CandidateTestTraceError(str(error)) from error
    for index, artifact in enumerate(artifacts):
        relative = _relative_path(artifact["path"], f"artifacts[{index}].path")
        artifact_path = _resolve_existing_file(
            evidence_root,
            relative,
            f"artifacts[{index}]",
        )
        if file_sha256(artifact_path) != artifact["sha256"]:
            raise CandidateTestTraceError(f"artifact SHA-256 不匹配：{artifact['path']}")
        allowed = ALLOWED_PARSERS_BY_KIND.get(artifact["kind"], frozenset())
        if artifact["parser"] not in allowed:
            raise CandidateTestTraceError(
                f"artifact kind/parser 不受支持：{artifact['kind']}/{artifact['parser']}"
            )
    return manifest, artifacts, manifest_relative.as_posix()


def _parse_fact_marker(
    output: str,
    *,
    event_test: str | None,
    event_package: str,
    specs_by_fact: Mapping[str, tuple[TestSpec, FactSpec]],
) -> tuple[TestSpec, FactSpec, dict[str, Any]] | None:
    stripped = output.strip()
    marker_index = stripped.find(FACT_PREFIX)
    if marker_index < 0:
        return None
    if stripped[:marker_index].strip():
        # testing 框架可以在 marker 前添加文件:行号，但不能添加任意说明文本。
        prefix = stripped[:marker_index].strip()
        if not re.fullmatch(r"[A-Za-z0-9_./-]+:\d+:", prefix):
            raise CandidateTestTraceError("事实 marker 前出现非 testing 行号前缀")
    if event_test is None:
        raise CandidateTestTraceError("事实 marker 必须属于精确测试事件")
    payload = _loads_json(stripped[marker_index + len(FACT_PREFIX) :], "测试事实 marker")
    if not isinstance(payload, dict):
        raise CandidateTestTraceError("测试事实 marker 顶层必须是对象")
    _require_exact_keys(
        payload,
        required={"schema_version", "fact_id", "scenario_id", "record_type", "data"},
        description="测试事实 marker",
    )
    if payload.get("schema_version") != FACT_SCHEMA_VERSION:
        raise CandidateTestTraceError("测试事实 marker schema_version 不匹配")
    fact_id = payload.get("fact_id")
    if not isinstance(fact_id, str) or fact_id not in specs_by_fact:
        raise CandidateTestTraceError(f"测试输出了冻结映射外 fact_id：{fact_id!r}")
    test_spec, fact_spec = specs_by_fact[fact_id]
    if (event_package, event_test) != test_spec.key:
        raise CandidateTestTraceError(
            f"fact_id {fact_id} 由错误测试输出：{event_package}/{event_test}"
        )
    if payload.get("scenario_id") != fact_spec.scenario_id:
        raise CandidateTestTraceError(f"fact_id {fact_id} 的 scenario_id 不匹配")
    if payload.get("record_type") != fact_spec.record_type:
        raise CandidateTestTraceError(f"fact_id {fact_id} 的 record_type 不匹配")
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise CandidateTestTraceError(f"fact_id {fact_id} 的 data 必须是非空对象")
    if set(data) != set(fact_spec.data_keys):
        raise CandidateTestTraceError(
            f"fact_id {fact_id} 的 data 字段不闭合："
            f"expected={sorted(fact_spec.data_keys)} actual={sorted(data)}"
        )
    return test_spec, fact_spec, dict(data)


def parse_go_test_json(
    path: Path,
    tests: Sequence[TestSpec],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """只接受非缓存、完整且精确通过的 ``go test -json`` 原始日志。"""

    specs_by_test = {test.key: test for test in tests}
    specs_by_fact = {
        fact.fact_id: (test, fact)
        for test in tests
        for fact in test.facts
    }
    mapped_packages = {test.package for test in tests}
    run_counts: Counter[tuple[str, str]] = Counter()
    pass_counts: Counter[tuple[str, str]] = Counter()
    package_pass_counts: Counter[str] = Counter()
    facts: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise CandidateTestTraceError(f"go test 日志无法读取：{error}") from error
    if not lines:
        raise CandidateTestTraceError("go test 日志为空")
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise CandidateTestTraceError(f"go test 日志第 {line_number} 行为空")
        event = _loads_json(line, f"go test 日志第 {line_number} 行")
        if not isinstance(event, dict):
            raise CandidateTestTraceError(f"go test 日志第 {line_number} 行必须是对象")
        action = event.get("Action")
        package = event.get("Package")
        test_name = event.get("Test")
        if not isinstance(action, str) or not isinstance(package, str) or not package:
            raise CandidateTestTraceError(f"go test 日志第 {line_number} 行缺少 Action/Package")
        if test_name is not None and not isinstance(test_name, str):
            raise CandidateTestTraceError(f"go test 日志第 {line_number} 行 Test 非字符串")
        output = event.get("Output")
        if output is not None and not isinstance(output, str):
            raise CandidateTestTraceError(f"go test 日志第 {line_number} 行 Output 非字符串")
        if output and "(cached)" in output:
            raise CandidateTestTraceError("go test 日志包含缓存结果，必须使用 -count=1")
        if action == "fail":
            raise CandidateTestTraceError(
                f"go test 日志包含失败事件：{package}/{test_name or '<package>'}"
            )
        if action == "skip" and (package, test_name or "") in specs_by_test:
            raise CandidateTestTraceError(f"冻结测试被跳过：{package}/{test_name}")
        if package in mapped_packages and test_name and TEST_NAME_RE.fullmatch(test_name):
            key = (package, test_name)
            if key not in specs_by_test:
                raise CandidateTestTraceError(
                    f"go test 日志包含冻结映射外顶层测试：{package}/{test_name}"
                )
            if action == "run":
                run_counts[key] += 1
            elif action == "pass":
                pass_counts[key] += 1
        if action == "pass" and test_name is None and package in mapped_packages:
            package_pass_counts[package] += 1
        if action == "output" and output:
            parsed = _parse_fact_marker(
                output,
                event_test=test_name,
                event_package=package,
                specs_by_fact=specs_by_fact,
            )
            if parsed is not None:
                _, fact_spec, data = parsed
                if fact_spec.fact_id in facts:
                    raise CandidateTestTraceError(
                        f"go test 日志重复输出 fact_id：{fact_spec.fact_id}"
                    )
                facts[fact_spec.fact_id] = data
        events.append(event)

    for test in tests:
        if run_counts[test.key] != 1 or pass_counts[test.key] != 1:
            raise CandidateTestTraceError(
                f"冻结测试必须恰好 run/pass 一次：{test.package}/{test.name} "
                f"run={run_counts[test.key]} pass={pass_counts[test.key]}"
            )
        for fact in test.facts:
            if fact.fact_id not in facts:
                raise CandidateTestTraceError(
                    f"已通过测试缺少冻结事实输出：{test.package}/{test.name}/{fact.fact_id}"
                )
    for package in sorted(mapped_packages):
        if package_pass_counts[package] != 1:
            raise CandidateTestTraceError(
                f"冻结测试包必须恰好通过一次：{package} pass={package_pass_counts[package]}"
            )
    return facts, events


def _artifact_sources_for_fact(
    artifacts: Sequence[Mapping[str, Any]],
    fact: FactSpec,
) -> list[str]:
    sources: list[str] = []
    for kind in fact.required_source_kinds:
        matches = sorted(
            artifact["path"]
            for artifact in artifacts
            if artifact.get("kind") == kind
            and fact.scenario_id in artifact.get("scenario_ids", [])
        )
        if not matches:
            raise CandidateTestTraceError(
                f"fact_id {fact.fact_id} 缺少场景 {fact.scenario_id} 的 {kind} 原始证据"
            )
        sources.extend(matches)
    return list(dict.fromkeys(sources))


def _ensure_output_target(root: Path, relative: PurePosixPath, description: str) -> Path:
    root_resolved = root.resolve(strict=True)
    current = root_resolved
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise CandidateTestTraceError(f"{description}父目录包含符号链接：{relative}")
        if current.exists() and not current.is_dir():
            raise CandidateTestTraceError(f"{description}父路径不是目录：{relative}")
    target = root_resolved.joinpath(*relative.parts)
    if target.exists() or target.is_symlink():
        raise CandidateTestTraceError(f"{description}已存在，拒绝覆盖：{relative}")
    return target


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def generate_test_traces(
    *,
    source_root: Path,
    evidence_root: Path,
    capture_manifest_path: Path,
    go_test_artifact: str,
    mapping_path: Path,
    profile_path: Path,
    trace_dir: str,
    output_manifest: str,
    output_receipt: str,
    expected_codex_version: str,
    expected_mapping_sha256: str,
    expected_profile_sha256: str,
) -> dict[str, Any]:
    """生成 trace、新 capture manifest 与摘要回执，所有输出均拒绝覆盖。"""

    mapping, tests = load_mapping(
        mapping_path,
        expected_codex_version=expected_codex_version,
        expected_sha256=expected_mapping_sha256,
    )
    mapping_sha = file_sha256(mapping_path)
    if profile_path.is_symlink() or not profile_path.is_file():
        raise CandidateTestTraceError("冻结规则画像必须是非符号链接普通文件")
    profile_sha = file_sha256(profile_path)
    if (
        not SHA256_RE.fullmatch(expected_profile_sha256)
        or profile_sha != expected_profile_sha256
    ):
        raise CandidateTestTraceError("冻结规则画像 SHA-256 不匹配")
    profile = _load_json(profile_path, "冻结规则画像")
    if (
        not isinstance(profile, dict)
        or profile.get("codex_version") != expected_codex_version
    ):
        raise CandidateTestTraceError("冻结规则画像 Codex 版本与 Campaign 目标不匹配")
    source_files = verify_source_snapshot(source_root, tests)
    manifest, artifacts, base_manifest_relative = load_capture_manifest(
        capture_manifest_path,
        evidence_root,
        expected_codex_version,
    )

    go_relative = _relative_path(go_test_artifact, "go-test-artifact")
    go_matches = [artifact for artifact in artifacts if artifact["path"] == go_test_artifact]
    if len(go_matches) != 1:
        raise CandidateTestTraceError("go-test-artifact 必须在基础 manifest 中恰好声明一次")
    go_artifact = go_matches[0]
    if (
        go_artifact.get("kind") != "stdout_log"
        or go_artifact.get("parser") != "opaque_bound_source"
    ):
        raise CandidateTestTraceError(
            "go-test-artifact 必须声明为 stdout_log/opaque_bound_source"
        )
    required_scenarios = {fact.scenario_id for test in tests for fact in test.facts}
    if not required_scenarios.issubset(set(go_artifact.get("scenario_ids", []))):
        raise CandidateTestTraceError("go-test-artifact 未声明全部冻结事实场景")
    go_path = _resolve_existing_file(evidence_root, go_relative, "go test 原始日志")
    go_sha = file_sha256(go_path)
    if go_sha != go_artifact["sha256"]:
        raise CandidateTestTraceError("go test 原始日志 SHA-256 与 manifest 不匹配")
    facts, _ = parse_go_test_json(go_path, tests)

    trace_dir_path = _relative_path(trace_dir, "trace-dir")
    manifest_relative = _relative_path(output_manifest, "output-manifest")
    receipt_relative = _relative_path(output_receipt, "output-receipt")
    records_by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trace_kind_by_scenario: dict[str, str] = {}
    test_by_fact = {
        fact.fact_id: test
        for test in tests
        for fact in test.facts
    }
    spec_by_fact = {
        fact.fact_id: fact
        for test in tests
        for fact in test.facts
    }
    for fact_id in sorted(facts):
        test = test_by_fact[fact_id]
        fact = spec_by_fact[fact_id]
        previous_kind = trace_kind_by_scenario.setdefault(fact.scenario_id, fact.trace_kind)
        if previous_kind != fact.trace_kind:
            raise CandidateTestTraceError(
                f"同一场景不能混用 process/websocket trace：{fact.scenario_id}"
            )
        raw_sources = _artifact_sources_for_fact(artifacts, fact)
        data = dict(facts[fact_id])
        data["_test_proof"] = {
            "go_test_log_sha256": go_sha,
            "mapping_sha256": mapping_sha,
            "package": test.package,
            "source_files": [
                {"path": path, "sha256": digest}
                for path, digest in test.source_files
            ],
            "test_file": test.test_file,
            "test_file_sha256": test.test_file_sha256,
            "test_name": test.name,
        }
        records_by_scenario[fact.scenario_id].append(
            {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "record_id": f"go-test:{fact_id}:{go_sha[:16]}",
                "scenario_id": fact.scenario_id,
                "record_type": fact.record_type,
                "data": data,
                "source_artifacts": [go_test_artifact, *raw_sources],
            }
        )

    trace_payloads: list[tuple[PurePosixPath, bytes, dict[str, Any]]] = []
    for scenario_id in sorted(records_by_scenario):
        records = sorted(records_by_scenario[scenario_id], key=lambda item: item["record_id"])
        payload = b"".join(_canonical_json_bytes(record, jsonl=True) for record in records)
        relative = trace_dir_path / scenario_id / "go-test-facts.jsonl"
        kind = trace_kind_by_scenario[scenario_id]
        trace_payloads.append(
            (
                relative,
                payload,
                {
                    "path": relative.as_posix(),
                    "sha256": bytes_sha256(payload),
                    "kind": kind,
                    "scenario_id": scenario_id,
                    "record_count": len(records),
                },
            )
        )

    all_output_relatives = [relative for relative, _, _ in trace_payloads]
    all_output_relatives.extend([manifest_relative, receipt_relative])
    if len(set(all_output_relatives)) != len(all_output_relatives):
        raise CandidateTestTraceError("输出路径互相冲突")
    targets = {
        relative: _ensure_output_target(evidence_root, relative, "生成输出")
        for relative in all_output_relatives
    }

    generated_artifacts = [metadata for _, _, metadata in trace_payloads]
    output_manifest_value = {
        "schema_version": manifest["schema_version"],
        "codex_version": manifest["codex_version"],
        "capture_id": f"{manifest['capture_id']}:go-test-{go_sha[:12]}",
        "status": "complete",
        "artifacts": [
            *artifacts,
            *[
                {
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "kind": item["kind"],
                    "parser": "observation_jsonl",
                    "scenario_ids": [item["scenario_id"]],
                    "labels": {
                        "generator": "candidate_test_trace.py",
                        "go_test_log_sha256": go_sha,
                        "mapping_sha256": mapping_sha,
                    },
                }
                for item in generated_artifacts
            ],
        ],
    }
    try:
        _validate_capture_manifest(
            output_manifest_value,
            expected_codex_version,
        )
    except ValueError as error:
        raise CandidateTestTraceError(str(error)) from error
    manifest_payload = _canonical_json_bytes(output_manifest_value) + b"\n"
    manifest_sha = bytes_sha256(manifest_payload)

    source_profile_relative = _relative_existing_file(
        source_root,
        profile_path,
        "冻结规则画像",
    ).as_posix()
    source_mapping_relative = _relative_existing_file(
        source_root,
        mapping_path,
        "测试事实冻结映射",
    ).as_posix()
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "codex_version": expected_codex_version,
        "status": "pass",
        "base_capture_manifest": {
            "path": base_manifest_relative,
            "sha256": file_sha256(capture_manifest_path),
        },
        "mapping": {"path": source_mapping_relative, "sha256": mapping_sha},
        "profile": {"path": source_profile_relative, "sha256": profile_sha},
        "go_test_log": {"artifact_path": go_test_artifact, "sha256": go_sha},
        "source_snapshot": {"files": source_files},
        "tests": [
            {
                "package": test.package,
                "name": test.name,
                "status": "pass",
                "fact_ids": [fact.fact_id for fact in test.facts],
            }
            for test in tests
        ],
        "generated": {
            "capture_manifest": {
                "path": manifest_relative.as_posix(),
                "sha256": manifest_sha,
            },
            "trace_artifacts": generated_artifacts,
        },
    }
    receipt_payload = _canonical_json_bytes(receipt) + b"\n"

    for relative, payload, _ in trace_payloads:
        _write_exclusive(targets[relative], payload)
    _write_exclusive(targets[manifest_relative], manifest_payload)
    _write_exclusive(targets[receipt_relative], receipt_payload)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从已通过的 go test -json 原始日志生成候选抽象 trace",
    )
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--capture-manifest", required=True, type=Path)
    parser.add_argument("--go-test-artifact", required=True)
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--expected-codex-version", required=True)
    parser.add_argument("--expected-mapping-sha256", required=True)
    parser.add_argument("--expected-profile-sha256", required=True)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--output-manifest", required=True)
    parser.add_argument("--output-receipt", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_root = args.source_root.resolve()
    mapping = args.mapping or source_root / DEFAULT_MAPPING_RELATIVE_PATH
    profile = args.profile or source_root / DEFAULT_PROFILE_RELATIVE_PATH
    try:
        receipt = generate_test_traces(
            source_root=source_root,
            evidence_root=args.evidence_root,
            capture_manifest_path=args.capture_manifest,
            go_test_artifact=args.go_test_artifact,
            mapping_path=mapping,
            profile_path=profile,
            trace_dir=args.trace_dir,
            output_manifest=args.output_manifest,
            output_receipt=args.output_receipt,
            expected_codex_version=args.expected_codex_version,
            expected_mapping_sha256=args.expected_mapping_sha256,
            expected_profile_sha256=args.expected_profile_sha256,
        )
    except CandidateTestTraceError as error:
        print(f"candidate-test-trace: fail: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
