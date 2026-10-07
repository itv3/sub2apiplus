"""Claude 评估器（升级控制面设计 B4-03）测试共用的合成夹具。

不依赖任何真实取证数据，在临时目录里按 Claude 证据形态造一份「官方／候选」两侧证据：

* ``print`` probe：一条连接两个请求（``/v1/messages`` 明文 JSON 与 ``/v1/messages/count_tokens`` gzip 正文），带等长脱敏的
  Authorization，同连接两个上游响应（SSE 与 JSON），加中继清单 ``relay-manifest.json`` 与连接索引 ``relay/relay.json``；
  场景 ``transport.http``。
* ``failure-529`` probe：两条连接（第一条被中继按合成计划回 529，第二条是客户端隔 600 毫秒以 chunked 正文重试并收到
  chunked SSE），``relay/intervention.jsonl`` 记一条合成动作，加中继清单与连接索引；场景 ``failure.529``。
* ``tls`` probe：P 通道 ``tls-clienthello.pcap``，两个 ClientHello（无 ALPN）与一个不带载荷的 TCP 包；场景
  ``transport.native_tls``。

文件名与目录层次照真实证据标签规则（``claude_upgrade_evidence_labels_<版本>.json``）：中继请求字节与干预记录同在
``relay/`` 下，checker 按同目录把干预记录挂到连接上。上游响应字节与连接索引不进标签，由 ``bind_capture_manifest`` 从
封存绑定补成伴随文件。

断言画像由 checker 的 ``build_expectations`` 从合成台账、场景清单与判据模板生成（模板里有 ``$release`` 引用），四条规则：
``SPEC-HDR-001``（两条原子断言）、``SPEC-FAIL-001``、``SPEC-TLS-001`` 三条只选线上记录（``dual_wire``），``SPEC-META-001``
选取证运行时记录（``candidate_profile``）。台账里另有一条 scenario-only 断言，生成时必须被排除。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_rule_assertion as assertion

TARGET_VERSION = "2.1.285"
CAMPAIGN_ID = "claude-code-2_1_285-evaluator-fixture"
OWNER_NONCE = "8" * 64
AUTHORITY = {"assertion_profile_sha256": "a" * 64, "classification_package_digest": "b" * 64, "review_sha256": "c" * 64}
RULES = ["SPEC-FAIL-001", "SPEC-HDR-001", "SPEC-META-001", "SPEC-TLS-001"]
DUAL_WIRE_RULES = ["SPEC-FAIL-001", "SPEC-HDR-001", "SPEC-TLS-001"]
UPSTREAM_IP = "160.79.104.10"

SCENARIOS_PAYLOAD: dict[str, Any] = {
    "schema_version": assertion.SCENARIO_MANIFEST_SCHEMA,
    "claude_version": TARGET_VERSION,
    "evidence_scenarios": [
        {
            "scenario_id": "transport.http",
            "purpose": "rule_evidence",
            "description": "HTTP/1.1 请求形态",
            "trigger": "合成夹具：print probe",
            "preconditions": ["合成夹具不联网"],
            "probe_ids": ["print"],
            "required_artifact_kinds": ["relay_binary", "relay_manifest"],
            "covers": ["SPEC-HDR-001", "SPEC-META-001"],
        },
        {
            "scenario_id": "failure.529",
            "purpose": "rule_evidence",
            "description": "529 过载后的重试",
            "trigger": "合成夹具：failure-529 probe",
            "preconditions": ["合成夹具不联网"],
            "probe_ids": ["failure-529"],
            "required_artifact_kinds": ["relay_binary", "relay_manifest", "synthetic_intervention"],
            "covers": ["SPEC-FAIL-001"],
        },
        {
            "scenario_id": "transport.native_tls",
            "purpose": "rule_evidence",
            "description": "原生 TLS ClientHello",
            "trigger": "合成夹具：tls probe",
            "preconditions": ["合成夹具不联网"],
            "probe_ids": ["tls"],
            "required_artifact_kinds": ["pcap"],
            "covers": ["SPEC-TLS-001"],
        },
        {
            "scenario_id": "aux.usage",
            "purpose": "discovery",
            "description": "没有任何规则引用的场景，生成画像时不得出现",
            "trigger": "合成夹具",
            "preconditions": [],
            "probe_ids": [],
            "required_artifact_kinds": ["relay_manifest"],
            "covers": [],
        },
    ],
}

LEDGER: dict[str, Any] = {
    "schema_version": assertion.ATOMIC_LEDGER_SCHEMA,
    "campaign_id": CAMPAIGN_ID,
    "target_version": TARGET_VERSION,
    "scenario_only_owner_ids": ["SCENARIO-LOCAL-REJECTION"],
    "assertions": [
        {"assertion_id": "SPEC-HDR-001", "owner_kind": "required_rule", "owner_id": "SPEC-HDR-001", "evidence_item_ids": ["EVIDENCE-print"], "conditions": [], "channels": ["R"], "result": "passed"},
        {"assertion_id": "SPEC-HDR-002", "owner_kind": "required_rule", "owner_id": "SPEC-HDR-001", "evidence_item_ids": ["EVIDENCE-print"], "conditions": [], "channels": ["R"], "result": "passed"},
        {"assertion_id": "SPEC-FAIL-001", "owner_kind": "required_rule", "owner_id": "SPEC-FAIL-001", "evidence_item_ids": ["EVIDENCE-failure-529"], "conditions": [], "channels": ["R"], "result": "passed"},
        {"assertion_id": "SPEC-TLS-001", "owner_kind": "required_rule", "owner_id": "SPEC-TLS-001", "evidence_item_ids": ["EVIDENCE-tls"], "conditions": [], "channels": ["P"], "result": "passed"},
        {"assertion_id": "SPEC-META-001", "owner_kind": "required_rule", "owner_id": "SPEC-META-001", "evidence_item_ids": ["EVIDENCE-print"], "conditions": [], "channels": ["M"], "result": "passed"},
        {"assertion_id": "SPEC-STATE-005", "owner_kind": "scenario_only", "owner_id": "SCENARIO-LOCAL-REJECTION", "evidence_item_ids": [], "conditions": [], "channels": ["M"], "result": "passed"},
    ],
}

RELEASE_WIRE: dict[str, Any] = {"headers": {"anthropic-version": "2023-06-01"}, "tls": {"server_name": "api.anthropic.com"}}

TEMPLATES: dict[str, Any] = {
    "schema_version": assertion.TEMPLATES_SCHEMA_VERSION,
    "description": "B4-03 合成夹具的判据模板",
    "templates": {
        "SPEC-HDR-001": {
            "description": "Messages 请求的版本头取目标 Release 的 Wire 值，Authorization 只以等长脱敏形态出现",
            "checks": [
                {
                    "id": "version-header",
                    "description": "anthropic-version 头等于 Release Wire 声明值",
                    "select": {"record_type": "claude_http_request", "where": [{"path": "data.path", "operator": "equal", "value": "/v1/messages"}]},
                    "assertion": {"operator": "all_equal", "path": "data.header_values.anthropic-version", "value": [{"$release": "wire:/headers/anthropic-version"}]},
                },
                {
                    "id": "authorization-shape",
                    "description": "Authorization 是 Bearer 且已等长脱敏",
                    "select": {"record_type": "claude_http_request"},
                    "assertion": {"operator": "all_subset", "path": "data.secret_headers.authorization", "value": {"scheme": "Bearer", "scrubbed": True}},
                },
            ],
        },
        "SPEC-HDR-002": {
            "description": "Messages 请求正文顶层键序以 model、max_tokens 开头",
            "checks": [
                {
                    "id": "body-key-prefix",
                    "description": "正文顶层键序前缀",
                    "select": {"record_type": "claude_http_request", "where": [{"path": "data.path", "operator": "equal", "value": "/v1/messages"}]},
                    "assertion": {"operator": "all_list_prefix", "path": "data.body_keys", "value": ["model", "max_tokens"]},
                }
            ],
        },
        "SPEC-FAIL-001": {
            "description": "529 只合成一次，客户端在新连接上重试",
            "checks": [
                {
                    "id": "synthetic-529",
                    "description": "中继按计划合成了一次 529",
                    "select": {"record_type": "relay_intervention", "where": [{"path": "data.action", "operator": "equal", "value": "status_529"}]},
                    "assertion": {"operator": "count_equal", "value": 1},
                },
                {
                    "id": "retry-new-connection",
                    "description": "第一条连接带合成动作，重试连接没有",
                    "select": {"record_type": "relay_connection"},
                    "assertion": {"operator": "distinct_values_equal", "path": "data.intervention_actions", "value": [["status_529"], []]},
                },
                {
                    "id": "response-sequence",
                    "description": "推理请求的上游响应依次是 529 与 200",
                    "select": {"record_type": "probe_run", "where": [{"path": "data.message_count", "operator": "equal", "value": 2}]},
                    "assertion": {"operator": "all_equal", "path": "data.message_response_statuses", "value": [529, 200]},
                },
                {
                    "id": "retry-backoff",
                    "description": "重试连接比首个连接晚 500～750 毫秒打开",
                    "select": {
                        "record_type": "claude_http_request",
                        "where": [
                            {"path": "data.endpoint", "operator": "equal", "value": "POST /v1/messages?beta=true"},
                            {"path": "data.probe_endpoint_index", "operator": "equal", "value": 1},
                        ],
                    },
                    "assertion": {"operator": "all_between", "path": "data.previous_same_endpoint_gap_ms", "minimum": 500, "maximum": 750},
                },
                {
                    "id": "retry-same-body",
                    "description": "重试请求的 Body 与首个请求逐字相同",
                    "select": {"record_type": "claude_http_request", "where": [{"path": "data.probe_endpoint_index", "operator": "equal", "value": 1}]},
                    "assertion": {
                        "operator": "all_equal_reference",
                        "path": "data.body",
                        "reference": {"where": [{"path": "data.probe_endpoint_index", "operator": "equal", "value": 0}]},
                    },
                },
            ],
        },
        "SPEC-TLS-001": {
            "description": "ClientHello 的 SNI 取 Release Wire 值且不带 ALPN",
            "checks": [
                {
                    "id": "sni",
                    "description": "SNI 等于 Release Wire 声明值",
                    "select": {"record_type": "tls_client_hello"},
                    "assertion": {"operator": "all_equal", "path": "data.sni", "value": {"$release": "wire:/tls/server_name"}},
                },
                {
                    "id": "no-alpn",
                    "description": "不带 ALPN 扩展",
                    "select": {"record_type": "tls_client_hello"},
                    "assertion": {"operator": "all_equal", "path": "data.has_alpn_extension", "value": False},
                },
            ],
        },
        "SPEC-META-001": {
            "description": "取证运行收据：中继完整性通过、客户端版本等于目标版本",
            "checks": [
                {
                    "id": "relay-integrity",
                    "description": "中继完整性通过",
                    "select": {"record_type": "runtime_receipt"},
                    "assertion": {"operator": "all_equal", "path": "data.relay_integrity_result", "value": "pass"},
                },
                {
                    "id": "client-version",
                    "description": "客户端版本等于目标版本",
                    "select": {"record_type": "runtime_receipt"},
                    "assertion": {"operator": "all_equal", "path": "data.client_version", "value": TARGET_VERSION},
                },
            ],
        },
    },
}

RULE_MANIFEST: dict[str, Any] = {
    "schema_version": assertion.RULE_MANIFEST_SCHEMA,
    "claude_version": TARGET_VERSION,
    "required_rules": list(RULES),
}


def build_profile() -> dict[str, Any]:
    """用 checker 的生成器生成合成断言画像。"""

    return assertion.build_expectations(
        claude_version=TARGET_VERSION,
        ledger=json.loads(json.dumps(LEDGER)),
        scenarios_payload=json.loads(json.dumps(SCENARIOS_PAYLOAD)),
        templates=json.loads(json.dumps(TEMPLATES)),
        release_wire=json.loads(json.dumps(RELEASE_WIRE)),
        rule_manifest=json.loads(json.dumps(RULE_MANIFEST)),
    )


def write_profile(path: Path) -> str:
    """把合成画像按规范字节写出，返回文件摘要。"""

    raw = assertion.render_expectations(build_profile())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def write_rule_manifest(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(RULE_MANIFEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 证据字节
# ---------------------------------------------------------------------------


def http_request(
    path: str,
    body: dict[str, Any],
    *,
    anthropic_version: str = "2023-06-01",
    encoding: str | None = None,
    chunked: bool = False,
    secret: str = "<secret>" + "X" * 24,
) -> bytes:
    """造一个 HTTP/1.1 请求的客户端字节（头名大小写与顺序按给定写出）。"""

    raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if encoding == "gzip":
        raw_body = gzip.compress(raw_body, mtime=0)
    headers = [
        ("Host", "api.anthropic.com"),
        ("Authorization", f"Bearer {secret}"),
        ("anthropic-version", anthropic_version),
        ("content-type", "application/json"),
        ("User-Agent", f"claude-cli/{TARGET_VERSION} (external, cli)"),
    ]
    if encoding is not None:
        headers.append(("Content-Encoding", encoding))
    if chunked:
        headers.append(("Transfer-Encoding", "chunked"))
        half = len(raw_body) // 2
        payload = b"".join(f"{len(part):x}\r\n".encode() + part + b"\r\n" for part in (raw_body[:half], raw_body[half:]) if part) + b"0\r\n\r\n"
    else:
        headers.append(("Content-Length", str(len(raw_body))))
        payload = raw_body
    head = f"POST {path}?beta=true HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers) + "\r\n"
    return head.encode("latin-1") + payload


def messages_body(text: str = "hi") -> dict[str, Any]:
    return {"model": "claude-opus-5", "max_tokens": 32000, "messages": [{"role": "user", "content": text}], "stream": True}


def client_hello(sni: str, alpn: list[str] | None = None) -> bytes:
    """造一个 TLS 1.3 风格的 ClientHello 记录（server_name、supported_versions，可选 ALPN）。"""

    name = sni.encode("ascii")
    server_name = struct.pack(">BH", 0, len(name)) + name
    sni_data = struct.pack(">H", len(server_name)) + server_name
    extensions = struct.pack(">HH", 0, len(sni_data)) + sni_data
    versions = bytes([2]) + b"\x03\x04"
    extensions += struct.pack(">HH", 43, len(versions)) + versions
    if alpn:
        protocols = b"".join(bytes([len(item)]) + item.encode("ascii") for item in alpn)
        alpn_data = struct.pack(">H", len(protocols)) + protocols
        extensions += struct.pack(">HH", 16, len(alpn_data)) + alpn_data
    ciphers = [0x1301, 0x1302, 0x1303, 0xC02B]
    body = (
        b"\x03\x03"
        + bytes(32)
        + bytes([32])
        + bytes(range(32))
        + struct.pack(">H", 2 * len(ciphers))
        + b"".join(struct.pack(">H", cipher) for cipher in ciphers)
        + b"\x01\x00"
        + struct.pack(">H", len(extensions))
        + extensions
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake


def ethernet_frame(payload: bytes, *, sport: int, destination: str = UPSTREAM_IP, dport: int = 443) -> bytes:
    tcp = struct.pack(">HHIIBBHHH", sport, dport, 1, 0, 5 << 4, 0x18, 65535, 0, 0)
    total = 20 + len(tcp) + len(payload)
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total, 1, 0, 64, 6, 0, socket.inet_aton("172.30.0.10"), socket.inet_aton(destination))
    return bytes(6) + bytes(6) + b"\x08\x00" + ip + tcp + payload


def pcap_bytes(frames: list[bytes]) -> bytes:
    header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    return header + b"".join(struct.pack("<IIII", 1787320443, index, len(frame), len(frame)) + frame for index, frame in enumerate(frames))


SSE_BODY = b"event: message_start\ndata: {}\n\nevent: content_block_delta\ndata: {}\n\nevent: message_stop\ndata: {}\n\n"


def http_response(status: int, reason: str, body: bytes, *, content_type: str, chunked: bool = False, extra: tuple[tuple[str, str], ...] = ()) -> bytes:
    """造一个 HTTP/1.1 响应（Content-Length 或 chunked）。"""

    headers = [("content-type", content_type), ("request-id", f"req_fixture_{status}"), *extra]
    if chunked:
        headers.append(("transfer-encoding", "chunked"))
        payload = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    else:
        headers.append(("content-length", str(len(body))))
        payload = body
    return (f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers) + "\r\n").encode("latin-1") + payload


def relay_index(connections: list[tuple[int, int]], *, synthetic: bool) -> bytes:
    """中继连接索引 ``relay/relay.json``：``[(连接号, 打开时刻毫秒)]``。"""

    payload = {
        "schema_version": "claude-fw-e-relay-index/v1",
        "mode": "synthetic" if synthetic else "live",
        "production_forwarding_enabled": not synthetic,
        "upstream_host": "api.anthropic.com",
        "connections": [
            {
                "connection_id": connection_id,
                "valid": True,
                "sni": "api.anthropic.com",
                "client_alpn": "http/1.1",
                "client_alpn_offer": ["http/1.1"],
                "opened_at_unix_ms": opened,
                "closed_at_unix_ms": opened + 900,
            }
            for connection_id, opened in connections
        ],
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def relay_manifest(probe: str, *, integrity: str = "pass", client_version: str = TARGET_VERSION, plan: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "claude-fw-e-relay-manifest/v5",
        "probe_id": probe,
        "fw_f_v5_probe": probe,
        "model": "claude-opus-5",
        "status": "complete",
        "response_plan": plan,
        "client": {"version": f"{client_version} (Claude Code)"},
        "injected_probe_env": {"DISABLE_TELEMETRY": "1"},
        "upstream": {"host": "api.anthropic.com", "live_requests": 1},
        "relay_integrity": {"result": integrity, "messages_request_count": 1, "hello_request_count": 0, "excluded_connections": []},
        "message_request_expectation": "at_least_one",
        "scenario_result": {
            "expected_outcome": "success",
            "driver": "print",
            "inner_result": {"local_error_results": [], "error_results": [], "return_codes": [0]},
        },
        "credential_scrubbing": {"verified": True},
    }


@dataclass(frozen=True)
class SideEvidence:
    """一侧证据：证据根、评估用 manifest 路径与封存前的 capture manifest。"""

    evidence_root: Path
    manifest_path: Path
    capture_manifest: dict[str, Any]


def _artifact(job: str, probe: str, relative: str, kind: str, parser: str, scenario: str, **extra: Any) -> dict[str, Any]:
    # 标签键取真实标签规则里出现过的那几个（probe、response_mode、channel）。
    labels = {"probe": probe, "response_mode": "synthetic" if probe.startswith("failure-") else "real", "channel": "P" if kind == "pcap" else "R"}
    return {"job_id": job, "probe_id": probe, "path": f"{job}/{probe}/{relative}", "kind": kind, "parser": parser, "labels": labels, "scenario_ids": [scenario], **extra}


def write_side(
    side_dir: Path,
    *,
    capture_id: str,
    anthropic_version: str = "2023-06-01",
    integrity: str = "pass",
    sni: str = "api.anthropic.com",
) -> SideEvidence:
    """在 ``side_dir/evidence`` 下写一侧合成证据，并把封存绑定合成为评估用 manifest（``side_dir/evaluation-manifest.json``）。"""

    root = side_dir / "evidence"
    job = "job-main"
    files: dict[str, bytes] = {
        f"{job}/print/relay/conn001.client_to_upstream.bin": http_request("/v1/messages", messages_body(), anthropic_version=anthropic_version)
        + http_request("/v1/messages/count_tokens", {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}, anthropic_version=anthropic_version, encoding="gzip"),
        f"{job}/print/relay-manifest.json": (json.dumps(relay_manifest("print", integrity=integrity), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        f"{job}/failure-529/relay/conn001.client_to_upstream.bin": http_request("/v1/messages", messages_body("retry me")),
        f"{job}/failure-529/relay/conn002.client_to_upstream.bin": http_request("/v1/messages", messages_body("retry me"), chunked=True),
        f"{job}/failure-529/relay/intervention.jsonl": (
            json.dumps(
                {
                    "t_unix_ms": 1787320443123,
                    "type": "synthetic_response",
                    "profile": "failure-529",
                    "plan": "status_529_then_pass",
                    "action": "status_529",
                    "connection_id": 1,
                    "request_line": "POST /v1/messages?beta=true HTTP/1.1",
                    "message_ordinal": 1,
                    "delay_seconds": 0,
                    "production_forwarded": False,
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8"),
        f"{job}/failure-529/relay-manifest.json": (json.dumps(relay_manifest("failure-529", plan="status_529_then_pass"), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        # 伴随文件：上游响应字节与中继连接索引（不进标签，由 bind_capture_manifest 从封存绑定补上）。
        f"{job}/print/relay/conn001.upstream_to_client.bin": http_response(200, "OK", SSE_BODY, content_type="text/event-stream")
        + http_response(200, "OK", b'{"input_tokens":3}', content_type="application/json"),
        f"{job}/print/relay/relay.json": relay_index([(1, 1787320440000)], synthetic=False),
        f"{job}/failure-529/relay/conn001.upstream_to_client.bin": http_response(
            529, "Overloaded", b'{"type":"error","error":{"type":"overloaded_error"}}', content_type="application/json"
        ),
        f"{job}/failure-529/relay/conn002.upstream_to_client.bin": http_response(200, "OK", SSE_BODY, content_type="text/event-stream", chunked=True),
        f"{job}/failure-529/relay/relay.json": relay_index([(1, 1787320443000), (2, 1787320443600)], synthetic=True),
        f"{job}/tls/tls-clienthello.pcap": pcap_bytes(
            [
                ethernet_frame(b"", sport=50000),
                ethernet_frame(client_hello(sni), sport=50001),
                ethernet_frame(client_hello(sni), sport=50002),
            ]
        ),
    }
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    derive = {"parser": "h1_request_stream", "kind": "process_trace"}
    artifacts = [
        _artifact(job, "print", "relay/conn001.client_to_upstream.bin", "relay_binary", "opaque_bound_source", "transport.http", connection={"ordinal": 1, "connection_id": 1}, derive=dict(derive)),
        _artifact(job, "print", "relay-manifest.json", "relay_manifest", "claude_relay_manifest", "transport.http"),
        _artifact(job, "failure-529", "relay/conn001.client_to_upstream.bin", "relay_binary", "opaque_bound_source", "failure.529", connection={"ordinal": 1, "connection_id": 1}, derive=dict(derive)),
        _artifact(job, "failure-529", "relay/conn002.client_to_upstream.bin", "relay_binary", "opaque_bound_source", "failure.529", connection={"ordinal": 2, "connection_id": 2}, derive=dict(derive)),
        _artifact(job, "failure-529", "relay/intervention.jsonl", "synthetic_intervention", "jsonl", "failure.529"),
        _artifact(job, "failure-529", "relay-manifest.json", "relay_manifest", "claude_relay_manifest", "failure.529"),
        _artifact(job, "tls", "tls-clienthello.pcap", "pcap", "pcap_client_hello", "transport.native_tls"),
    ]
    artifacts.sort(key=lambda item: (item["path"], json.dumps(item["labels"], sort_keys=True)))
    capture_manifest = {
        "schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION,
        "campaign_id": CAMPAIGN_ID,
        "target_version": TARGET_VERSION,
        "attempt": {"attempt_id": "attempt-0001"},
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "label_mismatches": [],
    }
    bindings = {relative: {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)} for relative, payload in files.items()}
    manifest = assertion.bind_capture_manifest(capture_manifest, bindings, capture_id=capture_id)
    manifest_path = side_dir / "evaluation-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return SideEvidence(evidence_root=root, manifest_path=manifest_path, capture_manifest=capture_manifest)
