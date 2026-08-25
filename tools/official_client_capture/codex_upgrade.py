#!/usr/bin/env python3
"""编排 Codex CLI 版本升级的源码扫描、抓包、出站面比较与覆盖报告。"""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import string
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import (
    ConfigurationError,
    track_models_for_version,
)
from tools.official_client_capture.capturelib.security import (
    ensure_private_directory,
    file_sha256,
    normalize_json_shape,
    secure_write_json,
    secure_write_text,
)
from tools.official_client_capture.acceptance_contract import (
    AcceptanceContractError,
    LEGACY_RESULTS_SCHEMAS,
    MODE_DUAL_WIRE,
    RESULTS_SCHEMA_V2,
    build_contract_payload as build_acceptance_contract,
    contract_sha256 as acceptance_contract_sha256,
    expected_check_ids_for_side,
    load_profile as load_acceptance_profile,
    repository_profile_path as acceptance_profile_path,
    verify_frozen_contract,
)
from tools.official_client_capture.assertion_gate import (
    AssertionGateError,
    BUNDLE_DIR_NAME as ASSERTION_BUNDLE_DIR_NAME,
    run_assertion_gate,
    validate_gate_receipt,
)
from tools.official_client_capture.candidate_evidence_guard import (
    scan_files_for_secrets,
)
from tools.official_client_capture.candidate_rule_assertion import (
    ASSERTION_SCHEMA_VERSION as MACHINE_ASSERTION_SCHEMA,
    build_assertion_command as build_machine_assertion_command,
    command_sha256 as machine_command_sha256,
    load_observations as load_assertion_observations,
    source_spec_section_sha256,
)
from tools.official_client_capture.codex_upgrade_environment_probe import (
    EnvironmentProbeError,
    ProbeArguments as EnvironmentProbeArguments,
    STATE_FILES as ENVIRONMENT_STATE_FILES,
    run_probe as run_environment_probe,
)
from tools.official_client_capture import codex_upgrade_gate_receipt as external_gate_receipt
from tools.official_client_capture.codex_upgrade_receipt_finalizer import (
    CLIENT_BINDING_SCHEMA as FINALIZED_CLIENT_BINDING_SCHEMA,
    OBSERVED_PROFILE_SCHEMA as FINALIZED_OBSERVED_PROFILE_SCHEMA,
    RESTORATION_INPUTS,
    RESTORATION_SCHEMA as FINALIZED_RESTORATION_SCHEMA,
    ReceiptFinalizerError,
    finalize_restoration,
    finalize_scenario,
    replay_receipt,
)
from tools.official_client_capture.scenario_receipts import (
    FACTS_SCHEMA_VERSION as SCENARIO_FACTS_SCHEMA,
    SCHEMA_VERSION as SCENARIO_RECEIPT_SCHEMA,
    SUPPORTED_SCENARIOS as SCENARIO_RECEIPT_SCENARIOS,
    ScenarioReceiptError,
    validate_facts_document as validate_scenario_facts_document,
    validate_receipt as validate_scenario_receipt,
)
from tools.official_client_capture.model_condition_receipts import (
    ModelConditionReceiptError,
    validate_receipt as validate_model_condition_receipt,
)
from tools.official_client_capture.pcap_clienthello import (
    iter_packets,
    parse_client_hello,
    tcp_payload,
)
from tools.official_client_capture.relay_extract import (
    H2_PREFACE,
    parse_h1_stream,
    parse_h2_stream,
    parse_ws_frames,
)


RULE_SCHEMA = "codex-egress-rule-manifest/v1"
REPORT_SCHEMA = "codex-upgrade-report/v1"
SURFACE_SCHEMA = "codex-egress-surface/v1"
SOURCE_SCHEMA = "codex-egress-source-inventory/v1"
EXTRA_JOB_SCHEMA = "codex-upgrade-extra-jobs/v1"
CAMPAIGN_SCHEMA = "codex-upgrade-campaign/v1"
MIGRATION_SCHEMA = "codex-upgrade-rule-migration/v1"
ASSERTION_TEMPLATE_SCHEMA = "codex-egress-rule-assertion-template/v1"
ASSERTION_PROFILE_SCHEMA = "codex-candidate-rule-expectations/v1"
PROFILE_SCHEMA = "codex-egress-profile/v1"
OBSERVED_PROFILE_SCHEMA = FINALIZED_OBSERVED_PROFILE_SCHEMA
CLIENT_BINDING_SCHEMA = FINALIZED_CLIENT_BINDING_SCHEMA
CLIENT_REQUEST_PROOF_SCHEMA = "codex-egress-client-request-evidence/v1"
CLIENT_RESPONSE_PROOF_SCHEMA = "codex-egress-client-response-evidence/v1"
RESTORATION_SCHEMA = FINALIZED_RESTORATION_SCHEMA
CAPTURE_ATTEMPT_SCHEMA = "codex-upgrade-capture-attempt/v1"
CAPTURE_RESERVATION_SCHEMA = "codex-upgrade-capture-reservation/v1"
SEAL_FAILURE_SCHEMA = "codex-upgrade-seal-failure/v1"
SEAL_PREVIEW_SCHEMA = "codex-upgrade-seal-preview/v1"
SCENARIO_SCHEMA = "codex-upgrade-scenarios/v1"
STAGE_SCHEMA = "codex-upgrade-stage-result/v1"
COMPARISON_SCHEMA = "codex-upgrade-comparison/v1"
ACCEPTANCE_SCHEMA = "codex-upgrade-acceptance/v1"
MIGRATION_CLASSIFICATIONS = {
    "inherit",
    "change",
    "add",
    "delete",
    "condition_change",
    "blocked",
}
ASSERTION_STATUSES = {"pass", "fail", "blocked", "not_applicable"}
REQUIRED_CLIENT_BINDINGS = frozenset({"kilo-compatible", "kilo-responses"})
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CODEX_USER_AGENT_VERSION_RE = re.compile(
    r"(?:codex_exec|codex-tui|codex_cli_rs)/(\d+\.\d+\.\d+)"
    r"|\((?:codex_exec|codex-tui|codex_cli_rs);\s*(\d+\.\d+\.\d+)\)"
)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
RUN_NONCE_RE = SHA256_RE
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_ABSOLUTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+:-]+$")
IMMUTABLE_IMAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[a-f0-9]{64}$"
)
IMAGE_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
RULE_RE = re.compile(r"^SPEC-[A-Z0-9]+-\d{3}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")
QUOTED_STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
NETWORK_SUFFIXES = {".rs", ".toml"}
SKIP_DIRECTORIES = {
    ".git",
    "target",
    "node_modules",
    "vendor",
    "fixtures",
    "snapshots",
}
NETWORK_MARKERS = (
    "reqwest",
    "hyper",
    "tungstenite",
    "websocket",
    "ClientBuilder",
    "Request::builder",
    "RequestBuilder",
    "send_request",
    "connect_async",
    ".request(",
    ".execute(",
    ".send(",
)
ENDPOINT_MARKERS = (
    "backend-api",
    "/responses",
    "/models",
    "/compact",
    "/wham",
    "/search",
    "/images",
    "/realtime",
    "/files",
    "api.openai.com",
    "chatgpt.com",
    "auth.openai.com",
    "oaiusercontent.com",
)
NETWORK_PACKAGES = {
    "h2",
    "http",
    "http-body",
    "http-body-util",
    "hyper",
    "hyper-util",
    "native-tls",
    "openssl",
    "reqwest",
    "rustls",
    "tokio-rustls",
    "tokio-tungstenite",
    "tungstenite",
    "webpki-roots",
}
MAX_JSON_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class Job:
    """一次可独立恢复的抓包任务。"""

    job_id: str
    phase: str
    suites: tuple[str, ...]
    description: str
    steps: tuple[dict[str, Any], ...]
    evidence_roots: tuple[str, ...]
    covers: tuple[str, ...]
    scenario_ids: tuple[str, ...] = ()
    required: bool = True
    # SCN-REALITY-01：本 job 必须为哪些场景产出真实性收据。不复用 scenario_ids——
    # 后者表达「该 job 覆盖哪些场景」，official-core 一个 job 就覆盖 9 个场景，
    # 不可能逐个产收据；真实性门禁只约束已证实失效的目标场景。
    required_scenario_receipts: tuple[str, ...] = ()
    track: str = "main"
    model_id: str = ""
    expected_use_responses_lite: bool = False
    required_model_receipt: bool = False


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job_execution_sha256(job: Job) -> str:
    """绑定真实执行定义，同时允许批准场景重新映射规则与场景说明。

    required_scenario_receipts 不属于「场景说明」而属于执行契约：它决定这个 job
    算不算成功。若不入指纹，同一个 execution_sha256 下门禁要求可被悄改，而
    _validate_capture_job_results 与 _prior_complete_results 都检测不到。
    """

    return _fingerprint(
        {
            "id": job.job_id,
            "phase": job.phase,
            "suites": list(job.suites),
            "steps": [dict(step) for step in job.steps],
            "evidence_roots": list(job.evidence_roots),
            "required": job.required,
            "required_scenario_receipts": list(job.required_scenario_receipts),
            "track": getattr(job, "track", "main"),
            "model_id": getattr(job, "model_id", ""),
            "expected_use_responses_lite": getattr(
                job, "expected_use_responses_lite", False
            ),
            "required_model_receipt": getattr(job, "required_model_receipt", False),
        }
    )


def _is_rfc3339_timestamp(value: Any) -> bool:
    """判断时间是否为带时区的 RFC 3339／ISO 8601 字符串。"""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _rfc3339_datetime(value: Any, label: str) -> datetime:
    """解析带时区时间，并统一为可比较的 datetime。"""

    if not _is_rfc3339_timestamp(value):
        raise ConfigurationError(f"{label} 不是带时区 RFC 3339 时间。")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _utc_now() -> str:
    """返回带微秒精度的 UTC RFC 3339 时间。"""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _normalized_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _source_entry(kind: str, relative: str, value: str) -> dict[str, str]:
    core = {"kind": kind, "file": relative, "value": value}
    return {**core, "fingerprint": _fingerprint(core)}


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if path.name == "Cargo.lock" or path.suffix in NETWORK_SUFFIXES:
            yield path


def _cargo_entries(path: Path, relative: str) -> list[dict[str, str]]:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"无法解析 {path}：{error}") from error
    entries: list[dict[str, str]] = []
    for package in data.get("package", []):
        name = package.get("name")
        if name not in NETWORK_PACKAGES:
            continue
        value = "|".join(
            str(package.get(key, ""))
            for key in ("name", "version", "source", "checksum")
        )
        entries.append(_source_entry("network_dependency", relative, value))
    return entries


def scan_source_tree(root: Path, version: str) -> dict[str, Any]:
    """生成稳定的出站源码线索清单；新增线索必须人工分类。"""

    if not root.is_dir() or root.is_symlink():
        raise ConfigurationError(f"源码目录不存在或不可信：{root}")
    entries: dict[str, dict[str, str]] = {}
    scanned_files = 0
    network_files = 0
    for path in _iter_source_files(root):
        scanned_files += 1
        relative = path.relative_to(root).as_posix()
        if path.name == "Cargo.lock":
            for entry in _cargo_entries(path, relative):
                entries[entry["fingerprint"]] = entry
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        file_matched = False
        for line in text.splitlines():
            normalized = _normalized_line(line)
            if not normalized:
                continue
            if any(marker in normalized for marker in NETWORK_MARKERS):
                entry = _source_entry("network_callsite", relative, normalized[:800])
                entries[entry["fingerprint"]] = entry
                file_matched = True
            for match in QUOTED_STRING_RE.finditer(line):
                literal = match.group(1).replace(r'\"', '"')
                if (
                    literal.startswith(("http://", "https://", "/"))
                    and any(marker in literal for marker in ENDPOINT_MARKERS)
                ):
                    entry = _source_entry("endpoint_literal", relative, literal)
                    entries[entry["fingerprint"]] = entry
                    file_matched = True
                if (
                    HEADER_NAME_RE.fullmatch(literal)
                    and any(
                        token in normalized.lower()
                        for token in ("header", ".insert(", ".append(")
                    )
                ):
                    entry = _source_entry(
                        "header_literal",
                        relative,
                        literal.lower().replace("_", "-"),
                    )
                    entries[entry["fingerprint"]] = entry
                    file_matched = True
        if file_matched:
            network_files += 1
            entry = _source_entry("network_file", relative, file_sha256(path))
            entries[entry["fingerprint"]] = entry
    return {
        "schema_version": SOURCE_SCHEMA,
        "codex_version": version,
        "root": str(root.resolve()),
        "files_scanned": scanned_files,
        "network_files": network_files,
        "entry_count": len(entries),
        "entries": sorted(
            entries.values(),
            key=lambda item: (item["kind"], item["file"], item["value"]),
        ),
    }


def compare_inventory(
    baseline: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    baseline_by_id = {
        item["fingerprint"]: item for item in baseline.get("entries", [])
    }
    target_by_id = {
        item["fingerprint"]: item for item in target.get("entries", [])
    }
    added = sorted(
        (target_by_id[key] for key in target_by_id.keys() - baseline_by_id.keys()),
        key=lambda item: (item["kind"], item["file"], item["value"]),
    )
    removed = sorted(
        (baseline_by_id[key] for key in baseline_by_id.keys() - target_by_id.keys()),
        key=lambda item: (item["kind"], item["file"], item["value"]),
    )
    return {
        "baseline_version": baseline.get("codex_version"),
        "target_version": target.get("codex_version"),
        "added_count": len(added),
        "removed_count": len(removed),
        "added": added,
        "removed": removed,
    }


def _normalized_path(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path or "/"
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not query:
        return path
    return path + "?" + "&".join(name for name, _ in query)


def _header_names(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(name) for name in value]
    names: list[str] = []
    if not isinstance(value, list):
        return names
    for item in value:
        if isinstance(item, (list, tuple)) and item:
            names.append(str(item[0]))
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def _header_value(value: Any, expected_name: str) -> str | None:
    expected = expected_name.lower()
    if isinstance(value, dict):
        for name, item_value in value.items():
            if str(name).lower() == expected:
                return str(item_value)
        return None
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            name, item_value = item[0], item[1]
        elif isinstance(item, dict):
            name, item_value = item.get("name"), item.get("value")
        else:
            continue
        if str(name).lower() == expected:
            return str(item_value)
    return None


def _normalized_host(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = urllib.parse.urlsplit(
            raw if "://" in raw else f"//{raw}"
        )
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid-host>"
    if not hostname or len(hostname) > 253:
        return "<invalid-host>"
    try:
        normalized = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return "<invalid-host>"
    if not re.fullmatch(r"[a-z0-9._:-]+", normalized):
        return "<invalid-host>"
    if port is None:
        return normalized
    rendered = f"[{normalized}]" if ":" in normalized else normalized
    return f"{rendered}:{port}"


def _request_host(request: dict[str, Any], path: str) -> str:
    headers = request.get("headers")
    candidates = (
        request.get("host"),
        request.get("authority"),
        _header_value(headers, ":authority"),
        _header_value(headers, "host"),
        path if "://" in path else None,
    )
    for candidate in candidates:
        normalized = _normalized_host(candidate)
        if normalized:
            return normalized
    return "<unknown>"


def _surface(kind: str, fields: dict[str, Any], source: str) -> dict[str, Any]:
    core = {"kind": kind, **fields}
    return {**core, "fingerprint": _fingerprint(core), "sources": [source]}


def _request_surface(request: dict[str, Any], source: str) -> dict[str, Any] | None:
    request_line = request.get("request_line")
    method = request.get("method")
    path = request.get("path") or request.get("url")
    protocol = request.get("http_version") or request.get("protocol")
    if isinstance(request_line, str):
        parts = request_line.split(" ", 2)
        if len(parts) == 3:
            method, path, protocol = parts
    if not isinstance(method, str) or not isinstance(path, str):
        return None
    headers = request.get("header_names_in_order")
    if not isinstance(headers, list):
        headers = _header_names(request.get("headers"))
    json_shape = request.get("json_shape")
    if json_shape is None:
        json_shape = request.get("shape")
    body = request.get("body")
    if json_shape is None and isinstance(body, dict):
        json_shape = body.get("shape")
        if json_shape is None and isinstance(body.get("text"), str):
            try:
                json_shape = normalize_json_shape(json.loads(body["text"]))
            except json.JSONDecodeError:
                json_shape = None
    body_shape = (
        _fingerprint({"shape": json_shape})
        if isinstance(json_shape, (dict, list))
        else None
    )
    return _surface(
        "http_request",
        {
            "host": _request_host(request, path),
            "method": method.upper(),
            "path": _normalized_path(path),
            "protocol": str(protocol or "unknown"),
            "header_names": [str(item) for item in headers],
            "body_shape_sha256": body_shape,
        },
        source,
    )


def _h2_request_surface(
    frame: dict[str, Any], source: str
) -> dict[str, Any] | None:
    if frame.get("type") != "HEADERS":
        return None
    headers = frame.get("headers")
    method = _header_value(headers, ":method")
    path = _header_value(headers, ":path")
    if method is None or path is None:
        return None
    return _request_surface(
        {
            "method": method,
            "path": path,
            "protocol": "h2",
            "headers": headers,
            "header_names_in_order": frame.get("header_names_in_order", []),
        },
        source,
    )


def _ws_surface(frame: dict[str, Any], source: str) -> dict[str, Any] | None:
    event_type = frame.get("event_type")
    opcode = frame.get("opcode")
    fields = frame.get("top_level_fields_in_order")
    shape = frame.get("shape")
    if not event_type and not opcode and not fields:
        return None
    return _surface(
        "websocket_frame",
        {
            "event_type": event_type,
            "opcode": opcode,
            "compressed": frame.get("compressed"),
            "rsv1_deflate": frame.get("rsv1_deflate"),
            "top_level_fields": list(fields) if isinstance(fields, list) else [],
            "shape_sha256": (
                _fingerprint({"shape": shape})
                if isinstance(shape, (dict, list))
                else None
            ),
        },
        source,
    )


def _extract_json_surfaces(
    value: Any, source: str
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    output: list[dict[str, Any]] = []
    direct_request = value.get("request")
    if isinstance(direct_request, dict):
        item = _request_surface(direct_request, source)
        if item:
            output.append(item)
    if value.get("_websocket") is True and value.get("from_client") is True:
        payload = value.get("json")
        if isinstance(payload, dict):
            item = _ws_surface(
                {
                    "event_type": payload.get("type"),
                    "opcode": "TEXT",
                    "top_level_fields_in_order": list(payload),
                    "shape": normalize_json_shape(payload),
                },
                source,
            )
            if item:
                output.append(item)
    records = value.get("records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            request = record.get("request")
            if isinstance(request, dict):
                item = _request_surface(request, source)
                if item:
                    output.append(item)
            frame = record.get("frame")
            if isinstance(frame, dict):
                item = _ws_surface(frame, source)
                if item:
                    output.append(item)
    connections = value.get("connections")
    if isinstance(connections, list):
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            for request in connection.get("requests", []):
                if isinstance(request, dict):
                    item = _request_surface(request, source)
                    if item:
                        output.append(item)
            for frame in connection.get("ws_frames", []):
                if isinstance(frame, dict):
                    item = _ws_surface(frame, source)
                    if item:
                        output.append(item)
            for frame in connection.get("frames", []):
                if isinstance(frame, dict):
                    item = _h2_request_surface(frame, source)
                    if item:
                        output.append(item)
    frames = value.get("frames")
    if isinstance(frames, list):
        for frame in frames:
            if isinstance(frame, dict):
                item = _h2_request_surface(frame, source)
                if item:
                    output.append(item)
    requests = value.get("requests")
    if isinstance(requests, list):
        for request in requests:
            if isinstance(request, dict):
                item = _request_surface(request, source)
                if item:
                    output.append(item)
    if (
        value.get("schema_version") == "codex-wham-consume-safe/v1"
        and isinstance(value.get("request_line"), str)
    ):
        item = _request_surface(
            {
                "request_line": value["request_line"],
                "header_names_in_order": value.get("header_names", []),
                "shape": value.get("body"),
            },
            source,
        )
        if item:
            output.append(item)
    hellos = value.get("client_hellos")
    if isinstance(hellos, list):
        for hello in hellos:
            if not isinstance(hello, dict):
                continue
            output.append(
                _surface(
                    "tls_client_hello",
                    {
                        "sni": hello.get("sni") or "<target-host>",
                        "cipher_suites": hello.get("cipher_suites", []),
                        "extensions": hello.get("extensions", []),
                        "alpn": hello.get("alpn") or hello.get("offered_alpn", []),
                    },
                    source,
                )
            )
    single_hello = value.get("client_hello")
    if isinstance(single_hello, dict):
        output.append(
            _surface(
                "tls_client_hello",
                {
                    "sni": single_hello.get("sni") or "<target-host>",
                    "cipher_suites": single_hello.get("cipher_suites", []),
                    "extensions": single_hello.get("extension_types", []),
                    "alpn": single_hello.get("alpn", []),
                },
                source,
            )
        )
    return output


def _scan_relay_bytes(path: Path, source: str) -> list[dict[str, Any]]:
    """直接解析中继上行字节，避免升级编排依赖人工先运行提取器。"""

    if not path.name.endswith(".client_to_upstream.bin"):
        return []
    data = path.read_bytes()
    if not data:
        return []
    output: list[dict[str, Any]] = []
    if data.startswith(H2_PREFACE):
        parsed = parse_h2_stream(data)
        for frame in parsed.get("frames", []):
            if isinstance(frame, dict):
                item = _h2_request_surface(frame, source)
                if item:
                    output.append(item)
        return output
    requests = parse_h1_stream(data)
    for request in requests:
        item = _request_surface(request, source)
        if item:
            output.append(item)
    if any(
        "upgrade" in str(name).lower()
        for request in requests
        for name in request.get("header_names_in_order", [])
    ):
        header_end = data.find(b"\r\n\r\n")
        if header_end >= 0:
            for frame in parse_ws_frames(data[header_end + 4 :]):
                item = _ws_surface(frame, source)
                if item:
                    output.append(item)
    return output


def _scan_pcap(path: Path, source: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for link, packet in iter_packets(path):
        parsed = tcp_payload(link, packet)
        if not parsed:
            continue
        _, _, payload = parsed
        hello = parse_client_hello(payload)
        if not hello:
            continue
        sni, extensions, ciphers, alpn = hello
        output.append(
            _surface(
                "tls_client_hello",
                {
                    "sni": sni or "<unknown>",
                    "cipher_suites": list(ciphers),
                    "extensions": list(extensions),
                    "alpn": list(alpn),
                },
                source,
            )
        )
    return output


def _merge_surfaces(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        fingerprint = item["fingerprint"]
        if fingerprint not in merged:
            merged[fingerprint] = dict(item)
            continue
        sources = set(merged[fingerprint].get("sources", []))
        sources.update(item.get("sources", []))
        merged[fingerprint]["sources"] = sorted(sources)
    return sorted(
        merged.values(),
        key=lambda item: (
            item["kind"],
            str(item.get("path", "")),
            item["fingerprint"],
        ),
    )


def scan_evidence(paths: Iterable[Path], label: str) -> dict[str, Any]:
    """从规范化 JSON、JSONL、relay 摘要和 pcap 建立动态出站面。"""

    surfaces: list[dict[str, Any]] = []
    warnings: list[str] = []
    files: set[Path] = set()
    for root in paths:
        if root.is_file():
            files.add(root)
        elif root.is_dir():
            files.update(
                path
                for path in root.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in {".json", ".jsonl", ".pcap", ".bin"}
            )
    for path in sorted(files):
        source = str(path)
        try:
            if path.suffix.lower() == ".bin":
                surfaces.extend(_scan_relay_bytes(path, source))
                continue
            if path.suffix.lower() == ".pcap":
                surfaces.extend(_scan_pcap(path, source))
                continue
            if path.stat().st_size > MAX_JSON_BYTES:
                warnings.append(f"跳过超大 JSON：{path}")
                continue
            if path.suffix.lower() == ".jsonl":
                with path.open(encoding="utf-8") as stream:
                    for index, line in enumerate(stream, 1):
                        if not line.strip():
                            continue
                        value = json.loads(line)
                        surfaces.extend(
                            _extract_json_surfaces(value, f"{source}:{index}")
                        )
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            surfaces.extend(_extract_json_surfaces(value, source))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            warnings.append(f"无法解析 {path}：{type(error).__name__}")
    merged = _merge_surfaces(surfaces)
    return {
        "schema_version": SURFACE_SCHEMA,
        "label": label,
        "input_paths": [str(path) for path in paths],
        "file_count": len(files),
        "surface_count": len(merged),
        "surfaces": merged,
        "warnings": warnings,
    }


def compare_surfaces(
    baseline: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    baseline_by_id = {
        item["fingerprint"]: item for item in baseline.get("surfaces", [])
    }
    target_by_id = {
        item["fingerprint"]: item for item in target.get("surfaces", [])
    }
    added = [
        target_by_id[key] for key in sorted(target_by_id.keys() - baseline_by_id.keys())
    ]
    removed = [
        baseline_by_id[key]
        for key in sorted(baseline_by_id.keys() - target_by_id.keys())
    ]
    return {
        "baseline": baseline.get("label"),
        "target": target.get("label"),
        "equal": not added and not removed,
        "added_count": len(added),
        "removed_count": len(removed),
        "added": added,
        "removed": removed,
    }


def load_rule_manifest(path: Path, baseline_version: str) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法读取规则清单 {path}：{error}") from error
    if payload.get("schema_version") != RULE_SCHEMA:
        raise ConfigurationError("规则清单 schema_version 不受支持。")
    if payload.get("codex_version") != baseline_version:
        raise ConfigurationError(
            "规则清单版本与 --baseline-version 不一致。"
        )
    rules = payload.get("required_rules")
    if not isinstance(rules, list) or not rules:
        raise ConfigurationError("规则清单 required_rules 不能为空。")
    if len(rules) != len(set(rules)):
        raise ConfigurationError("规则清单存在重复编号。")
    if any(not isinstance(rule, str) or not RULE_RE.fullmatch(rule) for rule in rules):
        raise ConfigurationError("规则清单包含非法编号。")
    return tuple(rules)


def _job(
    job_id: str,
    phase: str,
    description: str,
    argv: list[str],
    environment: dict[str, str],
    evidence_roots: list[str],
    covers: Iterable[str],
    *,
    suites: tuple[str, ...] = ("full",),
    timeout: int = 1200,
) -> Job:
    return Job(
        job_id=job_id,
        phase=phase,
        suites=suites,
        description=description,
        steps=({"argv": argv, "environment": environment, "timeout": timeout},),
        evidence_roots=tuple(evidence_roots),
        covers=tuple(covers),
    )


def _format_template(value: str, context: dict[str, str]) -> str:
    try:
        return value.format_map(context)
    except KeyError as error:
        raise ConfigurationError(f"任务模板引用未知变量：{error.args[0]}") from error


def load_extra_jobs(path: Path | None, context: dict[str, str]) -> list[Job]:
    """加载未来新形态所需的附加任务，不要求修改总编排器。"""

    if path is None:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法读取附加任务：{error}") from error
    if payload.get("schema_version") != EXTRA_JOB_SCHEMA:
        raise ConfigurationError("附加任务 schema_version 不受支持。")
    jobs: list[Job] = []
    for raw in payload.get("jobs", []):
        if not isinstance(raw, dict):
            raise ConfigurationError("附加任务必须是对象。")
        job_id = raw.get("id")
        phase = raw.get("phase")
        if not isinstance(job_id, str) or not SAFE_ID_RE.fullmatch(job_id):
            raise ConfigurationError("附加任务 id 非法。")
        if phase not in {"official", "candidate"}:
            raise ConfigurationError(f"{job_id} 的 phase 非法。")
        steps = []
        for raw_step in raw.get("steps", []):
            argv = raw_step.get("argv")
            environment = raw_step.get("environment", {})
            if not isinstance(argv, list) or not argv or not all(
                isinstance(item, str) and item for item in argv
            ):
                raise ConfigurationError(f"{job_id} 的 argv 非法。")
            if not isinstance(environment, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()
            ):
                raise ConfigurationError(f"{job_id} 的 environment 非法。")
            steps.append(
                {
                    "argv": [_format_template(item, context) for item in argv],
                    "environment": {
                        key: _format_template(value, context)
                        for key, value in environment.items()
                    },
                    "timeout": int(raw_step.get("timeout", 1800)),
                }
            )
        roots = raw.get("evidence_roots", [])
        covers = raw.get("covers", [])
        suites = raw.get("suites", ["full"])
        if (
            not steps
            or not isinstance(roots, list)
            or not isinstance(covers, list)
            or not isinstance(suites, list)
        ):
            raise ConfigurationError(f"{job_id} 的任务字段不完整。")
        jobs.append(
            Job(
                job_id=job_id,
                phase=phase,
                suites=tuple(str(item) for item in suites),
                description=str(raw.get("description", job_id)),
                steps=tuple(steps),
                evidence_roots=tuple(
                    _format_template(str(item), context) for item in roots
                ),
                covers=tuple(str(item) for item in covers),
                required=bool(raw.get("required", True)),
            )
        )
    return jobs


def _validate_side_triggers(scenario: dict[str, Any]) -> None:
    """校验分侧触发契约。

    A11／A13／A14 的单一 trigger 原文描述的是候选侧受控形态，官方侧照抄会让
    「受控第二跳」「dummy token」这类手法被当成合法触发——k36 用的正是场景定义里
    根本没写的改 last_refresh 手法。分侧后两侧各自独立声明，不再互相沿用。
    """

    side_triggers = scenario.get("side_triggers")
    if side_triggers is None:
        return
    scenario_id = scenario.get("scenario_id")
    if not isinstance(side_triggers, dict) or set(side_triggers) != {
        "official",
        "candidate",
    }:
        raise ConfigurationError(f"证据场景 {scenario_id} 的分侧触发必须同时声明两侧。")
    for side, contract in side_triggers.items():
        if not isinstance(contract, dict) or set(contract) != {
            "trigger",
            "preconditions",
        }:
            raise ConfigurationError(
                f"证据场景 {scenario_id} 的 {side} 触发契约字段不闭合。"
            )
        if not isinstance(contract["trigger"], str) or not contract["trigger"].strip():
            raise ConfigurationError(
                f"证据场景 {scenario_id} 的 {side} 触发描述不能为空。"
            )
        preconditions = contract["preconditions"]
        if (
            not isinstance(preconditions, list)
            or not preconditions
            or not all(
                isinstance(item, str) and item.strip() for item in preconditions
            )
        ):
            raise ConfigurationError(
                f"证据场景 {scenario_id} 的 {side} 前置条件非法。"
            )


def _validate_scenario_manifest_shape(payload: dict[str, Any]) -> None:
    """在无第三方 JSON Schema 依赖时执行同等失败关闭的场景结构校验。"""

    required_top = {
        "schema_version",
        "codex_version",
        "profile_id",
        "source_spec",
        "rule_manifest",
        "variable_contract",
        "evidence_scenarios",
        "capture_jobs",
        "required_client_bindings",
    }
    if not required_top.issubset(payload) or set(payload) - required_top - {"$schema"}:
        raise ConfigurationError("场景清单顶层字段不闭合。")
    if not VERSION_RE.fullmatch(str(payload.get("codex_version", ""))):
        raise ConfigurationError("场景清单 codex_version 非法。")
    if not SAFE_ID_RE.fullmatch(str(payload.get("profile_id", ""))):
        raise ConfigurationError("场景清单 profile_id 非法。")

    variables = payload.get("variable_contract")
    if not isinstance(variables, list) or not variables:
        raise ConfigurationError("场景清单 variable_contract 不能为空。")
    variable_names: set[str] = set()
    for index, variable in enumerate(variables, 1):
        required = {"name", "type", "required", "sensitive", "description"}
        if (
            not isinstance(variable, dict)
            or not required.issubset(variable)
            or set(variable) - required - {"default"}
        ):
            raise ConfigurationError(f"场景变量 {index} 字段不闭合。")
        name = variable.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"^[a-z][a-z0-9_]*$", name)
            or name in variable_names
            or variable.get("type")
            not in {"string", "integer", "absolute_path", "sha256", "image_reference"}
            or not isinstance(variable.get("required"), bool)
            or not isinstance(variable.get("sensitive"), bool)
            or not isinstance(variable.get("description"), str)
            or not variable["description"].strip()
        ):
            raise ConfigurationError(f"场景变量 {index} 定义非法。")
        if "default" in variable and (
            isinstance(variable["default"], bool)
            or not isinstance(variable["default"], (str, int))
        ):
            raise ConfigurationError(f"场景变量 {name} 默认值非法。")
        variable_names.add(name)

    scenarios = payload.get("evidence_scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ConfigurationError("场景清单 evidence_scenarios 不能为空。")
    scenario_ids: set[str] = set()
    for index, scenario in enumerate(scenarios, 1):
        required = {
            "scenario_id",
            "description",
            "trigger",
            "preconditions",
            "required_artifact_kinds",
            "covers",
        }
        # side_triggers 是唯一登记的可选字段：A11／A13／A14 的官方侧与候选侧触发
        # 形态本就不同，用同一个 trigger 描述会让 official 沿用候选侧受控手法。
        optional = {"side_triggers"}
        if (
            not isinstance(scenario, dict)
            or not required.issubset(scenario)
            or set(scenario) - required - optional
        ):
            raise ConfigurationError(f"证据场景 {index} 字段不闭合。")
        _validate_side_triggers(scenario)
        scenario_id = scenario.get("scenario_id")
        if (
            not isinstance(scenario_id, str)
            or not re.fullmatch(r"^A[0-9]{2}$", scenario_id)
            or scenario_id in scenario_ids
        ):
            raise ConfigurationError(f"证据场景 {index} scenario_id 非法或重复。")
        if any(
            not isinstance(scenario.get(field), str) or not scenario[field].strip()
            for field in ("description", "trigger")
        ):
            raise ConfigurationError(f"证据场景 {scenario_id} 描述或触发条件非法。")
        for field in ("preconditions", "required_artifact_kinds", "covers"):
            values = scenario.get(field)
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(value, str) and value for value in values)
                or len(values) != len(set(values))
            ):
                raise ConfigurationError(f"证据场景 {scenario_id} 的 {field} 非法。")
        if not all(RULE_RE.fullmatch(rule) for rule in scenario["covers"]):
            raise ConfigurationError(f"证据场景 {scenario_id} covers 非法。")
        scenario_ids.add(scenario_id)

    jobs = payload.get("capture_jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ConfigurationError("场景清单 capture_jobs 不能为空。")
    for index, job in enumerate(jobs, 1):
        required = {
            "id",
            "phase",
            "suites",
            "scenario_ids",
            "description",
            "required",
            "steps",
            "evidence_roots",
            "covers",
            "required_scenario_receipts",
        }
        model_fields = {
            "track",
            "model_id",
            "expected_use_responses_lite",
            "required_model_receipt",
        }
        if (
            not isinstance(job, dict)
            or not required.issubset(job)
            or set(job) - required - model_fields
            or (set(job) & model_fields and not model_fields.issubset(job))
        ):
            raise ConfigurationError(f"场景任务 {index} 字段不闭合。")
        if model_fields.issubset(job):
            if (
                job["track"] not in {"main", "lite"}
                or not isinstance(job["model_id"], str)
                or not job["model_id"].strip()
                or not isinstance(job["expected_use_responses_lite"], bool)
                or not isinstance(job["required_model_receipt"], bool)
                or (
                    job["track"] == "lite"
                    and job["expected_use_responses_lite"] is not True
                )
            ):
                raise ConfigurationError(f"场景任务 {index} 的模型轨道契约非法。")
        if not isinstance(job.get("required"), bool):
            raise ConfigurationError(f"场景任务 {index} required 必须是布尔值。")
        receipts = job.get("required_scenario_receipts")
        if (
            not isinstance(receipts, list)
            or len(set(map(str, receipts))) != len(receipts)
            or not all(
                isinstance(item, str) and item in SCENARIO_RECEIPT_SCENARIOS
                for item in receipts
            )
        ):
            raise ConfigurationError(
                f"场景任务 {index} required_scenario_receipts 只能声明已登记的目标场景。"
            )
        if not set(receipts).issubset(set(job.get("scenario_ids") or [])):
            raise ConfigurationError(
                f"场景任务 {index} 声明的真实性收据场景必须在自身 scenario_ids 内。"
            )
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ConfigurationError(f"场景任务 {index} steps 不能为空。")
        for step_index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or set(step) != {
                "argv",
                "environment",
                "timeout_seconds",
            }:
                raise ConfigurationError(
                    f"场景任务 {index} 步骤 {step_index} 字段不闭合。"
                )
            if (
                not isinstance(step.get("environment"), dict)
                or not isinstance(step.get("timeout_seconds"), int)
                or isinstance(step.get("timeout_seconds"), bool)
                or step["timeout_seconds"] <= 0
            ):
                raise ConfigurationError(
                    f"场景任务 {index} 步骤 {step_index} 环境或超时非法。"
                )
            if (
                job.get("phase") == "candidate"
                and step["environment"].get("CODEX_VERSION")
                != "{target_version}"
            ):
                raise ConfigurationError(
                    f"候选场景任务 {index} 步骤 {step_index} 必须从 Campaign "
                    "target_version 注入 CODEX_VERSION。"
                )

    clients = payload.get("required_client_bindings")
    if (
        not isinstance(clients, list)
        or not all(isinstance(client, str) and SAFE_ID_RE.fullmatch(client) for client in clients)
        or len(clients) != len(set(clients))
        or not REQUIRED_CLIENT_BINDINGS.issubset(set(clients))
    ):
        raise ConfigurationError("场景清单必须至少绑定 Kilo Compatible 与 Responses。")


def _validate_scenario_variable_contract(
    payload: dict[str, Any], context: dict[str, str]
) -> None:
    """让版本清单声明、模板引用与实际 Campaign 值形成闭环。"""

    variables = {
        item["name"]: item for item in payload["variable_contract"]
    }
    for name, contract in variables.items():
        if contract["sensitive"] is True:
            raise ConfigurationError(
                f"场景变量 {name} 被标为敏感；秘密不得进入版本任务模板。"
            )
        value = context.get(name, "")
        if contract["required"] and not value:
            raise ConfigurationError(f"场景必需变量 {name} 没有 Campaign 值。")
        if not value:
            continue
        variable_type = contract["type"]
        valid = True
        if variable_type == "integer":
            valid = value.isdecimal() and int(value) > 0
        elif variable_type == "absolute_path":
            valid = bool(SAFE_ABSOLUTE_PATH_RE.fullmatch(value))
        elif variable_type == "sha256":
            valid = bool(SHA256_RE.fullmatch(value))
        elif variable_type == "image_reference":
            valid = bool(IMMUTABLE_IMAGE_RE.fullmatch(value))
        elif variable_type == "string":
            valid = bool(value.strip())
        if not valid:
            raise ConfigurationError(f"场景变量 {name} 的实际值不符合 {variable_type}。")

    formatter = string.Formatter()
    template_values: list[str] = []
    for job in payload["capture_jobs"]:
        template_values.extend(str(value) for value in job["evidence_roots"])
        if "model_id" in job:
            template_values.append(job["model_id"])
        for step in job["steps"]:
            template_values.extend(step["argv"])
            template_values.extend(step["environment"].values())
    for value in template_values:
        try:
            parsed = list(formatter.parse(value))
        except ValueError as error:
            raise ConfigurationError(f"场景任务模板语法非法：{value}") from error
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if format_spec or conversion is not None:
                raise ConfigurationError(
                    f"场景任务模板禁止格式化与类型转换：{value}"
                )
            if not re.fullmatch(r"^[a-z][a-z0-9_]*$", field_name):
                raise ConfigurationError(f"场景任务模板字段非法：{field_name}")
            if field_name not in variables:
                raise ConfigurationError(
                    f"场景任务模板引用了 variable_contract 外变量：{field_name}"
                )


def load_scenario_jobs(
    path: Path,
    context: dict[str, str],
    *,
    expected_version: str | None = None,
    expected_rule_sha256: str | None = None,
    require_bindings: bool = False,
) -> list[Job]:
    """从版本化场景清单生成任务，避免在编排器中加入版本分支。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法读取场景清单 {path}：{error}") from error
    if payload.get("schema_version") != SCENARIO_SCHEMA:
        raise ConfigurationError("场景清单 schema_version 不受支持。")
    _validate_scenario_manifest_shape(payload)
    _validate_scenario_variable_contract(payload, context)
    if expected_version is not None and payload.get("codex_version") != expected_version:
        raise ConfigurationError("场景清单 codex_version 与当前阶段不一致。")
    rule_binding = payload.get("rule_manifest")
    source_binding = payload.get("source_spec")
    if require_bindings and not isinstance(rule_binding, dict):
        raise ConfigurationError("场景清单缺少规则清单摘要绑定。")
    if require_bindings and not isinstance(source_binding, dict):
        raise ConfigurationError("场景清单缺少规格第二章摘要绑定。")
    if isinstance(rule_binding, dict):
        rule_sha = rule_binding.get("sha256")
        rule_count = rule_binding.get("rule_count")
        if not SHA256_RE.fullmatch(str(rule_sha)) or not isinstance(rule_count, int):
            raise ConfigurationError("场景清单规则清单绑定非法。")
        if expected_rule_sha256 is not None and rule_sha != expected_rule_sha256:
            raise ConfigurationError("场景清单绑定了其他版本的规则清单。")
    if isinstance(source_binding, dict):
        source_path = source_binding.get("path")
        fragment = source_binding.get("fragment")
        source_sha = source_binding.get("sha256")
        if (
            not isinstance(source_path, str)
            or Path(source_path).is_absolute()
            or ".." in Path(source_path).parts
            or not isinstance(fragment, str)
            or not SHA256_RE.fullmatch(str(source_sha))
        ):
            raise ConfigurationError("场景清单规格摘要绑定非法。")
        resolved_source = Path(__file__).resolve().parents[2] / source_path
        if (
            not resolved_source.is_file()
            or resolved_source.is_symlink()
            or source_spec_section_sha256(resolved_source, fragment) != source_sha
        ):
            raise ConfigurationError("场景清单规格第二章摘要不一致。")
    raw_scenarios = payload.get("evidence_scenarios")
    if require_bindings and (not isinstance(raw_scenarios, list) or not raw_scenarios):
        raise ConfigurationError("场景清单 evidence_scenarios 不能为空。")
    scenario_rules: dict[str, set[str]] = {}
    for raw_scenario in raw_scenarios or []:
        if not isinstance(raw_scenario, dict):
            raise ConfigurationError("证据场景必须是对象。")
        scenario_id = raw_scenario.get("scenario_id")
        covers = raw_scenario.get("covers")
        if (
            not isinstance(scenario_id, str)
            or scenario_id in scenario_rules
            or not isinstance(covers, list)
            or not covers
        ):
            raise ConfigurationError("证据场景身份或 covers 非法。")
        scenario_rules[scenario_id] = set(str(value) for value in covers)
    jobs: list[Job] = []
    for raw in payload.get("capture_jobs", []):
        if not isinstance(raw, dict):
            raise ConfigurationError("场景任务必须是对象。")
        job_id = raw.get("id")
        phase = raw.get("phase")
        if not isinstance(job_id, str) or not SAFE_ID_RE.fullmatch(job_id):
            raise ConfigurationError("场景任务 id 非法。")
        if phase not in {"official", "candidate"}:
            raise ConfigurationError(f"{job_id} 的 phase 非法。")
        raw_suites = raw.get("suites")
        if (
            not isinstance(raw_suites, list)
            or not raw_suites
            or not set(raw_suites).issubset({"core", "full"})
        ):
            raise ConfigurationError(f"{job_id} 的 suites 非法。")
        steps: list[dict[str, Any]] = []
        for raw_step in raw.get("steps", []):
            if not isinstance(raw_step, dict):
                raise ConfigurationError(f"{job_id} 的步骤必须是对象。")
            argv = raw_step.get("argv")
            environment = raw_step.get("environment")
            if not isinstance(argv, list) or not argv or not all(
                isinstance(item, str) and item for item in argv
            ):
                raise ConfigurationError(f"{job_id} 的 argv 非法。")
            if not isinstance(environment, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()
            ):
                raise ConfigurationError(f"{job_id} 的 environment 非法。")
            timeout = raw_step.get("timeout_seconds")
            if not isinstance(timeout, int) or timeout <= 0:
                raise ConfigurationError(f"{job_id} 的 timeout_seconds 非法。")
            steps.append(
                {
                    "argv": [_format_template(item, context) for item in argv],
                    "environment": {
                        key: _format_template(value, context)
                        for key, value in environment.items()
                    },
                    "timeout": timeout,
                }
            )
        roots = raw.get("evidence_roots")
        covers = raw.get("covers")
        scenario_ids = raw.get("scenario_ids")
        if not steps:
            raise ConfigurationError(f"{job_id} 没有可执行步骤。")
        if not isinstance(roots, list) or not roots:
            raise ConfigurationError(f"{job_id} 的 evidence_roots 不能为空。")
        if not isinstance(covers, list) or not covers or not all(
            isinstance(rule, str) and RULE_RE.fullmatch(rule) for rule in covers
        ):
            raise ConfigurationError(f"{job_id} 的 covers 非法。")
        if (
            not isinstance(scenario_ids, list)
            or not scenario_ids
            or len(scenario_ids) != len(set(scenario_ids))
            or not set(scenario_ids).issubset(scenario_rules)
        ):
            raise ConfigurationError(f"{job_id} 的 scenario_ids 非法。")
        scenario_coverage = set().union(
            *(scenario_rules[scenario_id] for scenario_id in scenario_ids)
        )
        if not set(covers).issubset(scenario_coverage):
            raise ConfigurationError(f"{job_id} 的 covers 未被绑定场景证明。")
        jobs.append(
            Job(
                job_id=job_id,
                phase=phase,
                suites=tuple(str(item) for item in raw_suites),
                description=str(raw.get("description", job_id)),
                steps=tuple(steps),
                evidence_roots=tuple(
                    _format_template(str(item), context) for item in roots
                ),
                covers=tuple(covers),
                scenario_ids=tuple(str(item) for item in scenario_ids),
                required=raw["required"],
                required_scenario_receipts=tuple(
                    str(item) for item in raw["required_scenario_receipts"]
                ),
                track=str(raw.get("track", "main")),
                model_id=_format_template(
                    str(raw.get("model_id", "{model}")), context
                ),
                expected_use_responses_lite=bool(
                    raw.get("expected_use_responses_lite", False)
                ),
                required_model_receipt=bool(
                    raw.get("required_model_receipt", False)
                ),
            )
        )
    if not jobs:
        raise ConfigurationError("场景清单 capture_jobs 不能为空。")
    return jobs


def _expand_roots(patterns: Iterable[str]) -> list[Path]:
    output: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            output.extend(Path(match) for match in matches)
        else:
            output.append(Path(pattern))
    return output


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


@dataclass(frozen=True)
class ScenarioReceiptContext:
    """场景真实性收据所需的 attempt 身份与落盘位置。

    这三元身份是编排侧的权威事实，采集脚本无从得知也不能自行声明；由 run 阶段
    透传给外层 finalizer 注入，防止跨轮次复用收据。
    """

    campaign_id: str
    attempt_id: str
    run_nonce: str
    evidence_root: Path
    campaign_dir: Path


# 采集脚本把原始事实写在本 job 证据根的这个子目录下，文件名按场景固定。
SCENARIO_FACTS_DIR = "scenario-facts"
# facts 与收据里 evidence_bindings 共用的证据根角色名。
SCENARIO_EVIDENCE_ROOT_ROLE = "job_evidence"


def _job_run_id(job: Job) -> str:
    """取出 job 步骤声明的 RUN_ID，它把收据绑定到具体证据根。"""

    values = {
        str(step.get("environment", {}).get("RUN_ID"))
        for step in job.steps
        if step.get("environment", {}).get("RUN_ID")
    }
    if len(values) != 1:
        raise ConfigurationError(
            f"{job.job_id} 必须恰好声明一个 RUN_ID 才能产出场景真实性收据。"
        )
    return values.pop()


def _finalize_scenario_receipt(
    job: Job,
    scenario_id: str,
    job_root: Path,
    context: ScenarioReceiptContext,
    attempt_index: int,
) -> dict[str, Any]:
    """校验采集侧原始事实并承接为收据；任一环节不成立即抛错，不产出收据。"""

    run_id = _job_run_id(job)
    facts_source = job_root / SCENARIO_FACTS_DIR / f"{scenario_id}-facts.json"
    if facts_source.is_symlink() or not facts_source.is_file():
        raise ConfigurationError(
            f"{job.job_id} 未产出 {scenario_id} 的场景原始事实，目标协议分支未成立。"
        )
    try:
        payload = json.loads(facts_source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"{scenario_id} 场景原始事实不可读：{error}") from error
    # 逐条按本 job 的证据根复核 evidence_bindings 的路径、大小与 SHA-256。
    approved_roots = {SCENARIO_EVIDENCE_ROOT_ROLE: job_root}
    validate_scenario_facts_document(
        payload,
        scenario_id=scenario_id,
        job_id=job.job_id,
        run_id=run_id,
        approved_roots=approved_roots,
    )
    receipt_dir = ensure_private_directory(
        context.evidence_root
        / "receipts"
        / "scenarios"
        / job.job_id
        / f"retry-{attempt_index}",
        context.campaign_dir,
    )
    facts_copy = receipt_dir / f"{scenario_id}-facts.json"
    _secure_write_json_once(facts_copy, payload)
    output = receipt_dir / f"{scenario_id}-scenario-receipt.json"
    receipt = finalize_scenario(
        argparse.Namespace(
            evidence_root=context.evidence_root,
            output=output,
            scenario_id=scenario_id,
            job_id=job.job_id,
            campaign_id=context.campaign_id,
            attempt_id=context.attempt_id,
            run_nonce=context.run_nonce,
            run_id=run_id,
            facts=facts_copy,
        )
    )
    validate_scenario_receipt(
        receipt,
        scenario_id=scenario_id,
        job_id=job.job_id,
        campaign_id=context.campaign_id,
        attempt_id=context.attempt_id,
        run_nonce=context.run_nonce,
        run_id=run_id,
        approved_roots=approved_roots,
    )
    return {
        "scenario_id": scenario_id,
        "path": str(output),
        "sha256": file_sha256(output),
        "final_state": receipt["final_state"],
    }


def _collect_scenario_receipts(
    job: Job,
    existing_roots: list[Path],
    context: ScenarioReceiptContext | None,
    attempt_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 job 声明逐场景产出真实性收据，失败原因逐条记录。"""

    receipts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if not job.required_scenario_receipts:
        return receipts, failures
    for scenario_id in job.required_scenario_receipts:
        try:
            if context is None:
                raise ConfigurationError(
                    "声明了场景真实性收据的任务必须在 attempt 上下文内执行。"
                )
            if len(existing_roots) != 1:
                raise ConfigurationError(
                    f"{job.job_id} 必须恰好命中一个证据根才能绑定场景收据。"
                )
            receipts.append(
                _finalize_scenario_receipt(
                    job, scenario_id, existing_roots[0], context, attempt_index
                )
            )
        except (
            ConfigurationError,
            ReceiptFinalizerError,
            ScenarioReceiptError,
            OSError,
        ) as error:
            failures.append(
                {"scenario_id": scenario_id, "reason": str(error)[:512] or "未知失败"}
            )
    return receipts, failures


def _collect_model_condition_receipt(
    job: Job,
    existing_roots: list[Path],
) -> tuple[dict[str, Any] | None, str | None]:
    """复核 job 证据根内的模型条件成功收据。"""

    if not getattr(job, "required_model_receipt", False):
        return None, None
    try:
        if len(existing_roots) != 1:
            raise ConfigurationError(
                f"{job.job_id} 必须恰好命中一个证据根才能绑定模型条件收据。"
            )
        root = existing_roots[0]
        path = root / "model-condition-receipt.json"
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"{job.job_id} 未产出模型条件成功收据。")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"模型条件收据不可读：{error}") from error
        validated = validate_model_condition_receipt(
            payload,
            root=root,
            job_id=job.job_id,
            track=getattr(job, "track", "main"),
            model_id=getattr(job, "model_id", ""),
            use_responses_lite=getattr(job, "expected_use_responses_lite", False),
        )
        return {
            "path": str(path),
            "sha256": file_sha256(path),
            "track": validated["track"],
            "model_id": validated["model_id"],
            "models_response_sha256": validated["models_response_sha256"],
            "use_responses_lite": validated["use_responses_lite"],
            "model_fallback": validated["model_fallback"],
        }, None
    except (ConfigurationError, ModelConditionReceiptError, OSError) as error:
        return None, str(error)[:512] or "未知失败"


def _revalidate_model_condition_result(job: Job, result: dict[str, Any]) -> None:
    """在 seal／resume 时重放模型收据，拒绝 run 后篡改证据或结果字段。"""

    expected_coordinates = {
        "track": job.track,
        "model_id": job.model_id,
        "expected_use_responses_lite": job.expected_use_responses_lite,
        "required_model_receipt": job.required_model_receipt,
    }
    if any(result.get(key) != value for key, value in expected_coordinates.items()):
        raise ConfigurationError(f"{job.job_id} 的模型轨道结果坐标漂移。")
    receipt_reference = result.get("model_condition_receipt")
    failure = result.get("model_condition_receipt_failure")
    if not job.required_model_receipt:
        if receipt_reference is not None or failure is not None:
            raise ConfigurationError(f"{job.job_id} 未声明模型收据却携带模型结果。")
        return
    roots = result.get("evidence_roots")
    if not isinstance(roots, list) or len(roots) != 1:
        raise ConfigurationError(f"{job.job_id} 的模型收据必须绑定唯一 evidence root。")
    root = Path(roots[0])
    path = root / "model-condition-receipt.json"
    if (
        failure is not None
        or not isinstance(receipt_reference, dict)
        or receipt_reference.get("path") != str(path)
        or not path.is_file()
        or path.is_symlink()
        or receipt_reference.get("sha256") != file_sha256(path)
    ):
        raise ConfigurationError(f"{job.job_id} 的模型条件收据缺失或摘要不一致。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"{job.job_id} 的模型条件收据不可读：{error}") from error
    validated = validate_model_condition_receipt(
        payload,
        root=root,
        job_id=job.job_id,
        track=job.track,
        model_id=job.model_id,
        use_responses_lite=job.expected_use_responses_lite,
    )
    for key in (
        "track",
        "model_id",
        "models_response_sha256",
        "use_responses_lite",
        "model_fallback",
    ):
        if receipt_reference.get(key) != validated[key]:
            raise ConfigurationError(f"{job.job_id} 的模型条件结果字段 {key} 不一致。")


def run_job(
    job: Job,
    log_root: Path,
    attempt_index: int = 1,
    scenario_context: ScenarioReceiptContext | None = None,
) -> dict[str, Any]:
    """顺序执行任务步骤，并保留不含命令环境值的日志。

    attempt_index 只用于在同一 attempt 内区分补跑的第几次，写进任务收据供审计。
    scenario_context 携带 attempt 身份，供声明了真实性收据的任务承接原始事实。
    """

    started = time.time()
    step_results: list[dict[str, Any]] = []
    for index, step in enumerate(job.steps, 1):
        log_path = log_root / (
            f"{job.job_id}-{index}.log"
            if attempt_index == 1
            else f"{job.job_id}-retry{attempt_index}-{index}.log"
        )
        environment = os.environ.copy()
        environment.update(step.get("environment", {}))
        argv = list(step["argv"])
        with log_path.open("w", encoding="utf-8") as log:
            os.chmod(log_path, 0o600)
            process = subprocess.Popen(
                argv,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                return_code = process.wait(timeout=int(step.get("timeout", 1800)))
            except KeyboardInterrupt:
                _terminate_process(process)
                raise
            except subprocess.TimeoutExpired:
                _terminate_process(process)
                return_code = 124
        step_results.append(
            {
                "step": index,
                "argv": [Path(argv[0]).name, *argv[1:]],
                "return_code": return_code,
                "log": str(log_path),
            }
        )
        if return_code != 0:
            break
    roots_by_pattern: dict[str, list[Path]] = {}
    missing_patterns: list[str] = []
    empty_patterns: list[str] = []
    for pattern in job.evidence_roots:
        matches = [Path(value) for value in sorted(glob.glob(pattern))]
        if not matches and Path(pattern).exists():
            matches = [Path(pattern)]
        existing = [path for path in matches if path.exists() and not path.is_symlink()]
        roots_by_pattern[pattern] = existing
        if not existing:
            missing_patterns.append(pattern)
            continue
        if not any(
            path.is_file()
            and path.stat().st_size > 0
            or path.is_dir()
            and any(
                child.is_file() and not child.is_symlink() and child.stat().st_size > 0
                for child in path.rglob("*")
            )
            for path in existing
        ):
            empty_patterns.append(pattern)
    existing_roots = [
        root for values in roots_by_pattern.values() for root in values
    ]
    steps_ok = len(step_results) == len(job.steps) and all(
        item["return_code"] == 0 for item in step_results
    )
    scenario_receipts_found, scenario_receipt_failures = _collect_scenario_receipts(
        job, existing_roots, scenario_context, attempt_index
    )
    # SCN-REALITY-01 的第四条件：声明的场景收据必须齐备且合法。退出码为 0、证据
    # 目录非空但目标协议分支一跳未发生，正是 k36 的实际形态，必须判失败。
    scenario_receipts_ok = not scenario_receipt_failures and len(
        scenario_receipts_found
    ) == len(job.required_scenario_receipts)
    model_receipt, model_receipt_failure = _collect_model_condition_receipt(
        job, existing_roots
    )
    model_receipt_ok = (
        model_receipt_failure is None
        and (not getattr(job, "required_model_receipt", False) or model_receipt is not None)
    )
    status = (
        "complete"
        if steps_ok
        and not missing_patterns
        and not empty_patterns
        and scenario_receipts_ok
        and model_receipt_ok
        else "failed"
    )
    return {
        "id": job.job_id,
        "phase": job.phase,
        "required": job.required,
        "execution_sha256": _job_execution_sha256(job),
        "status": status,
        "attempt_index": attempt_index,
        "description": job.description,
        "duration_seconds": round(time.time() - started, 3),
        "steps": step_results,
        "evidence_roots": [str(root) for root in existing_roots],
        "missing_evidence_patterns": missing_patterns,
        "empty_evidence_patterns": empty_patterns,
        "covers": list(job.covers),
        "scenario_ids": list(job.scenario_ids),
        "scenario_receipts": scenario_receipts_found,
        "scenario_receipt_failures": scenario_receipt_failures,
        "track": getattr(job, "track", "main"),
        "model_id": getattr(job, "model_id", ""),
        "expected_use_responses_lite": getattr(
            job, "expected_use_responses_lite", False
        ),
        "required_model_receipt": getattr(job, "required_model_receipt", False),
        "model_condition_receipt": model_receipt,
        "model_condition_receipt_failure": model_receipt_failure,
    }


# 上游波动（模型 at capacity、压缩原因未触发、CLI 偶发多一次信任提示）会让个别任务
# 落空。这类失败重跑即消失，但整轮 official 采集要 20 分钟——不在同一 attempt 内补跑，
# 就只能靠 resume 整轮重来，而重来同样要赌全部 17 项一次全过，收敛极慢。
#
# 在同一 attempt 内补跑不等于跨 attempt 拼接证据：run_nonce、环境探针边界、Campaign
# 身份全程不变，产出的仍然是同一次采集的证据。补跑前把上一次的证据目录整体归档，避免
# 与新证据混在一起；补跑次数写进任务收据，供审计还原真实执行过程。
# attempt_index 从 1 起算、判定为 `attempt_index > JOB_RETRY_LIMIT`，故总尝试次数 = 本值 + 1：
# 值 2 即总共 3 次（首次 + retry2 + retry3）。
#
# **这个常量是 official 与 candidate 共用的，不要为了给候选侧提速而下调。**
# k64 实证：改为 1（总共 2 次）后，官方侧 `official-core` 连续两次栽在 codex-ws 的 s4／s2
# 上（双轮对话与工具调用场景，本就最易受上游抖动影响），28/28 退化为 27/28 —— 而这类失败
# 恰恰需要第三次机会；历史正式采集中的 `official-relay-realtime-webrtc` 曾在第 3 次才成功。
# 候选侧的提速改由场景超时承担（`run_sub2api_*_matrix.sh` 的 --timeout 300→70），
# 那两处只作用于候选矩阵，不波及官方链路。
JOB_RETRY_LIMIT = 2
JOB_RETRY_DELAY_SECONDS = 30


def _archive_failed_job_evidence(result: dict[str, Any], attempt_index: int) -> None:
    """把失败那次的证据目录整体挪走，让补跑从干净状态开始。"""

    for value in result.get("evidence_roots") or []:
        root = Path(value)
        if not root.exists() or root.is_symlink():
            continue
        archived = root.with_name(f"{root.name}.failed-attempt{attempt_index}")
        suffix = 1
        while archived.exists():
            suffix += 1
            archived = root.with_name(
                f"{root.name}.failed-attempt{attempt_index}-{suffix}"
            )
        try:
            root.rename(archived)
        except OSError:
            # 归档失败不能吞掉：证据残留会让补跑在旧样本上得出结论。
            raise ConfigurationError(f"无法归档失败任务的证据目录：{root}")


def _run_job_with_retry(
    job: Job,
    log_root: Path,
    scenario_context: ScenarioReceiptContext | None = None,
) -> dict[str, Any]:
    """在同一 attempt 内对失败任务做有限补跑，返回最后一次的收据。"""

    attempt_index = 1
    while True:
        result = run_job(job, log_root, attempt_index, scenario_context)
        if result.get("status") == "complete":
            return result
        if not job.required or attempt_index > JOB_RETRY_LIMIT:
            return result
        _archive_failed_job_evidence(result, attempt_index)
        attempt_index += 1
        time.sleep(JOB_RETRY_DELAY_SECONDS)


def build_coverage(
    rules: tuple[str, ...],
    jobs: list[Job],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    status_by_job = {result["id"]: result["status"] for result in results}
    rows = []
    for rule in rules:
        by_phase: dict[str, list[str]] = {"official": [], "candidate": []}
        for job in jobs:
            if rule in job.covers:
                by_phase[job.phase].append(job.job_id)
        official_complete = any(
            status_by_job.get(job_id) == "complete"
            for job_id in by_phase["official"]
        )
        candidate_complete = any(
            status_by_job.get(job_id) == "complete"
            for job_id in by_phase["candidate"]
        )
        rows.append(
            {
                "rule": rule,
                "official_jobs": by_phase["official"],
                "candidate_jobs": by_phase["candidate"],
                "official_evidence_collected": official_complete,
                "candidate_evidence_collected": candidate_complete,
                "evidence_complete": official_complete and candidate_complete,
            }
        )
    complete = [row for row in rows if row["evidence_complete"]]
    return {
        "required_rule_count": len(rules),
        "evidence_complete_count": len(complete),
        "complete": len(complete) == len(rules),
        "rules": rows,
    }


def _validate_capture_job_results(
    jobs: list[Job],
    results: Any,
    *,
    phase: str,
) -> None:
    if not isinstance(results, list):
        raise ConfigurationError(f"{phase} 抓包 results 必须是数组。")
    expected = {job.job_id: job for job in jobs if job.required}
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise ConfigurationError(f"{phase} 抓包任务收据必须是对象。")
        job_id = result.get("id")
        if not isinstance(job_id, str) or job_id in seen:
            raise ConfigurationError(f"{phase} 抓包任务收据身份非法或重复。")
        seen.add(job_id)
        if job_id in expected:
            expected_job = expected[job_id]
            if (
                result.get("phase") != phase
                or result.get("status") != "complete"
                or result.get("execution_sha256")
                != _job_execution_sha256(expected_job)
            ):
                raise ConfigurationError(
                    f"{phase} 必需抓包任务 {job_id} 未完成或执行定义漂移。"
                )
            _revalidate_model_condition_result(expected_job, result)
    missing = set(expected) - seen
    if missing:
        raise ConfigurationError(f"{phase} 缺少必需抓包任务收据：{sorted(missing)}")


def _render_report(payload: dict[str, Any]) -> str:
    jobs = payload["jobs"]
    coverage = payload["coverage"]
    source_diff = payload["source_diff"]
    official_diff = payload["baseline_to_target_official"]
    candidate_diff = payload["official_to_candidate"]
    lines = [
        f"# Codex CLI {payload['target_version']} 升级审计报告",
        "",
        f"- 基线版本：{payload['baseline_version']}",
        f"- 目标版本：{payload['target_version']}",
        f"- 任务状态：{payload['status']}",
        (
            f"- 规则证据覆盖：{coverage['evidence_complete_count']}/"
            f"{coverage['required_rule_count']}"
        ),
        f"- 新增源码线索：{source_diff['added_count']}",
        f"- 新增官方动态形态：{official_diff['added_count']}",
        (
            f"- 官方与 Sub2API 动态形态差异："
            f"+{candidate_diff['added_count']}/-{candidate_diff['removed_count']}"
        ),
        "",
        "## 抓包任务",
        "",
        "| 任务 | 边界 | 状态 |",
        "|---|---|---|",
    ]
    for job in jobs:
        lines.append(f"| {job['id']} | {job['phase']} | {job['status']} |")
    incomplete = [
        row["rule"] for row in coverage["rules"] if not row["evidence_complete"]
    ]
    lines.extend(["", "## 尚未闭合的规则", ""])
    lines.append("、".join(incomplete) if incomplete else "无。")
    lines.extend(["", "## 新形态候选", ""])
    if source_diff["added_count"] or official_diff["added_count"]:
        lines.append(
            "存在尚未分类的源码或动态形态变化；必须判定为既有规则变化、"
            "新增规则或不适用后，才能建立目标版本画像。"
        )
    else:
        lines.append("未发现新增源码线索或新增动态出站形态。")
    lines.extend(
        [
            "",
            "## 判定边界",
            "",
            "任务成功只表示证据已收集并完成结构比较，不自动等同于规则通过。",
            "账号权限或场景未触发造成的缺口必须保持失败状态，不能继承旧版本结论。",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_output_path(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError("Campaign 目录必须是非符号链接的绝对路径。")
    resolved = path.resolve(strict=False)
    forbidden = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path("/tmp").resolve(),
    }
    if resolved in forbidden:
        raise ConfigurationError("Campaign 目录不能是根目录、HOME 或 /tmp 本身。")
    if path.exists():
        raise ConfigurationError("Campaign 目录已存在；plan 必须使用新目录。")


def _validate_existing_campaign_path(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError("--campaign-dir 必须是非符号链接的绝对路径。")
    if not path.is_dir():
        raise ConfigurationError(f"Campaign 目录不存在：{path}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_campaign_reference(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--campaign-dir",
            "--campaign",
            dest="campaign_dir",
            type=Path,
            required=True,
        )

    def add_candidate_reference(target: argparse.ArgumentParser) -> None:
        add_campaign_reference(target)
        target.add_argument("--candidate-id", required=True)

    def add_capture_receipts(
        target: argparse.ArgumentParser,
        *,
        candidate: bool,
    ) -> None:
        target.add_argument(
            "--attempt-id",
            help="run 阶段返回的不可变 attempt ID；seal 阶段必需。",
        )
        target.add_argument(
            "--capture-manifest",
            type=Path,
            help="finalizer 生成并位于证据根内的统一 capture manifest。",
        )
        target.add_argument(
            "--assertion-evidence-root",
            type=Path,
            help="capture manifest 内 artifact 路径所基于的证据根。",
        )
        target.add_argument(
            "--restoration-report",
            type=Path,
            help="兼容校验：只能指向本次 run 自动生成的环境恢复报告。",
        )
        target.add_argument(
            "--evidence-root",
            action="append",
            default=[],
            type=Path,
            help="seal 时重申 run 已绑定的证据根或其子目录，可重复。",
        )
        target.add_argument(
            "--approve-seal-sha256",
            help="人工复核 seal-preview.json 后回传的联合摘要。",
        )
        if candidate:
            target.add_argument(
                "--observed-profile-receipt",
                type=Path,
                help="由运行中 Sub2API 产生的实际画像观测收据。",
            )

    plan = subparsers.add_parser("plan", help="预检并创建不可变 Campaign")
    plan.add_argument(
        "--campaign-dir",
        "--output",
        dest="campaign_dir",
        type=Path,
        required=True,
    )
    plan.add_argument("--baseline-version", required=True)
    plan.add_argument("--target-version", required=True)
    plan.add_argument("--baseline-source", type=Path, required=True)
    plan.add_argument("--target-source", type=Path, required=True)
    plan.add_argument("--baseline-evidence", type=Path, required=True)
    plan.add_argument("--target-sha256", required=True)
    plan.add_argument(
        "--target-package",
        type=Path,
        required=True,
        help="官方 codex-package 压缩包的持久绝对路径。",
    )
    plan.add_argument("--target-package-sha256", required=True)
    plan.add_argument("--target-code-mode-host-sha256", required=True)
    plan.add_argument(
        "--runtime-image",
        required=True,
        help="官方采集运行时的 repository@sha256 不可变镜像引用。",
    )
    plan.add_argument(
        "--rule-manifest",
        type=Path,
        required=True,
    )
    plan.add_argument("--scenario-manifest", type=Path, required=True)
    plan.add_argument("--extra-jobs", type=Path)
    plan.add_argument("--suite", choices=("core", "full"), default="full")
    plan.add_argument(
        "--campaign-id",
        default="",
        help="留空时按目标版本和 UTC 时间生成。",
    )
    plan.add_argument(
        "--model",
        required=True,
        help=(
            "主升级线模型，必须属于目标版本 main 轨道"
            "（上游 use_responses_lite=false）。"
        ),
    )
    plan.add_argument(
        "--lite-model",
        required=True,
        help="Lite 专项模型，必须属于目标版本 lite 轨道。",
    )
    plan.add_argument("--capture-root", type=Path, default=Path("/root/oauth-capture"))
    plan.add_argument("--capture-container", default="capture-cli")
    plan.add_argument("--service-container", default="sub2apiplus")
    plan.add_argument("--keeper-container", default="sub2apiplus-keeper")
    plan.add_argument("--postgres-container", default="sub2apiplus-postgres")
    plan.add_argument("--redis-container", default="sub2apiplus-redis")
    plan.add_argument("--capture-codex-bin", default="/usr/local/bin/codex-capture")
    plan.add_argument("--relay-codex-bin", default="/root/.local/bin/codex")
    plan.add_argument(
        "--capture-code-mode-host-bin",
        default="/usr/local/bin/codex-code-mode-host",
    )
    plan.add_argument(
        "--relay-code-mode-host-bin",
        default="/root/.local/bin/codex-code-mode-host",
    )
    plan.add_argument("--codex-account-id", type=int, default=90)
    plan.add_argument("--api-key-id", type=int, default=1)
    plan.add_argument(
        "--live-attestation-compose-dir",
        default="",
        help="候选部署的 compose 工作目录；A11 需据此重建服务启用 candidatecapture provider。",
    )
    plan.add_argument(
        "--live-attestation-compose-files",
        default="",
        help="候选部署的 compose -f 参数串，恢复时按同一串拉回。",
    )

    official = subparsers.add_parser(
        "capture-official", help="运行或封存目标官方 CLI 证据"
    )
    official.add_argument("capture_action", choices=("run", "seal"))
    add_campaign_reference(official)
    add_capture_receipts(official, candidate=False)
    official.add_argument("--acknowledge-live-requests", action="store_true")

    classify = subparsers.add_parser(
        "classify", help="生成差异草案或封存已审核的目标规则迁移"
    )
    add_campaign_reference(classify)
    classify.add_argument("--target-rule-manifest", type=Path)
    classify.add_argument("--migration-manifest", type=Path)
    classify.add_argument("--scenario-manifest", type=Path)
    classify.add_argument("--profile-manifest", type=Path)
    classify.add_argument("--assertion-profile-manifest", type=Path)
    classify.add_argument("--approve-manifest-sha256")

    prepare_profile = subparsers.add_parser(
        "prepare-profile",
        help="把完整 Snapshot 规范化为待人工审核的目标画像清单",
    )
    add_campaign_reference(prepare_profile)
    prepare_profile.add_argument("--snapshot", type=Path, required=True)
    prepare_profile.add_argument("--profile-id", required=True)
    prepare_profile.add_argument("--output", type=Path, required=True)

    stage_profile = subparsers.add_parser(
        "stage-profile",
        help="把已批准完整画像编译成不切 Active 的候选 RuntimeCatalog",
    )
    add_campaign_reference(stage_profile)
    stage_profile.add_argument(
        "--output",
        type=Path,
        required=True,
        help="不存在的候选目录绝对路径；不会修改仓库或生产 selector。",
    )

    candidate = subparsers.add_parser(
        "capture-candidate", help="运行或封存一个 Sub2API 候选"
    )
    candidate.add_argument("capture_action", choices=("run", "seal"))
    add_candidate_reference(candidate)
    candidate.add_argument("--runtime-image")
    candidate.add_argument("--candidate-image-id")
    candidate.add_argument("--candidate-source", type=Path)
    candidate.add_argument("--build-id")
    candidate.add_argument("--deployed-version")
    candidate.add_argument("--profile-id")
    candidate.add_argument("--profile-digest")
    add_capture_receipts(candidate, candidate=True)
    candidate.add_argument(
        "--client-evidence",
        action="append",
        default=[],
        metavar="CLIENT=PATH",
    )
    candidate.add_argument("--acknowledge-live-requests", action="store_true")

    compare = subparsers.add_parser("compare", help="仅使用封存证据离线比较")
    add_candidate_reference(compare)

    accept = subparsers.add_parser("accept", help="执行逐规则正式验收门禁")
    add_candidate_reference(accept)
    accept.add_argument("--assertions", type=Path)
    accept.add_argument(
        "--external-gate-root",
        type=Path,
        required=True,
        help="candidate 外部门禁收据所在的 0700 evidence root",
    )
    accept.add_argument(
        "--external-gate-receipt",
        type=Path,
        required=True,
        help="由独立 finalizer 生成并可重放的 candidate_external 收据",
    )

    all_command = subparsers.add_parser(
        "all", help="兼容入口：对已批准画像只启动一次候选 run"
    )
    add_candidate_reference(all_command)
    all_command.add_argument("--runtime-image", required=True)
    all_command.add_argument("--candidate-image-id")
    all_command.add_argument("--candidate-source", type=Path)
    all_command.add_argument("--build-id", required=True)
    all_command.add_argument("--deployed-version", required=True)
    all_command.add_argument("--profile-id", required=True)
    all_command.add_argument("--profile-digest", required=True)
    all_command.add_argument("--acknowledge-live-requests", action="store_true")

    status = subparsers.add_parser("status", help="显示 Campaign 状态和下一命令")
    add_campaign_reference(status)
    status.add_argument("--candidate-id")

    resume = subparsers.add_parser("resume", help="按最近稳定状态续跑失败阶段")
    add_campaign_reference(resume)
    resume.add_argument("--candidate-id")
    resume.add_argument("--runtime-image")
    resume.add_argument("--candidate-image-id")
    resume.add_argument("--candidate-source", type=Path)
    resume.add_argument("--build-id")
    resume.add_argument("--deployed-version")
    resume.add_argument("--profile-id")
    resume.add_argument("--profile-digest")
    resume.add_argument("--assertions", type=Path)
    resume.add_argument("--external-gate-root", type=Path)
    resume.add_argument("--external-gate-receipt", type=Path)
    resume.add_argument("--rerun-failed", action="store_true")
    resume.add_argument("--acknowledge-live-requests", action="store_true")
    return parser


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if not getattr(arguments, "redis_container", None):
        arguments.redis_container = "sub2apiplus-redis"
    for field, value in (
        ("--baseline-version", arguments.baseline_version),
        ("--target-version", arguments.target_version),
    ):
        if not VERSION_RE.fullmatch(value):
            raise ConfigurationError(f"{field} 必须是三段版本号。")
    supported_upgrade_pairs = {
        ("0.145.0", "0.147.0"),
        ("0.147.0", "0.149.1"),
    }
    upgrade_pair = (arguments.baseline_version, arguments.target_version)
    if upgrade_pair in supported_upgrade_pairs:
        main_models = track_models_for_version(arguments.target_version, "main")
        lite_models = track_models_for_version(arguments.target_version, "lite")
        if arguments.model not in main_models:
            raise ConfigurationError(
                f"{arguments.baseline_version}→{arguments.target_version} 主升级线"
                f"只能使用 {'／'.join(main_models)}。"
            )
        if arguments.lite_model not in lite_models:
            raise ConfigurationError(
                f"{arguments.baseline_version}→{arguments.target_version} Lite 专项"
                f"只能使用 {'／'.join(lite_models)}。"
            )
    if not SHA256_RE.fullmatch(arguments.target_sha256):
        raise ConfigurationError("--target-sha256 必须是 64 位小写 SHA-256。")
    if not SHA256_RE.fullmatch(arguments.target_package_sha256):
        raise ConfigurationError(
            "--target-package-sha256 必须是 64 位小写 SHA-256。"
        )
    if not SHA256_RE.fullmatch(arguments.target_code_mode_host_sha256):
        raise ConfigurationError(
            "--target-code-mode-host-sha256 必须是 64 位小写 SHA-256。"
        )
    if not IMMUTABLE_IMAGE_RE.fullmatch(arguments.runtime_image):
        raise ConfigurationError(
            "--runtime-image 必须是 repository@sha256:<manifest-digest>。"
        )
    if not arguments.baseline_evidence.exists():
        raise ConfigurationError("--baseline-evidence 不存在。")
    for field, source in (
        ("--baseline-source", arguments.baseline_source),
        ("--target-source", arguments.target_source),
    ):
        if not source.is_dir() or source.is_symlink():
            raise ConfigurationError(f"{field} 不存在或不是可信目录。")
    if arguments.codex_account_id <= 0 or arguments.api_key_id <= 0:
        raise ConfigurationError("账号和 API Key ID 必须为正整数。")
    if arguments.campaign_id:
        if not SAFE_ID_RE.fullmatch(arguments.campaign_id):
            raise ConfigurationError("--campaign-id 格式非法。")
    else:
        version = arguments.target_version.replace(".", "_")
        arguments.campaign_id = (
            f"codex-{version}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        )
    if not arguments.capture_root.is_absolute():
        raise ConfigurationError("--capture-root 必须是绝对路径。")
    if not SAFE_ABSOLUTE_PATH_RE.fullmatch(str(arguments.capture_root)):
        raise ConfigurationError("--capture-root 包含不安全字符。")
    for field in (
        "capture_container",
        "service_container",
        "keeper_container",
        "postgres_container",
        "redis_container",
        "model",
        "lite_model",
    ):
        value = str(getattr(arguments, field))
        if not SAFE_ID_RE.fullmatch(value):
            raise ConfigurationError(f"--{field.replace('_', '-')} 格式非法。")
    if (
        not arguments.target_package.is_absolute()
        or not SAFE_ABSOLUTE_PATH_RE.fullmatch(str(arguments.target_package))
        or arguments.target_package.is_symlink()
        or not arguments.target_package.is_file()
    ):
        raise ConfigurationError(
            "--target-package 必须是存在、非符号链接的安全绝对文件。"
        )
    arguments.target_package_identity = _verify_codex_package(
        arguments.target_package,
        expected_version=arguments.target_version,
        expected_package_sha256=arguments.target_package_sha256,
        expected_binary_sha256=arguments.target_sha256,
        expected_code_mode_host_sha256=arguments.target_code_mode_host_sha256,
    )
    for field in (
        "capture_codex_bin",
        "relay_codex_bin",
        "capture_code_mode_host_bin",
        "relay_code_mode_host_bin",
    ):
        value = str(getattr(arguments, field))
        if not SAFE_ABSOLUTE_PATH_RE.fullmatch(value):
            raise ConfigurationError(f"--{field.replace('_', '-')} 路径不安全。")
    campaign_dir = getattr(arguments, "campaign_dir", None) or getattr(
        arguments, "output", None
    )
    if campaign_dir is None:
        raise ConfigurationError("缺少 Campaign 目录。")
    arguments.campaign_dir = campaign_dir
    arguments.output = campaign_dir
    _validate_output_path(campaign_dir)


def _safe_plan(
    arguments: argparse.Namespace,
    jobs: list[Job],
    rules: tuple[str, ...],
) -> dict[str, Any]:
    mapped = {
        phase: {rule for job in jobs if job.phase == phase for rule in job.covers}
        for phase in ("official", "candidate")
    }
    return {
        "schema_version": REPORT_SCHEMA,
        "mode": "dry-run",
        "baseline_version": arguments.baseline_version,
        "target_version": arguments.target_version,
        "campaign_id": arguments.campaign_id,
        "suite": arguments.suite,
        "baseline_source": str(arguments.baseline_source),
        "target_source": str(arguments.target_source),
        "baseline_evidence": str(arguments.baseline_evidence),
        "output": str(arguments.output),
        "coverage_plan": {
            "required_rule_count": len(rules),
            "official_unmapped": sorted(set(rules) - mapped["official"]),
            "candidate_unmapped": sorted(set(rules) - mapped["candidate"]),
        },
        "jobs": [
            {
                "id": job.job_id,
                "phase": job.phase,
                "description": job.description,
                "steps": [
                    {"argv": step["argv"], "timeout": step.get("timeout")}
                    for step in job.steps
                ],
                "evidence_roots": list(job.evidence_roots),
                "covers": list(job.covers),
                "scenario_ids": list(job.scenario_ids),
                "track": job.track,
                "model_id": job.model_id,
                "expected_use_responses_lite": job.expected_use_responses_lite,
                "required_model_receipt": job.required_model_receipt,
            }
            for job in jobs
        ],
    }


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法读取{label} {path}：{error}") from error
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label}必须是 JSON 对象：{path}")
    return payload


def _reject_symlink_components(path: Path, root: Path, label: str) -> None:
    """拒绝从 Campaign 根到目标文件之间的任一符号链接。"""

    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ConfigurationError(f"{label}越过 Campaign 根目录。") from error
    current = root
    if current.is_symlink():
        raise ConfigurationError(f"{label} Campaign 根目录是符号链接。")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ConfigurationError(f"{label}路径包含符号链接：{current}")


def _secure_write_json_once(path: Path, payload: dict[str, Any]) -> None:
    """以硬链接发布临时文件，实现跨进程原子且不可覆盖的 JSON 封存。"""

    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ConfigurationError(f"不可变文件已经存在，禁止覆盖：{path}") from error
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _campaign_lock(campaign_dir: Path) -> Iterable[None]:
    """以 Campaign 级文件锁串行化 attempt 预约与阶段发布。"""

    _validate_existing_campaign_path(campaign_dir)
    lock_path = campaign_dir / ".campaign.lock"
    _reject_symlink_components(lock_path, campaign_dir, "Campaign 锁")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(lock_path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or (not created and stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise ConfigurationError("Campaign 锁必须是当前用户拥有的 0600 普通文件。")
        if created:
            os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _campaign_file(campaign_dir: Path, relative: str) -> Path:
    path = campaign_dir / relative
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(campaign_dir.resolve()):
        raise ConfigurationError(f"Campaign 文件越过根目录：{relative}")
    return path


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[a-f0-9]{40,64}", value) else None


def _source_identity(root: Path, version: str) -> tuple[dict[str, Any], dict[str, Any]]:
    inventory = scan_source_tree(root, version)
    cargo_lock = root / "Cargo.lock"
    identity = {
        "path": str(root.resolve()),
        "source_tree_sha256": _directory_tree_digest(root),
        "egress_inventory_sha256": _fingerprint(inventory),
        "cargo_lock_sha256": file_sha256(cargo_lock) if cargo_lock.is_file() else None,
        "git_commit": _git_commit(root),
    }
    return identity, inventory


def _verify_codex_package(
    package: Path,
    *,
    expected_version: str,
    expected_package_sha256: str,
    expected_binary_sha256: str,
    expected_code_mode_host_sha256: str,
) -> dict[str, Any]:
    """验证官方 package 内的 CLI 与 Code Mode helper 形成同一身份闭包。"""

    if file_sha256(package) != expected_package_sha256:
        raise ConfigurationError("官方 codex-package 压缩包摘要不一致。")
    required_members = {
        "codex-package.json",
        "bin/codex",
        "bin/codex-code-mode-host",
    }
    try:
        with tarfile.open(package, mode="r:gz") as archive:
            members_by_name: dict[str, list[tarfile.TarInfo]] = {}
            for member in archive.getmembers():
                members_by_name.setdefault(member.name.removeprefix("./"), []).append(
                    member
                )
            if any(
                len(members_by_name.get(name, [])) != 1
                for name in required_members
            ):
                raise ConfigurationError(
                    "官方 codex-package 缺少必要成员或存在重名成员。"
                )
            selected = {
                name: members_by_name[name][0] for name in required_members
            }
            if any(not member.isfile() for member in selected.values()):
                raise ConfigurationError(
                    "官方 codex-package 必要成员必须是普通文件。"
                )

            def member_bytes(name: str, *, limit: int | None = None) -> bytes:
                member = selected[name]
                if limit is not None and member.size > limit:
                    raise ConfigurationError(
                        f"官方 codex-package 成员过大：{name}"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise ConfigurationError(
                        f"无法读取官方 codex-package 成员：{name}"
                    )
                return stream.read()

            def member_sha256(name: str) -> str:
                stream = archive.extractfile(selected[name])
                if stream is None:
                    raise ConfigurationError(
                        f"无法读取官方 codex-package 成员：{name}"
                    )
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                return digest.hexdigest()

            metadata = json.loads(
                member_bytes("codex-package.json", limit=1024 * 1024).decode(
                    "utf-8"
                )
            )
            binary_sha256 = member_sha256("bin/codex")
            helper_sha256 = member_sha256("bin/codex-code-mode-host")
    except ConfigurationError:
        raise
    except (
        OSError,
        tarfile.TarError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise ConfigurationError("无法验证官方 codex-package。") from error
    if not isinstance(metadata, dict):
        raise ConfigurationError("官方 codex-package 元数据必须是对象。")
    package_target = metadata.get("target")
    if (
        metadata.get("layoutVersion") != 1
        or metadata.get("version") != expected_version
        or metadata.get("variant") != "codex"
        or metadata.get("entrypoint") != "bin/codex"
        or not isinstance(package_target, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", package_target)
    ):
        raise ConfigurationError("官方 codex-package 元数据与目标坐标不一致。")
    if binary_sha256 != expected_binary_sha256:
        raise ConfigurationError("官方 package 内 Codex CLI 摘要不一致。")
    if helper_sha256 != expected_code_mode_host_sha256:
        raise ConfigurationError(
            "官方 package 内 codex-code-mode-host 摘要不一致。"
        )
    return {
        "asset_sha256": expected_package_sha256,
        "layout_version": 1,
        "target": package_target,
        "variant": "codex",
        "entrypoint": "bin/codex",
        "binary_sha256": binary_sha256,
        "code_mode_host_sha256": helper_sha256,
    }


# 评估侧受管文件：只读既有证据做判定、汇总与编目，不决定任何证据的字节内容。
#
# 这份清单是**白名单**，且必须逐个论证——判定依据是「该文件的改动能否改变已封存证据
# 的字节」。未登记的文件一律按产出侧处理（见 _tool_identity_sides 的 fail-close），
# 新增文件因此默认落到严格侧，不会因为忘记登记而被静默放行。
#
# 之所以要这个划分：Campaign 建立后工具身份一旦漂移就整轮作废，而历史上多数作废源于
# 判据／门禁类修复（k43 的负样本契约、k46 的门禁语义、k56 的四处 seal 门禁修复）。
# 这些修复改的是「怎么判断」，不是「采到什么」，把它们与采集脚本同等对待，代价是
# 每修一次就重采一轮官方证据。
#
# 放宽的边界很窄：评估侧文件改动后，已封存证据逐字节不变，只是重新评估一遍即可；
# 因此承接（resume）成立。产出侧（采集驱动、探针、中继、脱敏、收据生成、环境快照）
# 任何改动都可能改变证据本身，仍然严格拒绝。
_EVALUATION_SIDE_FILES = frozenset(
    {
        # 从批准断言画像推导验收模型；只读画像与规则，不接触证据字节。
        "acceptance_contract.py",
        # seal 前把 accept 的证据前提失败关闭；只读已收口的 bundle。
        "assertion_gate.py",
        # 按显式规则把已存在的证据根扫描成 manifest；不写证据。
        "build_capture_manifest.py",
        # 按冻结声明把多 job 根编目成三份计划；不写证据。
        "build_evidence_catalog.py",
        # 编排逐规则断言并汇总为 accept 所需结果文档；不写证据。
        "build_rule_assertion_results.py",
        # 对候选抓包执行逐规则断言；只读抓包。
        "candidate_rule_assertion.py",
        # 采集后校验中继样本完整性；只读样本。
        "check_sample_integrity.py",
        # capture manifest 的校验 schema；只约束校验严格度，不产生内容。
        "candidate_capture_manifest.schema.json",
    }
)


def _tool_identity_sides(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """把受管文件按证据影响面分成两组，并各自计算摘要。

    fail-close：只有显式登记在 _EVALUATION_SIDE_FILES 里的路径才进评估侧，
    其余（含任何新增文件）全部计入产出侧。
    """

    production = [e for e in entries if e["path"] not in _EVALUATION_SIDE_FILES]
    evaluation = [e for e in entries if e["path"] in _EVALUATION_SIDE_FILES]
    return {
        "production_count": len(production),
        "production_sha256": _fingerprint({"entries": production}),
        "evaluation_count": len(evaluation),
        "evaluation_sha256": _fingerprint({"entries": evaluation}),
    }


def _tool_tree_entries(tool_root: Path) -> list[dict[str, Any]]:
    """按受管口径列出一棵工具树的文件摘要。

    抽成独立函数供两处共用：`_tool_identity` 算受管树自身的身份，
    `_verify_execution_tree` 用同一口径比对采集实际执行的那份副本。
    """

    files = sorted(
        path
        for path in tool_root.rglob("*")
        if (
            path.is_file()
            and not path.is_symlink()
            and path.suffix in {".py", ".sh", ".json"}
            and "tests" not in path.relative_to(tool_root).parts
            # 目标版本五件套会在 plan 后由人工审核产生，并由分类阶段独立绑定；
            # 它们不是编排器可执行信任根，不能反向击穿既有 Campaign。
            and "versions" not in path.relative_to(tool_root).parts
            and "__pycache__" not in path.relative_to(tool_root).parts
        )
    )
    return [
        {
            "path": path.relative_to(tool_root).as_posix(),
            "sha256": file_sha256(path),
        }
        for path in files
        if path.is_file() and not path.is_symlink()
    ]


def _verify_execution_tree(capture_root: Path | None) -> None:
    """确认采集实际执行的工具副本与受管树逐字一致。

    `_tool_identity` 只扫描本文件所在的受管树，而采集脚本与 relay 由
    `$CAPTURE_MOUNT/tools/official_client_capture/` 执行——那是**另一份副本**。
    k71 因此出现「工具身份校验通过、跑的却是旧代码」：受管树里
    `upstream_byte_relay.py` 的 `legacy_compact_ordinal`（Cookie 按到达序号下发）
    与 job 定义里写死的 `gpt-5.6-luna`（Lite 轨）都已就位，执行副本却停在更早版本，
    `EP-015`／`EP-022`／`EP-014`／`BODY-006` 四条判据随之必败，且没有任何门禁报警。
    职责边界：本校验只回答「存在的那份执行副本有没有漂移」。执行位置不存在时直接放行
    ——那不是漂移，而是路径配置问题，真实采集会在脚本解析
    `$CAPTURE_MOUNT/tools/...` 时自己失败；把「必须存在」也塞进来，只会让所有用假
    capture_root 的单元测试无法运行。存在但有文件缺失或摘要不符时 fail-close 拒绝。
    """

    if capture_root is None:
        return
    managed_root = Path(__file__).resolve().parent
    execution_root = capture_root / "tools" / "official_client_capture"
    try:
        execution_root_is_dir = execution_root.is_dir()
        execution_root_is_symlink = execution_root.is_symlink()
    except OSError:
        # CI 等普通用户可能连 capture_root 的父目录都无权遍历；这与目录不存在
        # 一样表示当前没有可执行副本可供比较。真实采集仍会在解析脚本时失败。
        return
    if not execution_root_is_dir or execution_root_is_symlink:
        return
    if execution_root.resolve() == managed_root.resolve():
        return
    actual = {
        entry["path"]: entry["sha256"]
        for entry in _tool_tree_entries(execution_root)
    }
    drift = [
        entry["path"]
        for entry in _tool_tree_entries(managed_root)
        if actual.get(entry["path"]) != entry["sha256"]
    ]
    if drift:
        raise ConfigurationError(
            "采集执行位置与受管工具树不一致，实际会跑到未受校验的副本："
            + f"{execution_root}；共 {len(drift)} 个文件漂移，前几个："
            + ", ".join(drift[:5])
        )


def _tool_identity(*, include_git: bool = True) -> dict[str, Any]:
    tool_root = Path(__file__).resolve().parent
    entries = _tool_tree_entries(tool_root)
    return {
        "git_commit": (
            _git_commit(Path(__file__).resolve().parents[2])
            if include_git
            else None
        ),
        "entry_count": len(entries),
        "files_sha256": _fingerprint({"entries": entries}),
        "entries": entries,
        **_tool_identity_sides(entries),
    }


def _tool_identity_drift(
    current: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, list[str]]:
    """逐文件比对两份工具身份，按证据影响面归类变化路径。"""

    def index(identity: Mapping[str, Any]) -> dict[str, str]:
        raw = identity.get("entries")
        if not isinstance(raw, list):
            return {}
        return {
            str(item.get("path")): str(item.get("sha256"))
            for item in raw
            if isinstance(item, dict)
        }

    now, before = index(current), index(expected)
    changed = sorted(
        path
        for path in set(now) | set(before)
        if now.get(path) != before.get(path)
    )
    return {
        "production": [p for p in changed if p not in _EVALUATION_SIDE_FILES],
        "evaluation": [p for p in changed if p in _EVALUATION_SIDE_FILES],
    }


def _job_context(arguments: argparse.Namespace) -> dict[str, str]:
    return {
        "baseline_version": arguments.baseline_version,
        "target_version": arguments.target_version,
        "campaign_id": arguments.campaign_id,
        "candidate_id": str(getattr(arguments, "candidate_id", "") or ""),
        "capture_root": str(arguments.capture_root),
        "output": str(arguments.output),
        "campaign_dir": str(arguments.output),
        "repo_root": str(Path(__file__).resolve().parents[2]),
        "model": arguments.model,
        "lite_model": str(getattr(arguments, "lite_model", "") or ""),
        "runtime_image": str(arguments.runtime_image),
        "target_sha256": arguments.target_sha256,
        "profile_id": str(getattr(arguments, "profile_id", "") or ""),
        "profile_digest": str(getattr(arguments, "profile_digest", "") or ""),
        "build_id": str(getattr(arguments, "build_id", "") or ""),
        "deployed_version": str(
            getattr(arguments, "deployed_version", "") or ""
        ),
        "candidate_image_id": str(
            getattr(arguments, "candidate_image_id", "") or ""
        ),
        "source_tree_sha256": str(
            getattr(arguments, "source_tree_sha256", "") or ""
        ),
        "capture_container": arguments.capture_container,
        "service_container": arguments.service_container,
        "keeper_container": arguments.keeper_container,
        "postgres_container": arguments.postgres_container,
        "redis_container": arguments.redis_container,
        "capture_codex_bin": arguments.capture_codex_bin,
        "relay_codex_bin": arguments.relay_codex_bin,
        "codex_account_id": str(arguments.codex_account_id),
        "api_key_id": str(arguments.api_key_id),
        # A11 的 Live attestation 只读进程环境，采集侧需按本轮四元组重建候选服务；
        # 缺省为空表示不注入，届时 A11 会以断言失败暴露而不是静默跳过。
        "live_attestation_compose_dir": str(
            getattr(arguments, "live_attestation_compose_dir", "") or ""
        ),
        "live_attestation_compose_files": str(
            getattr(arguments, "live_attestation_compose_files", "") or ""
        ),
    }


def _validate_jobs(jobs: list[Job], rules: tuple[str, ...]) -> None:
    duplicate_jobs = sorted(
        {
            job.job_id
            for job in jobs
            if sum(item.job_id == job.job_id for item in jobs) > 1
        }
    )
    if duplicate_jobs:
        raise ConfigurationError(f"任务 ID 重复：{duplicate_jobs}")
    evidence_owners: dict[tuple[str, str], list[str]] = {}
    for job in jobs:
        for root in job.evidence_roots:
            evidence_owners.setdefault((job.phase, root), []).append(job.job_id)
    duplicate_roots = {
        f"{phase}:{root}": sorted(set(job_ids))
        for (phase, root), job_ids in evidence_owners.items()
        if len(set(job_ids)) > 1
    }
    if duplicate_roots:
        raise ConfigurationError(
            "同一阶段的证据根必须由单一任务独占："
            f"{duplicate_roots}"
        )
    unknown_rules = sorted({rule for job in jobs for rule in job.covers} - set(rules))
    if unknown_rules:
        raise ConfigurationError(f"任务引用规则清单外编号：{unknown_rules}")
    unbound_jobs = sorted(
        job.job_id for job in jobs if job.covers and not job.scenario_ids
    )
    if unbound_jobs:
        raise ConfigurationError(
            "只有版本化场景清单中的任务可以声明规则覆盖；"
            f"未绑定场景的任务={unbound_jobs}"
        )


def _validate_phase_coverage(jobs: list[Job], rules: tuple[str, ...]) -> None:
    required = set(rules)
    missing = {
        phase: sorted(
            required
            - {
                rule
                for job in jobs
                if job.phase == phase and job.required
                for rule in job.covers
            }
        )
        for phase in ("official", "candidate")
    }
    incomplete = {phase: values for phase, values in missing.items() if values}
    if incomplete:
        raise ConfigurationError(f"full 场景清单存在未映射规则：{incomplete}")


def _load_plan_jobs(
    arguments: argparse.Namespace,
    rules: tuple[str, ...],
) -> tuple[list[Job], Path]:
    scenario_manifest = arguments.scenario_manifest
    if not scenario_manifest.is_file() or scenario_manifest.is_symlink():
        raise ConfigurationError(f"场景清单不存在或不可信：{scenario_manifest}")
    context = _job_context(arguments)
    jobs = load_scenario_jobs(
        scenario_manifest,
        context,
        expected_version=arguments.baseline_version,
        expected_rule_sha256=file_sha256(arguments.rule_manifest),
        require_bindings=True,
    )
    jobs.extend(load_extra_jobs(arguments.extra_jobs, context))
    jobs = [job for job in jobs if arguments.suite in job.suites]
    _validate_jobs(jobs, rules)
    if arguments.suite == "full":
        _validate_phase_coverage(jobs, rules)
    return jobs, scenario_manifest


def create_campaign(arguments: argparse.Namespace) -> dict[str, Any]:
    """创建只写一次的 Campaign 核心清单和计划期分析产物。"""

    _validate_arguments(arguments)
    rules = load_rule_manifest(arguments.rule_manifest, arguments.baseline_version)
    jobs, scenario_manifest = _load_plan_jobs(arguments, rules)
    baseline_identity, baseline_source = _source_identity(
        arguments.baseline_source, arguments.baseline_version
    )
    target_identity, target_source = _source_identity(
        arguments.target_source, arguments.target_version
    )
    source_diff = compare_inventory(baseline_source, target_source)
    baseline_surface = scan_evidence(
        [arguments.baseline_evidence], "baseline-official"
    )

    campaign_dir = arguments.campaign_dir
    ensure_private_directory(campaign_dir)
    inputs_root = ensure_private_directory(campaign_dir / "inputs", campaign_dir)
    analysis_root = ensure_private_directory(campaign_dir / "analysis", campaign_dir)
    baseline_rules_payload = _read_json(arguments.rule_manifest, "基线规则清单")
    scenario_payload = _read_json(scenario_manifest, "官方发现场景清单")
    secure_write_json(inputs_root / "baseline-rules.json", baseline_rules_payload)
    secure_write_json(inputs_root / "discovery-scenarios.json", scenario_payload)
    extra_jobs_reference: dict[str, Any] | None = None
    if arguments.extra_jobs is not None:
        extra_payload = _read_json(arguments.extra_jobs, "附加任务清单")
        secure_write_json(inputs_root / "extra-jobs.json", extra_payload)
        extra_jobs_reference = {
            "path": "inputs/extra-jobs.json",
            "sha256": file_sha256(inputs_root / "extra-jobs.json"),
        }
    for name, payload in (
        ("baseline-source.json", baseline_source),
        ("target-source.json", target_source),
        ("source-diff.json", source_diff),
        ("baseline-surface.json", baseline_surface),
    ):
        secure_write_json(analysis_root / name, payload)

    official_identity = {
        "cli_version": arguments.target_version,
        "binary_sha256": arguments.target_sha256,
        "package": arguments.target_package_identity,
        "source_tree_sha256": target_identity["source_tree_sha256"],
        "cargo_lock_sha256": target_identity["cargo_lock_sha256"],
        "git_commit": target_identity["git_commit"],
        "runtime_image": arguments.runtime_image,
        "operating_system": sys.platform,
        "architecture": os.uname().machine,
        "tls_dependencies_sha256": _fingerprint(
            {
                "entries": [
                    item
                    for item in target_source.get("entries", [])
                    if item.get("kind") == "network_dependency"
                ]
            }
        ),
    }
    # 冻结工具身份之前先确认执行副本与受管树一致：这里记进 manifest 的是受管树的
    # 摘要，若执行位置此刻已经漂移，整轮采集都会跑在未受校验的代码上（k71 即此）。
    _verify_execution_tree(getattr(arguments, "capture_root", None))
    plan = _safe_plan(arguments, jobs, rules)
    manifest = {
        "schema_version": CAMPAIGN_SCHEMA,
        "campaign_id": arguments.campaign_id,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseline_version": arguments.baseline_version,
        "target_version": arguments.target_version,
        "target_sha256": arguments.target_sha256,
        "suite": arguments.suite,
        "official_identity": official_identity,
        "baseline_identity": baseline_identity,
        "tool_identity": _tool_identity(),
        "inputs": {
            "baseline_rules": {
                "path": "inputs/baseline-rules.json",
                "sha256": file_sha256(inputs_root / "baseline-rules.json"),
            },
            "discovery_scenarios": {
                "path": "inputs/discovery-scenarios.json",
                "sha256": file_sha256(inputs_root / "discovery-scenarios.json"),
            },
            "extra_jobs": extra_jobs_reference,
        },
        "analysis": {
            name: {
                "path": f"analysis/{name}.json",
                "sha256": file_sha256(analysis_root / f"{name}.json"),
            }
            for name in (
                "baseline-source",
                "target-source",
                "source-diff",
                "baseline-surface",
            )
        },
        "configuration": {
            "baseline_source": str(arguments.baseline_source.resolve()),
            "target_source": str(arguments.target_source.resolve()),
            "target_package": str(arguments.target_package.resolve()),
            "baseline_evidence": str(arguments.baseline_evidence.resolve()),
            "runtime_image": arguments.runtime_image,
            "model": arguments.model,
            "lite_model": arguments.lite_model,
            "capture_root": str(arguments.capture_root),
            "capture_container": arguments.capture_container,
            "service_container": arguments.service_container,
            "keeper_container": arguments.keeper_container,
            "postgres_container": arguments.postgres_container,
            "redis_container": arguments.redis_container,
            "capture_codex_bin": arguments.capture_codex_bin,
            "relay_codex_bin": arguments.relay_codex_bin,
            "capture_code_mode_host_bin": arguments.capture_code_mode_host_bin,
            "relay_code_mode_host_bin": arguments.relay_code_mode_host_bin,
            "codex_account_id": arguments.codex_account_id,
            "api_key_id": arguments.api_key_id,
            "live_attestation_compose_dir": str(
                getattr(arguments, "live_attestation_compose_dir", "") or ""
            ),
            "live_attestation_compose_files": str(
                getattr(arguments, "live_attestation_compose_files", "") or ""
            ),
        },
        "required_rules": list(rules),
        "coverage_plan": plan["coverage_plan"],
        "jobs": plan["jobs"],
    }
    manifest_path = campaign_dir / "campaign.json"
    _secure_write_json_once(manifest_path, manifest)
    secure_write_text(
        campaign_dir / "campaign.sha256", file_sha256(manifest_path) + "\n"
    )
    return manifest


def load_campaign_manifest(path: Path) -> dict[str, Any]:
    """加载 Campaign，并验证核心清单及其计划期输入未被改写。"""

    campaign_dir = path.parent if path.name == "campaign.json" else path
    _validate_existing_campaign_path(campaign_dir)
    manifest_path = campaign_dir / "campaign.json"
    digest_path = campaign_dir / "campaign.sha256"
    if not manifest_path.is_file() or not digest_path.is_file():
        raise ConfigurationError("Campaign 缺少 campaign.json 或 campaign.sha256。")
    expected = digest_path.read_text(encoding="utf-8").strip()
    if not SHA256_RE.fullmatch(expected) or file_sha256(manifest_path) != expected:
        raise ConfigurationError("Campaign 核心清单摘要不一致，拒绝继续。")
    manifest = _read_json(manifest_path, "Campaign 核心清单")
    if manifest.get("schema_version") != CAMPAIGN_SCHEMA:
        raise ConfigurationError("Campaign schema_version 不受支持。")
    for group_name in ("inputs", "analysis"):
        for reference in manifest.get(group_name, {}).values():
            if reference is None:
                continue
            relative = reference.get("path")
            expected_sha = reference.get("sha256")
            if not isinstance(relative, str) or not SHA256_RE.fullmatch(
                str(expected_sha)
            ):
                raise ConfigurationError(f"Campaign {group_name} 引用非法。")
            target = _campaign_file(campaign_dir, relative)
            if not target.is_file() or file_sha256(target) != expected_sha:
                raise ConfigurationError(f"Campaign 输入摘要漂移：{relative}")
    return manifest


def _stage_path(
    campaign_dir: Path,
    stage: str,
    candidate_id: str | None = None,
) -> tuple[str, Path]:
    aliases = {
        "official": "capture-official",
        "candidate": "capture-candidate",
        "classification": "classify",
        "comparison": "compare",
        "acceptance": "accept",
    }
    canonical = aliases.get(stage, stage)
    if canonical == "capture-official":
        return canonical, campaign_dir / "official" / "result.json"
    if canonical == "classify":
        return canonical, campaign_dir / "classification" / "result.json"
    if canonical in {"capture-candidate", "compare", "accept"}:
        if not candidate_id or not SAFE_ID_RE.fullmatch(candidate_id):
            raise ConfigurationError(f"{canonical} 必须提供合法 candidate-id。")
        roots = {
            "capture-candidate": "candidates",
            "compare": "comparisons",
            "accept": "acceptance",
        }
        return canonical, campaign_dir / roots[canonical] / candidate_id / "result.json"
    raise ConfigurationError(f"未知 Campaign 阶段：{stage}")


def _require_file_binding(value: Any, label: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "sha256"}
        or not isinstance(value.get("path"), str)
        or not value["path"]
        or not SHA256_RE.fullmatch(str(value.get("sha256")))
    ):
        raise ConfigurationError(f"{label}文件绑定非法。")


def _validate_stage_contract(document: dict[str, Any]) -> None:
    """对运行期阶段收据执行失败关闭的核心契约校验。"""

    if document.get("schema_version") != STAGE_SCHEMA:
        raise ConfigurationError("阶段收据 schema_version 不受支持。")
    stage = document.get("stage")
    status = document.get("status")
    if stage not in {"capture-official", "classify", "capture-candidate", "compare", "accept"}:
        raise ConfigurationError("阶段收据 stage 非法。")
    if status not in {"complete", "blocked", "failed"}:
        raise ConfigurationError("阶段收据 status 非法。")
    if stage in {"capture-official", "capture-candidate"} and status == "complete":
        required = {
            "identity",
            "attempt",
            "seal_preview",
            "results",
            "evidence_roots",
            "evidence_inventory",
            "surface",
            "client_bindings",
            "assertion_context",
            "assertion_gate",
            "restoration",
            "security",
        }
        missing = sorted(required - set(document))
        if missing:
            raise ConfigurationError(f"抓包阶段收据缺少字段：{missing}")
        try:
            validate_gate_receipt(
                document.get("assertion_gate"),
                side="official" if stage == "capture-official" else "candidate",
            )
        except AssertionGateError as error:
            raise ConfigurationError(
                f"抓包阶段断言门禁收据非法：{error}"
            ) from error
        if not isinstance(document["results"], list) or not document["results"]:
            raise ConfigurationError("抓包阶段 results 不能为空。")
        _require_file_binding(document.get("attempt"), "抓包 attempt")
        _require_file_binding(document.get("seal_preview"), "seal 预览")
        if not isinstance(document["evidence_roots"], list) or not document["evidence_roots"]:
            raise ConfigurationError("抓包阶段 evidence_roots 不能为空。")
        inventory = document["evidence_inventory"]
        if (
            not isinstance(inventory, dict)
            or not isinstance(inventory.get("entries"), list)
            or not inventory["entries"]
            or inventory.get("entry_count") != len(inventory["entries"])
            or inventory.get("digest") != _fingerprint({"entries": inventory["entries"]})
        ):
            raise ConfigurationError("抓包阶段 evidence_inventory 非法。")
        inventory_index = {
            entry.get("path"): entry.get("sha256")
            for entry in inventory["entries"]
            if isinstance(entry, dict)
        }
        _require_file_binding(document["surface"], "抓包表面")
        restoration = document["restoration"]
        if (
            not isinstance(restoration, dict)
            or restoration.get("passed") is not True
            or not isinstance(restoration.get("checks"), list)
            or not restoration["checks"]
        ):
            raise ConfigurationError("抓包阶段恢复门禁未通过。")
        _require_file_binding(restoration.get("report"), "环境恢复报告")
        if inventory_index.get(restoration["report"]["path"]) != restoration["report"]["sha256"]:
            raise ConfigurationError("环境恢复报告未绑定封存证据清单。")
        security = document["security"]
        if (
            not isinstance(security, dict)
            or security.get("raw_evidence_private") is not True
            or security.get("known_secret_scan_passed") is not True
        ):
            raise ConfigurationError("抓包阶段秘密扫描门禁未通过。")
        context = document["assertion_context"]
        if (
            not isinstance(context, dict)
            or not isinstance(context.get("capture_manifest_path"), str)
            or not isinstance(context.get("evidence_root"), str)
            or not isinstance(context.get("evidence_prefix"), str)
        ):
            raise ConfigurationError("抓包阶段 assertion_context 非法。")
        _require_file_binding(context.get("capture_manifest"), "capture manifest")
        if inventory_index.get(context["capture_manifest"]["path"]) != context["capture_manifest"]["sha256"]:
            raise ConfigurationError("capture manifest 未绑定封存证据清单。")
        if stage == "capture-candidate":
            post_client = restoration.get("post_client")
            if (
                not isinstance(post_client, dict)
                or post_client.get("passed") is not True
                or not isinstance(post_client.get("checks"), list)
                or not post_client["checks"]
            ):
                raise ConfigurationError("候选阶段缺少 Kilo 后环境恢复门禁。")
            _require_file_binding(
                post_client.get("report"), "Kilo 后环境恢复报告"
            )
            if (
                inventory_index.get(post_client["report"]["path"])
                != post_client["report"]["sha256"]
            ):
                raise ConfigurationError("Kilo 后恢复报告未绑定封存证据清单。")
            _require_file_binding(document.get("observed_profile"), "运行画像观测")
            if inventory_index.get(document["observed_profile"]["path"]) != document["observed_profile"]["sha256"]:
                raise ConfigurationError("运行画像观测未绑定封存证据清单。")
            client_bindings = document.get("client_bindings")
            client_ids = {
                item.get("client_id")
                for item in client_bindings
                if isinstance(item, dict)
            } if isinstance(client_bindings, list) else set()
            if not REQUIRED_CLIENT_BINDINGS.issubset(client_ids):
                raise ConfigurationError("候选阶段缺少两种必需 Kilo 客户端绑定。")
        else:
            binary_verification = document.get("binary_verification")
            if (
                not isinstance(binary_verification, dict)
                or binary_verification.get("passed") is not True
                or not SHA256_RE.fullmatch(
                    str(binary_verification.get("expected_sha256", ""))
                )
                or not VERSION_RE.fullmatch(
                    str(binary_verification.get("expected_version", ""))
                )
                or not IMMUTABLE_IMAGE_RE.fullmatch(
                    str(binary_verification.get("runtime_image_reference", ""))
                )
                or not IMAGE_ID_RE.fullmatch(
                    str(binary_verification.get("runtime_image_id", ""))
                )
                or not isinstance(binary_verification.get("identities"), list)
                or len(binary_verification["identities"]) < 3
            ):
                raise ConfigurationError("官方阶段缺少完整二进制身份验证。")
    if stage == "classify" and status in {"complete", "blocked"}:
        for field in (
            "target_rule_manifest",
            "migration_manifest",
            "scenario_manifest",
            "profile_manifest",
            "assertion_profile_manifest",
        ):
            _require_file_binding(document.get(field), field)
    if stage == "compare" and status == "complete":
        for field in (
            "official_package_digest",
            "candidate_package_digest",
            "classification_package_digest",
        ):
            if not SHA256_RE.fullmatch(str(document.get(field))):
                raise ConfigurationError(f"比较阶段 {field} 非法。")
        if document.get("offline_only") is not True:
            raise ConfigurationError("比较阶段必须是纯离线。")
    if stage == "accept" and status == "complete":
        _require_file_binding(document.get("assertion_result"), "逐规则断言结果")
        _require_file_binding(document.get("evidence_seal"), "验收证据封印")
        external_gate = document.get("candidate_external_gate")
        if (
            not isinstance(external_gate, dict)
            or set(external_gate) != {"evidence_root", "receipt"}
            or not isinstance(external_gate.get("evidence_root"), str)
            or not isinstance(external_gate.get("receipt"), dict)
            or set(external_gate["receipt"]) != {"path", "sha256", "bytes"}
        ):
            raise ConfigurationError("验收阶段缺少 candidate 外部门禁绑定。")
        identity = document.get("candidate_identity")
        if (
            not isinstance(identity, dict)
            or set(identity)
            != {
                "source_tree_sha256",
                "image_id",
                "image_reference",
                "build_id",
                "deployed_version",
            }
            or not SHA256_RE.fullmatch(str(identity.get("source_tree_sha256", "")))
            or not IMAGE_ID_RE.fullmatch(str(identity.get("image_id", "")))
            or not IMMUTABLE_IMAGE_RE.fullmatch(str(identity.get("image_reference", "")))
        ):
            raise ConfigurationError("验收阶段 candidate 身份投影非法。")


def save_stage_result(
    campaign_dir: Path,
    stage: str,
    payload: dict[str, Any],
    candidate_id: str | None = None,
) -> Path:
    """封存阶段结果；同一阶段和候选编号永不覆盖。"""

    manifest = load_campaign_manifest(campaign_dir)
    canonical, path = _stage_path(campaign_dir, stage, candidate_id)
    _reject_symlink_components(path.parent, campaign_dir, f"{canonical} 阶段目录")
    if path.exists():
        raise ConfigurationError(f"阶段结果已存在，禁止覆盖：{path}")
    if path.parent.exists() and path.parent.is_symlink():
        raise ConfigurationError(f"阶段目录不可信：{path.parent}")
    ensure_private_directory(path.parent, campaign_dir)
    document = dict(payload)
    evidence_roots = [Path(value) for value in document.get("evidence_roots", [])]
    if canonical in {"capture-official", "capture-candidate"} and evidence_roots:
        document.setdefault("evidence_inventory", _evidence_inventory(evidence_roots))
        security = document.setdefault("security", _evidence_security(evidence_roots))
        if not security.get("known_secret_scan_passed"):
            raise ConfigurationError(f"{canonical} 证据秘密扫描未通过。")
    result_schema = document.pop("schema_version", None)
    document["schema_version"] = STAGE_SCHEMA
    if result_schema and result_schema != STAGE_SCHEMA:
        document["result_schema_version"] = result_schema
    document["stage"] = canonical
    document["campaign_id"] = manifest["campaign_id"]
    if candidate_id is not None:
        document["candidate_id"] = candidate_id
    document["campaign_manifest_sha256"] = file_sha256(
        campaign_dir / "campaign.json"
    )
    with _campaign_lock(campaign_dir):
        _reject_contaminated_campaign(campaign_dir)
        _reject_symlink_components(path.parent, campaign_dir, f"{canonical} 阶段目录")
        if path.exists() or path.is_symlink():
            raise ConfigurationError(f"阶段结果已存在，禁止覆盖：{path}")
        if canonical in {"capture-official", "capture-candidate"} and evidence_roots:
            current_inventory = _evidence_inventory(evidence_roots)
            if current_inventory != document.get("evidence_inventory"):
                raise ConfigurationError(
                    f"{canonical} 证据在封存审批后发生变化，禁止写入。"
                )
            current_security = _evidence_security(evidence_roots)
            expected_security = document.get("security")
            if not isinstance(expected_security, dict) or any(
                expected_security.get(key) != value
                for key, value in current_security.items()
            ):
                raise ConfigurationError(
                    f"{canonical} 证据秘密扫描结果在封存审批后发生变化。"
                )
            if (
                expected_security.get("raw_evidence_private") is not True
                or not _evidence_permissions_private(evidence_roots)
            ):
                raise ConfigurationError(
                    f"{canonical} 原始证据权限在封存审批后发生变化。"
                )
        document["sealed_at_utc"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        _validate_stage_contract(document)
        document["package_digest"] = _fingerprint(document)
        _secure_write_json_once(path, document)
    return path


def _load_stage_result(
    campaign_dir: Path,
    stage: str,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    canonical, path = _stage_path(campaign_dir, stage, candidate_id)
    _reject_symlink_components(path, campaign_dir, f"{canonical} 阶段结果")
    if not path.is_file() or path.is_symlink():
        raise ConfigurationError(f"阶段尚未封存：{canonical}")
    payload = _read_json(path, f"{canonical} 阶段结果")
    expected = payload.get("package_digest")
    unsigned = dict(payload)
    unsigned.pop("package_digest", None)
    if not SHA256_RE.fullmatch(str(expected)) or _fingerprint(unsigned) != expected:
        raise ConfigurationError(f"{canonical} 阶段结果摘要不一致。")
    if payload.get("campaign_manifest_sha256") != file_sha256(
        campaign_dir / "campaign.json"
    ):
        raise ConfigurationError(f"{canonical} 未绑定当前 Campaign。")
    campaign_manifest = _read_json(campaign_dir / "campaign.json", "Campaign 核心清单")
    if (
        payload.get("stage") != canonical
        or payload.get("campaign_id") != campaign_manifest.get("campaign_id")
    ):
        raise ConfigurationError(f"{canonical} 阶段身份与路径不一致。")
    candidate_stage = canonical in {"capture-candidate", "compare", "accept"}
    if candidate_stage and payload.get("candidate_id") != candidate_id:
        raise ConfigurationError(f"{canonical} candidate-id 与路径不一致。")
    if not candidate_stage and "candidate_id" in payload:
        raise ConfigurationError(f"{canonical} 不得携带 candidate-id。")
    _validate_stage_contract(payload)
    if canonical in {"capture-official", "capture-candidate"}:
        _verify_campaign_binding(campaign_dir, payload.get("attempt"), "抓包 attempt")
        _verify_campaign_binding(
            campaign_dir, payload.get("seal_preview"), "seal 预览"
        )
        _verify_capture_seal_preview(campaign_dir, payload, canonical)
        _replay_capture_stage_receipts(campaign_dir, payload, canonical)
        _verify_stage_evidence(
            payload,
            "官方" if canonical == "capture-official" else "候选",
        )
    if canonical == "classify" and payload.get("status") in {"complete", "blocked"}:
        fields = (
            "target_rule_manifest",
            "migration_manifest",
            "scenario_manifest",
            "profile_manifest",
            "assertion_profile_manifest",
        )
        for field in fields:
            _verify_campaign_binding(campaign_dir, payload.get(field), field)
        expected_joint = _fingerprint(
            {field: payload[field]["sha256"] for field in fields}
        )
        if payload.get("joint_manifest_sha256") != expected_joint:
            raise ConfigurationError("分类五件套联合摘要不一致。")
    if canonical == "accept" and payload.get("status") == "complete":
        candidate = _load_stage_result(
            campaign_dir,
            "capture-candidate",
            candidate_id,
        )
        _replay_bound_candidate_external_gate(
            payload.get("candidate_external_gate"),
            manifest=campaign_manifest,
            candidate_id=str(candidate_id),
            candidate=candidate,
        )
        seal_path = _campaign_file(
            campaign_dir,
            str(payload["evidence_seal"]["path"]),
        )
        seal = _read_json(seal_path, "验收证据封印")
        if seal.get("candidate_external_gate") != payload.get(
            "candidate_external_gate"
        ):
            raise ConfigurationError("验收证据封印未绑定同一 candidate 外部门禁。")
    return payload


def _verify_campaign_binding(
    campaign_dir: Path,
    reference: Any,
    label: str,
) -> None:
    _require_file_binding(reference, label)
    path = _campaign_file(campaign_dir, reference["path"])
    if path.is_symlink() or not path.is_file() or file_sha256(path) != reference["sha256"]:
        raise ConfigurationError(f"{label}在封存后漂移或丢失。")


def _verify_capture_seal_preview(
    campaign_dir: Path,
    stage: dict[str, Any],
    canonical: str,
) -> None:
    """证明阶段 payload 仍是人工批准的同一组机器事实。"""

    phase = "candidate" if canonical == "capture-candidate" else "official"
    candidate_id = stage.get("candidate_id") if phase == "candidate" else None
    attempt_path = _campaign_file(
        campaign_dir,
        str(stage["attempt"]["path"]),
    )
    attempt_payload = _read_json(attempt_path, "抓包 attempt")
    attempt_id = attempt_payload.get("attempt_id")
    if not isinstance(attempt_id, str):
        raise ConfigurationError("抓包 attempt 缺少 attempt-id。")
    attempt_root, attempt = _load_capture_attempt(
        campaign_dir,
        phase,
        candidate_id,
        attempt_id,
    )
    preview_path = _campaign_file(
        campaign_dir,
        str(stage["seal_preview"]["path"]),
    )
    if preview_path.parent != attempt_root:
        raise ConfigurationError("seal 预览与抓包 attempt 不在同一目录。")
    preview = _read_json(preview_path, "seal 预览")

    envelope_fields = {
        "schema_version",
        "stage",
        "campaign_id",
        "candidate_id",
        "campaign_manifest_sha256",
        "sealed_at_utc",
        "package_digest",
        "result_schema_version",
        "seal_preview",
    }
    stage_payload = {
        key: value for key, value in stage.items() if key not in envelope_fields
    }
    expected_core: dict[str, Any] = {
        "schema_version": SEAL_PREVIEW_SCHEMA,
        "campaign_id": attempt["campaign_id"],
        "phase": phase,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "attempt_digest": attempt["attempt_digest"],
        "stage_payload_sha256": _fingerprint(stage_payload),
        "evidence_inventory_digest": stage_payload["evidence_inventory"]["digest"],
        "assertion_manifest_sha256": stage_payload["assertion_context"][
            "capture_manifest"
        ]["sha256"],
        "restoration_report_sha256": stage_payload["restoration"]["report"][
            "sha256"
        ],
    }
    if phase == "candidate":
        expected_core.update(
            {
                "post_client_restoration_sha256": stage_payload["restoration"]
                ["post_client"]["report"]["sha256"],
                "observed_profile_sha256": stage_payload["observed_profile"][
                    "sha256"
                ],
                "client_receipt_sha256": {
                    item["client_id"]: item["receipt"]["sha256"]
                    for item in stage_payload["client_bindings"]
                },
            }
        )
    review_sha256 = _fingerprint(expected_core)
    expected_preview = {
        **expected_core,
        "status": "approval_required",
        "review_sha256": review_sha256,
    }
    if preview != expected_preview:
        raise ConfigurationError("阶段收据与人工批准的 seal 预览不一致。")


def _latest_attempt_summary(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
) -> dict[str, Any] | None:
    """返回指定抓包边界最近一个可验证 attempt 的摘要。"""

    for path, reservation in _ordered_capture_attempts(
        campaign_dir,
        phase,
        candidate_id,
    ):
        attempt_path = path / "attempt.json"
        if not attempt_path.exists():
            return {
                "attempt_id": path.name,
                "status": "reserved_or_interrupted",
                "seal_preview": False,
                "run_nonce": reservation["run_nonce"],
                "attempt_started_at_utc": reservation["started_at_utc"],
                "evidence_root": None,
            }
        _, attempt = _load_capture_attempt(
            campaign_dir, phase, candidate_id, path.name
        )
        client_checkpoint_at: str | None = None
        if phase == "candidate":
            environment = attempt.get("environment")
            evidence_root = Path(
                str(environment.get("evidence_root", ""))
                if isinstance(environment, dict)
                else ""
            )
            checkpoint_receipt = (
                evidence_root / "receipts" / "client-restoration-report.json"
            )
            checkpoint_manifest = (
                evidence_root
                / "environment"
                / "client-after"
                / "probe-manifest.json"
            )
            checkpoint_present = checkpoint_receipt.exists() or checkpoint_manifest.exists()
            if checkpoint_present:
                if (
                    not checkpoint_receipt.is_file()
                    or checkpoint_receipt.is_symlink()
                    or not checkpoint_manifest.is_file()
                    or checkpoint_manifest.is_symlink()
                ):
                    raise ConfigurationError("Kilo 后检查点材料不完整或不可信。")
                _validate_restoration_report(
                    checkpoint_receipt,
                    [evidence_root],
                    phase="candidate",
                    candidate_id=str(candidate_id),
                )
                checkpoint = _read_json(
                    checkpoint_manifest, "Kilo 后探针清单"
                )
                if checkpoint.get("phase") != "after" or not _is_rfc3339_timestamp(
                    checkpoint.get("observed_at_utc")
                ):
                    raise ConfigurationError("Kilo 后探针清单身份或时间非法。")
                client_checkpoint_at = str(checkpoint["observed_at_utc"])
        preview_path = path / "seal-preview.json"
        preview_exists = preview_path.exists() or preview_path.is_symlink()
        if preview_exists:
            if preview_path.is_symlink() or not preview_path.is_file():
                raise ConfigurationError("seal 预览路径不可信。")
            preview = _read_json(preview_path, "seal 预览")
            core = {
                key: value
                for key, value in preview.items()
                if key not in {"status", "review_sha256"}
            }
            if (
                preview.get("schema_version") != SEAL_PREVIEW_SCHEMA
                or preview.get("campaign_id") != attempt.get("campaign_id")
                or preview.get("phase") != phase
                or preview.get("candidate_id") != candidate_id
                or preview.get("attempt_id") != path.name
                or preview.get("attempt_digest") != attempt.get("attempt_digest")
                or preview.get("status") != "approval_required"
                or preview.get("review_sha256") != _fingerprint(core)
            ):
                raise ConfigurationError("seal 预览身份或复核摘要不一致。")
        return {
            "attempt_id": path.name,
            "status": attempt["status"],
            "seal_preview": preview_exists,
            "client_checkpoint_at_utc": client_checkpoint_at,
            "run_nonce": attempt["run_nonce"],
            "attempt_started_at_utc": attempt["started_at_utc"],
            "evidence_root": (
                attempt.get("environment", {}).get("evidence_root")
                if isinstance(attempt.get("environment"), dict)
                else None
            ),
        }
    return None


def _ordered_capture_attempts(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
) -> list[tuple[Path, dict[str, Any]]]:
    """按预约微秒时间倒序排列 attempt，编号仅作为同刻平局键。"""

    relative = _capture_attempt_relative(phase, candidate_id)
    root = campaign_dir / relative / "attempts"
    if not root.is_dir() or root.is_symlink():
        return []
    attempts: list[tuple[Path, dict[str, Any]]] = []
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink() or not SAFE_ID_RE.fullmatch(path.name):
            continue
        reservation = _load_capture_reservation(
            campaign_dir,
            path,
            phase=phase,
            candidate_id=candidate_id,
        )
        attempts.append((path, reservation))
    return sorted(
        attempts,
        key=lambda item: (
            _rfc3339_datetime(
                item[1]["started_at_utc"], "抓包预约 started_at_utc"
            ),
            item[0].name,
        ),
        reverse=True,
    )


def _campaign_attempt_roots(
    campaign_dir: Path,
) -> list[tuple[str, str | None, Path]]:
    """枚举 Campaign 内全部正式 attempt 目录，忽略未发布的隐藏临时目录。"""

    scopes: list[tuple[str, str | None, Path]] = [
        ("official", None, campaign_dir / "official" / "attempts")
    ]
    candidates_root = campaign_dir / "candidates"
    if candidates_root.exists():
        if candidates_root.is_symlink() or not candidates_root.is_dir():
            raise ConfigurationError("候选抓包目录不可信。")
        for candidate_root in sorted(candidates_root.iterdir()):
            if not candidate_root.is_dir() or candidate_root.is_symlink():
                continue
            if not SAFE_ID_RE.fullmatch(candidate_root.name):
                raise ConfigurationError("候选抓包目录包含非法 candidate-id。")
            scopes.append(
                (
                    "candidate",
                    candidate_root.name,
                    candidate_root / "attempts",
                )
            )

    attempts: list[tuple[str, str | None, Path]] = []
    for phase, current_candidate_id, attempts_root in scopes:
        if not attempts_root.exists():
            continue
        if attempts_root.is_symlink() or not attempts_root.is_dir():
            raise ConfigurationError("抓包 attempts 目录不可信。")
        for attempt_root in sorted(attempts_root.iterdir()):
            if not attempt_root.is_dir() or attempt_root.is_symlink():
                continue
            if not SAFE_ID_RE.fullmatch(attempt_root.name):
                if attempt_root.name.startswith(".reservation-"):
                    continue
                raise ConfigurationError("抓包目录包含非法 attempt-id。")
            attempts.append((phase, current_candidate_id, attempt_root))
    return attempts


def _campaign_contamination_records(campaign_dir: Path) -> list[str]:
    """从主 attempt／seal 失败事实推导污染，旁路 marker 仅作冗余提示。"""

    records: list[str] = []
    marker = campaign_dir / "environment-contaminated.json"
    if marker.exists() or marker.is_symlink():
        records.append("campaign-marker")
    for phase, current_candidate_id, attempt_root in _campaign_attempt_roots(
        campaign_dir
    ):
        _load_capture_reservation(
            campaign_dir,
            attempt_root,
            phase=phase,
            candidate_id=current_candidate_id,
        )
        attempt_path = attempt_root / "attempt.json"
        if attempt_path.exists() or attempt_path.is_symlink():
            if attempt_path.is_symlink() or not attempt_path.is_file():
                raise ConfigurationError("抓包 attempt 收据路径不可信。")
            _, attempt = _load_capture_attempt(
                campaign_dir,
                phase,
                current_candidate_id,
                attempt_root.name,
            )
            if attempt.get("status") == "environment_contaminated":
                records.append(
                    f"{current_candidate_id or phase}:{attempt_root.name}:attempt"
                )
        seal_failure = attempt_root / "seal-failure.json"
        if seal_failure.exists() or seal_failure.is_symlink():
            if seal_failure.is_symlink() or not seal_failure.is_file():
                raise ConfigurationError("候选 seal 失败收据路径不可信。")
            failure = _read_json(seal_failure, "候选 seal 失败收据")
            failure_digest = failure.get("failure_digest")
            unsigned_failure = dict(failure)
            unsigned_failure.pop("failure_digest", None)
            reservation = _load_capture_reservation(
                campaign_dir,
                attempt_root,
                phase=phase,
                candidate_id=current_candidate_id,
            )
            if (
                phase != "candidate"
                or failure.get("schema_version") != SEAL_FAILURE_SCHEMA
                or failure.get("campaign_id") != reservation.get("campaign_id")
                or failure.get("campaign_manifest_sha256")
                != reservation.get("campaign_manifest_sha256")
                or failure.get("phase") != "candidate"
                or failure.get("candidate_id") != current_candidate_id
                or failure.get("attempt_id") != attempt_root.name
                or failure.get("run_nonce") != reservation.get("run_nonce")
                or not _is_rfc3339_timestamp(failure.get("failed_at_utc"))
                or failure.get("reason") != "Kilo 后环境恢复门禁失败"
                or not isinstance(failure.get("error_type"), str)
                or not failure["error_type"]
                or not SHA256_RE.fullmatch(str(failure_digest))
                or _fingerprint(unsigned_failure) != failure_digest
            ):
                raise ConfigurationError("候选 seal 失败收据身份或摘要不一致。")
            records.append(
                f"{current_candidate_id or phase}:{attempt_root.name}:seal"
            )
    return records


def _reject_contaminated_campaign(campaign_dir: Path) -> None:
    """任何主污染事实存在时，除只读 status 外禁止继续使用 Campaign。"""

    records = _campaign_contamination_records(campaign_dir)
    if records:
        raise ConfigurationError(
            "环境恢复失败已封锁 Campaign；只能只读 status，并在人工恢复后新建 "
            f"Campaign。污染事实={records}"
        )


def campaign_status(
    campaign_dir: Path,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """从不可变阶段收据推导状态，不回写 Campaign 核心清单。"""

    if candidate_id is not None and not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("status --candidate-id 格式非法。")
    manifest = load_campaign_manifest(campaign_dir)
    stage_status: dict[str, Any] = {}
    for stage in ("capture-official", "classify"):
        try:
            stage_status[stage] = _load_stage_result(campaign_dir, stage).get(
                "status", "unknown"
            )
        except ConfigurationError as error:
            if "尚未封存" not in str(error):
                raise
            stage_status[stage] = "pending"
    candidates_root = campaign_dir / "candidates"
    candidates = sorted(
        path.name
        for path in candidates_root.iterdir()
        if (
            path.is_dir()
            and not path.is_symlink()
            and SAFE_ID_RE.fullmatch(path.name)
            and (path / "result.json").is_file()
            and not (path / "result.json").is_symlink()
        )
    ) if candidates_root.is_dir() else []
    comparisons: list[str] = []
    acceptance: list[str] = []
    candidate_states: dict[str, str] = {}
    for current_candidate_id in candidates:
        candidate_stage = _load_stage_result(
            campaign_dir, "capture-candidate", current_candidate_id
        )
        if candidate_stage.get("status") != "complete":
            raise ConfigurationError("候选抓包阶段尚未完整封存。")
        candidate_states[current_candidate_id] = "candidate_sealed"
        for stage, output in (("compare", comparisons), ("accept", acceptance)):
            try:
                value = _load_stage_result(campaign_dir, stage, current_candidate_id)
            except ConfigurationError as error:
                if "尚未封存" not in str(error):
                    raise
                continue
            if value.get("status") == "complete":
                output.append(current_candidate_id)
                candidate_states[current_candidate_id] = (
                    "ready" if stage == "accept" else "compared"
                )
                if stage == "accept":
                    _verify_campaign_binding(
                        campaign_dir, value.get("assertion_result"), "逐规则断言结果"
                    )
                    _verify_campaign_binding(
                        campaign_dir, value.get("evidence_seal"), "验收证据封印"
                    )
                    _verify_stage_evidence(
                        _load_stage_result(campaign_dir, "capture-official"),
                        "官方",
                    )
                    _verify_stage_evidence(
                        _load_stage_result(
                            campaign_dir,
                            "capture-candidate",
                            current_candidate_id,
                        ),
                        "候选",
                    )
    contamination_records = _campaign_contamination_records(campaign_dir)
    contaminated = bool(contamination_records)
    active_unsealed = {
        "official": _active_unsealed_attempts(campaign_dir, "official"),
        "candidate": _active_unsealed_attempts(campaign_dir, "candidate"),
    }
    failed_attempts = {
        "official": _failed_capture_attempts(campaign_dir, "official"),
        "candidate": _failed_capture_attempts(campaign_dir, "candidate"),
    }
    official_attempt = (
        None
        if stage_status["capture-official"] == "complete"
        else _latest_attempt_summary(campaign_dir, "official", None)
    )
    candidate_attempt = (
        _latest_attempt_summary(campaign_dir, "candidate", candidate_id)
        if candidate_id is not None and candidate_id not in candidate_states
        else None
    )
    if contaminated:
        status = "environment_contaminated"
        next_command = "人工恢复并证明环境洁净后新建 Campaign"
    elif (
        stage_status["capture-official"] == "complete"
        and active_unsealed["official"]
    ) or (
        candidate_id is not None
        and candidate_id in candidate_states
        and active_unsealed["candidate"]
    ):
        status = "capture_state_inconsistent"
        next_command = "人工审计额外未封存预约并新建 Campaign"
    elif candidate_id is not None and candidate_id in candidate_states:
        status = candidate_states[candidate_id]
        next_command = {
            "candidate_sealed": "compare",
            "compared": "accept",
            "ready": "按已封存身份执行受控灰度",
        }[status]
    elif candidate_attempt is not None:
        if candidate_attempt["status"] == "reserved_or_interrupted":
            status = "candidate_capture_interrupted"
            next_command = "人工审计孤儿预约并新建 Campaign；不得自动重跑"
        elif candidate_attempt["status"] == "awaiting_receipts":
            if candidate_attempt["seal_preview"]:
                status = "candidate_awaiting_seal_approval"
                next_command = (
                    "capture-candidate seal --approve-seal-sha256 <review_sha256>"
                )
            elif candidate_attempt.get("client_checkpoint_at_utc"):
                status = "candidate_client_checkpoint_created"
                next_command = "生成 nonce／时间绑定收据后重新执行 capture-candidate seal"
            else:
                status = "candidate_awaiting_client_checkpoint"
                next_command = "Kilo 两入口完成后执行首次 capture-candidate seal"
        else:
            status = "candidate_capture_failed"
            next_command = "修复失败任务后使用 resume --rerun-failed"
    elif candidate_id is None and (
        active_unsealed["candidate"] or failed_attempts["candidate"]
    ):
        status = "candidate_selection_required"
        next_command = "从 attempt 列表选择原 candidate-id 执行 status／seal／resume"
    elif candidate_id is not None and stage_status["classify"] == "complete":
        status = "profile_approved"
        next_command = "capture-candidate"
    elif acceptance:
        status = "ready"
        next_command = "指定 --candidate-id 查看或续跑单个候选"
    elif comparisons:
        status = "compared"
        next_command = "指定 --candidate-id 执行 accept"
    elif candidates:
        status = "candidate_sealed"
        next_command = "指定 --candidate-id 执行 compare"
    elif stage_status["classify"] == "blocked":
        status = "blocked"
        next_command = "解决阻塞并创建新的分类 revision"
    elif stage_status["classify"] == "complete":
        status = "profile_approved"
        next_command = "capture-candidate"
    elif stage_status["capture-official"] == "complete":
        status = "official_sealed"
        next_command = "classify"
    elif official_attempt is not None:
        if official_attempt["status"] == "reserved_or_interrupted":
            status = "official_capture_interrupted"
            next_command = "人工审计孤儿预约并新建 Campaign；不得自动重跑"
        elif official_attempt["status"] == "awaiting_receipts":
            status = (
                "official_awaiting_seal_approval"
                if official_attempt["seal_preview"]
                else "official_awaiting_receipts"
            )
            next_command = (
                "capture-official seal --approve-seal-sha256 <review_sha256>"
                if official_attempt["seal_preview"]
                else "完成机器 finalizer 后执行 capture-official seal"
            )
        else:
            status = "official_capture_failed"
            next_command = "修复失败任务后使用 resume --rerun-failed"
    else:
        status = "planned"
        next_command = "capture-official"
    return {
        "schema_version": "codex-upgrade-status/v1",
        "campaign_id": manifest["campaign_id"],
        "status": status,
        "stages": stage_status,
        "candidates": candidates,
        "comparisons": comparisons,
        "accepted_candidates": acceptance,
        "candidate_states": candidate_states,
        "official_attempt": official_attempt,
        "candidate_attempt": candidate_attempt,
        "selected_candidate_id": candidate_id,
        "active_unsealed_attempts": active_unsealed,
        "failed_attempts": failed_attempts,
        "contamination_records": contamination_records,
        "next_command": next_command,
    }


def _campaign_arguments(
    campaign_dir: Path,
    manifest: dict[str, Any],
    *,
    candidate_id: str | None = None,
    runtime_image: str | None = None,
    profile_id: str | None = None,
    profile_digest: str | None = None,
    build_id: str | None = None,
    deployed_version: str | None = None,
    candidate_image_id: str | None = None,
    source_tree_sha256: str | None = None,
) -> argparse.Namespace:
    configuration = manifest["configuration"]
    run_id = manifest["campaign_id"]
    if candidate_id:
        run_id = f"{run_id}-{candidate_id}"
    extra_reference = manifest.get("inputs", {}).get("extra_jobs")
    return argparse.Namespace(
        command="capture-candidate" if candidate_id else "capture-official",
        baseline_version=manifest["baseline_version"],
        target_version=manifest["target_version"],
        baseline_source=Path(configuration["baseline_source"]),
        target_source=Path(configuration["target_source"]),
        baseline_evidence=Path(configuration["baseline_evidence"]),
        target_sha256=manifest["target_sha256"],
        target_package=Path(configuration["target_package"]),
        target_package_sha256=manifest["official_identity"]["package"][
            "asset_sha256"
        ],
        target_code_mode_host_sha256=manifest["official_identity"]["package"][
            "code_mode_host_sha256"
        ],
        target_package_identity=manifest["official_identity"]["package"],
        runtime_image=runtime_image or configuration["runtime_image"],
        output=campaign_dir,
        campaign_dir=campaign_dir,
        rule_manifest=_campaign_file(
            campaign_dir, manifest["inputs"]["baseline_rules"]["path"]
        ),
        scenario_manifest=_campaign_file(
            campaign_dir, manifest["inputs"]["discovery_scenarios"]["path"]
        ),
        extra_jobs=(
            _campaign_file(campaign_dir, extra_reference["path"])
            if extra_reference
            else None
        ),
        suite=manifest["suite"],
        campaign_id=run_id,
        model=configuration["model"],
        # 历史 Campaign Schema 尚无 lite_model；只按其冻结 target_version
        # 恢复当时受管轨道。新 Campaign 在 plan 阶段必须显式写入该字段。
        lite_model=(
            configuration.get("lite_model")
            or track_models_for_version(manifest["target_version"], "lite")[0]
        ),
        capture_root=Path(configuration["capture_root"]),
        capture_container=configuration["capture_container"],
        service_container=configuration["service_container"],
        keeper_container=configuration["keeper_container"],
        postgres_container=configuration["postgres_container"],
        redis_container=configuration["redis_container"],
        capture_codex_bin=configuration["capture_codex_bin"],
        relay_codex_bin=configuration["relay_codex_bin"],
        capture_code_mode_host_bin=configuration["capture_code_mode_host_bin"],
        relay_code_mode_host_bin=configuration["relay_code_mode_host_bin"],
        codex_account_id=int(configuration["codex_account_id"]),
        api_key_id=int(configuration["api_key_id"]),
        live_attestation_compose_dir=str(
            configuration.get("live_attestation_compose_dir", "") or ""
        ),
        live_attestation_compose_files=str(
            configuration.get("live_attestation_compose_files", "") or ""
        ),
        candidate_id=candidate_id,
        profile_id=profile_id,
        profile_digest=profile_digest,
        build_id=build_id,
        deployed_version=deployed_version,
        candidate_image_id=candidate_image_id,
        source_tree_sha256=source_tree_sha256,
    )


def _approved_rules(
    campaign_dir: Path,
    manifest: dict[str, Any],
    *,
    require_approved: bool,
) -> tuple[str, ...]:
    if require_approved:
        classification = _load_stage_result(campaign_dir, "classify")
        if classification.get("status") != "complete":
            raise ConfigurationError("目标规则迁移尚未批准。")
        reference = classification.get("target_rule_manifest")
        if not isinstance(reference, dict):
            raise ConfigurationError("分类收据缺少目标规则清单。")
        path = _campaign_file(campaign_dir, str(reference.get("path", "")))
        if not path.is_file() or file_sha256(path) != reference.get("sha256"):
            raise ConfigurationError("目标规则清单摘要不一致。")
        return load_rule_manifest(path, manifest["target_version"])
    reference = manifest["inputs"]["baseline_rules"]
    return load_rule_manifest(
        _campaign_file(campaign_dir, reference["path"]),
        manifest["baseline_version"],
    )


def _campaign_jobs(
    campaign_dir: Path,
    manifest: dict[str, Any],
    phase: str,
    *,
    candidate_id: str | None = None,
    runtime_image: str | None = None,
    profile_id: str | None = None,
    profile_digest: str | None = None,
    build_id: str | None = None,
    deployed_version: str | None = None,
    candidate_image_id: str | None = None,
    source_tree_sha256: str | None = None,
    use_approved_scenario: bool | None = None,
) -> list[Job]:
    arguments = _campaign_arguments(
        campaign_dir,
        manifest,
        candidate_id=candidate_id,
        runtime_image=runtime_image,
        profile_id=profile_id,
        profile_digest=profile_digest,
        build_id=build_id,
        deployed_version=deployed_version,
        candidate_image_id=candidate_image_id,
        source_tree_sha256=source_tree_sha256,
    )
    approved_target = (
        phase == "candidate"
        if use_approved_scenario is None
        else use_approved_scenario
    )
    if approved_target:
        classification = _load_stage_result(campaign_dir, "classify")
        if classification.get("status") != "complete":
            raise ConfigurationError("目标版本场景尚未批准。")
        scenario_reference = classification.get("scenario_manifest")
        if not isinstance(scenario_reference, dict):
            raise ConfigurationError("分类收据缺少目标场景清单。")
        scenario_path = _campaign_file(
            campaign_dir, str(scenario_reference.get("path", ""))
        )
        if (
            not scenario_path.is_file()
            or scenario_path.is_symlink()
            or file_sha256(scenario_path) != scenario_reference.get("sha256")
        ):
            raise ConfigurationError("目标场景清单摘要不一致。")
        arguments.scenario_manifest = scenario_path
    context = _job_context(arguments)
    jobs = load_scenario_jobs(
        arguments.scenario_manifest,
        context,
        expected_version=(
            manifest["target_version"]
            if approved_target
            else manifest["baseline_version"]
        ),
        require_bindings=True,
    )
    if not approved_target:
        jobs.extend(load_extra_jobs(arguments.extra_jobs, context))
    jobs = [
        job
        for job in jobs
        if job.phase == phase and manifest["suite"] in job.suites
    ]
    rules = _approved_rules(
        campaign_dir, manifest, require_approved=approved_target
    )
    _validate_jobs(jobs, rules)
    if not jobs:
        raise ConfigurationError(f"场景清单没有 {phase} 阶段任务。")
    return jobs


def _verify_plan_identity(campaign_dir: Path, manifest: dict[str, Any]) -> None:
    target_source = Path(manifest["configuration"]["target_source"])
    expected = manifest["official_identity"]
    cargo_lock = target_source / "Cargo.lock"
    current_identity = {
        "source_tree_sha256": _directory_tree_digest(target_source),
        "cargo_lock_sha256": file_sha256(cargo_lock) if cargo_lock.is_file() else None,
    }
    for field, value in current_identity.items():
        if value != expected.get(field):
            raise ConfigurationError(f"官方目标身份漂移：{field}")
    package_identity = expected.get("package")
    if not isinstance(package_identity, dict):
        raise ConfigurationError("官方目标身份缺少 package 闭包。")
    current_package_identity = _verify_codex_package(
        Path(manifest["configuration"]["target_package"]),
        expected_version=manifest["target_version"],
        expected_package_sha256=str(package_identity.get("asset_sha256", "")),
        expected_binary_sha256=manifest["target_sha256"],
        expected_code_mode_host_sha256=str(
            package_identity.get("code_mode_host_sha256", "")
        ),
    )
    if current_package_identity != package_identity:
        raise ConfigurationError("官方目标 package 身份漂移。")
    current_tool = _tool_identity(include_git=False)
    expected_tool = manifest["tool_identity"]
    if current_tool["files_sha256"] == expected_tool["files_sha256"]:
        return
    # 工具确实变了。按证据影响面分级判定，而不是一律拒绝：产出侧改动会改变证据字节，
    # 必须整轮重来；评估侧改动只改变「怎么判断」，已封存证据逐字节不变，重新评估即可。
    #
    # 兼容：plan 时未记录分组摘要的旧 Campaign 无法证明其分级前提，退回严格拒绝。
    if "production_sha256" not in expected_tool:
        raise ConfigurationError(
            "升级工具摘要在 plan 后发生变化（该 Campaign 的 plan 未记录分组摘要，"
            "无法分级判定，只能整轮重建）。"
        )
    drift = _tool_identity_drift(current_tool, expected_tool)
    if drift["production"] or (
        current_tool["production_sha256"] != expected_tool["production_sha256"]
    ):
        raise ConfigurationError(
            "升级工具的产出侧在 plan 后发生变化，证据字节前提已不成立："
            + "、".join(drift["production"] or ["<摘要不一致但无法定位文件>"])
        )
    # 只有评估侧变化：放行，但把变化清单落进台账供审计，避免静默放行。
    _record_evaluation_side_drift(campaign_dir, current_tool, expected_tool, drift)


TOOL_EVALUATION_DRIFT_SCHEMA = "codex-upgrade-tool-evaluation-drift/v1"


def _record_evaluation_side_drift(
    campaign_dir: Path,
    current_tool: Mapping[str, Any],
    expected_tool: Mapping[str, Any],
    drift: Mapping[str, list[str]],
) -> None:
    """把被放行的评估侧漂移追加进独立台账，作为不可省略的审计痕迹。

    放行不等于无痕：每次评估侧改动都要留下「改了哪些文件、放行时的新摘要」，
    否则 accept 阶段无法解释某轮结果是用哪一版判据算出来的。

    台账另立文件而不写回 `campaign.json`——后者由 `campaign.sha256` 保护且一次封存
    不可变，追加内容会直接破坏 Campaign 完整性校验。
    """

    if not drift["evaluation"]:
        return
    path = campaign_dir / "tool-evaluation-drift.json"
    if path.is_symlink():
        raise ConfigurationError("评估侧漂移台账不允许是符号链接。")
    if path.is_file():
        ledger = _read_json(path, "评估侧漂移台账")
        if ledger.get("schema_version") != TOOL_EVALUATION_DRIFT_SCHEMA:
            raise ConfigurationError("评估侧漂移台账 schema_version 不受支持。")
        records = ledger.get("records")
        if not isinstance(records, list):
            raise ConfigurationError("评估侧漂移台账结构非法。")
    else:
        records = []
    record = {
        "changed_files": list(drift["evaluation"]),
        "plan_evaluation_sha256": str(expected_tool.get("evaluation_sha256", "")),
        "current_evaluation_sha256": current_tool["evaluation_sha256"],
        "production_sha256": current_tool["production_sha256"],
        "files_sha256": current_tool["files_sha256"],
    }
    # 同一份评估侧状态重复进入不再追加，台账按「不同的评估侧版本」计数。
    if records and records[-1].get("current_evaluation_sha256") == (
        record["current_evaluation_sha256"]
    ):
        return
    records.append(record)
    payload = {
        "schema_version": TOOL_EVALUATION_DRIFT_SCHEMA,
        "plan_production_sha256": str(expected_tool.get("production_sha256", "")),
        "records": records,
    }
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _directory_tree_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ConfigurationError(f"候选源码目录不存在或不可信：{root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        entries.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return _fingerprint({"entries": entries})


def _container_image_id(container: str) -> str | None:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", container],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if value.startswith("sha256:") else None


def _image_repo_digests(image_id: str) -> set[str]:
    """读取 Docker config image ID 对应的 OCI 仓库摘要集合。"""

    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image_id,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ConfigurationError("无法读取候选镜像 RepoDigests。") from error
    if (
        not isinstance(payload, list)
        or not payload
        or any(
            not isinstance(value, str)
            or not IMMUTABLE_IMAGE_RE.fullmatch(value)
            for value in payload
        )
    ):
        raise ConfigurationError("候选镜像缺少合法、不可变的 RepoDigests。")
    return set(payload)


def _verify_container_image_reference(
    container: str,
    image_reference: str,
    expected_image_id: str | None = None,
) -> str:
    """分别验证运行容器 config image ID 与其 OCI 仓库摘要。"""

    actual_image_id = _container_image_id(container)
    if not actual_image_id or not IMAGE_ID_RE.fullmatch(actual_image_id):
        raise ConfigurationError("无法读取运行容器的实际 image ID。")
    if expected_image_id is not None and actual_image_id != expected_image_id:
        raise ConfigurationError("运行容器实际 image ID 与冻结身份不一致。")
    if image_reference not in _image_repo_digests(actual_image_id):
        raise ConfigurationError(
            "--runtime-image 不是运行镜像实际 RepoDigests 中的不可变引用。"
        )
    return actual_image_id


def _validate_codex_identity(
    *,
    path: str,
    sha256: str,
    version_output: str,
    expected_sha256: str,
    expected_version: str,
    label: str,
) -> dict[str, str]:
    match = re.fullmatch(r"codex-cli (?P<version>[0-9]+\.[0-9]+\.[0-9]+)", version_output)
    if sha256 != expected_sha256 or not match or match.group("version") != expected_version:
        raise ConfigurationError(f"{label} Codex 二进制版本或 SHA-256 不一致。")
    return {
        "label": label,
        "path": path,
        "version": match.group("version"),
        "version_output": version_output,
        "sha256": sha256,
    }


def _verify_official_binaries(manifest: dict[str, Any]) -> dict[str, Any]:
    """在任何真实官方请求前验证所有可能执行的 Codex 二进制。"""

    configuration = manifest["configuration"]
    expected_sha256 = manifest["target_sha256"]
    expected_version = manifest["target_version"]
    container = configuration["capture_container"]
    runtime_image_reference = str(manifest["official_identity"]["runtime_image"])
    runtime_image_id = _verify_container_image_reference(
        container,
        runtime_image_reference,
    )
    container_probe = (
        "import hashlib,json,pathlib,subprocess,sys;"
        "p=pathlib.Path(sys.argv[1]);"
        "h=hashlib.sha256(p.read_bytes()).hexdigest();"
        "r=subprocess.run([str(p),'--version'],capture_output=True,text=True,timeout=30);"
        "print(json.dumps({'sha256':h,'version':(r.stdout or r.stderr).strip(),'return_code':r.returncode}))"
    )
    identities: list[dict[str, str]] = []
    for name in ("capture_codex_bin", "relay_codex_bin"):
        binary = str(configuration[name])
        try:
            result = subprocess.run(
                ["docker", "exec", container, "python3", "-c", container_probe, binary],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            payload = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"无法验证容器内 {name}：{error}") from error
        if not isinstance(payload, dict) or payload.get("return_code") != 0:
            raise ConfigurationError(f"容器内 {name} 无法执行 --version。")
        identities.append(
            _validate_codex_identity(
                path=binary,
                sha256=str(payload.get("sha256", "")),
                version_output=str(payload.get("version", "")),
                expected_sha256=expected_sha256,
                expected_version=expected_version,
                label=f"container:{name}",
            )
        )

    host_relay = Path(configuration["relay_codex_bin"])
    if (
        host_relay.is_symlink()
        or not host_relay.is_file()
        or not os.access(host_relay, os.X_OK)
    ):
        raise ConfigurationError("宿主机 relay_codex_bin 不存在、不可信或不可执行。")
    try:
        host_version = subprocess.run(
            [str(host_relay), "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigurationError(f"无法验证宿主机 relay_codex_bin：{error}") from error
    identities.append(
        _validate_codex_identity(
            path=str(host_relay),
            sha256=file_sha256(host_relay),
            version_output=(host_version.stdout or host_version.stderr).strip(),
            expected_sha256=expected_sha256,
            expected_version=expected_version,
            label="host:relay_codex_bin",
        )
    )
    package_identity = manifest["official_identity"].get("package")
    if not isinstance(package_identity, dict):
        raise ConfigurationError("官方目标身份缺少 package 闭包。")
    expected_helper_sha256 = str(
        package_identity.get("code_mode_host_sha256", "")
    )
    helper_probe = (
        "import hashlib,json,os,pathlib,sys;"
        "p=pathlib.Path(sys.argv[1]);"
        "print(json.dumps({'is_file':p.is_file(),'is_symlink':p.is_symlink(),"
        "'executable':os.access(p,os.X_OK),'sha256':"
        "hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ''}))"
    )
    helpers: list[dict[str, str]] = []
    for name in ("capture_code_mode_host_bin", "relay_code_mode_host_bin"):
        helper = str(configuration[name])
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "python3",
                    "-c",
                    helper_probe,
                    helper,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            payload = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"无法验证容器内 {name}：{error}") from error
        if (
            not isinstance(payload, dict)
            or payload.get("is_file") is not True
            or payload.get("is_symlink") is not False
            or payload.get("executable") is not True
            or payload.get("sha256") != expected_helper_sha256
        ):
            raise ConfigurationError(f"容器内 {name} 与官方 package 不一致。")
        helpers.append(
            {
                "label": f"container:{name}",
                "path": helper,
                "sha256": expected_helper_sha256,
            }
        )

    host_helper = Path(configuration["relay_code_mode_host_bin"])
    if (
        host_helper.is_symlink()
        or not host_helper.is_file()
        or not os.access(host_helper, os.X_OK)
        or file_sha256(host_helper) != expected_helper_sha256
    ):
        raise ConfigurationError(
            "宿主机 relay_code_mode_host_bin 与官方 package 不一致。"
        )
    helpers.append(
        {
            "label": "host:relay_code_mode_host_bin",
            "path": str(host_helper),
            "sha256": expected_helper_sha256,
        }
    )
    return {
        "passed": True,
        "expected_version": expected_version,
        "expected_sha256": expected_sha256,
        "runtime_image_reference": runtime_image_reference,
        "runtime_image_id": runtime_image_id,
        "identities": identities,
        "package": package_identity,
        "helpers": helpers,
    }


def _evidence_files(roots: Iterable[Path]) -> list[tuple[str, Path]]:
    root_map = _evidence_root_map(roots)
    output: dict[Path, str] = {}
    for root, prefix in root_map:
        if not root.exists() or root.is_symlink():
            continue
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in files:
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.name if root.is_file() else path.relative_to(root).as_posix()
            output[path.resolve()] = f"{prefix}/{relative}"
    return [(output[path], path) for path in sorted(output)]


def _evidence_root_map(roots: Iterable[Path]) -> list[tuple[Path, str]]:
    """为多个证据根生成稳定且无冲突的逻辑前缀。"""

    resolved = sorted({path.resolve(strict=False) for path in roots})
    counts: dict[str, int] = {}
    for root in resolved:
        counts[root.name] = counts.get(root.name, 0) + 1
    return [
        (
            root,
            root.name if counts[root.name] == 1 else f"{index:03d}-{root.name}",
        )
        for index, root in enumerate(resolved, 1)
    ]


def _evidence_file_binding(
    path: Path,
    roots: Iterable[Path],
    *,
    label: str,
) -> tuple[dict[str, str], Path, str]:
    """把非符号链接证据文件绑定到唯一证据根和逻辑清单路径。"""

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label}必须是证据根内的非符号链接普通文件。")
    resolved = path.resolve(strict=True)
    matches: list[tuple[Path, str, Path]] = []
    for root, prefix in _evidence_root_map(roots):
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            relative = resolved.relative_to(root.resolve(strict=True))
        except ValueError:
            continue
        matches.append((root.resolve(strict=True), prefix, relative))
    if len(matches) != 1:
        raise ConfigurationError(f"{label}必须唯一归属于一个已收集证据根。")
    root, prefix, relative = matches[0]
    logical = f"{prefix}/{relative.as_posix()}"
    return {"path": logical, "sha256": file_sha256(resolved)}, root, prefix


def _evidence_inventory(roots: Iterable[Path]) -> dict[str, Any]:
    entries = [
        {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for relative, path in _evidence_files(roots)
    ]
    return {
        "entry_count": len(entries),
        "entries": entries,
        "digest": _fingerprint({"entries": entries}),
    }


def _evidence_security(roots: Iterable[Path]) -> dict[str, Any]:
    secret_names = [
        name
        for name in (
            "ADMIN_BEARER_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "SUB2API_API_KEY",
        )
        if os.environ.get(name)
    ]
    report = scan_files_for_secrets(
        _evidence_files(roots), secret_env_names=secret_names
    )
    return {
        "known_secret_scan_passed": bool(report["passed"]),
        "known_secret_env_names": report["known_secret_env_names"],
        "file_count": report["file_count"],
        "scanned_bytes": report["scanned_bytes"],
        "findings": report["findings"],
        "limitation": (
            None
            if secret_names
            else "未读取容器内 OAuth 凭据值；仍执行令牌形态启发式扫描。"
        ),
    }


def _evidence_permissions_private(roots: Iterable[Path]) -> bool:
    """确认原始证据根、目录和文件均未向 group/other 开放。"""

    for root in {path.resolve(strict=False) for path in roots}:
        if not root.exists() or root.is_symlink():
            return False
        paths = [root] if root.is_file() else [root, *root.rglob("*")]
        for path in paths:
            if path.is_symlink() or path.stat().st_mode & 0o077:
                return False
    return True


def _resolve_receipt(
    explicit: Path | None,
    roots: list[Path],
    filename: str,
    *,
    label: str,
) -> Path:
    if explicit is not None:
        return explicit
    matches = sorted(
        path
        for root in roots
        if root.is_dir() and not root.is_symlink()
        for path in root.rglob(filename)
        if path.is_file() and not path.is_symlink()
    )
    if len(matches) != 1:
        raise ConfigurationError(f"{label}必须显式提供或在证据根内唯一发现。")
    return matches[0]


def _capture_assertion_context(
    manifest_path: Path | None,
    requested_root: Path | None,
    evidence_roots: list[Path],
    *,
    target_version: str,
) -> dict[str, Any]:
    """定位断言证据包并返回单根断言上下文。

    主手册 §4.4.3 规定：断言器只读取一个证据根，即 attempt 内的
    `assertion-bundle/`；它由 ACC-02 从各 job 根只读收口而来，自包含 manifest、
    原件与派生观测。因此这里返回的 `evidence_root` 是 bundle 目录本身，而
    `evidence_prefix` 是 bundle 在封存 inventory 中的逻辑前缀（`<所属根>/
    assertion-bundle`）——这是此前路径空间失配的修复点：
    机器 check 的相对路径加上该前缀后，必须逐字命中 inventory 条目。
    """

    path = _resolve_receipt(
        manifest_path,
        evidence_roots,
        "capture-manifest.json",
        label="统一 capture manifest",
    )
    binding, owning_root, owning_prefix = _evidence_file_binding(
        path, evidence_roots, label="统一 capture manifest"
    )
    bundle_dir = path.parent.resolve(strict=True)
    if bundle_dir.name != ASSERTION_BUNDLE_DIR_NAME:
        raise ConfigurationError(
            "统一 capture manifest 必须位于 attempt 的 "
            f"{ASSERTION_BUNDLE_DIR_NAME}/ 断言证据包内。"
        )
    if bundle_dir == owning_root:
        raise ConfigurationError(
            f"{ASSERTION_BUNDLE_DIR_NAME}/ 必须是已收集证据根内的子目录，"
            "不能自成独立证据根。"
        )
    relative_bundle = bundle_dir.relative_to(owning_root).as_posix()
    prefix = f"{owning_prefix}/{relative_bundle}"
    if requested_root is not None and requested_root.resolve(strict=True) != bundle_dir:
        raise ConfigurationError(
            "--assertion-evidence-root 与断言证据包目录不一致。"
        )
    try:
        load_assertion_observations(path, bundle_dir, target_version)
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"统一 capture manifest 验证失败：{error}") from error
    return {
        "capture_manifest": binding,
        "capture_manifest_path": str(path.resolve(strict=True)),
        "evidence_root": str(bundle_dir),
        "evidence_prefix": prefix,
    }


def _run_seal_assertion_gate(
    assertion_context: dict[str, Any],
    roots: list[Path],
    *,
    phase: str,
    target_version: str,
) -> dict[str, Any]:
    """ACC-03：seal 前按分侧验收契约执行断言门禁，任一失败拒绝封存。"""

    bundle_dir = Path(assertion_context["evidence_root"])
    # provenance 里的 source_root 名即封存 inventory 的逻辑前缀，重放时按同一
    # 映射回到真实目录；bundle 是某个根内的子目录，不单独充当来源根。
    source_roots = {prefix: root for root, prefix in _evidence_root_map(roots)}
    try:
        profile = load_acceptance_profile(acceptance_profile_path())
        contract = verify_frozen_contract(profile)
        return run_assertion_gate(
            bundle_dir=bundle_dir,
            source_roots=source_roots,
            side="official" if phase == "official" else "candidate",
            profile=profile,
            contract=contract,
            target_version=target_version,
        )
    except (AcceptanceContractError, AssertionGateError) as error:
        raise ConfigurationError(
            f"{phase} seal 断言门禁失败：{error}"
        ) from error


def _validate_restoration_report(
    report_path: Path | None,
    evidence_roots: list[Path],
    *,
    phase: str,
    candidate_id: str | None,
) -> dict[str, Any]:
    path = _resolve_receipt(
        report_path,
        evidence_roots,
        "restoration-report.json",
        label="环境恢复报告",
    )
    binding, root, _ = _evidence_file_binding(
        path, evidence_roots, label="环境恢复报告"
    )
    try:
        report = replay_receipt(path, root, expected_subcommand="restoration")
    except (ReceiptFinalizerError, OSError, ValueError) as error:
        raise ConfigurationError(f"环境恢复报告无法由机器 finalizer 重放：{error}") from error
    if (
        report.get("schema_version") != RESTORATION_SCHEMA
        or report.get("phase") != phase
        or report.get("candidate_id") != candidate_id
        or report.get("status") != "restored"
    ):
        raise ConfigurationError("环境恢复报告身份或状态不一致。")
    checks = report.get("checks")
    required_checks = {
        "service_state_restored",
        "container_state_restored",
        "database_state_preserved",
        "account_state_preserved",
        "configuration_state_restored",
    }
    if not isinstance(checks, list):
        raise ConfigurationError("环境恢复报告 checks 必须是数组。")
    seen: set[str] = set()
    for check in checks:
        if not isinstance(check, dict):
            raise ConfigurationError("环境恢复检查结构非法。")
        check_id = check.get("id")
        if (
            not isinstance(check_id, str)
            or check_id in seen
            or check.get("passed") is not True
        ):
            raise ConfigurationError("环境恢复检查缺失、重复或未通过。")
        seen.add(check_id)
    if seen != required_checks:
        raise ConfigurationError(
            "环境恢复报告检查集合不闭合："
            f"缺少={sorted(required_checks - seen)}，多余={sorted(seen - required_checks)}"
        )
    return {"passed": True, "report": binding, "checks": checks}


def _validate_observed_profile_receipt(
    receipt_path: Path | None,
    evidence_roots: list[Path],
    *,
    campaign_id: str,
    attempt_id: str,
    run_nonce: str,
    attempt_started_at_utc: str,
    client_checkpoint_at_utc: str,
    candidate_id: str,
    target_version: str,
    expected_profile_id: str,
    expected_profile_digest: str,
    image_id: str,
    image_reference: str,
    source_tree_sha256: str,
    build_id: str,
    deployed_version: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    path = _resolve_receipt(
        receipt_path,
        evidence_roots,
        "observed-profile.json",
        label="运行画像观测收据",
    )
    binding, root, _ = _evidence_file_binding(
        path, evidence_roots, label="运行画像观测收据"
    )
    try:
        receipt = replay_receipt(
            path, root, expected_subcommand="observed-profile"
        )
    except (ReceiptFinalizerError, OSError, ValueError) as error:
        raise ConfigurationError(
            f"运行画像观测收据无法由机器 finalizer 重放：{error}"
        ) from error
    expected = {
        "schema_version": OBSERVED_PROFILE_SCHEMA,
        "status": "active",
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "run_nonce": run_nonce,
        "attempt_started_at_utc": attempt_started_at_utc,
        "client_checkpoint_at_utc": client_checkpoint_at_utc,
        "candidate_id": candidate_id,
        "target_version": target_version,
        "profile_id": expected_profile_id,
        "profile_digest": expected_profile_digest,
        "image_id": image_id,
        "image_reference": image_reference,
        "source_tree_sha256": source_tree_sha256,
        "build_id": build_id,
        "deployed_version": deployed_version,
        "source": "sub2api-runtime",
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ConfigurationError(f"运行画像观测收据 {field} 不一致。")
    return binding, receipt


def _parse_client_evidence(
    values: Iterable[str],
    evidence_roots: list[Path],
    *,
    campaign_id: str,
    attempt_id: str,
    run_nonce: str,
    attempt_started_at_utc: str,
    client_checkpoint_at_utc: str,
    candidate_id: str,
    target_version: str,
    model: str,
    identity: dict[str, Any],
) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        client_id, separator, raw_path = value.partition("=")
        if (
            not separator
            or not SAFE_ID_RE.fullmatch(client_id)
            or client_id in seen
        ):
            raise ConfigurationError(f"--client-evidence 格式非法或重复：{value}")
        path = Path(raw_path)
        binding, root, _ = _evidence_file_binding(
            path, evidence_roots, label=f"第三方入口 {client_id} 收据"
        )
        try:
            receipt = replay_receipt(
                path, root, expected_subcommand="kilo-binding"
            )
        except (ReceiptFinalizerError, OSError, ValueError) as error:
            raise ConfigurationError(
                f"第三方入口 {client_id} 收据无法由机器 finalizer 重放：{error}"
            ) from error
        expected_protocols = {
            "kilo-compatible": "openai-compatible",
            "kilo-responses": "openai-responses",
        }
        expected_entrypoints = {
            "kilo-compatible": "/v1/chat/completions",
            "kilo-responses": "/v1/responses",
        }
        expected = {
            "schema_version": CLIENT_BINDING_SCHEMA,
            "status": "success",
            "campaign_id": campaign_id,
            "attempt_id": attempt_id,
            "run_nonce": run_nonce,
            "attempt_started_at_utc": attempt_started_at_utc,
            "client_checkpoint_at_utc": client_checkpoint_at_utc,
            "client_id": client_id,
            "protocol": expected_protocols.get(client_id),
            "entrypoint": expected_entrypoints.get(client_id),
            "model": model,
            "candidate_id": candidate_id,
            "target_version": target_version,
            "profile_id": identity.get("profile_id"),
            "profile_digest": identity.get("profile_digest"),
            "candidate_image_id": identity.get("image_id"),
            "source_tree_sha256": identity.get("source_tree_sha256"),
            "build_id": identity.get("build_id"),
            "deployed_version": identity.get("deployed_version"),
        }
        if expected["protocol"] is None:
            raise ConfigurationError(f"第三方入口 {client_id} 未声明协议契约。")
        for field, expected_value in expected.items():
            if receipt.get(field) != expected_value:
                raise ConfigurationError(f"第三方入口 {client_id} 的 {field} 不一致。")
        if not isinstance(receipt.get("client_version"), str) or not receipt["client_version"].strip():
            raise ConfigurationError(f"第三方入口 {client_id} 缺少客户端版本。")
        seen.add(client_id)
        bindings.append(
            {
                "client_id": client_id,
                "status": "success",
                "campaign_id": receipt["campaign_id"],
                "attempt_id": receipt["attempt_id"],
                "run_nonce": receipt["run_nonce"],
                "attempt_started_at_utc": receipt["attempt_started_at_utc"],
                "client_checkpoint_at_utc": receipt[
                    "client_checkpoint_at_utc"
                ],
                "client_version": receipt["client_version"],
                "protocol": receipt["protocol"],
                "entrypoint": receipt["entrypoint"],
                "model": receipt["model"],
                "profile_id": receipt["profile_id"],
                "profile_digest": receipt["profile_digest"],
                "receipt": binding,
                "source_tree_sha256": receipt["source_tree_sha256"],
                "build_id": receipt["build_id"],
                "deployed_version": receipt["deployed_version"],
                "request_evidence": receipt["request_evidence"],
                "response_evidence": receipt["response_evidence"],
                "request_proof": receipt["request_proof"],
                "response_proof": receipt["response_proof"],
                "raw_evidence": receipt["raw_evidence"],
            }
        )
    return bindings


def _load_capture_reservation(
    campaign_dir: Path,
    attempt_root: Path,
    *,
    phase: str,
    candidate_id: str | None,
) -> dict[str, Any]:
    """读取 attempt 原子发布前即存在的不可变预约收据。"""

    path = attempt_root / "reservation.json"
    _reject_symlink_components(path, campaign_dir, "抓包预约收据")
    if not path.is_file() or path.is_symlink():
        raise ConfigurationError(f"抓包 attempt 缺少原子预约收据：{attempt_root.name}")
    payload = _read_json(path, "抓包预约收据")
    required = {
        "schema_version",
        "campaign_id",
        "campaign_manifest_sha256",
        "phase",
        "candidate_id",
        "attempt_id",
        "run_nonce",
        "started_at_utc",
        "identity_sha256",
        "planned_jobs",
        "reservation_digest",
    }
    digest = payload.get("reservation_digest")
    unsigned = dict(payload)
    unsigned.pop("reservation_digest", None)
    manifest = load_campaign_manifest(campaign_dir)
    if (
        set(payload) != required
        or payload.get("schema_version") != CAPTURE_RESERVATION_SCHEMA
        or payload.get("campaign_id") != manifest["campaign_id"]
        or payload.get("campaign_manifest_sha256")
        != file_sha256(campaign_dir / "campaign.json")
        or payload.get("phase") != phase
        or payload.get("candidate_id")
        != (candidate_id if phase == "candidate" else None)
        or payload.get("attempt_id") != attempt_root.name
        or not RUN_NONCE_RE.fullmatch(str(payload.get("run_nonce", "")))
        or not _is_rfc3339_timestamp(payload.get("started_at_utc"))
        or not SHA256_RE.fullmatch(str(payload.get("identity_sha256", "")))
        or not SHA256_RE.fullmatch(str(digest))
        or _fingerprint(unsigned) != digest
    ):
        raise ConfigurationError("抓包预约身份或摘要不一致。")
    planned_jobs = payload.get("planned_jobs")
    if not isinstance(planned_jobs, list) or not planned_jobs:
        raise ConfigurationError("抓包预约缺少计划任务。")
    seen: set[str] = set()
    for item in planned_jobs:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "required", "execution_sha256"}
            or not SAFE_ID_RE.fullmatch(str(item.get("id", "")))
            or item.get("id") in seen
            or not isinstance(item.get("required"), bool)
            or not SHA256_RE.fullmatch(str(item.get("execution_sha256", "")))
        ):
            raise ConfigurationError("抓包预约计划任务非法、重复或摘要缺失。")
        seen.add(str(item["id"]))
    return payload


def _reserve_capture_attempt(
    campaign_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
    identity: dict[str, Any],
    jobs: list[Job],
    allow_failed_rerun: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """在跨进程锁内原子发布预约，关闭 check-then-create 与空目录窗口。"""

    relative = _capture_attempt_relative(phase, candidate_id)
    with _campaign_lock(campaign_dir):
        _reject_contaminated_campaign(campaign_dir)
        canonical = "capture-official" if phase == "official" else "capture-candidate"
        _, result_path = _stage_path(campaign_dir, canonical, candidate_id)
        if result_path.exists() or result_path.is_symlink():
            raise ConfigurationError(f"{canonical} 已封存，禁止创建新 attempt。")
        active = _active_unsealed_attempts(campaign_dir, phase)
        if active:
            raise ConfigurationError(
                f"Campaign 存在未封存预约或 attempt，禁止并行 run：{active}"
            )
        failed = _failed_capture_attempts(campaign_dir, phase)
        if failed and not allow_failed_rerun:
            raise ConfigurationError(
                "存在失败 attempt；只能显式使用 resume --rerun-failed，禁止直接 "
                f"capture-* run 绕过：{failed}"
            )

        attempts_root = ensure_private_directory(
            campaign_dir / relative / "attempts", campaign_dir
        )
        attempt_id = (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            + f"-{secrets.token_hex(8)}"
        )
        final_root = attempts_root / attempt_id
        if final_root.exists() or final_root.is_symlink():
            raise ConfigurationError("随机 attempt-id 发生冲突。")
        run_nonce = secrets.token_hex(32)
        reservation: dict[str, Any] = {
            "schema_version": CAPTURE_RESERVATION_SCHEMA,
            "campaign_id": load_campaign_manifest(campaign_dir)["campaign_id"],
            "campaign_manifest_sha256": file_sha256(
                campaign_dir / "campaign.json"
            ),
            "phase": phase,
            "candidate_id": candidate_id if phase == "candidate" else None,
            "attempt_id": attempt_id,
            "run_nonce": run_nonce,
            "started_at_utc": _utc_now(),
            "identity_sha256": _fingerprint(identity),
            "planned_jobs": [
                {
                    "id": job.job_id,
                    "required": job.required,
                    "execution_sha256": _job_execution_sha256(job),
                }
                for job in jobs
            ],
        }
        reservation["reservation_digest"] = _fingerprint(reservation)

        temporary_root = Path(
            tempfile.mkdtemp(prefix=".reservation-", dir=attempts_root)
        )
        temporary_root.chmod(0o700)
        try:
            _secure_write_json_once(
                temporary_root / "reservation.json", reservation
            )
            os.rename(temporary_root, final_root)
            directory_descriptor = os.open(
                attempts_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            if temporary_root.exists() and not temporary_root.is_symlink():
                (temporary_root / "reservation.json").unlink(missing_ok=True)
                try:
                    temporary_root.rmdir()
                except OSError:
                    pass
            raise
        return final_root, reservation


def _prior_complete_results(
    campaign_dir: Path,
    relative: Path,
    jobs: list[Job],
    *,
    phase: str,
    candidate_id: str | None,
    identity: dict[str, Any],
) -> list[dict[str, Any]]:
    """验证最近失败 attempt 身份；新 attempt 为防漂移始终重跑全部任务。"""

    attempts_root = campaign_dir / relative / "attempts"
    if not attempts_root.is_dir() or attempts_root.is_symlink():
        raise ConfigurationError("--rerun-failed 找不到先前失败 attempt。")
    expected_jobs = {job.job_id: job for job in jobs}
    for attempt, _ in _ordered_capture_attempts(
        campaign_dir,
        phase,
        candidate_id,
    ):
        receipt = attempt / "attempt.json"
        if not receipt.is_file() or receipt.is_symlink():
            continue
        _, payload = _load_capture_attempt(
            campaign_dir,
            phase,
            candidate_id,
            attempt.name,
        )
        if payload.get("status") != "failed":
            continue
        if _fingerprint(payload.get("identity")) != _fingerprint(identity):
            raise ConfigurationError(
                "先前失败 attempt 身份与本次重跑不一致；不得在同一 Campaign 混用身份，"
                "请新建 Campaign。"
            )
        results = payload.get("results")
        if not isinstance(results, list):
            continue
        completed = []
        for item in results:
            if not isinstance(item, dict) or item.get("status") != "complete":
                continue
            job_id = item.get("id")
            if (
                not isinstance(job_id, str)
                or job_id not in expected_jobs
                or item.get("execution_sha256")
                != _job_execution_sha256(expected_jobs[job_id])
            ):
                raise ConfigurationError("先前失败 attempt 的已完成任务定义漂移。")
            completed.append(item)
        if len({item["id"] for item in completed}) != len(completed):
            raise ConfigurationError("先前失败 attempt 含重复任务收据。")
        # 承接上一轮已完成的任务，只重跑失败项。这不是无条件复用：调用方必须在本轮
        # before 探针采完后调用 _verify_environment_continuity，证明「上一轮 after」到
        # 「本轮 before」之间环境没有漂移。窗口因此首尾相接，被承接的证据仍处在探针
        # 证明范围内；一旦环境变了，旧证据的前提就不成立，必须整轮重采。
        for item in completed:
            item["carried_from_attempt"] = attempt.name
        return completed
    raise ConfigurationError("--rerun-failed 找不到同身份失败 attempt。")


# 承接旧结果时要求逐字相同的探针种类。database 必然随采集增长（usage_logs 等水位表），
# 由 restoration 的 before_subset 规则单独覆盖，不在此处比较。
CONTINUITY_PROBE_KINDS = ("service", "containers", "account", "configuration")


def _probe_snapshot_digests(manifest: Mapping[str, Any]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for snapshot in manifest.get("snapshots") or []:
        if not isinstance(snapshot, dict):
            continue
        kind = snapshot.get("kind")
        digest = snapshot.get("sha256")
        if isinstance(kind, str) and isinstance(digest, str):
            digests[kind] = digest
    return digests


def _verify_environment_continuity(
    campaign_dir: Path,
    relative: Path,
    carried_attempt_ids: set[str],
    before_manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
    """证明被承接 attempt 的收尾环境与本轮起始环境一致。

    承接旧任务结果的前提是环境没有在两轮之间发生漂移——否则那些证据描述的是另一套
    环境。比较 service／containers／account／configuration 四类探针快照；database 会随
    采集自然增长，由 restoration 的 before_subset 规则单独覆盖。
    """

    if not carried_attempt_ids:
        return None
    if len(carried_attempt_ids) != 1:
        raise ConfigurationError("承接结果必须全部来自同一个先前 attempt。")
    source_attempt = next(iter(carried_attempt_ids))
    after_path = (
        campaign_dir
        / relative
        / "attempts"
        / source_attempt
        / "evidence"
        / "environment"
        / "after"
        / "probe-manifest.json"
    )
    if after_path.is_symlink() or not after_path.is_file():
        raise ConfigurationError("被承接 attempt 缺少 after 环境探针，无法证明连续性。")
    after_manifest = _read_json(after_path, "被承接 attempt 的 after 探针")
    previous = _probe_snapshot_digests(after_manifest)
    current = _probe_snapshot_digests(before_manifest)
    drifted = [
        kind
        for kind in CONTINUITY_PROBE_KINDS
        if previous.get(kind) != current.get(kind)
    ]
    if drifted:
        raise ConfigurationError(
            "承接失败：上一轮结束后环境已漂移（"
            + "、".join(drifted)
            + "），被承接任务的证据前提不再成立，请不带 --rerun-failed 整轮重采。"
        )
    return {
        "schema_version": "codex-upgrade-attempt-continuity/v1",
        "source_attempt_id": source_attempt,
        "compared_kinds": list(CONTINUITY_PROBE_KINDS),
        "source_after_probe_sha256": file_sha256(after_path),
    }


def _capture_attempt_relative(phase: str, candidate_id: str | None) -> Path:
    """返回抓包 attempt 的阶段相对目录。"""

    if phase == "official":
        return Path("official")
    if not candidate_id or not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("候选 attempt 必须绑定合法 candidate-id。")
    return Path("candidates") / candidate_id


def _capture_attempt_path(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
) -> Path:
    """解析并约束一个既有抓包 attempt 路径。"""

    if not SAFE_ID_RE.fullmatch(attempt_id):
        raise ConfigurationError("--attempt-id 格式非法。")
    relative = _capture_attempt_relative(phase, candidate_id)
    path = campaign_dir / relative / "attempts" / attempt_id
    _reject_symlink_components(path, campaign_dir, "抓包 attempt")
    if not path.is_dir() or path.is_symlink():
        raise ConfigurationError(f"抓包 attempt 不存在或不可信：{attempt_id}")
    return path


def _write_capture_attempt(
    campaign_dir: Path,
    attempt_root: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """只写一次封存 run 阶段 attempt，不允许 seal 回写。"""

    phase = str(payload.get("phase", ""))
    candidate_id = payload.get("candidate_id")
    reservation = _load_capture_reservation(
        campaign_dir,
        attempt_root,
        phase=phase,
        candidate_id=(str(candidate_id) if candidate_id is not None else None),
    )
    identity = payload.get("identity")
    if not isinstance(identity, dict) or _fingerprint(identity) != reservation.get(
        "identity_sha256"
    ):
        raise ConfigurationError("抓包 attempt 身份与原子预约不一致。")
    planned = {
        item["id"]: item["execution_sha256"]
        for item in reservation["planned_jobs"]
    }
    for result in payload.get("results", []):
        if (
            not isinstance(result, dict)
            or result.get("id") not in planned
            or result.get("execution_sha256") != planned[result["id"]]
        ):
            raise ConfigurationError("抓包 attempt 任务不在原子预约内或执行摘要漂移。")

    document = dict(payload)
    document["schema_version"] = CAPTURE_ATTEMPT_SCHEMA
    document["campaign_manifest_sha256"] = file_sha256(
        campaign_dir / "campaign.json"
    )
    document["attempt_id"] = attempt_root.name
    document["run_nonce"] = reservation["run_nonce"]
    document["started_at_utc"] = reservation["started_at_utc"]
    document["completed_at_utc"] = _utc_now()
    document["reservation"] = {
        "path": str((attempt_root / "reservation.json").relative_to(campaign_dir)),
        "sha256": file_sha256(attempt_root / "reservation.json"),
    }
    document["attempt_digest"] = _fingerprint(document)
    _secure_write_json_once(attempt_root / "attempt.json", document)
    return document


def _load_capture_attempt(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
) -> tuple[Path, dict[str, Any]]:
    """读取并重验 run 阶段的不可变 attempt。"""

    attempt_root = _capture_attempt_path(
        campaign_dir, phase, candidate_id, attempt_id
    )
    path = attempt_root / "attempt.json"
    _reject_symlink_components(path, campaign_dir, "抓包 attempt 收据")
    payload = _read_json(path, "抓包 attempt 收据")
    reservation = _load_capture_reservation(
        campaign_dir,
        attempt_root,
        phase=phase,
        candidate_id=candidate_id,
    )
    expected_digest = payload.get("attempt_digest")
    unsigned = dict(payload)
    unsigned.pop("attempt_digest", None)
    expected_candidate = candidate_id if phase == "candidate" else None
    manifest = load_campaign_manifest(campaign_dir)
    if (
        payload.get("schema_version") != CAPTURE_ATTEMPT_SCHEMA
        or payload.get("campaign_id") != manifest["campaign_id"]
        or payload.get("campaign_manifest_sha256")
        != file_sha256(campaign_dir / "campaign.json")
        or payload.get("attempt_id") != attempt_id
        or payload.get("phase") != phase
        or payload.get("candidate_id") != expected_candidate
        or payload.get("run_nonce") != reservation.get("run_nonce")
        or payload.get("started_at_utc") != reservation.get("started_at_utc")
        or not _is_rfc3339_timestamp(payload.get("completed_at_utc"))
        or _fingerprint(payload.get("identity"))
        != reservation.get("identity_sha256")
        or payload.get("reservation")
        != {
            "path": str((attempt_root / "reservation.json").relative_to(campaign_dir)),
            "sha256": file_sha256(attempt_root / "reservation.json"),
        }
        or not SHA256_RE.fullmatch(str(expected_digest))
        or _fingerprint(unsigned) != expected_digest
    ):
        raise ConfigurationError("抓包 attempt 身份或摘要不一致。")
    if _rfc3339_datetime(
        payload["completed_at_utc"], "attempt.completed_at_utc"
    ) < _rfc3339_datetime(
        payload["started_at_utc"], "attempt.started_at_utc"
    ):
        raise ConfigurationError("抓包 attempt 完成时间早于原子预约时间。")
    if payload.get("status") not in {
        "awaiting_receipts",
        "failed",
        "environment_contaminated",
    }:
        raise ConfigurationError("抓包 attempt 状态非法。")
    return attempt_root, payload


def _active_unsealed_attempts(
    campaign_dir: Path,
    phase: str,
) -> list[str]:
    """列出未完成预约或未被阶段结果绑定的 attempt。"""

    scopes: list[tuple[str | None, Path]] = []
    if phase == "official":
        scopes.append((None, campaign_dir / "official"))
    else:
        candidates_root = campaign_dir / "candidates"
        if not candidates_root.exists():
            return []
        if candidates_root.is_symlink() or not candidates_root.is_dir():
            raise ConfigurationError("候选抓包目录不可信。")
        for candidate_root in sorted(candidates_root.iterdir()):
            if not candidate_root.is_dir() or candidate_root.is_symlink():
                continue
            if not SAFE_ID_RE.fullmatch(candidate_root.name):
                raise ConfigurationError("候选抓包目录包含非法 candidate-id。")
            scopes.append((candidate_root.name, candidate_root))

    active: list[str] = []
    for candidate_id, scope in scopes:
        sealed_attempt_id: str | None = None
        result_path = scope / "result.json"
        if result_path.exists() or result_path.is_symlink():
            if result_path.is_symlink() or not result_path.is_file():
                raise ConfigurationError("抓包阶段结果路径不可信。")
            stage = _load_stage_result(
                campaign_dir,
                "capture-official" if phase == "official" else "capture-candidate",
                candidate_id,
            )
            attempt_reference = stage.get("attempt")
            if not isinstance(attempt_reference, dict):
                raise ConfigurationError("已封存抓包阶段缺少 attempt 绑定。")
            sealed_attempt_id = Path(str(attempt_reference.get("path", ""))).parent.name
        attempts_root = scope / "attempts"
        if not attempts_root.exists():
            continue
        if attempts_root.is_symlink() or not attempts_root.is_dir():
            raise ConfigurationError("抓包 attempts 目录不可信。")
        for attempt_root in sorted(attempts_root.iterdir()):
            if not attempt_root.is_dir() or attempt_root.is_symlink():
                continue
            if not SAFE_ID_RE.fullmatch(attempt_root.name):
                # 原子发布前的隐藏临时目录不属于可见 attempt 命名空间。
                if attempt_root.name.startswith(".reservation-"):
                    continue
                raise ConfigurationError("抓包目录包含非法 attempt-id。")
            _load_capture_reservation(
                campaign_dir,
                attempt_root,
                phase=phase,
                candidate_id=candidate_id,
            )
            attempt_path = attempt_root / "attempt.json"
            if not attempt_path.exists():
                active.append(
                    f"{candidate_id or 'official'}:{attempt_root.name}:reserved_or_interrupted"
                )
                continue
            if attempt_path.is_symlink() or not attempt_path.is_file():
                raise ConfigurationError("抓包 attempt 收据路径不可信。")
            _, attempt = _load_capture_attempt(
                campaign_dir,
                phase,
                candidate_id,
                attempt_root.name,
            )
            if (
                attempt.get("status") == "awaiting_receipts"
                and attempt_root.name != sealed_attempt_id
            ):
                active.append(
                    f"{candidate_id or 'official'}:{attempt_root.name}"
                )
    return active


def _failed_capture_attempts(campaign_dir: Path, phase: str) -> list[str]:
    """列出尚未通过显式 resume 处理的失败 attempt。"""

    failed: list[str] = []
    for current_phase, candidate_id, attempt_root in _campaign_attempt_roots(
        campaign_dir
    ):
        if current_phase != phase or not (attempt_root / "attempt.json").is_file():
            continue
        _, attempt = _load_capture_attempt(
            campaign_dir,
            phase,
            candidate_id,
            attempt_root.name,
        )
        if attempt.get("status") == "failed":
            failed.append(f"{candidate_id or 'official'}:{attempt_root.name}")
    return failed


def _candidate_identity_for_run(
    arguments: argparse.Namespace,
    manifest: dict[str, Any],
    classification: dict[str, Any],
) -> dict[str, Any]:
    """在任何候选请求发出前冻结实际候选身份。"""

    required = {
        "runtime_image": getattr(arguments, "runtime_image", None),
        "build_id": getattr(arguments, "build_id", None),
        "deployed_version": getattr(arguments, "deployed_version", None),
        "profile_id": getattr(arguments, "profile_id", None),
        "profile_digest": getattr(arguments, "profile_digest", None),
    }
    missing = sorted(field for field, value in required.items() if not value)
    if missing:
        raise ConfigurationError(f"候选 run 缺少身份参数：{missing}")
    if not IMMUTABLE_IMAGE_RE.fullmatch(str(arguments.runtime_image)):
        raise ConfigurationError(
            "候选 --runtime-image 必须是 repository@sha256:<manifest-digest>。"
        )
    if not SHA256_RE.fullmatch(str(arguments.profile_digest)):
        raise ConfigurationError("--profile-digest 必须是 64 位小写 SHA-256。")
    for field in ("profile_id", "build_id", "deployed_version"):
        if not SAFE_ID_RE.fullmatch(str(getattr(arguments, field))):
            raise ConfigurationError(f"--{field.replace('_', '-')} 格式非法。")
    approved_profile_id, approved_profile_digest = _profile_binding_from_manifest(
        arguments.campaign_dir, classification
    )
    if (
        arguments.profile_id != approved_profile_id
        or arguments.profile_digest != approved_profile_digest
    ):
        raise ConfigurationError("候选运行画像 ID／digest 与批准画像不一致。")
    if arguments.candidate_image_id and not IMAGE_ID_RE.fullmatch(
        arguments.candidate_image_id
    ):
        raise ConfigurationError("--candidate-image-id 格式非法。")
    source_root = arguments.candidate_source or Path(__file__).resolve().parents[2]
    if not source_root.is_dir() or source_root.is_symlink():
        raise ConfigurationError("--candidate-source 不存在或不是可信目录。")
    image_id = _verify_container_image_reference(
        manifest["configuration"]["service_container"],
        str(arguments.runtime_image),
        arguments.candidate_image_id,
    )
    return {
        "git_commit": _git_commit(source_root),
        "source_root": str(source_root.resolve(strict=True)),
        "source_tree_sha256": _directory_tree_digest(source_root),
        "image_reference": arguments.runtime_image,
        "image_digest": "sha256:"
        + arguments.runtime_image.rsplit("sha256:", 1)[-1],
        "image_id": image_id,
        "build_id": arguments.build_id,
        "deployed_version": arguments.deployed_version,
        "profile_id": arguments.profile_id,
        "profile_digest": arguments.profile_digest,
    }


def _verify_candidate_attempt_identity(
    manifest: dict[str, Any], identity: dict[str, Any]
) -> None:
    """seal 前重验源码树和运行容器，防止 run／seal 间换包。"""

    source_root = Path(str(identity.get("source_root", "")))
    if (
        not source_root.is_absolute()
        or not source_root.is_dir()
        or source_root.is_symlink()
        or _directory_tree_digest(source_root) != identity.get("source_tree_sha256")
    ):
        raise ConfigurationError("候选源码树在 run／seal 之间发生漂移。")
    _verify_container_image_reference(
        manifest["configuration"]["service_container"],
        str(identity.get("image_reference", "")),
        str(identity.get("image_id", "")),
    )


def _deduplicate_evidence_roots(
    values: Iterable[Path], *, require_nonempty: bool = True
) -> list[Path]:
    """解析证据根并拒绝符号链接、文件和重复别名。"""

    roots: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        if not value.is_absolute() or value.is_symlink() or not value.is_dir():
            raise ConfigurationError(f"证据根不存在或不可信：{value}")
        resolved = value.resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    if require_nonempty and not roots:
        raise ConfigurationError("抓包 attempt 没有可封存证据根。")
    return roots


def _environment_probe_arguments(
    manifest: dict[str, Any],
    output_dir: Path,
    phase: str,
) -> EnvironmentProbeArguments:
    """只从不可变 Campaign 配置构造环境探针参数。"""

    configuration = manifest["configuration"]
    return EnvironmentProbeArguments(
        output_dir=output_dir,
        service_container=configuration["service_container"],
        keeper_container=configuration["keeper_container"],
        postgres_container=configuration["postgres_container"],
        redis_container=configuration["redis_container"],
        capture_container=configuration["capture_container"],
        account_id=configuration["codex_account_id"],
        api_key_id=configuration["api_key_id"],
        phase=phase,
    )


def _probe_capture_environment(
    manifest: dict[str, Any],
    output_dir: Path,
    phase: str,
) -> dict[str, Any]:
    """执行独立只读探针；单独包装便于离线测试替换执行边界。"""

    return run_environment_probe(
        _environment_probe_arguments(manifest, output_dir, phase)
    )


def _finalize_attempt_restoration(
    evidence_root: Path,
    *,
    phase: str,
    candidate_id: str | None,
    before_directory: str = "before",
    after_directory: str = "after",
    output_name: str = "restoration-report.json",
) -> tuple[Path, dict[str, Any]]:
    """只根据自动探针的十份快照生成恢复收据。"""

    receipts_root = ensure_private_directory(evidence_root / "receipts", evidence_root)
    output = receipts_root / output_name
    state_names = {
        "service": ENVIRONMENT_STATE_FILES["service"],
        "container": ENVIRONMENT_STATE_FILES["containers"],
        "database": ENVIRONMENT_STATE_FILES["database"],
        "account": ENVIRONMENT_STATE_FILES["account"],
        "configuration": ENVIRONMENT_STATE_FILES["configuration"],
    }
    values: dict[str, Any] = {
        "evidence_root": evidence_root,
        "output": output.relative_to(evidence_root),
        "phase": phase,
        "candidate_id": candidate_id,
    }
    for _, before_name, after_name, _ in RESTORATION_INPUTS:
        state_key = before_name.removesuffix("_before")
        filename = state_names[state_key]
        values[before_name] = Path("environment") / before_directory / filename
        values[after_name] = Path("environment") / after_directory / filename
    receipt = finalize_restoration(argparse.Namespace(**values))
    return output, receipt


def _candidate_post_client_restoration(
    manifest: dict[str, Any],
    evidence_root: Path,
    candidate_id: str,
) -> tuple[Path, dict[str, Any], str, bool]:
    """在 Kilo 两入口完成后，再证明其间没有丢失环境或持久数据。"""

    client_after = evidence_root / "environment" / "client-after"
    receipt_path = evidence_root / "receipts" / "client-restoration-report.json"
    if receipt_path.exists():
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ConfigurationError("Kilo 后恢复收据路径不可信。")
        try:
            receipt = replay_receipt(
                receipt_path,
                evidence_root,
                expected_subcommand="restoration",
            )
        except (ReceiptFinalizerError, OSError, ValueError) as error:
            raise ConfigurationError(f"Kilo 后恢复收据无法重放：{error}") from error
        probe_manifest = _read_json(
            client_after / "probe-manifest.json", "Kilo 后探针清单"
        )
        checkpoint_at = probe_manifest.get("observed_at_utc")
        if probe_manifest.get("phase") != "after" or not _is_rfc3339_timestamp(
            checkpoint_at
        ):
            raise ConfigurationError("Kilo 后探针清单缺少可信检查点时间。")
        return receipt_path, receipt, str(checkpoint_at), False
    if client_after.exists() or client_after.is_symlink():
        raise ConfigurationError("Kilo 后探针已存在但没有可重放恢复收据。")
    _probe_capture_environment(manifest, client_after, "after")
    receipt_path, receipt = _finalize_attempt_restoration(
        evidence_root,
        phase="candidate",
        candidate_id=candidate_id,
        before_directory="after",
        after_directory="client-after",
        output_name="client-restoration-report.json",
    )
    probe_manifest = _read_json(
        client_after / "probe-manifest.json", "Kilo 后探针清单"
    )
    checkpoint_at = probe_manifest.get("observed_at_utc")
    if probe_manifest.get("phase") != "after" or not _is_rfc3339_timestamp(
        checkpoint_at
    ):
        raise ConfigurationError("Kilo 后探针清单缺少可信检查点时间。")
    return receipt_path, receipt, str(checkpoint_at), True


def _record_candidate_seal_failure(
    campaign_dir: Path,
    attempt_root: Path,
    attempt: dict[str, Any],
    error: BaseException,
) -> None:
    """把 Kilo 后恢复失败写入 attempt 主证据；Campaign marker 仅作冗余。"""

    document: dict[str, Any] = {
        "schema_version": SEAL_FAILURE_SCHEMA,
        "campaign_id": attempt["campaign_id"],
        "campaign_manifest_sha256": attempt["campaign_manifest_sha256"],
        "phase": "candidate",
        "candidate_id": attempt["candidate_id"],
        "attempt_id": attempt["attempt_id"],
        "run_nonce": attempt["run_nonce"],
        "failed_at_utc": _utc_now(),
        "error_type": type(error).__name__,
        "reason": "Kilo 后环境恢复门禁失败",
    }
    document["failure_digest"] = _fingerprint(document)
    _secure_write_json_once(attempt_root / "seal-failure.json", document)
    marker = campaign_dir / "environment-contaminated.json"
    if marker.exists() or marker.is_symlink():
        return
    try:
        _secure_write_json_once(
            marker,
            {
                "schema_version": "codex-upgrade-environment-contamination/v1",
                "phase": "candidate",
                "candidate_id": attempt["candidate_id"],
                "attempt_id": attempt["attempt_id"],
                "reason": document["reason"],
            },
        )
    except (ConfigurationError, OSError):
        # 主 seal-failure 收据已经落盘；旁路提示写失败不得抹掉污染事实。
        pass


def _attempt_evidence_binding(evidence_root: Path, path: Path) -> dict[str, Any]:
    """生成相对 attempt 证据根且含字节数的稳定文件绑定。"""

    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"attempt 派生证据不存在或不可信：{path}")
    return {
        "path": path.relative_to(evidence_root).as_posix(),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def _write_or_verify_json(path: Path, payload: dict[str, Any]) -> None:
    """创建派生 JSON；已存在时只允许逐字段一致。"""

    if path.exists():
        if path.is_symlink() or _read_json(path, "派生封存文件") != payload:
            raise ConfigurationError(f"派生封存文件已经存在且内容不一致：{path}")
        return
    _secure_write_json_once(path, payload)


def _seal_preview(
    campaign_dir: Path,
    attempt_root: Path,
    *,
    phase: str,
    candidate_id: str | None,
    attempt: dict[str, Any],
    stage_payload: dict[str, Any],
    approve_sha256: str | None,
) -> tuple[dict[str, Any], bool]:
    """生成或复核 seal 预览；人工只批准机器事实联合摘要。"""

    core = {
        "schema_version": SEAL_PREVIEW_SCHEMA,
        "campaign_id": attempt["campaign_id"],
        "phase": phase,
        "candidate_id": candidate_id,
        "attempt_id": attempt["attempt_id"],
        "attempt_digest": attempt["attempt_digest"],
        "stage_payload_sha256": _fingerprint(stage_payload),
        "evidence_inventory_digest": stage_payload["evidence_inventory"]["digest"],
        "assertion_manifest_sha256": stage_payload["assertion_context"][
            "capture_manifest"
        ]["sha256"],
        "restoration_report_sha256": stage_payload["restoration"]["report"][
            "sha256"
        ],
    }
    if phase == "candidate":
        core["post_client_restoration_sha256"] = stage_payload["restoration"][
            "post_client"
        ]["report"]["sha256"]
        core["observed_profile_sha256"] = stage_payload["observed_profile"][
            "sha256"
        ]
        core["client_receipt_sha256"] = {
            item["client_id"]: item["receipt"]["sha256"]
            for item in stage_payload["client_bindings"]
        }
    review_sha256 = _fingerprint(core)
    preview = {
        **core,
        "status": "approval_required",
        "review_sha256": review_sha256,
    }
    path = attempt_root / "seal-preview.json"
    _write_or_verify_json(path, preview)
    if approve_sha256 is None:
        return preview, False
    if not SHA256_RE.fullmatch(approve_sha256):
        raise ConfigurationError("--approve-seal-sha256 格式非法。")
    if approve_sha256 != review_sha256:
        raise ConfigurationError("seal 批准摘要与当前机器事实不一致。")
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("seal 预览路径不可信。")
    return preview, True


def _run_capture_attempt(
    arguments: argparse.Namespace,
    phase: str,
) -> dict[str, Any]:
    """执行真实抓包，并以独立前后探针自动证明环境恢复。"""

    _reject_contaminated_campaign(arguments.campaign_dir)
    # 采集脚本与 relay 从 capture_root 下的副本执行，不是本文件所在的受管树；
    # 两者漂移会让「工具身份校验通过、跑的却是旧代码」，见 _verify_execution_tree。
    _verify_execution_tree(getattr(arguments, "capture_root", None))
    seal_only = {
        "attempt_id": getattr(arguments, "attempt_id", None),
        "capture_manifest": getattr(arguments, "capture_manifest", None),
        "assertion_evidence_root": getattr(
            arguments, "assertion_evidence_root", None
        ),
        "restoration_report": getattr(arguments, "restoration_report", None),
        "evidence_root": getattr(arguments, "evidence_root", []),
        "approve_seal_sha256": getattr(
            arguments, "approve_seal_sha256", None
        ),
        "observed_profile_receipt": getattr(
            arguments, "observed_profile_receipt", None
        ),
        "client_evidence": getattr(arguments, "client_evidence", []),
    }
    unexpected = sorted(name for name, value in seal_only.items() if value)
    if unexpected:
        raise ConfigurationError(
            f"run 不读取 seal 收据参数，请在 seal 阶段提供：{unexpected}"
        )
    campaign_dir = arguments.campaign_dir
    manifest = load_campaign_manifest(campaign_dir)
    _verify_plan_identity(campaign_dir, manifest)
    if not arguments.acknowledge_live_requests:
        raise ConfigurationError(
            "抓包会产生真实请求，必须同时确认 --acknowledge-live-requests。"
        )
    candidate_id: str | None = None
    identity: dict[str, Any]
    classification: dict[str, Any] | None = None
    if phase == "official":
        try:
            _load_stage_result(campaign_dir, "capture-official")
        except ConfigurationError as error:
            if "尚未封存" not in str(error):
                raise
        else:
            raise ConfigurationError("官方证据已经封存，禁止重复抓包。")
        active_attempts = _active_unsealed_attempts(campaign_dir, "official")
        if active_attempts:
            raise ConfigurationError(
                f"官方存在待封存 attempt，禁止再次 run：{active_attempts}"
            )
        jobs = _campaign_jobs(campaign_dir, manifest, "official")
        attempt_relative = _capture_attempt_relative("official", None)
        binary_verification = _verify_official_binaries(manifest)
        identity = dict(manifest["official_identity"])
    else:
        classification = _load_stage_result(campaign_dir, "classify")
        if classification.get("status") != "complete":
            raise ConfigurationError("目标画像尚未批准，禁止候选抓包。")
        candidate_id = arguments.candidate_id
        if not SAFE_ID_RE.fullmatch(candidate_id):
            raise ConfigurationError("--candidate-id 格式非法。")
        _, candidate_result_path = _stage_path(
            campaign_dir, "capture-candidate", candidate_id
        )
        if candidate_result_path.exists():
            raise ConfigurationError("candidate-id 已封存，必须使用新编号。")
        active_attempts = _active_unsealed_attempts(campaign_dir, "candidate")
        if active_attempts:
            raise ConfigurationError(
                "Campaign 存在待封存候选 attempt，必须先完成其 Kilo 后恢复与 seal："
                f"{active_attempts}"
            )
        identity = _candidate_identity_for_run(arguments, manifest, classification)
        jobs = _campaign_jobs(
            campaign_dir,
            manifest,
            "candidate",
            candidate_id=candidate_id,
            runtime_image=identity["image_reference"],
            profile_id=identity["profile_id"],
            profile_digest=identity["profile_digest"],
            build_id=identity["build_id"],
            deployed_version=identity["deployed_version"],
            candidate_image_id=identity["image_id"],
            source_tree_sha256=identity["source_tree_sha256"],
        )
        attempt_relative = _capture_attempt_relative("candidate", candidate_id)
        binary_verification = None

    planned_jobs = list(jobs)
    prior_results: list[dict[str, Any]] = []
    if getattr(arguments, "rerun_failed", False):
        prior_results = _prior_complete_results(
            campaign_dir,
            attempt_relative,
            jobs,
            phase=phase,
            candidate_id=candidate_id,
            identity=identity,
        )
        completed_ids = {item["id"] for item in prior_results}
        jobs = [job for job in jobs if job.job_id not in completed_ids]

    attempt_root, reservation = _reserve_capture_attempt(
        campaign_dir,
        phase=phase,
        candidate_id=candidate_id,
        identity=identity,
        jobs=planned_jobs,
        allow_failed_rerun=bool(getattr(arguments, "rerun_failed", False)),
    )
    log_root = ensure_private_directory(attempt_root / "logs", campaign_dir)
    evidence_root = ensure_private_directory(
        attempt_root / "evidence", campaign_dir
    )
    environment_root = ensure_private_directory(
        evidence_root / "environment", evidence_root
    )
    if binary_verification is not None:
        _secure_write_json_once(
            attempt_root / "official-binary-verification.json",
            binary_verification,
        )
    results: list[dict[str, Any]] = list(prior_results)
    execution_error: BaseException | None = None
    restoration_error: BaseException | None = None
    before_manifest: dict[str, Any] | None = None
    after_manifest: dict[str, Any] | None = None
    restoration_path: Path | None = None
    restoration_receipt: dict[str, Any] | None = None
    continuity: dict[str, Any] | None = None
    try:
        before_manifest = _probe_capture_environment(
            manifest, environment_root / "before", "before"
        )
        continuity = _verify_environment_continuity(
            campaign_dir,
            attempt_relative,
            {
                str(item.get("carried_from_attempt"))
                for item in prior_results
                if item.get("carried_from_attempt")
            },
            before_manifest,
        )
    except BaseException as error:
        execution_error = error
    else:
        scenario_context = ScenarioReceiptContext(
            campaign_id=str(manifest["campaign_id"]),
            attempt_id=attempt_root.name,
            run_nonce=str(reservation["run_nonce"]),
            evidence_root=evidence_root,
            campaign_dir=campaign_dir,
        )
        try:
            for job in jobs:
                result = _run_job_with_retry(job, log_root, scenario_context)
                results.append(result)
                _secure_write_json_once(
                    attempt_root / f"job-{job.job_id}.json", result
                )
        except BaseException as error:
            # KeyboardInterrupt 与进程创建失败也必须先完成 after 探针。
            execution_error = error
        finally:
            try:
                after_manifest = _probe_capture_environment(
                    manifest, environment_root / "after", "after"
                )
                restoration_path, restoration_receipt = (
                    _finalize_attempt_restoration(
                        evidence_root,
                        phase=phase,
                        candidate_id=candidate_id,
                    )
                )
            except BaseException as error:
                restoration_error = error

    result_by_id = {
        result.get("id"): result
        for result in results
        if isinstance(result, dict) and isinstance(result.get("id"), str)
    }
    required_jobs_ok = execution_error is None and all(
        result_by_id.get(job.job_id, {}).get("status") == "complete"
        and result_by_id[job.job_id].get("execution_sha256")
        == _job_execution_sha256(job)
        for job in planned_jobs
        if job.required
    )
    try:
        job_evidence_roots = _deduplicate_evidence_roots(
            (
                Path(root)
                for result in results
                for root in result.get("evidence_roots", [])
            ),
            require_nonempty=required_jobs_ok,
        )
    except ConfigurationError as error:
        if execution_error is None:
            execution_error = error
        required_jobs_ok = False
        job_evidence_roots = []
    evidence_roots = _deduplicate_evidence_roots(
        [
            *job_evidence_roots,
            evidence_root,
            log_root,
        ],
        require_nonempty=False,
    )

    environment: dict[str, Any] = {
        "evidence_root": str(evidence_root.resolve(strict=True)),
        "before_probe": None,
        "after_probe": None,
        "restoration_report": None,
    }
    before_probe_path = environment_root / "before" / "probe-manifest.json"
    after_probe_path = environment_root / "after" / "probe-manifest.json"
    if before_manifest is not None:
        environment["before_probe"] = _attempt_evidence_binding(
            evidence_root, before_probe_path
        )
    if after_manifest is not None:
        environment["after_probe"] = _attempt_evidence_binding(
            evidence_root, after_probe_path
        )
    if restoration_path is not None and restoration_receipt is not None:
        environment["restoration_report"] = _attempt_evidence_binding(
            evidence_root, restoration_path
        )

    contamination: dict[str, Any] | None = None
    if before_manifest is not None and restoration_error is not None:
        contamination = {
            "schema_version": "codex-upgrade-environment-contamination/v1",
            "phase": phase,
            "candidate_id": candidate_id,
            "attempt_id": attempt_root.name,
            "reason": (
                "独立 after 探针或恢复 finalizer 未通过："
                f"{type(restoration_error).__name__}"
            ),
        }
    status = (
        "environment_contaminated"
        if contamination is not None
        else "awaiting_receipts"
        if required_jobs_ok and restoration_receipt is not None
        else "failed"
    )
    attempt = _write_capture_attempt(
        campaign_dir,
        attempt_root,
        {
            "campaign_id": manifest["campaign_id"],
            "phase": phase,
            "candidate_id": candidate_id,
            "status": status,
            "continuity": continuity,
            "identity": identity,
            "results": results,
            "evidence_roots": [str(root) for root in evidence_roots],
            "environment": environment,
            "binary_verification": binary_verification,
            "execution_error": (
                {
                    "type": type(execution_error).__name__,
                    "message": str(execution_error)[:1000],
                }
                if execution_error is not None
                else None
            ),
            "restoration_error": (
                {
                    "type": type(restoration_error).__name__,
                    "message": str(restoration_error)[:1000],
                }
                if restoration_error is not None
                else None
            ),
            "next_gate": (
                "运行 capture manifest finalizer；候选还需完成运行画像与两种 Kilo 原始 witness。"
                if status == "awaiting_receipts"
                else None
            ),
        },
    )
    if contamination is not None:
        try:
            _secure_write_json_once(
                campaign_dir / "environment-contaminated.json", contamination
            )
        except (ConfigurationError, OSError):
            # attempt.json 的 environment_contaminated 是主事实，marker 仅为提示。
            pass
        raise RuntimeError(contamination["reason"])
    if execution_error is not None:
        raise execution_error
    return {
        "status": status,
        "phase": phase,
        "candidate_id": candidate_id,
        "attempt_id": attempt_root.name,
        "attempt": str(attempt_root),
        "attempt_digest": attempt["attempt_digest"],
        "run_nonce": reservation["run_nonce"],
        "started_at_utc": reservation["started_at_utc"],
        "environment": environment,
        "results": results,
        "next_command": (
            f"capture-{phase} seal --attempt-id {attempt_root.name}"
            if status == "awaiting_receipts"
            else "修复失败任务后使用 resume --rerun-failed。"
        ),
    }


def _seal_capture_attempt(
    arguments: argparse.Namespace,
    phase: str,
) -> dict[str, Any]:
    """从不可变 attempt 与机器收据构建预览，并经摘要复核后封存阶段。"""

    campaign_dir = arguments.campaign_dir
    _reject_contaminated_campaign(campaign_dir)
    manifest = load_campaign_manifest(campaign_dir)
    _verify_plan_identity(campaign_dir, manifest)
    attempt_id = getattr(arguments, "attempt_id", None)
    if not attempt_id:
        raise ConfigurationError("seal 必须提供 --attempt-id。")
    candidate_id = arguments.candidate_id if phase == "candidate" else None
    attempt_root, attempt = _load_capture_attempt(
        campaign_dir, phase, candidate_id, attempt_id
    )
    if attempt.get("status") != "awaiting_receipts":
        raise ConfigurationError("只有 awaiting_receipts attempt 可以 seal。")
    if phase == "official":
        try:
            _load_stage_result(campaign_dir, "capture-official")
        except ConfigurationError as error:
            if "尚未封存" not in str(error):
                raise
        else:
            raise ConfigurationError("官方证据已经封存，禁止覆盖。")
        jobs = _campaign_jobs(campaign_dir, manifest, "official")
        identity = attempt["identity"]
        classification = None
        current_binary_verification = _verify_official_binaries(manifest)
        if _fingerprint(current_binary_verification) != _fingerprint(
            attempt.get("binary_verification", {})
        ):
            raise ConfigurationError("官方 CLI 二进制在 run／seal 之间发生漂移。")
    else:
        classification = _load_stage_result(campaign_dir, "classify")
        if classification.get("status") != "complete":
            raise ConfigurationError("目标画像尚未批准，禁止候选 seal。")
        identity = attempt.get("identity")
        if not isinstance(identity, dict):
            raise ConfigurationError("候选 attempt 缺少不可变身份。")
        optional_identity = {
            "runtime_image": "image_reference",
            "candidate_image_id": "image_id",
            "build_id": "build_id",
            "deployed_version": "deployed_version",
            "profile_id": "profile_id",
            "profile_digest": "profile_digest",
        }
        for argument_name, identity_name in optional_identity.items():
            value = getattr(arguments, argument_name, None)
            if value is not None and value != identity.get(identity_name):
                raise ConfigurationError(
                    f"seal 参数 --{argument_name.replace('_', '-')} 与 run 身份不一致。"
                )
        candidate_source = getattr(arguments, "candidate_source", None)
        if (
            candidate_source is not None
            and candidate_source.resolve(strict=True)
            != Path(str(identity.get("source_root", ""))).resolve(strict=True)
        ):
            raise ConfigurationError("seal 的 --candidate-source 与 run 身份不一致。")
        _verify_candidate_attempt_identity(manifest, identity)
        approved_profile_id, approved_profile_digest = _profile_binding_from_manifest(
            campaign_dir, classification
        )
        if (
            identity.get("profile_id") != approved_profile_id
            or identity.get("profile_digest") != approved_profile_digest
        ):
            raise ConfigurationError("候选 attempt 与当前批准画像不一致。")
        jobs = _campaign_jobs(
            campaign_dir,
            manifest,
            "candidate",
            candidate_id=candidate_id,
            runtime_image=str(identity.get("image_reference", "")),
            profile_id=str(identity.get("profile_id", "")),
            profile_digest=str(identity.get("profile_digest", "")),
            build_id=str(identity.get("build_id", "")),
            deployed_version=str(identity.get("deployed_version", "")),
            candidate_image_id=str(identity.get("image_id", "")),
            source_tree_sha256=str(identity.get("source_tree_sha256", "")),
        )
    _validate_capture_job_results(jobs, attempt.get("results"), phase=phase)

    roots = _deduplicate_evidence_roots(
        Path(value) for value in attempt.get("evidence_roots", [])
    )
    requested_roots = _deduplicate_evidence_roots(
        getattr(arguments, "evidence_root", []),
        require_nonempty=False,
    )
    for requested_root in requested_roots:
        if not any(
            requested_root == root or requested_root.is_relative_to(root)
            for root in roots
        ):
            raise ConfigurationError(
                "seal 的 --evidence-root 不能扩展 run 已绑定的证据边界。"
            )
    environment = attempt.get("environment")
    if not isinstance(environment, dict):
        raise ConfigurationError("抓包 attempt 缺少自动环境探针绑定。")
    attempt_evidence_root = Path(str(environment.get("evidence_root", "")))
    if (
        not attempt_evidence_root.is_absolute()
        or attempt_evidence_root.resolve(strict=True) not in roots
    ):
        raise ConfigurationError("抓包 attempt 的环境证据根未纳入 seal。")
    restoration_reference = environment.get("restoration_report")
    if (
        not isinstance(restoration_reference, dict)
        or set(restoration_reference) != {"path", "sha256", "bytes"}
    ):
        raise ConfigurationError("抓包 attempt 缺少机器恢复收据绑定。")
    restoration_path = attempt_evidence_root / str(
        restoration_reference.get("path", "")
    )
    if (
        restoration_path.is_symlink()
        or not restoration_path.is_file()
        or restoration_path.stat().st_size != restoration_reference.get("bytes")
        or file_sha256(restoration_path) != restoration_reference.get("sha256")
    ):
        raise ConfigurationError("抓包 attempt 的机器恢复收据发生漂移。")
    requested_restoration = getattr(arguments, "restoration_report", None)
    if (
        requested_restoration is not None
        and requested_restoration.resolve(strict=True)
        != restoration_path.resolve(strict=True)
    ):
        raise ConfigurationError(
            "--restoration-report 只能指向 run 自动生成的恢复收据。"
        )
    post_client_path: Path | None = None
    client_checkpoint_at: str | None = None
    client_checkpoint_created = False
    if phase == "candidate":
        try:
            with _campaign_lock(campaign_dir):
                _reject_contaminated_campaign(campaign_dir)
                (
                    post_client_path,
                    _,
                    client_checkpoint_at,
                    client_checkpoint_created,
                ) = _candidate_post_client_restoration(
                    manifest,
                    attempt_evidence_root,
                    str(candidate_id),
                )
            if _rfc3339_datetime(
                client_checkpoint_at, "Kilo 后检查点时间"
            ) < _rfc3339_datetime(
                attempt["completed_at_utc"], "attempt.completed_at_utc"
            ):
                raise ConfigurationError("Kilo 后检查点早于候选 run 完成时间。")
        except (
            ConfigurationError,
            EnvironmentProbeError,
            ReceiptFinalizerError,
            OSError,
            ValueError,
        ) as error:
            _record_candidate_seal_failure(
                campaign_dir,
                attempt_root,
                attempt,
                error,
            )
            raise RuntimeError("Kilo 后环境恢复门禁失败。") from error
        if client_checkpoint_created:
            return {
                "status": "client_checkpoint_created",
                "phase": "candidate",
                "campaign_id": attempt["campaign_id"],
                "candidate_id": candidate_id,
                "attempt_id": attempt["attempt_id"],
                "run_nonce": attempt["run_nonce"],
                "attempt_started_at_utc": attempt["started_at_utc"],
                "client_checkpoint_at_utc": client_checkpoint_at,
                "evidence_root": str(attempt_evidence_root),
                "next_command": (
                    "使用上述不可变边界生成 observed-profile 与两份 Kilo 收据；"
                    "随后重新执行 capture-candidate seal。"
                ),
            }
    restoration = _validate_restoration_report(
        restoration_path,
        roots,
        phase=phase,
        candidate_id=candidate_id,
    )
    assertion_context = _capture_assertion_context(
        getattr(arguments, "capture_manifest", None),
        getattr(arguments, "assertion_evidence_root", None),
        roots,
        target_version=manifest["target_version"],
    )
    assertion_gate = _run_seal_assertion_gate(
        assertion_context,
        roots,
        phase=phase,
        target_version=manifest["target_version"],
    )
    client_bindings: list[dict[str, Any]] = []
    observed_profile: dict[str, str] | None = None
    if phase == "candidate":
        if post_client_path is None or client_checkpoint_at is None:
            raise ConfigurationError("候选 seal 缺少 Kilo 后检查点。")
        observed_profile, observed_receipt = _validate_observed_profile_receipt(
            getattr(arguments, "observed_profile_receipt", None),
            [attempt_evidence_root],
            campaign_id=attempt["campaign_id"],
            attempt_id=attempt["attempt_id"],
            run_nonce=attempt["run_nonce"],
            attempt_started_at_utc=attempt["started_at_utc"],
            client_checkpoint_at_utc=client_checkpoint_at,
            candidate_id=str(candidate_id),
            target_version=manifest["target_version"],
            expected_profile_id=str(identity["profile_id"]),
            expected_profile_digest=str(identity["profile_digest"]),
            image_id=str(identity["image_id"]),
            image_reference=str(identity["image_reference"]),
            source_tree_sha256=str(identity["source_tree_sha256"]),
            build_id=str(identity["build_id"]),
            deployed_version=str(identity["deployed_version"]),
        )
        if (
            observed_receipt.get("profile_id") != identity["profile_id"]
            or observed_receipt.get("profile_digest") != identity["profile_digest"]
        ):
            raise ConfigurationError("运行画像收据与 attempt 身份不一致。")
        client_bindings = _parse_client_evidence(
            arguments.client_evidence,
            [attempt_evidence_root],
            campaign_id=attempt["campaign_id"],
            attempt_id=attempt["attempt_id"],
            run_nonce=attempt["run_nonce"],
            attempt_started_at_utc=attempt["started_at_utc"],
            client_checkpoint_at_utc=client_checkpoint_at,
            candidate_id=str(candidate_id),
            target_version=manifest["target_version"],
            model=manifest["configuration"]["model"],
            identity=identity,
        )
        required_clients = _required_client_bindings(campaign_dir, classification)
        observed_clients = {item["client_id"] for item in client_bindings}
        if not required_clients.issubset(observed_clients):
            raise ConfigurationError(
                "候选 seal 缺少目标场景要求的第三方客户端收据："
                f"{sorted(required_clients - observed_clients)}"
            )
        restoration["post_client"] = _validate_restoration_report(
            post_client_path,
            roots,
            phase="candidate",
            candidate_id=str(candidate_id),
        )

    surface = scan_evidence(
        [Path(value) for value in attempt["evidence_roots"]],
        "target-official" if phase == "official" else "target-sub2api",
    )
    if surface["file_count"] == 0:
        raise ConfigurationError(f"{phase} attempt 没有可封存抓包证据。")
    derived_root = ensure_private_directory(attempt_root / "finalized", campaign_dir)
    surface_path = derived_root / "surface.json"
    _write_or_verify_json(surface_path, surface)

    evidence_inventory = _evidence_inventory(roots)
    security = _evidence_security(roots)
    raw_evidence_private = _evidence_permissions_private(roots)
    if not security["known_secret_scan_passed"]:
        raise ConfigurationError(
            f"{phase} 证据秘密扫描失败：{len(security['findings'])} 个命中。"
        )
    if not raw_evidence_private:
        raise ConfigurationError(f"{phase} 原始证据权限不是目录 0700／文件 0600。")
    surface_binding = {
        "path": str(surface_path.relative_to(campaign_dir)),
        "sha256": file_sha256(surface_path),
    }
    payload: dict[str, Any] = {
        "status": "complete",
        "identity": {
            key: value for key, value in identity.items() if key != "source_root"
        },
        "attempt": {
            "path": str((attempt_root / "attempt.json").relative_to(campaign_dir)),
            "sha256": file_sha256(attempt_root / "attempt.json"),
        },
        "results": attempt["results"],
        "evidence_roots": [str(root) for root in roots],
        "evidence_inventory": evidence_inventory,
        "surface": surface_binding,
        "client_bindings": client_bindings,
        "assertion_context": assertion_context,
        "assertion_gate": assertion_gate,
        "restoration": restoration,
        "security": {"raw_evidence_private": raw_evidence_private, **security},
    }
    if observed_profile is not None:
        payload["observed_profile"] = observed_profile
    if phase == "official":
        binary_verification = attempt.get("binary_verification")
        if not isinstance(binary_verification, dict):
            raise ConfigurationError("官方 attempt 缺少二进制身份验证。")
        payload["binary_verification"] = binary_verification

    preview, approved = _seal_preview(
        campaign_dir,
        attempt_root,
        phase=phase,
        candidate_id=candidate_id,
        attempt=attempt,
        stage_payload=payload,
        approve_sha256=getattr(arguments, "approve_seal_sha256", None),
    )
    if not approved:
        return {
            "status": "approval_required",
            "phase": phase,
            "candidate_id": candidate_id,
            "attempt_id": attempt_id,
            "seal_preview": str(attempt_root / "seal-preview.json"),
            "review_sha256": preview["review_sha256"],
            "message": "复核机器 finalizer 事实后，以同一摘要再次执行 seal。",
        }
    payload["seal_preview"] = {
        "path": str((attempt_root / "seal-preview.json").relative_to(campaign_dir)),
        "sha256": file_sha256(attempt_root / "seal-preview.json"),
    }
    save_stage_result(
        campaign_dir,
        "capture-official" if phase == "official" else "capture-candidate",
        payload,
        candidate_id=candidate_id,
    )
    return {
        **payload,
        "status": "complete",
        "review_sha256": preview["review_sha256"],
    }


def _surface_from_stage(
    campaign_dir: Path,
    stage: str,
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    result = _load_stage_result(campaign_dir, stage, candidate_id)
    reference = result.get("surface")
    if isinstance(reference, dict):
        path = _campaign_file(campaign_dir, str(reference.get("path", "")))
        if not path.is_file() or file_sha256(path) != reference.get("sha256"):
            raise ConfigurationError(f"{stage} 的表面证据摘要不一致。")
        return _read_json(path, f"{stage} 规范化表面")
    roots = [Path(value) for value in result.get("evidence_roots", [])]
    if not roots:
        raise ConfigurationError(f"{stage} 阶段缺少证据根目录。")
    label = "target-official" if stage == "capture-official" else "target-sub2api"
    surface = scan_evidence(roots, label)
    relative_root = Path("official") if stage == "capture-official" else Path("candidates") / str(candidate_id)
    output = _campaign_file(campaign_dir, str(relative_root / "surface-derived.json"))
    if output.exists():
        existing = _read_json(output, f"{stage} 派生表面")
        if _fingerprint(existing) != _fingerprint(surface):
            raise ConfigurationError(f"{stage} 派生表面与封存证据不一致。")
        return existing
    ensure_private_directory(output.parent, campaign_dir)
    secure_write_json(output, surface)
    return surface


def _analysis_payload(
    campaign_dir: Path,
    manifest: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    reference = manifest["analysis"][name]
    path = _campaign_file(campaign_dir, reference["path"])
    if file_sha256(path) != reference["sha256"]:
        raise ConfigurationError(f"计划期分析摘要漂移：{name}")
    return _read_json(path, f"计划期分析 {name}")


def _validate_migration_manifest(
    migration: dict[str, Any],
    *,
    baseline_version: str,
    target_version: str,
    baseline_rules: tuple[str, ...],
    target_rules: tuple[str, ...],
    source_diff: dict[str, Any],
    official_diff: dict[str, Any],
) -> dict[str, Any]:
    if migration.get("schema_version") != MIGRATION_SCHEMA:
        raise ConfigurationError("规则迁移清单 schema_version 不受支持。")
    if migration.get("baseline_version") != baseline_version:
        raise ConfigurationError("规则迁移清单 baseline_version 不一致。")
    if migration.get("target_version") != target_version:
        raise ConfigurationError("规则迁移清单 target_version 不一致。")
    entries = migration.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("规则迁移清单 entries 不能为空。")
    seen_baseline: list[str] = []
    seen_target: list[str] = []
    blocked = False
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ConfigurationError(f"迁移项 {index} 必须是对象。")
        classification = entry.get("classification")
        baseline_rule = entry.get("baseline_rule")
        target_rule = entry.get("target_rule")
        if classification not in MIGRATION_CLASSIFICATIONS:
            raise ConfigurationError(f"迁移项 {index} classification 非法。")
        if baseline_rule is not None and (
            not isinstance(baseline_rule, str) or not RULE_RE.fullmatch(baseline_rule)
        ):
            raise ConfigurationError(f"迁移项 {index} baseline_rule 非法。")
        if target_rule is not None and (
            not isinstance(target_rule, str) or not RULE_RE.fullmatch(target_rule)
        ):
            raise ConfigurationError(f"迁移项 {index} target_rule 非法。")
        if classification == "add" and (baseline_rule is not None or target_rule is None):
            raise ConfigurationError("add 迁移必须只设置 target_rule。")
        if classification == "delete" and (baseline_rule is None or target_rule is not None):
            raise ConfigurationError("delete 迁移必须只设置 baseline_rule。")
        if classification in {"inherit", "change", "condition_change"} and (
            baseline_rule is None or target_rule is None
        ):
            raise ConfigurationError(f"{classification} 迁移必须同时绑定新旧规则。")
        if classification == "inherit" and baseline_rule != target_rule:
            raise ConfigurationError("inherit 迁移必须保持规则编号一致。")
        if classification == "blocked" and baseline_rule is None and target_rule is None:
            raise ConfigurationError("blocked 迁移至少要绑定一侧规则。")
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ConfigurationError(f"迁移项 {index} 缺少 rationale。")
        evidence_refs = entry.get("evidence_refs", [])
        if not isinstance(evidence_refs, list) or not all(
            isinstance(value, str) and value for value in evidence_refs
        ):
            raise ConfigurationError(f"迁移项 {index} evidence_refs 非法。")
        if classification != "blocked" and not evidence_refs:
            raise ConfigurationError(f"迁移项 {index} 缺少证据引用。")
        if baseline_rule is not None:
            seen_baseline.append(baseline_rule)
        if target_rule is not None:
            seen_target.append(target_rule)
        blocked = blocked or classification == "blocked"
    if sorted(seen_baseline) != sorted(baseline_rules) or len(seen_baseline) != len(
        set(seen_baseline)
    ):
        raise ConfigurationError("规则迁移未使基线规则唯一闭环。")
    if sorted(seen_target) != sorted(target_rules) or len(seen_target) != len(
        set(seen_target)
    ):
        raise ConfigurationError("规则迁移未使目标规则唯一闭环。")

    expected_discoveries = {
        (source, change, item["fingerprint"])
        for source, change, values in (
            ("source", "added", source_diff.get("added", [])),
            ("source", "removed", source_diff.get("removed", [])),
            ("dynamic", "added", official_diff.get("added", [])),
            ("dynamic", "removed", official_diff.get("removed", [])),
        )
        for item in values
    }
    discoveries = migration.get("discovery_classifications", [])
    if not isinstance(discoveries, list):
        raise ConfigurationError("discovery_classifications 必须是数组。")
    seen_discoveries: list[tuple[str, str, str]] = []
    for index, item in enumerate(discoveries, 1):
        if not isinstance(item, dict):
            raise ConfigurationError(f"发现分类 {index} 必须是对象。")
        source = item.get("source")
        change = item.get("change")
        fingerprint = item.get("fingerprint")
        classification = item.get("classification")
        if (
            source not in {"source", "dynamic"}
            or change not in {"added", "removed"}
            or not SHA256_RE.fullmatch(str(fingerprint))
        ):
            raise ConfigurationError(f"发现分类 {index} 身份非法。")
        if classification not in MIGRATION_CLASSIFICATIONS:
            raise ConfigurationError(f"发现分类 {index} classification 非法。")
        target_rule = item.get("target_rule")
        if target_rule is not None and target_rule not in target_rules:
            raise ConfigurationError(f"发现分类 {index} 引用目标规则清单外编号。")
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ConfigurationError(f"发现分类 {index} 缺少 rationale。")
        evidence_refs = item.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs or not all(
            isinstance(value, str) and value for value in evidence_refs
        ):
            raise ConfigurationError(f"发现分类 {index} 缺少证据引用。")
        allowed = (
            {"add", "change", "condition_change", "blocked"}
            if change == "added"
            else {"delete", "change", "condition_change", "blocked"}
        )
        if classification not in allowed:
            raise ConfigurationError(
                f"发现分类 {index} 的 {change}/{classification} 组合非法。"
            )
        if classification in {"add", "change", "condition_change"} and target_rule is None:
            raise ConfigurationError(f"发现分类 {index} 必须绑定 target_rule。")
        if classification == "delete" and target_rule is not None:
            raise ConfigurationError(f"发现分类 {index} delete 不能绑定 target_rule。")
        seen_discoveries.append((source, str(change), str(fingerprint)))
        blocked = blocked or classification == "blocked"
    if set(seen_discoveries) != expected_discoveries or len(seen_discoveries) != len(
        set(seen_discoveries)
    ):
        missing = sorted(expected_discoveries - set(seen_discoveries))
        extra = sorted(set(seen_discoveries) - expected_discoveries)
        raise ConfigurationError(
            f"源码／动态增删形态未唯一分类；缺失={missing}，多余={extra}。"
        )
    return {
        "blocked": blocked,
        "entry_count": len(entries),
        "discovery_count": len(discoveries),
        "unclassified_count": 0,
    }


def _classification_differences(
    campaign_dir: Path,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_diff = _analysis_payload(campaign_dir, manifest, "source-diff")
    baseline_surface = _analysis_payload(campaign_dir, manifest, "baseline-surface")
    official_surface = _surface_from_stage(campaign_dir, "capture-official")
    official_diff = compare_surfaces(baseline_surface, official_surface)
    discovery_root = ensure_private_directory(
        campaign_dir / "classification" / "discovery", campaign_dir
    )
    output = discovery_root / "baseline-to-target-official.json"
    if output.exists():
        existing = _read_json(output, "官方动态差异")
        if _fingerprint(existing) != _fingerprint(official_diff):
            raise ConfigurationError("官方动态差异与已封存证据不一致。")
    else:
        secure_write_json(output, official_diff)
    return source_diff, official_diff


def _json_pointer(path: tuple[str, ...]) -> str:
    """把 JSON 遍历路径编码为稳定、可审核的 JSON Pointer。"""

    if not path:
        return ""
    return "/" + "/".join(
        part.replace("~", "~0").replace("/", "~1") for part in path
    )


def _replace_json_string_literal(
    value: Any,
    old: str,
    new: str,
    path: tuple[str, ...] = (),
) -> tuple[Any, list[str]]:
    """只替换 JSON 字符串中的版本字面，并返回全部受影响坐标。"""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        changed: list[str] = []
        for key, child in value.items():
            replaced, child_paths = _replace_json_string_literal(
                child,
                old,
                new,
                (*path, str(key)),
            )
            result[key] = replaced
            changed.extend(child_paths)
        return result, changed
    if isinstance(value, list):
        result_list: list[Any] = []
        changed = []
        for index, child in enumerate(value):
            replaced, child_paths = _replace_json_string_literal(
                child,
                old,
                new,
                (*path, str(index)),
            )
            result_list.append(replaced)
            changed.extend(child_paths)
        return result_list, changed
    if isinstance(value, str) and old in value:
        return value.replace(old, new), [_json_pointer(path)]
    return value, []


def _json_string_paths_containing(
    value: Any,
    literal: str,
    path: tuple[str, ...] = (),
) -> list[str]:
    """列出仍含指定版本字面的所有 JSON 字符串坐标。"""

    if isinstance(value, dict):
        return [
            pointer
            for key, child in value.items()
            for pointer in _json_string_paths_containing(
                child,
                literal,
                (*path, str(key)),
            )
        ]
    if isinstance(value, list):
        return [
            pointer
            for index, child in enumerate(value)
            for pointer in _json_string_paths_containing(
                child,
                literal,
                (*path, str(index)),
            )
        ]
    if isinstance(value, str) and literal in value:
        return [_json_pointer(path)]
    return []


def _assertion_profile_version_coordinates(
    value: Any,
    path: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    """提取断言画像中具有 Codex 版本语义的字段、header/query 与 UA 坐标。"""

    coordinates: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = (*path, str(key))
            if (
                key in {"codex_version", "client_version", "version"}
                and isinstance(child, str)
                and VERSION_RE.fullmatch(child)
            ):
                coordinates.append((_json_pointer(child_path), child))
            coordinates.extend(
                _assertion_profile_version_coordinates(child, child_path)
            )
        return coordinates
    if isinstance(value, list):
        if (
            len(value) == 2
            and isinstance(value[0], str)
            and value[0].lower() in {"client_version", "version"}
            and isinstance(value[1], str)
            and VERSION_RE.fullmatch(value[1])
        ):
            coordinates.append((_json_pointer((*path, "1")), value[1]))
        for index, child in enumerate(value):
            coordinates.extend(
                _assertion_profile_version_coordinates(
                    child,
                    (*path, str(index)),
                )
            )
        return coordinates
    if isinstance(value, str):
        matches = CODEX_USER_AGENT_VERSION_RE.finditer(value)
        for index, match in enumerate(matches, 1):
            version = next(group for group in match.groups() if group is not None)
            coordinates.append((f"{_json_pointer(path)}#ua-{index}", version))
    return coordinates


def _apply_assertion_profile_overrides(
    profile: dict[str, Any],
    *,
    target_version: str,
    base_profile_path: Path,
) -> tuple[dict[str, Any], int]:
    """把版本专属、人工审核过的期望变更确定性应用到 classify 草案。

    selector 修正属于基线画像自身的缺陷，直接修在基线画像；真正的版本行为变化
    则必须留在目标版本 override 中。这样不会为了让 0.147 通过而反向篡改 0.145
    的冻结期望，同时每个变更都要求 before 精确命中，画像漂移时会失败关闭。
    """

    path = Path(__file__).with_name(
        f"candidate_rule_expectation_overrides_{target_version.replace('.', '_')}.json"
    )
    if not path.exists():
        return profile, 0
    payload = _read_json(path, "目标版本断言期望覆盖清单")
    required = {
        "schema_version",
        "base_codex_version",
        "target_codex_version",
        "base_profile_sha256",
        "operations",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ConfigurationError("目标版本断言期望覆盖清单字段不闭合。")
    if payload["schema_version"] != "codex-candidate-rule-expectation-overrides/v1":
        raise ConfigurationError("目标版本断言期望覆盖清单 schema_version 不受支持。")
    if payload["base_codex_version"] != profile.get("codex_version"):
        raise ConfigurationError("目标版本断言期望覆盖清单基线版本不一致。")
    if payload["target_codex_version"] != target_version:
        raise ConfigurationError("目标版本断言期望覆盖清单目标版本不一致。")
    if payload["base_profile_sha256"] != file_sha256(base_profile_path):
        raise ConfigurationError("目标版本断言期望覆盖清单绑定的基线画像摘要不一致。")
    operations = payload["operations"]
    if not isinstance(operations, list) or not operations:
        raise ConfigurationError("目标版本断言期望覆盖清单 operations 不能为空。")

    updated = json.loads(json.dumps(profile, ensure_ascii=False))
    seen: set[tuple[str, str]] = set()
    for index, operation in enumerate(operations, 1):
        expected_keys = {"rule_id", "check_id", "before", "after", "rationale"}
        if not isinstance(operation, dict) or set(operation) != expected_keys:
            raise ConfigurationError(f"断言期望覆盖操作 {index} 字段不闭合。")
        identity = (operation["rule_id"], operation["check_id"])
        if (
            not all(isinstance(item, str) and item for item in identity)
            or identity in seen
            or not isinstance(operation["rationale"], str)
            or not operation["rationale"].strip()
        ):
            raise ConfigurationError(f"断言期望覆盖操作 {index} 身份非法或重复。")
        seen.add(identity)
        matches = [
            check
            for rule in updated.get("rules", [])
            if rule.get("rule_id") == identity[0]
            for check in rule.get("checks", [])
            if check.get("id") == identity[1]
        ]
        if len(matches) != 1:
            raise ConfigurationError(f"断言期望覆盖操作 {index} 未唯一命中 check。")
        assertion = matches[0].get("assertion")
        if not isinstance(assertion, dict) or assertion.get("value") != operation["before"]:
            raise ConfigurationError(f"断言期望覆盖操作 {index} 的 before 不匹配。")
        assertion["value"] = operation["after"]
    return updated, len(operations)


def _write_classification_draft(
    campaign_dir: Path,
    manifest: dict[str, Any],
    source_diff: dict[str, Any],
    official_diff: dict[str, Any],
) -> dict[str, Any]:
    revision = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
    draft_root = ensure_private_directory(
        campaign_dir / "classification" / "draft" / revision, campaign_dir
    )
    rules = tuple(manifest["required_rules"])
    target_rules = {
        "schema_version": RULE_SCHEMA,
        "codex_version": manifest["target_version"],
        "required_rules": list(rules),
    }
    discovery_scenarios = _read_json(
        _campaign_file(
            campaign_dir,
            manifest["inputs"]["discovery_scenarios"]["path"],
        ),
        "基线发现场景清单",
    )
    scenario = json.loads(json.dumps(discovery_scenarios, ensure_ascii=False))
    draft_profile_id = f"codex-{manifest['target_version']}-draft"
    scenario["codex_version"] = manifest["target_version"]
    scenario["profile_id"] = draft_profile_id
    scenario["rule_manifest"] = {
        "path": "target-rules.json",
        "sha256": _normalized_json_sha256(target_rules),
        "rule_count": len(rules),
    }
    discoveries = [
        {
            "source": source,
            "change": change,
            "fingerprint": item["fingerprint"],
            "classification": "blocked",
            "target_rule": None,
            "evidence_refs": [
                "source-diff.json"
                if source == "source"
                else "baseline-to-target-official.json"
            ],
            "rationale": "待人工分类。",
        }
        for source, change, values in (
            ("source", "added", source_diff.get("added", [])),
            ("source", "removed", source_diff.get("removed", [])),
            ("dynamic", "added", official_diff.get("added", [])),
            ("dynamic", "removed", official_diff.get("removed", [])),
        )
        for item in values
    ]
    migration = {
        "schema_version": MIGRATION_SCHEMA,
        "baseline_version": manifest["baseline_version"],
        "target_version": manifest["target_version"],
        "status": "draft",
        "entries": [
            {
                "baseline_rule": rule,
                "target_rule": rule,
                "classification": "inherit",
                "rationale": "草案占位；必须人工复核。",
                "evidence_refs": ["source-diff.json", "baseline-to-target-official.json"],
            }
            for rule in rules
        ],
        "discovery_classifications": discoveries,
    }
    profile = {
        "schema_version": PROFILE_SCHEMA,
        "codex_version": manifest["target_version"],
        "profile_id": draft_profile_id,
        "profile_payload": {"status": "待实现并审核"},
        "profile_payload_sha256": _fingerprint({"status": "待实现并审核"}),
        "profile_digest": "0" * 64,
        "status": "draft",
    }
    baseline_profile_path = Path(__file__).with_name(
        "candidate_rule_expectations_"
        f"{manifest['baseline_version'].replace('.', '_')}.json"
    )
    assertion_profile = _read_json(baseline_profile_path, "基线断言画像")
    assertion_profile, assertion_override_count = _apply_assertion_profile_overrides(
        assertion_profile,
        target_version=manifest["target_version"],
        base_profile_path=baseline_profile_path,
    )
    assertion_profile = json.loads(
        json.dumps(assertion_profile, ensure_ascii=False)
    )
    assertion_profile["codex_version"] = manifest["target_version"]
    assertion_profile, assertion_version_paths = _replace_json_string_literal(
        assertion_profile,
        manifest["baseline_version"],
        manifest["target_version"],
    )
    secure_write_json(draft_root / "target-rules.json", target_rules)
    secure_write_json(draft_root / "rule-migration.json", migration)
    secure_write_json(draft_root / "scenarios.json", scenario)
    secure_write_json(draft_root / "profile.json", profile)
    secure_write_json(draft_root / "assertion-profile.json", assertion_profile)
    receipt = {
        "status": "draft",
        "revision": revision,
        "path": str(draft_root),
        "source_added": source_diff.get("added_count", 0),
        "dynamic_added": official_diff.get("added_count", 0),
        "blocked_discoveries": len(discoveries),
        "assertion_version_replacements": {
            "baseline_version": manifest["baseline_version"],
            "target_version": manifest["target_version"],
            "count": len(assertion_version_paths),
            "paths": assertion_version_paths,
        },
        "assertion_override_count": assertion_override_count,
    }
    secure_write_json(draft_root / "draft.json", receipt)
    return receipt


def _validate_assertion_profile_manifest(
    payload: dict[str, Any],
    *,
    baseline_version: str,
    target_version: str,
    target_rules: tuple[str, ...],
) -> None:
    """验证目标版本断言画像的规则、场景和规格摘要闭环。"""

    if payload.get("schema_version") != ASSERTION_PROFILE_SCHEMA:
        raise ConfigurationError("目标断言画像 schema_version 不受支持。")
    if payload.get("codex_version") != target_version:
        raise ConfigurationError("目标断言画像 codex_version 不一致。")
    stale_paths = _json_string_paths_containing(payload, baseline_version)
    if baseline_version != target_version and stale_paths:
        raise ConfigurationError(
            "目标断言画像仍残留 baseline 版本坐标："
            + ", ".join(stale_paths[:8])
        )
    version_coordinates = _assertion_profile_version_coordinates(payload)
    behavior_coordinates = [
        (path, version)
        for path, version in version_coordinates
        if path != "/codex_version"
    ]
    if not behavior_coordinates:
        raise ConfigurationError("目标断言画像缺少可审核的行为版本坐标。")
    mismatches = [
        (path, version)
        for path, version in behavior_coordinates
        if version != target_version
    ]
    if mismatches:
        raise ConfigurationError(
            "目标断言画像行为版本坐标与 target_version 不一致："
            + ", ".join(f"{path}={version}" for path, version in mismatches[:8])
        )
    source_spec = payload.get("source_spec")
    source_sha = payload.get("source_spec_sha256")
    if not isinstance(source_spec, str) or not SHA256_RE.fullmatch(str(source_sha)):
        raise ConfigurationError("目标断言画像规格摘要绑定非法。")
    source_path_text, separator, fragment = source_spec.partition("#")
    source_path = Path(source_path_text)
    if (
        not separator
        or source_path.is_absolute()
        or ".." in source_path.parts
        or not fragment
    ):
        raise ConfigurationError("目标断言画像 source_spec 非法。")
    resolved_source = Path(__file__).resolve().parents[2] / source_path
    if (
        not resolved_source.is_file()
        or resolved_source.is_symlink()
        or source_spec_section_sha256(resolved_source, fragment) != source_sha
    ):
        raise ConfigurationError("目标断言画像规格第二章摘要不一致。")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ConfigurationError("目标断言画像 scenarios 不能为空。")
    scenario_ids = [
        item.get("scenario_id") for item in scenarios if isinstance(item, dict)
    ]
    if (
        len(scenario_ids) != len(scenarios)
        or len(scenario_ids) != len(set(scenario_ids))
        or any(not isinstance(value, str) or not value for value in scenario_ids)
    ):
        raise ConfigurationError("目标断言画像 scenario_id 非法或重复。")
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        raise ConfigurationError("目标断言画像 rules 必须是数组。")
    rule_ids: list[str] = []
    for index, rule in enumerate(raw_rules, 1):
        if not isinstance(rule, dict):
            raise ConfigurationError(f"目标断言画像规则 {index} 必须是对象。")
        rule_id = rule.get("rule_id")
        selected_scenarios = rule.get("scenario_ids")
        checks = rule.get("checks")
        if (
            not isinstance(rule_id, str)
            or not RULE_RE.fullmatch(rule_id)
            or not isinstance(selected_scenarios, list)
            or not selected_scenarios
            or not set(selected_scenarios).issubset(set(scenario_ids))
            or not isinstance(checks, list)
            or not checks
        ):
            raise ConfigurationError(f"目标断言画像规则 {index} 结构非法。")
        rule_ids.append(rule_id)
    if tuple(rule_ids) != target_rules or len(rule_ids) != len(set(rule_ids)):
        raise ConfigurationError("目标断言画像未按顺序精确覆盖目标规则全集。")


def _approved_manifest_payload(source: Path, label: str) -> dict[str, Any]:
    if not source.is_file() or source.is_symlink():
        raise ConfigurationError(f"{label}不存在或不可信：{source}")
    return _read_json(source, label)


def _normalized_json_sha256(payload: dict[str, Any]) -> str:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _approved_reference(
    campaign_dir: Path,
    destination: Path,
    payload: dict[str, Any],
) -> dict[str, str]:
    return {
        "path": destination.relative_to(campaign_dir).as_posix(),
        "sha256": _normalized_json_sha256(payload),
    }


def classify_campaign(
    campaign_dir: Path,
    *,
    target_rule_manifest: Path | None = None,
    migration_manifest: Path | None = None,
    scenario_manifest: Path | None = None,
    profile_manifest: Path | None = None,
    assertion_profile_manifest: Path | None = None,
    approve_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    _reject_contaminated_campaign(campaign_dir)
    manifest = load_campaign_manifest(campaign_dir)
    _verify_plan_identity(campaign_dir, manifest)
    official = _load_stage_result(campaign_dir, "capture-official")
    if official.get("status") != "complete":
        raise ConfigurationError("官方证据尚未完整封存。")
    source_diff, official_diff = _classification_differences(campaign_dir, manifest)
    if target_rule_manifest is None and migration_manifest is None:
        return _write_classification_draft(
            campaign_dir, manifest, source_diff, official_diff
        )
    if (
        target_rule_manifest is None
        or migration_manifest is None
        or scenario_manifest is None
        or profile_manifest is None
        or assertion_profile_manifest is None
    ):
        raise ConfigurationError(
            "批准分类必须同时提供目标规则、迁移、场景、运行画像和断言画像清单。"
        )
    baseline_rules = tuple(manifest["required_rules"])
    target_rules = load_rule_manifest(target_rule_manifest, manifest["target_version"])
    target_payload = _approved_manifest_payload(target_rule_manifest, "目标规则清单")
    migration = _approved_manifest_payload(migration_manifest, "规则迁移清单")
    if migration.get("status") != "approved":
        raise ConfigurationError("规则迁移清单 status 必须是 approved。")
    validation = _validate_migration_manifest(
        migration,
        baseline_version=manifest["baseline_version"],
        target_version=manifest["target_version"],
        baseline_rules=baseline_rules,
        target_rules=target_rules,
        source_diff=source_diff,
        official_diff=official_diff,
    )
    scenario_payload = _approved_manifest_payload(
        scenario_manifest, "目标场景清单"
    )
    profile_payload = _approved_manifest_payload(profile_manifest, "目标画像清单")
    if profile_payload.get("schema_version") != PROFILE_SCHEMA:
        raise ConfigurationError("目标画像清单 schema_version 不受支持。")
    if profile_payload.get("status") != "approved":
        raise ConfigurationError("目标画像清单 status 必须是 approved。")
    if profile_payload.get("codex_version") != manifest["target_version"]:
        raise ConfigurationError("目标画像清单 codex_version 不一致。")
    if not SAFE_ID_RE.fullmatch(str(profile_payload.get("profile_id", ""))):
        raise ConfigurationError("目标画像清单 profile_id 非法。")
    if not SHA256_RE.fullmatch(str(profile_payload.get("profile_digest", ""))):
        raise ConfigurationError("目标画像清单 profile_digest 非法。")
    profile_snapshot = profile_payload.get("profile_payload")
    if not isinstance(profile_snapshot, dict) or not profile_snapshot:
        raise ConfigurationError("目标画像清单 profile_payload 不能为空。")
    if profile_payload.get("profile_payload_sha256") != _fingerprint(profile_snapshot):
        raise ConfigurationError("目标画像 profile_payload 摘要不一致。")
    if scenario_payload.get("profile_id") != profile_payload.get("profile_id"):
        raise ConfigurationError("目标场景清单 profile_id 与运行画像不一致。")
    assertion_profile_payload = _approved_manifest_payload(
        assertion_profile_manifest, "目标断言画像清单"
    )
    _validate_assertion_profile_manifest(
        assertion_profile_payload,
        baseline_version=manifest["baseline_version"],
        target_version=manifest["target_version"],
        target_rules=target_rules,
    )
    scenario_arguments = _campaign_arguments(campaign_dir, manifest)
    scenario_arguments.scenario_manifest = scenario_manifest
    scenario_context = _job_context(scenario_arguments)
    scenario_jobs = load_scenario_jobs(
        scenario_manifest,
        scenario_context,
        expected_version=manifest["target_version"],
        expected_rule_sha256=file_sha256(target_rule_manifest),
        require_bindings=True,
    )
    scenario_jobs = [
        job for job in scenario_jobs if manifest["suite"] in job.suites
    ]
    _validate_jobs(scenario_jobs, target_rules)
    if manifest["suite"] == "full":
        _validate_phase_coverage(scenario_jobs, target_rules)
    _validate_capture_job_results(
        [job for job in scenario_jobs if job.phase == "official"],
        official.get("results"),
        phase="official",
    )
    approved_root = campaign_dir / "classification" / "approved"
    target_destination = approved_root / "target-rules.json"
    migration_destination = approved_root / "rule-migration.json"
    scenario_destination = approved_root / "scenarios.json"
    profile_destination = approved_root / "profile.json"
    assertion_profile_destination = approved_root / "assertion-profile.json"
    target_reference = _approved_reference(
        campaign_dir, target_destination, target_payload
    )
    migration_reference = _approved_reference(
        campaign_dir, migration_destination, migration
    )
    scenario_reference = _approved_reference(
        campaign_dir, scenario_destination, scenario_payload
    )
    profile_reference = _approved_reference(
        campaign_dir, profile_destination, profile_payload
    )
    assertion_profile_reference = _approved_reference(
        campaign_dir,
        assertion_profile_destination,
        assertion_profile_payload,
    )
    references = {
        "target_rule_manifest": target_reference,
        "migration_manifest": migration_reference,
        "scenario_manifest": scenario_reference,
        "profile_manifest": profile_reference,
        "assertion_profile_manifest": assertion_profile_reference,
    }
    joint_digest = _fingerprint(
        {
            key: value["sha256"] if value else None
            for key, value in references.items()
        }
    )
    if approve_manifest_sha256 is None:
        return {
            "status": "approval_required",
            "joint_manifest_sha256": joint_digest,
            "message": "复核五份目标版本清单后，以该联合摘要再次执行 classify。",
        }
    if not SHA256_RE.fullmatch(approve_manifest_sha256):
        raise ConfigurationError("--approve-manifest-sha256 格式非法。")
    if approve_manifest_sha256 != joint_digest:
        raise ConfigurationError("批准联合摘要与清单内容不一致。")
    if approved_root.exists():
        raise ConfigurationError("分类批准目录已经存在，禁止覆盖。")
    ensure_private_directory(approved_root.parent, campaign_dir)
    try:
        approved_root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ConfigurationError("分类批准目录已经存在，禁止覆盖。") from error
    secure_write_json(target_destination, target_payload)
    secure_write_json(migration_destination, migration)
    secure_write_json(scenario_destination, scenario_payload)
    secure_write_json(profile_destination, profile_payload)
    secure_write_json(assertion_profile_destination, assertion_profile_payload)
    payload = {
        "status": "blocked" if validation["blocked"] else "complete",
        **references,
        "joint_manifest_sha256": joint_digest,
        "baseline_rule_count": len(baseline_rules),
        "target_rule_count": len(target_rules),
        "migration": validation,
        "source_diff_sha256": _fingerprint(source_diff),
        "official_diff_sha256": _fingerprint(official_diff),
    }
    save_stage_result(campaign_dir, "classify", payload)
    return payload


def _profile_binding_from_manifest(
    campaign_dir: Path,
    classification: dict[str, Any],
) -> tuple[str | None, str | None]:
    reference = classification.get("profile_manifest")
    if not isinstance(reference, dict):
        return None, None
    path = _campaign_file(campaign_dir, str(reference.get("path", "")))
    if not path.is_file() or file_sha256(path) != reference.get("sha256"):
        raise ConfigurationError("批准画像清单摘要不一致。")
    profile = _read_json(path, "批准画像清单")
    profile_id = profile.get("profile_id") or profile.get("id")
    profile_digest = profile.get("profile_digest") or profile.get("digest")
    if isinstance(profile.get("profile"), dict):
        profile_id = profile_id or profile["profile"].get("id")
        profile_digest = profile_digest or profile["profile"].get("digest")
    return (
        str(profile_id) if profile_id is not None else None,
        str(profile_digest) if profile_digest is not None else None,
    )


def prepare_profile_manifest(
    campaign_dir: Path,
    snapshot: Path,
    profile_id: str,
    output: Path,
) -> dict[str, Any]:
    """把官方取证形成的完整 Snapshot 规范化为 classify 审核输入。"""

    _validate_existing_campaign_path(campaign_dir)
    _reject_contaminated_campaign(campaign_dir)
    manifest = load_campaign_manifest(campaign_dir)
    _verify_plan_identity(campaign_dir, manifest)
    if campaign_status(campaign_dir, None).get("status") != "official_sealed":
        raise ConfigurationError("prepare-profile 只允许从 official_sealed 状态执行。")
    if not SAFE_ID_RE.fullmatch(profile_id):
        raise ConfigurationError("--profile-id 格式非法。")
    if not snapshot.is_file() or snapshot.is_symlink():
        raise ConfigurationError("--snapshot 必须是非符号链接普通文件。")
    if not output.is_absolute() or output.is_symlink() or output.exists():
        raise ConfigurationError("--output 必须是不存在的非符号链接绝对路径。")
    resolved_output = output.resolve(strict=False)
    if resolved_output in {
        Path("/").resolve(),
        Path.home().resolve(),
        Path("/tmp").resolve(),
    }:
        raise ConfigurationError("--output 不能是根目录、HOME 或 /tmp 本身。")
    try:
        resolved_output.relative_to(campaign_dir.resolve(strict=True))
    except ValueError:
        pass
    else:
        raise ConfigurationError("画像草案不得写入不可变 Campaign 目录。")
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            "go",
            "run",
            "./cmd/egresscatalogstage",
            "-prepare-snapshot",
            str(snapshot),
            "-prepare-profile-id",
            profile_id,
            "-prepare-output",
            str(output),
        ],
        cwd=repository_root / "backend",
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigurationError("画像草案生成器失败：" + (detail or "无错误输出"))
    try:
        profile = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ConfigurationError("画像草案生成器未返回合法 JSON。") from error
    if (
        not isinstance(profile, dict)
        or profile.get("status") != "draft"
        or profile.get("codex_version") != manifest["target_version"]
        or profile.get("profile_id") != profile_id
        or not SHA256_RE.fullmatch(str(profile.get("profile_digest", "")))
        or not output.is_file()
        or output.is_symlink()
        or _fingerprint(_read_json(output, "画像草案")) != _fingerprint(profile)
    ):
        raise ConfigurationError("画像草案身份、版本或落盘内容不一致。")
    profile["output"] = str(output)
    return profile


def stage_profile_catalog(campaign_dir: Path, output: Path) -> dict[str, Any]:
    """验证 profile_approved 身份并调用 Go 契约生成离线候选目录。"""

    _validate_existing_campaign_path(campaign_dir)
    _reject_contaminated_campaign(campaign_dir)
    manifest = load_campaign_manifest(campaign_dir)
    _verify_plan_identity(campaign_dir, manifest)
    status = campaign_status(campaign_dir, None)
    if status.get("status") != "profile_approved":
        raise ConfigurationError("stage-profile 只允许从 profile_approved 状态执行。")
    classification = _load_stage_result(campaign_dir, "classify")
    if (
        classification.get("status") != "complete"
        or classification.get("migration", {}).get("unclassified_count") != 0
    ):
        raise ConfigurationError("分类尚未完整批准或仍有未分类项。")
    references = {
        key: classification.get(key)
        for key in (
            "target_rule_manifest",
            "migration_manifest",
            "scenario_manifest",
            "profile_manifest",
            "assertion_profile_manifest",
        )
    }
    for label, reference in references.items():
        if not isinstance(reference, dict):
            raise ConfigurationError(f"批准分类缺少引用：{label}")
        approved_path = _campaign_file(
            campaign_dir,
            str(reference.get("path", "")),
        )
        if (
            not approved_path.is_file()
            or approved_path.is_symlink()
            or file_sha256(approved_path) != reference.get("sha256")
        ):
            raise ConfigurationError(f"批准分类引用摘要不一致：{label}")
    joint_digest = _fingerprint(
        {
            key: reference["sha256"]
            for key, reference in references.items()
            if isinstance(reference, dict)
        }
    )
    if joint_digest != classification.get("joint_manifest_sha256"):
        raise ConfigurationError("五份批准清单联合摘要不一致。")
    profile_reference = references["profile_manifest"]
    assert isinstance(profile_reference, dict)
    profile_path = _campaign_file(campaign_dir, profile_reference["path"])
    profile = _read_json(profile_path, "批准画像清单")
    if (
        profile.get("status") != "approved"
        or profile.get("codex_version") != manifest.get("target_version")
        or not SHA256_RE.fullmatch(str(profile.get("profile_digest", "")))
    ):
        raise ConfigurationError("批准画像身份不完整或与 Campaign 目标版本不一致。")

    if not output.is_absolute() or output.is_symlink():
        raise ConfigurationError("--output 必须是非符号链接绝对路径。")
    resolved_output = output.resolve(strict=False)
    if resolved_output in {
        Path("/").resolve(),
        Path.home().resolve(),
        Path("/tmp").resolve(),
    }:
        raise ConfigurationError("--output 不能是根目录、HOME 或 /tmp 本身。")
    if output.exists():
        raise ConfigurationError("--output 已存在，禁止覆盖候选目录。")
    try:
        resolved_output.relative_to(campaign_dir.resolve(strict=True))
    except ValueError:
        pass
    else:
        raise ConfigurationError("候选 RuntimeCatalog 不得写入不可变 Campaign 目录。")

    repository_root = Path(__file__).resolve().parents[2]
    backend_root = repository_root / "backend"
    command = [
        "go",
        "run",
        "./cmd/egresscatalogstage",
        "-profile-manifest",
        str(profile_path),
        "-campaign-id",
        str(manifest["campaign_id"]),
        "-classification-sha256",
        joint_digest,
        "-output",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=backend_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigurationError(
            "候选 RuntimeCatalog 生成器失败：" + (detail or "无错误输出")
        )
    try:
        receipt = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ConfigurationError("候选 RuntimeCatalog 生成器未返回合法收据。") from error
    if (
        not isinstance(receipt, dict)
        or receipt.get("campaign_id") != manifest["campaign_id"]
        or receipt.get("classification_sha256") != joint_digest
        or receipt.get("target_version") != manifest["target_version"]
        or receipt.get("target_profile_digest") != profile["profile_digest"]
        or receipt.get("active_unchanged") is not True
        or receipt.get("production_selector_changed") is not False
        or receipt.get("candidate_release_mode") != "previous"
    ):
        raise ConfigurationError("候选 RuntimeCatalog 收据身份或权限边界不一致。")
    _verify_catalog_stage_output(output, receipt)
    receipt["output"] = str(output)
    return receipt


def _verify_catalog_stage_output(output: Path, receipt: dict[str, Any]) -> None:
    """逐文件重算候选目录，拒绝收据与落盘资产分离。"""

    if not output.is_dir() or output.is_symlink():
        raise ConfigurationError("候选 RuntimeCatalog 输出目录不存在或不可信。")
    receipt_path = output / "catalog-stage-receipt.json"
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise ConfigurationError("候选 RuntimeCatalog 缺少可信生成收据。")
    stored_receipt = _read_json(receipt_path, "候选 RuntimeCatalog 收据")
    if _fingerprint(stored_receipt) != _fingerprint(receipt):
        raise ConfigurationError("候选 RuntimeCatalog 标准输出与落盘收据不一致。")
    inventory = receipt.get("inventory")
    if not isinstance(inventory, list) or not inventory:
        raise ConfigurationError("候选 RuntimeCatalog inventory 为空。")
    if _fingerprint(inventory) != receipt.get("inventory_sha256"):
        raise ConfigurationError("候选 RuntimeCatalog inventory 摘要不一致。")
    seen: set[str] = set()
    for item in inventory:
        if not isinstance(item, dict):
            raise ConfigurationError("候选 RuntimeCatalog inventory 条目非法。")
        relative_raw = item.get("path")
        if not isinstance(relative_raw, str):
            raise ConfigurationError("候选 RuntimeCatalog inventory 路径非法。")
        relative = Path(relative_raw)
        if (
            relative.is_absolute()
            or relative.as_posix() != relative_raw
            or ".." in relative.parts
            or relative_raw in seen
        ):
            raise ConfigurationError("候选 RuntimeCatalog inventory 路径越界或重复。")
        seen.add(relative_raw)
        target = output / relative
        if (
            not target.is_file()
            or target.is_symlink()
            or file_sha256(target) != item.get("sha256")
            or target.stat().st_size != item.get("size")
        ):
            raise ConfigurationError(
                f"候选 RuntimeCatalog inventory 文件漂移：{relative_raw}"
            )
    actual = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.name != "catalog-stage-receipt.json"
    }
    if actual != seen:
        raise ConfigurationError("候选 RuntimeCatalog inventory 未精确覆盖输出文件。")


def _verify_stage_evidence(stage: dict[str, Any], label: str) -> None:
    expected_inventory = stage.get("evidence_inventory")
    if not isinstance(expected_inventory, dict):
        raise ConfigurationError(f"{label}缺少封存证据清单。")
    roots = [Path(value) for value in stage.get("evidence_roots", [])]
    if not roots:
        raise ConfigurationError(f"{label}缺少证据根。")
    current = _evidence_inventory(roots)
    if current["digest"] != expected_inventory.get("digest"):
        raise ConfigurationError(f"{label}原始证据摘要在封存后发生变化。")
    security = stage.get("security")
    if (
        not isinstance(security, dict)
        or security.get("raw_evidence_private") is not True
        or security.get("known_secret_scan_passed") is not True
    ):
        raise ConfigurationError(f"{label}秘密扫描门禁未通过。")
    current_security = _evidence_security(roots)
    if not current_security.get("known_secret_scan_passed"):
        raise ConfigurationError(f"{label}当前证据秘密扫描未通过。")
    if not _evidence_permissions_private(roots):
        raise ConfigurationError(f"{label}当前证据权限不再私有。")


def _verify_sealed_official_binaries(
    official: dict[str, Any], manifest: dict[str, Any]
) -> bool:
    verification = official.get("binary_verification")
    identities = verification.get("identities") if isinstance(verification, dict) else None
    helpers = verification.get("helpers") if isinstance(verification, dict) else None
    expected_labels = {
        "container:capture_codex_bin",
        "container:relay_codex_bin",
        "host:relay_codex_bin",
    }
    expected_helper_labels = {
        "container:capture_code_mode_host_bin",
        "container:relay_code_mode_host_bin",
        "host:relay_code_mode_host_bin",
    }
    package_identity = manifest.get("official_identity", {}).get("package")
    return bool(
        isinstance(verification, dict)
        and verification.get("passed") is True
        and verification.get("expected_version") == manifest.get("target_version")
        and verification.get("expected_sha256") == manifest.get("target_sha256")
        and verification.get("runtime_image_reference")
        == manifest.get("official_identity", {}).get("runtime_image")
        and IMAGE_ID_RE.fullmatch(str(verification.get("runtime_image_id", "")))
        and isinstance(identities, list)
        and {item.get("label") for item in identities if isinstance(item, dict)}
        == expected_labels
        and all(
            isinstance(item, dict)
            and item.get("version") == manifest.get("target_version")
            and item.get("sha256") == manifest.get("target_sha256")
            for item in identities
        )
        and isinstance(package_identity, dict)
        and verification.get("package") == package_identity
        and isinstance(helpers, list)
        and {item.get("label") for item in helpers if isinstance(item, dict)}
        == expected_helper_labels
        and all(
            isinstance(item, dict)
            and item.get("sha256")
            == package_identity.get("code_mode_host_sha256")
            for item in helpers
        )
    )


def compare_campaign(campaign_dir: Path, candidate_id: str) -> dict[str, Any]:
    """只读取封存材料并写比较收据；本函数不运行任何命令或网络请求。"""

    _reject_contaminated_campaign(campaign_dir)
    manifest = load_campaign_manifest(campaign_dir)
    _verify_plan_identity(campaign_dir, manifest)
    official = _load_stage_result(campaign_dir, "capture-official")
    classification = _load_stage_result(campaign_dir, "classify")
    candidate = _load_stage_result(
        campaign_dir, "capture-candidate", candidate_id
    )
    for label, value in (
        ("官方", official),
        ("分类", classification),
        ("候选", candidate),
    ):
        if value.get("status") != "complete":
            raise ConfigurationError(f"{label}阶段尚未完整封存。")
    if not official.get("restoration", {}).get("passed"):
        raise ConfigurationError("官方抓包环境恢复门禁未通过。")
    if not candidate.get("restoration", {}).get("passed"):
        raise ConfigurationError("候选抓包环境恢复门禁未通过。")
    if not _verify_sealed_official_binaries(official, manifest):
        raise ConfigurationError("官方抓包未绑定全部目标 Codex 二进制身份。")
    _verify_stage_evidence(official, "官方")
    _verify_stage_evidence(candidate, "候选")
    official_surface = _surface_from_stage(campaign_dir, "capture-official")
    candidate_surface = _surface_from_stage(
        campaign_dir, "capture-candidate", candidate_id=candidate_id
    )
    difference = compare_surfaces(official_surface, candidate_surface)
    rules = _approved_rules(campaign_dir, manifest, require_approved=True)
    official_jobs = _campaign_jobs(
        campaign_dir,
        manifest,
        "official",
        use_approved_scenario=True,
    )
    candidate_identity = candidate.get("identity", {})
    candidate_jobs = _campaign_jobs(
        campaign_dir,
        manifest,
        "candidate",
        candidate_id=candidate_id,
        runtime_image=str(candidate_identity.get("image_reference", "")),
        profile_id=str(candidate_identity.get("profile_id", "")),
        profile_digest=str(candidate_identity.get("profile_digest", "")),
        build_id=str(candidate_identity.get("build_id", "")),
        deployed_version=str(candidate_identity.get("deployed_version", "")),
        candidate_image_id=str(candidate_identity.get("image_id", "")),
        source_tree_sha256=str(candidate_identity.get("source_tree_sha256", "")),
    )
    _validate_capture_job_results(
        official_jobs,
        official.get("results"),
        phase="official",
    )
    _validate_capture_job_results(
        candidate_jobs,
        candidate.get("results"),
        phase="candidate",
    )
    results = list(official.get("results", [])) + list(candidate.get("results", []))
    coverage = build_coverage(rules, official_jobs + candidate_jobs, results)
    if not coverage.get("complete"):
        raise ConfigurationError("官方／候选逐规则抓包覆盖不完整。")
    approved_profile_id, approved_profile_digest = _profile_binding_from_manifest(
        campaign_dir, classification
    )
    candidate_profile_id = candidate_identity.get("profile_id")
    candidate_profile_digest = candidate_identity.get("profile_digest")
    profile_binding_matches = bool(candidate_profile_id and candidate_profile_digest)
    if approved_profile_id is not None:
        profile_binding_matches = (
            profile_binding_matches and candidate_profile_id == approved_profile_id
        )
    if approved_profile_digest is not None:
        profile_binding_matches = (
            profile_binding_matches
            and candidate_profile_digest == approved_profile_digest
        )
    report = {
        "schema_version": COMPARISON_SCHEMA,
        "status": "complete",
        "candidate_id": candidate_id,
        "target_version": manifest["target_version"],
        "equal": difference["equal"],
        "official_to_candidate": difference,
        "coverage": coverage,
        "official_package_digest": official["package_digest"],
        "candidate_package_digest": candidate["package_digest"],
        "classification_package_digest": classification["package_digest"],
        "official_evidence_inventory_digest": official["evidence_inventory"]["digest"],
        "candidate_evidence_inventory_digest": candidate["evidence_inventory"]["digest"],
        "profile_id": candidate_profile_id,
        "profile_digest": candidate_profile_digest,
        "profile_binding_matches": profile_binding_matches,
        "offline_only": True,
    }
    assertion_root = ensure_private_directory(
        campaign_dir / "assertions" / candidate_id, campaign_dir
    )
    skeleton_path = assertion_root / "results.template.json"
    _, comparison_path = _stage_path(campaign_dir, "compare", candidate_id)
    if comparison_path.exists():
        sealed = _load_stage_result(campaign_dir, "compare", candidate_id)
        for field, expected in report.items():
            if field == "schema_version":
                continue
            if sealed.get(field) != expected:
                raise ConfigurationError("既有比较收据与当前封存证据不一致。")
    else:
        save_stage_result(campaign_dir, "compare", report, candidate_id=candidate_id)
        sealed = _load_stage_result(campaign_dir, "compare", candidate_id)
    machine_root = ensure_private_directory(
        assertion_root / "machine",
        campaign_dir,
    )
    ensure_private_directory(machine_root / "official", campaign_dir)
    ensure_private_directory(machine_root / "candidate", campaign_dir)
    validation_modes = _acceptance_validation_modes(
        campaign_dir, classification, rules
    )
    official_authority = _classification_official_authority(classification)
    template_rules: list[dict[str, Any]] = []
    for rule in rules:
        official_output = machine_root / "official" / f"{rule}.json"
        candidate_output = machine_root / "candidate" / f"{rule}.json"
        row: dict[str, Any] = {
            "rule": rule,
            "validation_mode": validation_modes[rule],
            "status": "blocked",
            "candidate_evidence_refs": [],
            "candidate_machine_result": {
                "path": candidate_output.relative_to(campaign_dir).as_posix(),
                "sha256": None,
            },
            "candidate_command": _campaign_machine_command(
                campaign_dir,
                manifest,
                classification,
                candidate,
                rule=rule,
                output=candidate_output,
                side="candidate",
            ),
            "evidence_level": "unreviewed",
            "rationale": "待执行机器断言并复核。",
        }
        if validation_modes[rule] == "dual_wire":
            row["official_evidence_refs"] = []
            row["official_machine_result"] = {
                "path": official_output.relative_to(campaign_dir).as_posix(),
                "sha256": None,
            }
            row["official_command"] = _campaign_machine_command(
                campaign_dir,
                manifest,
                classification,
                official,
                rule=rule,
                output=official_output,
                side="official",
            )
        else:
            row["official_authority"] = dict(official_authority)
        template_rules.append(row)
    skeleton = {
        "schema_version": ASSERTION_TEMPLATE_SCHEMA,
        "document_kind": "template",
        "candidate_id": candidate_id,
        "target_version": manifest["target_version"],
        "profile_id": candidate_profile_id,
        "profile_digest": candidate_profile_digest,
        "official_package_digest": official["package_digest"],
        "candidate_package_digest": candidate["package_digest"],
        "comparison_package_digest": sealed["package_digest"],
        "rules": template_rules,
    }
    if skeleton_path.exists():
        existing_skeleton = _read_json(skeleton_path, "逐规则断言模板")
        if _fingerprint(existing_skeleton) != _fingerprint(skeleton):
            raise ConfigurationError("逐规则断言模板已经存在且内容不同。")
    else:
        secure_write_json(skeleton_path, skeleton)
    report["assertion_template"] = str(skeleton_path)
    return report


def _acceptance_contract(
    campaign_dir: Path,
    classification: dict[str, Any],
) -> dict[str, Any]:
    """从本 Campaign **批准的**断言画像机器推导验收契约。

    权威是 classify 阶段人工批准并摘要绑定的 `assertion-profile.json`，不是仓库
    冻结画像——目标规则集允许相对基线增删，契约必须随批准画像走。仓库冻结摘要
    只用于 seal 前预检与工具自检（见 `acceptance_contract.FROZEN_CONTRACT_SHA256`）。
    """

    reference = classification.get("assertion_profile_manifest")
    if not isinstance(reference, dict):
        raise ConfigurationError("分类收据缺少批准断言画像。")
    path = _campaign_file(campaign_dir, str(reference.get("path", "")))
    if path.is_symlink() or not path.is_file() or file_sha256(path) != reference.get(
        "sha256"
    ):
        raise ConfigurationError("批准断言画像在封存后漂移或丢失。")
    try:
        return build_acceptance_contract(load_acceptance_profile(path))
    except AcceptanceContractError as error:
        raise ConfigurationError(f"验收契约不可用：{error}") from error


def _acceptance_validation_modes(
    campaign_dir: Path,
    classification: dict[str, Any],
    rules: tuple[str, ...],
) -> dict[str, str]:
    """推导每条规则的 validation_mode；规则集与批准画像不符即失败关闭。"""

    modes = _acceptance_contract(campaign_dir, classification)["validation_modes"]
    missing = sorted(set(rules) - set(modes))
    extra = sorted(set(modes) - set(rules))
    if missing or extra:
        raise ConfigurationError(
            f"目标规则集与批准断言画像不一致：缺失 {missing}，多余 {extra}。"
        )
    return {rule: modes[rule] for rule in rules}


def _acceptance_contract_sha256(
    campaign_dir: Path,
    classification: dict[str, Any],
) -> str:
    return acceptance_contract_sha256(
        _acceptance_contract(campaign_dir, classification)
    )


def _acceptance_expected_check_ids(
    campaign_dir: Path,
    classification: dict[str, Any],
    rule: str,
    side: str,
) -> list[str]:
    """本侧应执行的 check 全集；侧别限定项由验收契约按登记依据剔除。"""

    contract = _acceptance_contract(campaign_dir, classification)
    expected = contract["expected_check_ids"].get(rule)
    if not isinstance(expected, list) or not expected:
        raise ConfigurationError(f"批准断言画像缺少规则 {rule} 的 check 全集。")
    try:
        return expected_check_ids_for_side(contract, rule, side)
    except AcceptanceContractError as error:
        raise ConfigurationError(f"批准断言画像 check 全集不可用：{error}") from error


def _classification_official_authority(
    classification: dict[str, Any],
) -> dict[str, str]:
    """candidate_profile 行的官方权威：批准画像链的三个逐字摘要。"""

    reference = classification.get("assertion_profile_manifest")
    if not isinstance(reference, dict) or not SHA256_RE.fullmatch(
        str(reference.get("sha256", ""))
    ):
        raise ConfigurationError("分类收据缺少批准断言画像摘要。")
    package_digest = classification.get("package_digest")
    joint = classification.get("joint_manifest_sha256")
    if not SHA256_RE.fullmatch(str(package_digest or "")):
        raise ConfigurationError("分类收据缺少 classification package digest。")
    if not SHA256_RE.fullmatch(str(joint or "")):
        raise ConfigurationError("分类收据缺少批准联合摘要。")
    return {
        "assertion_profile_sha256": str(reference["sha256"]),
        "classification_package_digest": str(package_digest),
        "review_sha256": str(joint),
    }


def _inventory_index(stage: dict[str, Any], label: str) -> dict[str, str]:
    inventory = stage.get("evidence_inventory")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("entries"), list):
        raise ConfigurationError(f"{label}阶段缺少封存证据清单。")
    result: dict[str, str] = {}
    for entry in inventory["entries"]:
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{label}证据清单条目非法。")
        path = entry.get("path")
        digest = entry.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or not SHA256_RE.fullmatch(str(digest))
            or path in result
        ):
            raise ConfigurationError(f"{label}证据清单路径或摘要非法。")
        result[path] = str(digest)
    if not result:
        raise ConfigurationError(f"{label}证据清单为空。")
    return result


def _physical_evidence_index(stage: dict[str, Any], label: str) -> dict[str, Path]:
    roots = [Path(value) for value in stage.get("evidence_roots", [])]
    if not roots:
        raise ConfigurationError(f"{label}阶段缺少证据根。")
    index = {logical: path for logical, path in _evidence_files(roots)}
    if not index:
        raise ConfigurationError(f"{label}阶段当前证据为空。")
    return index


def _bound_evidence_path(
    stage: dict[str, Any],
    reference: Any,
    *,
    label: str,
) -> Path:
    _require_file_binding(reference, label)
    path = _physical_evidence_index(stage, label).get(reference["path"])
    if path is None or file_sha256(path) != reference["sha256"]:
        raise ConfigurationError(f"{label}证据文件漂移或丢失。")
    return path


def _candidate_stage_receipt_boundary(
    campaign_dir: Path,
    stage: dict[str, Any],
) -> tuple[dict[str, Any], Path, str]:
    """从阶段绑定的 attempt 推导收据根与 Kilo 后时间上界。"""

    attempt_reference = stage.get("attempt")
    _require_file_binding(attempt_reference, "候选 attempt")
    attempt_relative = Path(str(attempt_reference["path"]))
    attempt_id = attempt_relative.parent.name
    candidate_id = str(stage.get("candidate_id", ""))
    _, attempt = _load_capture_attempt(
        campaign_dir,
        "candidate",
        candidate_id,
        attempt_id,
    )
    environment = attempt.get("environment")
    if not isinstance(environment, dict):
        raise ConfigurationError("候选 attempt 缺少环境证据边界。")
    evidence_root = Path(str(environment.get("evidence_root", "")))
    stage_roots = {
        Path(value).resolve(strict=True)
        for value in stage.get("evidence_roots", [])
    }
    if (
        not evidence_root.is_absolute()
        or evidence_root.is_symlink()
        or not evidence_root.is_dir()
        or evidence_root.resolve(strict=True) not in stage_roots
    ):
        raise ConfigurationError("候选 attempt 收据根未绑定当前阶段。")
    checkpoint_path = (
        evidence_root
        / "environment"
        / "client-after"
        / "probe-manifest.json"
    )
    if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
        raise ConfigurationError("候选阶段缺少 Kilo 后探针清单。")
    checkpoint = _read_json(checkpoint_path, "Kilo 后探针清单")
    checkpoint_at = checkpoint.get("observed_at_utc")
    if checkpoint.get("phase") != "after" or not _is_rfc3339_timestamp(
        checkpoint_at
    ):
        raise ConfigurationError("Kilo 后探针清单身份或时间非法。")
    if _rfc3339_datetime(
        checkpoint_at, "Kilo 后检查点时间"
    ) < _rfc3339_datetime(
        attempt["completed_at_utc"], "attempt.completed_at_utc"
    ):
        raise ConfigurationError("Kilo 后检查点早于候选 run 完成时间。")
    return attempt, evidence_root.resolve(strict=True), str(checkpoint_at)


def _replay_capture_stage_receipts(
    campaign_dir: Path,
    stage: dict[str, Any],
    label: str,
) -> None:
    """在 status／compare／accept 每次读取时重放机器 finalizer。"""

    roots = [Path(value) for value in stage.get("evidence_roots", [])]
    campaign = load_campaign_manifest(campaign_dir)
    phase = "candidate" if stage.get("stage") == "capture-candidate" else "official"
    candidate_id = stage.get("candidate_id") if phase == "candidate" else None
    restoration_path = _bound_evidence_path(
        stage,
        stage.get("restoration", {}).get("report"),
        label=f"{label}环境恢复报告",
    )
    restoration = _validate_restoration_report(
        restoration_path,
        roots,
        phase=phase,
        candidate_id=candidate_id,
    )
    sealed_restoration = stage.get("restoration")
    if not isinstance(sealed_restoration, dict):
        raise ConfigurationError(f"{label}环境恢复门禁缺失。")
    capture_restoration = {
        key: value
        for key, value in sealed_restoration.items()
        if key != "post_client"
    }
    if _fingerprint(restoration) != _fingerprint(capture_restoration):
        raise ConfigurationError(f"{label}环境恢复机器收据与阶段封存内容不一致。")
    if phase == "official":
        return

    post_client = sealed_restoration.get("post_client")
    if not isinstance(post_client, dict):
        raise ConfigurationError(f"{label}缺少 Kilo 后恢复门禁。")
    post_client_path = _bound_evidence_path(
        stage,
        post_client.get("report"),
        label=f"{label}Kilo 后恢复报告",
    )
    replayed_post_client = _validate_restoration_report(
        post_client_path,
        roots,
        phase="candidate",
        candidate_id=str(candidate_id),
    )
    if _fingerprint(replayed_post_client) != _fingerprint(post_client):
        raise ConfigurationError(f"{label}Kilo 后恢复机器收据与封存内容不一致。")

    identity = stage.get("identity")
    if not isinstance(identity, dict):
        raise ConfigurationError(f"{label}候选身份缺失。")
    attempt, receipt_root, client_checkpoint_at = (
        _candidate_stage_receipt_boundary(campaign_dir, stage)
    )
    observed_path = _bound_evidence_path(
        stage,
        stage.get("observed_profile"),
        label=f"{label}运行画像观测",
    )
    observed_binding, _ = _validate_observed_profile_receipt(
        observed_path,
        [receipt_root],
        campaign_id=attempt["campaign_id"],
        attempt_id=attempt["attempt_id"],
        run_nonce=attempt["run_nonce"],
        attempt_started_at_utc=attempt["started_at_utc"],
        client_checkpoint_at_utc=client_checkpoint_at,
        candidate_id=str(candidate_id),
        target_version=campaign["target_version"],
        expected_profile_id=str(identity.get("profile_id", "")),
        expected_profile_digest=str(identity.get("profile_digest", "")),
        image_id=str(identity.get("image_id", "")),
        image_reference=str(identity.get("image_reference", "")),
        source_tree_sha256=str(identity.get("source_tree_sha256", "")),
        build_id=str(identity.get("build_id", "")),
        deployed_version=str(identity.get("deployed_version", "")),
    )
    if observed_binding != stage.get("observed_profile"):
        raise ConfigurationError(f"{label}运行画像绑定与阶段封存内容不一致。")
    client_bindings = stage.get("client_bindings")
    if not isinstance(client_bindings, list):
        raise ConfigurationError(f"{label}第三方客户端绑定缺失。")
    client_specs: list[str] = []
    for item in client_bindings:
        if not isinstance(item, dict) or not isinstance(item.get("client_id"), str):
            raise ConfigurationError(f"{label}第三方客户端绑定结构非法。")
        receipt_path = _bound_evidence_path(
            stage,
            item.get("receipt"),
            label=f"{label}第三方入口 {item['client_id']} 收据",
        )
        client_specs.append(f"{item['client_id']}={receipt_path}")
    replayed = _parse_client_evidence(
        client_specs,
        [receipt_root],
        campaign_id=attempt["campaign_id"],
        attempt_id=attempt["attempt_id"],
        run_nonce=attempt["run_nonce"],
        attempt_started_at_utc=attempt["started_at_utc"],
        client_checkpoint_at_utc=client_checkpoint_at,
        candidate_id=str(candidate_id),
        target_version=campaign["target_version"],
        model=campaign["configuration"]["model"],
        identity=identity,
    )
    if _fingerprint({"items": replayed}) != _fingerprint(
        {"items": client_bindings}
    ):
        raise ConfigurationError(f"{label}Kilo 机器收据与阶段封存内容不一致。")


def _validate_evidence_bindings(
    values: Any,
    inventory: dict[str, str],
    *,
    rule: str,
    label: str,
) -> set[str]:
    if not isinstance(values, list) or not values:
        raise ConfigurationError(f"逐规则断言 {rule} 缺少{label}证据引用。")
    paths: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            raise ConfigurationError(f"逐规则断言 {rule} 的{label}证据引用非法。")
        path = value.get("path")
        digest = value.get("sha256")
        if not isinstance(path, str) or inventory.get(path) != digest:
            raise ConfigurationError(f"逐规则断言 {rule} 的{label}证据摘要不匹配。")
        if path in paths:
            raise ConfigurationError(f"逐规则断言 {rule} 的{label}证据重复。")
        paths.add(path)
    return paths


def _command_option(command: list[str], name: str, rule: str) -> str:
    positions = [index for index, value in enumerate(command) if value == name]
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise ConfigurationError(f"逐规则断言 {rule} 的机器命令缺少唯一 {name}。")
    return command[positions[0] + 1]


def _campaign_machine_command(
    campaign_dir: Path,
    manifest: dict[str, Any],
    classification: dict[str, Any],
    capture_stage: dict[str, Any],
    *,
    rule: str,
    output: Path,
    side: str,
) -> list[str]:
    context = capture_stage.get("assertion_context")
    if not isinstance(context, dict):
        raise ConfigurationError("抓包阶段缺少 assertion_context。")
    profile_reference = classification.get("assertion_profile_manifest")
    rule_reference = classification.get("target_rule_manifest")
    if not isinstance(profile_reference, dict) or not isinstance(rule_reference, dict):
        raise ConfigurationError("分类收据缺少目标断言画像或规则清单。")
    profile_path = _campaign_file(campaign_dir, str(profile_reference.get("path", "")))
    rule_path = _campaign_file(campaign_dir, str(rule_reference.get("path", "")))
    return build_machine_assertion_command(
        rule_id=rule,
        capture_manifest=str(context.get("capture_manifest_path", "")),
        evidence_root=str(context.get("evidence_root", "")),
        profile=str(profile_path.resolve(strict=True)),
        rule_manifest=str(rule_path.resolve(strict=True)),
        expected_codex_version=manifest["target_version"],
        expected_profile_sha256=str(profile_reference.get("sha256", "")),
        side=side,
        output=str(output.resolve()),
    )


def _rerun_machine_assertion(
    command: list[str],
    submitted: dict[str, Any],
    *,
    rule: str,
    label: str,
) -> None:
    """在 accept 内离线重放 checker，防止手工伪造 pass 收据。"""

    output_positions = [index for index, value in enumerate(command) if value == "--output"]
    if len(output_positions) != 1 or output_positions[0] + 1 >= len(command):
        raise ConfigurationError(f"逐规则断言 {rule} {label}重放命令缺少输出路径。")
    with tempfile.TemporaryDirectory(prefix="codex-egress-assertion-") as temporary:
        replay_output = Path(temporary) / "result.json"
        replay_command = list(command)
        replay_command[output_positions[0] + 1] = str(replay_output)
        try:
            replay = subprocess.run(
                replay_command,
                cwd=Path(__file__).resolve().parents[2],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ConfigurationError(
                f"逐规则断言 {rule} {label}离线重放失败：{error}"
            ) from error
        if replay.returncode != 0 or not replay_output.is_file() or replay_output.is_symlink():
            detail = replay.stderr.strip() or replay.stdout.strip()
            raise ConfigurationError(
                f"逐规则断言 {rule} {label}离线重放未通过：{detail[:500]}"
            )
        replay_result = _read_json(replay_output, f"{rule} {label}重放结果")
    expected_fields = {
        "schema_version": MACHINE_ASSERTION_SCHEMA,
        "rule_id": rule,
        "status": "pass",
        "exit_code": 0,
        "checker_sha256": submitted.get("checker_sha256"),
        "command_sha256": machine_command_sha256(replay_command),
    }
    for field, expected in expected_fields.items():
        if replay_result.get(field) != expected:
            raise ConfigurationError(
                f"逐规则断言 {rule} {label}重放结果 {field} 不一致。"
            )
    if replay_result.get("checks") != submitted.get("checks"):
        raise ConfigurationError(
            f"逐规则断言 {rule} {label}提交检查项与离线重放不一致。"
        )


def _validate_machine_assertion(
    campaign_dir: Path,
    manifest: dict[str, Any],
    classification: dict[str, Any],
    capture_stage: dict[str, Any],
    row: dict[str, Any],
    rule: str,
    bound_paths: set[str],
    *,
    side: str,
) -> set[str]:
    label = "官方" if side == "official" else "候选"
    reference = row.get(f"{side}_machine_result")
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ConfigurationError(f"逐规则断言 {rule} 缺少{label}机器结果绑定。")
    relative = reference.get("path")
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ConfigurationError(f"逐规则断言 {rule} {label}机器结果路径必须位于 Campaign。")
    result_path = _campaign_file(campaign_dir, relative)
    if (
        not result_path.is_file()
        or result_path.is_symlink()
        or file_sha256(result_path) != reference.get("sha256")
    ):
        raise ConfigurationError(f"逐规则断言 {rule} {label}机器结果摘要不一致。")
    result = _read_json(result_path, f"{rule} {label}机器断言结果")
    if result.get("schema_version") != MACHINE_ASSERTION_SCHEMA:
        raise ConfigurationError(f"逐规则断言 {rule} {label}机器结果 schema 不受支持。")
    if (
        result.get("rule_id") != rule
        or result.get("status") != "pass"
        or result.get("exit_code") != 0
    ):
        raise ConfigurationError(f"逐规则断言 {rule} {label}机器结果未通过。")
    command = row.get(f"{side}_command")
    if not isinstance(command, list) or not command or not all(
        isinstance(value, str) and value for value in command
    ):
        raise ConfigurationError(f"逐规则断言 {rule} 缺少{label}机器命令。")
    context = capture_stage.get("assertion_context")
    if not isinstance(context, dict):
        raise ConfigurationError(f"{label}抓包阶段缺少 assertion_context。")
    expected_command = _campaign_machine_command(
        campaign_dir,
        manifest,
        classification,
        capture_stage,
        rule=rule,
        output=result_path,
        side=side,
    )
    if command != expected_command:
        raise ConfigurationError(f"逐规则断言 {rule} {label}机器命令未精确绑定当前版本 Campaign。")
    if machine_command_sha256(command) != result.get("command_sha256"):
        raise ConfigurationError(f"逐规则断言 {rule} {label}命令摘要不一致。")
    pinned_checkers = {
        entry.get("path"): entry.get("sha256")
        for entry in manifest.get("tool_identity", {}).get("entries", [])
        if isinstance(entry, dict)
    }
    checker_sha = pinned_checkers.get("candidate_rule_assertion.py")
    if not checker_sha or checker_sha != result.get("checker_sha256"):
        raise ConfigurationError(f"逐规则断言 {rule} checker 未绑定 plan 工具摘要。")
    checker_path = Path(__file__).resolve().parent / "candidate_rule_assertion.py"
    if not checker_path.is_file() or file_sha256(checker_path) != checker_sha:
        raise ConfigurationError(f"逐规则断言 {rule} checker 文件在 plan 后漂移。")
    checks = result.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ConfigurationError(f"逐规则断言 {rule} 没有机器检查项。")
    check_ids: set[str] = set()
    for check in checks:
        if not isinstance(check, dict) or check.get("passed") is not True:
            raise ConfigurationError(f"逐规则断言 {rule} {label}存在未通过机器检查。")
        check_id = check.get("id")
        evidence_paths = check.get("evidence_paths")
        if (
            not isinstance(check_id, str)
            or not check_id
            or check_id in check_ids
            or check.get("expected") is None
            or check.get("actual") is None
            or not isinstance(evidence_paths, list)
            or not evidence_paths
        ):
            raise ConfigurationError(f"逐规则断言 {rule} {label}机器检查结构非法。")
        prefix = context.get("evidence_prefix")
        logical_paths = {
            f"{prefix}/{path}"
            for path in evidence_paths
            if isinstance(path, str) and not Path(path).is_absolute() and ".." not in Path(path).parts
        }
        if len(logical_paths) != len(evidence_paths) or not logical_paths.issubset(bound_paths):
            raise ConfigurationError(f"逐规则断言 {rule} {label}机器检查未精确引用封存证据。")
        check_ids.add(check_id)
    _rerun_machine_assertion(command, result, rule=rule, label=label)
    return check_ids


def _validate_assertion_results(
    campaign_dir: Path,
    assertions: dict[str, Any],
    *,
    rules: tuple[str, ...],
    manifest: dict[str, Any],
    candidate_id: str,
    classification: dict[str, Any],
    official: dict[str, Any],
    candidate: dict[str, Any],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    required_top_fields = {
        "schema_version",
        "document_kind",
        "candidate_id",
        "target_version",
        "profile_id",
        "profile_digest",
        "official_package_digest",
        "candidate_package_digest",
        "comparison_package_digest",
        "acceptance_contract_sha256",
        "rules",
    }
    if assertions.get("schema_version") in LEGACY_RESULTS_SCHEMAS:
        raise ConfigurationError(
            "逐规则断言旧 schema 已废除：v1 的双侧同构与正负例契约没有事实来源，"
            "必须按 codex-egress-rule-assertions/v2 重新生成。"
        )
    if assertions.get("schema_version") != RESULTS_SCHEMA_V2:
        raise ConfigurationError("逐规则断言 schema_version 不受支持。")
    if not required_top_fields.issubset(assertions) or set(assertions) - required_top_fields - {"$schema"}:
        raise ConfigurationError("逐规则断言结果顶层字段不闭合。")
    if assertions.get("acceptance_contract_sha256") != _acceptance_contract_sha256(
        campaign_dir, classification
    ):
        raise ConfigurationError("逐规则断言未绑定冻结验收契约摘要。")
    if assertions.get("document_kind") != "results":
        raise ConfigurationError("逐规则断言 document_kind 必须是 results。")
    for field, expected in (
        ("candidate_id", candidate_id),
        ("target_version", manifest["target_version"]),
    ):
        if assertions.get(field) != expected:
            raise ConfigurationError(f"逐规则断言 {field} 与 Campaign 不一致。")
    identity = candidate.get("identity", {})
    for field in ("profile_id", "profile_digest"):
        if assertions.get(field) != identity.get(field):
            raise ConfigurationError(f"逐规则断言 {field} 未绑定候选身份。")
    required_bindings = (
        ("official_package_digest", comparison.get("official_package_digest")),
        ("candidate_package_digest", comparison.get("candidate_package_digest")),
        ("comparison_package_digest", comparison.get("package_digest")),
    )
    for field, expected in required_bindings:
        if assertions.get(field) != expected:
            raise ConfigurationError(f"逐规则断言 {field} 摘要不一致。")
    rows = assertions.get("rules")
    if not isinstance(rows, list):
        raise ConfigurationError("逐规则断言 rules 必须是数组。")
    official_inventory = _inventory_index(official, "官方")
    candidate_inventory = _inventory_index(candidate, "候选")
    validation_modes = _acceptance_validation_modes(
        campaign_dir, classification, rules
    )
    official_authority = _classification_official_authority(classification)
    seen: list[str] = []
    passed = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ConfigurationError(f"逐规则断言 {index} 必须是对象。")
        rule = row.get("rule")
        if rule not in rules:
            raise ConfigurationError(f"逐规则断言 {index} 引用清单外规则。")
        mode = validation_modes[str(rule)]
        if row.get("validation_mode") != mode:
            raise ConfigurationError(
                f"逐规则断言 {rule} 的 validation_mode 与验收契约不一致。"
            )
        common_row_fields = {
            "rule",
            "validation_mode",
            "status",
            "candidate_evidence_refs",
            "candidate_machine_result",
            "candidate_command",
            "evidence_level",
            "rationale",
        }
        if mode == MODE_DUAL_WIRE:
            required_row_fields = common_row_fields | {
                "official_evidence_refs",
                "official_machine_result",
                "official_command",
            }
        else:
            required_row_fields = common_row_fields | {"official_authority"}
        if set(row) != required_row_fields:
            raise ConfigurationError(f"逐规则断言 {index} 字段不闭合。")
        if row.get("status") != "pass":
            raise ConfigurationError(f"逐规则断言 {rule} 没有机器通过，不得接受。")
        if row.get("evidence_level") != "full":
            raise ConfigurationError(f"逐规则断言 {rule} 证据等级不是 full。")
        rationale = row.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ConfigurationError(f"逐规则断言 {rule} 缺少 rationale。")
        candidate_expected_check_ids = set(
            _acceptance_expected_check_ids(
                campaign_dir, classification, str(rule), "candidate"
            )
        )
        candidate_paths = _validate_evidence_bindings(
            row.get("candidate_evidence_refs"),
            candidate_inventory,
            rule=str(rule),
            label="候选",
        )
        candidate_check_ids = _validate_machine_assertion(
            campaign_dir,
            manifest,
            classification,
            candidate,
            row,
            str(rule),
            candidate_paths,
            side="candidate",
        )
        if candidate_check_ids != candidate_expected_check_ids:
            raise ConfigurationError(
                f"逐规则断言 {rule} 候选机器检查与批准画像 check 全集不一致。"
            )
        if mode == MODE_DUAL_WIRE:
            official_paths = _validate_evidence_bindings(
                row.get("official_evidence_refs"),
                official_inventory,
                rule=str(rule),
                label="官方",
            )
            official_check_ids = _validate_machine_assertion(
                campaign_dir,
                manifest,
                classification,
                official,
                row,
                str(rule),
                official_paths,
                side="official",
            )
            official_expected_check_ids = set(
                _acceptance_expected_check_ids(
                    campaign_dir, classification, str(rule), "official"
                )
            )
            if official_check_ids != official_expected_check_ids:
                raise ConfigurationError(
                    f"逐规则断言 {rule} 官方机器检查与批准画像 check 全集不一致。"
                )
        else:
            if row.get("official_authority") != official_authority:
                raise ConfigurationError(
                    f"逐规则断言 {rule} 未逐字绑定批准画像官方权威。"
                )
        seen.append(str(rule))
        passed += 1
    if sorted(seen) != sorted(rules) or len(seen) != len(set(seen)):
        raise ConfigurationError("逐规则断言未使目标规则全集唯一闭环。")
    return {
        "complete": True,
        "rule_count": len(rules),
        "pass_count": passed,
        "not_applicable_count": 0,
        "failed_rules": [],
    }


def _required_client_bindings(
    campaign_dir: Path,
    classification: dict[str, Any],
) -> set[str]:
    reference = classification.get("scenario_manifest")
    if not isinstance(reference, dict):
        return set()
    path = _campaign_file(campaign_dir, str(reference.get("path", "")))
    payload = _read_json(path, "目标场景清单")
    raw = payload.get("required_client_bindings", [])
    if (
        not isinstance(raw, list)
        or not all(isinstance(item, str) for item in raw)
        or len(raw) != len(set(raw))
        or not REQUIRED_CLIENT_BINDINGS.issubset(set(raw))
    ):
        raise ConfigurationError("目标场景 required_client_bindings 非法。")
    return set(raw)


def _write_blocked_acceptance_attempt(
    campaign_dir: Path,
    candidate_id: str,
    result: dict[str, Any],
) -> Path:
    """把未通过的 accept 结果写入独立 attempt，保留失败门禁供复核。

    accept 成功结果按 candidate-id 只封存一次；失败结果则允许修复后重跑，因此不能写入
    成功结果的规范路径，也不能覆盖上一轮失败。这里沿用抓包 attempt 的时间戳加随机后缀
    规则，为每次失败建立不可覆盖的私有目录。
    """

    if not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("accept candidate-id 格式非法。")
    attempts_root = ensure_private_directory(
        campaign_dir / "acceptance" / candidate_id / "attempts",
        campaign_dir,
    )
    attempt_id = (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        + f"-{secrets.token_hex(8)}"
    )
    attempt_root = attempts_root / attempt_id
    if attempt_root.exists() or attempt_root.is_symlink():
        raise ConfigurationError("accept attempt-id 随机碰撞。")
    ensure_private_directory(attempt_root, campaign_dir)
    secure_write_json(attempt_root / "result.json", result)
    return attempt_root


def _candidate_external_gate_binding(
    *,
    evidence_root: Path,
    receipt_path: Path,
    manifest: dict[str, Any],
    candidate_id: str,
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """独立重放 candidate 外部门禁，并与封存候选身份逐字段交叉验证。"""

    try:
        root = evidence_root.resolve(strict=True)
        candidate_receipt = (
            receipt_path.resolve(strict=True)
            if receipt_path.is_absolute()
            else (root / receipt_path).resolve(strict=True)
        )
        relative = candidate_receipt.relative_to(root).as_posix()
        payload = external_gate_receipt.replay(root, relative)
    except (
        OSError,
        RuntimeError,
        ValueError,
        external_gate_receipt.GateReceiptError,
    ) as error:
        raise ConfigurationError(f"candidate 外部门禁收据无法重放：{error}") from error
    identity = candidate.get("identity")
    subject = payload.get("subject")
    if not isinstance(identity, dict) or not isinstance(subject, dict):
        raise ConfigurationError("candidate 外部门禁缺少候选身份。")
    expected = {
        "campaign_id": manifest.get("campaign_id"),
        "candidate_id": candidate_id,
        "target_version": manifest.get("target_version"),
        "profile_id": identity.get("profile_id"),
        "profile_digest": identity.get("profile_digest"),
        "candidate_package_digest": candidate.get("package_digest"),
        "candidate_source_tree_sha256": identity.get("source_tree_sha256"),
        "candidate_image_id": identity.get("image_id"),
        "candidate_image_reference": identity.get("image_reference"),
    }
    if payload.get("phase") != external_gate_receipt.CANDIDATE_PHASE or any(
        subject.get(key) != value for key, value in expected.items()
    ):
        raise ConfigurationError("candidate 外部门禁收据与 Campaign／候选身份不一致。")
    binding = {
        "evidence_root": str(root),
        "receipt": {
            "path": relative,
            "sha256": file_sha256(candidate_receipt),
            "bytes": candidate_receipt.stat().st_size,
        },
    }
    return binding, payload


def _replay_bound_candidate_external_gate(
    value: Any,
    *,
    manifest: dict[str, Any],
    candidate_id: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """从 AcceptanceFact 保存的根与相对路径重放外部门禁。"""

    if not isinstance(value, dict) or set(value) != {"evidence_root", "receipt"}:
        raise ConfigurationError("AcceptanceFact candidate 外部门禁绑定非法。")
    receipt = value.get("receipt")
    if (
        not isinstance(value.get("evidence_root"), str)
        or not isinstance(receipt, dict)
        or set(receipt) != {"path", "sha256", "bytes"}
        or not isinstance(receipt.get("path"), str)
        or not SHA256_RE.fullmatch(str(receipt.get("sha256")))
        or not isinstance(receipt.get("bytes"), int)
        or receipt.get("bytes") <= 0
    ):
        raise ConfigurationError("AcceptanceFact candidate 外部门禁文件绑定非法。")
    binding, payload = _candidate_external_gate_binding(
        evidence_root=Path(value["evidence_root"]),
        receipt_path=Path(receipt["path"]),
        manifest=manifest,
        candidate_id=candidate_id,
        candidate=candidate,
    )
    if binding != value:
        raise ConfigurationError("AcceptanceFact candidate 外部门禁摘要或大小漂移。")
    return payload


def accept_campaign(
    campaign_dir: Path,
    candidate_id: str,
    assertions_path: Path,
    external_gate_root: Path,
    external_gate_path: Path,
) -> dict[str, Any]:
    _reject_contaminated_campaign(campaign_dir)
    if assertions_path.is_symlink() or not assertions_path.is_file():
        raise ConfigurationError("逐规则断言结果必须是非符号链接普通文件。")
    manifest = load_campaign_manifest(campaign_dir)
    _verify_plan_identity(campaign_dir, manifest)
    official = _load_stage_result(campaign_dir, "capture-official")
    classification = _load_stage_result(campaign_dir, "classify")
    candidate = _load_stage_result(
        campaign_dir, "capture-candidate", candidate_id
    )
    comparison = _load_stage_result(campaign_dir, "compare", candidate_id)
    external_gate_binding, external_gate = _candidate_external_gate_binding(
        evidence_root=external_gate_root,
        receipt_path=external_gate_path,
        manifest=manifest,
        candidate_id=candidate_id,
        candidate=candidate,
    )
    _verify_stage_evidence(official, "官方")
    _verify_stage_evidence(candidate, "候选")
    rules = _approved_rules(campaign_dir, manifest, require_approved=True)
    assertions = _read_json(assertions_path, "逐规则断言结果")
    assertion_gate = _validate_assertion_results(
        campaign_dir,
        assertions,
        rules=rules,
        manifest=manifest,
        candidate_id=candidate_id,
        classification=classification,
        official=official,
        candidate=candidate,
        comparison=comparison,
    )
    identity = candidate.get("identity", {})
    required_identity_fields = {
        "git_commit",
        "source_tree_sha256",
        "image_reference",
        "image_digest",
        "image_id",
        "build_id",
        "deployed_version",
        "profile_id",
        "profile_digest",
    }
    identity_complete = required_identity_fields.issubset(identity) and all(
        identity.get(field) for field in required_identity_fields
    )
    identity_complete = identity_complete and bool(
        re.fullmatch(r"[a-f0-9]{40,64}", str(identity.get("git_commit", "")))
        and SHA256_RE.fullmatch(str(identity.get("source_tree_sha256", "")))
        and IMMUTABLE_IMAGE_RE.fullmatch(str(identity.get("image_reference", "")))
        and IMAGE_ID_RE.fullmatch(str(identity.get("image_digest", "")))
        and IMAGE_ID_RE.fullmatch(str(identity.get("image_id", "")))
        and SAFE_ID_RE.fullmatch(str(identity.get("build_id", "")))
        and SAFE_ID_RE.fullmatch(str(identity.get("deployed_version", "")))
        and SAFE_ID_RE.fullmatch(str(identity.get("profile_id", "")))
        and SHA256_RE.fullmatch(str(identity.get("profile_digest", "")))
    )
    attempt, receipt_root, client_checkpoint_at = (
        _candidate_stage_receipt_boundary(campaign_dir, candidate)
    )
    observed_profile_path = _bound_evidence_path(
        candidate,
        candidate.get("observed_profile"),
        label="运行画像观测",
    )
    _, observed_profile_receipt = _validate_observed_profile_receipt(
        observed_profile_path,
        [receipt_root],
        campaign_id=attempt["campaign_id"],
        attempt_id=attempt["attempt_id"],
        run_nonce=attempt["run_nonce"],
        attempt_started_at_utc=attempt["started_at_utc"],
        client_checkpoint_at_utc=client_checkpoint_at,
        candidate_id=candidate_id,
        target_version=manifest["target_version"],
        expected_profile_id=str(identity.get("profile_id", "")),
        expected_profile_digest=str(identity.get("profile_digest", "")),
        image_id=str(identity.get("image_id", "")),
        image_reference=str(identity.get("image_reference", "")),
        source_tree_sha256=str(identity.get("source_tree_sha256", "")),
        build_id=str(identity.get("build_id", "")),
        deployed_version=str(identity.get("deployed_version", "")),
    )
    client_bindings = candidate.get("client_bindings")
    if not isinstance(client_bindings, list):
        raise ConfigurationError("候选阶段缺少第三方客户端绑定。")
    client_specs: list[str] = []
    for item in client_bindings:
        if not isinstance(item, dict) or not isinstance(item.get("client_id"), str):
            raise ConfigurationError("第三方客户端绑定结构非法。")
        receipt_path = _bound_evidence_path(
            candidate,
            item.get("receipt"),
            label=f"第三方入口 {item['client_id']} 收据",
        )
        client_specs.append(f"{item['client_id']}={receipt_path}")
    reparsed_client_bindings = _parse_client_evidence(
        client_specs,
        [receipt_root],
        campaign_id=attempt["campaign_id"],
        attempt_id=attempt["attempt_id"],
        run_nonce=attempt["run_nonce"],
        attempt_started_at_utc=attempt["started_at_utc"],
        client_checkpoint_at_utc=client_checkpoint_at,
        candidate_id=candidate_id,
        target_version=manifest["target_version"],
        model=manifest["configuration"]["model"],
        identity=identity,
    )
    observed_clients = {item.get("client_id") for item in client_bindings if isinstance(item, dict)}
    required_clients = _required_client_bindings(campaign_dir, classification)
    client_bindings_valid = all(
        isinstance(item, dict)
        and item.get("status", "success") == "success"
        and item.get("model") == manifest["configuration"]["model"]
        and item.get("profile_id") == identity.get("profile_id")
        and item.get("profile_digest") == identity.get("profile_digest")
        and item.get("protocol")
        in {"openai-compatible", "openai-responses"}
        and isinstance(item.get("request_evidence"), list)
        and bool(item.get("request_evidence"))
        and isinstance(item.get("response_evidence"), list)
        and bool(item.get("response_evidence"))
        for item in client_bindings
    )
    coverage = comparison.get("coverage", {})
    official_inventory_digest = official.get("evidence_inventory", {}).get("digest")
    candidate_inventory_digest = candidate.get("evidence_inventory", {}).get("digest")
    gates = {
        "full_suite": manifest.get("suite") == "full",
        "official_identity_matches": official.get("identity")
        == manifest.get("official_identity"),
        "official_binary_identity_matches": _verify_sealed_official_binaries(
            official, manifest
        ),
        "candidate_identity_complete": identity_complete,
        "profile_binding_matches": bool(comparison.get("profile_binding_matches"))
        and observed_profile_receipt.get("status") == "active",
        # compare 的完整动态形态差异用于发现与复核，不能直接充当验收门禁：官方侧与候选
        # 侧的采集计划分别是 28 个任务和 7 个必需任务，完整指纹集合天然不等。行为等价性
        # 由批准断言画像推导的 42 条规则逐侧重放负责；这里仅要求 compare 本身已离线完成，
        # 其 package digest、证据 inventory 与画像绑定仍由下方独立门禁逐项校验。
        "comparison_complete": comparison.get("status") == "complete"
        and comparison.get("offline_only") is True,
        "rule_evidence_coverage": bool(official.get("results"))
        and bool(candidate.get("results"))
        and bool(coverage.get("complete")),
        "rule_assertions_complete": assertion_gate["complete"],
        "classification_unblocked": classification.get("status") == "complete"
        and classification.get("migration", {}).get("unclassified_count") == 0,
        "official_restoration_passed": bool(
            official.get("restoration", {}).get("passed")
        ),
        "candidate_restoration_passed": bool(
            candidate.get("restoration", {}).get("passed")
        ),
        "official_security_passed": official.get("security", {}).get("known_secret_scan_passed") is True,
        "candidate_security_passed": candidate.get("security", {}).get("known_secret_scan_passed") is True,
        "evidence_inventory_binding_matches": (
            comparison.get("official_evidence_inventory_digest") == official_inventory_digest
            and comparison.get("candidate_evidence_inventory_digest") == candidate_inventory_digest
        ),
        "third_party_bindings_complete": client_bindings_valid
        and _fingerprint({"items": client_bindings})
        == _fingerprint({"items": reparsed_client_bindings})
        and required_clients.issubset(observed_clients),
        "candidate_external_gate_complete": external_gate.get("phase")
        == external_gate_receipt.CANDIDATE_PHASE,
    }
    accepted = all(gates.values())
    result = {
        "schema_version": ACCEPTANCE_SCHEMA,
        "status": "complete" if accepted else "blocked",
        "accepted": accepted,
        "candidate_id": candidate_id,
        "target_version": manifest["target_version"],
        "profile_id": identity.get("profile_id"),
        "profile_digest": identity.get("profile_digest"),
        "assertions": assertion_gate,
        "gates": gates,
        "failed_gates": sorted(key for key, value in gates.items() if not value),
        "official_package_digest": official["package_digest"],
        "candidate_package_digest": candidate["package_digest"],
        "comparison_package_digest": comparison["package_digest"],
        "classification_package_digest": classification["package_digest"],
        "official_evidence_inventory_digest": official_inventory_digest,
        "candidate_evidence_inventory_digest": candidate_inventory_digest,
        "candidate_identity": {
            "source_tree_sha256": identity.get("source_tree_sha256"),
            "image_id": identity.get("image_id"),
            "image_reference": identity.get("image_reference"),
            "build_id": identity.get("build_id"),
            "deployed_version": identity.get("deployed_version"),
        },
        "candidate_external_gate": external_gate_binding,
        # 保留原始集合是否逐项相等的诊断事实，但不把不同采集计划造成的差集伪装成失败。
        "equal": bool(comparison.get("equal")),
    }
    if accepted:
        acceptance_root = ensure_private_directory(
            campaign_dir / "acceptance" / candidate_id, campaign_dir
        )
        canonical_assertions = campaign_dir / "assertions" / candidate_id / "results.json"
        if assertions_path.resolve(strict=True) != canonical_assertions.resolve(strict=False):
            if canonical_assertions.exists():
                existing_assertions = _read_json(
                    canonical_assertions, "已封存逐规则断言结果"
                )
                if _fingerprint(existing_assertions) != _fingerprint(assertions):
                    raise ConfigurationError("逐规则断言结果已经封存且内容不同。")
            else:
                secure_write_json(canonical_assertions, assertions)
        assertion_binding = {
            "path": canonical_assertions.relative_to(campaign_dir).as_posix(),
            "sha256": file_sha256(canonical_assertions),
        }
        seal_path = acceptance_root / "evidence-seal.json"
        evidence_seal = {
            "campaign_manifest_sha256": file_sha256(campaign_dir / "campaign.json"),
            "official_package_digest": official["package_digest"],
            "classification_package_digest": classification["package_digest"],
            "candidate_package_digest": candidate["package_digest"],
            "comparison_package_digest": comparison["package_digest"],
            "assertion_result": assertion_binding,
            "candidate_external_gate": external_gate_binding,
            "official_evidence_inventory_digest": official_inventory_digest,
            "candidate_evidence_inventory_digest": candidate_inventory_digest,
        }
        if seal_path.exists():
            existing_seal = _read_json(seal_path, "已封存验收证据封印")
            if _fingerprint(existing_seal) != _fingerprint(evidence_seal):
                raise ConfigurationError("验收证据封印已经存在且内容不同。")
        else:
            secure_write_json(seal_path, evidence_seal)
        result["assertion_result"] = assertion_binding
        result["evidence_seal"] = {
            "path": seal_path.relative_to(campaign_dir).as_posix(),
            "sha256": file_sha256(seal_path),
        }
        save_stage_result(campaign_dir, "accept", result, candidate_id=candidate_id)
    else:
        _write_blocked_acceptance_attempt(campaign_dir, candidate_id, result)
    return result


def _normalize_legacy_argv(argv: list[str]) -> tuple[list[str], str | None]:
    commands = {
        "plan",
        "capture-official",
        "classify",
        "prepare-profile",
        "stage-profile",
        "capture-candidate",
        "compare",
        "accept",
        "all",
        "status",
        "resume",
    }
    if argv and argv[0] in commands:
        return argv, None
    if "--dry-run" in argv:
        normalized = [value for value in argv if value != "--dry-run"]
        return ["plan", *normalized], "旧 --dry-run 已映射为 plan。"
    if "--execute" in argv:
        return argv, (
            "旧 --execute 已停用：真实抓包必须显式执行 plan、capture-official、"
            "classify、capture-candidate、compare、accept，避免自动跳过人工分类。"
        )
    return argv, None


def _resolve_classification_inputs(
    arguments: argparse.Namespace,
    manifest: dict[str, Any],
) -> None:
    if not arguments.approve_manifest_sha256:
        return
    if arguments.target_rule_manifest or arguments.migration_manifest:
        return
    version_root = (
        Path(__file__).resolve().parent
        / "versions"
        / manifest["target_version"]
    )
    candidates = {
        "target_rule_manifest": version_root / "target-rules.json",
        "migration_manifest": version_root / "rule-migration.json",
        "scenario_manifest": version_root / "scenarios.json",
        "profile_manifest": version_root / "profile.json",
        "assertion_profile_manifest": version_root / "assertion-profile.json",
    }
    missing = [str(path) for path in candidates.values() if not path.is_file()]
    if missing:
        raise ConfigurationError(
            f"批准摘要已提供，但目标版本清单不完整：{missing}"
        )
    for field, path in candidates.items():
        setattr(arguments, field, path)


def _default_assertions_path(campaign_dir: Path, candidate_id: str) -> Path:
    return campaign_dir / "assertions" / candidate_id / "results.json"


def _resume_campaign(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    status = campaign_status(arguments.campaign_dir, arguments.candidate_id)
    current = status["status"]
    if current == "environment_contaminated":
        raise ConfigurationError("环境恢复失败已封锁 Campaign，不能自动 resume。")
    if current in {
        "official_capture_interrupted",
        "candidate_capture_interrupted",
        "capture_state_inconsistent",
    }:
        raise ConfigurationError("存在孤儿预约或并发残留；只能人工审计后新建 Campaign。")
    if current == "candidate_selection_required":
        raise ConfigurationError("请先按 status 输出选择原 candidate-id，resume 不会猜测。")
    if current in {"official_capture_failed", "candidate_capture_failed"}:
        if not arguments.rerun_failed:
            raise ConfigurationError(
                "失败 attempt 只能显式使用 resume --rerun-failed 创建新 attempt。"
            )
        phase = "official" if current == "official_capture_failed" else "candidate"
        if phase == "candidate":
            required = {
                "candidate_id": arguments.candidate_id,
                "runtime_image": arguments.runtime_image,
                "build_id": arguments.build_id,
                "profile_id": arguments.profile_id,
                "profile_digest": arguments.profile_digest,
                "deployed_version": arguments.deployed_version,
            }
            missing = sorted(key for key, value in required.items() if not value)
            if missing:
                raise ConfigurationError(f"候选失败重跑缺少身份参数：{missing}")
        result = _run_capture_attempt(arguments, phase)
        return result, 0 if result.get("status") == "complete" else 2
    if current == "planned":
        result = _run_capture_attempt(arguments, "official")
        return result, 0 if result.get("status") == "complete" else 2
    if current in {"official_awaiting_receipts", "official_awaiting_seal_approval"}:
        raise ConfigurationError(
            "官方 attempt 正等待机器收据或 seal 摘要批准；resume 不会代替人工审核。"
        )
    if current == "official_sealed":
        raise ConfigurationError("下一步是人工 classify；resume 不会自动批准语义分类。")
    if current == "blocked":
        raise ConfigurationError("分类存在 blocker；解决后应以新 Campaign/revision 审核。")
    if current == "profile_approved":
        required = {
            "candidate_id": arguments.candidate_id,
            "runtime_image": arguments.runtime_image,
            "build_id": arguments.build_id,
            "profile_id": arguments.profile_id,
            "profile_digest": arguments.profile_digest,
            "deployed_version": arguments.deployed_version,
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            raise ConfigurationError(f"resume 候选抓包缺少参数：{missing}")
        result = _run_capture_attempt(arguments, "candidate")
        return result, 0 if result.get("status") == "complete" else 2
    if current in {
        "candidate_awaiting_client_checkpoint",
        "candidate_client_checkpoint_created",
        "candidate_awaiting_seal_approval",
    }:
        raise ConfigurationError(
            "候选 attempt 正等待 Kilo／机器收据或 seal 摘要批准；resume 不会代替人工审核。"
        )
    if current == "candidate_sealed":
        if not arguments.candidate_id:
            raise ConfigurationError("resume compare 必须指定 --candidate-id。")
        result = compare_campaign(arguments.campaign_dir, arguments.candidate_id)
        return result, 0 if result["equal"] else 2
    if current == "compared":
        if not arguments.candidate_id:
            raise ConfigurationError("resume accept 必须指定 --candidate-id。")
        if not arguments.external_gate_root or not arguments.external_gate_receipt:
            raise ConfigurationError(
                "resume accept 必须提供 --external-gate-root 和 "
                "--external-gate-receipt。"
            )
        assertions = arguments.assertions or _default_assertions_path(
            arguments.campaign_dir, arguments.candidate_id
        )
        result = accept_campaign(
            arguments.campaign_dir,
            arguments.candidate_id,
            assertions,
            arguments.external_gate_root,
            arguments.external_gate_receipt,
        )
        return result, 0 if result["accepted"] else 2
    return status, 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    normalized_argv, legacy_message = _normalize_legacy_argv(raw_argv)
    if "--execute" in raw_argv and not normalized_argv[:1] in (["all"],):
        print(f"升级审计失败：{legacy_message}", file=sys.stderr)
        return 1
    parser = _build_parser()
    arguments = parser.parse_args(normalized_argv)
    if legacy_message:
        print(legacy_message, file=sys.stderr)
    try:
        command = arguments.command
        if command == "plan":
            manifest = create_campaign(arguments)
            result = {
                "status": "planned",
                "campaign_id": manifest["campaign_id"],
                "campaign_dir": str(arguments.campaign_dir),
                "required_rule_count": len(manifest["required_rules"]),
                "job_count": len(manifest["jobs"]),
            }
            return_code = 0
        elif command == "capture-official":
            if arguments.capture_action == "run":
                result = _run_capture_attempt(arguments, "official")
            else:
                result = _seal_capture_attempt(arguments, "official")
            return_code = 0 if result.get("status") == "complete" else 2
        elif command == "classify":
            manifest = load_campaign_manifest(arguments.campaign_dir)
            _resolve_classification_inputs(arguments, manifest)
            result = classify_campaign(
                arguments.campaign_dir,
                target_rule_manifest=arguments.target_rule_manifest,
                migration_manifest=arguments.migration_manifest,
                scenario_manifest=arguments.scenario_manifest,
                profile_manifest=arguments.profile_manifest,
                assertion_profile_manifest=arguments.assertion_profile_manifest,
                approve_manifest_sha256=arguments.approve_manifest_sha256,
            )
            return_code = 0 if result.get("status") == "complete" else 2
        elif command == "prepare-profile":
            result = prepare_profile_manifest(
                arguments.campaign_dir,
                arguments.snapshot,
                arguments.profile_id,
                arguments.output,
            )
            return_code = 0
        elif command == "stage-profile":
            result = stage_profile_catalog(arguments.campaign_dir, arguments.output)
            return_code = 0
        elif command == "capture-candidate":
            if arguments.capture_action == "run":
                result = _run_capture_attempt(arguments, "candidate")
            else:
                result = _seal_capture_attempt(arguments, "candidate")
            return_code = 0 if result.get("status") == "complete" else 2
        elif command == "compare":
            result = compare_campaign(arguments.campaign_dir, arguments.candidate_id)
            return_code = 0 if result["equal"] else 2
        elif command == "accept":
            assertions = arguments.assertions or _default_assertions_path(
                arguments.campaign_dir, arguments.candidate_id
            )
            result = accept_campaign(
                arguments.campaign_dir,
                arguments.candidate_id,
                assertions,
                arguments.external_gate_root,
                arguments.external_gate_receipt,
            )
            return_code = 0 if result["accepted"] else 2
        elif command == "all":
            if campaign_status(
                arguments.campaign_dir, arguments.candidate_id
            )["status"] != "profile_approved":
                raise ConfigurationError("all 只允许从 profile_approved 状态开始。")
            result = _run_capture_attempt(arguments, "candidate")
            result["next_command"] = (
                "完成 Kilo witness、机器 finalizer 与 seal 摘要复核；封存后再执行 compare。"
            )
            return_code = 2
        elif command == "status":
            result = campaign_status(arguments.campaign_dir, arguments.candidate_id)
            return_code = 0
        elif command == "resume":
            result, return_code = _resume_campaign(arguments)
        else:
            raise ConfigurationError(f"不受支持的命令：{command}")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return return_code
    except KeyboardInterrupt:
        print("升级抓包已中断；当前任务已收到终止信号。", file=sys.stderr)
        return 130
    except (
        ConfigurationError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"升级审计失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
