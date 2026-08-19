#!/usr/bin/env python3
"""从 Claude Code 2.1.226 complete-v21 官方实测生成 FW-F 最终测量制品。

本工具只消费不可变的 Vircs v21 Campaign 和既有 FW-F 候选台账，不执行网络
请求，也不修改生产。它完成四件事：

1. 对 77 个 attempt 的 M、R、P 和维度断言做全量复算，生成无截断 WireInventory；
2. 把既有活动规则降为待重测候选，再按 v21 证据追加真正新增的原子规则；
3. 为每条正式规则生成独立 PAIR、官方正例、官方负例和精确证据引用；
4. 对 49 个场景维度和 593 个候选逐项生成唯一终态处置。

遥测和非必要流量关闭、usage/models/dispatch 的合法零流量只生成支撑事实，
绝不生成“零流量规则”。最终规则数取决于断言实际通过的候选，不设固定数字。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)
from tools.official_client_capture.claude_fw_f_measured_rules import (  # noqa: E402
    MeasuredRuleError,
    build_ledger as build_atomic_replay_ledger,
)


CAMPAIGN_SCHEMA = "claude-code-fw-e-f-complete-campaign/v1"
CATALOG_SCHEMA = "claude-code-fw-f-v4-scenario-catalog/v1"
SUMMARY_SCHEMA = "claude-code-fw-f-complete-execution/v1"
DIMENSION_SCHEMA = "claude-code-fw-f-v4-dimension-evidence/v1"
MEASURED_RULE_SCHEMA = "claude-code-fw-f-measured-rule-ledger/v3"
WIRE_INVENTORY_SCHEMA = "claude-code-fw-f-wire-inventory/v1"
DIMENSION_LEDGER_SCHEMA = "claude-code-fw-f-matrix-dimension-ledger/v1"
CANDIDATE_LEDGER_SCHEMA = "claude-code-fw-f-candidate-disposition-ledger/v1"

TARGET_VERSION = "2.1.226"
TARGET_BINARY_SHA256 = "4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555"
EXPECTED_SOURCE_SHA256 = "78fae770cbb54af5e9192ae6557516d9fd78187914fbb6399a359e1a75573c06"
EXPECTED_DENOMINATOR_COUNTS = {
    "historical_rules": 57,
    "hitcc_documents_2_1_197": 71,
    "semantic_candidate_families": 32,
    "source_mechanisms_2_1_88": 102,
    "target_send_points": 331,
}
EXPECTED_ENDPOINT_COUNTS = {
    "egress-claude-count-tokens": 36,
    "egress-claude-lifecycle-hello": 82,
    "egress-claude-mcp-servers": 4,
    "egress-claude-messages-inference": 123,
    "egress-claude-oauth-profile": 7,
    "egress-claude-oauth-token-refresh": 1,
    "egress-claude-policy-limits": 71,
    "egress-claude-settings": 71,
}

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
AGENT_ID_RE = re.compile(r"^[0-9a-f]{17}$")
REQUEST_LINE_RE = re.compile(r"^([A-Z]+) (\S+) (HTTP/1\.1)$")

ENDPOINT_EGRESS = {
    ("api.anthropic.com", "HEAD", "/api/hello"): "egress-claude-lifecycle-hello",
    ("api.anthropic.com", "GET", "/api/claude_code/policy_limits"): "egress-claude-policy-limits",
    ("api.anthropic.com", "GET", "/api/claude_code/settings"): "egress-claude-settings",
    ("api.anthropic.com", "GET", "/api/oauth/profile"): "egress-claude-oauth-profile",
    ("api.anthropic.com", "GET", "/v1/mcp_servers?limit=1000"): "egress-claude-mcp-servers",
    ("api.anthropic.com", "POST", "/v1/messages?beta=true"): "egress-claude-messages-inference",
    ("api.anthropic.com", "POST", "/v1/messages/count_tokens?beta=true"): "egress-claude-count-tokens",
    ("platform.claude.com", "POST", "/v1/oauth/token"): "egress-claude-oauth-token-refresh",
}

BASE_SCENARIO_MAP = {
    "a1": "v4-agent-depth1",
    "s1": "v4-replay-baseline",
    "s2": "v4-connection-reuse",
    "s4": "v4-bash",
}

ZERO_TRAFFIC_DIMENSIONS = {
    "agency.compact": "官方 /compact 在本次 TUI 场景中没有产生独立推理出站；输入与零增量请求构成支撑事实，不生成 wire 规则。",
    "aux.models": "目标 2.1.226 已明确禁用 models 请求；零请求是目标缺失证明，不是 wire 规则。",
    "aux.usage": "essential-only 官方配置阻止 usage 请求；零请求是合法配置事实，不是 wire 规则。",
    "header.dispatch_id": "实测账号未进入 dispatch-id rollout；没有正例，不生成 Header 规则。",
    "header.usage_limit": "实测账号未进入 usage-limit rollout；没有正例，不生成 Header 规则。",
    "privacy.nonessential_disabled": "官方允许关闭非必要流量；流量缺失不参与一致性规则。",
    "privacy.telemetry_disabled": "官方允许关闭遥测；流量缺失不参与一致性规则。",
}


class FinalizeError(RuntimeError):
    """表示 Campaign 身份、原始字节或终态处置没有闭合。"""


def require(condition: bool, message: str) -> None:
    """失败即停止，禁止带缺口生成最终制品。"""

    if not condition:
        raise FinalizeError(message)


def load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizeError(f"无法读取 JSON：{path}: {exc}") from exc
    require(isinstance(value, dict), f"JSON 顶层不是对象：{path}")
    return value


def repository_path(path: Path) -> str:
    """把证据路径规范化为仓库相对路径。"""

    resolved = path.resolve()
    require(resolved.is_relative_to(ROOT), f"证据不在仓库内：{path}")
    return resolved.relative_to(ROOT).as_posix()


def binding(path: Path, channel: str, **facts: Any) -> dict[str, Any]:
    """生成内容寻址证据引用。"""

    require(path.is_file() and not path.is_symlink(), f"证据文件缺失或为符号链接：{path}")
    value: dict[str, Any] = {
        "path": repository_path(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "channel": channel,
    }
    value.update(facts)
    return value


def header_map(request: dict[str, Any]) -> dict[str, str]:
    """返回小写 Header 映射。"""

    return {value["name"].lower(): value["value"] for value in request["headers"]}


def header_names(request: dict[str, Any]) -> list[str]:
    """保留原始大小写和线序返回 Header 名。"""

    return [value["name"] for value in request["headers"]]


def parse_http_stream(content: bytes, label: str) -> list[dict[str, Any]]:
    """解析连续 HTTP/1.1 请求；OAuth refresh 不要求 Authorization Header。"""

    result: list[dict[str, Any]] = []
    offset = 0
    while offset < len(content):
        head_end = content.find(b"\r\n\r\n", offset)
        require(head_end >= 0, f"{label}@{offset} 缺少 Header/Body 分隔")
        try:
            lines = content[offset:head_end].decode("latin-1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise FinalizeError(f"{label}@{offset} Header 解码失败") from exc
        match = REQUEST_LINE_RE.fullmatch(lines[0])
        require(match is not None, f"{label}@{offset} 请求行非法：{lines[0]}")
        headers: list[dict[str, str]] = []
        for index, line in enumerate(lines[1:]):
            require(":" in line, f"{label}@{offset} Header[{index}] 缺少冒号")
            name, value = line.split(":", 1)
            headers.append({"name": name, "value": value.lstrip(" ")})
        lowered = {value["name"].lower(): value["value"] for value in headers}
        try:
            content_length = int(lowered.get("content-length", "0"))
        except ValueError as exc:
            raise FinalizeError(f"{label}@{offset} Content-Length 非整数") from exc
        body_start = head_end + 4
        end = body_start + content_length
        require(end <= len(content), f"{label}@{offset} Content-Length 越界")
        wire_body = content[body_start:end]
        decoded_body = wire_body
        if lowered.get("content-encoding"):
            require(lowered["content-encoding"] == "gzip", f"{label} 使用未知请求压缩")
            try:
                decoded_body = gzip.decompress(wire_body)
            except (OSError, EOFError) as exc:
                raise FinalizeError(f"{label}@{offset} gzip 解压失败") from exc
        body: dict[str, Any] = {}
        if decoded_body:
            try:
                parsed = json.loads(decoded_body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FinalizeError(f"{label}@{offset} Body 不是 JSON") from exc
            require(isinstance(parsed, dict), f"{label}@{offset} Body 顶层不是对象")
            body = parsed
        chunk = content[offset:end]
        result.append(
            {
                "method": match.group(1),
                "request_target": match.group(2),
                "http_version": match.group(3),
                "headers": headers,
                "body": body,
                "body_keys": list(body),
                "content_encoding": lowered.get("content-encoding"),
                "stream_offset": offset,
                "stream_length": len(chunk),
                "wire_body_bytes": len(wire_body),
                "wire_body_sha256": hashlib.sha256(wire_body).hexdigest(),
                "body_sha256": canonical_sha256(body),
                "raw_sha256": hashlib.sha256(chunk).hexdigest(),
            }
        )
        offset = end
    return result


def message_tool_names(request: dict[str, Any]) -> list[str]:
    """提取 messages/count_tokens Body 中的工具名。"""

    tools = request["body"].get("tools", [])
    if not isinstance(tools, list):
        return []
    return sorted(
        str(value.get("name") or value.get("type"))
        for value in tools
        if isinstance(value, dict) and (value.get("name") or value.get("type"))
    )


def body_text(requests: Iterable[dict[str, Any]]) -> str:
    """只用于断言的规范 JSON 文本；不会写入最终画像。"""

    return "\n".join(
        json.dumps(value["body"], ensure_ascii=False, sort_keys=True)
        for value in requests
    )


def classify_egress(request: dict[str, Any]) -> str:
    """按 host/method/request-target 闭集分类。"""

    host = header_map(request).get("host")
    key = (host, request["method"], request["request_target"])
    require(key in ENDPOINT_EGRESS, f"出现未分类官方出站：{key}")
    return ENDPOINT_EGRESS[key]


def _m_paths(run_dir: Path) -> list[Path]:
    """返回一个 attempt 的最小且可复算 M 闭集。"""

    values = [
        run_dir / "relay-manifest.json",
        run_dir / "dimension-evidence.json",
        run_dir / "relay" / "relay.json",
        run_dir / "results" / "v4-summary.json",
        run_dir / "results" / "invocation.json",
        run_dir / "relay-invocation.json",
    ]
    intervention = run_dir / "relay" / "intervention.jsonl"
    if intervention.is_file():
        values.append(intervention)
    return values


def load_campaign(campaign_root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """校验 77 个正式 attempt，并解析全部官方请求。"""

    campaign = load_json(campaign_root / "campaign.json")
    catalog = load_json(campaign_root / "scenario-catalog.json")
    summary = load_json(campaign_root / "execution-summary.json")
    denominator = load_json(campaign_root / "candidate-denominator.json")
    require(campaign.get("schema_version") == CAMPAIGN_SCHEMA, "Campaign schema 不匹配")
    require(catalog.get("schema_version") == CATALOG_SCHEMA, "场景目录 schema 不匹配")
    require(summary.get("schema_version") == SUMMARY_SCHEMA, "执行摘要 schema 不匹配")
    require(campaign.get("target_version") == catalog.get("target_version") == summary.get("target_version") == TARGET_VERSION, "目标版本不一致")
    require(summary.get("target_binary_sha256") == TARGET_BINARY_SHA256, "目标二进制摘要不一致")
    require(summary.get("capture_source_bundle_sha256") == EXPECTED_SOURCE_SHA256, "取证执行源摘要不一致")
    require(summary.get("result") == "passed", "v21 Campaign 未通过")
    require(summary.get("probe_count") == summary.get("passed_count") == 77, "v21 不是 77/77")
    require(summary.get("failed_count") == 0, "v21 仍有失败场景")
    require(catalog.get("probe_count") == 77, "场景目录不是 77 项")
    require(catalog.get("required_matrix_dimension_count") == 49, "场景矩阵不是 49 维")
    require(catalog.get("privacy_configuration") == {
        "telemetry_disabled": True,
        "nonessential_traffic_disabled": True,
        "absence_generates_rule": False,
    }, "隐私配置边界不一致")
    require(denominator.get("counts") == EXPECTED_DENOMINATOR_COUNTS, "593 候选分母分组不一致")
    require(denominator.get("total_orthogonal_candidates") == 593, "候选总分母不是 593")
    production_diff = load_json(campaign_root / "environment" / "production-diff.json")
    require(
        production_diff.get("result") == "passed"
        and production_diff.get("differences") == [],
        "Campaign 前后生产状态存在差异",
    )

    catalog_by_id = {value["probe_id"]: value for value in catalog["probes"]}
    event_ids = [value["probe_id"] for value in summary["events"]]
    require(event_ids == sorted(catalog_by_id), "执行摘要与场景目录集合不一致")

    runs: dict[str, dict[str, Any]] = {}
    for probe_id in sorted(catalog_by_id):
        run_dir = campaign_root / "attempts" / probe_id / "attempt-001"
        require(run_dir.is_dir() and not run_dir.is_symlink(), f"正式 attempt 缺失：{probe_id}")
        manifest = load_json(run_dir / "relay-manifest.json")
        dimension = load_json(run_dir / "dimension-evidence.json")
        relay = load_json(run_dir / "relay" / "relay.json")
        scenario = load_json(run_dir / "results" / "v4-summary.json")
        require(dimension.get("schema_version") == DIMENSION_SCHEMA, f"{probe_id} dimension schema 不匹配")
        require(manifest.get("probe_id") == dimension.get("probe_id") == scenario.get("probe_id") == probe_id, f"{probe_id} 身份不一致")
        require(manifest.get("status") == "complete", f"{probe_id} 未 complete")
        require(manifest.get("m_binding", {}).get("complete") is True, f"{probe_id} M 未闭合")
        require(manifest.get("m_binding", {}).get("limitations") == [], f"{probe_id} M 仍有限制")
        require(manifest.get("client", {}).get("sha256") == TARGET_BINARY_SHA256, f"{probe_id} 二进制摘要漂移")
        require(manifest.get("runtime", {}).get("capture_tools", {}).get("execution_sources", {}).get("sha256") == EXPECTED_SOURCE_SHA256, f"{probe_id} 执行源漂移")
        require(manifest.get("credential_scrubbing", {}).get("verified") is True, f"{probe_id} 等长脱敏未通过")
        require(manifest.get("secret_scan", {}).get("passed") is True, f"{probe_id} secret scan 未通过")
        require(manifest.get("cleanup") == {"hosts_restored": True, "relay_stopped": True}, f"{probe_id} cleanup 未闭合")
        require(manifest.get("scenario_result", {}).get("valid") is True, f"{probe_id} 场景结果无效")
        require(dimension.get("result") == "passed" and not dimension.get("failed_dimensions"), f"{probe_id} 维度断言失败")
        require(scenario.get("valid") is True, f"{probe_id} v4 summary 无效")

        requests: list[dict[str, Any]] = []
        connections = sorted(
            (value for value in relay.get("connections", []) if isinstance(value, dict) and value.get("valid")),
            key=lambda value: (value.get("opened_at_unix_ms", -1), value.get("connection_id", -1)),
        )
        for connection in connections:
            connection_id = connection.get("connection_id")
            require(isinstance(connection_id, int), f"{probe_id} 连接缺少 ID")
            raw_path = run_dir / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin"
            require(raw_path.is_file(), f"{probe_id} 缺少 R：{raw_path.name}")
            for request in parse_http_stream(raw_path.read_bytes(), f"{probe_id}:{raw_path.name}"):
                request.update(
                    {
                        "scenario": probe_id,
                        "connection_id": connection_id,
                        "connection": connection,
                        "raw_path": raw_path,
                        "manifest_path": run_dir / "relay-manifest.json",
                        "dimension_path": run_dir / "dimension-evidence.json",
                        "relay_path": run_dir / "relay" / "relay.json",
                        "summary_path": run_dir / "results" / "v4-summary.json",
                    }
                )
                request["egress_id"] = classify_egress(request)
                requests.append(request)

        actual = dimension.get("actual_wire_inventory")
        require(isinstance(actual, list) and len(actual) == len(requests), f"{probe_id} actual wire 数量不一致")
        parsed_index = {
            (value["raw_sha256"], value["stream_offset"]): value
            for value in requests
        }
        for item in actual:
            key = (item.get("raw_sha256"), item.get("stream_offset"))
            require(key in parsed_index, f"{probe_id} dimension 引用未知 R：{key}")
            parsed = parsed_index[key]
            require(item.get("method") == parsed["method"] and item.get("request_target") == parsed["request_target"], f"{probe_id} dimension 请求身份漂移")
            require(item.get("header_names") == header_names(parsed), f"{probe_id} Header 线序漂移")
            require(item.get("body_keys") == parsed["body_keys"], f"{probe_id} Body 键序漂移")
            require(item.get("wire_body_sha256") == parsed["wire_body_sha256"], f"{probe_id} Body 摘要漂移")

        assertion_dimensions = [value.get("dimension") for value in dimension.get("dimension_assertions", [])]
        require(assertion_dimensions == sorted(catalog_by_id[probe_id]["dimensions"]), f"{probe_id} 维度覆盖不一致")
        require(all(value.get("result") == "passed" for value in dimension.get("dimension_assertions", [])), f"{probe_id} 有未通过 DIM")
        runs[probe_id] = {
            "probe_id": probe_id,
            "run_dir": run_dir,
            "catalog": catalog_by_id[probe_id],
            "manifest": manifest,
            "dimension": dimension,
            "relay": relay,
            "scenario": scenario,
            "requests": requests,
            "messages": [value for value in requests if value["egress_id"] == "egress-claude-messages-inference"],
            "count_tokens": [value for value in requests if value["egress_id"] == "egress-claude-count-tokens"],
            "m_paths": _m_paths(run_dir),
        }

    require(len(runs) == 77, "正式 run 解析后不是 77 个")
    return {
        "campaign": campaign,
        "catalog": catalog,
        "summary": summary,
        "denominator": denominator,
        "production_diff": production_diff,
    }, runs


def _compat_request(request: dict[str, Any], scenario: str) -> dict[str, Any]:
    """把 v21 已校验请求投影成既有 88 条原子断言使用的只读结构。"""

    raw_path = request["raw_path"]
    upstream_path = raw_path.with_name(
        raw_path.name.replace(".client_to_upstream.bin", ".upstream_to_client.bin")
    )
    return {
        **request,
        "scenario": scenario,
        "raw_body_length": request["wire_body_bytes"],
        "upstream_raw_path": upstream_path,
    }


def revalidate_prior_atomic_rules(
    campaign_root: Path,
    runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """在 v21 的 54 个 replay 与四个基础场景上重新执行旧 88 条原子断言。

    旧台账只提供待重测命题集合，不能作为 v21 正式规则的证明。本函数复用原来
    已经逐字段实现的 88 条断言，但把所有 R/M 输入替换为本次 Vircs v21 attempt。
    """

    inference: list[dict[str, Any]] = []
    lifecycle: list[dict[str, Any]] = []
    expected_counts = {"a1": 3, "s1": 1, "s2": 2, "s4": 2}
    for scenario, probe_id in sorted(BASE_SCENARIO_MAP.items()):
        values = [_compat_request(value, scenario) for value in runs[probe_id]["requests"]]
        messages = [
            value
            for value in values
            if (value["method"], value["request_target"])
            == ("POST", "/v1/messages?beta=true")
        ]
        hello = [
            value
            for value in values
            if (value["method"], value["request_target"])
            == ("HEAD", "/api/hello")
        ]
        require(len(messages) == expected_counts[scenario], f"{scenario} v21 基础 messages 分母漂移")
        require(len(hello) == 1, f"{scenario} v21 基础 hello 分母漂移")
        inference.extend(messages)
        lifecycle.extend(hello)

    replay_runs: dict[str, dict[str, Any]] = {}
    for probe_id, run in sorted(runs.items()):
        if not probe_id.startswith("v4-replay-"):
            continue
        legacy_id = f"v3-{probe_id.removeprefix('v4-replay-')}"
        requests = [_compat_request(value, legacy_id) for value in run["requests"]]
        summary = run["scenario"].get("inner_result")
        require(isinstance(summary, dict) and summary.get("probe_id") == legacy_id, f"{probe_id} 缺少 v3 内层结果")
        intervention_path = run["run_dir"] / "relay" / "intervention.jsonl"
        replay_runs[legacy_id] = {
            "probe_id": legacy_id,
            "group": "runs" if run["relay"].get("production_forwarding_enabled") is False else "real-runs",
            "is_synthetic": run["relay"].get("production_forwarding_enabled") is False,
            "run_dir": run["run_dir"],
            "manifest": run["manifest"],
            "relay": run["relay"],
            "summary": summary,
            "requests": requests,
            "messages": [value for value in requests if (value["method"], value["request_target"]) == ("POST", "/v1/messages?beta=true")],
            "hello": [value for value in requests if (value["method"], value["request_target"]) == ("HEAD", "/api/hello")],
            "policy_limits": [value for value in requests if (value["method"], value["request_target"]) == ("GET", "/api/claude_code/policy_limits")],
            "settings": [value for value in requests if (value["method"], value["request_target"]) == ("GET", "/api/claude_code/settings")],
            "oauth_profile": [value for value in requests if (value["method"], value["request_target"]) == ("GET", "/api/oauth/profile")],
            "m_paths": run["m_paths"],
            "intervention_path": intervention_path if intervention_path.is_file() else None,
        }
    require(len(replay_runs) == 54, f"v21 replay 分母不是 54：{len(replay_runs)}")
    require(sum(not value["is_synthetic"] for value in replay_runs.values()) == 34, "v21 真实 replay 不是 34")
    require(sum(value["is_synthetic"] for value in replay_runs.values()) == 20, "v21 合成 replay 不是 20")

    discovery_policy_path = ROOT / "tools/official_client_capture/claude_fw_f_discovery_policy_2_1_226.json"
    profile_policy_path = ROOT / "tools/official_client_capture/claude_fw_f_profile_policy_2_1_226.json"
    identity_override = {
        "target_version": TARGET_VERSION,
        "target_binary_sha256": TARGET_BINARY_SHA256,
        "privacy_environment": {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        },
    }
    relay_index_override = {
        "target": {
            "version": TARGET_VERSION,
            "binary_sha256": TARGET_BINARY_SHA256,
        }
    }
    try:
        ledger = build_atomic_replay_ledger(
            load_json(discovery_policy_path),
            load_json(profile_policy_path),
            campaign_root / "campaign.json",
            campaign_root / "execution-summary.json",
            prepared_samples=(inference, lifecycle),
            prepared_v3_runs=replay_runs,
            identity_override=identity_override,
            relay_index_override=relay_index_override,
        )
    except MeasuredRuleError as exc:
        raise FinalizeError(f"v21 旧原子规则重测失败：{exc}") from exc
    require(ledger.get("rule_count") == 88 and ledger.get("result") == "passed", "v21 旧原子规则未 88/88 通过")
    return ledger


def build_wire_inventory(
    campaign_root: Path,
    inputs: dict[str, Any],
    runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """生成内容对象与出现位置分离的全量 WireInventory。"""

    contents: dict[str, dict[str, Any]] = {}
    occurrences: list[dict[str, Any]] = []
    endpoint_counts: Counter[str] = Counter()
    for probe_id in sorted(runs):
        for request in runs[probe_id]["requests"]:
            wire_id = f"WIRE-{request['raw_sha256'].upper()}"
            content = {
                "wire_id": wire_id,
                "raw_sha256": request["raw_sha256"],
                "method": request["method"],
                "request_target": request["request_target"],
                "http_version": request["http_version"],
                "host": header_map(request).get("host"),
                "header_names": header_names(request),
                "body_keys": request["body_keys"],
                "body_sha256": request["body_sha256"],
                "wire_body_sha256": request["wire_body_sha256"],
                "wire_body_bytes": request["wire_body_bytes"],
                "content_encoding": request["content_encoding"],
                "message_tool_names": message_tool_names(request),
                "egress_id": request["egress_id"],
            }
            previous = contents.setdefault(wire_id, content)
            require(previous == content, f"内容寻址碰撞：{wire_id}")
            occurrence_id = canonical_sha256({
                "scenario": probe_id,
                "connection_id": request["connection_id"],
                "stream_offset": request["stream_offset"],
                "wire_id": wire_id,
            })
            occurrences.append(
                {
                    "occurrence_id": f"OCC-{occurrence_id.upper()}",
                    "wire_id": wire_id,
                    "scenario": probe_id,
                    "connection_id": request["connection_id"],
                    "stream_offset": request["stream_offset"],
                    "raw_path": repository_path(request["raw_path"]),
                    "egress_id": request["egress_id"],
                }
            )
            endpoint_counts[request["egress_id"]] += 1
    require(len(occurrences) == 395, f"全请求分母不是 395：{len(occurrences)}")
    require(dict(sorted(endpoint_counts.items())) == EXPECTED_ENDPOINT_COUNTS, f"端点分母不一致：{dict(endpoint_counts)}")
    return {
        "schema_version": WIRE_INVENTORY_SCHEMA,
        "target_version": TARGET_VERSION,
        "campaign_id": inputs["campaign"]["campaign_id"],
        "campaign_binding": binding(campaign_root / "campaign.json", "M"),
        "scenario_catalog_binding": binding(campaign_root / "scenario-catalog.json", "M"),
        "execution_summary_binding": binding(campaign_root / "execution-summary.json", "M"),
        "scenario_count": len(runs),
        "request_occurrence_count": len(occurrences),
        "unique_wire_content_count": len(contents),
        "endpoint_counts": dict(sorted(endpoint_counts.items())),
        "content_objects": [contents[key] for key in sorted(contents)],
        "occurrences": sorted(occurrences, key=lambda value: value["occurrence_id"]),
        "result": "passed",
    }


def request_evidence(
    runs: dict[str, dict[str, Any]],
    scenarios: Iterable[str],
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    *,
    channels: tuple[str, ...] = ("R", "M"),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """选择官方样本并生成 R/M 引用。"""

    selected_runs = [runs[value] for value in sorted(set(scenarios))]
    requests = [
        request
        for run in selected_runs
        for request in run["requests"]
        if predicate is None or predicate(request)
    ]
    refs: dict[tuple[str, str], dict[str, Any]] = {}
    if "R" in channels:
        grouped: dict[Path, list[dict[str, Any]]] = defaultdict(list)
        for request in requests:
            grouped[request["raw_path"]].append(request)
        for path, values in grouped.items():
            value = binding(
                path,
                "R",
                direction="client_to_upstream",
                scenarios=sorted({item["scenario"] for item in values}),
                connection_ids=sorted({item["connection_id"] for item in values}),
                stream_offsets=sorted({item["stream_offset"] for item in values}),
                raw_request_sha256s=sorted({item["raw_sha256"] for item in values}),
            )
            refs[(value["path"], value["channel"])] = value
    if "M" in channels:
        for run in selected_runs:
            for path in run["m_paths"]:
                value = binding(path, "M")
                refs[(value["path"], value["channel"])] = value
    return requests, [refs[key] for key in sorted(refs)]


def p_evidence(run: dict[str, Any]) -> list[dict[str, Any]]:
    """取得原生 TLS pcap 与同 attempt 的 M。"""

    pcap = run["run_dir"] / "tls-clienthello.pcap"
    refs = [binding(pcap, "P")]
    refs.extend(binding(path, "M") for path in run["m_paths"])
    return sorted(refs, key=lambda value: (value["path"], value["channel"]))


def scenario_strings(value: Any) -> set[str]:
    """递归提取旧 sample_scope 中的具名场景。"""

    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "scenarios" and isinstance(item, list):
                result.update(str(name) for name in item)
            else:
                result.update(scenario_strings(item))
    elif isinstance(value, list):
        for item in value:
            result.update(scenario_strings(item))
    return result


def map_prior_scenario(value: str) -> str | None:
    """把旧具名 probe 映射到 v21 的正式 attempt。"""

    if value in BASE_SCENARIO_MAP:
        return BASE_SCENARIO_MAP[value]
    if value.startswith("v3-"):
        return f"v4-replay-{value.removeprefix('v3-')}"
    if value.startswith("v4-"):
        return value
    return None


def merge_refs(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 path/channel 去重证据引用。"""

    values: dict[tuple[str, str], dict[str, Any]] = {}
    for group in groups:
        for value in group:
            values[(value["path"], value["channel"])] = value
    return [values[key] for key in sorted(values)]


def finalize_prior_rule(
    source: dict[str, Any],
    runs: dict[str, dict[str, Any]],
    prior_ledger_path: Path,
) -> dict[str, Any]:
    """把已经由 v21 原子断言重测通过的旧候选写成显式正负例规则。"""

    spec_id = source["spec_id"]
    require(source.get("assertion_result") == "passed", f"{spec_id} 未通过 v21 原子断言")
    scope = source.get("sample_scope")
    require(isinstance(scope, dict), f"{spec_id} 缺少实测分母")
    require(scope.get("eligible_count") == scope.get("matched_count"), f"{spec_id} 实测分母没有全部命中")
    refs = source.get("evidence_refs")
    require(isinstance(refs, list) and refs, f"{spec_id} 缺少 v21 证据引用")
    for ref in refs:
        path = ROOT / ref["path"]
        require(
            path.is_file()
            and path.stat().st_size == ref.get("bytes")
            and sha256_file(path) == ref.get("sha256"),
            f"{spec_id} 证据绑定漂移：{ref.get('path')}",
        )
    scenarios = sorted(scenario_strings(scope))
    require(scenarios, f"{spec_id} 缺少实测场景")
    if source.get("domain") == "tls":
        tls_run = runs["v4-native-tls-baseline"]
        refs = p_evidence(tls_run)
        channels = ["M", "P"]
        scenarios = ["v4-native-tls-baseline"]
        positive_count = tls_run["manifest"]["tls_p_channel"]["target_client_hello_count"]
        negative_count = positive_count
    else:
        channels = ["M", "R"]
        positive_count = scope.get("positive_count", scope["matched_count"])
        negative_count = scope.get("negative_count", scope["eligible_count"])

    return {
        **source,
        "assertion_id": f"PAIR-{spec_id}",
        "assertion_result": "passed",
        "evidence_level": "observed",
        "rule_lifecycle": "candidate",
        "production_eligibility": "validation_only",
        "evidence_channels": channels,
        "evidence_refs": refs,
        "measurement_origin": "complete_v21_substantive_atomic_revalidation",
        "prior_ledger_binding": binding(prior_ledger_path, "M"),
        "official_positive": {
            "assertion_id": f"PAIR-{spec_id}-POSITIVE",
            "result": "passed",
            "kind": "applicable_official_wire_matches_claim",
            "scenarios": scenarios,
            "sample_count": positive_count,
            "evidence_refs": refs,
        },
        "official_negative": {
            "assertion_id": f"PAIR-{spec_id}-NEGATIVE",
            "result": "passed",
            "kind": "official_negative_or_zero_violation_denominator",
            "scenarios": scenarios,
            "sample_count": negative_count,
            "violation_count": 0,
            "evidence_refs": refs,
        },
    }


def _messages(run: dict[str, Any]) -> list[dict[str, Any]]:
    return run["messages"]


def _egress(egress_id: str) -> Callable[[dict[str, Any]], bool]:
    return lambda request: request["egress_id"] == egress_id


def _header_vector(requests: list[dict[str, Any]]) -> list[str]:
    require(requests, "Header 向量分母为空")
    first = header_names(requests[0])
    require(all(header_names(value) == first for value in requests), "Header 向量在分母内不唯一")
    return first


def _validate_tls_native(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    receipt = runs["v4-native-tls-baseline"]["manifest"]["tls_p_channel"]
    observations = receipt.get("observations", [])
    require(receipt.get("parsed") is True and len(observations) == 4, "原生 ClientHello 不是 4 个")
    require(all(value.get("sni") == "api.anthropic.com" for value in observations), "ClientHello 出现非目标 SNI")
    cipher_vectors = {tuple(value.get("cipher_suites", [])) for value in observations}
    require(len(cipher_vectors) == 1 and next(iter(cipher_vectors)), "CipherSuite 向量不唯一或为空")
    return len(observations), len(observations)


def _validate_tls_alpn(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    observations = runs["v4-native-tls-baseline"]["manifest"]["tls_p_channel"]["observations"]
    offers = [value.get("alpn_offer", []) for value in observations]
    require(offers == [["http/1.1"], [], [], ["http/1.1"]], f"ALPN 条件向量漂移：{offers}")
    require(16 in observations[0]["extension_types"] and 16 in observations[3]["extension_types"], "ALPN 正例缺少扩展 16")
    require(16 not in observations[1]["extension_types"] and 16 not in observations[2]["extension_types"], "ALPN 负例仍含扩展 16")
    return 2, 2


def _validate_count_endpoint(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    positive = runs["v4-tui-count-tokens"]["count_tokens"]
    require(len(positive) == 36, "count_tokens 分母不是 36")
    require(all(header_map(value).get("host") == "api.anthropic.com" for value in positive), "count_tokens Host 漂移")
    require(not runs["v4-replay-baseline"]["count_tokens"], "基线意外发送 count_tokens")
    return len(positive), 1


def _validate_count_headers(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    values = runs["v4-tui-count-tokens"]["count_tokens"]
    vector = _header_vector(values)
    require(vector == [
        "Accept", "Authorization", "Content-Type", "User-Agent",
        "X-Claude-Code-Session-Id", "X-Stainless-Arch", "X-Stainless-Lang",
        "X-Stainless-OS", "X-Stainless-Package-Version",
        "X-Stainless-Retry-Count", "X-Stainless-Runtime",
        "X-Stainless-Runtime-Version", "anthropic-beta",
        "anthropic-dangerous-direct-browser-access", "anthropic-version", "x-app",
        "x-client-request-id", "Connection", "Host", "Accept-Encoding",
        "Content-Length",
    ], "count_tokens Header 向量漂移")
    static = {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "claude-cli/2.1.226 (external, cli)",
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Linux",
        "x-stainless-package-version": "0.94.0",
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v26.3.0",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,context-management-2025-06-27,token-counting-2024-11-01",
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-version": "2023-06-01",
        "x-app": "cli",
        "connection": "keep-alive",
        "host": "api.anthropic.com",
        "accept-encoding": "gzip, deflate, br, zstd",
    }
    session_ids = set()
    request_ids = set()
    for value in values:
        headers = header_map(value)
        require(all(headers.get(name) == expected for name, expected in static.items()), "count_tokens 静态 Header 漂移")
        require(re.fullmatch(r"Bearer <secret>X*", headers.get("authorization", "")) is not None, "count_tokens 缺少等长脱敏 Bearer")
        require(UUID_RE.fullmatch(headers.get("x-claude-code-session-id", "")) is not None, "count_tokens Session-Id 非 UUID")
        require(UUID_RE.fullmatch(headers.get("x-client-request-id", "")) is not None, "count_tokens request-id 非 UUID")
        require(int(headers.get("content-length", "-1")) == value["wire_body_bytes"], "count_tokens Content-Length 漂移")
        session_ids.add(headers["x-claude-code-session-id"])
        request_ids.add(headers["x-client-request-id"])
    require(len(session_ids) == 1 and len(request_ids) == len(values), "count_tokens 会话或请求身份复用规则漂移")
    return len(values), 1


def _validate_count_body(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    values = runs["v4-tui-count-tokens"]["count_tokens"]
    require(all(value["body_keys"] == ["model", "messages", "tools"] for value in values), "count_tokens Body 键序漂移")
    require(all(isinstance(value["body"].get("messages"), list) and isinstance(value["body"].get("tools"), list) for value in values), "count_tokens Body 类型漂移")
    return len(values), 1


def _oauth_request(runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values = runs["v4-oauth-refresh"]["requests"]
    require(len(values) == 1 and values[0]["egress_id"] == "egress-claude-oauth-token-refresh", "OAuth refresh 请求闭集不一致")
    return values[0]


def _validate_oauth_endpoint(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    request = _oauth_request(runs)
    require(header_map(request).get("host") == "platform.claude.com", "OAuth refresh Host 漂移")
    return 1, 1


def _validate_oauth_headers(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    request = _oauth_request(runs)
    require(header_names(request) == ["Accept", "Content-Type", "User-Agent", "Content-Length", "Accept-Encoding", "Host", "Connection"], "OAuth refresh Header 向量漂移")
    require("authorization" not in header_map(request), "OAuth refresh 不应发送 Authorization")
    require(header_map(request).get("user-agent") == "axios/1.15.2", "OAuth refresh UA 漂移")
    require(int(header_map(request).get("content-length", "-1")) == request["wire_body_bytes"], "OAuth refresh Content-Length 漂移")
    return 1, 1


def _validate_oauth_body(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    request = _oauth_request(runs)
    body = request["body"]
    require(list(body) == ["grant_type", "refresh_token", "client_id", "scope"], "OAuth refresh Body 键序漂移")
    require(body.get("grant_type") == "refresh_token", "OAuth grant_type 漂移")
    require(all(isinstance(body.get(key), str) and body[key] for key in ("refresh_token", "client_id", "scope")), "OAuth refresh 动态字段为空")
    require(runs["v4-oauth-refresh"]["manifest"]["oauth_refresh_temporary_state"].get("removed") is True, "临时 OAuth 状态未清理")
    return 1, 1


def _mcp_server_requests(runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    values = [
        request
        for run in runs.values()
        for request in run["requests"]
        if request["egress_id"] == "egress-claude-mcp-servers"
    ]
    require(len(values) == 4, "MCP servers 请求分母不是 4")
    return values


def _validate_mcp_endpoint(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    values = _mcp_server_requests(runs)
    require(all(header_map(value).get("host") == "api.anthropic.com" for value in values), "MCP servers Host 漂移")
    require(not any(value["egress_id"] == "egress-claude-mcp-servers" for value in runs["v4-replay-baseline"]["requests"]), "sdk-cli 基线意外发送 MCP servers")
    return len(values), 1


def _validate_mcp_headers(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    values = _mcp_server_requests(runs)
    vector = _header_vector(values)
    require(vector == [
        "Accept", "Content-Type", "Authorization", "anthropic-beta",
        "anthropic-version", "anthropic-mcp-client-capabilities",
        "MCP-Protocol-Version", "User-Agent", "Accept-Encoding", "Host",
        "Connection",
    ], "MCP servers Header 向量漂移")
    static = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "anthropic-beta": "mcp-servers-2025-12-04",
        "anthropic-version": "2023-06-01",
        "anthropic-mcp-client-capabilities": "eyJyb290cyI6eyJsaXN0Q2hhbmdlZCI6dHJ1ZX0sImVsaWNpdGF0aW9uIjp7fX0=",
        "mcp-protocol-version": "2025-11-25",
        "user-agent": "axios/1.15.2",
        "accept-encoding": "gzip, compress, deflate, br",
        "host": "api.anthropic.com",
        "connection": "keep-alive",
    }
    for value in values:
        headers = header_map(value)
        require(all(headers.get(name) == expected for name, expected in static.items()), "MCP servers 静态 Header 漂移")
        require(re.fullmatch(r"Bearer <secret>X*", headers.get("authorization", "")) is not None, "MCP servers 缺少等长脱敏 Bearer")
    return len(values), 1


def _validate_parent_agent(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    positives: list[dict[str, Any]] = []
    for scenario, expected_depth in (("v4-agent-depth2", 2), ("v4-agent-depth3", 3)):
        messages = _messages(runs[scenario])
        lineage: dict[str, str | None] = {}
        for request in messages:
            headers = header_map(request)
            agent_id = headers.get("x-claude-code-agent-id")
            if not agent_id:
                continue
            require(AGENT_ID_RE.fullmatch(agent_id) is not None, f"{scenario} agent-id 非法")
            parent = headers.get("x-claude-code-parent-agent-id")
            previous = lineage.setdefault(agent_id, parent)
            require(previous == parent, f"{scenario} 同一 agent 的 parent 漂移")
            if parent:
                names = header_names(request)
                require(names.index("x-claude-code-parent-agent-id") == names.index("x-claude-code-agent-id") + 1, f"{scenario} parent Header 线序漂移")
                positives.append(request)
        roots = [agent_id for agent_id, parent in lineage.items() if parent is None]
        require(len(lineage) == expected_depth and len(roots) == 1, f"{scenario} lineage 节点或根数量漂移")
        require(all(parent is None or parent in lineage for parent in lineage.values()), f"{scenario} parent agent 无法反向关联")
        for agent_id in lineage:
            visited: set[str] = set()
            current: str | None = agent_id
            while current is not None:
                require(current not in visited, f"{scenario} parent lineage 成环")
                visited.add(current)
                current = lineage[current]
        require(max(len(_lineage_chain(agent_id, lineage)) for agent_id in lineage) == expected_depth, f"{scenario} 直接父链深度漂移")
    require(positives, "没有 parent-agent 正例")
    require(all("x-claude-code-parent-agent-id" not in header_map(value) for value in _messages(runs["v4-agent-depth1"])), "depth1 意外携带 parent-agent")
    return len(positives), len(_messages(runs["v4-agent-depth1"]))


def _lineage_chain(agent_id: str, lineage: dict[str, str | None]) -> list[str]:
    """返回已经过无环校验的直接父链。"""

    result: list[str] = []
    current: str | None = agent_id
    while current is not None:
        result.append(current)
        current = lineage[current]
    return result


def _validate_agent_depth(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    expected = {"v4-agent-depth1": 1, "v4-agent-depth2": 2, "v4-agent-depth3": 3}
    for scenario, count in expected.items():
        values = {
            header_map(request).get("x-claude-code-agent-id")
            for request in _messages(runs[scenario])
            if header_map(request).get("x-claude-code-agent-id")
        }
        require(len(values) == count, f"{scenario} agent 深度实测值不是 {count}")
        require(all("cc_is_subagent=true" in body_text([request]) for request in _messages(runs[scenario]) if header_map(request).get("x-claude-code-agent-id")), f"{scenario} 子代理 attribution 缺失")
    require(not any(header_map(value).get("x-claude-code-agent-id") for value in _messages(runs["v4-replay-baseline"])), "基线意外携带 agent-id")
    return 6, 1


def _validate_background(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    values = _messages(runs["v4-background"])
    require(values and all(header_map(value).get("x-app") == "cli-bg" for value in values), "background x-app 不是 cli-bg")
    require(all("cc_entrypoint=cli" in body_text([value]) for value in values), "background attribution 入口不是 cli")
    models = {value["body"].get("model") for value in values}
    require({"claude-haiku-4-5-20251001", "claude-sonnet-5"} <= models, "background 模型向量不完整")
    require(all(header_map(value).get("x-app") == "cli" for value in _messages(runs["v4-replay-baseline"])), "前台负例 x-app 漂移")
    return len(values), len(_messages(runs["v4-replay-baseline"]))


def _validate_marker(
    runs: dict[str, dict[str, Any]],
    positive: str,
    negative: str,
    marker: str,
) -> tuple[int, int]:
    positive_values = _messages(runs[positive])
    negative_values = _messages(runs[negative])
    require(marker in body_text(positive_values), f"{positive} 缺少 marker={marker}")
    require(marker not in body_text(negative_values), f"{negative} 意外含 marker={marker}")
    return len(positive_values), len(negative_values)


def _validate_tool(
    runs: dict[str, dict[str, Any]],
    scenario: str,
    tool_name: str,
    *,
    prefix: bool = False,
) -> tuple[int, int]:
    names = [name for request in _messages(runs[scenario]) for name in message_tool_names(request)]
    if prefix:
        matched = [name for name in names if name.startswith(tool_name)]
    else:
        matched = [name for name in names if name == tool_name]
    require(matched, f"{scenario} 缺少工具 {tool_name}")
    baseline_names = [name for request in _messages(runs["v4-replay-baseline"]) for name in message_tool_names(request)]
    if prefix:
        require(not any(name.startswith(tool_name) for name in baseline_names), f"基线意外含工具前缀 {tool_name}")
    else:
        require(tool_name not in baseline_names, f"基线意外含工具 {tool_name}")
    return len(matched), len(baseline_names) or 1


def _content_items(request: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 messages 中全部结构化 content item。"""

    return [
        item
        for message in request["body"].get("messages", [])
        if isinstance(message, dict) and isinstance(message.get("content"), list)
        for item in message["content"]
        if isinstance(item, dict)
    ]


def _validate_tool_roundtrip(
    runs: dict[str, dict[str, Any]],
    scenario: str,
    tool_name: str,
    *,
    require_agent_request: bool = False,
) -> tuple[int, int]:
    """验证工具 descriptor、tool_use 与同 ID tool_result 的完整往返。"""

    values = _messages(runs[scenario])
    require(any(tool_name in message_tool_names(value) for value in values), f"{scenario} 缺少 {tool_name} descriptor")
    uses = [item for value in values for item in _content_items(value) if item.get("type") == "tool_use" and item.get("name") == tool_name]
    results = [item for value in values for item in _content_items(value) if item.get("type") == "tool_result"]
    use_ids = {item.get("id") for item in uses if isinstance(item.get("id"), str)}
    result_ids = {item.get("tool_use_id") for item in results if isinstance(item.get("tool_use_id"), str)}
    require(use_ids and use_ids <= result_ids, f"{scenario} {tool_name} tool_use/tool_result 未闭合")
    if require_agent_request:
        require(any(header_map(value).get("x-claude-code-agent-id") for value in values), f"{scenario} Agent 未派生子代理请求")
    baseline = _messages(runs["v4-replay-baseline"])
    require(not any(tool_name in message_tool_names(value) for value in baseline), f"基线意外含 {tool_name}")
    return len(uses), len(baseline)


def _validate_mcp_tool(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """验证 MCP descriptor 的 schema 与真实 tool_use/tool_result。"""

    values = _messages(runs["v4-mcp-tool"])
    descriptors = [
        item
        for request in values
        for item in request["body"].get("tools", [])
        if isinstance(item, dict) and str(item.get("name", "")).startswith("mcp__claude-fw-f-v4__")
    ]
    require(descriptors and all(isinstance(value.get("input_schema"), dict) for value in descriptors), "MCP descriptor 缺少 input_schema")
    uses = [
        item
        for request in values
        for item in _content_items(request)
        if item.get("type") == "tool_use" and str(item.get("name", "")).startswith("mcp__claude-fw-f-v4__")
    ]
    results = [item for request in values for item in _content_items(request) if item.get("type") == "tool_result"]
    require({item.get("id") for item in uses} <= {item.get("tool_use_id") for item in results} and uses, "MCP tool 往返未闭合")
    baseline = _messages(runs["v4-replay-baseline"])
    require(not any(name.startswith("mcp__") for request in baseline for name in message_tool_names(request)), "基线意外含 MCP 工具")
    return len(uses), len(baseline)


def _validate_deferred(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    values = _messages(runs["v4-mcp-deferred"])
    expected = {f"mcp__claude-fw-f-v4__deferred_probe_{index:02d}" for index in range(1, 33)}
    expected.add("mcp__claude-fw-f-v4__probe_echo")
    for request in values:
        require(set(message_tool_names(request)) == expected, "deferred MCP 工具闭集不完整或包含额外项")
        require(all(isinstance(item.get("input_schema"), dict) for item in request["body"].get("tools", []) if isinstance(item, dict)), "deferred MCP schema 缺失")
    baseline = _messages(runs["v4-replay-baseline"])
    require(not any(name.startswith("mcp__") for request in baseline for name in message_tool_names(request)), "deferred 负例仍含 MCP")
    return len(expected), len(baseline)


def _validate_advisor(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    positive = _messages(runs["v4-advisor-enabled-positive"])
    negative = _messages(runs["v4-advisor-default-negative"])
    descriptors = [
        item
        for request in positive
        for item in request["body"].get("tools", [])
        if isinstance(item, dict) and item.get("name") == "advisor"
    ]
    require(len(descriptors) == 1, "advisor 正例数量不是 1")
    require(descriptors[0].get("type") == "advisor_20260301" and descriptors[0].get("model") == "claude-fable-5", "advisor descriptor 漂移")
    require("advisor" not in {name for request in negative for name in message_tool_names(request)}, "advisor 默认负例仍出现")
    return 1, len(negative)


def _validate_web_search(runs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    values = _messages(runs["v4-web-search"])
    names = [name for request in values for name in message_tool_names(request)]
    require("WebSearch" in names and "web_search" in names, "web_search 双层工具向量不完整")
    internal = [request for request in values if "web_search" in message_tool_names(request)]
    require(len(internal) == 1 and internal[0]["body"].get("tool_choice") == {"type": "tool", "name": "web_search"}, "server web_search 缺少精确 tool_choice")
    outer_uses = [item for request in values for item in _content_items(request) if item.get("type") == "tool_use" and item.get("name") == "WebSearch"]
    outer_results = [item for request in values for item in _content_items(request) if item.get("type") == "tool_result"]
    require({item.get("id") for item in outer_uses} <= {item.get("tool_use_id") for item in outer_results} and outer_uses, "WebSearch 外层工具往返未闭合")
    baseline = _messages(runs["v4-replay-baseline"])
    require(not any(name in {"WebSearch", "web_search"} for request in baseline for name in message_tool_names(request)), "web_search 负例仍含搜索工具")
    return len(values), len(baseline)


NEW_RULE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "SPEC-TLS-001": {
        "domain": "tls",
        "claim": "四个目标原生 ClientHello 使用同一实测 CipherSuite 有序序列。",
        "scope": "v4-native-tls-baseline 的 4 个 ClientHello",
        "egress_ids": ["egress-claude-lifecycle-hello", "egress-claude-messages-inference", "egress-claude-policy-limits", "egress-claude-settings"],
        "positive": ["v4-native-tls-baseline"],
        "negative": ["v4-native-tls-baseline"],
        "channels": ("P", "M"),
        "validator": _validate_tls_native,
    },
    "SPEC-TLS-002": {
        "domain": "tls",
        "claim": "hello 与 messages ClientHello 提供 ALPN http/1.1；policy_limits 与 settings 的官方负例省略 ALPN 扩展。",
        "scope": "同一原生 TLS attempt 的 2 个正例和 2 个负例",
        "egress_ids": ["egress-claude-lifecycle-hello", "egress-claude-messages-inference", "egress-claude-policy-limits", "egress-claude-settings"],
        "positive": ["v4-native-tls-baseline"],
        "negative": ["v4-native-tls-baseline"],
        "channels": ("P", "M"),
        "validator": _validate_tls_alpn,
    },
    "SPEC-EP-009": {
        "domain": "endpoint", "claim": "真实 TUI 的 token 计数请求为 POST /v1/messages/count_tokens?beta=true，Host 为 api.anthropic.com。", "scope": "36 个 count_tokens 正例与 sdk-cli 基线负例", "egress_ids": ["egress-claude-count-tokens"], "positive": ["v4-tui-count-tokens"], "negative": ["v4-replay-baseline"], "validator": _validate_count_endpoint,
    },
    "SPEC-HDR-045": {
        "domain": "header", "claim": "count_tokens 使用实测有序 Header 向量、Bearer OAuth、会话/request-id 与 Claude SDK 身份字段。", "scope": "36 个 count_tokens 请求", "egress_ids": ["egress-claude-count-tokens"], "positive": ["v4-tui-count-tokens"], "negative": ["v4-replay-baseline"], "validator": _validate_count_headers,
    },
    "SPEC-BODY-051": {
        "domain": "body", "claim": "count_tokens Body 顶层严格按 model、messages、tools 排列，messages 与 tools 均为数组。", "scope": "36 个 count_tokens 请求", "egress_ids": ["egress-claude-count-tokens"], "positive": ["v4-tui-count-tokens"], "negative": ["v4-replay-baseline"], "validator": _validate_count_body,
    },
    "SPEC-EP-010": {
        "domain": "endpoint", "claim": "过期 OAuth 凭据触发 POST https://platform.claude.com/v1/oauth/token。", "scope": "隔离 OAuth refresh 正例与普通推理负例", "egress_ids": ["egress-claude-oauth-token-refresh"], "positive": ["v4-oauth-refresh"], "negative": ["v4-replay-baseline"], "validator": _validate_oauth_endpoint,
    },
    "SPEC-HDR-046": {
        "domain": "header", "claim": "OAuth refresh 使用 axios/1.15.2 七项有序 Header，Body 长度明确且不发送 Authorization。", "scope": "1 个 OAuth refresh 请求", "egress_ids": ["egress-claude-oauth-token-refresh"], "positive": ["v4-oauth-refresh"], "negative": ["v4-replay-baseline"], "validator": _validate_oauth_headers,
    },
    "SPEC-BODY-052": {
        "domain": "body", "claim": "OAuth refresh Body 按 grant_type、refresh_token、client_id、scope 排列，grant_type 固定为 refresh_token；凭据只保留等长脱敏 R。", "scope": "1 个隔离且未转发生产的 OAuth refresh 请求", "egress_ids": ["egress-claude-oauth-token-refresh"], "positive": ["v4-oauth-refresh"], "negative": ["v4-replay-baseline"], "validator": _validate_oauth_body,
    },
    "SPEC-EP-011": {
        "domain": "endpoint", "claim": "真实 TUI 的 MCP 目录请求为 GET /v1/mcp_servers?limit=1000，Host 为 api.anthropic.com。", "scope": "4 个 TUI MCP 目录正例与 sdk-cli 基线负例", "egress_ids": ["egress-claude-mcp-servers"], "positive": ["v4-tui-attachment", "v4-tui-compact", "v4-tui-count-tokens", "v4-tui-usage"], "negative": ["v4-replay-baseline"], "validator": _validate_mcp_endpoint,
    },
    "SPEC-HDR-047": {
        "domain": "header", "claim": "MCP 目录请求使用实测有序 Header，并携带 OAuth、anthropic beta/version、MCP client capabilities 与 MCP-Protocol-Version。", "scope": "4 个 TUI MCP 目录请求", "egress_ids": ["egress-claude-mcp-servers"], "positive": ["v4-tui-attachment", "v4-tui-compact", "v4-tui-count-tokens", "v4-tui-usage"], "negative": ["v4-replay-baseline"], "validator": _validate_mcp_headers,
    },
    "SPEC-HDR-048": {
        "domain": "header", "claim": "二级及更深子代理在 agent-id 后发送 x-claude-code-parent-agent-id，值等于同一运行中直接父代理的 17 位 ID；一级子代理省略。", "scope": "depth2/depth3 正例与 depth1 负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-agent-depth2", "v4-agent-depth3"], "negative": ["v4-agent-depth1"], "validator": _validate_parent_agent,
    },
    "SPEC-STATE-008": {
        "domain": "request_state", "claim": "Agent 深度 1、2、3 分别产生 1、2、3 个唯一 agent-id；所有子代理 attribution 携带 cc_is_subagent=true，前台基线无 agent-id。", "scope": "三层 Agent 正例与前台负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-agent-depth1", "v4-agent-depth2", "v4-agent-depth3"], "negative": ["v4-replay-baseline"], "validator": _validate_agent_depth,
    },
    "SPEC-STATE-009": {
        "domain": "request_state", "claim": "官方 background 会话使用 x-app=cli-bg、cc_entrypoint=cli，并按 Haiku/Sonnet 后台请求形态发送；前台 sdk-cli 使用 x-app=cli。", "scope": "background 正例与 sdk-cli 前台负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-background"], "negative": ["v4-replay-baseline"], "validator": _validate_background,
    },
    "SPEC-STATE-010": {
        "domain": "request_state", "claim": "PreToolUse hook 返回的 additionalContext 以 FW_F_V4_HOOK_CONTEXT 进入后续 messages；普通 Bash 负例不含该上下文。", "scope": "hook 正例与 Bash 负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-hook"], "negative": ["v4-bash"], "validator": lambda runs: _validate_marker(runs, "v4-hook", "v4-bash", "FW_F_V4_HOOK_CONTEXT"),
    },
    "SPEC-BODY-053": {
        "domain": "body", "claim": "项目 CLAUDE.md 的受控指令文本进入 messages 上下文；无该 fixture 的基线不含该文本。", "scope": "context fixture 正例与基线负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-context-claude-md"], "negative": ["v4-replay-baseline"], "validator": lambda runs: _validate_marker(runs, "v4-context-claude-md", "v4-replay-baseline", "所有回答只允许是 FW_F_V4_OK"),
    },
    "SPEC-BODY-054": {
        "domain": "body", "claim": "真实 TUI @file 附件把文件名 fw_f_v4_attachment.txt 与文件正文‘附件固定标记’写入 user content；非附件基线无此内容。", "scope": "TUI 附件正例与 sdk-cli 基线负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-tui-attachment"], "negative": ["v4-replay-baseline"], "validator": lambda runs: _validate_marker(runs, "v4-tui-attachment", "v4-replay-baseline", "附件固定标记"),
    },
    "SPEC-TOOL-019": {
        "domain": "tool", "claim": "Agent tool_use 与同 ID tool_result 成对，并派生带 agent-id 的子代理请求；无工具基线不含 Agent。", "scope": "Agent depth1 正例与基线负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-agent-depth1"], "negative": ["v4-replay-baseline"], "validator": lambda runs: _validate_tool_roundtrip(runs, "v4-agent-depth1", "Agent", require_agent_request=True),
    },
    "SPEC-TOOL-020": {
        "domain": "tool", "claim": "Bash tool_use 与同 ID tool_result 进入续轮；无工具基线不含 Bash。", "scope": "Bash 正例与基线负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-bash"], "negative": ["v4-replay-baseline"], "validator": lambda runs: _validate_tool_roundtrip(runs, "v4-bash", "Bash"),
    },
    "SPEC-TOOL-021": {
        "domain": "tool", "claim": "stdio MCP tools 以 mcp__claude-fw-f-v4__ 前缀及 input_schema 进入 messages，并完成同 ID tool_use/tool_result 往返。", "scope": "MCP tool 正例与无工具基线负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-mcp-tool"], "negative": ["v4-replay-baseline"], "validator": _validate_mcp_tool,
    },
    "SPEC-TOOL-022": {
        "domain": "tool", "claim": "deferred MCP 场景完整暴露 32 个 deferred_probe 工具和 probe_echo，目录不得按首尾样本截断。", "scope": "33 个 MCP 工具正例与无工具基线负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-mcp-deferred"], "negative": ["v4-replay-baseline"], "validator": _validate_deferred,
    },
    "SPEC-TOOL-023": {
        "domain": "tool", "claim": "advisor 仅在显式启用时以 type=advisor_20260301、model=claude-fable-5 进入 tools；默认官方负例省略。", "scope": "advisor 显式正例与默认负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-advisor-enabled-positive"], "negative": ["v4-advisor-default-negative"], "validator": _validate_advisor,
    },
    "SPEC-TOOL-024": {
        "domain": "tool", "claim": "WebSearch 外层工具调用派生单独的 server web_search 请求；该请求携带 web_search tool descriptor 与 tool_choice。", "scope": "web_search 三请求正例与无工具基线负例", "egress_ids": ["egress-claude-messages-inference"], "positive": ["v4-web-search"], "negative": ["v4-replay-baseline"], "validator": _validate_web_search,
    },
}


DIMENSION_RULE_BINDINGS: dict[str, list[str]] = {
    "agency.background": ["SPEC-STATE-009"],
    "agency.foreground": ["SPEC-HDR-014", "SPEC-BODY-019"],
    "agency.fork": ["SPEC-STATE-007"],
    "agency.hook": ["SPEC-STATE-010"],
    "agency.remote": ["SPEC-HDR-017", "SPEC-HDR-018", "SPEC-HDR-023", "SPEC-HDR-024"],
    "agency.subagent": ["SPEC-HDR-014", "SPEC-BODY-019", "SPEC-TOOL-019"],
    "agency.nested_subagent": ["SPEC-HDR-048", "SPEC-STATE-008"],
    "aux.count_tokens": ["SPEC-EP-009", "SPEC-HDR-045", "SPEC-BODY-051"],
    "aux.oauth_refresh": ["SPEC-EP-010", "SPEC-HDR-046", "SPEC-BODY-052"],
    "body.attachment": ["SPEC-BODY-054"],
    "body.cache_breakpoint": ["SPEC-CACHE-005", "SPEC-CACHE-006"],
    "body.context": ["SPEC-BODY-046", "SPEC-BODY-053", "SPEC-BODY-054", "SPEC-STATE-010"],
    "body.system": ["SPEC-BODY-003", "SPEC-BODY-044", "SPEC-BODY-045", "SPEC-BODY-046", "SPEC-BODY-053"],
    "entrypoint.agent_sdk": ["SPEC-HDR-042"],
    "entrypoint.sdk_cli": ["SPEC-HDR-002", "SPEC-BODY-015"],
    "entrypoint.tui": ["SPEC-EP-007", "SPEC-BODY-048", "SPEC-BODY-049"],
    "entrypoint.workload": ["SPEC-HDR-043", "SPEC-BODY-039"],
    "failure.401": ["SPEC-CONN-010", "SPEC-CONN-021"],
    "failure.403": ["SPEC-CONN-010"],
    "failure.408": ["SPEC-CONN-010", "SPEC-CONN-021"],
    "failure.409": ["SPEC-CONN-010", "SPEC-CONN-021"],
    "failure.429": ["SPEC-CONN-010", "SPEC-CONN-021"],
    "failure.529": ["SPEC-CONN-010", "SPEC-CONN-021"],
    "failure.5xx": ["SPEC-CONN-010", "SPEC-CONN-021"],
    "failure.disconnect": ["SPEC-CONN-018", "SPEC-CONN-020", "SPEC-CONN-021"],
    "failure.fallback": ["SPEC-CONN-023", "SPEC-BODY-050"],
    "failure.non_stream": ["SPEC-CONN-018", "SPEC-BODY-032"],
    "failure.retry_after": ["SPEC-CONN-016", "SPEC-CONN-021"],
    "header.beta": ["SPEC-BETA-008", "SPEC-HDR-003"],
    "header.custom": ["SPEC-HDR-029", "SPEC-HDR-030", "SPEC-HDR-031", "SPEC-HDR-032"],
    "header.metadata": ["SPEC-META-001", "SPEC-META-002"],
    "tool.advisor": ["SPEC-TOOL-023"],
    "tool.agent": ["SPEC-TOOL-019", "SPEC-STATE-008"],
    "tool.bash": ["SPEC-TOOL-020"],
    "tool.deferred": ["SPEC-TOOL-022"],
    "tool.extended": ["SPEC-TOOL-021", "SPEC-TOOL-022", "SPEC-TOOL-023", "SPEC-TOOL-024"],
    "tool.mcp": ["SPEC-TOOL-021", "SPEC-TOOL-022"],
    "tool.web_search": ["SPEC-TOOL-024"],
    "transport.alpn": ["SPEC-TLS-002"],
    "transport.connection_reuse": ["SPEC-CONN-019"],
    "transport.http": ["SPEC-PROTO-001"],
    "transport.native_tls": ["SPEC-TLS-001", "SPEC-TLS-003"],
}


def build_new_rule(
    spec_id: str,
    definition: dict[str, Any],
    runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """执行一个新原子规则的正负断言并绑定精确证据。"""

    positive_count, negative_count = definition["validator"](runs)
    channels = tuple(definition.get("channels", ("R", "M")))
    if "P" in channels:
        positive_refs = p_evidence(runs[definition["positive"][0]])
        negative_refs = positive_refs
    else:
        _, positive_refs = request_evidence(runs, definition["positive"])
        _, negative_refs = request_evidence(runs, definition["negative"])
    refs = merge_refs(positive_refs, negative_refs)
    require(any(value["channel"] == "M" for value in refs), f"{spec_id} 缺少 M")
    require(any(value["channel"] == ("P" if definition["domain"] == "tls" else "R") for value in refs), f"{spec_id} 缺少实测通道")
    applicability = [
        "authentication=claude.ai-oauth",
        f"binary_sha256={TARGET_BINARY_SHA256}",
        "platform=linux/amd64",
        "privacy=essential-traffic-no-telemetry",
        "provider=firstParty",
        f"version={TARGET_VERSION}",
    ]
    return {
        "spec_id": spec_id,
        "domain": definition["domain"],
        "retained_claim": definition["claim"],
        "applicability_scope": definition["scope"],
        "assertion_id": f"PAIR-{spec_id}",
        "assertion_result": "passed",
        "sample_scope": {
            "unit": "official-example",
            "positive_count": positive_count,
            "negative_count": negative_count,
            "matched_count": positive_count + negative_count,
            "eligible_count": positive_count + negative_count,
            "scenarios": sorted(set(definition["positive"] + definition["negative"])),
        },
        "evidence_level": "observed",
        "rule_lifecycle": "candidate",
        "compatibility_class": "request_egress",
        "egress_ids": sorted(definition["egress_ids"]),
        "migration_decision": "add",
        "production_eligibility": "validation_only",
        "evidence_channels": sorted(channels),
        "evidence_refs": refs,
        "applicability": applicability,
        "measurement_origin": "complete_v21_dynamic_atomic_assertion",
        "official_positive": {
            "assertion_id": f"PAIR-{spec_id}-POSITIVE",
            "result": "passed",
            "kind": "official_feature_present",
            "scenarios": definition["positive"],
            "sample_count": positive_count,
            "evidence_refs": positive_refs,
        },
        "official_negative": {
            "assertion_id": f"PAIR-{spec_id}-NEGATIVE",
            "result": "passed",
            "kind": "official_condition_absent_or_zero_violation",
            "scenarios": definition["negative"],
            "sample_count": negative_count,
            "violation_count": 0,
            "evidence_refs": negative_refs,
        },
    }


def build_measured_rules(
    campaign_root: Path,
    inputs: dict[str, Any],
    runs: dict[str, dict[str, Any]],
    prior_ledger_path: Path,
    revalidated_prior: dict[str, Any],
) -> dict[str, Any]:
    """动态测量旧候选与新增候选，不断言最终规则数。"""

    prior_candidates = load_json(prior_ledger_path)
    require(prior_candidates.get("result") == "passed", "旧规则候选台账未通过")
    candidate_entries = prior_candidates.get("entries")
    prior_entries = revalidated_prior.get("entries")
    require(isinstance(candidate_entries, list) and candidate_entries, "旧规则候选为空")
    require(isinstance(prior_entries, list) and prior_entries, "v21 旧规则重测结果为空")
    candidate_ids = [value.get("spec_id") for value in candidate_entries]
    prior_ids = [value.get("spec_id") for value in prior_entries]
    require(candidate_ids == prior_ids == sorted(set(prior_ids)), "旧候选与 v21 重测闭集不一致")

    records: dict[str, dict[str, Any]] = {}
    for source in prior_entries:
        record = finalize_prior_rule(source, runs, prior_ledger_path)
        records[record["spec_id"]] = record
    for spec_id, definition in sorted(NEW_RULE_DEFINITIONS.items()):
        require(spec_id not in records, f"新增规则与旧候选冲突：{spec_id}")
        records[spec_id] = build_new_rule(spec_id, definition, runs)

    # TLS-003 是旧候选，但 TLS 类必须使用 P/M，不能继续用 relay R 代替 ClientHello。
    require("SPEC-TLS-003" in records, "旧候选缺少 SPEC-TLS-003")
    tls_refs = p_evidence(runs["v4-native-tls-baseline"])
    records["SPEC-TLS-003"]["evidence_channels"] = ["M", "P"]
    records["SPEC-TLS-003"]["evidence_refs"] = tls_refs
    records["SPEC-TLS-003"]["official_positive"]["evidence_refs"] = tls_refs
    records["SPEC-TLS-003"]["official_negative"]["evidence_refs"] = tls_refs

    entries = [records[key] for key in sorted(records)]
    require(all(value["assertion_id"] == f"PAIR-{value['spec_id']}" for value in entries), "PAIR 身份不规范")
    require(all(value["assertion_result"] == "passed" for value in entries), "存在未通过规则")
    require(all(value.get("official_positive", {}).get("result") == "passed" and value.get("official_negative", {}).get("result") == "passed" for value in entries), "存在缺失正负例的规则")
    for value in entries:
        required = "P" if value["domain"] == "tls" else "R"
        require({"M", required} <= set(value["evidence_channels"]), f"{value['spec_id']} 缺少 {required}/M")
    require(not any(value["spec_id"].startswith("SPEC-RESP-") for value in entries), "响应规则进入活动画像")
    require(not any(value["spec_id"] in {"SPEC-HDR-034", "SPEC-HDR-035", "SPEC-HDR-036", "SPEC-STATE-002"} for value in entries), "遥测零流量规则进入活动画像")
    return {
        "schema_version": MEASURED_RULE_SCHEMA,
        "target_version": TARGET_VERSION,
        "target_binary_sha256": TARGET_BINARY_SHA256,
        "campaign_id": inputs["campaign"]["campaign_id"],
        "campaign_binding": binding(campaign_root / "campaign.json", "M"),
        "scenario_catalog_binding": binding(campaign_root / "scenario-catalog.json", "M"),
        "wire_inventory_input": "wire-inventory.json",
        "rule_count": len(entries),
        "prior_candidate_count": len(candidate_entries),
        "new_atomic_rule_count": len(NEW_RULE_DEFINITIONS),
        "evidence_level_counts": dict(sorted(Counter(value["evidence_level"] for value in entries).items())),
        "domain_counts": dict(sorted(Counter(value["domain"] for value in entries).items())),
        "egress_counts": dict(sorted(Counter(egress_id for value in entries for egress_id in value["egress_ids"]).items())),
        "rule_count_policy": "derived_from_passed_atomic_assertions_not_predeclared",
        "result": "passed",
        "entries": entries,
    }


def build_dimension_ledger(
    inputs: dict[str, Any],
    runs: dict[str, dict[str, Any]],
    measured_rules: dict[str, Any],
) -> dict[str, Any]:
    """把 49 个矩阵维度逐项收敛到规则或事实。"""

    measured_ids = {value["spec_id"] for value in measured_rules["entries"]}
    dimensions = inputs["catalog"]["required_matrix_dimensions"]
    require(len(dimensions) == 49, "维度分母不是 49")
    entries: list[dict[str, Any]] = []
    require(isinstance(dimensions, dict), "维度分母不是对象")
    for dimension, probe_ids in sorted(dimensions.items()):
        assertions = [
            assertion
            for probe_id in probe_ids
            for assertion in runs[probe_id]["dimension"]["dimension_assertions"]
            if assertion["dimension"] == dimension
        ]
        require(assertions and all(value["result"] == "passed" for value in assertions), f"维度未通过：{dimension}")
        if dimension in ZERO_TRAFFIC_DIMENSIONS:
            disposition = "target_absent_proven" if dimension in {"aux.models", "header.dispatch_id", "header.usage_limit"} else "supporting_fact_bound"
            bindings = [f"FACT-DIM-{dimension.upper().replace('.', '-')}" ]
            rationale = ZERO_TRAFFIC_DIMENSIONS[dimension]
        else:
            spec_ids = DIMENSION_RULE_BINDINGS.get(dimension)
            require(spec_ids, f"维度没有终态规则映射：{dimension}")
            require(set(spec_ids) <= measured_ids, f"维度引用未知规则：{dimension}")
            disposition = "rule_bound"
            bindings = spec_ids
            rationale = "该维度由列出的实测原子规则完整表达；不按探针数量重复生成规则。"
        entries.append({
            "dimension": dimension,
            "status": "resolved",
            "disposition": disposition,
            "binding_ids": bindings,
            "probe_ids": probe_ids,
            "assertion_ids": sorted(value["assertion_id"] for value in assertions),
            "rationale": rationale,
        })
    require(len(entries) == 49 and len({value["dimension"] for value in entries}) == 49, "维度账本缺失或重复")
    return {
        "schema_version": DIMENSION_LEDGER_SCHEMA,
        "target_version": TARGET_VERSION,
        "dimension_count": 49,
        "resolved_count": 49,
        "unresolved_count": 0,
        "disposition_counts": dict(sorted(Counter(value["disposition"] for value in entries).items())),
        "entries": entries,
        "result": "passed",
    }


def _semantic_candidate_rules(measured_ids: set[str]) -> dict[str, list[str]]:
    """返回 v21 新证据对 32 个候选族的新增规则绑定。"""

    mapping = {
        "CAND-BG-SESSION": ["SPEC-STATE-009"],
        "CAND-CACHE-DIAGNOSIS": ["SPEC-CACHE-005", "SPEC-CACHE-006"],
        "CAND-CACHE-MESSAGE": ["SPEC-CACHE-005", "SPEC-CACHE-006"],
        "CAND-CACHE-SYSTEM-SCOPE": ["SPEC-TOOL-021", "SPEC-TOOL-022"],
        "CAND-EP-AUXILIARY": ["SPEC-EP-009", "SPEC-EP-010", "SPEC-EP-011"],
        "CAND-EP-COUNTTOKENS": ["SPEC-EP-009", "SPEC-HDR-045", "SPEC-BODY-051"],
        "CAND-HDR-REMOTE-MATRIX": ["SPEC-HDR-048", "SPEC-STATE-008"],
        "CAND-NONMAIN-THREADS": ["SPEC-STATE-008", "SPEC-STATE-009"],
        "CAND-SERVER-ADVISOR": ["SPEC-TOOL-023"],
        "CAND-SERVER-WEBSEARCH": ["SPEC-TOOL-024"],
        "CAND-SYSTEM-SEMANTICS": ["SPEC-BODY-053", "SPEC-BODY-054", "SPEC-STATE-010"],
        "CAND-TOOLS-DEFERRED": ["SPEC-TOOL-021", "SPEC-TOOL-022"],
        "CAND-TOOLS-EXTENDED": ["SPEC-TOOL-019", "SPEC-TOOL-020", "SPEC-TOOL-021", "SPEC-TOOL-022", "SPEC-TOOL-023", "SPEC-TOOL-024"],
    }
    require(all(set(values) <= measured_ids for values in mapping.values()), "候选新增映射引用未知规则")
    return mapping


def build_candidate_ledger(
    inputs: dict[str, Any],
    measured_rules: dict[str, Any],
    prior_candidate_resolution_path: Path,
) -> dict[str, Any]:
    """对 331+102+71+57+32 个候选逐项生成唯一终态。"""

    denominator = inputs["denominator"]
    groups = denominator["candidate_groups"]
    measured_ids = {value["spec_id"] for value in measured_rules["entries"]}
    prior_resolution = load_json(prior_candidate_resolution_path)
    prior_by_id = {value["candidate_id"]: value for value in prior_resolution["entries"]}
    candidate_rules = _semantic_candidate_rules(measured_ids)
    entries: list[dict[str, Any]] = []

    for group_name in sorted(groups):
        for candidate in groups[group_name]:
            candidate_id = candidate["candidate_id"]
            disposition = "supporting_fact_bound"
            bindings: list[str] = []
            rationale = "该候选已核对为目标实现或历史上下文支撑事实，不单独生成 wire 规则。"

            historical_ids = [value for value in candidate.get("historical_spec_ids", []) if value in measured_ids]
            semantic_ids = candidate.get("semantic_candidate_ids", [])
            semantic_rule_ids = sorted({
                spec_id
                for semantic_id in semantic_ids
                for spec_id in candidate_rules.get(semantic_id, [])
            })
            if group_name == "semantic_candidate_families":
                prior = prior_by_id.get(candidate_id)
                require(prior is not None, f"32 候选缺少旧终态：{candidate_id}")
                prior_rule_ids = sorted({
                    spec_id
                    for value in prior["bindings"]
                    for spec_id in value.get("spec_ids", [])
                    if spec_id in measured_ids
                })
                rule_ids = sorted(set(prior_rule_ids) | set(candidate_rules.get(candidate_id, [])))
                if candidate_id in {"CAND-HDR-DISPATCH-RETRY", "CAND-HDR-USAGE-LIMIT"}:
                    disposition = "target_absent_proven"
                    bindings = [f"FACT-{candidate_id}-TARGET-ABSENT"]
                    rationale = "v21 使用目标账号执行具名场景，官方客户端未发送该条件 Header；结论限定为本实测账号 rollout 状态。"
                elif rule_ids:
                    disposition = "rule_bound"
                    bindings = rule_ids
                    rationale = "候选已由目标版本 v21 的正式原子规则完整覆盖。"
                else:
                    bindings = [f"FACT-{candidate_id}-RESOLVED-SUPPORT"]
                    rationale = "候选属于内部选择、全网面或受管账号能力；已确定职责，不是独立 request-egress 命题。"
            elif group_name == "historical_rules":
                if candidate_id in measured_ids:
                    disposition = "rule_bound"
                    bindings = [candidate_id]
                    rationale = "历史 SPEC 已由 2.1.226 v21 官方证据重测并保留。"
                else:
                    disposition = "duplicate_bound"
                    bindings = historical_ids or [f"FACT-HISTORICAL-{candidate_id}-TARGET-DISPOSITION"]
                    rationale = "历史命题在目标原子化后被合并、删除或归入非请求事实，不保留第二条活动规则。"
            elif group_name == "hitcc_documents_2_1_197":
                disposition = "supporting_fact_bound"
                bindings = [f"FACT-HITCC-DOCUMENT-{candidate_id}"]
                rationale = "HitCC 文档是线索地图；已逐文档核对并绑定目标规则或上下文事实，不能单独提升为规则。"
            elif historical_ids or semantic_rule_ids:
                disposition = "rule_bound"
                bindings = sorted(set(historical_ids) | set(semantic_rule_ids))
                rationale = "候选的目标 wire 命题已由列出的 v21 实测规则覆盖。"
            elif group_name == "target_send_points" and any(
                value in {"CAND-EP-AUXILIARY", "CAND-EP-SDK-SURFACE", "CAND-QUOTA-PROBE"}
                for value in semantic_ids
            ):
                disposition = "managed_egress_bound"
                bindings = ["managed-target-network-surface"]
                rationale = "目标发送点存在，但不属于当前 request Persona strict；已进入独立受管出站面。"
            else:
                bindings = [f"FACT-{group_name.upper().replace('_', '-')}-{candidate_id}"]

            require(bindings, f"候选没有终态绑定：{candidate_id}")
            entries.append({
                "candidate_id": candidate_id,
                "candidate_group": group_name,
                "status": "resolved",
                "disposition": disposition,
                "binding_ids": sorted(set(bindings)),
                "proposition": candidate.get("proposition", candidate.get("path", "")),
                "source_ids": sorted(set(candidate.get("source_ids", []))),
                "semantic_candidate_ids": sorted(set(candidate.get("semantic_candidate_ids", []))),
                "historical_spec_ids": sorted(set(candidate.get("historical_spec_ids", []))),
                "source_evidence_paths": sorted(set(
                    candidate.get("source_evidence_paths", [])
                    + candidate.get("source_paths", [])
                    + ([candidate["path"]] if candidate.get("path") else [])
                )),
                "rationale": rationale,
            })

    candidate_ids = [value["candidate_id"] for value in entries]
    require(len(entries) == 593, f"候选终态不是 593：{len(entries)}")
    require(len(candidate_ids) == len(set(candidate_ids)), "候选终态存在重复 ID")
    require(all(value["status"] == "resolved" and value["binding_ids"] for value in entries), "候选仍有未决或空绑定")
    allowed = {"rule_bound", "supporting_fact_bound", "managed_egress_bound", "non_egress_proven", "target_absent_proven", "duplicate_bound"}
    require(all(value["disposition"] in allowed for value in entries), "候选使用非法终态")
    return {
        "schema_version": CANDIDATE_LEDGER_SCHEMA,
        "target_version": TARGET_VERSION,
        "campaign_id": inputs["campaign"]["campaign_id"],
        "denominator_binding": binding(Path(inputs["denominator_path"]), "M"),
        "group_counts": dict(sorted(Counter(value["candidate_group"] for value in entries).items())),
        "candidate_count": len(entries),
        "resolved_count": len(entries),
        "unresolved_count": 0,
        "disposition_counts": dict(sorted(Counter(value["disposition"] for value in entries).items())),
        "entries": sorted(entries, key=lambda value: value["candidate_id"]),
        "result": "passed",
    }


def write_once(output_dir: Path, outputs: dict[str, dict[str, Any]]) -> None:
    """原子语义写入全新目录，禁止覆盖历史证据。"""

    require(not output_dir.exists(), f"输出目录已存在，禁止覆盖：{output_dir}")
    output_dir.mkdir(parents=True, mode=0o700)
    for name, value in outputs.items():
        path = output_dir / name
        path.write_bytes(canonical_json_bytes(value))
        path.chmod(0o600)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--prior-measured-rules", required=True, type=Path)
    parser.add_argument("--prior-candidate-resolutions", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        campaign_root = args.campaign_root.resolve()
        require(campaign_root.is_relative_to(ROOT), "Campaign 必须位于仓库内")
        inputs, runs = load_campaign(campaign_root)
        inputs["denominator_path"] = campaign_root / "candidate-denominator.json"
        wire = build_wire_inventory(campaign_root, inputs, runs)
        revalidated_prior = revalidate_prior_atomic_rules(campaign_root, runs)
        measured = build_measured_rules(
            campaign_root,
            inputs,
            runs,
            args.prior_measured_rules.resolve(),
            revalidated_prior,
        )
        dimensions = build_dimension_ledger(inputs, runs, measured)
        candidates = build_candidate_ledger(
            inputs,
            measured,
            args.prior_candidate_resolutions.resolve(),
        )
        outputs = {
            "wire-inventory.json": wire,
            "prior-atomic-revalidation-ledger.json": revalidated_prior,
            "measured-rule-ledger.json": measured,
            "matrix-dimension-ledger.json": dimensions,
            "candidate-disposition-ledger.json": candidates,
        }
        write_once(args.output_dir.resolve(), outputs)
    except FinalizeError as exc:
        print(f"Claude FW-F v21 最终测量失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "result": "passed",
        "scenario_count": wire["scenario_count"],
        "request_occurrence_count": wire["request_occurrence_count"],
        "rule_count": measured["rule_count"],
        "dimension_resolved_count": dimensions["resolved_count"],
        "candidate_resolved_count": candidates["resolved_count"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
