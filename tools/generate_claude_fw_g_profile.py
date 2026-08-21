#!/usr/bin/env python3
"""从 FW-F 受批 Snapshot 与 2.1.226 原生证据生成 FW-G 内容寻址制品。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.official_client_capture.claude_fw_f_profile import parse_http_stream


EXPECTED_PROFILE_DIGEST = (
    "4da60bc238694a06a0dc80d68117abddd2de98c7c924c4db4c5dd929ea411e17"
)
PREVIOUS_WIRE_DIGEST = (
    "07dbb15e8a621c4ef4922a9dec09e08d032fd649414f7d2f572c95c87b5679a7"
)
PREVIOUS_RELEASE_DIGEST = (
    "c1053492eabc0b10d9d5f92f807a1df0d507c777b64a528e938426350c0d5350"
)
PREVIOUS_BUNDLE_DIGEST = (
    "4213ea92a7d76c4ef3aa318f4d93628cbcf675dc86566b107dddb70a70e6eb41"
)
EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST = (
    "c09fdc9158d4d3aee7e5d28cc4a794e259a3c56899e00d558afab4c537389045"
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
EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS = {
    "summarized": "454962fdfd52d81ab8200e78b6d158dbc3595c94810737a3c6dd58ad2db096f9",
    "omitted": "b958407da791047ac69fe05ac5c5fb93e87c839942d4a321d5140650ae49efee",
}
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
MODEL_CAPABILITY_MODELS = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")
MODEL_CAPABILITY_EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS = 42
MODEL_CAPABILITY_FAILED_ATTEMPTS = {
    "claude-opus-5-v4-replay-baseline/attempt-001",
    "claude-opus-5-v4-replay-tui/attempt-001",
    "claude-opus-5-v4-replay-tui/attempt-002",
}
MODEL_CAPABILITY_BASE_COMMIT = "c662b59a3e558f7aa352cf3a424a603aa0c254eb"
EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
MODEL_CAPABILITY_PRIOR_TRANSITIONS = (
    (
        "docs/egress/maintenance/claude-fw-g-desktop-title-source-transition.json",
        "3d176f9b24549388936fd918e3eb2a160263eac372160611b42ab3dd62da6884",
    ),
    (
        "docs/egress/maintenance/claude-fw-g-desktop-title-test-transition.json",
        "eecadfe40a4bf5f6e1e55ac2493d74557cccb0568e61d33ef0291c30ab5056d3",
    ),
)
MODEL_CAPABILITY_SOURCE_TRANSITIONS = (
    (
        "backend/internal/officialegress/claude_body.go",
        "ba45723ed345a30cbd358ea9d0c23fe065df410a921fc3c69aa3eb6d5dd846b7",
        "按模型能力目录选择场景并重建 fallbacks、Body 顺序和 Fable server fallback。",
    ),
    (
        "backend/internal/officialegress/claude_ingress.go",
        "0920335717e3d646bc4d54bda997ea8c81a23bd3fd056d6258b442824abd0204",
        "验证官方模型场景、fallbacks 与 Fable 成对锁存 Header。",
    ),
    (
        "backend/internal/officialegress/claude_profile.go",
        "6c347093c3b722b6b9339fc9f497b279f5d9839d6dc99212313933fe0b5756b1",
        "切换到包含三模型能力目录摘要的新内容寻址 Profile。",
    ),
    (
        "backend/internal/officialegress/claude_runtime.go",
        "5b24eec2cedd2cdd44e9045933261b62dfac2f5d9cc64d6c057ff0024af9167c",
        "加载模型能力目录，执行精确模型门禁、count_tokens 和 Fable 会话锁存状态机。",
    ),
    (
        "backend/internal/officialegress/claude_runtime_test.go",
        "eb8945aec040a5c7107b86138bac48aac467d19fdf453a6105770e46132d8e48",
        "增加三模型逐场景、未知模型、错误 fallback、count_tokens 与锁存正负测试。",
    ),
    (
        "backend/internal/officialegress/claude_tools.go",
        "e49b12302ff469ec5d446f9d184466a25c65c8b7b3e1db8cf793b1bfc63d7b79",
        "仅允许官方入口精确承接模型目录中已取证场景的工具形态，不放宽第三方动态工具准入。",
    ),
    (
        "backend/internal/officialegress/claude_wire.go",
        "9558669b8f9c546116d1942424405d18e9a665b1fd6967e77c35ba4cb6fa14d8",
        "切换到新 Wire 并在启动时校验模型目录、证据、场景及 Header／Body 顺序。",
    ),
    (
        "backend/internal/officialegress/claude_fw_g_desktop_title_transition_test.go",
        "4916d31469ac694b3b6c957551b474adb2206da2db361a3b74bb6dc91931afdb",
        "让既有 Desktop 标题迁移只绑定其历史 Wire，并由本轮追加迁移承接当前源码。",
    ),
    (
        "backend/internal/officialegress/claude_fw_g_source_transition_test.go",
        "e943503961ebaf1e890410fcd61a8745f205b09668b9b807d5d7ec5404961fae",
        "让 FW-G 初始收据继续绑定历史制品身份，并允许本轮追加迁移承接当前源码。",
    ),
    (
        "backend/internal/officialegress/claude_fw_g_thinking_display_transition_test.go",
        "e3abae6d0a1ff445adb531618653b8f0c84e53de7c07705531ee8cfaf5719bd6",
        "让 thinking.display 收据只绑定其历史 Profile／Wire，并由本轮追加迁移承接当前源码。",
    ),
    (
        "backend/internal/service/gateway_claude_fw_g.go",
        "750ecc54194ba13a01bdf42a0a962fc6b76195cca3c00179cf369017407d0e6d",
        "把实际上游响应模型交给 Persona 会话 finalizer，禁止客户端预造 Fable 锁存。",
    ),
    (
        "backend/internal/service/claude_fw_g_source_transition_test.go",
        "9ef2cc4a97c023f6ad4921e2664d823ffcfc2435442b18a9673529d136e93893",
        "让 service 层既有 FW-G 收据继续绑定历史制品，并由本轮追加迁移承接当前源码。",
    ),
    (
        "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
        "4cc9b2acbbc50203cab78eeeedaf0d4f46aaa4500d082e27fdf5a8e9b937b31b",
        "按 Codex 粒度明确公共 RequiredRules 与独立模型能力目录的证据和换版合同。",
    ),
    (
        "tools/generate_claude_fw_g_profile.py",
        "871e74ac3e0466d26977e9bb44256ec2e4d913b7e87895c7a307c4c5926420e4",
        "从三模型官方 Campaign 确定性生成 Profile、Wire、能力目录和追加式迁移收据。",
    ),
)


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


def repository_relative(path: Path) -> str:
    absolute = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    try:
        return absolute.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise SystemExit(f"模型能力制品不在仓库内：{absolute}") from exc


def build_model_capability_source_transitions(
    profile_path: Path,
    wire_path: Path,
) -> list[dict[str, str]]:
    specs = list(MODEL_CAPABILITY_SOURCE_TRANSITIONS)
    specs.extend(
        [
            (
                repository_relative(profile_path),
                EMPTY_FILE_SHA256,
                "新增内容寻址 Profile，冻结三模型显式闭集和能力目录摘要。",
            ),
            (
                repository_relative(wire_path),
                EMPTY_FILE_SHA256,
                "新增内容寻址 Wire，冻结三模型逐场景官方请求与状态差异。",
            ),
        ]
    )
    paths = [path for path, _, _ in specs]
    if len(paths) != len(set(paths)):
        raise SystemExit("模型能力源码 transition 路径重复")
    transitions: list[dict[str, str]] = []
    for path, before_digest, reason in sorted(specs):
        source_path = ROOT / path
        if not source_path.is_file():
            raise SystemExit(f"模型能力源码 transition 缺少文件：{path}")
        after_digest = sha256_hex(source_path.read_bytes())
        if before_digest == after_digest:
            raise SystemExit(f"模型能力源码 transition 未发生变化：{path}")
        transitions.append(
            {
                "path": path,
                "from_sha256": before_digest,
                "to_sha256": after_digest,
                "reason": reason,
            }
        )
    return transitions


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


def build_thinking_display_policy(
    supplement_root: Path,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    expected_body_keys = list(baseline.get("body", {}))
    if baseline.get("body", {}).get("thinking") != {"type": "adaptive"}:
        raise SystemExit("thinking.display 缺省证据不是 adaptive 单键对象")
    evidence: dict[str, dict[str, Any]] = {
        "default": {
            "path": baseline["evidence_path"],
            "raw_sha256": baseline["raw_sha256"],
        }
    }
    attempts = {"summarized": "002", "omitted": "001"}
    for display, attempt in attempts.items():
        probe = f"v4-thinking-display-{display}"
        attempt_root = supplement_root / "attempts" / probe / f"attempt-{attempt}"
        manifest_path = attempt_root / "relay-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dimension = manifest.get("dimension_evidence") or {}
        if (
            manifest.get("status") != "complete"
            or manifest.get("m_binding", {}).get("complete") is not True
            or manifest.get("secret_scan", {}).get("passed") is not True
            or dimension.get("result") != "passed"
        ):
            raise SystemExit(f"{probe} R/M 没有完整通过")
        summary = json.loads(
            (attempt_root / "results" / "v4-summary.json").read_text(encoding="utf-8")
        )
        argv = summary.get("inner_result", {}).get("invocation", {}).get("argv_redacted", [])
        expected_pair = ["--thinking-display", display]
        if summary.get("valid") is not True or not any(
            argv[index : index + 2] == expected_pair for index in range(len(argv) - 1)
        ):
            raise SystemExit(f"{probe} 未绑定冻结 CLI 参数")
        requests: list[dict[str, Any]] = []
        for path in sorted((attempt_root / "relay").glob("conn*.client_to_upstream.bin")):
            for request in parse_http_stream(path.read_bytes(), probe):
                if request.get("request_target") != "/v1/messages?beta=true":
                    continue
                request = dict(request)
                request["evidence_path"] = path.as_posix()
                requests.append(request)
        if len(requests) != 1:
            raise SystemExit(f"{probe} messages R 通道数量不是 1")
        request = requests[0]
        if request.get("raw_sha256") != EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS[display]:
            raise SystemExit(f"{probe} R 通道摘要漂移")
        thinking = request.get("body", {}).get("thinking")
        if (
            not isinstance(thinking, dict)
            or list(thinking) != ["type", "display"]
            or thinking != {"type": "adaptive", "display": display}
            or list(request.get("body", {})) != expected_body_keys
        ):
            raise SystemExit(f"{probe} thinking wire 或顶层顺序不一致")
        evidence[display] = {
            "path": request["evidence_path"],
            "raw_sha256": request["raw_sha256"],
        }
    production_receipt = supplement_root / "environment" / "production-change-receipt.json"
    production = json.loads(production_receipt.read_text(encoding="utf-8"))
    if production.get("result") != "passed" or production.get("differences") != []:
        raise SystemExit("thinking.display 补充 Campaign 影响了生产环境")
    evidence["production_change_receipt"] = {
        "path": production_receipt.as_posix(),
        "sha256": sha256_hex(production_receipt.read_bytes()),
    }
    return {
        "schema_version": "claude-code-thinking-display-policy/v1",
        "type": "adaptive",
        "field_order": ["type", "display"],
        "display_values": ["summarized", "omitted"],
        "evidence": evidence,
    }


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
    header_order = [str(header.get("name", "")) for header in request.get("headers", [])]
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
        "fallbacks_json": wire_json(body.get("fallbacks")),
        "output_config_json": wire_json(body.get("output_config")),
        "temperature_json": wire_json(body.get("temperature")),
        "thinking_present": "thinking" in body,
        "context_management_present": "context_management" in body,
        "fallbacks_present": "fallbacks" in body,
        "output_config_present": "output_config" in body,
        "temperature_present": "temperature" in body,
        "stream_present": "stream" in body,
        "stream_json": wire_json(body.get("stream")),
        "tools_present": "tools" in body,
        "tools_json": wire_json(body.get("tools")),
        "system_present": "system" in body,
        "system_blocks_json": wire_json(public_system_blocks(request)),
        "metadata_present": "metadata" in body,
        "body_order": list(body),
        "header_order": header_order,
        "fallback_latched_by_present": bool(
            header_value(request, "x-cc-fallback-latched-by")
        ),
        "refusal_fallback_value": header_value(request, "x-is-refusal-fallback"),
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
    thinking_display_campaign_root: Path,
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
    web_search_outer = select_messages_request(
        campaign_root,
        "v4-web-search",
        allowed_raw_sha256s,
        lambda request: request.get("body", {}).get("tools", [{}])[0].get("name")
        == "WebSearch"
        and "tool_choice" not in request.get("body", {}),
        "outer WebSearch",
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
            "web_search_outer": scenario_projection(web_search_outer),
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
                "x-cc-fallback-latched-by",
                "x-client-request-id",
                "x-is-refusal-fallback",
            ],
            "conditional_order": [
                "x-anthropic-additional-protection",
                "x-app",
                "x-claude-remote-container-id",
                "x-claude-remote-session-id",
                "x-client-app",
                "x-claude-code-agent-id",
                "x-claude-code-parent-agent-id",
                "x-cc-fallback-latched-by",
                "x-client-request-id",
                "x-is-refusal-fallback",
            ],
        },
        "thinking": build_thinking_display_policy(
            thinking_display_campaign_root,
            baseline,
        ),
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


def successful_model_attempts(campaign_root: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    attempts_root = campaign_root / "attempts"
    successful: dict[str, Path] = {}
    failed: set[str] = set()
    attempt_receipts: list[dict[str, Any]] = []
    telemetry_markers = ("telemetry", "event_logging", "datadog", "statsig", "analytics")
    for scenario_root in sorted(attempts_root.iterdir()):
        if not scenario_root.is_dir():
            continue
        for attempt_root in sorted(scenario_root.glob("attempt-*")):
            manifest_path = attempt_root / "relay-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            attempt_id = f"{scenario_root.name}/{attempt_root.name}"
            complete = (
                manifest.get("status") == "complete"
                and manifest.get("m_binding", {}).get("complete") is True
                and manifest.get("secret_scan", {}).get("passed") is True
                and manifest.get("dimension_evidence", {}).get("result") == "passed"
                and manifest.get("m_binding", {})
                .get("requirements", {})
                .get("hosts_restored")
                is True
            )
            if not complete:
                failed.add(attempt_id)
                attempt_receipts.append(
                    {
                        "attempt_id": attempt_id,
                        "status": "historical_failed",
                        "manifest_sha256": sha256_hex(manifest_path.read_bytes()),
                    }
                )
                continue
            for item in manifest.get("dimension_evidence", {}).get(
                "actual_wire_inventory", []
            ):
                identity = " ".join(
                    str(item.get(name, ""))
                    for name in ("request_target", "host", "evidence_file")
                ).lower()
                if any(marker in identity for marker in telemetry_markers):
                    raise SystemExit(f"模型能力 Campaign 混入遥测／行为请求：{attempt_id}")
            runtime_path = (
                campaign_root
                / "runtime-receipts"
                / f"{scenario_root.name}-{attempt_root.name}.json"
            )
            if not runtime_path.is_file():
                raise SystemExit(f"模型能力 attempt 缺少 runtime receipt：{attempt_id}")
            successful[scenario_root.name] = attempt_root
            attempt_receipts.append(
                {
                    "attempt_id": attempt_id,
                    "status": "complete",
                    "manifest_sha256": sha256_hex(manifest_path.read_bytes()),
                    "runtime_receipt_sha256": sha256_hex(runtime_path.read_bytes()),
                }
            )
    complete_count = sum(item["status"] == "complete" for item in attempt_receipts)
    if complete_count != MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS:
        raise SystemExit(f"模型能力成功 attempt 数量不是 42：{complete_count}")
    if failed != MODEL_CAPABILITY_FAILED_ATTEMPTS:
        raise SystemExit(f"模型能力历史失败集合漂移：{sorted(failed)}")

    production_path = campaign_root / "environment" / "production-diff.json"
    production = json.loads(production_path.read_text(encoding="utf-8"))
    if production.get("identical") is not True or production.get("changed_fields") != []:
        raise SystemExit("模型能力 Campaign 改变了 Vircs 生产服务")
    evidence = {
        "campaign_root": campaign_root.as_posix(),
        "successful_attempts": complete_count,
        "historical_failed_attempts": len(failed),
        "attempts": attempt_receipts,
        "production_diff": {
            "path": production_path.as_posix(),
            "sha256": sha256_hex(production_path.read_bytes()),
            "identical": True,
        },
        "behavior_traffic_disposition": "excluded-from-rules-and-difference-judgement",
    }
    evidence["campaign_sha256"] = sha256_hex(canonical_bytes(evidence))
    return successful, evidence


def load_attempt_requests(attempt_root: Path, scenario: str) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for path in sorted((attempt_root / "relay").glob("conn*.client_to_upstream.bin")):
        for request in parse_http_stream(path.read_bytes(), scenario):
            request = dict(request)
            request["evidence_path"] = path.as_posix()
            requests.append(request)
    return requests


def select_base_campaign_request(
    campaign_root: Path,
    scenario: str,
    allowed_raw_sha256s: set[str],
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    attempt_roots = sorted((campaign_root / "attempts" / scenario).glob("attempt-*"))
    selected_attempt = None
    for attempt_root in attempt_roots:
        manifest_path = attempt_root / "relay-manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("status") == "complete"
            and manifest.get("m_binding", {}).get("complete") is True
            and manifest.get("secret_scan", {}).get("passed") is True
        ):
            selected_attempt = attempt_root
    if selected_attempt is None:
        raise SystemExit(f"基础 Campaign 缺少成功场景：{scenario}")
    for request in load_attempt_requests(selected_attempt, scenario):
        if not predicate(request):
            continue
        if request.get("raw_sha256") not in allowed_raw_sha256s:
            raise SystemExit(f"{scenario} 请求未绑定 MeasuredRuleLedger")
        return request
    raise SystemExit(f"{scenario} 没有 {description}")


def select_attempt_request(
    attempts: dict[str, Path],
    scenario: str,
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    attempt = attempts.get(scenario)
    if attempt is None:
        raise SystemExit(f"模型能力 Campaign 缺少成功场景：{scenario}")
    selected = next(
        (request for request in load_attempt_requests(attempt, scenario) if predicate(request)),
        None,
    )
    if selected is None:
        raise SystemExit(f"{scenario} 没有 {description}")
    return selected


def build_model_scenarios(
    model: str,
    attempts: dict[str, Path],
    base_scenarios: dict[str, Any],
) -> dict[str, Any]:
    prefix = f"{model}-"

    def primary(suffix: str) -> dict[str, Any]:
        return select_attempt_request(
            attempts,
            prefix + suffix,
            lambda request: request.get("request_target") == "/v1/messages?beta=true"
            and request.get("body", {}).get("model") == model,
            model + " messages",
        )

    agent = select_attempt_request(
        attempts,
        prefix + "v4-agent-depth1",
        lambda request: request.get("body", {}).get("model") == model
        and bool(header_value(request, "x-claude-code-agent-id")),
        "agent 子请求",
    )
    tui_main = select_attempt_request(
        attempts,
        prefix + "v4-replay-tui",
        lambda request: request.get("body", {}).get("model") == model,
        "TUI 主请求",
    )
    background_attempt = attempts.get(prefix + "v4-background")
    if background_attempt is None:
        raise SystemExit(f"{model} 缺少 background 成功场景")
    background_requests = [
        request
        for request in load_attempt_requests(background_attempt, prefix + "v4-background")
        if request.get("request_target") == "/v1/messages?beta=true"
    ]
    if len(background_requests) != 4:
        raise SystemExit(f"{model} background 请求数量不是 4")
    background_agent_attempt = attempts.get(prefix + "v4-background-subagent")
    if background_agent_attempt is None:
        raise SystemExit(f"{model} 缺少 background subagent 成功场景")
    background_agent_requests = [
        request
        for request in load_attempt_requests(
            background_agent_attempt, prefix + "v4-background-subagent"
        )
        if request.get("request_target") == "/v1/messages?beta=true"
        and request.get("body", {}).get("model") == model
    ]
    sdk_cli_background_agent = next(
        (
            request
            for request in background_agent_requests
            if not header_value(request, "x-claude-code-agent-id")
        ),
        None,
    )
    agent_background = next(
        (
            request
            for request in background_agent_requests
            if header_value(request, "x-claude-code-agent-id")
        ),
        None,
    )
    if sdk_cli_background_agent is None or agent_background is None:
        raise SystemExit(f"{model} background subagent 形态不完整")
    web_search_attempt = attempts.get(prefix + "v4-web-search")
    if web_search_attempt is None:
        raise SystemExit(f"{model} 缺少 WebSearch 成功场景")
    web_search_requests = load_attempt_requests(
        web_search_attempt, prefix + "v4-web-search"
    )
    web_search_outer = next(
        (
            request
            for request in web_search_requests
            if request.get("body", {}).get("model") == model
            and request.get("body", {}).get("tools", [{}])[0].get("name") == "WebSearch"
            and "tool_choice" not in request.get("body", {})
        ),
        None,
    )
    web_search_server = next(
        (
            request
            for request in web_search_requests
            if request.get("body", {}).get("model") == model
            and request.get("body", {}).get("tools", [{}])[0].get("name") == "web_search"
            and "tool_choice" in request.get("body", {})
        ),
        None,
    )
    if web_search_outer is None or web_search_server is None:
        raise SystemExit(f"{model} WebSearch 外层／server 形态不完整")

    scenarios = copy.deepcopy(base_scenarios)
    scenarios.update(
        {
            "sdk_cli": scenario_projection(primary("v4-replay-baseline")),
            "agent": scenario_projection(agent),
            "tui_main": scenario_projection(tui_main),
            "background": [scenario_projection(request) for request in background_requests],
            "custom_system": scenario_projection(primary("v4-replay-custom-system")),
            "append_system": scenario_projection(primary("v4-replay-append-system")),
            "exclude_dynamic": scenario_projection(
                primary("v4-replay-exclude-dynamic-system")
            ),
            "custom_agent": scenario_projection(primary("v4-replay-custom-agent")),
            "sdk_cli_background_agent": scenario_projection(sdk_cli_background_agent),
            "agent_background": scenario_projection(agent_background),
            "web_search_outer": scenario_projection(web_search_outer),
            "web_search_server": scenario_projection(web_search_server),
            "server_fallback": None,
        }
    )
    if model == "claude-fable-5":
        fallback = select_attempt_request(
            attempts,
            prefix + "v4-bash",
            lambda request: request.get("body", {}).get("model") == "claude-opus-4-8"
            and header_value(request, "x-is-refusal-fallback") == "true"
            and bool(header_value(request, "x-cc-fallback-latched-by")),
            "server fallback 锁存请求",
        )
        scenarios["server_fallback"] = scenario_projection(fallback)
    return scenarios


def build_model_capability_catalog(
    campaign_root: Path,
    base_campaign_root: Path,
    measured_ledger: Path,
    base_scenarios: dict[str, Any],
) -> dict[str, Any]:
    attempts, evidence = successful_model_attempts(campaign_root)
    allowed_raw_sha256s = collect_measured_raw_sha256s(measured_ledger)
    sonnet_scenarios = copy.deepcopy(base_scenarios)
    sonnet_scenarios["server_fallback"] = None

    models: list[dict[str, Any]] = []
    for model in MODEL_CAPABILITY_MODELS:
        if model == "claude-sonnet-5":
            scenarios = sonnet_scenarios
            count_tokens = select_base_campaign_request(
                base_campaign_root,
                "v4-tui-count-tokens",
                allowed_raw_sha256s,
                lambda request: request.get("request_target", "").startswith(
                    "/v1/messages/count_tokens"
                )
                and request.get("body", {}).get("model") == model,
                "Sonnet count_tokens",
            )
            effort_evidence = {
                effort: scenario_projection(
                    select_messages_request(
                        base_campaign_root,
                        (
                            "v4-replay-baseline"
                            if effort == "high"
                            else f"v4-replay-effort-{effort}"
                        ),
                        allowed_raw_sha256s,
                        lambda request, expected=effort: request.get("body", {})
                        .get("output_config", {})
                        .get("effort")
                        == expected,
                        f"Sonnet effort={effort}",
                    )
                )["evidence"]
                for effort in MODEL_CAPABILITY_EFFORTS
            }
            thinking_disabled = select_messages_request(
                base_campaign_root,
                "v4-replay-thinking-disabled",
                allowed_raw_sha256s,
                lambda request: "thinking" not in request.get("body", {}),
                "Sonnet thinking disabled",
            )
        else:
            scenarios = build_model_scenarios(model, attempts, base_scenarios)
            prefix = f"{model}-"
            count_tokens = select_attempt_request(
                attempts,
                prefix + "v4-tui-count-tokens",
                lambda request: request.get("request_target", "").startswith(
                    "/v1/messages/count_tokens"
                )
                and request.get("body", {}).get("model") == model,
                model + " count_tokens",
            )
            effort_evidence = {}
            for effort in MODEL_CAPABILITY_EFFORTS:
                effort_scenario = (
                    "v4-replay-baseline"
                    if effort == "high"
                    else f"v4-replay-effort-{effort}"
                )
                request = select_attempt_request(
                    attempts,
                    prefix + effort_scenario,
                    lambda request, expected=effort: request.get("body", {})
                    .get("output_config", {})
                    .get("effort")
                    == expected,
                    f"{model} effort={effort}",
                )
                effort_evidence[effort] = {
                    "path": request["evidence_path"],
                    "raw_sha256": request["raw_sha256"],
                }
            thinking_disabled = select_attempt_request(
                attempts,
                prefix + "v4-replay-thinking-disabled",
                lambda request: request.get("body", {}).get("model") == model
                and "thinking" not in request.get("body", {}),
                model + " thinking disabled",
            )
        models.append(
            {
                "canonical_model": model,
                "aliases": [model],
                "effort_values": list(MODEL_CAPABILITY_EFFORTS),
                "effort_evidence": effort_evidence,
                "thinking_disable_evidence": {
                    "path": thinking_disabled["evidence_path"],
                    "raw_sha256": thinking_disabled["raw_sha256"],
                },
                "count_tokens_evidence": {
                    "path": count_tokens["evidence_path"],
                    "raw_sha256": count_tokens["raw_sha256"],
                },
                "legacy_retry_fallback_supported": model == "claude-sonnet-5",
                "scenarios": scenarios,
            }
        )
    return {
        "schema_version": "claude-code-model-capability-catalog/v1",
        "required_rule_count": EXPECTED_RULE_COUNT,
        "unknown_model_policy": "deny",
        "alias_policy": "explicit-only",
        "evidence": evidence,
        "models": models,
    }


def extend_profile_for_model_capabilities(
    base_document: dict[str, Any],
    model_catalog: dict[str, Any],
) -> dict[str, Any]:
    document = copy.deepcopy(base_document)
    document["schema_version"] = "claude-code-fw-f-target-profile/v6"
    identity = document.setdefault("identity", {})
    identity["supported_models"] = [
        model["canonical_model"] for model in model_catalog["models"]
    ]
    identity["model_alias_policy"] = model_catalog["alias_policy"]
    identity["unknown_model_policy"] = model_catalog["unknown_model_policy"]
    identity["model_capability_catalog_sha256"] = sha256_hex(
        canonical_bytes(model_catalog)
    )
    body = document.setdefault("body", {})
    field_types = body.setdefault("field_types", {})
    field_types["fallbacks"] = ["array"]
    optional = body.setdefault("optional_fields", [])
    if "fallbacks" not in optional:
        optional.append("fallbacks")
    optional.sort()
    if len(document.get("rules", [])) != EXPECTED_RULE_COUNT:
        raise SystemExit("模型能力扩展改变了 40 条 RequiredRules")
    return document


def build_wire_artifact(
    messages_path: Path,
    tls_path: Path,
    campaign_root: Path,
    measured_ledger: Path,
    thinking_display_campaign_root: Path,
    profile_digest: str,
    model_catalog: dict[str, Any],
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
        "schema_version": "claude-code-fw-g-wire-artifact/v3",
        "identity": {
            "version": EXPECTED_VERSION,
            "platform": "linux/amd64",
            "profile_digest": profile_digest,
            "model_capability_catalog_digest": sha256_hex(
                canonical_bytes(model_catalog)
            ),
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
        "implementation_policy": {
            **build_implementation_policy(
                campaign_root, measured_ledger, thinking_display_campaign_root
            ),
            "schema_version": "claude-code-fw-g-implementation-policy/v3",
            "model_catalog": model_catalog,
        },
        "transports": {
            "http1_with_alpn": with_alpn[0],
            "http1_without_alpn": without_alpn[0],
        },
    }


def build_model_capability_release_identity(
    profile_digest: str,
    wire_digest: str,
    model_catalog_digest: str,
) -> tuple[str, str]:
    release = {
        "schema_version": "claude-code-fw-g-model-capability-release/v1",
        "version": EXPECTED_VERSION,
        "platform": "linux/amd64",
        "profile_sha256": profile_digest,
        "wire_sha256": wire_digest,
        "model_capability_catalog_sha256": model_catalog_digest,
        "required_rules_manifest_sha256": EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST,
        "required_rule_count": EXPECTED_RULE_COUNT,
    }
    release_digest = sha256_hex(canonical_bytes(release))
    bundle = {
        "schema_version": "claude-code-fw-g-model-capability-bundle/v1",
        "persona": "claude-code-oauth",
        "release_sha256": release_digest,
        "profile_sha256": profile_digest,
        "wire_sha256": wire_digest,
    }
    return release_digest, sha256_hex(canonical_bytes(bundle))


def build_model_capability_receipt(
    model_catalog: dict[str, Any],
    profile_path: Path,
    profile_digest: str,
    wire_path: Path,
    wire_digest: str,
    release_digest: str,
    bundle_digest: str,
) -> dict[str, Any]:
    model_catalog_digest = sha256_hex(canonical_bytes(model_catalog))
    return {
        "schema_version": "official-egress-claude-model-capability-source-transition/v1",
        "date": "2026-08-21",
        "phase": "FW-G",
        "base_commit": MODEL_CAPABILITY_BASE_COMMIT,
        "prior_transitions": [
            {"path": path, "sha256": digest}
            for path, digest in MODEL_CAPABILITY_PRIOR_TRANSITIONS
        ],
        "target": {
            "product": "claude-code",
            "version": EXPECTED_VERSION,
            "platform": "linux/amd64",
            "models": list(MODEL_CAPABILITY_MODELS),
        },
        "invariants": {
            "required_rule_count_before": EXPECTED_RULE_COUNT,
            "required_rule_count_after": EXPECTED_RULE_COUNT,
            "required_rules_duplicated_per_model": False,
            "unknown_model_policy": "deny",
            "alias_policy": "explicit-only",
            "telemetry_and_behavior_traffic": "excluded",
        },
        "evidence": model_catalog["evidence"],
        "model_capability_catalog_sha256": model_catalog_digest,
        "artifacts": {
            "before": {
                "profile_sha256": EXPECTED_PROFILE_DIGEST,
                "wire_sha256": PREVIOUS_WIRE_DIGEST,
                "release_sha256": PREVIOUS_RELEASE_DIGEST,
                "bundle_sha256": PREVIOUS_BUNDLE_DIGEST,
            },
            "after": {
                "profile": profile_path.as_posix(),
                "profile_sha256": profile_digest,
                "wire": wire_path.as_posix(),
                "wire_sha256": wire_digest,
                "release_sha256": release_digest,
                "bundle_sha256": bundle_digest,
            },
        },
        "transitions": build_model_capability_source_transitions(
            profile_path,
            wire_path,
        ),
        "safety": {
            "vircs_production_changed": False,
            "production_selector_changed": False,
            "dmit_deployed": False,
        },
        "result": "passed",
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
    parser.add_argument("--thinking-display-campaign-root", type=Path)
    parser.add_argument("--model-capability-campaign-root", type=Path)
    parser.add_argument("--model-capability-receipt-output", type=Path)
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

    model_catalog = None
    if args.model_capability_campaign_root is not None:
        if args.campaign_root is None or args.thinking_display_campaign_root is None:
            raise SystemExit("模型能力目录需要基础 Campaign 与 thinking.display Campaign")
        base_policy = build_implementation_policy(
            args.campaign_root,
            args.measured_ledger,
            args.thinking_display_campaign_root,
        )
        model_catalog = build_model_capability_catalog(
            args.model_capability_campaign_root,
            args.campaign_root,
            args.measured_ledger,
            base_policy["scenarios"],
        )
        document = extend_profile_for_model_capabilities(document, model_catalog)

    raw = canonical_bytes(document)
    digest = hashlib.sha256(raw).hexdigest()
    if model_catalog is None and digest != EXPECTED_PROFILE_DIGEST:
        raise SystemExit(f"画像摘要不一致：{digest}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    print(f"generated {args.output} sha256={digest} bytes={len(raw)}")
    wire_args = (
        args.messages_evidence,
        args.tls_pcap,
        args.wire_output,
        args.campaign_root,
        args.thinking_display_campaign_root,
        args.model_capability_campaign_root,
        args.model_capability_receipt_output,
    )
    if any(wire_args) and not all(wire_args):
        raise SystemExit("生成 wire artifact 时必须同时提供五个 wire 参数")
    if all(wire_args):
        if model_catalog is None:
            raise SystemExit("生成新版 wire artifact 时缺少模型能力目录")
        wire_document = build_wire_artifact(
            args.messages_evidence,
            args.tls_pcap,
            args.campaign_root,
            args.measured_ledger,
            args.thinking_display_campaign_root,
            digest,
            model_catalog,
        )
        wire_raw = canonical_bytes(wire_document)
        wire_digest = sha256_hex(wire_raw)
        args.wire_output.parent.mkdir(parents=True, exist_ok=True)
        args.wire_output.write_bytes(wire_raw)
        print(
            f"generated {args.wire_output} sha256={wire_digest} "
            f"bytes={len(wire_raw)}"
        )
        model_catalog_digest = sha256_hex(canonical_bytes(model_catalog))
        release_digest, bundle_digest = build_model_capability_release_identity(
            digest, wire_digest, model_catalog_digest
        )
        receipt = build_model_capability_receipt(
            model_catalog,
            args.output,
            digest,
            args.wire_output,
            wire_digest,
            release_digest,
            bundle_digest,
        )
        receipt_raw = canonical_bytes(receipt)
        args.model_capability_receipt_output.parent.mkdir(parents=True, exist_ok=True)
        args.model_capability_receipt_output.write_bytes(receipt_raw)
        print(
            f"generated {args.model_capability_receipt_output} "
            f"sha256={sha256_hex(receipt_raw)} bytes={len(receipt_raw)}"
        )
        print(f"release_sha256={release_digest} bundle_sha256={bundle_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
