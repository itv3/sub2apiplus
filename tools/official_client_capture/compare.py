#!/usr/bin/env python3
"""比较两份已脱敏的官方客户端结构证据。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.analysis import (
    OFFICIAL_EGRESS_CONTRACTS,
    OFFICIAL_EGRESS_TLS_CONTRACTS,
    compare_normalized,
    compare_official_egress_contract,
    compare_official_egress_tls_contract,
)
from tools.official_client_capture.capturelib.security import secure_write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="比较两份脱敏抓包结构。")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--contract",
        choices=OFFICIAL_EGRESS_CONTRACTS,
        help="按 OAuth 官方出站契约验收，同时保留 raw_equal。",
    )
    parser.add_argument(
        "--candidate-ingress",
        type=Path,
        help="契约验收所需的同次候选入站规范化证据。",
    )
    parser.add_argument(
        "--tls-contract",
        choices=OFFICIAL_EGRESS_TLS_CONTRACTS,
        help="从 direct pcap 规范化结果中筛选并比较目标业务 Transport。",
    )
    arguments = parser.parse_args()
    if arguments.contract and arguments.tls_contract:
        parser.error("--contract 与 --tls-contract 不能同时提供。")
    if bool(arguments.contract) != bool(arguments.candidate_ingress):
        parser.error("--contract 与 --candidate-ingress 必须同时提供。")
    baseline = json.loads(arguments.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(arguments.candidate.read_text(encoding="utf-8"))
    if arguments.tls_contract:
        result = compare_official_egress_tls_contract(
            baseline,
            candidate,
            arguments.tls_contract,
        )
    elif arguments.contract:
        candidate_ingress = json.loads(
            arguments.candidate_ingress.read_text(encoding="utf-8")
        )
        result = compare_official_egress_contract(
            baseline,
            candidate,
            candidate_ingress,
            arguments.contract,
        )
    else:
        result = compare_normalized(baseline, candidate)
    if arguments.output:
        secure_write_json(arguments.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["equal"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
