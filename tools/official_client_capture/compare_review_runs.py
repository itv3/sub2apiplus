#!/usr/bin/env python3
"""汇总本轮官方/候选运行，并生成应用层与 TLS 契约报告。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tools.official_client_capture.capturelib.analysis import (
    compare_official_egress_contract,
    compare_official_egress_tls_contract,
    normalize_direct_pcap,
    normalize_mitm_directory,
)
from tools.official_client_capture.capturelib.security import secure_write_json


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(output: Path, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    secure_write_json(output / name, payload)
    return payload


def merge_normalized(
    output: Path, paths: list[Path], name: str
) -> dict[str, Any]:
    lifecycle = {
        "response_state_count": 0,
        "matched_client_frame_count": 0,
        "unmatched_client_frame_count": 0,
    }
    payload: dict[str, Any] = {
        "schema_version": "official-client-capture-normalized/v1",
        "source_files": [],
        "record_count": 0,
        "records": [],
        "turn_state_lifecycle": lifecycle,
    }
    for path in paths:
        current = load(path)
        payload["source_files"].extend(current.get("source_files", []))
        payload["records"].extend(current.get("records", []))
        current_lifecycle = current.get("turn_state_lifecycle", {})
        for key in lifecycle:
            lifecycle[key] += int(current_lifecycle.get(key, 0) or 0)
    payload["record_count"] = len(payload["records"])
    return save(output, name, payload)


def merge_tls(output: Path, paths: list[Path], name: str) -> dict[str, Any]:
    hellos: list[dict[str, Any]] = []
    for path in paths:
        hellos.extend(load(path).get("client_hellos", []))
    return save(
        output,
        name,
        {
            "schema_version": "official-client-capture-tls/v1",
            "target_hosts": ["chatgpt.com"],
            "client_hello_count": len(hellos),
            "client_hellos": hellos,
        },
    )


def normalize_mitm(output: Path, source: Path, name: str) -> dict[str, Any]:
    return normalize_mitm_directory(source, output / name)


def normalize_pcap(output: Path, source: Path, name: str) -> dict[str, Any]:
    return normalize_direct_pcap(
        pcap_path=source,
        output_path=output / name,
        target_hosts=("chatgpt.com",),
        tshark_bin="/usr/bin/tshark",
    )


def add_contract(
    output: Path,
    results: dict[str, dict[str, Any]],
    name: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    ingress: dict[str, Any],
    contract: str,
) -> None:
    result = compare_official_egress_contract(
        baseline, candidate, ingress, contract
    )
    results[name] = result
    save(output, f"{name}-contract-diff.json", result)


def add_tls_contract(
    output: Path,
    results: dict[str, dict[str, Any]],
    name: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    contract: str,
) -> None:
    result = compare_official_egress_tls_contract(baseline, candidate, contract)
    results[name] = result
    save(output, f"{name}-diff.json", result)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-standard", type=Path, required=True)
    parser.add_argument("--candidate-http", type=Path, required=True)
    parser.add_argument("--candidate-ws", type=Path, required=True)
    parser.add_argument("--candidate-direct", type=Path, required=True)
    parser.add_argument("--official-nonlite", type=Path, required=True)
    parser.add_argument("--candidate-nonlite", type=Path, required=True)
    parser.add_argument("--candidate-nonlite-direct", type=Path, required=True)
    parser.add_argument("--official-compaction", type=Path, required=True)
    parser.add_argument("--candidate-compaction", type=Path, required=True)
    parser.add_argument("--candidate-compaction-direct", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    arguments.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    arguments.output.chmod(0o700)
    results: dict[str, dict[str, Any]] = {}

    for subject, contract, candidate_root in (
        ("codex-http", "oauth-codex-http", arguments.candidate_http),
        ("codex-ws", "oauth-codex-ws", arguments.candidate_ws),
    ):
        scenarios = ("s1", "s2", "s4")
        baseline = merge_normalized(
            arguments.output,
            [
                arguments.official_standard
                / f"analysis/mitm/{subject}/{scenario}.json"
                for scenario in scenarios
            ],
            f"{subject}-baseline.json",
        )
        candidate = normalize_mitm(
            arguments.output,
            candidate_root / f"mitm/{subject}",
            f"{subject}-candidate.json",
        )
        ingress = normalize_mitm(
            arguments.output,
            candidate_root / f"ingress/{subject}",
            f"{subject}-ingress.json",
        )
        add_contract(
            arguments.output,
            results,
            subject,
            baseline,
            candidate,
            ingress,
            contract,
        )

        baseline_tls = merge_tls(
            arguments.output,
            [
                arguments.official_standard
                / f"analysis/direct/{subject}/{scenario}.json"
                for scenario in scenarios
            ],
            f"{subject}-tls-baseline.json",
        )
        normalized_candidates = [
            normalize_pcap(
                arguments.output,
                arguments.candidate_direct
                / f"direct/{subject}-{scenario}/egress.pcap",
                f"{subject}-tls-candidate-{scenario}.json",
            )
            for scenario in scenarios
        ]
        candidate_tls = save(
            arguments.output,
            f"{subject}-tls-candidate.json",
            {
                "schema_version": "official-client-capture-tls/v1",
                "target_hosts": ["chatgpt.com"],
                "client_hellos": sum(
                    (item["client_hellos"] for item in normalized_candidates), []
                ),
            },
        )
        candidate_tls["client_hello_count"] = len(candidate_tls["client_hellos"])
        save(arguments.output, f"{subject}-tls-candidate.json", candidate_tls)
        add_tls_contract(
            arguments.output,
            results,
            f"{subject}-tls",
            baseline_tls,
            candidate_tls,
            "codex-http" if subject == "codex-http" else "codex-ws",
        )

    nonlite_analyses = sorted(
        arguments.official_nonlite.glob("analysis/mitm/codex-http/*.json")
    )
    if len(nonlite_analyses) == 1:
        nonlite_baseline = load(nonlite_analyses[0])
    else:
        nonlite_baseline = normalize_mitm(
            arguments.output,
            arguments.official_nonlite / "mitm/codex-http",
            "codex-nonlite-baseline.json",
        )
    nonlite_candidate = normalize_mitm(
        arguments.output,
        arguments.candidate_nonlite / "mitm/codex-http",
        "codex-nonlite-candidate.json",
    )
    nonlite_ingress = normalize_mitm(
        arguments.output,
        arguments.candidate_nonlite / "ingress/codex-http",
        "codex-nonlite-ingress.json",
    )
    add_contract(
        arguments.output,
        results,
        "codex-nonlite",
        nonlite_baseline,
        nonlite_candidate,
        nonlite_ingress,
        "oauth-codex-http",
    )
    candidate_nonlite_pcaps = sorted(
        arguments.candidate_nonlite_direct.glob("direct/codex-http-*/egress.pcap")
    )
    if len(candidate_nonlite_pcaps) != 1:
        raise RuntimeError("候选非 Lite direct 必须且只能包含一个 codex-http 场景。")
    nonlite_tls_candidate = normalize_pcap(
        arguments.output,
        candidate_nonlite_pcaps[0],
        "codex-nonlite-tls-candidate.json",
    )
    official_nonlite_analyses = sorted(
        arguments.official_nonlite.glob("analysis/direct/codex-http/*.json")
    )
    if len(official_nonlite_analyses) == 1:
        nonlite_tls_baseline = load(official_nonlite_analyses[0])
    else:
        nonlite_tls_baseline = normalize_pcap(
            arguments.output,
            arguments.official_nonlite / "direct/codex-http/egress.pcap",
            "codex-nonlite-tls-baseline.json",
        )
    add_tls_contract(
        arguments.output,
        results,
        "codex-nonlite-tls",
        nonlite_tls_baseline,
        nonlite_tls_candidate,
        "codex-http",
    )

    compaction_baseline = normalize_mitm(
        arguments.output,
        arguments.official_compaction / "mitm/codex-compact",
        "codex-compaction-baseline.json",
    )
    compaction_candidate = normalize_mitm(
        arguments.output,
        arguments.candidate_compaction / "mitm/codex-compact",
        "codex-compaction-candidate.json",
    )
    compaction_ingress = normalize_mitm(
        arguments.output,
        arguments.candidate_compaction / "ingress/codex-compact",
        "codex-compaction-ingress.json",
    )
    compaction_control_result = compare_official_egress_contract(
        compaction_baseline,
        compaction_candidate,
        compaction_ingress,
        "oauth-codex-http",
    )
    save(
        arguments.output,
        "codex-compaction-control-contract-diff.json",
        compaction_control_result,
    )
    save(
        arguments.output,
        "codex-compaction-control-observation.json",
        {
            "schema_version": "codex-compaction-control-observation/v1",
            "acceptance_contract": False,
            "reason": (
                "thread/compact/start 实测产生带 compaction_trigger 的普通 "
                "/responses 请求，不是 /responses/compact 端点，故不冒充 compact 契约。"
            ),
            "official_record_count": compaction_baseline.get("record_count", 0),
            "candidate_record_count": compaction_candidate.get("record_count", 0),
            "candidate_ingress_record_count": compaction_ingress.get(
                "record_count", 0
            ),
            "observed_contract_equal": compaction_control_result.get("equal"),
            "observed_undeclared_differences": len(
                compaction_control_result.get("undeclared_differences", [])
            ),
        },
    )
    compaction_tls_candidate = normalize_pcap(
        arguments.output,
        arguments.candidate_compaction_direct
        / "direct/codex-compact-compact/egress.pcap",
        "codex-compaction-tls-candidate.json",
    )
    compaction_tls_baseline = normalize_pcap(
        arguments.output,
        arguments.official_compaction / "direct/codex-compact/egress.pcap",
        "codex-compaction-tls-baseline.json",
    )
    add_tls_contract(
        arguments.output,
        results,
        "codex-compaction-control-tls",
        compaction_tls_baseline,
        compaction_tls_candidate,
        "codex-http",
    )

    summary = {
        name: {
            "equal": value.get("equal"),
            "contract_equal": value.get("contract_equal"),
            "raw_equal": value.get("raw_equal"),
            "undeclared_differences": len(
                value.get("undeclared_differences", [])
            ),
            "candidate_semantic_preserved": value.get(
                "candidate_semantic_preserved"
            ),
            "ws_turn_state_lifecycle_valid": value.get(
                "ws_turn_state_lifecycle_valid"
            ),
        }
        for name, value in results.items()
    }
    summary["compaction_control"] = {
        "acceptance_contract": False,
        "reason": "该控制面动作不是 /responses/compact 端点。",
        "observed_equal": compaction_control_result.get("equal"),
        "observed_undeclared_differences": len(
            compaction_control_result.get("undeclared_differences", [])
        ),
    }
    summary["all_equal"] = all(value.get("equal") for value in results.values())
    save(arguments.output, "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_equal"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
