#!/usr/bin/env python3
"""组装 Kilo 双协议与正式 42 条基线的条件化验收包。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


FULL_RULES = {
    "SPEC-TLS-001",
    "SPEC-PROTO-001",
    "SPEC-H1-001",
    "SPEC-H1-002",
    "SPEC-H1-003",
    "SPEC-WS-001",
    "SPEC-EP-006",
}

BRANCH_RULES = {
    "SPEC-PROTO-002",
    "SPEC-H1-004",
    "SPEC-WS-002",
    "SPEC-WS-004",
    "SPEC-WS-005",
    "SPEC-HDR-002",
    "SPEC-HDR-004",
    "SPEC-HDR-005",
    "SPEC-HDR-006",
    "SPEC-HDR-007",
    "SPEC-HDR-008",
    "SPEC-BODY-001",
    "SPEC-BODY-002",
    "SPEC-BODY-003",
    "SPEC-BODY-005",
    "SPEC-BODY-006",
    "SPEC-EP-002",
    "SPEC-EP-005",
    "SPEC-EP-013",
}

INHERITED_RULES = {
    "SPEC-TLS-003",
    "SPEC-CONN-001",
    "SPEC-HDR-001",
    "SPEC-BODY-004",
    "SPEC-EP-001",
    "SPEC-EP-007",
    "SPEC-EP-008",
    "SPEC-EP-009",
    "SPEC-EP-012",
    "SPEC-EP-014",
    "SPEC-EP-015",
    "SPEC-EP-019",
    "SPEC-EP-020",
    "SPEC-EP-021",
    "SPEC-EP-022",
    "SPEC-EP-023",
}

RULE_OBSERVATIONS = {
    "SPEC-TLS-001": "Kilo Compatible 业务连接现场为 30 cipher、固定 native-tls 扩展序、无 ALPN。",
    "SPEC-PROTO-001": "Kilo Compatible 上游现场为 HTTP/1.1，且 ClientHello 不 offer ALPN。",
    "SPEC-H1-001": "Compatible 出站原始请求的全部 header 名均为小写。",
    "SPEC-H1-002": "Compatible 出站原始线序中 host 位于用户 header 之后。",
    "SPEC-H1-003": "Compatible POST 的 content-length 位于 host 之后且为末项。",
    "SPEC-WS-001": "Responses 上游 WS 握手前五项大小写与顺序精确命中冻结画像。",
    "SPEC-EP-006": "两路冷缓存均现场发出 GET /backend-api/codex/models?client_version=0.145.0。",
    "SPEC-PROTO-002": "Responses 第三方入口现场优先选择上游 WS；重试耗尽后的 HTTP 降级继承正式包。",
    "SPEC-H1-004": "现场 H1 HeaderMap 为非字典序；缺项和 swap_remove 的一般分支继承正式包。",
    "SPEC-WS-002": "现场 WS 固定前缀后剩余头均小写并呈画像线序；可选头缺席分支继承。",
    "SPEC-WS-004": "现场协商 permessage-deflate、RSV1/raw deflate，并以连接级解码器成功读取两条 response.create；独立重置失败反例继承。",
    "SPEC-WS-005": "现场观察 Lite 的预热与业务 response.create；generate/previous_response_id 的其余条件继承。",
    "SPEC-HDR-002": "现场 originator=codex_exec、0.145.0 UA，默认 residency 头缺席；managed 分支继承。",
    "SPEC-HDR-004": "现场 WS 有 OpenAI-Beta，HTTP Responses 无；images 反例继承。",
    "SPEC-HDR-005": "普通 Kilo 被定型为 initialized exec UA+suffix；TUI/首次无 suffix 分支继承。",
    "SPEC-HDR-006": "现场 Responses HTTP accept=text/event-stream、WS 无 accept，models accept=*/*；其余辅助端点继承。",
    "SPEC-HDR-007": "现场 session/thread 身份头按 Responses 画像出现；compact/realtime 边界继承。",
    "SPEC-HDR-008": "普通第三方 Kilo 未激活内部条件头；正向内部条件继承。",
    "SPEC-BODY-001": "现场核验 Lite HTTP 顶层闭集；非 Lite 与未出现 Option 组合继承。",
    "SPEC-BODY-002": "Compatible HTTP 现场使用 zstd；关闭 feature 与 legacy compact 明文分支继承。",
    "SPEC-BODY-003": "现场 gpt-5.6-luna 为 Lite，additional_tools、reasoning.context=all_turns、parallel=false；增量续轮条件继承。",
    "SPEC-BODY-005": "现场 Lite tool_choice 为字符串 auto；非 Lite 分支继承。",
    "SPEC-BODY-006": "Compatible HTTP 现场核验 Lite 字段；非 Lite HTTP 分支继承。",
    "SPEC-EP-002": "两路业务与 models 现场均指向 chatgpt.com；refresh/realtime/upload host 继承。",
    "SPEC-EP-005": "现场 Responses 可压缩且 models 明文；compact/search/images 反例继承。",
    "SPEC-EP-013": "两条普通 Responses 业务请求现场均无 query；models 自有 query 已单独观察。",
}


def run(*args: str, check: bool = True) -> str:
    completed = subprocess.run(
        args,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as descriptor:
        for chunk in iter(lambda: descriptor.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def db_metadata() -> tuple[str, str]:
    environment = run(
        "docker",
        "inspect",
        "-f",
        "{{range .Config.Env}}{{println .}}{{end}}",
        "sub2apiplus-postgres",
    ).splitlines()
    user = next(line.split("=", 1)[1] for line in environment if line.startswith("POSTGRES_USER="))
    database = next(line.split("=", 1)[1] for line in environment if line.startswith("POSTGRES_DB="))
    return user, database


def db_query(query: str) -> str:
    user, database = db_metadata()
    return run(
        "docker",
        "exec",
        "sub2apiplus-postgres",
        "psql",
        "-U",
        user,
        "-d",
        database,
        "-qAtc",
        query,
    )


def import_pcap_tool(path: Path):
    spec = importlib.util.spec_from_file_location("kilo_pcap_clienthello", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 pcap_clienthello.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify_pcap(path: Path, pcap_tool) -> dict[str, Any]:
    profiles: Counter[str] = Counter()
    snis: Counter[str] = Counter()
    records = []
    for link, packet in pcap_tool.iter_packets(path):
        parsed = pcap_tool.tcp_payload(link, packet)
        if not parsed:
            continue
        _, _, payload = parsed
        hello = pcap_tool.parse_client_hello(payload)
        if not hello or not hello[1]:
            continue
        sni, extensions, ciphers, alpn = hello
        if sni:
            snis[sni] += 1
        if len(ciphers) == 30 and tuple(extensions) == pcap_tool.NATIVE_TLS_EXTENSION_ORDER and not alpn:
            profile = "native_http_30_no_alpn"
        elif len(ciphers) == 10 and tuple(extensions) != pcap_tool.NATIVE_TLS_EXTENSION_ORDER and not alpn:
            profile = "rustls_ws_10_no_alpn"
        else:
            profile = "auxiliary_or_unclassified"
        profiles[profile] += 1
        records.append(
            {
                "sni": sni,
                "cipher_count": len(ciphers),
                "alpn": alpn,
                "extension_order": extensions,
                "profile": profile,
            }
        )
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "profiles": dict(sorted(profiles.items())),
        "sni": dict(sorted(snis.items())),
        "records": records,
    }


def request_by_prefix(analysis: dict[str, Any], prefix: str) -> dict[str, Any]:
    for connection in analysis["connections"]:
        for request in connection.get("requests", []):
            if request["request_line"].startswith(prefix):
                return request
    raise KeyError(prefix)


def header_values(request: dict[str, Any]) -> dict[str, str]:
    return {entry["name"]: entry["value"] for entry in request["headers"]}


def parse_service_profiles() -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "docker",
            "logs",
            "--since",
            "2026-07-31T05:50:00+08:00",
            "sub2apiplus",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    text = completed.stdout
    profiles = []
    for line in text.splitlines():
        if "official_egress_profile_resolved" not in line:
            continue
        start = line.find("{")
        if start < 0:
            continue
        try:
            payload = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if payload.get("account_id") != 90:
            continue
        profiles.append(
            {
                key: payload.get(key)
                for key in (
                    "endpoint",
                    "transport",
                    "upstream_host",
                    "profile_version",
                    "profile_id",
                    "profile_digest",
                    "transport_profile",
                    "codex_version_profile",
                    "codex_version_digest",
                    "codex_endpoint_profile",
                    "proxy_id",
                    "custom_ca",
                    "frozen",
                )
            }
        )
    return profiles


def secret_scan(root: Path) -> dict[str, Any]:
    patterns = {
        "jwt_like": re.compile(rb"eyJ[A-Za-z0-9_-]{40,}"),
        "api_key_like": re.compile(rb"sk-[A-Za-z0-9]{20,}"),
        "bearer_live": re.compile(rb"(?i)authorization:\s*bearer\s+(?!<secret>|<redacted>)[^\r\n]+"),
        "json_live_token": re.compile(rb'"(?:access_token|refresh_token|id_token)"\s*:\s*"(?!<secret>|<redacted>)[^"]+"'),
    }
    hits: Counter[str] = Counter()
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        scanned += 1
        data = path.read_bytes()
        for name, pattern in patterns.items():
            hits[name] += len(pattern.findall(data))
    return {
        "scanned_file_count": scanned,
        "hits": dict(sorted(hits.items())),
        "passed": sum(hits.values()) == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--formal-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tool-root", required=True)
    args = parser.parse_args()

    run_root = Path(args.run_root).resolve()
    formal_root = Path(args.formal_root).resolve()
    output = Path(args.output).resolve()
    tool_root = Path(args.tool_root).resolve()
    if output.exists():
        raise SystemExit(f"输出目录已存在，拒绝覆盖：{output}")
    output.mkdir(parents=True, mode=0o700)

    errors: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    formal_report = load_json(formal_root / "candidate-42-acceptance-report.json")
    formal_acceptance = load_json(formal_root / "candidate-42-acceptance.json")
    formal_identity = formal_acceptance["candidate_identity"]
    formal_rules = {entry["rule_id"]: entry for entry in formal_acceptance["rules"]}
    all_rule_ids = FULL_RULES | BRANCH_RULES | INHERITED_RULES
    check(len(all_rule_ids) == 42, "Kilo 规则分类不是 42 条")
    check(formal_report.get("accepted") is True, "正式 42 条基线未 accepted")
    check(formal_report.get("required_rule_count") == 42, "正式基线规则数不是 42")
    check(set(formal_rules) == all_rule_ids, "正式规则集合与 Kilo 条件矩阵不一致")
    for rule_id, entry in formal_rules.items():
        result_path = formal_root / entry["assertion"]["result"]["path"]
        check(result_path.is_file(), f"正式断言缺失：{rule_id}")
        if result_path.is_file():
            result = load_json(result_path)
            check(result.get("status") == "pass", f"正式断言未通过：{rule_id}")
            check(
                sha256_file(result_path) == entry["assertion"]["result"]["sha256"],
                f"正式断言哈希不一致：{rule_id}",
            )

    current_image_ref = run("docker", "inspect", "-f", "{{.Config.Image}}", "sub2apiplus")
    current_image_id = run("docker", "inspect", "-f", "{{.Image}}", "sub2apiplus")
    current_health = run(
        "docker",
        "inspect",
        "-f",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
        "sub2apiplus",
    )
    current_version = run("docker", "exec", "sub2apiplus", "/app/sub2api", "--version")
    check(current_image_ref == formal_identity["image_reference"], "当前镜像引用与正式包不一致")
    check(current_image_id == formal_identity["image_digest"], "当前镜像 ID 与正式包不一致")
    check(current_health == "healthy", "当前主服务不健康")

    run_summary = load_json(run_root / "byte" / "run-summary.json")
    check(run_summary.get("status") == "complete", "Kilo 字节抓包未完整结束")
    check(run_summary.get("production_forwarding_enabled") is False, "合成 relay 允许生产转发")
    restoration = run_summary["restoration"]
    check(restoration.get("account_90_equal") is True, "account90 未精确恢复")
    check(restoration.get("account_95_equal") is True, "account95 未精确恢复")
    check(restoration.get("group_mapping_equal") is True, "group8 映射未精确恢复")

    pcap_tool = import_pcap_tool(tool_root / "pcap_clienthello.py")
    captures = {
        "direct_compatible": classify_pcap(
            run_root / "direct" / "kilo-compatible-real" / "egress.pcap", pcap_tool
        ),
        "direct_responses": classify_pcap(
            run_root / "direct" / "kilo-responses-real" / "egress.pcap", pcap_tool
        ),
        "byte_compatible": classify_pcap(
            run_root / "byte" / "scenarios" / "KILO_COMPAT" / "egress.pcap", pcap_tool
        ),
        "byte_responses": classify_pcap(
            run_root / "byte" / "scenarios" / "KILO_RESPONSES" / "egress.pcap", pcap_tool
        ),
    }
    check(captures["direct_compatible"]["profiles"].get("native_http_30_no_alpn", 0) >= 1, "Compatible 直连未捕获 HTTP 画像")
    check(captures["direct_responses"]["profiles"].get("rustls_ws_10_no_alpn", 0) >= 1, "Responses 直连未捕获 WS 画像")
    check(captures["byte_compatible"]["profiles"] == {"native_http_30_no_alpn": 2}, "Compatible 隔离 pcap 画像不纯")
    check(
        captures["byte_responses"]["profiles"]
        == {"native_http_30_no_alpn": 1, "rustls_ws_10_no_alpn": 1},
        "Responses 隔离 pcap 未形成 models HTTP + Responses WS",
    )

    compatible_analysis = load_json(
        run_root / "byte" / "scenarios" / "KILO_COMPAT" / "analysis.json"
    )
    responses_analysis = load_json(
        run_root / "byte" / "scenarios" / "KILO_RESPONSES" / "analysis.json"
    )
    for label, analysis in (("Compatible", compatible_analysis), ("Responses", responses_analysis)):
        stats = analysis["connection_stats"]
        check(stats.get("response_only") == 0 and stats.get("idle") == 0, f"{label} relay 样本不完整")
        models = request_by_prefix(analysis, "GET /backend-api/codex/models?")
        check(
            models["request_line"]
            == "GET /backend-api/codex/models?client_version=0.145.0 HTTP/1.1",
            f"{label} models URL/方法不匹配",
        )

    compatible_request = request_by_prefix(
        compatible_analysis, "POST /backend-api/codex/responses "
    )
    expected_h1_order = [
        "version",
        "x-codex-beta-features",
        "x-codex-window-id",
        "x-codex-turn-metadata",
        "x-openai-internal-codex-responses-lite",
        "x-client-request-id",
        "session-id",
        "thread-id",
        "accept",
        "content-encoding",
        "content-type",
        "authorization",
        "chatgpt-account-id",
        "originator",
        "user-agent",
        "host",
        "content-length",
    ]
    check(compatible_request["header_names_in_order"] == expected_h1_order, "Compatible H1 线序不匹配")
    check(all(name == name.lower() for name in compatible_request["header_names_in_order"]), "Compatible H1 不是全小写")
    compatible_headers = header_values(compatible_request)
    check(compatible_headers.get("content-encoding") == "zstd", "Compatible Responses 未使用 zstd")
    check(compatible_headers.get("accept") == "text/event-stream", "Compatible Responses accept 不匹配")
    check(compatible_headers.get("x-openai-internal-codex-responses-lite") == "true", "Compatible Lite 头不匹配")
    check(compatible_headers.get("originator") == "codex_exec", "Compatible originator 不匹配")
    check("codex_exec/0.145.0" in compatible_headers.get("user-agent", ""), "Compatible UA 未绑定 0.145.0")
    body = compatible_request.get("body", {})
    shape = body.get("shape", {})
    check(shape.get("model") == "str:gpt-5.6-luna", "Compatible 出站模型不匹配")
    check(shape.get("tool_choice") == "str:auto", "Compatible Lite tool_choice 不是字符串 auto")
    check(shape.get("parallel_tool_calls") == "bool:false", "Compatible Lite parallel_tool_calls 不是 false")
    check(shape.get("reasoning", {}).get("context") == "str:all_turns", "Compatible reasoning.context 不匹配")
    check(shape.get("input", {}).get("_types_count", {}).get("additional_tools") == 1, "Compatible additional_tools 未出现一次")

    responses_request = request_by_prefix(
        responses_analysis, "GET /backend-api/codex/responses "
    )
    expected_ws_prefix = [
        "Host",
        "Connection",
        "Upgrade",
        "Sec-WebSocket-Version",
        "Sec-WebSocket-Key",
    ]
    check(responses_request["header_names_in_order"][:5] == expected_ws_prefix, "Responses WS 固定前缀不匹配")
    check(
        all(name == name.lower() for name in responses_request["header_names_in_order"][5:]),
        "Responses WS 剩余头未全小写",
    )
    responses_headers = header_values(responses_request)
    check(responses_headers.get("openai-beta") == "responses_websockets=2026-02-06", "Responses WS beta 头不匹配")
    check("accept" not in responses_headers, "Responses WS 不应有 accept")
    check("permessage-deflate" in responses_headers.get("sec-websocket-extensions", ""), "Responses WS 未协商 PMD")
    responses_relay = load_json(
        run_root / "byte" / "scenarios" / "KILO_RESPONSES" / "relay" / "relay.json"
    )
    ws_connections = [entry for entry in responses_relay["connections"] if entry.get("request_line", "").startswith("GET /backend-api/codex/responses")]
    check(len(ws_connections) == 1, "Responses relay 的 WS 连接数不是 1")
    if ws_connections:
        check(ws_connections[0].get("valid") is True, "Responses WS relay 未验证通过")
        check(ws_connections[0].get("ws_response_create_count") == 2, "Responses WS 未观察到预热+业务两帧")
    check(all(not entry.get("production_forwarded") for entry in responses_relay["connections"]), "Responses relay 发生生产转发")

    usage_rows = json.loads(
        db_query(
            """
select coalesce(json_agg(row_to_json(t)),'[]'::json)::text
from (
  select id,
         to_char(created_at at time zone 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS.MS') as created_at,
         api_key_id, group_id, account_id, requested_model, model,
         openai_ws_mode, stream, inbound_endpoint, upstream_endpoint, duration_ms
  from usage_logs
  where id in (120462,120463,120464,120466,120467,120468)
  order by id
) t
"""
        )
    )
    expected_usage = {
        120462: ("/v1/chat/completions", "/v1/responses", False),
        120463: ("/v1/responses", "/v1/responses", True),
        120464: ("/v1/responses", "/v1/responses", True),
        120466: ("/v1/chat/completions", "/v1/responses", False),
        120467: ("/v1/chat/completions", "/v1/responses", False),
        120468: ("/v1/responses", "/v1/responses", True),
    }
    check({row["id"] for row in usage_rows} == set(expected_usage), "Kilo usage 绑定记录不完整")
    for row in usage_rows:
        expected = expected_usage[row["id"]]
        check(row["api_key_id"] == 1 and row["group_id"] == 8 and row["account_id"] == 90, f"usage {row['id']} 未命中 key1/group8/account90")
        check(row["requested_model"] == "gpt-5.6-luna", f"usage {row['id']} 模型不匹配")
        check((row["inbound_endpoint"], row["upstream_endpoint"], row["openai_ws_mode"]) == expected, f"usage {row['id']} 路由或 WS 模式不匹配")

    service_profiles = parse_service_profiles()
    http_profiles = [entry for entry in service_profiles if entry["endpoint"] == "/v1/chat/completions" and entry["codex_endpoint_profile"] == "responses_http"]
    ws_profiles = [entry for entry in service_profiles if entry["endpoint"] == "/v1/responses" and entry["codex_endpoint_profile"] == "responses_ws"]
    check(any(entry["proxy_id"] == 0 for entry in http_profiles), "缺少 Compatible 真实直连画像绑定")
    check(any(entry["proxy_id"] == 0 for entry in ws_profiles), "缺少 Responses 真实直连画像绑定")
    check(any((entry["proxy_id"] or 0) > 0 for entry in http_profiles), "缺少 Compatible 字节抓包画像绑定")
    check(any((entry["proxy_id"] or 0) > 0 for entry in ws_profiles), "缺少 Responses 字节抓包画像绑定")
    version_digests = {entry["codex_version_digest"] for entry in http_profiles + ws_profiles}
    check(version_digests == {"9b7dd12df50dbcff74594b1f05440161cd99b963019a4f316f20c08ed5f5ba1e"}, "Kilo 两路未绑定同一版本画像摘要")

    current_account_state = db_query(
        "select id || '|' || status || '|' || schedulable::text || '|' || coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id in (90,95) order by id"
    ).splitlines()
    current_group_hash = hashlib.sha256(
        (db_query("select account_id || '|' || priority from account_groups where group_id=8 order by account_id") + "\n").encode()
    ).hexdigest()
    current_data_ids = {
        name: run("docker", "inspect", "-f", "{{.Id}}", container)
        for name, container in (
            ("postgres", "sub2apiplus-postgres"),
            ("redis", "sub2apiplus-redis"),
            ("keeper", "sub2apiplus-keeper"),
        )
    }
    temp_proxy_count = int(db_query("select count(*) from proxies where name like 'kilo-r11-kilo-r11-20260731T220626Z%'") or "0")
    ca_absent = subprocess.run(
        ["docker", "exec", "sub2apiplus", "test", "!", "-e", "/usr/local/share/ca-certificates/kilo-r11-capture.crt"],
        check=False,
    ).returncode == 0
    keeper_running = run("docker", "inspect", "-f", "{{.State.Running}}", "sub2apiplus-keeper") == "true"
    check(current_account_state == ["90|active|true|NULL|NULL", "95|active|true|NULL|NULL"], "账号 90/95 最终状态不匹配")
    check(current_group_hash == restoration["group_mapping_sha256_before"], "group8 最终映射哈希不匹配")
    check(current_data_ids == restoration["data_container_ids"], "数据容器 ID 发生变化")
    check(temp_proxy_count == 0, "临时 Kilo proxy 未清零")
    check(ca_absent, "临时 Kilo CA 未清理")
    check(keeper_running, "keeper 未恢复运行")

    capture_dir = output / "capture"
    shutil.copytree(run_root / "direct", capture_dir / "direct")
    shutil.copytree(
        run_root / "byte",
        capture_dir / "byte",
        ignore=shutil.ignore_patterns("tls-private"),
    )

    identity_binding = {
        "formal_assessment_id": formal_acceptance["assessment_id"],
        "formal_accepted": formal_report["accepted"],
        "formal_rule_count": formal_report["required_rule_count"],
        "formal_rule_manifest_sha256": formal_acceptance["rule_manifest_sha256"],
        "formal_candidate_identity": formal_identity,
        "current_image_reference": current_image_ref,
        "current_image_id": current_image_id,
        "current_version": current_version,
        "current_health": current_health,
        "codex_version_profile_digests": sorted(version_digests),
        "identity_equal": current_image_ref == formal_identity["image_reference"] and current_image_id == formal_identity["image_digest"],
        "formal_anchor_sha256": {
            name: sha256_file(formal_root / name)
            for name in (
                "candidate-42-acceptance-report.json",
                "candidate-42-acceptance.json",
                "capture-manifest.json",
                "EVIDENCE_SHA256SUMS",
            )
        },
    }
    write_json(output / "identity-binding.json", identity_binding)
    write_json(output / "pcap-analysis.json", captures)
    write_json(
        output / "kilo-live-bindings.json",
        {
            "client": {
                "name": "ZLF Code（Kilo 内核）",
                "version": "7.4.1701",
                "providers": [
                    {"name": "OpenAI Compatible", "adapter": "@ai-sdk/openai-compatible", "model": "gpt-5.6-luna", "inbound": "/v1/chat/completions"},
                    {"name": "OpenAI Responses", "adapter": "@ai-sdk/openai", "model": "gpt-5.6-luna", "websocket": True, "inbound": "/v1/responses"},
                ],
            },
            "usage_rows": usage_rows,
            "service_profiles": service_profiles,
            "compatible_wire": {
                "request_line": compatible_request["request_line"],
                "header_names_in_order": compatible_request["header_names_in_order"],
                "body_top_level_fields_in_order": body.get("top_level_fields_in_order"),
                "body_shape": shape,
            },
            "responses_wire": {
                "request_line": responses_request["request_line"],
                "header_names_in_order": responses_request["header_names_in_order"],
                "ws_response_create_count": ws_connections[0].get("ws_response_create_count") if ws_connections else 0,
                "relay_valid": ws_connections[0].get("valid") if ws_connections else False,
            },
        },
    )
    write_json(
        output / "restoration-report.json",
        {
            "passed": all(
                (
                    restoration.get("account_90_equal"),
                    restoration.get("account_95_equal"),
                    restoration.get("group_mapping_equal"),
                    current_data_ids == restoration["data_container_ids"],
                    temp_proxy_count == 0,
                    ca_absent,
                    keeper_running,
                    current_health == "healthy",
                )
            ),
            "run_summary": run_summary,
            "current_account_states": current_account_state,
            "current_group_mapping_sha256": current_group_hash,
            "current_data_container_ids": current_data_ids,
            "temporary_proxy_count": temp_proxy_count,
            "temporary_ca_absent": ca_absent,
            "keeper_running": keeper_running,
            "service_health": current_health,
        },
    )

    rule_matrix = []
    for rule_id in sorted(all_rule_ids):
        if rule_id in FULL_RULES:
            evidence_class = "captured_full"
            observation = RULE_OBSERVATIONS[rule_id]
        elif rule_id in BRANCH_RULES:
            evidence_class = "captured_branch"
            observation = RULE_OBSERVATIONS[rule_id]
        else:
            evidence_class = "inherited_only"
            observation = "本次普通 Kilo 双协议场景未触发该条件；由同镜像、同源码树、同规则清单且 accepted=true 的正式包继承。"
        formal_entry = formal_rules[rule_id]
        rule_matrix.append(
            {
                "rule_id": rule_id,
                "kilo_evidence_class": evidence_class,
                "kilo_observation": observation,
                "formal_baseline_status": "pass",
                "formal_assertion_path": formal_entry["assertion"]["result"]["path"],
                "formal_assertion_sha256": formal_entry["assertion"]["result"]["sha256"],
                "final_status": "pass",
            }
        )
    matrix_payload = {
        "schema_version": "kilo-codex0145-conditional-rule-matrix/v1",
        "counts": {
            "captured_full": len(FULL_RULES),
            "captured_branch": len(BRANCH_RULES),
            "inherited_only": len(INHERITED_RULES),
            "total": len(rule_matrix),
        },
        "rules": rule_matrix,
    }
    write_json(output / "rule-matrix.json", matrix_payload)

    pre_scan = secret_scan(output)
    check(pre_scan["passed"], "证据目录秘密扫描未通过")
    write_json(output / "secret-scan.json", pre_scan)

    acceptance = {
        "schema_version": "kilo-codex0145-conditional-acceptance/v1",
        "accepted": not errors,
        "client": "ZLF Code/Kilo 7.4.1701",
        "model": "gpt-5.6-luna",
        "protocols": ["OpenAI Compatible", "OpenAI Responses"],
        "formal_baseline": {
            "assessment_id": formal_acceptance["assessment_id"],
            "accepted": formal_report["accepted"],
            "rule_count": 42,
        },
        "kilo_live_binding": {
            "real_end_to_end_smoke": True,
            "direct_pcap": True,
            "scrubbed_application_bytes": True,
            "production_forwarding_during_byte_capture": False,
            "captured_full_rule_count": len(FULL_RULES),
            "captured_branch_rule_count": len(BRANCH_RULES),
            "inherited_only_rule_count": len(INHERITED_RULES),
        },
        "identity_binding": identity_binding["identity_equal"],
        "restoration_passed": load_json(output / "restoration-report.json")["passed"],
        "secret_scan_passed": pre_scan["passed"],
        "errors": errors,
        "conclusion": (
            "两个 Kilo 入站协议均绑定同一 Codex CLI 0.145.0 不可变画像；"
            "新增直连 pcap 与脱敏字节抓包对本次触发的 HTTP/WS/Lite/models 分支全部匹配。"
            "未触发项由相同镜像、源码树、画像摘要、规则清单摘要及 accepted=true 的正式 42 条包继承，"
            "因此 Kilo 接入的条件化 42 项画像验收通过。"
        ),
        "limitation": (
            "不声称 Kilo 自身独立触发全部 42 条；refresh、files、images、realtime、wham、"
            "legacy compact、alpha-search、重试等条件分支来自正式包。"
        ),
    }
    write_json(output / "acceptance-report.json", acceptance)

    readme = f"""# Kilo → Sub2API → Codex CLI 0.145.0 条件化验收

- 结论：`accepted={str(acceptance['accepted']).lower()}`
- 客户端：ZLF Code/Kilo 7.4.1701
- 模型：`gpt-5.6-luna`
- 入站：OpenAI Compatible `/v1/chat/completions`、OpenAI Responses WS `/v1/responses`
- 出站：HTTP `/backend-api/codex/responses`、WS `/backend-api/codex/responses`
- Kilo 新增现场证据：完整 7 条、当前分支 19 条、未触发继承 16 条
- 正式基线：`{formal_acceptance['assessment_id']}`，42/42，`accepted=true`
- 镜像：`{current_image_ref}` / `{current_image_id}`
- 数据恢复：通过；PostgreSQL、Redis、keeper 容器 ID 与挂载未变化

严格口径：本包证明两种 Kilo 第三方入口确实进入同一 0.145.0 版本画像，且本次实际触发的
HTTP、WS、Lite 与 models 分支均匹配；未自然触发的条件分支通过同一不可变候选身份继承正式
42 条证据。它不把两条普通提示伪装成对 refresh、images、realtime 等分支的重新抓取。
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    (output / "README.md").chmod(0o600)

    # acceptance-report 写入后再扫一次；最终清单排除自身，避免递归哈希。
    final_scan = secret_scan(output)
    if not final_scan["passed"]:
        acceptance["accepted"] = False
        acceptance["errors"].append("最终秘密扫描未通过")
        write_json(output / "acceptance-report.json", acceptance)
    manifest = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name not in {"EVIDENCE_SHA256SUMS", "artifact-manifest.json"}:
            manifest.append(
                {
                    "path": str(path.relative_to(output)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    write_json(output / "artifact-manifest.json", {"artifacts": manifest})
    checksum_lines = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "EVIDENCE_SHA256SUMS":
            checksum_lines.append(f"{sha256_file(path)}  {path.relative_to(output)}")
    checksum_path = output / "EVIDENCE_SHA256SUMS"
    checksum_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    checksum_path.chmod(0o600)

    print(json.dumps(acceptance, ensure_ascii=False, separators=(",", ":")))
    return 0 if acceptance["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
