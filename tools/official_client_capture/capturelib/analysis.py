"""MITM JSONL 与 direct pcap 的脱敏规范化。"""

from __future__ import annotations

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
    "oauth-codex-ws",
)

_NORMALIZED_PLACEHOLDER_RE = re.compile(r"^<(dynamic|text|string):[^>]+>$")


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
            "host": "<target-host>",
            "path": _normalize_request_path(record.get("path")),
            "length": record.get("length"),
            "json_shape": normalize_json_shape(record.get("json")),
        }
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    return {
        "kind": "http_exchange",
        "task": record.get("_task"),
        "boundary": record.get("_boundary"),
        "subject": record.get("_subject"),
        "scenario": record.get("_scenario"),
        "request": {
            "method": request.get("method"),
            "host": "<target-host>",
            "path": _normalize_request_path(request.get("path")),
            "http_version": request.get("http_version"),
            "headers": _normalize_headers(request.get("headers")),
            "body_length": body.get("length"),
            "json_shape": normalize_json_shape(body.get("json")),
        },
        "response": {
            "status": response.get("status"),
            "http_version": response.get("http_version"),
        },
    }


def normalize_mitm_directory(input_dir: Path, output_path: Path) -> dict[str, Any]:
    """规范化一个 case 的全部 MITM JSONL。"""

    records: list[dict[str, Any]] = []
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
                    records.append(normalize_mitm_record(value))
    payload = {
        "schema_version": "official-client-capture-normalized/v1",
        "source_files": source_files,
        "record_count": len(records),
        "records": records,
    }
    secure_write_json(output_path, payload)
    return payload


def extract_client_hellos(
    *, pcap_path: Path, target_hosts: tuple[str, ...], tshark_bin: str
) -> list[dict[str, Any]]:
    """只提取目标 SNI 的 ClientHello 稳定字段。"""

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
        if sni.lower() not in allowed:
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
) -> dict[str, Any]:
    """校验 direct pcap 确实包含目标边界，而不是旧的错误网络命名空间。"""

    if not pcap_path.is_file() or pcap_path.stat().st_size <= 24:
        raise RuntimeError("direct pcap 缺失或为空。")
    client_hellos = extract_client_hellos(
        pcap_path=pcap_path, target_hosts=target_hosts, tshark_bin=tshark_bin
    )
    if not client_hellos:
        raise RuntimeError(
            "direct pcap 未发现目标 SNI 的 ClientHello；可能抓错网络命名空间。"
        )
    payload = {
        "schema_version": "official-client-capture-tls/v1",
        "target_hosts": list(target_hosts),
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


def compare_official_egress_contract(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    candidate_ingress: dict[str, Any],
    contract: str,
) -> dict[str, Any]:
    """比较 OAuth 官方出站契约，并单独验证候选入站到出站的语义守恒。

    原始严格比较继续保留，用于发现全部结构差异；契约比较只声明两类不能跨独立
    模型运行逐值比较的因素：对话历史，以及 Codex HTTP 的运行态 Cookie jar。
    任何其他 Header、路径、协议或固定 Body 差异仍会使验收失败。
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
    if contract == "oauth-codex-http":
        cookie_difference = _http_cookie_presence(baseline_records) != _http_cookie_presence(
            candidate_records
        )
        if cookie_difference:
            declared.append(
                {
                    "kind": "runtime_cookie_jar",
                    "scope": "request.headers.cookie",
                    "reason": "Cookie 由 Codex CLI 的辅助 ChatGPT 请求建立，Sub2API 模型转发链不伪造运行态 Cookie。",
                }
            )

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

    ws_errors: list[str] = []
    if contract == "oauth-codex-ws":
        ws_errors = _validate_official_egress_ws_item_turn_metadata(candidate_records)
        differences.extend(ws_errors)

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
        "ws_item_turn_metadata_valid": not ws_errors if contract == "oauth-codex-ws" else None,
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
                [str(item[0]).lower(), redact_header_value(str(item[0]), str(item[1]))]
                for item in headers
                if isinstance(item, list) and len(item) == 2
            ]
            if contract == "oauth-codex-http":
                headers = [
                    item
                    for item in headers
                    if not (
                        isinstance(item, list)
                        and len(item) == 2
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
            shape.pop("messages" if contract == "oauth-claude-http" else "input", None)
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
    return {
        "kind": "websocket_frame",
        "host": value.get("host"),
        "path": value.get("path"),
        "json_shape": shape,
    }


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
        if ingress_semantic != egress_semantic:
            errors.append(f"候选第 {index + 1} 条请求的 {field} 入站到出站语义不守恒。")
    return errors


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
