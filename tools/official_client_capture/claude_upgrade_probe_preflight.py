"""完整 Claude VC-5 冻结前的具体 probe／实验闭合预检。

本模块只核断言画像显式声明的 probe 是否进入实际 Job 与相应场景，包括 assertion.reference。
它不解析证据、不运行 checker、不授予认证，也不改写清单或来源；实验定义及版本事实仍由原目录、标签校验。
组计数断言可能仅靠一个现存实验通过，因此不能把场景有材料当成该组全部实验齐全。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from tools.official_client_capture import claude_acceptance_contract as acceptance_contract
from tools.official_client_capture import claude_rule_assertion
from tools.official_client_capture.capturelib.model import ConfigurationError


class ProbeCoverageError(ConfigurationError):
    """一次报告全部缺口，结构化报告供入口打印和短验读取，不作为批准收据。"""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__("完整 VC-5 冻结输入缺少具体 probe／实验：" + json.dumps(report, ensure_ascii=False, sort_keys=True))


def _selector_probes(selector: Mapping[str, Any]) -> set[str]:
    """只取已声明的正向 probe 精确值；不把 regex、否定或其他字段猜成新 probe。"""

    probes: set[str] = set()
    for condition in selector.get("where", []):
        if condition.get("path") != "labels.probe":
            continue
        if condition.get("operator") == "equal":
            probes.add(condition["value"])
        elif condition.get("operator") == "in":
            probes.update(condition["value"])
    return probes


def probe_coverage_preflight(
    profile: Mapping[str, Any], scenario_manifest: Mapping[str, Any], *, suite: str = "full"
) -> dict[str, Any]:
    """只读核实际套件的逐侧 Job／probe／场景闭合，一次返回所有规则与 check 的缺口。"""

    profile = claude_rule_assertion.validate_profile_document(profile, expected_claude_version=str(profile.get("claude_version", "")))
    contract = acceptance_contract.build_contract_payload(profile)
    sides = acceptance_contract.SIDES
    planned: dict[str, dict[str, str]] = {side: {} for side in sides}
    for job in scenario_manifest.get("capture_jobs", []):
        if suite not in job["suites"]:
            continue
        phase = job["phase"]
        if phase not in planned:
            raise ConfigurationError(f"完整 VC-5 预检收到非法 Job 阶段：{phase}")
        for step in job["steps"]:
            probe = step["probe_id"]
            if probe in planned[phase]:
                raise ConfigurationError(f"完整 VC-5 预检收到重复的 {phase} probe：{probe}")
            planned[phase][probe] = job["id"]
    scenario_probes = {
        item["scenario_id"]: set(item["probe_ids"]) for item in scenario_manifest.get("evidence_scenarios", [])
    }
    required: dict[str, set[str]] = {side: set() for side in sides}
    missing: dict[str, set[str]] = {side: set() for side in sides}
    problems: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    for rule in profile["rules"]:
        rule_id = rule["rule_id"]
        mode = contract["validation_modes"][rule_id]
        required_sides = sides if mode == acceptance_contract.MODE_DUAL_WIRE else ("candidate",)
        rule_required: dict[str, set[str]] = {side: set() for side in sides}
        rule_missing: dict[str, set[str]] = {side: set() for side in sides}
        for check in rule["checks"]:
            selectors = [("select", check["select"])]
            reference = check["assertion"].get("reference")
            if isinstance(reference, Mapping):
                selectors.append(("reference", reference))
            default_scenarios = check["select"].get("scenario_ids", rule["scenario_ids"])
            for origin, selector in selectors:
                probes = _selector_probes(selector)
                selected_scenarios = selector.get("scenario_ids", default_scenarios)
                declared = set().union(*(scenario_probes.get(item, set()) for item in selected_scenarios))
                for side in required_sides:
                    if not acceptance_contract.check_applies_to_side(contract, rule_id, check["id"], side):
                        continue
                    required[side].update(probes)
                    rule_required[side].update(probes)
                    absent = probes - set(planned[side])
                    missing[side].update(absent)
                    rule_missing[side].update(absent)
                    unbound = probes - declared
                    if absent or unbound:
                        problems.append({
                            "rule_id": rule_id, "check_id": check["id"], "selector": origin, "side": side,
                            "missing_probe_ids": sorted(absent), "unbound_probe_ids": sorted(unbound),
                        })
        rules.append({
            "rule_id": rule_id,
            "required_probe_ids": {side: sorted(rule_required[side]) for side in sides},
            "missing_probe_ids": {side: sorted(rule_missing[side]) for side in sides},
        })
    return {
        "status": "failed" if problems else "passed", "rule_count": len(profile["rules"]), "suite": suite,
        "required_probe_ids": {side: sorted(required[side]) for side in sides},
        "planned_probe_ids": {side: sorted(planned[side]) for side in sides},
        "missing_probe_ids": {side: sorted(missing[side]) for side in sides},
        "problems": problems, "rules": rules, "live_request_count": 0, "files_written": 0,
    }


def require_probe_coverage(
    profile: Mapping[str, Any], scenario_manifest: Mapping[str, Any], *, suite: str = "full"
) -> dict[str, Any]:
    """完整路径冻结前的失败关闭入口；不裁定原失败类别，不建立评估基线。"""

    report = probe_coverage_preflight(profile, scenario_manifest, suite=suite)
    if report["status"] != "passed":
        raise ProbeCoverageError(report)
    return report
