#!/usr/bin/env python3
"""Claude 验收契约：从批准的断言画像机器推导逐规则验收模型（Claude 升级控制面设计 B4-03）。

本模块是 Claude 评估器验收模型的唯一权威实现，结构与 Codex 的 ``acceptance_contract.py`` 相同（那份在 Claude 身份策略里
被忽略，这里不导入、按 Claude 的记录类型另写），产出三样东西，全部由画像内容推导，禁止手写：

1. **validation_mode 分组**：一条规则的全部 check 只选线上可观测的记录（``WIRE_RECORD_TYPES``）即为 ``dual_wire``，
   官方／候选两侧各执行一次机器断言；出现任何取证运行时记录（``INTERNAL_RECORD_TYPES``）即为 ``candidate_profile``，
   只在候选侧执行机器断言，官方权威改为批准画像链（批准断言画像摘要、分类包摘要与联合审核摘要）。
   Claude 的线上记录都经同一个 R 中继落盘：请求（``claude_http_request``）、上游响应（``claude_http_response``）、
   连接（``relay_connection``）、中继按计划给出的合成动作（``relay_intervention``）、ClientHello（``tls_client_hello``）
   与逐 probe 的聚合（``probe_run``，另带同 probe 的实验条件与客户端本地结果：两侧都由同一官方客户端按同一 probe
   驱动，可以逐侧比对）；``runtime_receipt`` 是取证运行自身的完整收据（中继完整性、运行时镜像、上游主机等侧别相关
   内容），按内部记录处理。
2. **分侧覆盖矩阵**：候选侧必须覆盖全部规则引用场景的 ``required_artifact_kinds``；官方侧只须覆盖 ``dual_wire`` 规则
   引用场景的 ``required_artifact_kinds``。
3. **check ID 全集**：每条规则应执行的 check 为画像 check 与通用 ``scenario-artifact-coverage`` 的并集；accept 按此复算，
   不接受人工正负例清单。

契约载荷刻意不含 ``claude_version``：规则集跨版本未变时摘要恒定，批准画像与仓库画像推导出的载荷必须命中同一冻结摘要。
仓库基线画像的冻结摘要按版本登记在 ``FROZEN_CONTRACT_SHA256S``（只用于仓库基线的冻结核对）；compare／accept 以本 Campaign
批准的断言画像推导契约，目标规则集允许相对基线增删，验收权威随批准画像走。

``SIDE_RESTRICTED_CHECKS`` 登记「某一侧结构性不可能出现该实验条件」的 check（门槛与 Codex 相同：必须能指出该侧没有任何
产出路径的机器可核依据，采集遗漏必须重采、不得登记）；本表任何变动都会改变契约摘要并失败关闭。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

PROFILE_SCHEMA = "claude-candidate-rule-expectations/v1"
CONTRACT_SCHEMA = "claude-egress-acceptance-contract/v1"

RESULTS_SCHEMA_V2 = "claude-egress-rule-assertions/v2"

MODE_DUAL_WIRE = "dual_wire"
MODE_CANDIDATE_PROFILE = "candidate_profile"

COVERAGE_CHECK_ID = "scenario-artifact-coverage"

SIDES = ("official", "candidate")

# 侧别限定 check：{(规则, check): 适用侧}。当前没有任何登记（Claude 两侧都经同一 R 中继取证）。
SIDE_RESTRICTED_CHECKS: dict[tuple[str, str], tuple[str, ...]] = {}

WIRE_RECORD_TYPES = frozenset(
    {"claude_http_request", "claude_http_response", "relay_connection", "relay_intervention", "probe_run", "tls_client_hello"}
)
INTERNAL_RECORD_TYPES = frozenset({"runtime_receipt"})
KNOWN_RECORD_TYPES = WIRE_RECORD_TYPES | INTERNAL_RECORD_TYPES

# 仓库基线画像（claude_rule_expectations_<版本>.json）推导出的契约冻结摘要，按版本登记；由
# ``python3 claude_acceptance_contract.py --profile <画像> --print-digest`` 生成，画像规则集变化时必然漂移，
# 必须连同分组与覆盖矩阵一起重新审核后才能更新。
FROZEN_CONTRACT_SHA256S: dict[str, str] = {
    "2.1.226": "0d9e5c0dfed617aba476b24f9399c39974ba716c7fc08bac80a2186c265b330e",
}


class AcceptanceContractError(RuntimeError):
    """画像不足以推导验收契约，或推导结果与冻结契约不一致。"""


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceContractError(f"{label} 必须是非空字符串")
    return value


def load_profile(path: Path) -> dict[str, Any]:
    """轻量读取断言画像：只核 schema 与非空结构，完整校验由 checker 的 ``load_profile`` 承担。"""

    try:
        profile = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AcceptanceContractError(f"断言画像不可读：{error}") from error
    if not isinstance(profile, dict) or profile.get("schema_version") != PROFILE_SCHEMA:
        raise AcceptanceContractError("断言画像 schema_version 不受支持")
    if not isinstance(profile.get("rules"), list) or not profile["rules"]:
        raise AcceptanceContractError("断言画像 rules 不能为空")
    if not isinstance(profile.get("scenarios"), list) or not profile["scenarios"]:
        raise AcceptanceContractError("断言画像 scenarios 不能为空")
    return profile


def _rules(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rules = profile.get("rules")
    if not isinstance(rules, list) or not rules:
        raise AcceptanceContractError("断言画像 rules 不能为空")
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise AcceptanceContractError("断言画像规则必须是对象")
        rule_id = _require_str(rule.get("rule_id"), "rule_id")
        if rule_id in seen:
            raise AcceptanceContractError(f"断言画像规则重复：{rule_id}")
        seen.add(rule_id)
        if not isinstance(rule.get("checks"), list) or not rule["checks"]:
            raise AcceptanceContractError(f"{rule_id} 没有 check")
    return list(rules)


def _record_types(rule: Mapping[str, Any]) -> list[str]:
    types: list[str] = []
    for check in rule["checks"]:
        select = check.get("select") if isinstance(check, Mapping) else None
        record_type = select.get("record_type") if isinstance(select, Mapping) else None
        record_type = _require_str(record_type, f"{rule['rule_id']} check.select.record_type")
        if record_type not in KNOWN_RECORD_TYPES:
            raise AcceptanceContractError(f"{rule['rule_id']} 引用未登记的记录类型：{record_type}")
        types.append(record_type)
    return types


def derive_validation_modes(profile: Mapping[str, Any]) -> dict[str, str]:
    """规则的全部 check 都只选线上记录为 ``dual_wire``，否则 ``candidate_profile``。"""

    return {
        str(rule["rule_id"]): (
            MODE_DUAL_WIRE if all(item in WIRE_RECORD_TYPES for item in _record_types(rule)) else MODE_CANDIDATE_PROFILE
        )
        for rule in _rules(profile)
    }


def derive_expected_check_ids(profile: Mapping[str, Any]) -> dict[str, list[str]]:
    """每条规则应执行的 check 全集：覆盖 check 在前，画像 check 按画像顺序；ID 不得重复或与覆盖 check 同名。"""

    expected: dict[str, list[str]] = {}
    for rule in _rules(profile):
        ids = [COVERAGE_CHECK_ID]
        for check in rule["checks"]:
            check_id = _require_str(check.get("id"), f"{rule['rule_id']} check.id")
            if check_id in ids:
                raise AcceptanceContractError(f"{rule['rule_id']} check ID 重复：{check_id}")
            ids.append(check_id)
        expected[str(rule["rule_id"])] = ids
    return expected


def derive_side_restricted_checks(profile: Mapping[str, Any]) -> dict[str, dict[str, list[str]]]:
    """把侧别限定表投影到画像：规则不在画像里时跳过（规则可删），规则在而 check 不在即失败关闭。"""

    checks_by_rule = {
        str(rule["rule_id"]): {str(check.get("id")) for check in rule["checks"] if isinstance(check, Mapping)}
        for rule in _rules(profile)
    }
    restricted: dict[str, dict[str, list[str]]] = {}
    for (rule_id, check_id), sides in sorted(SIDE_RESTRICTED_CHECKS.items()):
        if rule_id not in checks_by_rule:
            continue
        if check_id not in checks_by_rule[rule_id]:
            raise AcceptanceContractError(f"侧别限定表登记的 check 不在画像里：{rule_id}／{check_id}")
        if not sides or any(side not in SIDES for side in sides):
            raise AcceptanceContractError(f"侧别限定表的适用侧非法：{rule_id}／{check_id}")
        restricted.setdefault(rule_id, {})[check_id] = sorted(sides)
    return restricted


def check_applies_to_side(contract: Mapping[str, Any], rule_id: str, check_id: str, side: str) -> bool:
    """未登记的 check 两侧都适用。"""

    sides = (contract.get("side_restricted_checks") or {}).get(rule_id, {}).get(check_id)
    return sides is None or side in sides


def expected_check_ids_for_side(contract: Mapping[str, Any], rule_id: str, side: str) -> list[str]:
    """本侧应执行的 check：全集减去本侧不适用项。"""

    if side not in SIDES:
        raise AcceptanceContractError(f"验收侧非法：{side}")
    expected = contract.get("expected_check_ids", {}).get(rule_id)
    if not isinstance(expected, list):
        raise AcceptanceContractError(f"契约没有规则 {rule_id}")
    return [check_id for check_id in expected if check_applies_to_side(contract, rule_id, check_id, side)]


def derive_side_coverage(profile: Mapping[str, Any], modes: Mapping[str, str]) -> dict[str, dict[str, list[str]]]:
    """候选侧覆盖全部规则引用的场景，官方侧只覆盖 ``dual_wire`` 规则引用的场景；值为 required_artifact_kinds。"""

    scenarios: dict[str, list[str]] = {}
    for scenario in profile.get("scenarios") or []:
        if not isinstance(scenario, Mapping):
            raise AcceptanceContractError("断言画像场景必须是对象")
        scenario_id = _require_str(scenario.get("scenario_id"), "scenario_id")
        kinds = scenario.get("required_artifact_kinds")
        if not isinstance(kinds, list) or not kinds or not all(isinstance(item, str) and item for item in kinds):
            raise AcceptanceContractError(f"{scenario_id} 的 required_artifact_kinds 非法")
        scenarios[scenario_id] = sorted(set(kinds))
    coverage: dict[str, dict[str, list[str]]] = {"official": {}, "candidate": {}}
    for rule in _rules(profile):
        rule_id = str(rule["rule_id"])
        scenario_ids = rule.get("scenario_ids")
        if not isinstance(scenario_ids, list) or not scenario_ids:
            raise AcceptanceContractError(f"{rule_id} 的 scenario_ids 不能为空")
        for scenario_id in scenario_ids:
            if scenario_id not in scenarios:
                raise AcceptanceContractError(f"{rule_id} 引用未定义场景：{scenario_id}")
            coverage["candidate"][scenario_id] = scenarios[scenario_id]
            if modes[rule_id] == MODE_DUAL_WIRE:
                coverage["official"][scenario_id] = scenarios[scenario_id]
    return {side: dict(sorted(values.items())) for side, values in coverage.items()}


def build_contract_payload(profile: Mapping[str, Any]) -> dict[str, Any]:
    """契约载荷：分组、check 全集、侧别限定、分侧覆盖与计数；刻意不含版本号。"""

    modes = derive_validation_modes(profile)
    return {
        "schema_version": CONTRACT_SCHEMA,
        "coverage_check_id": COVERAGE_CHECK_ID,
        "validation_modes": dict(sorted(modes.items())),
        "expected_check_ids": dict(sorted(derive_expected_check_ids(profile).items())),
        "side_restricted_checks": derive_side_restricted_checks(profile),
        "side_coverage": derive_side_coverage(profile, modes),
        "rule_counts": {
            MODE_DUAL_WIRE: sum(1 for mode in modes.values() if mode == MODE_DUAL_WIRE),
            MODE_CANDIDATE_PROFILE: sum(1 for mode in modes.values() if mode == MODE_CANDIDATE_PROFILE),
        },
    }


def contract_sha256(payload: Mapping[str, Any]) -> str:
    """契约摘要：排序紧凑 JSON 的 sha256（无末尾换行，与 Codex 契约同一口径）。"""

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_frozen_contract(profile: Mapping[str, Any], version: str) -> dict[str, Any]:
    """仓库基线画像的契约必须命中该版本登记的冻结摘要。"""

    frozen = FROZEN_CONTRACT_SHA256S.get(version)
    if frozen is None:
        raise AcceptanceContractError(f"版本 {version} 没有登记仓库基线契约冻结摘要")
    payload = build_contract_payload(profile)
    actual = contract_sha256(payload)
    if actual != frozen:
        raise AcceptanceContractError(
            f"{version} 验收契约摘要漂移：实际 {actual}，冻结 {frozen}；规则集变化必须连同分组与覆盖矩阵重新审核"
        )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="由 Claude 断言画像推导验收契约")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--print-digest", action="store_true", help="只打印契约摘要")
    parser.add_argument("--verify-frozen", metavar="VERSION", help="核对该版本登记的冻结摘要")
    parser.add_argument("--output", type=Path, help="写出契约载荷（已存在即拒绝）")
    arguments = parser.parse_args(argv)
    try:
        profile = load_profile(arguments.profile)
        payload = (
            verify_frozen_contract(profile, arguments.verify_frozen)
            if arguments.verify_frozen
            else build_contract_payload(profile)
        )
    except AcceptanceContractError as error:
        print(f"Claude 验收契约不可用：{error}", file=sys.stderr)
        return 1
    digest = contract_sha256(payload)
    if arguments.print_digest:
        print(digest)
        return 0
    if arguments.output is not None:
        if arguments.output.exists() or arguments.output.is_symlink():
            print(f"输出已存在，拒绝覆盖：{arguments.output}", file=sys.stderr)
            return 1
        arguments.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"contract_sha256": digest, "rule_counts": payload["rule_counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
