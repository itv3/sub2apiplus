#!/usr/bin/env python3
"""从 FW-F 受批 Snapshot 与 2.1.226 原生证据生成 FW-G 内容寻址制品。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.official_client_capture.claude_fw_f_profile import parse_http_stream


EXPECTED_PROFILE_DIGEST = (
    "4da60bc238694a06a0dc80d68117abddd2de98c7c924c4db4c5dd929ea411e17"
)
EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST = (
    "50261962778b8a7cf85f2dd01a8057f8004e92c0978456e88d9457d4ef8030b3"
)
EXPECTED_ATOMIC_LEDGER_DIGEST = (
    "20e3c02365893db0af7164acd8300b658f653b29c2340acedfe0abf6973a4200"
)
EXPECTED_SNAPSHOT_DIGEST = (
    "935975161e6b325f2a6459924f9d4bfa59dc9430fcd29e32475694ad0279e79e"
)
EXPECTED_EVIDENCE_PACKAGE_DIGEST = (
    "4979fd6608e285a5735c416775f5ba48a576e00e26e55ebfbc8641f404606971"
)
EXPECTED_SUPPORT_ENVELOPE_DIGEST = (
    "f4954a6889f06c782f9d2de6837a82678cbc068ec55002a1fb4c2e3e30324f17"
)
EXPECTED_RULE_COUNT = 40
EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT = 106
EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT = 110
EXPECTED_VERSION = "2.1.226"
EXPECTED_MESSAGES_EVIDENCE_DIGEST = (
    "ca039dfcc431f7980bade1d06302da3703c87c350eac504b7b89161a87e34097"
)
EXPECTED_MESSAGES_BODY_DIGEST = (
    "0b82aa1ffd4ddd65a7c79101efb340a3c95de3977a4a41ace68b980ec8785063"
)
EXPECTED_TLS_PCAP_DIGEST = (
    "f5cd8ea0d20b2db2049822462bb9b8b2322eb4e5954e5c91e15aebb83646fab3"
)
EXPECTED_SYSTEM_TEXT_DIGESTS = [
    "0d7062851dd7bd7e66d4be4f12ac4951e3d2f587ec408295333a49963bd3f6b7",
    "6ed0608ab0f8a2e5966a72e22ffe1fc04a1ff6d5e220393c1ef30b7c9e336b30",
    "51e2fcae7a92f7d7ac83e42b2952bb0fa04e4c30eb31afc775c4f82314524321",
]
EXPECTED_CIPHERS = [
    4865,
    4866,
    4867,
    49195,
    49199,
    49196,
    49200,
    52393,
    52392,
    49161,
    49171,
    49162,
    49172,
    156,
    157,
    47,
    53,
]
EXPECTED_GROUPS = [29, 23, 24]
EXPECTED_POINT_FORMATS = [0]
EXPECTED_SIGNATURE_ALGORITHMS = [1027, 2052, 1025, 1283, 2053, 1281, 2054, 1537, 513]
EXPECTED_SUPPORTED_VERSIONS = [772, 771]
EXPECTED_KEY_SHARE_GROUPS = [29]
EXPECTED_PSK_MODES = [1]
EXPECTED_WITH_ALPN_EXTENSIONS = [0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43, 21]
EXPECTED_WITHOUT_ALPN_EXTENSIONS = [0, 23, 65281, 10, 11, 35, 13, 51, 45, 43]


def canonical_bytes(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_frozen_json(path: Path, expected_digest: str, label: str) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = sha256_hex(raw)
    if digest != expected_digest:
        raise SystemExit(f"{label} 摘要不一致：{digest}")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise SystemExit(f"{label} 不是 JSON 对象")
    return document


def required_rule_mapping(rules: Any, label: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(rules, list) or len(rules) != EXPECTED_RULE_COUNT:
        raise SystemExit(f"{label} RequiredRules 数量不是 40")
    result: dict[str, tuple[str, ...]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            raise SystemExit(f"{label} RequiredRule 不是对象")
        spec_id = rule.get("spec_id")
        atomic_ids = rule.get("atomic_assertion_ids")
        if (
            not isinstance(spec_id, str)
            or not spec_id.startswith("SPEC-")
            or not isinstance(atomic_ids, list)
            or not atomic_ids
            or any(not isinstance(item, str) or not item.startswith("SPEC-") for item in atomic_ids)
            or spec_id in result
        ):
            raise SystemExit(f"{label} RequiredRule 或原子映射非法：{spec_id}")
        result[spec_id] = tuple(atomic_ids)
    return result


def validate_fw_f_rule_authorities(
    snapshot: dict[str, Any],
    manifest: dict[str, Any],
    atomic_ledger: dict[str, Any],
    evidence_package: dict[str, Any],
    support_envelope: dict[str, Any],
) -> dict[str, Any]:
    try:
        profile_document = snapshot["payload"]["document"]
        evidence_rules = evidence_package["payload"]["rules"]
        support_spec_ids = support_envelope["payload"]["target_spec_ids"]
    except (KeyError, TypeError) as exc:
        raise SystemExit("FW-F Snapshot／EvidencePackage／SupportEnvelope 结构不完整") from exc
    if not isinstance(profile_document, dict):
        raise SystemExit("FW-F Snapshot profile document 非法")

    manifest_mapping = required_rule_mapping(manifest.get("required_rules"), "manifest")
    snapshot_mapping = required_rule_mapping(profile_document.get("rules"), "Snapshot")
    if manifest_mapping != snapshot_mapping:
        raise SystemExit("RequiredRules manifest 与 Snapshot 的规则／原子映射不一致")

    manifest_profile_atomic = [
        assertion_id
        for assertion_ids in manifest_mapping.values()
        for assertion_id in assertion_ids
    ]
    if (
        len(manifest_profile_atomic) != EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT
        or len(set(manifest_profile_atomic)) != EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT
    ):
        raise SystemExit("106 条画像原子断言未恰好一次归属 RequiredRules")

    scenario_groups = manifest.get("scenario_only_groups")
    if not isinstance(scenario_groups, list):
        raise SystemExit("RequiredRules manifest 缺少 scenario-only 分组")
    scenario_atomic = [
        assertion_id
        for group in scenario_groups
        if isinstance(group, dict)
        for assertion_id in group.get("atomic_assertion_ids", [])
    ]
    all_manifest_atomic = manifest_profile_atomic + scenario_atomic
    ledger_entries = atomic_ledger.get("entries")
    if not isinstance(ledger_entries, list):
        raise SystemExit("AtomicAssertionLedger entries 非法")
    ledger_atomic = [entry.get("spec_id") for entry in ledger_entries if isinstance(entry, dict)]
    if (
        len(all_manifest_atomic) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or len(set(all_manifest_atomic)) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or len(ledger_atomic) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or set(ledger_atomic) != set(all_manifest_atomic)
    ):
        raise SystemExit("110 条 AtomicAssertionLedger 未与画像／scenario-only 映射完全一致")

    evidence_spec_ids = [
        rule.get("spec_id") for rule in evidence_rules if isinstance(rule, dict)
    ]
    required_spec_ids = set(manifest_mapping)
    if (
        len(evidence_spec_ids) != EXPECTED_RULE_COUNT
        or len(set(evidence_spec_ids)) != EXPECTED_RULE_COUNT
        or set(evidence_spec_ids) != required_spec_ids
        or not isinstance(support_spec_ids, list)
        or len(support_spec_ids) != EXPECTED_RULE_COUNT
        or len(set(support_spec_ids)) != EXPECTED_RULE_COUNT
        or set(support_spec_ids) != required_spec_ids
    ):
        raise SystemExit("EvidencePackage／SupportEnvelope RequiredRule 集合不一致")

    strict_endpoints = profile_document.get("strict_endpoints")
    if not isinstance(strict_endpoints, list) or len(strict_endpoints) != 8:
        raise SystemExit("Snapshot strict endpoint 数量不是 8")
    endpoint_spec_ids: list[str] = []
    for endpoint in strict_endpoints:
        spec_ids = endpoint.get("spec_ids") if isinstance(endpoint, dict) else None
        if (
            not isinstance(spec_ids, list)
            or not spec_ids
            or len(spec_ids) != len(set(spec_ids))
            or not set(spec_ids).issubset(required_spec_ids)
        ):
            raise SystemExit("Snapshot strict endpoint 规则引用非法")
        endpoint_spec_ids.extend(spec_ids)
    if set(endpoint_spec_ids) != required_spec_ids:
        raise SystemExit("8 个 strict endpoint 的 SPEC 并集未覆盖 40 条 RequiredRules")
    return profile_document


def wire_json(value: Any) -> str:
    """保留官方请求中对象字段的插入顺序，供 Go 编译器逐字节重放。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_u16(raw: bytes, offset: int) -> tuple[int, int]:
    if offset + 2 > len(raw):
        raise ValueError("ClientHello 的 uint16 越界")
    return struct.unpack_from(">H", raw, offset)[0], offset + 2


def read_vector_u16(raw: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = read_u16(raw, offset)
    end = offset + length
    if end > len(raw):
        raise ValueError("ClientHello 的 uint16 vector 越界")
    return raw[offset:end], end


def parse_u16_list(raw: bytes) -> list[int]:
    if len(raw) % 2 != 0:
        raise ValueError("ClientHello 的 uint16 列表长度非法")
    return list(struct.unpack(f">{len(raw) // 2}H", raw))


def parse_client_hello(record: bytes) -> dict[str, Any]:
    if len(record) < 9 or record[0] != 22 or record[5] != 1:
        raise ValueError("不是 TLS ClientHello record")
    record_length = struct.unpack_from(">H", record, 3)[0]
    if len(record) != record_length + 5:
        raise ValueError("TLS record 长度不一致")
    handshake_length = int.from_bytes(record[6:9], "big")
    if handshake_length + 4 != record_length:
        raise ValueError("ClientHello handshake 长度不一致")
    body = record[9:]
    if len(body) < 35:
        raise ValueError("ClientHello body 过短")
    offset = 34
    session_length = body[offset]
    offset += 1 + session_length
    cipher_raw, offset = read_vector_u16(body, offset)
    if offset >= len(body):
        raise ValueError("ClientHello 缺少 compression methods")
    compression_length = body[offset]
    offset += 1 + compression_length
    extensions_raw, offset = read_vector_u16(body, offset)
    if offset != len(body):
        raise ValueError("ClientHello extensions 后有多余字节")

    extensions: list[int] = []
    extension_values: dict[int, bytes] = {}
    ext_offset = 0
    while ext_offset < len(extensions_raw):
        ext_id, ext_offset = read_u16(extensions_raw, ext_offset)
        ext_value, ext_offset = read_vector_u16(extensions_raw, ext_offset)
        extensions.append(ext_id)
        extension_values[ext_id] = ext_value

    sni = ""
    if 0 in extension_values:
        names, _ = read_vector_u16(extension_values[0], 0)
        if len(names) < 3 or names[0] != 0:
            raise ValueError("ClientHello SNI 结构非法")
        name_length = struct.unpack_from(">H", names, 1)[0]
        sni = names[3 : 3 + name_length].decode("ascii")

    groups: list[int] = []
    if 10 in extension_values:
        group_raw, _ = read_vector_u16(extension_values[10], 0)
        groups = parse_u16_list(group_raw)

    point_formats: list[int] = []
    if 11 in extension_values:
        value = extension_values[11]
        if not value or value[0] + 1 != len(value):
            raise ValueError("ClientHello point formats 结构非法")
        point_formats = list(value[1:])

    signature_algorithms: list[int] = []
    if 13 in extension_values:
        signature_raw, _ = read_vector_u16(extension_values[13], 0)
        signature_algorithms = parse_u16_list(signature_raw)

    alpn: list[str] = []
    if 16 in extension_values:
        names, _ = read_vector_u16(extension_values[16], 0)
        name_offset = 0
        while name_offset < len(names):
            name_length = names[name_offset]
            name_offset += 1
            alpn.append(names[name_offset : name_offset + name_length].decode("ascii"))
            name_offset += name_length

    supported_versions: list[int] = []
    if 43 in extension_values:
        value = extension_values[43]
        if not value or value[0] + 1 != len(value):
            raise ValueError("ClientHello supported versions 结构非法")
        supported_versions = parse_u16_list(value[1:])

    key_share_groups: list[int] = []
    if 51 in extension_values:
        shares, _ = read_vector_u16(extension_values[51], 0)
        share_offset = 0
        while share_offset < len(shares):
            group, share_offset = read_u16(shares, share_offset)
            key, share_offset = read_vector_u16(shares, share_offset)
            if not key:
                raise ValueError("ClientHello key share 为空")
            key_share_groups.append(group)

    psk_modes: list[int] = []
    if 45 in extension_values:
        value = extension_values[45]
        if not value or value[0] + 1 != len(value):
            raise ValueError("ClientHello PSK modes 结构非法")
        psk_modes = list(value[1:])

    return {
        "cipher_suites": parse_u16_list(cipher_raw),
        "supported_groups": groups,
        "point_formats": point_formats,
        "signature_algorithms": signature_algorithms,
        "alpn": alpn,
        "extensions": extensions,
        "supported_versions": supported_versions,
        "key_share_groups": key_share_groups,
        "psk_modes": psk_modes,
        "sni": sni,
        "tls_min_version": 771,
        "tls_max_version": 772,
    }


def extract_client_hellos(pcap: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0
    while offset + 9 <= len(pcap):
        hit = pcap.find(b"\x16\x03\x01", offset)
        if hit < 0:
            break
        record_length = struct.unpack_from(">H", pcap, hit + 3)[0]
        end = hit + 5 + record_length
        if end <= len(pcap):
            try:
                parsed = parse_client_hello(pcap[hit:end])
            except (UnicodeDecodeError, ValueError):
                pass
            else:
                if parsed["sni"] == "api.anthropic.com":
                    records.append(parsed)
                    offset = end
                    continue
        offset = hit + 1
    return records


def validate_tls_vector(vector: dict[str, Any], with_alpn: bool) -> None:
    expected = {
        "cipher_suites": EXPECTED_CIPHERS,
        "supported_groups": EXPECTED_GROUPS,
        "point_formats": EXPECTED_POINT_FORMATS,
        "signature_algorithms": EXPECTED_SIGNATURE_ALGORITHMS,
        "supported_versions": EXPECTED_SUPPORTED_VERSIONS,
        "key_share_groups": EXPECTED_KEY_SHARE_GROUPS,
        "psk_modes": EXPECTED_PSK_MODES,
        "sni": "api.anthropic.com",
        "tls_min_version": 771,
        "tls_max_version": 772,
    }
    for name, value in expected.items():
        if vector.get(name) != value:
            raise SystemExit(f"ClientHello {name} 与 2.1.226 P 通道不一致")
    expected_alpn = ["http/1.1"] if with_alpn else []
    expected_extensions = (
        EXPECTED_WITH_ALPN_EXTENSIONS if with_alpn else EXPECTED_WITHOUT_ALPN_EXTENSIONS
    )
    if vector["alpn"] != expected_alpn or vector["extensions"] != expected_extensions:
        raise SystemExit("ClientHello ALPN／扩展序与 2.1.226 P 通道不一致")


def collect_measured_raw_sha256s(ledger_path: Path) -> set[str]:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if ledger.get("target_version") != EXPECTED_VERSION or ledger.get("rule_count") != 110:
        raise SystemExit("MeasuredRuleLedger 身份或原子断言数量不一致")
    raw_sha256s: set[str] = set()
    for entry in ledger.get("entries", []):
        for reference in entry.get("evidence_refs", []):
            raw_sha256s.update(reference.get("raw_request_sha256s", []))
    if not raw_sha256s:
        raise SystemExit("MeasuredRuleLedger 没有 R 通道请求摘要")
    return raw_sha256s


def load_scenario_messages(
    campaign_root: Path,
    scenario: str,
    allowed_raw_sha256s: set[str],
) -> list[dict[str, Any]]:
    relay_root = campaign_root / "attempts" / scenario / "attempt-001" / "relay"
    requests: list[dict[str, Any]] = []
    for path in sorted(relay_root.glob("conn*.client_to_upstream.bin")):
        for request in parse_http_stream(path.read_bytes(), scenario):
            if request.get("request_target") != "/v1/messages?beta=true":
                continue
            raw_sha256 = request.get("raw_sha256", "")
            if raw_sha256 not in allowed_raw_sha256s:
                raise SystemExit(f"{scenario} 请求未绑定 MeasuredRuleLedger：{raw_sha256}")
            request = dict(request)
            request["evidence_path"] = path.as_posix()
            requests.append(request)
    if not requests:
        raise SystemExit(f"{scenario} 没有 messages R 通道")
    return requests


def header_value(request: dict[str, Any], name: str) -> str:
    target = name.lower()
    for header in request.get("headers", []):
        if str(header.get("name", "")).lower() == target:
            return str(header.get("value", ""))
    return ""


def public_system_blocks(request: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = request.get("body", {}).get("system", [])
    if not isinstance(blocks, list) or len(blocks) < 1:
        raise SystemExit("场景请求缺少 Persona system blocks")
    return blocks[1:]


def scenario_projection(request: dict[str, Any]) -> dict[str, Any]:
    body = request.get("body", {})
    return {
        "evidence": {
            "path": request["evidence_path"],
            "raw_sha256": request["raw_sha256"],
        },
        "model": body.get("model"),
        # 制品本身按 canonical JSON 内容寻址，会递归重排对象键。以下字段必须
        # 另存原 wire JSON 字符串，否则 context_management、tools 等对象的键序
        # 会在生成制品时被破坏，FW-G 无法做逐字节 PAIR。
        "max_tokens_json": wire_json(body.get("max_tokens")),
        "thinking_json": wire_json(body.get("thinking")),
        "context_management_json": wire_json(body.get("context_management")),
        "output_config_json": wire_json(body.get("output_config")),
        "temperature_json": wire_json(body.get("temperature")),
        "thinking_present": "thinking" in body,
        "context_management_present": "context_management" in body,
        "output_config_present": "output_config" in body,
        "temperature_present": "temperature" in body,
        "stream_present": "stream" in body,
        "stream_json": wire_json(body.get("stream")),
        "tools_present": "tools" in body,
        "tools_json": wire_json(body.get("tools")),
        "system_present": "system" in body,
        "system_blocks_json": wire_json(public_system_blocks(request)),
        "metadata_present": "metadata" in body,
        "user_agent": header_value(request, "User-Agent"),
        "anthropic_beta": header_value(request, "anthropic-beta"),
        "x_app": header_value(request, "x-app"),
    }


def select_messages_request(
    campaign_root: Path,
    scenario: str,
    allowed_raw_sha256s: set[str],
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    requests = load_scenario_messages(campaign_root, scenario, allowed_raw_sha256s)
    selected = next((request for request in requests if predicate(request)), None)
    if selected is None:
        raise SystemExit(f"{scenario} 没有 {description} 请求")
    return selected


def tool_policy_projection(request: dict[str, Any]) -> dict[str, Any]:
    body = request.get("body", {})
    tools = body.get("tools")
    if not isinstance(tools, list):
        raise SystemExit("工具策略证据的 tools 不是数组")
    return {
        "evidence": {
            "path": request["evidence_path"],
            "raw_sha256": request["raw_sha256"],
        },
        "tools_json": wire_json(tools),
        "tool_choice_json": wire_json(body.get("tool_choice")),
        "anthropic_beta": header_value(request, "anthropic-beta"),
    }


def build_tool_policy(
    campaign_root: Path,
    allowed_raw_sha256s: set[str],
) -> dict[str, Any]:
    structured = select_messages_request(
        campaign_root,
        "v4-replay-json-schema",
        allowed_raw_sha256s,
        lambda request: any(
            isinstance(tool, dict) and tool.get("name") == "StructuredOutput"
            for tool in request.get("body", {}).get("tools", [])
        ),
        "StructuredOutput",
    )
    agent = select_messages_request(
        campaign_root,
        "v4-agent-depth1",
        allowed_raw_sha256s,
        lambda request: any(
            isinstance(tool, dict) and tool.get("name") == "Agent"
            for tool in request.get("body", {}).get("tools", [])
        ),
        "Agent",
    )
    bash = select_messages_request(
        campaign_root,
        "v4-bash",
        allowed_raw_sha256s,
        lambda request: any(
            isinstance(tool, dict) and tool.get("name") == "Bash"
            for tool in request.get("body", {}).get("tools", [])
        ),
        "Bash",
    )
    mcp = select_messages_request(
        campaign_root,
        "v4-mcp-deferred",
        allowed_raw_sha256s,
        lambda request: len(request.get("body", {}).get("tools", [])) == 33,
        "33 项 deferred MCP 目录",
    )
    advisor = select_messages_request(
        campaign_root,
        "v4-advisor-enabled-positive",
        allowed_raw_sha256s,
        lambda request: any(
            isinstance(tool, dict) and tool.get("type") == "advisor_20260301"
            for tool in request.get("body", {}).get("tools", [])
        ),
        "advisor",
    )
    background = select_messages_request(
        campaign_root,
        "v4-background",
        allowed_raw_sha256s,
        lambda request: len(request.get("body", {}).get("tools", [])) == 12,
        "12 项 background 目录",
    )
    web_search_requests = load_scenario_messages(
        campaign_root, "v4-web-search", allowed_raw_sha256s
    )
    web_search_outer = next(
        (
            request
            for request in web_search_requests
            if request.get("body", {}).get("tools", [{}])[0].get("name")
            == "WebSearch"
            and "tool_choice" not in request.get("body", {})
        ),
        None,
    )
    web_search_server = next(
        (
            request
            for request in web_search_requests
            if request.get("body", {}).get("tools", [{}])[0].get("name")
            == "web_search"
            and "tool_choice" in request.get("body", {})
        ),
        None,
    )
    if web_search_outer is None or web_search_server is None:
        raise SystemExit("v4-web-search 没有形成外层与 server 派生请求")
    return {
        "schema_version": "claude-code-fw-g-tool-policy/v1",
        "unknown_tool_policy": "deny",
        "structured_output": tool_policy_projection(structured),
        "agent": tool_policy_projection(agent),
        "bash": tool_policy_projection(bash),
        "mcp_deferred": tool_policy_projection(mcp),
        "advisor": tool_policy_projection(advisor),
        "background": tool_policy_projection(background),
        "web_search_outer": tool_policy_projection(web_search_outer),
        "web_search_server": tool_policy_projection(web_search_server),
    }


def build_implementation_policy(
    campaign_root: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    allowed_raw_sha256s = collect_measured_raw_sha256s(ledger_path)
    baseline = load_scenario_messages(
        campaign_root, "v4-replay-baseline", allowed_raw_sha256s
    )[0]
    agent_requests = load_scenario_messages(
        campaign_root, "v4-agent-depth1", allowed_raw_sha256s
    )
    agent = next(
        (request for request in agent_requests if header_value(request, "x-claude-code-agent-id")),
        None,
    )
    if agent is None:
        raise SystemExit("Agent 场景没有带 agent-id 的请求")
    tui_requests = load_scenario_messages(
        campaign_root, "v4-replay-tui", allowed_raw_sha256s
    )
    tui_main = next(
        (request for request in tui_requests if request["body"].get("model") == "claude-sonnet-5"),
        None,
    )
    tui_title = next(
        (
            request
            for request in tui_requests
            if request["body"].get("model") == "claude-haiku-4-5-20251001"
        ),
        None,
    )
    if tui_main is None or tui_title is None:
        raise SystemExit("TUI 场景没有形成标题请求与主请求条件对照")
    fallback_requests = load_scenario_messages(
        campaign_root, "v4-replay-fallback-model", allowed_raw_sha256s
    )
    fallback = next(
        (request for request in fallback_requests if request["body"].get("model") == "claude-haiku-4-5"),
        None,
    )
    if fallback is None:
        raise SystemExit("fallback 场景没有 Haiku 第四次请求")
    background_requests = load_scenario_messages(
        campaign_root, "v4-background", allowed_raw_sha256s
    )
    custom_system = load_scenario_messages(
        campaign_root, "v4-replay-custom-system", allowed_raw_sha256s
    )[0]
    append_system = load_scenario_messages(
        campaign_root, "v4-replay-append-system", allowed_raw_sha256s
    )[0]
    exclude_dynamic = load_scenario_messages(
        campaign_root, "v4-replay-exclude-dynamic-system", allowed_raw_sha256s
    )[0]
    custom_agent = load_scenario_messages(
        campaign_root, "v4-replay-custom-agent", allowed_raw_sha256s
    )[0]
    web_search_server = select_messages_request(
        campaign_root,
        "v4-web-search",
        allowed_raw_sha256s,
        lambda request: "tool_choice" in request.get("body", {}),
        "server web_search",
    )
    background = [scenario_projection(request) for request in background_requests]

    baseline_projection = scenario_projection(baseline)
    agent_projection = scenario_projection(agent)
    tui_main_projection = scenario_projection(tui_main)
    tui_title_projection = scenario_projection(tui_title)
    fallback_projection = scenario_projection(fallback)
    if baseline_projection["model"] != "claude-sonnet-5":
        raise SystemExit("基线模型不是 claude-sonnet-5")

    return {
        "schema_version": "claude-code-fw-g-implementation-policy/v2",
        "evidence_ledger": {
            "path": ledger_path.as_posix(),
            "sha256": sha256_hex(ledger_path.read_bytes()),
        },
        "scenarios": {
            "sdk_cli": baseline_projection,
            "agent": agent_projection,
            "tui_main": tui_main_projection,
            "tui_title": tui_title_projection,
            "fallback": fallback_projection,
            "background": background,
            "custom_system": scenario_projection(custom_system),
            "append_system": scenario_projection(append_system),
            "exclude_dynamic": scenario_projection(exclude_dynamic),
            "custom_agent": scenario_projection(custom_agent),
            "web_search_server": scenario_projection(web_search_server),
        },
        "identity": {
            "device_id_algorithm": "sha256-account-scope-ingress-binding-v1",
            "session_sources": ["official-consistent", "planner-derived"],
            "agent_id_pattern": "^[0-9a-f]{17}$",
            "metadata_order": ["extra_metadata", "device_id", "account_uuid", "session_id"],
        },
        "headers": {
            "custom_insert_after": "X-Claude-Code-Session-Id",
            "protected_names": [
                "accept",
                "authorization",
                "connection",
                "content-encoding",
                "content-length",
                "content-type",
                "host",
                "user-agent",
                "x-claude-code-session-id",
                "x-client-request-id",
            ],
            "conditional_order": [
                "x-anthropic-additional-protection",
                "x-app",
                "x-claude-remote-container-id",
                "x-claude-remote-session-id",
                "x-client-app",
                "x-claude-code-agent-id",
                "x-claude-code-parent-agent-id",
                "x-client-request-id",
            ],
        },
        "retry": {
            "retryable_statuses": [401, 408, 409, 429, 500, 502, 503, 529],
            "non_retryable_statuses": [400, 403],
            "default_base_ms": [500, 1000],
            "default_jitter_max_ms": 250,
            "retry_after_seconds_jitter_max_ms": 100,
            "http_date_policy": "default-backoff",
            "default_max_retries": 2,
            "retry_count_header": "0",
            "stream_timeout_seconds": 600,
            "non_stream_timeout_seconds": 300,
        },
        "tool_policy": build_tool_policy(campaign_root, allowed_raw_sha256s),
    }


def build_wire_artifact(
    messages_path: Path,
    tls_path: Path,
    campaign_root: Path,
    measured_ledger: Path,
) -> dict[str, Any]:
    raw_request = messages_path.read_bytes()
    if sha256_hex(raw_request) != EXPECTED_MESSAGES_EVIDENCE_DIGEST:
        raise SystemExit("messages R 通道摘要不一致")
    try:
        raw_head, raw_body = raw_request.split(b"\r\n\r\n", 1)
    except ValueError as exc:
        raise SystemExit("messages R 通道不是完整 HTTP/1.1 请求") from exc
    if sha256_hex(raw_body) != EXPECTED_MESSAGES_BODY_DIGEST:
        raise SystemExit("messages Body 摘要不一致")
    content_length_match = re.search(br"(?im)^Content-Length: (\d+)$", raw_head)
    if content_length_match is None or int(content_length_match.group(1)) != len(raw_body):
        raise SystemExit("messages R 通道 Content-Length 不一致")
    body = json.loads(raw_body)
    system = body.get("system")
    if not isinstance(system, list) or len(system) != 4:
        raise SystemExit("messages 基线不是四段 system")
    static_blocks = system[1:]
    for index, (block, expected_digest) in enumerate(
        zip(static_blocks, EXPECTED_SYSTEM_TEXT_DIGESTS, strict=True), start=1
    ):
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str) or sha256_hex(text.encode("utf-8")) != expected_digest:
            raise SystemExit(f"messages system[{index}] 文本摘要不一致")

    tls_pcap = tls_path.read_bytes()
    if sha256_hex(tls_pcap) != EXPECTED_TLS_PCAP_DIGEST:
        raise SystemExit("TLS P 通道摘要不一致")
    hellos = extract_client_hellos(tls_pcap)
    if len(hellos) != 4:
        raise SystemExit(f"TLS P 通道 ClientHello 数量不是 4：{len(hellos)}")
    with_alpn = [hello for hello in hellos if hello["alpn"]]
    without_alpn = [hello for hello in hellos if not hello["alpn"]]
    if len(with_alpn) != 2 or len(without_alpn) != 2:
        raise SystemExit("TLS P 通道没有形成 2+2 ALPN 条件对照")
    for vector in with_alpn:
        validate_tls_vector(vector, True)
    for vector in without_alpn:
        validate_tls_vector(vector, False)

    return {
        "schema_version": "claude-code-fw-g-wire-artifact/v2",
        "identity": {
            "version": EXPECTED_VERSION,
            "platform": "linux/amd64",
            "profile_digest": EXPECTED_PROFILE_DIGEST,
        },
        "evidence": {
            "messages_request": {
                "path": messages_path.as_posix(),
                "sha256": EXPECTED_MESSAGES_EVIDENCE_DIGEST,
                "bytes": len(raw_request),
            },
            "messages_body_sha256": EXPECTED_MESSAGES_BODY_DIGEST,
            "tls_pcap": {
                "path": tls_path.as_posix(),
                "sha256": EXPECTED_TLS_PCAP_DIGEST,
                "bytes": len(tls_pcap),
            },
        },
        "attestation": {
            "version_fingerprint": {
                "algorithm": "sha256-salt-message-index-v1",
                "salt": "59cf53e54c78",
                "message_indexes": [4, 7, 20],
                "hex_length": 3,
            },
            "cch": {
                "algorithm": "crypto-random-20-bit-nonce",
                "hex_length": 5,
                "lifecycle": "per-logical-request-reused-by-transport-retry",
            },
        },
        "messages": {
            "default_beta": raw_head.decode("latin1")
            .split("anthropic-beta: ", 1)[1]
            .split("\r\n", 1)[0],
            "system_blocks_json": wire_json(static_blocks),
            "system_text_sha256": EXPECTED_SYSTEM_TEXT_DIGESTS,
        },
        "implementation_policy": build_implementation_policy(
            campaign_root, measured_ledger
        ),
        "transports": {
            "http1_with_alpn": with_alpn[0],
            "http1_without_alpn": without_alpn[0],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--required-rules-manifest", type=Path, required=True)
    parser.add_argument("--measured-ledger", type=Path, required=True)
    parser.add_argument("--evidence-package", type=Path, required=True)
    parser.add_argument("--support-envelope", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--messages-evidence", type=Path)
    parser.add_argument("--tls-pcap", type=Path)
    parser.add_argument("--wire-output", type=Path)
    parser.add_argument("--campaign-root", type=Path)
    args = parser.parse_args()

    snapshot = load_frozen_json(args.snapshot, EXPECTED_SNAPSHOT_DIGEST, "Snapshot")
    manifest = load_frozen_json(
        args.required_rules_manifest,
        EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST,
        "RequiredRules manifest",
    )
    atomic_ledger = load_frozen_json(
        args.measured_ledger,
        EXPECTED_ATOMIC_LEDGER_DIGEST,
        "AtomicAssertionLedger",
    )
    evidence_package = load_frozen_json(
        args.evidence_package,
        EXPECTED_EVIDENCE_PACKAGE_DIGEST,
        "EvidencePackage",
    )
    support_envelope = load_frozen_json(
        args.support_envelope,
        EXPECTED_SUPPORT_ENVELOPE_DIGEST,
        "SupportEnvelope",
    )
    document = validate_fw_f_rule_authorities(
        snapshot,
        manifest,
        atomic_ledger,
        evidence_package,
        support_envelope,
    )
    if document.get("identity", {}).get("version") != EXPECTED_VERSION:
        raise SystemExit("Snapshot 目标版本不是 2.1.226")

    raw = canonical_bytes(document)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_PROFILE_DIGEST:
        raise SystemExit(f"画像摘要不一致：{digest}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    print(f"generated {args.output} sha256={digest} bytes={len(raw)}")
    wire_args = (
        args.messages_evidence,
        args.tls_pcap,
        args.wire_output,
        args.campaign_root,
    )
    if any(wire_args) and not all(wire_args):
        raise SystemExit("生成 wire artifact 时必须同时提供四个 wire 参数")
    if all(wire_args):
        wire_document = build_wire_artifact(
            args.messages_evidence,
            args.tls_pcap,
            args.campaign_root,
            args.measured_ledger,
        )
        wire_raw = canonical_bytes(wire_document)
        args.wire_output.parent.mkdir(parents=True, exist_ok=True)
        args.wire_output.write_bytes(wire_raw)
        print(
            f"generated {args.wire_output} sha256={sha256_hex(wire_raw)} "
            f"bytes={len(wire_raw)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
