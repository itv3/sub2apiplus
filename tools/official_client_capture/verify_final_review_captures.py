#!/usr/bin/env python3
"""验收最终复核的非 Lite、第三方定型、system 重组与动态 beta 抓包。"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from tools.official_client_capture.capturelib.analysis import (
    compare_official_egress_contract,
    compare_official_egress_tls_contract,
    normalize_direct_pcap,
    normalize_mitm_directory,
)
from tools.official_client_capture.capturelib.security import secure_write_json


def load_json(path: Path) -> dict[str, Any]:
    """读取单个 JSON 对象。"""

    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(directory: Path) -> list[dict[str, Any]]:
    """读取目录内全部 JSONL，不返回日志或认证值摘要。"""

    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
    return records


def header_present(record: dict[str, Any], section: str, name: str) -> bool:
    """检查原始抓包记录是否包含指定 Header。"""

    value = record.get(section)
    value = value if isinstance(value, dict) else {}
    headers = value.get("headers")
    expected = name.lower()
    if isinstance(headers, dict):
        return any(str(key).lower() == expected for key in headers)
    if isinstance(headers, list):
        return any(
            isinstance(item, list)
            and len(item) == 2
            and str(item[0]).lower() == expected
            for item in headers
        )
    return False


def header_value(record: dict[str, Any], section: str, name: str) -> str:
    """返回指定 Header；仅用于保存非认证画像字段的验证结果。"""

    value = record.get(section)
    value = value if isinstance(value, dict) else {}
    headers = value.get("headers")
    expected = name.lower()
    if isinstance(headers, dict):
        for key, item in headers.items():
            if str(key).lower() == expected:
                return str(item)
    if isinstance(headers, list):
        for item in headers:
            if (
                isinstance(item, list)
                and len(item) == 2
                and str(item[0]).lower() == expected
            ):
                return str(item[1])
    return ""


def http_requests(directory: Path, path_prefix: str) -> list[dict[str, Any]]:
    """筛选带 JSON body 的目标 POST 请求。"""

    selected: list[dict[str, Any]] = []
    for record in load_jsonl(directory):
        request = record.get("request")
        request = request if isinstance(request, dict) else {}
        body = request.get("body")
        body = body if isinstance(body, dict) else {}
        if (
            request.get("method") == "POST"
            and str(request.get("path", "")).startswith(path_prefix)
            and isinstance(body.get("json"), dict)
        ):
            selected.append(record)
    return selected


def websocket_requests(directory: Path) -> list[dict[str, Any]]:
    """筛选客户端发送的 response.create 帧。"""

    return [
        record
        for record in load_jsonl(directory)
        if record.get("from_client") is True
        and isinstance(record.get("json"), dict)
        and record["json"].get("type") == "response.create"
    ]


def request_payload(record: dict[str, Any]) -> dict[str, Any]:
    """读取 HTTP 抓包记录的 JSON 请求体。"""

    return record["request"]["body"]["json"]


def repeat_baseline(payload: dict[str, Any], count: int) -> dict[str, Any]:
    """按候选请求数重复同一官方固定画像观测。"""

    value = deepcopy(payload)
    records = value.get("records")
    records = records if isinstance(records, list) else []
    business_records = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("kind") == "http_exchange"
        and str(record.get("request", {}).get("path", "")).startswith(
            "/backend-api/codex/responses"
        )
    ]
    if len(business_records) != 1:
        raise ValueError("重复基准必须且只能包含一条官方业务请求。")
    value["records"] = [deepcopy(business_records[0]) for _ in range(count)]
    return value


def without_ws_prewarm(payload: dict[str, Any]) -> dict[str, Any]:
    """第三方客户端不产生 Codex 预热帧，业务画像比较只保留 generate 非 false 帧。"""

    value = deepcopy(payload)
    records = value.get("records")
    records = records if isinstance(records, list) else []
    value["records"] = [
        record
        for record in records
        if not (
            isinstance(record, dict)
            and record.get("kind") == "websocket_frame"
            and record.get("from_client") is True
            and isinstance(record.get("json_shape"), dict)
            and record["json_shape"].get("generate") is False
        )
    ]
    return value


def profile_assertions(
    ingress_payloads: list[dict[str, Any]],
    egress_payloads: list[dict[str, Any]],
) -> dict[str, bool]:
    """验证第三方非官方字段被统一成真实非 Lite 官方画像。"""

    pairs = list(zip(ingress_payloads, egress_payloads))
    return {
        "record_count_matches": bool(pairs)
        and len(ingress_payloads) == len(egress_payloads),
        "ingress_exercised_tool_choice_required": all(
            ingress.get("tool_choice") == "required" for ingress, _ in pairs
        ),
        "ingress_exercised_max_output_tokens": all(
            ingress.get("max_output_tokens") == 123 for ingress, _ in pairs
        ),
        "ingress_exercised_nonofficial_fixed_fields": all(
            ingress.get("store") is True
            and ingress.get("stream") is False
            and ingress.get("include") == ["message.output_text.logprobs"]
            and isinstance(ingress.get("reasoning"), dict)
            and ingress["reasoning"].get("context") == "none"
            for ingress, _ in pairs
        ),
        "egress_tool_choice_auto": all(
            egress.get("tool_choice") == "auto" for _, egress in pairs
        ),
        "egress_max_output_tokens_removed": all(
            "max_output_tokens" not in egress for _, egress in pairs
        ),
        "egress_store_false_stream_true": all(
            egress.get("store") is False and egress.get("stream") is True
            for _, egress in pairs
        ),
        "egress_include_official": all(
            egress.get("include") == ["reasoning.encrypted_content"]
            for _, egress in pairs
        ),
        "egress_reasoning_context_removed": all(
            not isinstance(egress.get("reasoning"), dict)
            or "context" not in egress["reasoning"]
            for _, egress in pairs
        ),
        "egress_parallel_from_capability": all(
            egress.get("parallel_tool_calls") is True for _, egress in pairs
        ),
        "instructions_preserved_top_level": all(
            ingress.get("instructions") == egress.get("instructions")
            and "instructions" in egress
            for ingress, egress in pairs
        ),
        "tools_preserved": all(
            ingress.get("tools") == egress.get("tools")
            for ingress, egress in pairs
        ),
        "business_reasoning_and_text_preserved": all(
            egress.get("reasoning", {}).get("effort")
            == ingress.get("reasoning", {}).get("effort")
            and egress.get("reasoning", {}).get("summary")
            == ingress.get("reasoning", {}).get("summary")
            and egress.get("text") == ingress.get("text")
            for ingress, egress in pairs
        ),
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-nonlite", type=Path, required=True)
    parser.add_argument("--candidate-nonlite-ws", type=Path, required=True)
    parser.add_argument("--candidate-nonlite-direct", type=Path, required=True)
    parser.add_argument("--third-party-http", type=Path, required=True)
    parser.add_argument("--third-party-ws", type=Path, required=True)
    parser.add_argument("--official-anthropic", type=Path, required=True)
    parser.add_argument("--third-party-anthropic", type=Path, required=True)
    parser.add_argument("--third-party-anthropic-direct", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """生成规范化证据、契约比较及逐字段断言。"""

    arguments = parse_arguments()
    arguments.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    arguments.output.chmod(0o700)

    normalized: dict[str, dict[str, Any]] = {}

    def normalize(name: str, directory: Path) -> dict[str, Any]:
        value = normalize_mitm_directory(
            directory, arguments.output / f"{name}.json"
        )
        normalized[name] = value
        return value

    official_http = load_json(
        arguments.official_nonlite / "analysis/mitm/codex-http/s1.json"
    )
    official_ws = load_json(
        arguments.official_nonlite / "analysis/mitm/codex-ws/s1.json"
    )
    official_anthropic = load_json(
        arguments.official_anthropic / "analysis/mitm/claude-http/s1.json"
    )

    nonlite_ws_candidate = normalize(
        "nonlite-ws-candidate", arguments.candidate_nonlite_ws / "mitm/codex-ws"
    )
    nonlite_ws_ingress = normalize(
        "nonlite-ws-ingress", arguments.candidate_nonlite_ws / "ingress/codex-ws"
    )
    third_http_candidate = normalize(
        "third-party-http-candidate",
        arguments.third_party_http / "mitm/third-party-openai-http",
    )
    third_http_ingress = normalize(
        "third-party-http-ingress",
        arguments.third_party_http / "ingress/third-party-openai-http",
    )
    third_ws_candidate = normalize(
        "third-party-ws-candidate",
        arguments.third_party_ws / "mitm/third-party-openai-ws",
    )
    third_ws_ingress = normalize(
        "third-party-ws-ingress",
        arguments.third_party_ws / "ingress/third-party-openai-ws",
    )
    third_anthropic_candidate = normalize(
        "third-party-anthropic-candidate",
        arguments.third_party_anthropic / "mitm/third-party-anthropic-http",
    )
    third_anthropic_ingress = normalize(
        "third-party-anthropic-ingress",
        arguments.third_party_anthropic / "ingress/third-party-anthropic-http",
    )

    third_http_count = len(
        http_requests(
            arguments.third_party_http / "mitm/third-party-openai-http",
            "/backend-api/codex/responses",
        )
    )
    comparisons = {
        "nonlite_ws": compare_official_egress_contract(
            official_ws,
            nonlite_ws_candidate,
            nonlite_ws_ingress,
            "oauth-codex-ws",
        ),
        "third_party_http": compare_official_egress_contract(
            repeat_baseline(official_http, third_http_count),
            third_http_candidate,
            third_http_ingress,
            "oauth-codex-http",
        ),
        "third_party_ws": compare_official_egress_contract(
            without_ws_prewarm(official_ws),
            third_ws_candidate,
            third_ws_ingress,
            "oauth-codex-ws",
        ),
        "third_party_anthropic": compare_official_egress_contract(
            official_anthropic,
            third_anthropic_candidate,
            third_anthropic_ingress,
            "oauth-claude-http",
        ),
    }
    for name, value in comparisons.items():
        secure_write_json(arguments.output / f"{name}-contract-diff.json", value)

    nonlite_ws_tls_candidate = normalize_direct_pcap(
        pcap_path=arguments.candidate_nonlite_direct
        / "direct/codex-ws-s1/egress.pcap",
        output_path=arguments.output / "nonlite-ws-tls-candidate.json",
        target_hosts=("chatgpt.com",),
        tshark_bin="/usr/bin/tshark",
    )
    anthropic_tls_candidate = normalize_direct_pcap(
        pcap_path=arguments.third_party_anthropic_direct
        / "direct/anthropic-http/egress.pcap",
        output_path=arguments.output / "anthropic-tls-candidate.json",
        target_hosts=("api.anthropic.com",),
        tshark_bin="/usr/bin/tshark",
    )
    tls_comparisons = {
        "nonlite_ws_tls": compare_official_egress_tls_contract(
            load_json(
                arguments.official_nonlite
                / "analysis/direct/codex-ws/s1.json"
            ),
            nonlite_ws_tls_candidate,
            "codex-ws",
        ),
        "anthropic_http_tls": compare_official_egress_tls_contract(
            load_json(
                arguments.official_anthropic
                / "analysis/direct/claude-http/s1.json"
            ),
            anthropic_tls_candidate,
            "anthropic-http",
        ),
    }
    for name, value in tls_comparisons.items():
        secure_write_json(arguments.output / f"{name}-diff.json", value)

    http_ingress_records = http_requests(
        arguments.third_party_http / "ingress/third-party-openai-http",
        "/v1/responses",
    )
    http_egress_records = http_requests(
        arguments.third_party_http / "mitm/third-party-openai-http",
        "/backend-api/codex/responses",
    )
    ws_ingress_records = websocket_requests(
        arguments.third_party_ws / "ingress/third-party-openai-ws"
    )
    ws_egress_records = websocket_requests(
        arguments.third_party_ws / "mitm/third-party-openai-ws"
    )
    http_profile = profile_assertions(
        [request_payload(item) for item in http_ingress_records],
        [request_payload(item) for item in http_egress_records],
    )
    ws_profile = profile_assertions(
        [item["json"] for item in ws_ingress_records],
        [item["json"] for item in ws_egress_records],
    )
    cookie_lifecycle = {
        "two_business_requests": len(http_egress_records) == 2,
        "first_request_without_cookie": bool(http_egress_records)
        and not header_present(http_egress_records[0], "request", "cookie"),
        "first_response_sets_cookie": bool(http_egress_records)
        and header_present(http_egress_records[0], "response", "set-cookie"),
        "second_request_replays_cookie": len(http_egress_records) >= 2
        and header_present(http_egress_records[1], "request", "cookie"),
    }

    anth_ingress_records = http_requests(
        arguments.third_party_anthropic / "ingress/third-party-anthropic-http",
        "/v1/messages",
    )
    anth_egress_records = http_requests(
        arguments.third_party_anthropic / "mitm/third-party-anthropic-http",
        "/v1/messages",
    )
    anth_ingress_payloads = [request_payload(item) for item in anth_ingress_records]
    anth_egress_payloads = [request_payload(item) for item in anth_egress_records]
    anthropic_assertions = {
        "single_ingress_and_egress": len(anth_ingress_payloads)
        == len(anth_egress_payloads)
        == 1,
        "three_system_blocks_exercised": bool(anth_ingress_payloads)
        and len(anth_ingress_payloads[0].get("system", [])) == 3,
        "custom_defer_loading_exercised": bool(anth_ingress_payloads)
        and anth_ingress_payloads[0].get("tools", [{}])[0]
        .get("custom", {})
        .get("defer_loading")
        is True,
        "advanced_tool_use_beta_emitted": bool(anth_egress_records)
        and "advanced-tool-use-2025-11-20"
        in header_value(anth_egress_records[0], "request", "anthropic-beta"),
        "system_semantic_preserved": comparisons["third_party_anthropic"].get(
            "candidate_semantic_preserved"
        )
        is True,
        "dynamic_beta_exercised": comparisons["third_party_anthropic"].get(
            "anthropic_dynamic_beta_exercised"
        )
        is True,
        "dynamic_beta_valid": comparisons["third_party_anthropic"].get(
            "anthropic_dynamic_beta_valid"
        )
        is True,
    }

    assertion_groups = {
        "http_nonlite_profile": http_profile,
        "ws_nonlite_profile": ws_profile,
        "cookie_lifecycle": cookie_lifecycle,
        "anthropic_system_and_beta": anthropic_assertions,
    }
    assertions_passed = all(
        value is True
        for group in assertion_groups.values()
        for value in group.values()
    )
    comparisons_passed = all(value.get("equal") for value in comparisons.values())
    tls_passed = all(value.get("equal") for value in tls_comparisons.values())
    summary = {
        "schema_version": "official-egress-final-review-verification/v1",
        "comparisons": {
            name: {
                "equal": value.get("equal"),
                "undeclared_differences": len(
                    value.get("undeclared_differences", [])
                ),
                "candidate_semantic_preserved": value.get(
                    "candidate_semantic_preserved"
                ),
                "anthropic_dynamic_beta_exercised": value.get(
                    "anthropic_dynamic_beta_exercised"
                ),
                "anthropic_dynamic_beta_valid": value.get(
                    "anthropic_dynamic_beta_valid"
                ),
                "ws_turn_state_lifecycle_valid": value.get(
                    "ws_turn_state_lifecycle_valid"
                ),
            }
            for name, value in comparisons.items()
        },
        "assertions": assertion_groups,
        "tls_comparisons": {
            name: {
                "equal": value.get("equal"),
                "baseline_business_observation_count": value.get(
                    "baseline_business_observation_count"
                ),
                "candidate_business_observation_count": value.get(
                    "candidate_business_observation_count"
                ),
            }
            for name, value in tls_comparisons.items()
        },
        "assertions_passed": assertions_passed,
        "comparisons_passed": comparisons_passed,
        "tls_passed": tls_passed,
        "all_passed": assertions_passed and comparisons_passed and tls_passed,
        "anthropic_upstream_result": {
            "success": False,
            "classification": "外部账号组织被禁用；请求已到达上游且不影响出站画像证据判定。",
        },
        "third_party_ws_scope": (
            "仅比较第三方客户端实际产生的业务帧；官方 Codex 预热帧已由 nonlite_ws 完整序列门禁覆盖。"
        ),
    }
    secure_write_json(arguments.output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
