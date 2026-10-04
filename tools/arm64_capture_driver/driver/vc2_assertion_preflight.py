#!/usr/bin/env python3
"""VC-2 批准前的离线判据门；全部枚举，候选内部判据留给 VC-5。

只读复用正式分类预览和封存读侧，不调用采集、批准或候选验收命令。
报告与正式验收收据分离；消费时重放全部事实，不能靠一份旧的绿色文件放行。
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
CLI_CONFIG = None
if __name__ == "__main__":
    from driver_config import load_config
    try:
        CLI_CONFIG = load_config()
    except (KeyError, ValueError, OSError):
        sys.exit("VC-2 离线门参数拒绝加载")
sys.path.insert(0, os.environ.get("D", str(Path(__file__).resolve().parents[3])))
from tools.official_client_capture import acceptance_contract as contract
from tools.official_client_capture import candidate_rule_assertion as checker
from tools.official_client_capture import codex_upgrade as upgrade

SCHEMA = "arm64-vc2-assertion-preflight/v1"
INPUTS = {
    "target_rule_manifest": "target-rules.json", "migration_manifest": "rule-migration.json",
    "scenario_manifest": "scenarios.json", "profile_manifest": "profile.json",
    "assertion_profile_manifest": "assertion-profile.json",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def plain_path(path):
    if (not path.is_absolute() or ".." in path.parts
            or any(item.is_symlink() for item in (path, *path.parents))):
        raise ValueError("离线门路径必须是无链接和父目录跳转的绝对路径")


def file_binding(path):
    plain_path(path)
    if not path.is_file():
        raise ValueError("离线门缺少绑定文件")
    return {"path": str(path), "sha256": checker.file_sha256(path)}


def read(path):
    file_binding(path)
    value = json.loads(path.read_text(), object_pairs_hook=checker._unique_json_object)
    if not isinstance(value, dict):
        raise ValueError("离线门 JSON 顶层必须为对象")
    return value


def validate_check(check, scenarios):
    """即使本阶段没有合法候选样本，也检查选择器、操作数及正则语法。"""
    selector, assertion = check["select"], check["assertion"]
    if selector.get("record_type") not in contract.KNOWN_RECORD_TYPES:
        raise ValueError("判据记录类型未登记")
    selected = selector.get("scenario_ids", scenarios)
    if not isinstance(selected, list) or not selected or not set(selected).issubset(scenarios):
        raise ValueError("判据选择器场景越出所属规则")
    conditions = selector.get("where", [])
    if not isinstance(conditions, list):
        raise ValueError("判据 where 必须是数组")
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError("判据 where 条件必须是对象")
        operator = condition.get("operator")
        if operator not in {"equal", "not_equal", "present", "absent", "contains", "in", "match", "subset"}:
            raise ValueError("判据 where 操作符未登记")
        expected = {"path", "operator"} | ({"value"} if operator not in {"present", "absent"} else set())
        if set(condition) != expected or not isinstance(condition.get("path"), str) or not condition["path"]:
            raise ValueError("判据 where 字段不闭合")
        if operator == "in" and not isinstance(condition["value"], list):
            raise ValueError("判据 in 操作数必须是数组")
        if operator == "match":
            if not isinstance(condition["value"], str):
                raise ValueError("判据 match 操作数必须是字符串")
            re.compile(condition["value"])
    operator = assertion.get("operator")
    if operator not in checker.ALLOWED_ASSERTION_OPERATORS:
        raise ValueError("判据断言操作符未登记")
    required, optional = {"operator"}, set()
    if operator in {"count_at_least", "count_equal"}:
        required.add("value")
    elif operator == "all_fields_equal":
        required.update({"left_path", "right_path"})
    else:
        required.add("path")
        if operator == "same_set_distinct_order":
            required.update({"minimum_records", "minimum_artifacts", "minimum_distinct_orders"})
        elif operator == "all_ordered_subset_of":
            required.add("allowed")
            optional.add("required")
        elif operator not in {"all_absent", "all_lowercase", "all_list_all_same", "all_list_all_different"}:
            required.add("value")
    if not required.issubset(assertion) or set(assertion) - required - optional:
        raise ValueError("判据断言字段不闭合")
    if operator in {"count_at_least", "count_equal", "distinct_count_at_least"}:
        if type(assertion.get("value")) is not int or assertion["value"] < 0:
            raise ValueError("计数断言必须使用非负整数")
    if operator not in {"count_at_least", "count_equal", "all_fields_equal"}:
        if not isinstance(assertion.get("path"), str) or not assertion["path"]:
            raise ValueError("字段断言缺少 path")
    if operator == "all_match":
        if not isinstance(assertion.get("value"), str):
            raise ValueError("all_match 必须使用正则字符串")
        re.compile(assertion["value"])
    if operator == "all_fields_equal" and any(not isinstance(assertion[key], str) or not assertion[key]
                                               for key in ("left_path", "right_path")):
        raise ValueError("字段比较路径不能为空")
    # 其余复合操作数由同一正式评估器校验，空输入绝不作为通过证据。
    checker._evaluate_assertion([], assertion)


def evaluate(profile, manifest, observations):
    """wire 判据逐项实跑；内部判据只登记待测，不用空样本伪造通过。"""
    payload = contract.build_contract_payload(profile)
    scenarios = {item["scenario_id"]: item for item in profile["scenarios"]}
    artifacts = manifest["artifacts"]
    if any(checker.ENVIRONMENT_PROBE_SNI_FIELD in item for item in artifacts):
        raise ValueError("官方封存包不得使用候选环境探针豁免")
    rows = []
    for rule in profile["rules"]:
        rule_id = rule["rule_id"]
        for check in rule["checks"]:
            validate_check(check, rule["scenario_ids"])
            base = {"rule_id": rule_id, "check_id": check["id"], "check_sha256": digest(check),
                    "validation_mode": payload["validation_modes"][rule_id], "structure": "passed"}
            if not contract.check_applies_to_side(payload, rule_id, check["id"], "official"):
                rows.append({**base, "status": "pending_candidate", "reason": "official_side_not_applicable"})
                continue
            if check["select"]["record_type"] in contract.INTERNAL_RECORD_TYPES:
                rows.append({**base, "status": "pending_candidate", "reason": "requires_candidate_internal_evidence"})
                continue
            selected_scenarios = check["select"].get("scenario_ids", rule["scenario_ids"])
            coverage = {}
            for scenario_id in selected_scenarios:
                kinds = sorted({item["kind"] for item in artifacts if scenario_id in item["scenario_ids"]})
                expected = scenarios[scenario_id]["required_artifact_kinds"]
                coverage[scenario_id] = {"expected": expected, "actual": kinds,
                                         "passed": set(expected).issubset(kinds)}
            matched = checker._select_observations(observations, check["select"], rule["scenario_ids"])
            passed, actual = checker._evaluate_assertion(matched, check["assertion"])
            # 合法负向 count_equal=0 可以无匹配，但仍要求原场景的封存证据完整。
            passed = passed and all(item["passed"] for item in coverage.values())
            rows.append({**base, "status": "passed" if passed else "failed",
                         "matched_count": len(matched), "coverage": coverage,
                         "result_sha256": digest(actual),
                         "evidence_paths_sha256": digest(sorted({p for item in matched for p in item.evidence_paths}))})
    counts = dict(Counter(row["status"] for row in rows))
    return {"status": "passed" if not counts.get("failed") else "failed",
            "rule_count": len(profile["rules"]), "check_count": len(rows), "counts": counts,
            "contract_sha256": contract.contract_sha256(payload), "checks": rows,
            "candidate_acceptance": "required_at_vc5"}


def collect(config, expected_joint):
    """分类预览只读验证官方链和五件套；沿合法导入链保留原始证据坐标。"""
    campaign = Path(config["D"]) / "evidence/campaigns" / config["NEW"]
    inputs = Path(config["D"]) / "control" / config["IN"]
    plain_path(campaign)
    plain_path(inputs)
    paths = {key: inputs / name for key, name in INPUTS.items()}
    paths.update(active_profile=Path(config["ACTIVE_PROFILE"]), profile_patch_manifest=Path(config["PROFILE_PATCH_JSON"]))
    bindings = {key: file_binding(path) for key, path in paths.items()}
    documents = {key: read(paths[key]) for key in INPUTS}
    joint = upgrade._fingerprint({key: upgrade._normalized_json_sha256(value) for key, value in documents.items()})
    if not re.fullmatch(r"[0-9a-f]{64}", expected_joint) or joint != expected_joint:
        raise ValueError("待批准联合摘要不一致")
    manifest = upgrade.load_campaign_manifest(campaign)
    if (manifest["campaign_id"] != config["NEW"] or manifest["target_version"] != config["TARGET_VERSION"]
            or documents["profile_manifest"]["profile_id"] != config["PROFILE_ID"]):
        raise ValueError("离线门 Campaign、目标版本或画像身份不一致")
    preview = upgrade.classify_campaign(campaign, **paths)
    if preview.get("status") != "approval_required" or preview.get("joint_manifest_sha256") != joint:
        raise ValueError("正式分类预览未通过")
    official = upgrade._load_stage_result(campaign, "capture-official")
    if official.get("status") != "complete":
        raise ValueError("官方证据尚未完整封存")
    context = official["assertion_context"]
    evidence_root = Path(context["evidence_root"])
    capture_path = Path(context["capture_manifest_path"])
    plain_path(evidence_root)
    if capture_path.parent != evidence_root or file_binding(capture_path)["sha256"] != context["capture_manifest"]["sha256"]:
        raise ValueError("官方断言包路径或摘要未绑定封存结果")
    profile = checker.load_profile(paths["assertion_profile_manifest"], paths["target_rule_manifest"],
        verify_frozen_digest=False, expected_codex_version=config["TARGET_VERSION"],
        expected_profile_sha256=bindings["assertion_profile_manifest"]["sha256"])
    capture, observations = checker.load_observations(capture_path, evidence_root, config["TARGET_VERSION"])
    results = evaluate(profile, capture, observations)
    if (bindings != {key: file_binding(path) for key, path in paths.items()}
            or checker.file_sha256(capture_path) != context["capture_manifest"]["sha256"]):
        raise ValueError("离线评估期间输入发生漂移")
    return {"schema_version": SCHEMA, "campaign_id": config["NEW"], "target_version": config["TARGET_VERSION"],
            "joint_manifest_sha256": joint, "input_files": bindings, "profile_digest": documents["profile_manifest"]["profile_digest"],
            "official_stage_sha256": upgrade._fingerprint(official), "assertion_context_sha256": digest(context),
            "capture_manifest": file_binding(capture_path), "preview_sha256": digest(preview),
            "evaluator_files": {Path(module.__file__).name: file_binding(Path(module.__file__).resolve())["sha256"]
                                for module in (checker, contract, upgrade)},
            "driver_sha256": checker.file_sha256(Path(__file__).resolve()), "results": results}


def save_report(config, result):
    """内容寻址、原子发布；相同结果再次执行只读复核，不覆盖历史失败报告。"""
    report_root = Path(config["D"]) / "control" / config["IN"] / "vc2-preflight"
    plain_path(report_root)
    report_root.mkdir(mode=0o700, exist_ok=True)
    path = report_root / (digest(result) + ".json")
    if path.exists():
        stored = read(path)
        if stored.get("result") != result or stored.get("result_sha256") != digest(result):
            raise ValueError("已有离线报告损坏")
        return path
    value = {"result": result, "result_sha256": digest(result), "recorded_at_utc": datetime.now(timezone.utc).isoformat()}
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".preflight-", dir=report_root)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read(path).get("result") != result:
                raise ValueError("并发离线报告结果不一致")
    finally:
        Path(temporary).unlink()
    return path


def consume(config, expected_joint, path):
    expected_root = Path(config["D"]) / "control" / config["IN"] / "vc2-preflight"
    if path.parent != expected_root:
        raise ValueError("离线报告不属于本轮输入根")
    stored = read(path)
    result = stored.get("result")
    if (stored.get("result_sha256") != digest(result) or path.name != digest(result) + ".json"
            or result.get("results", {}).get("status") != "passed"):
        raise ValueError("离线报告未通过或被篡改")
    if collect(config, expected_joint) != result:
        raise ValueError("离线报告重放不一致，禁止批准及进入 VC-3")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "verify", "dry-run"))
    parser.add_argument("--joint", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "verify":
            if args.report is None:
                raise ValueError("消费离线报告必须指定 --report")
            result = consume(CLI_CONFIG, args.joint, args.report)
            print(json.dumps(result["results"]["counts"], sort_keys=True))
        else:
            result = collect(CLI_CONFIG, args.joint)
            if args.mode == "record":
                print(save_report(CLI_CONFIG, result))
            else:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if result["results"]["status"] != "passed":
                print("VC-2 官方适用判据未通过，禁止批准及进入 VC-3", file=sys.stderr)
                return 3
    except (ValueError, OSError, KeyError, TypeError, re.error, upgrade.ConfigurationError,
            contract.AcceptanceContractError) as error:
        # 正式库异常可能含原始字段，终端只输出异常类型，不泄露证据正文。
        print(f"VC-2 离线门拒绝：{type(error).__name__}；请复核输入、封存链及判据", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
