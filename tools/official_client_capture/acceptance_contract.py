#!/usr/bin/env python3
"""ACC-01 验收契约：从批准断言画像机器推导逐规则验收模型。

本模块是 §10.8.10 验收模型的单一权威实现，产出三样东西，全部由画像内容推导，
禁止手写：

1. **validation_mode 分组**：一条规则的全部 check 只选 wire record
   （``http_request``／``websocket_frame``／``tls_client_hello``）即为 ``dual_wire``，
   要求官方／候选双侧机器断言；出现任何内部 record type 即为 ``candidate_profile``，
   只在候选侧执行机器断言，官方权威改为批准画像链。
2. **分侧覆盖矩阵**：候选侧必须覆盖全部规则引用场景的 ``required_artifact_kinds``；
   官方侧只须覆盖 ``dual_wire`` 规则引用场景的 ``required_artifact_kinds``。
3. **check ID 全集**：每条规则应执行的 check 为画像 check 与通用
   ``scenario-artifact-coverage`` 的并集；accept 按此复算，不再接受人工正负例清单。

契约载荷刻意不含 ``codex_version``：规则集跨版本未变时摘要恒定，批准画像与仓库
画像推导出的载荷必须命中同一冻结摘要；任何规则集变化都会使摘要漂移并 fail-close，
强制显式重审本契约。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

PROFILE_SCHEMA = "codex-candidate-rule-expectations/v1"
CONTRACT_SCHEMA = "codex-egress-acceptance-contract/v1"

RESULTS_SCHEMA_V2 = "codex-egress-rule-assertions/v2"
LEGACY_RESULTS_SCHEMAS = frozenset({"codex-egress-rule-assertions/v1"})

MODE_DUAL_WIRE = "dual_wire"
MODE_CANDIDATE_PROFILE = "candidate_profile"

COVERAGE_CHECK_ID = "scenario-artifact-coverage"

WIRE_RECORD_TYPES = frozenset(
    {"http_request", "websocket_frame", "tls_client_hello"}
)
# 与 candidate_test_trace.ALLOWED_RECORD_TYPES 保持一致；一致性由离线测试锁定，
# 不在运行时互相 import，避免契约权威与产出器实现纠缠。
INTERNAL_RECORD_TYPES = frozenset(
    {
        "alpha_search_flow",
        "compaction_decision",
        "conditional_header",
        "connection_lifecycle",
        "file_upload_chain",
        "header_assembly",
        "image_edit_encoding",
        "image_tool_flow",
        "lite_transform",
        "realtime_chain",
        "response_prefix_reuse",
        "serialization_boundary",
        "surface_identity",
        "transport_fallback",
        "turn_state_chain",
        "websocket_compression_context",
    }
)
KNOWN_RECORD_TYPES = WIRE_RECORD_TYPES | INTERNAL_RECORD_TYPES

DEFAULT_PROFILE_RELATIVE_PATH = (
    "tools/official_client_capture/candidate_rule_expectations_0_145_0.json"
)

# 由 `python3 acceptance_contract.py --print-digest` 生成；画像规则集变化时本摘要
# 必然漂移，必须连同 25／17 分组与覆盖矩阵一起重新审核后才能更新。
FROZEN_CONTRACT_SHA256 = (
    "14e9e33652ea01e7fa5a9cd51eeffce32b20e73d48bc93ea99ba40af069df957"
)


class AcceptanceContractError(RuntimeError):
    """画像不足以推导验收契约，或推导结果与冻结契约不一致。"""


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceContractError(f"{label} 必须是非空字符串")
    return value


def load_profile(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AcceptanceContractError(f"无法读取断言画像：{error}") from error
    if not isinstance(document, dict):
        raise AcceptanceContractError("断言画像必须是 JSON 对象")
    if document.get("schema_version") != PROFILE_SCHEMA:
        raise AcceptanceContractError(
            f"断言画像 schema_version 必须是 {PROFILE_SCHEMA}"
        )
    for field in ("scenarios", "rules"):
        if not isinstance(document.get(field), list) or not document[field]:
            raise AcceptanceContractError(f"断言画像 {field} 必须是非空数组")
    return document


def _scenario_kinds(profile: Mapping[str, Any]) -> dict[str, list[str]]:
    kinds_by_scenario: dict[str, list[str]] = {}
    for scenario in profile["scenarios"]:
        if not isinstance(scenario, dict):
            raise AcceptanceContractError("scenarios 项必须是对象")
        scenario_id = _require_str(scenario.get("scenario_id"), "scenario_id")
        if scenario_id in kinds_by_scenario:
            raise AcceptanceContractError(f"场景重复定义：{scenario_id}")
        raw_kinds = scenario.get("required_artifact_kinds")
        if not isinstance(raw_kinds, list) or not raw_kinds:
            raise AcceptanceContractError(
                f"场景 {scenario_id} 的 required_artifact_kinds 必须非空"
            )
        kinds = sorted({_require_str(kind, "artifact kind") for kind in raw_kinds})
        if len(kinds) != len(raw_kinds):
            raise AcceptanceContractError(
                f"场景 {scenario_id} 的 required_artifact_kinds 存在重复"
            )
        kinds_by_scenario[scenario_id] = kinds
    return kinds_by_scenario


def _rule_record_types(rule: Mapping[str, Any], rule_id: str) -> set[str]:
    checks = rule.get("checks")
    if not isinstance(checks, list) or not checks:
        raise AcceptanceContractError(f"规则 {rule_id} 的 checks 必须非空")
    record_types: set[str] = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("select"), dict):
            raise AcceptanceContractError(f"规则 {rule_id} 存在非法 check")
        record_type = _require_str(
            check["select"].get("record_type"), f"规则 {rule_id} 的 record_type"
        )
        if record_type not in KNOWN_RECORD_TYPES:
            raise AcceptanceContractError(
                f"规则 {rule_id} 使用未知 record type：{record_type}；"
                "新增类型必须先修订验收契约并重新冻结摘要"
            )
        record_types.add(record_type)
    return record_types


def derive_validation_modes(profile: Mapping[str, Any]) -> dict[str, str]:
    """逐规则推导 validation_mode；任何未知 record type 都失败关闭。"""

    modes: dict[str, str] = {}
    for rule in profile["rules"]:
        if not isinstance(rule, dict):
            raise AcceptanceContractError("rules 项必须是对象")
        rule_id = _require_str(rule.get("rule_id"), "rule_id")
        if rule_id in modes:
            raise AcceptanceContractError(f"规则重复定义：{rule_id}")
        record_types = _rule_record_types(rule, rule_id)
        modes[rule_id] = (
            MODE_DUAL_WIRE
            if record_types <= WIRE_RECORD_TYPES
            else MODE_CANDIDATE_PROFILE
        )
    return modes


def derive_expected_check_ids(profile: Mapping[str, Any]) -> dict[str, list[str]]:
    """每条规则应执行的 check ID 全集：通用覆盖 check ＋ 画像 check，顺序固定。"""

    expected: dict[str, list[str]] = {}
    for rule in profile["rules"]:
        rule_id = _require_str(rule.get("rule_id"), "rule_id")
        check_ids: list[str] = [COVERAGE_CHECK_ID]
        for check in rule.get("checks") or []:
            check_id = _require_str(check.get("id"), f"规则 {rule_id} 的 check id")
            if check_id in check_ids:
                raise AcceptanceContractError(
                    f"规则 {rule_id} 的 check id 重复或与通用覆盖 check 同名："
                    f"{check_id}"
                )
            check_ids.append(check_id)
        expected[rule_id] = check_ids
    return expected


def derive_side_coverage(
    profile: Mapping[str, Any],
    modes: Mapping[str, str],
) -> dict[str, dict[str, list[str]]]:
    """分侧覆盖矩阵：候选侧取全部规则引用场景，官方侧只取 dual_wire 规则引用场景。"""

    kinds_by_scenario = _scenario_kinds(profile)
    candidate_scenarios: set[str] = set()
    official_scenarios: set[str] = set()
    for rule in profile["rules"]:
        rule_id = _require_str(rule.get("rule_id"), "rule_id")
        scenario_ids = rule.get("scenario_ids")
        if not isinstance(scenario_ids, list) or not scenario_ids:
            raise AcceptanceContractError(f"规则 {rule_id} 的 scenario_ids 必须非空")
        for scenario_id in scenario_ids:
            scenario_id = _require_str(scenario_id, f"规则 {rule_id} 的 scenario id")
            if scenario_id not in kinds_by_scenario:
                raise AcceptanceContractError(
                    f"规则 {rule_id} 引用未定义场景：{scenario_id}"
                )
            candidate_scenarios.add(scenario_id)
            if modes[rule_id] == MODE_DUAL_WIRE:
                official_scenarios.add(scenario_id)
    return {
        "official": {
            scenario_id: kinds_by_scenario[scenario_id]
            for scenario_id in sorted(official_scenarios)
        },
        "candidate": {
            scenario_id: kinds_by_scenario[scenario_id]
            for scenario_id in sorted(candidate_scenarios)
        },
    }


def build_contract_payload(profile: Mapping[str, Any]) -> dict[str, Any]:
    """契约载荷；刻意不含 codex_version，规则集未变时跨版本摘要恒定。"""

    modes = derive_validation_modes(profile)
    payload = {
        "schema_version": CONTRACT_SCHEMA,
        "coverage_check_id": COVERAGE_CHECK_ID,
        "validation_modes": dict(sorted(modes.items())),
        "expected_check_ids": dict(
            sorted(derive_expected_check_ids(profile).items())
        ),
        "side_coverage": derive_side_coverage(profile, modes),
    }
    counts = {
        MODE_DUAL_WIRE: sum(
            1 for mode in modes.values() if mode == MODE_DUAL_WIRE
        ),
        MODE_CANDIDATE_PROFILE: sum(
            1 for mode in modes.values() if mode == MODE_CANDIDATE_PROFILE
        ),
    }
    payload["rule_counts"] = counts
    return payload


def contract_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def verify_frozen_contract(profile: Mapping[str, Any]) -> dict[str, Any]:
    """现算契约并断言命中冻结摘要；供 seal／accept 消费。"""

    payload = build_contract_payload(profile)
    digest = contract_sha256(payload)
    if digest != FROZEN_CONTRACT_SHA256:
        raise AcceptanceContractError(
            "验收契约与冻结摘要不一致：画像规则集已变化，必须重新审核 25／17 分组、"
            f"覆盖矩阵并更新冻结摘要（现算 {digest}）"
        )
    return payload


def repository_profile_path() -> Path:
    return Path(__file__).resolve().parents[2] / DEFAULT_PROFILE_RELATIVE_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="推导并冻结逐规则验收契约")
    parser.add_argument(
        "--profile",
        type=Path,
        default=repository_profile_path(),
        help="断言画像路径（默认取仓库冻结画像）",
    )
    parser.add_argument(
        "--print-digest",
        action="store_true",
        help="只输出契约载荷摘要",
    )
    parser.add_argument(
        "--verify-frozen",
        action="store_true",
        help="断言画像推导结果命中冻结摘要",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="把契约载荷写入指定文件供人工审核",
    )
    arguments = parser.parse_args()
    try:
        profile = load_profile(arguments.profile)
        if arguments.verify_frozen:
            payload = verify_frozen_contract(profile)
        else:
            payload = build_contract_payload(profile)
        digest = contract_sha256(payload)
    except AcceptanceContractError as error:
        print(f"验收契约推导失败：{error}", file=sys.stderr)
        return 1
    if arguments.output is not None:
        arguments.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if arguments.print_digest:
        print(digest)
    else:
        counts = payload["rule_counts"]
        print(
            f"验收契约有效：{MODE_DUAL_WIRE}={counts[MODE_DUAL_WIRE]}，"
            f"{MODE_CANDIDATE_PROFILE}={counts[MODE_CANDIDATE_PROFILE]}，"
            f"摘要={digest}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
