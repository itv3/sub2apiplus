"""MITM JSONL 与 direct pcap 的脱敏规范化。"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
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
