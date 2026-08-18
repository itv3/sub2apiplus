"""MITM JSONL 与 direct pcap 的脱敏规范化。"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from .security import normalize_json_shape, redact_header_value, secure_write_json


TSHARK_CLIENT_HELLO_FIELDS = (
    "frame.number",
    "ip.dst",
    "ipv6.dst",
    "tcp.dstport",
    "tls.handshake.extensions_server_name",
    "tls.handshake.ciphersuite",
    "tls.handshake.extension.type",
    "tls.handshake.extensions_alpn_str",
    "tls.handshake.extensions_supported_group",
    "tls.handshake.extensions_ec_point_format",
    "tls.handshake.sig_hash_alg",
    "tls.handshake.extensions.supported_version",
    "tls.handshake.extensions_key_share_group",
    "tls.extension.psk_ke_mode",
)

OFFICIAL_EGRESS_CONTRACTS = (
    "oauth-claude-http",
    "oauth-codex-http",
    "oauth-codex-compact-http",
    "oauth-codex-ws",
)
OFFICIAL_EGRESS_TLS_CONTRACTS = (
    "anthropic-http",
    "codex-http",
    "codex-ws",
)

_NORMALIZED_PLACEHOLDER_RE = re.compile(r"^<(dynamic|text|string):[^>]+>$")
_ANTHROPIC_SYSTEM_SPLIT_MARKER = "# Text output (does not apply to tool calls)"


def _normalize_headers(value: Any) -> list[list[str]]:
    result: list[list[str]] = []
    if not isinstance(value, list):
        return result
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            continue
        name, header_value = str(item[0]), str(item[1])
        result.append([name.lower(), redact_header_value(name, header_value)])
    return result


def _normalize_request_path(value: Any) -> Any:
    """保留查询参数名称与顺序，但移除全部查询值，避免 URL 凭据落盘。"""

    if not isinstance(value, str) or "?" not in value:
        return value
    parsed = urllib.parse.urlsplit(value)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    normalized_query = urllib.parse.urlencode(
        [(name, "<query-value>") for name, _value in query]
    )
    return urllib.parse.urlunsplit(("", "", parsed.path, normalized_query, ""))


def _normalized_observed_host(record: dict[str, Any], value: Any) -> str:
    """全量发现保留域名；普通已知目标抓包继续使用稳定占位符。"""

    if record.get("_capture_host_scope") != "all":
        return "<target-host>"
    host = str(value or "").strip().lower()
    if not host or any(character.isspace() for character in host):
        raise ValueError("全量网络 inventory 含非法 host。")
    return host


def normalize_mitm_record(record: dict[str, Any]) -> dict[str, Any]:
    """将原始 HTTP/WS 记录转换为可入库的结构证据。"""

    if record.get("_websocket"):
        return {
            "kind": "websocket_frame",
            "task": record.get("_task"),
            "boundary": record.get("_boundary"),
            "subject": record.get("_subject"),
            "scenario": record.get("_scenario"),
            "from_client": bool(record.get("from_client")),
            "capture_host_scope": record.get("_capture_host_scope", "targets"),
            "scheme": record.get("scheme"),
            "host": _normalized_observed_host(record, record.get("host")),
            "port": record.get("port"),
            "path": _normalize_request_path(record.get("path")),
            "length": record.get("length"),
            "json_shape": normalize_json_shape(record.get("json")),
        }
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    request_json = body.get("json")
    return {
        "kind": "http_exchange",
        "task": record.get("_task"),
        "boundary": record.get("_boundary"),
        "subject": record.get("_subject"),
        "scenario": record.get("_scenario"),
        "capture_host_scope": record.get("_capture_host_scope", "targets"),
        "request": {
            "method": request.get("method"),
            "scheme": request.get("scheme"),
            "host": _normalized_observed_host(record, request.get("host")),
            "port": request.get("port"),
            "path": _normalize_request_path(request.get("path")),
            "http_version": request.get("http_version"),
            "headers": _normalize_headers(request.get("headers")),
            "body_length": body.get("length"),
            "json_shape": normalize_json_shape(request_json),
            "semantic_summary": _request_semantic_summary(request_json),
        },
        "response": {
            "status": response.get("status"),
            "http_version": response.get("http_version"),
            "headers": _normalize_headers(response.get("headers")),
        },
    }


def _request_semantic_summary(value: Any) -> dict[str, Any]:
    """只输出正文不可逆摘要，供同次入站到出站的语义守恒验证。"""

    if not isinstance(value, dict):
        return {}
    summary = _anthropic_system_semantic_summary(value)
    return {"anthropic_system": summary} if summary else {}


def _semantic_text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text_blocks(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            result.append(text.strip())
    return result


def _anthropic_system_semantic_summary(payload: dict[str, Any]) -> dict[str, Any]:
    system_texts = _text_blocks(payload.get("system"))
    message_digests: list[str] = []
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content_texts = _text_blocks(message.get("content"))
            if content_texts:
                message_digests.append(
                    _semantic_text_digest("\n\n".join(content_texts))
                )
    if not system_texts and not message_digests:
        return {}

    result: dict[str, Any] = {
        "system_digest": (
            _semantic_text_digest("\n\n".join(system_texts))
            if system_texts
            else None
        ),
        "message_digests": message_digests,
    }
    boundary = "\n\n" + _ANTHROPIC_SYSTEM_SPLIT_MARKER
    profile_tail: str | None = None
    if len(system_texts) == 3 and system_texts[2].count(boundary) == 1:
        profile_tail = system_texts[2]
    elif (
        len(system_texts) == 4
        and _ANTHROPIC_SYSTEM_SPLIT_MARKER not in system_texts[2]
        and system_texts[3].startswith(_ANTHROPIC_SYSTEM_SPLIT_MARKER)
    ):
        profile_tail = system_texts[2] + "\n\n" + system_texts[3]
    if profile_tail is not None:
        result["official_profile_tail_digest"] = _semantic_text_digest(profile_tail)
    return result


def normalize_mitm_directory(input_dir: Path, output_path: Path) -> dict[str, Any]:
    """规范化一个 case 的全部 MITM JSONL。"""

    records: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []
    source_files: list[str] = []
    for path in sorted(input_dir.glob("*.jsonl")):
        source_files.append(path.name)
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    value = json.loads(line)
                except ValueError as error:
                    raise ValueError(f"{path}:{line_number} 不是合法 JSONL。") from error
                if isinstance(value, dict):
                    raw_records.append(value)
                    records.append(normalize_mitm_record(value))
    lifecycle_source_files: list[str] = []
    network_lifecycle: list[dict[str, Any]] = []
    for path in sorted(input_dir.glob("lifecycle-*.ndjson")):
        lifecycle_source_files.append(path.name)
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    value = json.loads(line)
                except ValueError as error:
                    raise ValueError(f"{path}:{line_number} 不是合法 NDJSON。") from error
                if not isinstance(value, dict) or value.get("_event") != "request":
                    continue
                network_lifecycle.append(
                    {
                        "event": "request",
                        "flow_id": value.get("_flow_id"),
                        "scenario": value.get("_scenario"),
                        "capture_host_scope": value.get(
                            "_capture_host_scope", "targets"
                        ),
                        "method": value.get("method"),
                        "scheme": value.get("scheme"),
                        "host": _normalized_observed_host(value, value.get("host")),
                        "port": value.get("port"),
                        "path": _normalize_request_path(value.get("path")),
                    }
                )
    payload = {
        "schema_version": "official-client-capture-normalized/v1",
        "source_files": source_files,
        "lifecycle_source_files": lifecycle_source_files,
        "record_count": len(records),
        "records": records,
        "network_lifecycle": network_lifecycle,
        "turn_state_lifecycle": _summarize_turn_state_lifecycle(raw_records),
    }
    secure_write_json(output_path, payload)
    return payload


def _summarize_turn_state_lifecycle(
    records: list[dict[str, Any]],
) -> dict[str, int]:
    """在原始私有证据内核对 turn-state，并只输出不可逆计数。"""

    response_states: list[str] = []
    matched_client_frames = 0
    unmatched_client_frames = 0
    for record in records:
        response = record.get("response")
        response = response if isinstance(response, dict) else {}
        headers = response.get("headers")
        if isinstance(headers, list):
            for item in headers:
                if (
                    isinstance(item, list)
                    and len(item) == 2
                    and str(item[0]).lower() == "x-codex-turn-state"
                    and str(item[1]).strip()
                ):
                    response_states.append(str(item[1]).strip())

        if not record.get("_websocket") or not record.get("from_client"):
            continue
        payload = record.get("json")
        payload = payload if isinstance(payload, dict) else {}
        metadata = payload.get("client_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        frame_state = metadata.get("x-codex-turn-state")
        if not isinstance(frame_state, str) or not frame_state.strip():
            continue
        if frame_state.strip() in response_states:
            matched_client_frames += 1
        else:
            unmatched_client_frames += 1
    return {
        "response_state_count": len(response_states),
        "matched_client_frame_count": matched_client_frames,
        "unmatched_client_frame_count": unmatched_client_frames,
    }


def extract_client_hellos(
    *,
    pcap_path: Path,
    target_hosts: tuple[str, ...],
    tshark_bin: str,
    capture_all_hosts: bool = False,
) -> list[dict[str, Any]]:
    """提取目标 SNI；FW-E 全量模式保留进程发出的全部 ClientHello。"""

    command = [
        tshark_bin,
        "-r",
        str(pcap_path),
        "-Y",
        "tls.handshake.type == 1",
        "-T",
        "fields",
        "-E",
        "separator=/t",
        "-E",
        "occurrence=a",
        "-E",
        "aggregator=,",
    ]
    for field in TSHARK_CLIENT_HELLO_FIELDS:
        command.extend(["-e", field])
    completed = subprocess.run(
        command, text=True, capture_output=True, check=False, timeout=60
    )
    if completed.returncode != 0:
        raise RuntimeError("tshark 无法解析 direct pcap。")
    allowed = {host.lower() for host in target_hosts}
    records: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        columns = line.split("\t")
        columns.extend([""] * (14 - len(columns)))
        (
            frame,
            ipv4,
            ipv6,
            port,
            sni,
            ciphers,
            extensions,
            alpn,
            curves,
            point_formats,
            signature_algorithms,
            supported_versions,
            key_share_groups,
            psk_modes,
        ) = columns[:14]
        if not sni or (not capture_all_hosts and sni.lower() not in allowed):
            continue
        records.append(
            {
                "frame": frame,
                "destination": ipv4 or ipv6,
                "port": port,
                "sni": sni.lower(),
                "cipher_suites": ciphers.split(",") if ciphers else [],
                "extension_types": extensions.split(",") if extensions else [],
                "alpn": alpn.split(",") if alpn else [],
                "curves": curves.split(",") if curves else [],
                "point_formats": point_formats.split(",") if point_formats else [],
                "signature_algorithms": (
                    signature_algorithms.split(",") if signature_algorithms else []
                ),
                "supported_versions": (
                    supported_versions.split(",") if supported_versions else []
                ),
                "key_share_groups": (
                    key_share_groups.split(",") if key_share_groups else []
                ),
                "psk_modes": psk_modes.split(",") if psk_modes else [],
            }
        )
    return records


def validate_tshark_client_hello_fields(
    *, tshark_bin: str, environment: dict[str, str]
) -> tuple[str, ...]:
    """在发出模型请求前确认固定 tshark 能提取完整 TLS Profile。"""

    completed = subprocess.run(
        [tshark_bin, "-G", "fields"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError("tshark 无法列出字段注册表。")
    available = {
        columns[2]
        for line in completed.stdout.splitlines()
        if len(columns := line.split("\t")) >= 3 and columns[0] == "F"
    }
    missing = [field for field in TSHARK_CLIENT_HELLO_FIELDS if field not in available]
    if missing:
        raise RuntimeError("tshark 缺少 TLS 抓包字段：" + ", ".join(missing))
    return TSHARK_CLIENT_HELLO_FIELDS


def normalize_direct_pcap(
    *,
    pcap_path: Path,
    output_path: Path,
    target_hosts: tuple[str, ...],
    tshark_bin: str,
    capture_all_hosts: bool = False,
) -> dict[str, Any]:
    """校验 direct pcap 确实包含目标边界，而不是旧的错误网络命名空间。"""

    if not pcap_path.is_file() or pcap_path.stat().st_size <= 24:
        raise RuntimeError("direct pcap 缺失或为空。")
    client_hellos = extract_client_hellos(
        pcap_path=pcap_path,
        target_hosts=target_hosts,
        tshark_bin=tshark_bin,
        capture_all_hosts=capture_all_hosts,
    )
    if not client_hellos:
        raise RuntimeError(
            "direct pcap 未发现目标 SNI 的 ClientHello；可能抓错网络命名空间。"
        )
    payload = {
        "schema_version": "official-client-capture-tls/v1",
        "target_hosts": list(target_hosts),
        "capture_host_scope": "all" if capture_all_hosts else "targets",
        "client_hello_count": len(client_hellos),
        "client_hellos": client_hellos,
    }
    secure_write_json(output_path, payload)
    return payload


def compare_normalized(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """比较两个已脱敏结构；后续 AnyRouter A/B 复用该入口。"""

    baseline_kind, baseline_records = _comparison_records(baseline)
    candidate_kind, candidate_records = _comparison_records(candidate)
    comparable = baseline_kind == candidate_kind and baseline_kind != "unknown"
    return {
        "schema_version": "official-client-capture-diff/v1",
        "equal": comparable and baseline_records == candidate_records,
        "baseline_evidence_kind": baseline_kind,
        "candidate_evidence_kind": candidate_kind,
        "baseline_record_count": len(baseline_records),
        "candidate_record_count": len(candidate_records),
        "baseline_observation_count": _observation_count(baseline),
        "candidate_observation_count": _observation_count(candidate),
        "baseline_only": [item for item in baseline_records if item not in candidate_records],
        "candidate_only": [item for item in candidate_records if item not in baseline_records],
    }


def compare_official_egress_tls_contract(
    baseline: dict[str, Any], candidate: dict[str, Any], contract: str
) -> dict[str, Any]:
    """只比较目标业务 Transport，辅助连接仍单列计数且不从原始 pcap 删除。"""

    requirements = {
        "anthropic-http": (17, ["http/1.1"]),
        "codex-http": (30, []),
        "codex-ws": (10, []),
    }
    if contract not in requirements:
        raise ValueError(f"未知官方出站 TLS 契约：{contract}")
    cipher_count, expected_alpn = requirements[contract]

    def select(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
        values = payload.get("client_hellos")
        values = values if isinstance(values, list) else []
        selected: list[dict[str, Any]] = []
        valid_observations = 0
        for value in values:
            if not isinstance(value, dict):
                continue
            ciphers = _normalize_tls_codes(value.get("cipher_suites"))
            alpn = value.get("alpn", [])
            if len(ciphers) != cipher_count or alpn != expected_alpn:
                continue
            valid_observations += 1
            record = {
                "target": "<target>",
                "cipher_suites": ciphers,
                "extension_types": _normalize_tls_codes(
                    value.get("extension_types")
                ),
                "alpn": alpn,
                "curves": _normalize_tls_codes(value.get("curves")),
                "point_formats": _normalize_tls_codes(
                    value.get("point_formats")
                ),
                "signature_algorithms": _normalize_tls_codes(
                    value.get("signature_algorithms")
                ),
                "supported_versions": _normalize_tls_codes(
                    value.get("supported_versions")
                ),
                "key_share_groups": _normalize_tls_codes(
                    value.get("key_share_groups")
                ),
                "psk_modes": _normalize_tls_codes(value.get("psk_modes")),
            }
            if contract == "codex-ws":
                # 官方 WS 扩展顺序会逐握手随机化，契约比较集合；其余字段仍按序。
                record["extension_types"] = sorted(record["extension_types"])
            selected.append(record)
        return (
            _deduplicate_records(selected),
            valid_observations,
            len(values) - valid_observations,
        )

    baseline_profiles, baseline_observations, baseline_auxiliary = select(baseline)
    candidate_profiles, candidate_observations, candidate_auxiliary = select(candidate)
    equal = bool(baseline_profiles) and baseline_profiles == candidate_profiles
    return {
        "schema_version": "official-client-oauth-egress-tls-contract-diff/v1",
        "contract": contract,
        "equal": equal,
        "baseline_business_observation_count": baseline_observations,
        "candidate_business_observation_count": candidate_observations,
        "baseline_business_profile_count": len(baseline_profiles),
        "candidate_business_profile_count": len(candidate_profiles),
        "baseline_auxiliary_observation_count": baseline_auxiliary,
        "candidate_auxiliary_observation_count": candidate_auxiliary,
        "baseline_only_profile_count": len(
            [item for item in baseline_profiles if item not in candidate_profiles]
        ),
        "candidate_only_profile_count": len(
            [item for item in candidate_profiles if item not in baseline_profiles]
        ),
    }


def compare_official_egress_contract(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    candidate_ingress: dict[str, Any],
    contract: str,
) -> dict[str, Any]:
    """比较 OAuth 官方出站契约，并单独验证候选入站到出站的语义守恒。

    原始严格比较继续保留，用于发现全部结构差异；跨独立运行只声明对话历史、
    动态身份、响应续链，以及已经由同轮 Set-Cookie→Cookie 证据闭合的冷 Cookie jar。
    其他 Header、路径、协议、Cookie 或固定 Body 差异仍会使验收失败。
    """

    if contract not in OFFICIAL_EGRESS_CONTRACTS:
        raise ValueError(f"未知 OAuth 官方出站契约：{contract}")

    raw = compare_normalized(baseline, candidate)
    baseline_records = _official_egress_contract_records(baseline, contract, "egress")
    candidate_records = _official_egress_contract_records(candidate, contract, "egress")
    ingress_records = _official_egress_contract_records(
        candidate_ingress, contract, "ingress"
    )

    differences: list[str] = []
    difference_paths: list[str] = []
    declared: list[dict[str, Any]] = [
        {
            "kind": "independent_model_run_conversation_history",
            "scope": "messages" if contract == "oauth-claude-http" else "input",
            "reason": "官方基准与候选是两次独立模型运行；候选正文改由同次入站到出站守恒验证。",
        },
        {
            "kind": "dynamic_identity_and_text_length",
            "scope": "normalized_placeholders",
            "reason": "动态身份和正文文本只比较类型与结构，不比较随机值或脱敏前长度。",
        },
    ]
    if contract.endswith("-http"):
        declared.append(
            {
                "kind": "http_header_order",
                "scope": "request.headers",
                "reason": "Header 名称和值严格比较，但 Go/代理运行时的发送顺序不作为画像契约。",
            }
        )
    if contract == "oauth-codex-ws":
        declared.append(
            {
                "kind": "independent_response_lineage",
                "scope": "previous_response_id",
                "reason": "官方基准与候选来自独立会话，响应续链只要求候选入站到出站守恒。",
            }
        )
    if contract in OFFICIAL_EGRESS_CONTRACTS:
        declared.append(
            {
                "kind": "independent_client_tool_catalog",
                "scope": "tools",
                "reason": (
                    "官方与候选是独立 CLI 环境，动态工具目录可不同；"
                    "候选 tools 仍由同次入站到出站守恒验证。"
                ),
            }
        )
    declared.append(
        {
            "kind": "paired_business_request_parameters",
            "scope": "reasoning/text or Anthropic generation parameters",
            "reason": (
                "跨独立运行的业务参数可不同；候选值由同次入站到出站守恒验证，"
                "固定官方字段仍在画像契约中严格比较。"
            ),
        }
    )
    if contract in {"oauth-codex-http", "oauth-codex-compact-http"}:
        cookie_errors, cookie_declaration = _validate_codex_cookie_lifecycle(
            baseline_records, candidate_records
        )
        differences.extend(cookie_errors)
        if cookie_declaration is not None:
            declared.append(cookie_declaration)
    baseline_contract = [
        _canonical_official_egress_record(record, contract)
        for record in baseline_records
    ]
    candidate_contract = [
        _canonical_official_egress_record(record, contract)
        for record in candidate_records
    ]
    if not baseline_records:
        differences.append("官方基准没有匹配到目标模型请求。")
    if not candidate_records:
        differences.append("候选出站没有匹配到目标模型请求。")
    if not ingress_records:
        differences.append("候选入站没有匹配到目标模型请求。")
    if contract.endswith("-http"):
        if Counter(_stable_record_key(item) for item in baseline_contract) != Counter(
            _stable_record_key(item) for item in candidate_contract
        ):
            differences.append("官方基准与候选的固定 HTTP 出站契约不一致。")
            difference_paths.extend(
                _record_sequence_difference_paths(
                    baseline_contract,
                    candidate_contract,
                )
            )
    elif baseline_contract != candidate_contract:
        differences.append("官方基准与候选的固定 WebSocket 帧序列不一致。")
        difference_paths.extend(
            _record_sequence_difference_paths(
                baseline_contract,
                candidate_contract,
            )
        )

    semantic_errors = _candidate_semantic_preservation_errors(
        ingress_records,
        candidate_records,
        contract,
    )
    differences.extend(semantic_errors)

    beta_errors: list[str] = []
    beta_exercised = False
    if contract == "oauth-claude-http":
        beta_errors, beta_exercised = _validate_anthropic_dynamic_beta(
            candidate_records
        )
        differences.extend(beta_errors)

    ws_errors: list[str] = []
    turn_state_errors: list[str] = []
    if contract == "oauth-codex-ws":
        ws_errors = _validate_official_egress_ws_item_turn_metadata(candidate_records)
        differences.extend(ws_errors)
        turn_state_errors = _validate_codex_turn_state_lifecycle(
            baseline,
            candidate,
        )
        differences.extend(turn_state_errors)

    return {
        "schema_version": "official-client-oauth-egress-contract-diff/v1",
        "contract": contract,
        "raw_equal": raw["equal"],
        "contract_equal": not differences,
        "equal": not differences,
        "baseline_record_count": len(baseline_records),
        "candidate_record_count": len(candidate_records),
        "candidate_ingress_record_count": len(ingress_records),
        "candidate_semantic_preserved": not semantic_errors,
        "anthropic_dynamic_beta_exercised": (
            beta_exercised if contract == "oauth-claude-http" else None
        ),
        "anthropic_dynamic_beta_valid": (
            not beta_errors
            if contract == "oauth-claude-http" and beta_exercised
            else None
        ),
        "ws_item_turn_metadata_valid": not ws_errors if contract == "oauth-codex-ws" else None,
        "ws_turn_state_lifecycle_valid": not turn_state_errors if contract == "oauth-codex-ws" else None,
        "declared_differences": declared,
        "undeclared_differences": differences,
        "contract_difference_paths": difference_paths,
        "raw_diff": {
            "schema_version": raw["schema_version"],
            "equal": raw["equal"],
            "baseline_evidence_kind": raw["baseline_evidence_kind"],
            "candidate_evidence_kind": raw["candidate_evidence_kind"],
            "baseline_record_count": raw["baseline_record_count"],
            "candidate_record_count": raw["candidate_record_count"],
            "baseline_only_count": len(raw["baseline_only"]),
            "candidate_only_count": len(raw["candidate_only"]),
        },
    }


def _official_egress_contract_records(
    payload: dict[str, Any], contract: str, boundary: str
) -> list[dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        return []
    selected: list[dict[str, Any]] = []
    if contract == "oauth-claude-http":
        expected_path = "/v1/messages"
    elif contract == "oauth-codex-http":
        expected_path = (
            "/v1/responses" if boundary == "ingress" else "/backend-api/codex/responses"
        )
    elif contract == "oauth-codex-compact-http":
        expected_path = (
            "/v1/responses/compact"
            if boundary == "ingress"
            else "/backend-api/codex/responses/compact"
        )
    else:
        expected_path = (
            "/v1/responses" if boundary == "ingress" else "/backend-api/codex/responses"
        )
    for value in records:
        if not isinstance(value, dict):
            continue
        if contract.endswith("-http"):
            if value.get("kind") != "http_exchange":
                continue
            request = value.get("request")
            request = request if isinstance(request, dict) else {}
            if request.get("method") == "POST" and _request_path_matches(
                request.get("path"), expected_path
            ):
                selected.append(value)
            continue
        if (
            value.get("kind") == "websocket_frame"
            and value.get("from_client")
            and _request_path_matches(value.get("path"), expected_path)
        ):
            selected.append(value)
    return selected


def _record_has_header(record: dict[str, Any], section: str, name: str) -> bool:
    value = record.get(section)
    value = value if isinstance(value, dict) else {}
    headers = value.get("headers")
    if not isinstance(headers, list):
        return False
    expected = name.lower()
    return any(
        isinstance(item, list)
        and len(item) == 2
        and str(item[0]).lower() == expected
        for item in headers
    )


def _validate_codex_cookie_lifecycle(
    baseline_records: list[dict[str, Any]],
    candidate_records: list[dict[str, Any]],
) -> tuple[list[str], dict[str, Any] | None]:
    """验证冷 jar 必须由同轮 Set-Cookie 建立并在后续请求回放。"""

    if len(baseline_records) != len(candidate_records):
        return [], None
    baseline_cookie = [
        _record_has_header(record, "request", "cookie")
        for record in baseline_records
    ]
    candidate_cookie = [
        _record_has_header(record, "request", "cookie")
        for record in candidate_records
    ]
    if baseline_cookie == candidate_cookie:
        return [], None
    cold_jar_closed = (
        len(candidate_records) >= 2
        and all(baseline_cookie)
        and not candidate_cookie[0]
        and all(candidate_cookie[1:])
        and _record_has_header(candidate_records[0], "response", "set-cookie")
    )
    if cold_jar_closed:
        return [], {
            "kind": "cold_cookie_jar_bootstrap",
            "scope": "request.headers.cookie",
            "reason": "候选进程内 jar 初始为空；首个响应实证 Set-Cookie，后续同账号请求均已回放 Cookie。",
        }
    return ["Codex Cookie 生命周期与官方基准不一致，且未形成首响应建立、后续请求回放的闭环。"], None


def _canonical_official_egress_record(
    record: dict[str, Any], contract: str
) -> dict[str, Any]:
    value = _canonical_normalized_placeholders(deepcopy(record))
    if contract.endswith("-http"):
        request = value.get("request")
        request = request if isinstance(request, dict) else {}
        request.pop("body_length", None)
        headers = request.get("headers")
        if isinstance(headers, list):
            headers = [
                [
                    str(item[0]).lower(),
                    _canonical_contract_header_value(
                        contract,
                        str(item[0]),
                        redact_header_value(str(item[0]), str(item[1])),
                    ),
                ]
                for item in headers
                if isinstance(item, list) and len(item) == 2
                and not (
                    contract
                    in {"oauth-codex-http", "oauth-codex-compact-http"}
                    and str(item[0]).lower() == "cookie"
                )
            ]
            request["headers"] = sorted(headers, key=_stable_record_key)
        shape = request.get("json_shape")
        if isinstance(shape, dict):
            if contract == "oauth-claude-http":
                shape["messages_cache_profile"] = _values_for_key(
                    shape.get("messages"), "cache_control"
                )
                for key in (
                    "context_management",
                    "max_tokens",
                    "output_config",
                    "stop_sequences",
                    "stream",
                    "temperature",
                    "thinking",
                    "tool_choice",
                    "top_k",
                    "top_p",
                ):
                    shape[key] = "<paired-candidate-semantic>"
            else:
                shape["reasoning"] = "<paired-candidate-semantic>"
                shape["text"] = "<paired-candidate-semantic>"
            shape.pop("messages" if contract == "oauth-claude-http" else "input", None)
            shape["tools"] = "<paired-candidate-semantic>"
        return {
            "kind": "http_exchange",
            "request": {
                "method": request.get("method"),
                "host": request.get("host"),
                "path": request.get("path"),
                "http_version": request.get("http_version"),
                "headers": request.get("headers", []),
                "json_shape": shape,
            },
        }

    shape = value.get("json_shape")
    shape = shape if isinstance(shape, dict) else {}
    prewarm = shape.get("generate") is False
    input_items = shape.get("input")
    if prewarm:
        input_contract = []
        if isinstance(input_items, list):
            for item in input_items:
                item = item if isinstance(item, dict) else {}
                input_contract.append(
                    {"type": item.get("type"), "role": item.get("role")}
                )
    else:
        input_contract = "<paired-candidate-semantic>"
    shape["input"] = input_contract
    shape["tools"] = "<paired-candidate-semantic>"
    shape["reasoning"] = "<paired-candidate-semantic>"
    shape["text"] = "<paired-candidate-semantic>"
    shape.pop("previous_response_id", None)
    return {
        "kind": "websocket_frame",
        "host": value.get("host"),
        "path": value.get("path"),
        "json_shape": shape,
    }


def _canonical_contract_header_value(
    contract: str, name: str, value: str
) -> str:
    """从固定画像比较中移除已由功能门禁独立验证的动态 beta。"""

    if contract != "oauth-claude-http" or name.lower() != "anthropic-beta":
        return value
    dynamic_tokens = {"advanced-tool-use-2025-11-20"}
    tokens = [
        token.strip()
        for token in value.split(",")
        if token.strip() and token.strip() not in dynamic_tokens
    ]
    return ",".join(tokens)


def _candidate_semantic_preservation_errors(
    ingress_records: list[dict[str, Any]],
    egress_records: list[dict[str, Any]],
    contract: str,
) -> list[str]:
    if len(ingress_records) != len(egress_records):
        return [
            "候选入站与出站的模型请求数量不一致："
            f"{len(ingress_records)} != {len(egress_records)}。"
        ]
    field = "messages" if contract == "oauth-claude-http" else "input"
    errors: list[str] = []
    for index, (ingress, egress) in enumerate(zip(ingress_records, egress_records)):
        if contract.endswith("-http"):
            ingress_request = ingress.get("request")
            ingress_request = ingress_request if isinstance(ingress_request, dict) else {}
            egress_request = egress.get("request")
            egress_request = egress_request if isinstance(egress_request, dict) else {}
            ingress_shape = ingress_request.get("json_shape")
            egress_shape = egress_request.get("json_shape")
        else:
            ingress_shape = ingress.get("json_shape")
            egress_shape = egress.get("json_shape")
        ingress_shape = ingress_shape if isinstance(ingress_shape, dict) else {}
        egress_shape = egress_shape if isinstance(egress_shape, dict) else {}
        ingress_semantic = deepcopy(ingress_shape.get(field))
        egress_semantic = deepcopy(egress_shape.get(field))
        if contract == "oauth-claude-http":
            _remove_key_recursive(ingress_semantic, "cache_control")
            _remove_key_recursive(egress_semantic, "cache_control")
        if contract == "oauth-codex-ws":
            _remove_ws_item_turn_metadata(ingress_semantic)
            _remove_ws_item_turn_metadata(egress_semantic)
        anthropic_system_preserved = (
            contract != "oauth-claude-http"
            or _anthropic_system_semantic_preserved(
                ingress,
                egress,
                ingress_shape,
                egress_shape,
            )
        )
        message_migration_preserved = (
            contract == "oauth-claude-http"
            and anthropic_system_preserved
            and _sequence_has_suffix(egress_semantic, ingress_semantic)
        )
        if ingress_semantic != egress_semantic and not message_migration_preserved:
            errors.append(f"候选第 {index + 1} 条请求的 {field} 入站到出站语义不守恒。")
        if not anthropic_system_preserved:
            errors.append(f"候选第 {index + 1} 条请求的 system 语义未在出站 system/messages 中保留。")
        allow_sampling_removal = (
            contract == "oauth-claude-http"
            and _anthropic_thinking_enabled(ingress_shape)
        )
        ingress_top_level = _semantic_top_level_shape(
            ingress_shape, contract, allow_sampling_removal
        )
        egress_top_level = _semantic_top_level_shape(
            egress_shape, contract, allow_sampling_removal
        )
        if ingress_top_level != egress_top_level:
            difference_paths: list[str] = []
            _collect_difference_paths(
                ingress_top_level,
                egress_top_level,
                "$",
                difference_paths,
                20,
            )
            paths = ", ".join(difference_paths) or "$"
            errors.append(
                f"候选第 {index + 1} 条请求的顶层参数入站到出站不守恒：{paths}。"
            )
    return errors


def _sequence_has_suffix(value: Any, suffix: Any) -> bool:
    """判断 Anthropic system 迁移后，原 messages 是否仍完整保留在尾部。"""

    if not isinstance(value, list) or not isinstance(suffix, list):
        return False
    if len(suffix) > len(value):
        return False
    if not suffix:
        return True
    return value[-len(suffix) :] == suffix


def _anthropic_system_semantic_preserved(
    ingress_record: dict[str, Any],
    egress_record: dict[str, Any],
    ingress_shape: dict[str, Any],
    egress_shape: dict[str, Any],
) -> bool:
    """验证官方 system 分段或第三方 system 迁移后的正文摘要保持一致。"""

    ingress_summary = _anthropic_system_summary_from_record(ingress_record)
    egress_summary = _anthropic_system_summary_from_record(egress_record)
    if ingress_summary:
        ingress_tail = ingress_summary.get("official_profile_tail_digest")
        if isinstance(ingress_tail, str) and ingress_tail:
            return ingress_tail == egress_summary.get("official_profile_tail_digest")
        ingress_digest = ingress_summary.get("system_digest")
        if not isinstance(ingress_digest, str) or not ingress_digest:
            return True
        if ingress_digest == egress_summary.get("system_digest"):
            return True
        message_digests = egress_summary.get("message_digests")
        return isinstance(message_digests, list) and ingress_digest in message_digests

    ingress_lengths = _normalized_text_lengths(ingress_shape.get("system"))
    if not ingress_lengths:
        return True
    # 三块及以上无法仅凭脱敏后的长度判断 split marker，旧证据必须明确失败，
    # 不能继续用“看起来像官方结构”作为语义守恒结论。
    if len(ingress_lengths) >= 3:
        return False
    expected_lengths = set(ingress_lengths)
    if len(ingress_lengths) > 1:
        expected_lengths.add(sum(ingress_lengths) + 2 * (len(ingress_lengths) - 1))
    egress_lengths = set(_normalized_text_lengths(egress_shape.get("system")))
    egress_lengths.update(_normalized_text_lengths(egress_shape.get("messages")))
    return bool(expected_lengths & egress_lengths)


def _anthropic_system_summary_from_record(record: dict[str, Any]) -> dict[str, Any]:
    request = record.get("request")
    request = request if isinstance(request, dict) else {}
    semantic = request.get("semantic_summary")
    semantic = semantic if isinstance(semantic, dict) else {}
    summary = semantic.get("anthropic_system")
    return summary if isinstance(summary, dict) else {}


def _normalized_text_lengths(value: Any) -> list[int]:
    result: list[int] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"text", "content", "system", "instructions"}:
                result.extend(_normalized_text_lengths(item))
            elif isinstance(item, (dict, list)):
                result.extend(_normalized_text_lengths(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_normalized_text_lengths(item))
    elif isinstance(value, str):
        match = re.fullmatch(r"<text:(\d+)>", value)
        if match:
            result.append(int(match.group(1)))
    return result


def _validate_anthropic_dynamic_beta(
    records: list[dict[str, Any]],
) -> tuple[list[str], bool]:
    errors: list[str] = []
    exercised = False
    for index, record in enumerate(records):
        request = record.get("request")
        request = request if isinstance(request, dict) else {}
        shape = request.get("json_shape")
        shape = shape if isinstance(shape, dict) else {}
        tools = shape.get("tools")
        requires_advanced = _shape_requires_advanced_tool_use(tools)
        exercised = exercised or requires_advanced
        if requires_advanced and not _record_header_contains(
            record,
            "request",
            "anthropic-beta",
            "advanced-tool-use-2025-11-20",
        ):
            errors.append(f"候选第 {index + 1} 条 Anthropic tool search 请求缺少 advanced-tool-use beta。")
    return errors, exercised


def _shape_requires_advanced_tool_use(value: Any) -> bool:
    if isinstance(value, dict):
        tool_type = str(value.get("type", "")).lower()
        if "tool_search" in tool_type or value.get("defer_loading") is True:
            return True
        custom = value.get("custom")
        if isinstance(custom, dict) and custom.get("defer_loading") is True:
            return True
        return any(_shape_requires_advanced_tool_use(item) for item in value.values())
    if isinstance(value, list):
        return any(_shape_requires_advanced_tool_use(item) for item in value)
    return False


def _record_header_contains(
    record: dict[str, Any],
    section: str,
    name: str,
    token: str,
) -> bool:
    value = record.get(section)
    value = value if isinstance(value, dict) else {}
    headers = value.get("headers")
    if not isinstance(headers, list):
        return False
    expected = name.lower()
    return any(
        isinstance(item, list)
        and len(item) == 2
        and str(item[0]).lower() == expected
        and token in str(item[1])
        for item in headers
    )


def _validate_codex_turn_state_lifecycle(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    baseline_lifecycle = baseline.get("turn_state_lifecycle")
    candidate_lifecycle = candidate.get("turn_state_lifecycle")
    if not isinstance(baseline_lifecycle, dict):
        return ["官方 WS 基准缺少 turn-state 生命周期摘要。"]
    if not isinstance(candidate_lifecycle, dict):
        return ["候选 WS 证据缺少 turn-state 生命周期摘要。"]
    baseline_response_count = int(
        baseline_lifecycle.get("response_state_count", 0) or 0
    )
    response_count = int(candidate_lifecycle.get("response_state_count", 0) or 0)
    matched_count = int(
        candidate_lifecycle.get("matched_client_frame_count", 0) or 0
    )
    unmatched_count = int(
        candidate_lifecycle.get("unmatched_client_frame_count", 0) or 0
    )
    errors: list[str] = []
    if baseline_response_count == 0:
        if response_count:
            errors.append("官方 WS 基准未下发 turn-state，但候选观察到了额外状态。")
        if matched_count:
            errors.append("官方 WS 基准无 turn-state，候选却回放了额外状态。")
    else:
        if response_count < 1:
            errors.append("官方 WS 基准下发了 turn-state，但候选握手响应未观察到。")
        if matched_count < 1:
            errors.append("候选 WS 后续 client_metadata 未回放握手 turn-state。")
    if unmatched_count:
        errors.append("候选 WS client_metadata 携带了无法追溯到握手响应的 turn-state。")
    return errors


def _semantic_top_level_shape(
    shape: dict[str, Any],
    contract: str,
    allow_anthropic_sampling_removal: bool = False,
) -> dict[str, Any]:
    """返回必须从同次入站保留到出站的顶层请求参数。"""

    value = deepcopy(shape)
    if contract == "oauth-claude-http":
        ignored = {
            "messages",
            "system",
            "metadata",
        }
        if allow_anthropic_sampling_removal:
            # 只有入口明确开启 thinking 时，官方协议才要求清理这些采样参数。
            # 非 thinking 场景必须逐字段比较，避免误删被“语义守恒”豁免。
            ignored.update({"temperature", "top_p", "top_k"})
    else:
        ignored = {
            "input",
            # 这两项由官方身份生命周期派生，不能要求第三方入站预先提供。
            "client_metadata",
            "prompt_cache_key",
            # 下列字段属于 Codex 官方客户端的固定出站契约，候选必须按官方
            # 画像定型，不能反过来要求保留第三方客户端传入的非官方值。
            "parallel_tool_calls",
            "store",
            "stream",
            "max_output_tokens",
            "tool_choice",
            "include",
        }
    for key in ignored:
        value.pop(key, None)
    if contract != "oauth-claude-http":
        reasoning = value.get("reasoning")
        if isinstance(reasoning, dict):
            # reasoning.context 仅用于 Lite 派生链；官方非 Lite 不序列化该字段。
            reasoning.pop("context", None)
    return value


def _anthropic_thinking_enabled(shape: dict[str, Any]) -> bool:
    thinking = shape.get("thinking")
    if not isinstance(thinking, dict):
        return False
    value = thinking.get("type")
    return isinstance(value, str) and value.strip().lower() in {"enabled", "adaptive"}


def _validate_official_egress_ws_item_turn_metadata(
    records: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    for record_index, record in enumerate(records):
        shape = record.get("json_shape")
        shape = shape if isinstance(shape, dict) else {}
        items = shape.get("input")
        if not isinstance(items, list):
            errors.append(f"候选第 {record_index + 1} 条 WS 帧缺少 input 数组。")
            continue
        prewarm = shape.get("generate") is False
        for item_index, item in enumerate(items):
            item = item if isinstance(item, dict) else {}
            metadata = item.get("internal_chat_message_metadata_passthrough")
            if prewarm:
                if metadata is not None:
                    errors.append(
                        f"候选第 {record_index + 1} 条 WS 预热帧的第 {item_index + 1} 项不应携带逐项 Turn 元数据。"
                    )
                continue
            if not isinstance(metadata, dict) or "turn_id" not in metadata:
                errors.append(
                    f"候选第 {record_index + 1} 条 WS 业务帧的第 {item_index + 1} 项缺少 turn_id。"
                )
    return errors


def _remove_ws_item_turn_metadata(value: Any) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            item.pop("internal_chat_message_metadata_passthrough", None)


def _remove_key_recursive(value: Any, target_key: str) -> None:
    if isinstance(value, dict):
        value.pop(target_key, None)
        for item in value.values():
            _remove_key_recursive(item, target_key)
    elif isinstance(value, list):
        for item in value:
            _remove_key_recursive(item, target_key)


def _values_for_key(value: Any, target_key: str) -> list[Any]:
    result: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == target_key:
                result.append(item)
            else:
                result.extend(_values_for_key(item, target_key))
    elif isinstance(value, list):
        for item in value:
            result.extend(_values_for_key(item, target_key))
    return result


def _canonical_normalized_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical_normalized_placeholders(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonical_normalized_placeholders(item) for item in value]
    if isinstance(value, str):
        match = _NORMALIZED_PLACEHOLDER_RE.match(value)
        if match:
            return f"<{match.group(1)}>"
    return value


def _http_cookie_presence(records: list[dict[str, Any]]) -> list[bool]:
    result: list[bool] = []
    for record in records:
        request = record.get("request")
        request = request if isinstance(request, dict) else {}
        headers = request.get("headers")
        headers = headers if isinstance(headers, list) else []
        result.append(
            any(
                isinstance(item, list)
                and len(item) == 2
                and str(item[0]).lower() == "cookie"
                for item in headers
            )
        )
    return result


def _stable_record_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_path_matches(value: Any, expected_path: str) -> bool:
    if not isinstance(value, str):
        return False
    return urllib.parse.urlsplit(value).path == expected_path


def _record_sequence_difference_paths(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]
) -> list[str]:
    paths: list[str] = []
    if len(baseline) != len(candidate):
        paths.append("$.record_count")
    for index, (baseline_record, candidate_record) in enumerate(
        zip(baseline, candidate)
    ):
        _collect_difference_paths(
            baseline_record,
            candidate_record,
            f"$.records[{index}]",
            paths,
            40,
        )
        if len(paths) >= 40:
            break
    return paths


def _collect_difference_paths(
    baseline: Any,
    candidate: Any,
    path: str,
    result: list[str],
    limit: int,
) -> None:
    if len(result) >= limit or baseline == candidate:
        return
    if isinstance(baseline, dict) and isinstance(candidate, dict):
        for key in sorted(set(baseline) | set(candidate)):
            child = f"{path}.{key}"
            if key not in baseline or key not in candidate:
                result.append(child)
                if len(result) >= limit:
                    return
                continue
            _collect_difference_paths(
                baseline[key], candidate[key], child, result, limit
            )
            if len(result) >= limit:
                return
        return
    if isinstance(baseline, list) and isinstance(candidate, list):
        if len(baseline) != len(candidate):
            result.append(f"{path}.length")
            if len(result) >= limit:
                return
        for index, (baseline_item, candidate_item) in enumerate(
            zip(baseline, candidate)
        ):
            _collect_difference_paths(
                baseline_item,
                candidate_item,
                f"{path}[{index}]",
                result,
                limit,
            )
            if len(result) >= limit:
                return
        return
    result.append(path)


def _comparison_records(payload: dict[str, Any]) -> tuple[str, list[Any]]:
    """选择同类证据的稳定比较字段，排除帧号和目标 IP 等运行时噪声。"""

    records = payload.get("records")
    if isinstance(records, list):
        normalized_records: list[dict[str, Any]] = []
        for value in records:
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            if kind == "http_exchange":
                request = value.get("request")
                request = request if isinstance(request, dict) else {}
                normalized_records.append(
                    {
                        "kind": "http_exchange",
                        "request": {
                            "method": request.get("method"),
                            "host": request.get("host"),
                            "path": request.get("path"),
                            "http_version": request.get("http_version"),
                            "headers": request.get("headers", []),
                            "json_shape": request.get("json_shape"),
                        },
                    }
                )
            elif kind == "websocket_frame" and value.get("from_client"):
                normalized_records.append(
                    {
                        "kind": "websocket_frame",
                        "host": value.get("host"),
                        "path": value.get("path"),
                        "json_shape": value.get("json_shape"),
                    }
                )
        return "mitm", normalized_records

    client_hellos = payload.get("client_hellos")
    if isinstance(client_hellos, list):
        normalized: list[dict[str, Any]] = []
        for value in client_hellos:
            if not isinstance(value, dict):
                continue
            normalized.append(
                {
                    "target": "<target>",
                    "cipher_suites": _normalize_tls_codes(
                        value.get("cipher_suites")
                    ),
                    "extension_types": _normalize_tls_codes(
                        value.get("extension_types")
                    ),
                    "alpn": value.get("alpn", []),
                    "curves": _normalize_tls_codes(value.get("curves")),
                    "point_formats": _normalize_tls_codes(
                        value.get("point_formats")
                    ),
                    "signature_algorithms": _normalize_tls_codes(
                        value.get("signature_algorithms")
                    ),
                    "supported_versions": _normalize_tls_codes(
                        value.get("supported_versions")
                    ),
                    "key_share_groups": _normalize_tls_codes(
                        value.get("key_share_groups")
                    ),
                    "psk_modes": _normalize_tls_codes(value.get("psk_modes")),
                }
            )
        return "direct_tls", _deduplicate_records(normalized)

    return "unknown", []


def _deduplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """TLS 重连次数单列为观察量，画像比较只保留唯一结构。"""

    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        key = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        unique[key] = record
    return [unique[key] for key in sorted(unique)]


def _observation_count(payload: dict[str, Any]) -> int:
    """返回原始规范化观察数，不把连接重试混入画像相等判断。"""

    for key in ("records", "client_hellos"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _normalize_tls_codes(value: Any) -> list[str]:
    """将 TLS GREASE 编号替换为稳定占位符，其余值保持原有文本。"""

    if not isinstance(value, list):
        return []
    return ["<grease>" if _is_grease(item) else str(item) for item in value]


def _is_grease(value: Any) -> bool:
    """识别十进制、十六进制及 tshark 注释形态中的 RFC 8701 GREASE。"""

    text = str(value).strip().lower()
    try:
        if re.fullmatch(r"\d+", text):
            number = int(text, 10)
        elif re.fullmatch(r"0x[0-9a-f]+", text):
            number = int(text, 16)
        else:
            match = re.search(r"0x([0-9a-f]{4})", text)
            if not match:
                return False
            number = int(match.group(1), 16)
    except ValueError:
        return False
    high_byte, low_byte = divmod(number, 256)
    return 0 <= number <= 0xFFFF and high_byte == low_byte and low_byte & 0x0F == 0x0A
