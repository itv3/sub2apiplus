#!/usr/bin/env python3
"""编排 Codex CLI 版本升级的源码扫描、抓包、出站面比较与覆盖报告。"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import (
    ensure_private_directory,
    file_sha256,
    normalize_json_shape,
    secure_write_json,
    secure_write_text,
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
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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
    required: bool = True


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def build_default_jobs(arguments: argparse.Namespace) -> list[Job]:
    """把现有专用脚本组合成一个可审计的升级任务矩阵。"""

    tool_root = Path(__file__).resolve().parent
    capture_root = arguments.capture_root
    campaign = arguments.campaign_id
    common_environment = {
        "CAPTURE_CONTAINER": arguments.capture_container,
        "CAPTURE_ROOT": str(capture_root),
        "CODEX_VERSION": arguments.target_version,
        "CODEX_BIN": arguments.relay_codex_bin,
        "MODEL": arguments.model,
    }
    core_rules = (
        "SPEC-TLS-001",
        "SPEC-TLS-003",
        "SPEC-PROTO-001",
        "SPEC-WS-001",
        "SPEC-WS-002",
        "SPEC-WS-004",
        "SPEC-WS-005",
        "SPEC-HDR-001",
        "SPEC-HDR-002",
        "SPEC-HDR-004",
        "SPEC-HDR-005",
        "SPEC-HDR-006",
        "SPEC-HDR-007",
        "SPEC-HDR-008",
        "SPEC-BODY-001",
        "SPEC-BODY-002",
        "SPEC-BODY-003",
        "SPEC-BODY-004",
        "SPEC-BODY-005",
        "SPEC-BODY-006",
        "SPEC-EP-005",
        "SPEC-EP-006",
        "SPEC-EP-009",
        "SPEC-EP-013",
    )
    jobs: list[Job] = [
        _job(
            "official-core",
            "official",
            "官方 Codex HTTP／WS 的 direct 与 MITM 核心矩阵",
            [
                "docker",
                "exec",
                arguments.capture_container,
                "python3",
                "/capture/tools/official_client_capture/capture.py",
                "--task",
                "oauth",
                "--batch-id",
                campaign,
                "--scenarios",
                "s1,s2,s4",
                "--subjects",
                "codex-http,codex-ws",
                "--evidence",
                "direct,mitm",
                "--expected-codex-version",
                arguments.target_version,
                "--expected-codex-sha256",
                arguments.target_sha256,
                "--runtime-image",
                arguments.runtime_image,
                "--profile-version",
                f"codex-{arguments.target_version}-upgrade-v1",
                "--codex-bin",
                arguments.capture_codex_bin,
                "--execute",
                "--acknowledge-live-requests",
            ],
            {},
            [
                str(
                    capture_root
                    / "runs"
                    / "official-client"
                    / "oauth"
                    / f"oauth-{campaign}"
                )
            ],
            core_rules,
            suites=("core", "full"),
            timeout=3600,
        ),
        _job(
            "official-compact",
            "official",
            "官方 legacy compact 的 direct 与 MITM 证据",
            ["bash", str(tool_root / "run_official_codex_compact_capture.sh")],
            {
                **common_environment,
                "RUN_ID": f"{campaign}-official-compact",
                "CODEX_MODEL": arguments.model,
            },
            [str(capture_root / "runs" / f"{campaign}-official-compact")],
            (
                "SPEC-EP-007",
                "SPEC-EP-014",
                "SPEC-EP-020",
                "SPEC-EP-021",
                "SPEC-EP-023",
            ),
            suites=("core", "full"),
        ),
        _job(
            "official-http-fallback",
            "official",
            "官方 WS 失败后降级 HTTP 的原始字节",
            ["bash", str(tool_root / "run_official_http_fallback_baseline.sh")],
            {
                **common_environment,
                "RUN_ID": f"{campaign}-official-http-fallback",
            },
            [str(capture_root / "runs" / f"{campaign}-official-http-fallback")],
            ("SPEC-PROTO-002", "SPEC-H1-001", "SPEC-H1-002", "SPEC-H1-003", "SPEC-H1-004"),
            suites=("core", "full"),
        ),
        _job(
            "candidate-core-direct",
            "candidate",
            "Sub2API HTTP／WS 的 direct TLS 核心矩阵",
            ["bash", str(tool_root / "run_sub2api_direct_matrix.sh")],
            {
                "CAPTURE_CONTAINER": arguments.capture_container,
                "CAPTURE_ROOT": str(capture_root),
                "SERVICE_CONTAINER": arguments.service_container,
                "POSTGRES_CONTAINER": arguments.postgres_container,
                "CODEX_ACCOUNT_ID": str(arguments.codex_account_id),
                "API_KEY_ID": str(arguments.api_key_id),
                "SUBJECTS": "codex-http codex-ws",
                "SCENARIOS": "s1 s2 s4",
                "CODEX_MODEL": arguments.model,
                "RUN_ID": f"{campaign}-candidate-direct-core",
            },
            [str(capture_root / "runs" / f"{campaign}-candidate-direct-core")],
            core_rules,
            suites=("core", "full"),
            timeout=3600,
        ),
        _job(
            "candidate-core-mitm",
            "candidate",
            "Sub2API HTTP／WS 的应用层核心矩阵",
            ["bash", str(tool_root / "run_sub2api_openai_mitm_matrix.sh")],
            {
                "CAPTURE_CONTAINER": arguments.capture_container,
                "CAPTURE_ROOT": str(capture_root),
                "SERVICE_CONTAINER": arguments.service_container,
                "KEEPER_CONTAINER": arguments.keeper_container,
                "POSTGRES_CONTAINER": arguments.postgres_container,
                "CODEX_ACCOUNT_ID": str(arguments.codex_account_id),
                "API_KEY_ID": str(arguments.api_key_id),
                "SUBJECTS": "codex-http codex-ws",
                "SCENARIOS": "s1 s2 s4",
                "CODEX_MODEL": arguments.model,
                "RUN_ID_PREFIX": f"{campaign}-candidate-mitm-core",
                "WINDOW_ID": "run",
            },
            [str(capture_root / "runs" / f"{campaign}-candidate-mitm-core-*-run")],
            core_rules,
            suites=("core", "full"),
            timeout=3600,
        ),
        _job(
            "candidate-h1-wire",
            "candidate",
            "Sub2API HTTP/1.1 原始 header 线序",
            ["bash", str(tool_root / "run_h1_wire_probe.sh")],
            {
                "CAPTURE_CONTAINER": arguments.capture_container,
                "CAPTURE_ROOT": str(capture_root),
                "SERVICE_CONTAINER": arguments.service_container,
                "KEEPER_CONTAINER": arguments.keeper_container,
                "POSTGRES_CONTAINER": arguments.postgres_container,
                "ACCOUNT_ID": str(arguments.codex_account_id),
                "API_KEY_ID": str(arguments.api_key_id),
                "MODEL": arguments.model,
                "RUN_ID": f"{campaign}-candidate-h1",
            },
            [str(capture_root / "runs" / f"{campaign}-candidate-h1")],
            ("SPEC-H1-001", "SPEC-H1-002", "SPEC-H1-003", "SPEC-H1-004"),
            suites=("core", "full"),
        ),
        _job(
            "candidate-compact-direct",
            "candidate",
            "Sub2API compact 的 direct TLS 证据",
            ["bash", str(tool_root / "run_sub2api_direct_matrix.sh")],
            {
                "CAPTURE_CONTAINER": arguments.capture_container,
                "CAPTURE_ROOT": str(capture_root),
                "SERVICE_CONTAINER": arguments.service_container,
                "POSTGRES_CONTAINER": arguments.postgres_container,
                "CODEX_ACCOUNT_ID": str(arguments.codex_account_id),
                "API_KEY_ID": str(arguments.api_key_id),
                "SUBJECTS": "codex-compact",
                "SCENARIOS": "compact",
                "CODEX_MODEL": arguments.model,
                "CODEX_VERSION": arguments.target_version,
                "RUN_ID": f"{campaign}-candidate-direct-compact",
            },
            [str(capture_root / "runs" / f"{campaign}-candidate-direct-compact")],
            (
                "SPEC-EP-007",
                "SPEC-EP-014",
                "SPEC-EP-020",
                "SPEC-EP-021",
                "SPEC-EP-023",
            ),
            suites=("core", "full"),
        ),
        _job(
            "candidate-compact-mitm",
            "candidate",
            "Sub2API compact 的应用层证据",
            ["bash", str(tool_root / "run_sub2api_openai_mitm_matrix.sh")],
            {
                "CAPTURE_CONTAINER": arguments.capture_container,
                "CAPTURE_ROOT": str(capture_root),
                "SERVICE_CONTAINER": arguments.service_container,
                "KEEPER_CONTAINER": arguments.keeper_container,
                "POSTGRES_CONTAINER": arguments.postgres_container,
                "CODEX_ACCOUNT_ID": str(arguments.codex_account_id),
                "API_KEY_ID": str(arguments.api_key_id),
                "SUBJECTS": "codex-compact",
                "SCENARIOS": "compact",
                "CODEX_MODEL": arguments.model,
                "CODEX_VERSION": arguments.target_version,
                "RUN_ID_PREFIX": f"{campaign}-candidate-mitm-compact",
                "WINDOW_ID": "run",
            },
            [str(capture_root / "runs" / f"{campaign}-candidate-mitm-compact-*-run")],
            (
                "SPEC-EP-007",
                "SPEC-EP-014",
                "SPEC-EP-020",
                "SPEC-EP-021",
                "SPEC-EP-023",
            ),
            suites=("core", "full"),
        ),
    ]

    relay_scenarios = (
        (
            "http-response",
            ("SPEC-H1-001", "SPEC-H1-002", "SPEC-H1-003", "SPEC-H1-004"),
            {},
        ),
        (
            "conn-retry",
            ("SPEC-CONN-001", "SPEC-PROTO-002"),
            {
                "RELAY_FORCE_WS_FALLBACK_426": "1",
                "RELAY_RETRY_PROBE": "keepalive-500",
                "RELAY_RETRY_PROBE_TARGET": "responses",
            },
        ),
        ("turnstate-compact", ("SPEC-BODY-004", "SPEC-EP-007", "SPEC-EP-014"), {"RELAY_INJECT_TURN_STATE": "upgrade-turn-state"}),
        ("residency-us", ("SPEC-HDR-002",), {}),
        ("image", ("SPEC-EP-001", "SPEC-EP-022", "SPEC-BODY-006"), {}),
        ("image-edit", ("SPEC-EP-001", "SPEC-EP-022", "SPEC-BODY-006"), {}),
        ("search", ("SPEC-EP-008", "SPEC-EP-015"), {}),
        (
            "realtime-webrtc",
            ("SPEC-EP-009", "SPEC-EP-012"),
            {"RELAY_HOSTS": "chatgpt.com api.openai.com"},
        ),
        ("runtime-metrics", ("SPEC-HDR-008",), {}),
        ("compact", ("SPEC-EP-021", "SPEC-EP-023"), {}),
        ("comp-hash-changed", ("SPEC-EP-023",), {}),
        ("model-downshift", ("SPEC-EP-023",), {}),
        (
            "file-upload",
            ("SPEC-EP-002",),
            {
                "DISABLE_FEATURES": "plugins",
                "RELAY_HOSTS": (
                    "chatgpt.com auth.openai.com api.openai.com "
                    "sdmntprwestus3.oaiusercontent.com"
                ),
            },
        ),
    )
    for scenario, covers, extra_environment in relay_scenarios:
        run_id = f"{campaign}-official-{scenario}"
        jobs.append(
            _job(
                f"official-relay-{scenario}",
                "official",
                f"官方复杂状态链：{scenario}",
                ["bash", str(tool_root / "run_official_relay_scenario.sh")],
                {
                    **common_environment,
                    **extra_environment,
                    "RUN_ID": run_id,
                    "SCENARIO": scenario,
                },
                [str(capture_root / "runs" / run_id)],
                covers,
                timeout=1800 if scenario not in {"compact", "comp-hash-changed", "model-downshift"} else 5400,
            )
        )
    jobs.append(
        _job(
            "candidate-images-wire",
            "candidate",
            "Sub2API images 端点的 HTTP/1.1 原始字节",
            ["bash", str(tool_root / "run_images_wire_probe.sh")],
            {
                "CAPTURE_CONTAINER": arguments.capture_container,
                "CAPTURE_ROOT": str(capture_root),
                "SERVICE_CONTAINER": arguments.service_container,
                "KEEPER_CONTAINER": arguments.keeper_container,
                "POSTGRES_CONTAINER": arguments.postgres_container,
                "ACCOUNT_ID": str(arguments.codex_account_id),
                "API_KEY_ID": str(arguments.api_key_id),
                "MODEL": arguments.model,
                "RUN_ID": f"{campaign}-candidate-images",
            },
            [str(capture_root / "runs" / f"{campaign}-candidate-images")],
            ("SPEC-EP-001", "SPEC-EP-022", "SPEC-BODY-006"),
        )
    )
    return jobs


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


def run_job(job: Job, log_root: Path) -> dict[str, Any]:
    """顺序执行任务步骤，并保留不含命令环境值的日志。"""

    started = time.time()
    step_results: list[dict[str, Any]] = []
    for index, step in enumerate(job.steps, 1):
        log_path = log_root / f"{job.job_id}-{index}.log"
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
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                _terminate_process(process)
                raise
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
    roots = _expand_roots(job.evidence_roots)
    existing_roots = [root for root in roots if root.exists()]
    steps_ok = len(step_results) == len(job.steps) and all(
        item["return_code"] == 0 for item in step_results
    )
    status = "complete" if steps_ok and existing_roots else "failed"
    return {
        "id": job.job_id,
        "phase": job.phase,
        "required": job.required,
        "status": status,
        "description": job.description,
        "duration_seconds": round(time.time() - started, 3),
        "steps": step_results,
        "evidence_roots": [str(root) for root in existing_roots],
        "missing_evidence_patterns": [
            pattern
            for pattern in job.evidence_roots
            if not glob.glob(pattern) and not Path(pattern).exists()
        ],
        "covers": list(job.covers),
    }


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
        raise ConfigurationError("--output 必须是非符号链接的绝对路径。")
    resolved = path.resolve(strict=False)
    forbidden = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path("/tmp").resolve(),
    }
    if resolved in forbidden:
        raise ConfigurationError("--output 不能是根目录、HOME 或 /tmp 本身。")
    if path.exists():
        raise ConfigurationError("--output 已存在；升级运行目录必须一次性使用。")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只输出计划，不写盘、不抓包")
    mode.add_argument("--execute", action="store_true", help="执行真实抓包与分析")
    parser.add_argument("--acknowledge-live-requests", action="store_true")
    parser.add_argument("--baseline-version", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--target-source", type=Path, required=True)
    parser.add_argument("--baseline-evidence", type=Path, required=True)
    parser.add_argument("--target-sha256", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--rule-manifest",
        type=Path,
        default=Path(__file__).with_name("codex_upgrade_rules_0_145_0.json"),
    )
    parser.add_argument("--extra-jobs", type=Path)
    parser.add_argument("--suite", choices=("core", "full"), default="full")
    parser.add_argument(
        "--campaign-id",
        default="",
        help="留空时按目标版本和 UTC 时间生成。",
    )
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--capture-root", type=Path, default=Path("/root/oauth-capture"))
    parser.add_argument("--capture-container", default="capture-cli")
    parser.add_argument("--service-container", default="sub2apiplus")
    parser.add_argument("--keeper-container", default="sub2apiplus-keeper")
    parser.add_argument("--postgres-container", default="sub2apiplus-postgres")
    parser.add_argument("--capture-codex-bin", default="/usr/local/bin/codex-capture")
    parser.add_argument("--relay-codex-bin", default="/root/.local/bin/codex")
    parser.add_argument("--codex-account-id", type=int, default=90)
    parser.add_argument("--api-key-id", type=int, default=1)
    return parser


def _validate_arguments(arguments: argparse.Namespace) -> None:
    for field, value in (
        ("--baseline-version", arguments.baseline_version),
        ("--target-version", arguments.target_version),
    ):
        if not VERSION_RE.fullmatch(value):
            raise ConfigurationError(f"{field} 必须是三段版本号。")
    if not SHA256_RE.fullmatch(arguments.target_sha256):
        raise ConfigurationError("--target-sha256 必须是 64 位小写 SHA-256。")
    if "sha256:" not in arguments.runtime_image:
        raise ConfigurationError("--runtime-image 必须包含镜像 digest。")
    if arguments.execute and not arguments.acknowledge_live_requests:
        raise ConfigurationError(
            "--execute 会产生真实请求，必须同时确认 --acknowledge-live-requests。"
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
    _validate_output_path(arguments.output)


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
            }
            for job in jobs
        ],
    }


def main() -> int:
    os.umask(0o077)
    parser = _build_parser()
    arguments = parser.parse_args()
    try:
        _validate_arguments(arguments)
        rules = load_rule_manifest(arguments.rule_manifest, arguments.baseline_version)
        context = {
            "baseline_version": arguments.baseline_version,
            "target_version": arguments.target_version,
            "campaign_id": arguments.campaign_id,
            "capture_root": str(arguments.capture_root),
            "output": str(arguments.output),
            "repo_root": str(Path(__file__).resolve().parents[2]),
            "model": arguments.model,
        }
        jobs = build_default_jobs(arguments)
        jobs.extend(load_extra_jobs(arguments.extra_jobs, context))
        jobs = [job for job in jobs if arguments.suite in job.suites]
        duplicate_jobs = sorted(
            {job.job_id for job in jobs if sum(item.job_id == job.job_id for item in jobs) > 1}
        )
        if duplicate_jobs:
            raise ConfigurationError(f"任务 ID 重复：{duplicate_jobs}")
        unknown_rules = sorted(
            {rule for job in jobs for rule in job.covers} - set(rules)
        )
        if unknown_rules:
            raise ConfigurationError(f"任务引用规则清单外编号：{unknown_rules}")
        if arguments.dry_run:
            print(
                json.dumps(
                    _safe_plan(arguments, jobs, rules),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        ensure_private_directory(arguments.output)
        log_root = ensure_private_directory(arguments.output / "logs", arguments.output)
        analysis_root = ensure_private_directory(
            arguments.output / "analysis", arguments.output
        )
        manifest = _safe_plan(arguments, jobs, rules)
        manifest["mode"] = "execute"
        secure_write_json(arguments.output / "manifest.json", manifest)

        baseline_source = scan_source_tree(
            arguments.baseline_source, arguments.baseline_version
        )
        target_source = scan_source_tree(
            arguments.target_source, arguments.target_version
        )
        source_diff = compare_inventory(baseline_source, target_source)
        secure_write_json(analysis_root / "baseline-source.json", baseline_source)
        secure_write_json(analysis_root / "target-source.json", target_source)
        secure_write_json(analysis_root / "source-diff.json", source_diff)

        results: list[dict[str, Any]] = []
        for job in jobs:
            result = run_job(job, log_root)
            results.append(result)
            secure_write_json(
                analysis_root / f"job-{job.job_id}.json",
                result,
            )
            failed_step_codes = [
                step["return_code"]
                for step in result.get("steps", [])
                if step["return_code"] != 0
            ]
            if 97 in failed_step_codes:
                raise RuntimeError(
                    f"{job.job_id} 环境恢复失败，停止后续抓包。"
                )

        official_paths = [
            Path(root)
            for result in results
            if result["phase"] == "official" and result["status"] == "complete"
            for root in result["evidence_roots"]
        ]
        candidate_paths = [
            Path(root)
            for result in results
            if result["phase"] == "candidate" and result["status"] == "complete"
            for root in result["evidence_roots"]
        ]
        baseline_surface = scan_evidence(
            [arguments.baseline_evidence], "baseline-official"
        )
        official_surface = scan_evidence(official_paths, "target-official")
        candidate_surface = scan_evidence(candidate_paths, "target-sub2api")
        baseline_to_official = compare_surfaces(
            baseline_surface, official_surface
        )
        official_to_candidate = compare_surfaces(
            official_surface, candidate_surface
        )
        coverage = build_coverage(rules, jobs, results)
        for name, value in (
            ("baseline-surface.json", baseline_surface),
            ("official-surface.json", official_surface),
            ("candidate-surface.json", candidate_surface),
            ("baseline-to-official.json", baseline_to_official),
            ("official-to-candidate.json", official_to_candidate),
            ("coverage.json", coverage),
        ):
            secure_write_json(analysis_root / name, value)

        required_jobs_ok = all(
            result["status"] == "complete"
            for result in results
            if result["required"]
        )
        no_new_candidates = (
            source_diff["added_count"] == 0
            and baseline_to_official["added_count"] == 0
        )
        comparison_equal = official_to_candidate["equal"]
        status = (
            "ready"
            if required_jobs_ok
            and coverage["complete"]
            and no_new_candidates
            and comparison_equal
            else "review_required"
        )
        report = {
            "schema_version": REPORT_SCHEMA,
            "campaign_id": arguments.campaign_id,
            "baseline_version": arguments.baseline_version,
            "target_version": arguments.target_version,
            "status": status,
            "jobs": results,
            "source_diff": source_diff,
            "baseline_to_target_official": baseline_to_official,
            "official_to_candidate": official_to_candidate,
            "coverage": coverage,
            "decision": {
                "required_jobs_complete": required_jobs_ok,
                "rule_evidence_complete": coverage["complete"],
                "new_candidates_classified": no_new_candidates,
                "official_candidate_surface_equal": comparison_equal,
                "note": (
                    "ready 只表示自动门禁通过；规则语义变化仍须写入目标版本规格。"
                ),
            },
        }
        secure_write_json(arguments.output / "report.json", report)
        secure_write_text(arguments.output / "report.md", _render_report(report))
        print(json.dumps({
            "status": status,
            "output": str(arguments.output),
            "jobs_complete": sum(item["status"] == "complete" for item in results),
            "jobs_total": len(results),
            "rule_evidence": (
                f"{coverage['evidence_complete_count']}/"
                f"{coverage['required_rule_count']}"
            ),
            "new_source_candidates": source_diff["added_count"],
            "new_dynamic_candidates": baseline_to_official["added_count"],
        }, ensure_ascii=False, indent=2))
        return 0 if status == "ready" else 2
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
