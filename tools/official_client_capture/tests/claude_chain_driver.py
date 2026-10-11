"""候选 B 组录制恢复驱动：同一录制世界的真实封存、构建、取证、对账、批准与续跑。

只接受显式历史 2.1.226 来源。在 VC-0 前冻结 40 规则、全部原候选 probe 与两 Job 的调度聚合映射。
原始 linux/amd64 事实、R/P 字节和宿主收据保持；本次 ARM64 runtime 与预约独立生成并重验。
Docker、构建和审核输入沿用已有明示离线来源；不替换正式准入、封存、Store、账本或恢复判定。
本模块只允许 fixture_only/regression_fixture，不登记任何正式来源或完整升级认证。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_chain_diagnostic_budget as diagnostic_budget
from tools.official_client_capture.tests import claude_fault_chain_world as stops
from tools.official_client_capture.tests import claude_recorded_full_validation_driver as full
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_recovery as recovery_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests

CANDIDATE = full.CANDIDATE
LIVE_JOB = candidate_tests.LIVE_JOB
REFRESH_JOB = candidate_tests.REFRESH_JOB
REFRESH_PROBE = "v4-oauth-refresh"
EVOLVED_DEPENDENCY = "claude_run_candidate_capture.sh"
ACTIVATION_MODULE = "tools.official_client_capture.tests.claude_chain_driver"
AGGREGATION_SCHEMA = "claude-recorded-candidate-job-aggregation/v1"
PREPARED_SCHEMA = "claude-recorded-candidate-recovery-start/v1"
PREPARED_ENV = "CLAUDE_CANDIDATE_RECORDED_START"
SOURCE_INITIALIZATION = "candidate-source-initialization"
# 与原候选短验相同的不可用假 token；只在断网录制适配作用域满足文件前检。
FIXTURE_ADMIN_TOKEN = b"hdr.payload.sig\n"
TOKEN_PREFLIGHT_ERROR = ("Claude 升级失败：计划里有网关账号刷新触发的步骤：先签发管理 token，并用 "
    "ADMIN_BEARER_TOKEN_FILE 指向只读 token 文件（不要放进命令行）。\n")
SCENARIO_METHODS = {
    "reconcile-resume": "_scenario_candidate_reconcile_resume",
    "tool-evolution": "_scenario_candidate_tool_evolution",
}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _coordinate_projection(value: Any, original: str, target: str) -> Any:
    """聚合只改独占证据根的精确前缀；业务参数、probe、触发条件和原步骤其它字段保持。"""

    if isinstance(value, dict):
        return {key: _coordinate_projection(item, original, target) for key, item in value.items()}
    if isinstance(value, list):
        return [_coordinate_projection(item, original, target) for item in value]
    if isinstance(value, str) and (value == original or value.startswith(original + "/")):
        return target + value[len(original):]
    return value


def aggregate_scenarios(original: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """40/40 与所有原步骤保留，只在建 Campaign 前改变 B 组控制回归的调度粒度。"""

    plan.validate_scenario_manifest(original)
    candidates = [job for job in original["capture_jobs"] if job["phase"] == "candidate"]
    by_id = {job["id"]: job for job in candidates}
    if LIVE_JOB not in by_id or REFRESH_JOB not in by_id:
        raise replay.ReplayError("聚合来源缺少原主作业或独立刷新作业")
    non_refresh = [job for job in candidates if job["id"] != REFRESH_JOB]
    live = copy.deepcopy(by_id[LIVE_JOB])
    if any(job["suites"] != live["suites"] or job["required"] is not True for job in non_refresh):
        raise replay.ReplayError("B 组聚合不能改变原套件或把可选作业冒充必需作业")
    live.update(description="fixture_only：全部原非刷新候选步骤的调度聚合；不替代正式 Job 分母。", steps=[])
    for key in ("scenario_ids", "covers", "tool_dependencies", "required_scenario_receipts"):
        live[key] = sorted({value for job in non_refresh for value in job[key]})
    target_root, mapping = live["evidence_roots"][0], []
    for job in candidates:
        target_id = REFRESH_JOB if job["id"] == REFRESH_JOB else LIVE_JOB
        for index, step in enumerate(job["steps"], 1):
            projected = (copy.deepcopy(step) if target_id == REFRESH_JOB else
                         _coordinate_projection(step, job["evidence_roots"][0], target_root))
            number = index if target_id == REFRESH_JOB else len(live["steps"]) + 1
            if target_id == LIVE_JOB:
                live["steps"].append(projected)
            mapping.append({"original_job_id": job["id"], "original_step_index": index, "probe_id": step["probe_id"],
                "target_job_id": target_id, "target_step_index": number,
                "original_step_sha256": _digest(step), "projected_step_sha256": _digest(projected),
                "original_evidence_root": job["evidence_roots"][0],
                "target_evidence_root": by_id[REFRESH_JOB]["evidence_roots"][0] if target_id == REFRESH_JOB else target_root})
    grouped = copy.deepcopy(original)
    grouped["capture_jobs"] = [copy.deepcopy(job) for job in original["capture_jobs"] if job["phase"] == "official"]
    grouped["capture_jobs"].extend([live, copy.deepcopy(by_id[REFRESH_JOB])])
    plan.validate_scenario_manifest(grouped)
    rules = sorted({rule for job in grouped["capture_jobs"] if job["phase"] == "official" for rule in job["covers"]})
    if len(rules) != 40:
        raise replay.ReplayError("候选聚合来源必须保留完整40规则")
    plan.validate_full_coverage([argparse.Namespace(**{**job, "job_id": job["id"]}) for job in grouped["capture_jobs"]], rules)
    return grouped, mapping


def _extend_refresh(original: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """已有完整录制链没执行刷新；B 组从同一版本模板声明并补回官方与候选的完整刷新 Job。"""

    result = copy.deepcopy(original)
    identifiers = {job["id"] for job in result["capture_jobs"]}
    example = result["capture_jobs"][0]["steps"][0]["argv"]
    facts = {flag: example[example.index(flag) + 1]
             for flag in ("--fw-f-v5-version-facts", "--fw-f-v5-version-facts-sha256")}
    for job in template["capture_jobs"]:
        if job["id"] not in {"official-synthetic-oauth-refresh", REFRESH_JOB} or job["id"] in identifiers:
            continue
        projected = copy.deepcopy(job)
        for step in projected["steps"]:
            for flag, value in facts.items():
                step["argv"][step["argv"].index(flag) + 1] = value
        result["capture_jobs"].append(projected)
    scenario_ids = {row["scenario_id"] for row in result["evidence_scenarios"]}
    result["evidence_scenarios"].extend(copy.deepcopy(row) for row in template["evidence_scenarios"]
                                        if REFRESH_PROBE in row["probe_ids"] and row["scenario_id"] not in scenario_ids)
    result["probe_catalog"]["probe_count"] = len({step["probe_id"] for job in result["capture_jobs"] if job["phase"] == "official"
        for step in job["steps"] if plan.step_model(step["probe_id"], step["argv"]) is None})
    plan.validate_scenario_manifest(result)
    return result


def _aggregate_labels(original: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    labels = copy.deepcopy(original)
    present = {entry["job_id"] for entry in labels["entries"]}
    labels["entries"].extend(copy.deepcopy(entry) for entry in template["entries"]
                            if entry["job_id"] in {"official-synthetic-oauth-refresh", REFRESH_JOB} and entry["job_id"] not in present)
    live = {"job_id": LIVE_JOB, "side": "candidate", "rules": []}
    entries = []
    for entry in labels["entries"]:
        if entry["side"] == "candidate" and entry["job_id"] != REFRESH_JOB:
            live["rules"].extend(copy.deepcopy(entry["rules"]))
        else:
            entries.append(entry)
    labels["entries"] = [*entries, live]
    return labels


def inputs(data: Path) -> dict[str, Any]:
    """完整控制输入先形成原件，再写独立聚合副本和映射；一切在 source state 与 Campaign 冻结之前完成。"""

    configured = full._inputs(data)
    original_binding = configured["bindings"]["scenarios"]
    original = source.read(source.bound(original_binding, root=data))
    template_path, label_template_path = plan.scenario_manifest_path("2.1.285"), plan.evidence_labels_path("2.1.285")
    extended = _extend_refresh(original, source.read(template_path))
    grouped, mapping = aggregate_scenarios(extended)
    root = data / "control/candidate-recovery"
    root.mkdir(mode=0o700)
    extended_path = source.write(root / "original-scenarios.json", extended)
    grouped_path = source.write(root / "grouped-scenarios.json", grouped)
    labels = _aggregate_labels(source.read(source.bound(configured["bindings"]["labels"], root=data)), source.read(label_template_path))
    labels_path = source.write(root / "grouped-labels.json", labels)
    aggregation = source.write(root / "aggregation.json", {
        "schema_version": AGGREGATION_SCHEMA, "fixture_only": True, "purpose": "regression_fixture",
        "baseline_scenarios": original_binding, "refresh_template": source.binding(template_path),
        "original_scenarios": source.binding(extended_path), "grouped_scenarios": source.binding(grouped_path),
        "original_labels": configured["bindings"]["labels"], "label_template": source.binding(label_template_path),
        "grouped_labels": source.binding(labels_path), "rules": configured["bindings"]["rules"], "step_mapping": mapping,
        "scope": "只改变候选 B 组控制回归调度粒度；原40规则和全部步骤保留，不替代原正式Job分母。",
        "formal_version_semantics_certified": False,
    })
    source.write(root / "failures.json", {})
    control = source.write(root / "control.json", {"schema_version": source.RECOVERY_CONTROL_SCHEMA,
        "fixture_only": True, "purpose": "regression_fixture", "control_root": str(root), "allowed_failure_probe_ids": [REFRESH_PROBE]})
    configured["bindings"].update(scenarios=source.binding(grouped_path), labels=source.binding(labels_path),
                                    candidate_aggregation=source.binding(aggregation))
    configured["candidate_recovery_control"] = source.binding(control)
    configured["jobs"] = {job["id"]: [step["probe_id"] for step in job["steps"]]
                            for job in grouped["capture_jobs"] if job["phase"] == "official"}
    configured["candidate_jobs"] = {job["id"]: [step["probe_id"] for step in job["steps"]]
                                     for job in grouped["capture_jobs"] if job["phase"] == "candidate"}
    validate_aggregation({"data_root": str(data), "inputs": configured})
    return configured


def _partial_input_origins(data: Path) -> dict[str, Any]:
    """绑定原输入生产者的三份来源说明；续作只读它们，不重调一次性生产者。"""

    bindings = {name: source.binding(data / "control" / relative) for name, relative in (
        ("base", "fixture-inputs/projection-origin.json"),
        ("active", "fixture-active-projection/projection-origin.json"),
        ("aggregation", "candidate-recovery/aggregation.json"))}
    for value in bindings.values():
        source.bound(value, root=data)
    return bindings


def _restore_partial_inputs(data: Path) -> dict[str, Any]:
    """从完整原件恢复原 inputs 引用集合；缺件、跨根、摘要漂移均拒绝，不写任何输入。"""

    _partial_input_origins(data)
    base_root, active_root, candidate_root = [data / "control" / name for name in (
        "fixture-inputs", "fixture-active-projection", "candidate-recovery")]
    origin = source.read(base_root / "projection-origin.json")
    if (set(origin) != {"fixture_only", "purpose", "target_version", "template_version", "inputs",
                        "static_conclusions", "template_version_facts"}
            or origin["fixture_only"] is not True or origin["purpose"] != "control_input_projection"
            or origin["target_version"] != source.VERSION or origin["template_version"] != "2.1.285"):
        raise replay.ReplayError("候选原基础输入来源说明不闭合")
    base_names = {"baseline": "baseline.json", "rules": "rules.json", "scenarios": "scenarios.json",
                  "labels": "labels.json", "inventory": "target-inventory.json",
                  "containment": "sink-containment.json", "dispositions": "dispositions.json"}
    records = copy.deepcopy(origin["inputs"])
    if set(records) != {*base_names, "version_facts"}:
        raise replay.ReplayError("候选原基础输入分母不闭合")
    for name, filename in base_names.items():
        if source.bound(records[name], root=data) != base_root / filename:
            raise replay.ReplayError("候选原基础输入坐标错位")
    facts_relative = "tools/official_client_capture/tests/fixtures/claude_recorded_version_facts.json"
    if source.bound(records["version_facts"], root=data) != data / facts_relative:
        raise replay.ReplayError("候选原版本事实不是原数据根副本")
    source.bound(origin["template_version_facts"])
    template = source.read(plan.scenario_manifest_path("2.1.285"))
    if origin["template_version_facts"]["sha256"] != template["version_facts"]["sha256"]:
        raise replay.ReplayError("候选原版本事实模板摘要漂移")
    active_origin = source.read(active_root / "projection-origin.json")
    if (set(active_origin) != {"fixture_only", "purpose", "baseline_version", "target_version", "source_version",
            "formal_version_semantics_certified", "originals", "projected", "model_scope", "method"}
            or active_origin["fixture_only"] is not True or active_origin["purpose"] != "control_input_projection"
            or active_origin["baseline_version"] != full.vc1.BASELINE
            or active_origin["target_version"] != source.VERSION or active_origin["source_version"] != source.VERSION
            or active_origin["formal_version_semantics_certified"] is not False
            or set(active_origin["originals"]) != {"profile", "wire", "catalog", "projection_policy"}):
        raise replay.ReplayError("候选原 Active 输入来源说明不闭合")
    for name in ("profile", "wire", "catalog"):
        source.bound(active_origin["originals"][name])
    from tools.official_client_capture import claude_upgrade_release as release
    if active_origin["originals"]["projection_policy"] != release.active_projection_policy(source.VERSION)[1]:
        raise replay.ReplayError("候选原 Active 策略绑定摘要漂移")
    active_names = {"active_release_catalog": "active-release-catalog.json", "active_profile": "active-profile.json",
        "active_wire": "active-wire.json", "active_projection_policy": "active-projection-policy.json",
        "review_projection_policy": "review-projection-policy.json", "static_baseline_anchors": "static-baseline-anchors.json",
        "static_baseline_reachability": "static-baseline-reachability.json"}
    if set(active_origin["projected"]) != set(active_names):
        raise replay.ReplayError("候选原 Active 投影分母不闭合")
    for name, filename in active_names.items():
        if source.bound(active_origin["projected"][name], root=data) != active_root / filename:
            raise replay.ReplayError("候选原 Active 投影坐标错位")
    aggregation_path = candidate_root / "aggregation.json"
    aggregation = source.read(aggregation_path)
    if aggregation["baseline_scenarios"] != records["scenarios"] or aggregation["original_labels"] != records["labels"]:
        raise replay.ReplayError("候选聚合输入不属于原基础输入")
    for name, filename in (("original_scenarios", "original-scenarios.json"),
                           ("grouped_scenarios", "grouped-scenarios.json"), ("grouped_labels", "grouped-labels.json")):
        if source.bound(aggregation[name], root=data) != candidate_root / filename:
            raise replay.ReplayError("候选原聚合输入坐标错位")
    control_path = candidate_root / "control.json"
    expected_control = {"schema_version": source.RECOVERY_CONTROL_SCHEMA, "fixture_only": True,
        "purpose": "regression_fixture", "control_root": str(candidate_root), "allowed_failure_probe_ids": [REFRESH_PROBE]}
    if source.read(control_path) != expected_control or source.read(candidate_root / "failures.json") != {}:
        raise replay.ReplayError("候选原初始化故障控制不是准备前原件")
    bindings = {**records, **active_origin["projected"], "full_scenarios": records["scenarios"],
        "active_projection_origin": source.binding(active_root / "projection-origin.json"),
        "candidate_aggregation": source.binding(aggregation_path), "scenarios": aggregation["grouped_scenarios"],
        "labels": aggregation["grouped_labels"]}
    current = source.read(source.bound(bindings["scenarios"], root=data))
    configured = {"bindings": bindings,
        "candidate_configuration": {"service_container": "sub2apiplus", "keeper_container": "sub2apiplus-keeper",
                                    "postgres_container": "sub2apiplus-postgres", "claude_account_id": "7", "api_key_id": "9"},
        "jobs": {job["id"]: [step["probe_id"] for step in job["steps"]] for job in current["capture_jobs"] if job["phase"] == "official"},
        "candidate_jobs": {job["id"]: [step["probe_id"] for step in job["steps"]] for job in current["capture_jobs"] if job["phase"] == "candidate"},
        "repository_bindings": {"fixture-only/baseline.json": records["baseline"], "fixture-only/rules.json": records["rules"],
                                facts_relative: records["version_facts"]},
        "full_control_inputs": True, "source_selections": [copy.deepcopy(full.FULL_SOURCE_SELECTION)],
        "candidate_recovery_control": source.binding(control_path)}
    validate_aggregation({"data_root": str(data), "inputs": configured})
    full.validate_frozen_inputs(data, configured)
    source.recovery_control({"data_root": str(data), "inputs": configured})
    return configured


def _legacy_partial_initialization(arguments: argparse.Namespace) -> dict[str, Any]:
    """旧缺源失败只导入原输入完成的窄停点；已有快照、准备动作或其它失败均不授予许可。"""

    data = Path(arguments.data).resolve(strict=True)
    world = stops.load_state("candidate-recorded-world")
    entry = stops.load_state("candidate-entry:" + arguments.scenario)
    expected = {"data": data, "scenario": arguments.scenario, "recorded": arguments.recorded, "freeze": arguments.freeze}
    if (not os.environ.get(stops.METHOD_RESUME_ENV) or world is None or world["data"] != data
            or world["root"] / "staging/data" != data or world["root"].is_symlink() or not world["root"].is_dir()
            or entry is None or any(entry[key] != value for key, value in expected.items())
            or len(entry["segments"]) != 1 or stops.load_state("candidate-namespace") is not None
            or stops.load_state("candidate-prepare") is not None):
        raise replay.ReplayError("候选原初始化缺唯一缺源失败原件，禁止重建")
    segment = entry["segments"][0]
    argv = segment["argv"]
    prefix = ["unshare", "-m", "-n", sys.executable, "-B", "-m", ACTIVATION_MODULE,
              "--data", str(data), "--recorded", arguments.recorded, "--freeze", arguments.freeze,
              "--scenario", arguments.scenario, "--parent-namespaces"]
    if len(argv) != len(prefix) + 1 or argv[:-1] != prefix:
        raise replay.ReplayError("候选原初始化 argv 与原坐标不一致")
    namespaces = json.loads(argv[-1])
    if set(namespaces) != {"mnt", "net"} or any(not isinstance(value, str) or not value for value in namespaces.values()):
        raise replay.ReplayError("候选原初始化命名空间来源不闭合")
    code, result, stderr = stops._original_command(segment, argv)
    missing = Path(arguments.recorded).parent / Path(full.FULL_SOURCE_SELECTION["attempt"]).parent
    error = "录制材料不存在或不可读：" + str(missing)
    if (code != 1 or result != {"status": "unverified", "fixture_only": True, "registered_chains": [], "error": error}
            or not all(marker in stderr for marker in ("replay.build_snapshot", "_source_plan", error))):
        raise replay.ReplayError("候选原初始化不是已核实的快照前缺源失败")
    expected_roots = {"runs", "evidence", "control", "tools"}
    expected_control = {"fixture-inputs", "fixture-active-projection", "candidate-recovery"}
    input_members = {
        "fixture-inputs": {"baseline.json", "rules.json", "scenarios.json", "labels.json", "target-inventory.json",
                           "sink-containment.json", "dispositions.json", "projection-origin.json"},
        "fixture-active-projection": {"active-profile.json", "active-wire.json", "active-release-catalog.json",
            "active-projection-policy.json", "review-projection-policy.json", "static-baseline-anchors.json",
            "static-baseline-reachability.json", "projection-origin.json"},
        "candidate-recovery": {"original-scenarios.json", "grouped-scenarios.json", "grouped-labels.json",
                               "aggregation.json", "failures.json", "control.json"}}
    if ({path.name for path in data.iterdir()} != expected_roots
            or {path.name for path in (data / "control").iterdir()} != expected_control
            or {path.name for path in (data / "evidence").iterdir()} != {"campaigns"}
            or any({path.name for path in (data / "control" / name).iterdir()} != members
                   for name, members in input_members.items())
            or any((data / "runs").iterdir()) or any((data / "evidence/campaigns").iterdir())):
        raise replay.ReplayError("候选原初始化已有半快照、冻结或形成动作，禁止覆盖")
    for name in (*expected_roots, "evidence/campaigns", *("control/" + value for value in expected_control)):
        replay._regular(data / name, data, directory=True)
    _verify_partial_tools(data)
    return _restore_partial_inputs(data)


def _verify_partial_tools(data: Path) -> None:
    """快照前续作只沿原产品工具副本；测试修复及其生成夹具另由原执行器与输入绑定核验。"""

    def product_files(root: Path) -> dict[str, str]:
        files = {}
        for directory, folders, names in os.walk(root, followlinks=False):
            folders[:] = sorted(name for name in folders if name not in {"tests", "__pycache__"})
            for path in (*(Path(directory) / name for name in folders), *(Path(directory) / name for name in names)):
                if path.is_symlink():
                    raise replay.ReplayError("候选原初始化工具副本含符号链接")
                if path.is_file() and path.suffix != ".pyc":
                    files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    if product_files(data / "tools") != product_files(full.vc1.REPOSITORY / "tools"):
        raise replay.ReplayError("候选原初始化非测试工具副本与原执行产品不一致")


def _verify_initialization_inputs(data: Path, configured: dict[str, Any]) -> dict[str, Any]:
    """后续每次续作复核原输入引用及来源说明；不把当前文件重新绑定为旧输入。"""

    _verify_partial_tools(data)
    if _restore_partial_inputs(data) != configured:
        raise replay.ReplayError("候选原初始化 inputs 与原件不一致")
    return _partial_input_origins(data)


def validate_aggregation(state: dict[str, Any]) -> dict[str, Any]:
    """逐次复算映射和来源；丢步骤、重复归属、规则遗漏或冻结后改字节均失败关闭。"""

    data = Path(state["data_root"])
    configured = state["inputs"]
    aggregation = source.read(source.bound(configured["bindings"]["candidate_aggregation"], root=data))
    if (aggregation.get("schema_version") != AGGREGATION_SCHEMA or aggregation.get("fixture_only") is not True
            or aggregation.get("purpose") != "regression_fixture" or aggregation.get("formal_version_semantics_certified") is not False):
        raise replay.ReplayError("B 组聚合合同身份非法")
    baseline = source.read(source.bound(aggregation["baseline_scenarios"], root=data))
    template = source.read(source.bound(aggregation["refresh_template"]))
    original = source.read(source.bound(aggregation["original_scenarios"], root=data))
    if original != _extend_refresh(baseline, template):
        raise replay.ReplayError("聚合原步骤与完整来源及刷新声明不闭合")
    expected, mapping = aggregate_scenarios(original)
    actual = source.read(source.bound(aggregation["grouped_scenarios"], root=data))
    if actual != expected or aggregation["step_mapping"] != mapping:
        raise replay.ReplayError("候选聚合步骤、规则或映射有遗漏、重复或漂移")
    if (configured["bindings"]["scenarios"] != aggregation["grouped_scenarios"]
            or configured["bindings"]["labels"] != aggregation["grouped_labels"]
            or configured["bindings"]["rules"] != aggregation["rules"]):
        raise replay.ReplayError("聚合绑定不是本次冻结输入")
    original_labels = source.read(source.bound(aggregation["original_labels"], root=data))
    label_template = source.read(source.bound(aggregation["label_template"]))
    label_path = source.bound(aggregation["grouped_labels"], root=data)
    if source.read(label_path) != _aggregate_labels(original_labels, label_template):
        raise replay.ReplayError("候选聚合标签未完整保留原来源")
    plan.load_evidence_labels(label_path, expected_version=source.VERSION, scenario_payload=actual)
    rules = source.read(source.bound(aggregation["rules"], root=data))["required_rules"]
    jobs = [argparse.Namespace(**{**job, "job_id": job["id"]}) for job in actual["capture_jobs"]]
    if len(rules) != 40:
        raise replay.ReplayError("B 组聚合不能裁剪完整40规则")
    plan.validate_full_coverage(jobs, rules)
    for phase, name in (("official", "jobs"), ("candidate", "candidate_jobs")):
        wanted = {job["id"]: [step["probe_id"] for step in job["steps"]] for job in actual["capture_jobs"] if job["phase"] == phase}
        if configured[name] != wanted or (phase == "official" and state.get("jobs", wanted) != wanted):
            raise replay.ReplayError("聚合作业白名单与冻结场景不符")
    source.recovery_control(state)
    return aggregation


@contextlib.contextmanager
def installed(state_path: Path, digest: str, *, read_only: bool = False) -> Iterator[Any]:
    state = source.load_state(state_path, digest)
    validate_aggregation(state)
    token_path = fixture_admin_token(Path(state['data_root']), create=not read_only)
    with mock.patch.dict(os.environ, {'ADMIN_BEARER_TOKEN': '', 'ADMIN_BEARER_TOKEN_FILE': str(token_path)}), \
            full.installed(state_path, digest) as adapter:
        yield adapter


def fixture_admin_token(data: Path, *, create: bool = True) -> Path:
    """私有录制夹具一次性写假 token；既有字节、权限或路径不符时拒绝，绝不覆盖。"""

    control = data / 'control'
    replay._regular(control, data, directory=True)
    path = control / 'recorded-candidate-admin-token.txt'
    if not path.exists() and not path.is_symlink():
        if not create:
            raise replay.ReplayError('成功候选重验缺少原假 token，禁止补写')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        try:
            os.write(descriptor, FIXTURE_ADMIN_TOKEN)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    replay._regular(path, control)
    if path.stat().st_mode & 0o777 != 0o400 or path.read_bytes() != FIXTURE_ADMIN_TOKEN:
        raise replay.ReplayError('录制候选假 token 原字节或只读权限已改变，禁止覆盖')
    return path


def token_preflight_retry_proof(case: Any, cursor_name: str, argv: list[str], campaign: Path,
                               saved: dict[str, Any], result: tuple[int, Any, str]) -> dict[str, Any] | None:
    """原两个 candidate-run 固定前检的一次续作许可；不是正式认证或通用失败重派。"""

    if result != (1, None, TOKEN_PREFLIGHT_ERROR):
        return None
    expected_method = {'candidate-reconcile': SCENARIO_METHODS['reconcile-resume'],
                       'candidate-evolution': SCENARIO_METHODS['tool-evolution']}.get(cursor_name)
    arguments = ['capture-candidate', 'run', '--campaign-dir', str(campaign),
                 '--candidate-id', CANDIDATE, '--acknowledge-live-requests']
    if (expected_method is None or case._testMethodName != expected_method or argv != arguments
            or saved.get('status') != 'complete' or saved.get('campaign') != campaign
            or stops.read_cut(saved['command']['stdout']) != b''):
        raise replay.ReplayError('候选固定前检缺原方法或原命令，禁止重派')
    world = stops.load_state('candidate-recorded-world')
    start = stops.load_state('candidate-vc5-start')
    if (world is None or world.get('data') != case.data
            or not isinstance(world.get('root'), Path) or world['root'] / 'staging/data' != case.data
            or case.root != case.data.parent or start is None
            or start.get('status') != 'complete' or start.get('campaign') != campaign
            or start.get('state_binding') != source.binding(case.state_path)
            or start.get('ready_binding') != json.loads(os.environ[PREPARED_ENV])):
        raise replay.ReplayError('候选固定前检未绑定原 VC-4 起点与世界')
    state = source.load_state(case.state_path, case.state_digest)
    if Path(state['data_root']) != case.data:
        raise replay.ReplayError('候选固定前检的录制来源不属于原世界')
    ready_path = source.bound(start['ready_binding'], root=case.data / 'control')
    ready = source.read(ready_path)
    if (ready.get('schema_version') != PREPARED_SCHEMA or ready.get('fixture_only') is not True
            or ready.get('purpose') != 'regression_fixture' or ready.get('candidate_id') != CANDIDATE
            or ready.get('candidate_revision') != 1 or ready.get('formal_version_semantics_certified') is not False
            or ready.get('state') != source.binding(case.state_path)):
        raise replay.ReplayError('候选固定前检不是原已形成的回归 VC-4 起点')
    expected_fault = {REFRESH_PROBE: 1 if cursor_name == 'candidate-reconcile' else 0}
    from tools.official_client_control.store import ControlStore
    from tools.official_client_control.contracts import RECEIPT_KINDS
    from tools.official_client_capture import claude_upgrade_store
    store = ControlStore.open_read_only(claude_upgrade_store.campaign_store_root(campaign))
    manifest = claude_upgrade.load_campaign_manifest(campaign)
    if 'preflight_retry' in saved:
        # 子动作之后允许原世界正常前进；授权时冻结的原件、r1事实和固定失败仍逐份重验。
        # 不重新作“无attempt”判定，不签第二次许可，也不将旧失败改成成功。
        proof = saved['preflight_retry'].get('proof', {})
        if (proof.get('kind') != 'candidate-admin-token-preflight' or proof.get('method') != expected_method
                or proof.get('logical_name') != 'command-step:' + cursor_name + ':candidate-run'
                or proof.get('campaign') != campaign or proof.get('original_command') != saved['command']
                or proof.get('source_binding') != source.binding(case.state_path)
                or proof.get('ready_binding') != start['ready_binding'] or proof.get('fault_input') != expected_fault
                or proof.get('fixture_only') is not True or proof.get('formal_version_semantics_certified') is not False):
            raise replay.ReplayError('候选原预约前许可不属于原方法／来源／失败')
        for binding in proof['bindings']:
            stops.read_cut(binding)
        facts = [store.load_fact(reference) for reference in proof['store_facts']]
        build = source.read(source.bound(ready['build_receipt'], root=campaign))
        if preflight_store_fact_refs(store, facts, build) != proof['store_facts']:
            raise replay.ReplayError('候选原预约前许可的r1准备事实未闭合')
        return proof
    cursor = stops.load_state('cursor:' + cursor_name)
    if (cursor is None or cursor.get('phase') != 0 or cursor.get('campaign') != campaign
            or cursor.get('completed_commands') != [] or cursor.get('capture_input_started') is not True):
        raise replay.ReplayError('候选固定前检缺首次采集游标，禁止重派')
    # command_step 在调用本专用证明前已跑真实 verify_case：Store外部闭合、VC、项目账与时间账。
    # 这里再收窄到“尚无候选采集副作用”，不能把任意 code1/resultNone 当作无副作用。
    candidate_root = campaign / 'candidates' / CANDIDATE
    replay._regular(candidate_root, campaign, directory=True)
    attempts = candidate_root / 'attempts'
    if attempts.is_symlink() or attempts.exists():
        raise replay.ReplayError('候选已有 attempt／预约目录，禁止预约前重派')
    if any(path.name.startswith('.reservation-') or path.name == 'reservation.json'
           for path in candidate_root.rglob('*')):
        raise replay.ReplayError('候选已有预约原件或半产物，禁止重派')
    configured = source.recovery_control(state)
    if configured is None:
        raise replay.ReplayError('候选固定前检缺原故障控制输入')
    control = Path(configured[1]['control_root'])
    if source.read(control / 'failures.json') != expected_fault:
        raise replay.ReplayError('候选固定前检的原故障输入已改变')
    for relative, empty in (('calls.jsonl', b''), ('counts.json', {})):
        path = control / relative
        if path.exists() or path.is_symlink():
            replay._regular(path, control)
            value = path.read_bytes() if isinstance(empty, bytes) else source.read(path)
            if value != empty:
                raise replay.ReplayError('候选已有复制动作或计数，禁止预约前重派')
    events = control / 'events'
    if events.is_symlink() or (events.exists() and (not events.is_dir() or any(events.iterdir()))):
        raise replay.ReplayError('候选已有复制事件或未知原件，禁止预约前重派')
    time_rows = timing._load_events(case.ledger_dir)
    last = time_rows[-1][0] if time_rows else {}
    if (last.get('event_id') != 'recorded-candidate-recovery-vc5-start'
            or last.get('event_type') != 'stage_started' or last.get('phase') != 'VC-5'
            or last.get('attempt_id') is not None or last.get('live_request_count') != 0
            or sum(row[0]['event_id'] == last.get('event_id') for row in time_rows) != 1
            or stops.read_cut(start['event_binding']) != time_rows[-1][1]):
        raise replay.ReplayError('候选时间账已超出原 VC-5 开始事件，禁止预约前重派')
    history = project.read_project_history_snapshot(case.project_root)
    if (history['head_sequence'] != 2 or len(history['events']) != 2
            or any(row['event_type'] != 'campaign_registered' for row in history['events'])):
        raise replay.ReplayError('候选项目账已有登记以外的计账／动作，禁止预约前重派')
    facts = store.list_facts(manifest['campaign_id'])
    from tools.official_client_capture import claude_upgrade_compare
    build, _build_binding = claude_upgrade_compare.load_candidate_build_receipt(campaign, manifest, CANDIDATE, 1)
    fact_refs = preflight_store_fact_refs(store, facts, build)
    for kind in ('candidate_evidence_package', 'candidate_delivery_plan', 'candidate_delivery_package', 'validation_execution_plan'):
        if store.list_object_refs(kind):
            raise replay.ReplayError('候选 Store 已有运行对象，禁止预约前重派')
    if any(store.list_receipt_refs(kind) for kind in RECEIPT_KINDS if kind != 'candidate_build'):
        raise replay.ReplayError('候选 Store 已有运行阶段收据，禁止预约前重派')
    build_ref = build['store']['candidate_build_receipt_ref']
    if store.list_receipt_refs('candidate_build') != [build_ref]:
        raise replay.ReplayError('候选 Store 不仅有原r1构建收据，禁止预约前重派')
    token_path = fixture_admin_token(case.data)
    if (os.environ.get('ADMIN_BEARER_TOKEN') or os.environ.get('ADMIN_BEARER_TOKEN_FILE') != str(token_path)):
        raise replay.ReplayError('候选续作未处于准确假 token 隔离作用域')
    bindings = [stops.file_cut(path) for path in (
        case.state_path, ready_path, campaign / 'campaign.json',
        source.bound(ready['build_receipt'], root=campaign), source.bound(ready['checkpoint'], root=campaign),
        case.ledger_dir / 'ledger.json', case.project_root / 'plan.json',
        case.project_root / 'events/000001.json', case.project_root / 'events/000002.json', token_path)]
    bindings.extend(stops.file_cut(store.root / 'facts' / reference['campaign_id'] / reference['dimension'] /
        f"{reference['sequence']:08d}-{reference['sha256']}.json") for reference in fact_refs)
    bindings.append(stops.file_cut(store.receipt_path(build_ref)))
    return {'kind': 'candidate-admin-token-preflight', 'logical_name': 'command-step:' + cursor_name + ':candidate-run',
        'campaign': campaign, 'original_command': saved['command'], 'method': expected_method,
        'source_binding': source.binding(case.state_path), 'ready_binding': start['ready_binding'],
        'bindings': bindings + [start['event_binding']], 'project_head': history,
        'time_sequence': last['sequence'], 'store_facts': fact_refs,
        'fault_input': expected_fault, 'fixture_only': True, 'formal_version_semantics_certified': False}


def preflight_store_fact_refs(store: Any, facts: list[dict[str, Any]], build: dict[str, Any]) -> list[dict[str, Any]]:
    """预约前原七条准备事实：明确r1先登记再冻结，并与原VC-4收据逐项闭合。"""

    expected = [('discovery', 1, 'discovery_recorded'),
        ('evidence', 1, 'evidence_recorded'), ('evidence', 2, 'rule_classification_recorded'),
        ('approval', 1, 'evidence_approved'), ('approval', 2, 'profile_approved'),
        ('validation', 1, 'candidate_revision_recorded'), ('validation', 2, 'candidate_frozen')]
    if [(fact['dimension'], fact['sequence'], fact['fact_kind']) for fact in facts] != expected:
        raise replay.ReplayError('候选 Store 不是原七条准备事实或顺序已变，禁止预约前重派')
    revision, frozen = facts[-2:]
    refs = [store.fact_ref(fact) for fact in facts]
    formed = {'tool_identity_ref': None, 'candidate_revision': 1, 'evaluation_baseline': None}
    if (any(reference['campaign_id'] != build['campaign_id'] for reference in refs)
            or revision['payload'] != {'revision': 1, 'candidate_id': CANDIDATE, 'supersedes': None, 'invalidation_ref': None}
            or revision.get('formed_under') != formed or frozen.get('formed_under') != formed
            or frozen.get('previous_fact_sha256') != refs[-2]['sha256']
            or build.get('candidate_id') != CANDIDATE or build.get('candidate_revision') != 1
            or refs[-1] != build['store']['candidate_frozen_ref']
            or frozen['payload'].get('candidate_id') != CANDIDATE
            or frozen['payload'].get('profile_approval_ref') != refs[4]
            or refs[4] != build['store']['profile_approval_ref']
            or frozen['payload'].get('candidate_build_receipt_ref') != build['store']['candidate_build_receipt_ref']):
        raise replay.ReplayError('候选原r1登记／冻结／构建收据引用未闭合，禁止预约前重派')
    return refs


def activate() -> None:
    """受监督子动作承接同一聚合与来源合同，安装失败不得回落到真实客户端。"""

    full.vc1.activate(installer=installed)


def _verify_original_prepare_world(world: dict[str, Any]) -> None:
    """准备交接只重读原 Store、两账、checkpoint 与来源，不创建新的成功凭证。"""

    data, campaign, manifest = world['data'], world['campaign'], world['manifest']
    stops.verify_case(SimpleNamespace(
        ledger_dir=claude_upgrade._campaign_timing_ledger_dir(campaign, manifest),
        project_root=project.find_project_ledger(campaign, client='claude')), campaign)
    state = world['state']
    if Path(state['data_root']) != data:
        raise replay.ReplayError('候选准备的原世界与来源状态不一致')
    replay.assert_source_untouched(state['snapshot'])
    for path, expected in state['freeze']['source_sentinel'].items():
        if replay._stat(Path(path)) != expected:
            raise replay.ReplayError('候选准备的原冻结来源元数据变化')


def _resume_vc1_prepare(state_path: Path, digest: str, data: Path) -> dict[str, Any]:
    """只承接原形成方法完整通过后的报告；只有 continuation 的半成品不算完成。"""

    report_path = data / 'control/recorded-chain-result.json'
    continuation_path = data / 'control/recorded-vc1-continuation.json'
    if not report_path.is_file() or not continuation_path.is_file():
        raise replay.ReplayError('候选原准备停在 vc1-started，内部形成动作未闭合；禁止整体重跑或重建')
    report = source.read(source.bound(source.binding(report_path), root=data))
    continuation = source.binding(continuation_path)
    budget = certification.claude_recorded_time_budget('vc-chain.claude-vc1-capture')
    seconds = diagnostic_budget.diagnostic_seconds()
    if (report.get('schema_version') != 'claude-recorded-vc0-vc1-chain/v1'
            or report.get('fixture_only') is not True or report.get('status') != 'passed'
            or report.get('scope') != 'VC-0→VC-1' or report.get('target_version') != source.VERSION
            or report.get('continuation') != continuation
            or report.get('registered_chains') != ['vc-chain.claude-vc1-capture']
            or report.get('full_vc0_vc6_certified') is not False
            or report.get('network_requests_this_run') != 0 or report.get('time_budget') != budget
            or report.get('diagnostic_only') != (seconds is not None)
            or not isinstance(report.get('elapsed_seconds'), (int, float))
            or not 0 <= report['elapsed_seconds'] <= (seconds or budget['method_timeout_seconds'])):
        raise replay.ReplayError('候选原 VC-1 完整报告未与固定来源、模式及预算闭合')
    world = full.vc1.load_continuation(continuation_path, continuation['sha256'])
    if (world['state_path'] != state_path or world['state_digest'] != digest or world['data'] != data
            or report.get('campaign') != str(world['campaign'])
            or report.get('store_campaign_id') != world['manifest']['campaign_id']
            or report.get('checkpoint') != world['document']['checkpoints']['VC-1']):
        raise replay.ReplayError('候选原 VC-1 报告与 continuation 不属于同一原世界')
    _verify_original_prepare_world(world)
    return continuation


def _resume_classified_prepare(world: dict[str, Any]) -> dict[str, Any]:
    """VC-2 与 VC-3 已全部落定时，只重新消费原阶段结果与已批准准备包。"""

    from tools.official_client_capture import claude_upgrade_profile as profile

    data, campaign, manifest = world['data'], world['campaign'], world['manifest']
    paths = [campaign / 'classification/result.json', campaign / profile.STAGE_RESULT_RELATIVE]
    if not all(path.is_file() and not path.is_symlink() for path in paths):
        raise replay.ReplayError('候选原准备停在 classify-started，内部形成动作未闭合；禁止整函数重跑')
    classification = claude_upgrade._load_stage_result(campaign, 'classify')
    stage = claude_upgrade._load_stage_result(campaign, 'stage-profile')
    if classification['affected_rules'] or len(classification['inherited_rules']) != 40:
        raise replay.ReplayError('候选原分类没有完整 40 规则继承分母')
    prepared = profile.load_prepare_result(campaign, manifest, stage['prepared_revision'])
    projection = json.loads(profile._prepared_files(campaign, prepared)[profile.PREPARE_FILES['projection']])
    if projection != full.validate_frozen_inputs(data, world['state']['inputs']):
        raise replay.ReplayError('候选原批准投影与 VC-0 前冻结输入不一致')
    claude_upgrade._replay_vc_checkpoint(campaign, claude_upgrade._vc_campaign_plan(campaign, manifest), 'VC-3')
    _verify_original_prepare_world(world)
    return {'classification': source.binding(paths[0]), 'profile': source.binding(paths[1]), 'profile_store': stage['store']}


def _resume_built_prepare(world: dict[str, Any]) -> dict[str, Any]:
    """只绑定原 r1 构建、VC-4 与最后的运行输入；缺最后输入时不补造或重新构建。"""

    from tools.official_client_capture import claude_upgrade_profile as profile
    from tools.official_client_capture import claude_upgrade_compare as compare

    data, campaign, manifest = world['data'], world['campaign'], world['manifest']
    receipt = campaign / 'candidates' / CANDIDATE / 'build-receipt.json'
    runtime = data / 'control/full-runtime-source.json'
    if not all(path.is_file() and not path.is_symlink() for path in (receipt, runtime)):
        raise replay.ReplayError('候选原准备停在 build-started，内部形成动作未闭合；禁止补造输入或重新构建')
    if claude_upgrade._current_candidate_revision(campaign, manifest) != 1:
        raise replay.ReplayError('候选原准备构建不再属于原 r1')
    compare.load_candidate_build_receipt(campaign, manifest, CANDIDATE, 1)
    checkpoint, _payload = claude_upgrade._replay_vc_checkpoint(campaign,
        claude_upgrade._vc_campaign_plan(campaign, manifest), 'VC-4', revision=1)
    expected = {'fixture_only': True, 'purpose': 'recorded_control_runtime_source',
        'build_receipt': source.binding(receipt),
        'profile_stage': source.binding(campaign / profile.STAGE_RESULT_RELATIVE),
        'candidate_configuration': manifest['configuration'], 'formal_version_semantics_certified': False}
    if source.read(source.bound(source.binding(runtime), root=data)) != expected:
        raise replay.ReplayError('候选原运行输入与 r1 构建及画像不同源')
    _verify_original_prepare_world(world)
    return {'revision': 1, 'candidate_id': CANDIDATE, 'build_receipt': expected['build_receipt'],
        'checkpoint': source.binding(checkpoint)}


def prepare(state_path: Path, digest: str) -> Path:
    """原形成链的明确交接停点；恢复实读已有原件，不重新形成 VC-0→VC-4。"""

    state = source.load_state(state_path, digest)
    validate_aggregation(state)
    expected = {'state': source.binding(state_path), 'data': Path(state['data_root'])}
    saved = stops.load_state('candidate-prepare')
    if saved is not None:
        if not os.environ.get(stops.METHOD_RESUME_ENV) or any(saved[key] != value for key, value in expected.items()):
            raise replay.ReplayError('候选原准备停点不是本次来源或恢复入口')
        if saved['phase'] == 'vc1-started':
            with installed(state_path, digest):
                continuation = _resume_vc1_prepare(state_path, digest, expected['data'])
            saved.update(phase='vc1-complete', continuation=continuation)
            stops.save_state('candidate-prepare', saved)
        if saved['phase'] == 'prepared':
            path = source.bound(saved['prepared'], root=expected['data'])
            prepared_world(state_path, digest, source.binding(path))
            return path
    else:
        if os.environ.get(stops.METHOD_RESUME_ENV):
            namespace = stops.load_state('candidate-namespace')
            if (namespace is None or namespace.get('prepare_started') is not False
                    or namespace['state_binding'] != expected['state'] or namespace['data'] != expected['data']):
                raise replay.ReplayError('候选原准备缺明确停点，禁止重新形成 Campaign')
        namespace = stops.load_state('candidate-namespace')
        if namespace is not None:
            if namespace.get('prepare_started') is not False:
                raise replay.ReplayError('候选原首次准备许可已使用但缺准备停点，禁止重派发')
            # 首次准备许可只用一次；任何 Campaign 形成副作用之前先落已开始标记。
            stops.save_state('candidate-namespace', {**namespace, 'prepare_started': True})
        saved = {**expected, 'phase': 'vc1-started'}
        stops.save_state('candidate-prepare', saved)
        first = full.vc1.run_chain(state_path, digest, installer=installed)
        saved.update(phase='vc1-complete', continuation=first['continuation'])
        stops.save_state('candidate-prepare', saved)
    continuation = saved['continuation']
    with installed(state_path, digest):
        world = full.vc1.load_continuation(source.bound(continuation), continuation["sha256"])
        data, campaign = world["data"], world["campaign"]
        if saved['phase'] == 'classify-started':
            saved.update(phase='classified', classified=_resume_classified_prepare(world))
            stops.save_state('candidate-prepare', saved)
        if saved['phase'] == 'build-started':
            saved.update(phase='built', built=_resume_built_prepare(world))
            stops.save_state('candidate-prepare', saved)
        if saved['phase'] == 'vc1-complete':
            saved['phase'] = 'classify-started'
            stops.save_state('candidate-prepare', saved)
            classified = full.classify_and_prepare(data, campaign)
            saved.update(phase='classified', classified=classified)
            stops.save_state('candidate-prepare', saved)
        for key in ('classification', 'profile'):
            source.bound(saved['classified'][key], root=campaign)
        claude_upgrade._load_stage_result(campaign, 'classify')
        claude_upgrade._load_stage_result(campaign, 'stage-profile')
        if saved['phase'] == 'classified':
            saved['phase'] = 'build-started'
            stops.save_state('candidate-prepare', saved)
            built = full.build_candidate(data, campaign)
            saved.update(phase='built', built=built)
            stops.save_state('candidate-prepare', saved)
        built = saved['built']
        source.bound(built['build_receipt'], root=campaign)
        checkpoint, _ = claude_upgrade._replay_vc_checkpoint(campaign, claude_upgrade._vc_campaign_plan(campaign, world['manifest']), 'VC-4', revision=1)
        if source.bound(built['checkpoint'], root=campaign) != checkpoint:
            raise replay.ReplayError('候选原准备的 VC-4 checkpoint 漂移')
    payload = {
        "schema_version": PREPARED_SCHEMA, "fixture_only": True, "purpose": "regression_fixture", "scope": "VC-0→VC-4",
        "state": source.binding(state_path), "continuation": continuation, "build_receipt": built["build_receipt"],
        "checkpoint": built["checkpoint"], "candidate_id": CANDIDATE, "candidate_revision": 1,
        "runtime_source": source.binding(data / "control/full-runtime-source.json"),
        "formal_version_semantics_certified": False,
    }
    path = data / 'control/candidate-recovery-start.json'
    if path.exists():
        # 只承接全部原 reader 已闭合、最后一次性写入与局部停点之间的窄窗。
        if source.read(path) != payload:
            raise replay.ReplayError('候选原准备收据与真实形成链不一致，禁止覆盖')
    else:
        source.write(path, payload)
    prepared_world(state_path, digest, source.binding(path))
    saved.update(phase='prepared', prepared=source.binding(path))
    stops.save_state('candidate-prepare', saved)
    return path


def prepared_world(state_path: Path, digest: str, binding: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """实核原固定起点、来源、Store、VC-4 与构建观察；不创建任何产物。"""
    state = source.load_state(state_path, digest)
    data = Path(state['data_root'])
    ready_path = source.bound(binding, root=data / 'control')
    ready = source.read(ready_path)
    if (ready_path != data / 'control/candidate-recovery-start.json'
            or ready.get('schema_version') != PREPARED_SCHEMA or ready.get('fixture_only') is not True
            or ready.get('purpose') != 'regression_fixture' or ready.get('scope') != 'VC-0→VC-4'
            or ready.get('formal_version_semantics_certified') is not False
            or ready.get('state') != source.binding(state_path)
            or ready.get('candidate_id') != CANDIDATE or ready.get('candidate_revision') != 1):
        raise replay.ReplayError('候选恢复起点不是本世界原固定 VC-4')
    with installed(state_path, digest):
        continuation = ready['continuation']
        world = full.vc1.load_continuation(source.bound(continuation, root=data), continuation['sha256'])
        campaign, manifest = world['campaign'], world['manifest']
        checkpoint, _ = claude_upgrade._replay_vc_checkpoint(campaign, claude_upgrade._vc_campaign_plan(campaign, manifest), 'VC-4', revision=1)
        if source.bound(ready['checkpoint'], root=campaign) != checkpoint:
            raise replay.ReplayError('候选恢复起点原 checkpoint 漂移')
        runtime_path = source.bound(ready['runtime_source'], root=data)
        if runtime_path != data / 'control/full-runtime-source.json' or source.read(runtime_path)['build_receipt'] != ready['build_receipt']:
            raise replay.ReplayError('候选恢复起点原运行观察与构建不同源')
        source.bound(ready['build_receipt'], root=campaign)
    return ready, world


def original_enclosing_commands(case: Any) -> list[dict[str, Any]]:
    """只返回本候选方法原 namespace 的完整退出记录，供内层未决命令核对包含关系。"""

    scenario = next((name for name, method in SCENARIO_METHODS.items() if method == case._testMethodName), None)
    if scenario is None:
        raise replay.ReplayError('候选方法不属于原 namespace 的冻结场景集合')
    entry = stops.load_state('candidate-entry:' + scenario)
    if entry is None:
        return []
    namespace = stops.load_state('candidate-namespace')
    if (namespace is None or entry.get('scenario') != scenario or entry.get('data') != case.data
            or namespace.get('scenario') != scenario or namespace.get('data') != case.data
            or namespace.get('state_path') != case.state_path or namespace.get('digest') != case.state_digest
            or source.bound(namespace['state_binding']) != case.state_path):
        raise replay.ReplayError('外层 namespace 与原候选方法／来源坐标不一致')
    source.bound(namespace['site_binding'], root=case.data)
    state = source.load_state(case.state_path, case.state_digest)
    roots = [str(Path(entry['recorded']).resolve().parent), str(Path(entry['freeze']).resolve())]
    if state['data_root'] != str(case.data) or roots != namespace['roots'] or roots != state['readonly_sources']:
        raise replay.ReplayError('外层 namespace 的录制／冻结根与原来源状态不同源')
    expected = ['unshare', '-m', '-n', sys.executable, '-B', '-m', ACTIVATION_MODULE,
        '--data', str(case.data), '--recorded', entry['recorded'], '--freeze', entry['freeze'],
        '--scenario', scenario, '--parent-namespaces']
    rows = entry['segments']
    for row in rows:
        argv = row['argv']
        if argv[:-1] != expected or len(argv) != len(expected) + 1:
            raise replay.ReplayError('外层追加观测不是本方法原 namespace 的准确命令')
        parents = json.loads(argv[-1])
        if set(parents) != {'mnt', 'net'} or any(not isinstance(value, str) or not value for value in parents.values()):
            raise replay.ReplayError('外层 namespace 的原父命名空间坐标不完整')
    return rows


class ExecutionToolCopy:
    """仅同步本世界执行副本的单个演进文件；实际 Store 仍在隔离源码树上现算身份。"""

    def __init__(self, data: Path) -> None:
        self.data, self.originals = data, {}
        saved = stops.load_state('candidate-execution-copy')
        if saved is not None:
            if not os.environ.get(stops.METHOD_RESUME_ENV) or saved['data'] != data:
                raise replay.ReplayError('执行副本原件只允许准确原世界恢复')
            for row in saved['changes']:
                path = row['path']
                if path.resolve() != path or not path.is_relative_to(data / 'tools') or not path.is_file():
                    raise replay.ReplayError('执行副本恢复坐标不可信')
                if path.read_bytes() not in (row['before'], row['after']):
                    raise replay.ReplayError('执行副本字节不是原件或已冻结的本次演进')
                self.originals[path] = row['before']
        else:
            if os.environ.get(stops.METHOD_RESUME_ENV):
                entry = stops.load_state('candidate-method-entry')
                if (entry is None or entry.get('status') != 'uninitialized'
                        or stops.load_state('candidate-vc5-start') is not None):
                    raise replay.ReplayError('执行副本原件清单缺失，禁止重建或追认旧演进')
            stops.save_state('candidate-execution-copy', {'data': data, 'changes': []})

    def write(self, key: str, content: bytes) -> None:
        path = self.data / "tools" / key
        if path.resolve() != path or not path.is_relative_to(self.data / "tools") or not path.is_file():
            raise replay.ReplayError("演进文件不在本世界执行副本")
        self.originals.setdefault(path, path.read_bytes())
        saved = stops.load_state('candidate-execution-copy') or {'data': self.data, 'changes': []}
        previous = next((row for row in saved['changes'] if row['path'] == path), None)
        if previous is not None and previous['after'] != content:
            raise replay.ReplayError('同一执行副本演进内容已改变，不能覆盖原停点')
        if previous is not None and path.read_bytes() not in (previous['before'], previous['after']):
            raise replay.ReplayError('执行副本当前字节漂移，禁止覆盖原现场')
        if previous is None:
            saved['changes'].append({'path': path, 'before': self.originals[path], 'after': content})
            stops.save_state('candidate-execution-copy', saved)
        path.write_bytes(content)

    def restore(self) -> None:
        for path, raw in self.originals.items():
            path.write_bytes(raw)
        self.originals.clear()


class ClaudeCandidateChainDriver(unittest.TestCase):
    """B 组只保留控制动作辅助方法；来源全部转至真实录制、封存与构建形成链。"""

    run_main = staticmethod(seal_tests.run_main)
    candidate_token_preflight_retry = token_preflight_retry_proof
    reconcile = recovery_tests.RecoveryChainCase.reconcile
    approve = recovery_tests.RecoveryChainCase.approve

    def setUp(self) -> None:
        if os.environ.get(source.STATE_ENV):
            self.state_path = Path(os.environ[source.STATE_ENV])
            self.state_digest = os.environ[source.DIGEST_ENV]
            state = source.load_state(self.state_path, self.state_digest)
            self.data = Path(state["data_root"])
            self.root = self.data.parent
            return
        super().setUp()
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        if os.environ.get(stops.METHOD_RESUME_ENV):
            saved = stops.load_state('candidate-recorded-world')
            if saved is None or saved['root'].is_symlink() or not saved['root'].is_dir():
                raise replay.ReplayError('候选 B 组缺少原世界停点，禁止重建')
            self.root, self.data = saved['root'], saved['data']
            return
        configured = os.environ.get("CLAUDE_RECORDED_CHAIN_RECORD_DIR") or os.environ.get("CLAUDE_FAULT_CHAIN_RECORDS")
        if configured:
            # 世界用原可写临时根；记录目录的父目录可能已被来源隔离挂成只读。
            parent = Path(tempfile.gettempdir()).resolve() / 'candidate-recorded-worlds'
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root = Path(tempfile.mkdtemp(prefix="candidate-b-", dir=parent)).resolve()
        else:
            directory = tempfile.TemporaryDirectory(prefix="claude-candidate-recorded-")
            self.addCleanup(directory.cleanup)
            self.root = Path(directory.name).resolve()
        self.data = self.root / "staging/data"
        stops.save_state('candidate-recorded-world', {'root': self.root, 'data': self.data})

    def recorded_entry(self, scenario: str) -> bool:
        """旧场景入口先建立独立的断网与只读来源世界；内部方法执行原有全部断言后才返回。"""

        if scenario not in SCENARIO_METHODS:
            raise replay.ReplayError("未知候选 B 组场景")
        if os.environ.get(source.STATE_ENV):
            return False
        if platform.system() != "Linux" or platform.machine() not in {"arm64", "aarch64"} or os.geteuid() != 0:
            raise replay.ReplayError("候选录制恢复链未验证：需要 Linux ARM64 root 与 mount/net namespace")
        recorded, frozen = os.environ.get("CLAUDE_2_1_226_RELAY_FIXTURE"), os.environ.get("CLAUDE_2_1_226_FREEZE_FIXTURE")
        if not recorded or not frozen:
            raise replay.ReplayError("候选 B 组需要显式历史 relay 与 freeze；不允许回落 fake_admission")
        entry_name = 'candidate-entry:' + scenario
        expected = {'data': self.data, 'scenario': scenario, 'recorded': recorded, 'freeze': frozen}
        previous = stops.load_state(entry_name)
        if previous is not None:
            if not os.environ.get(stops.METHOD_RESUME_ENV) or any(previous[key] != value for key, value in expected.items()):
                raise replay.ReplayError('候选原入口参数或原世界坐标已改变')
            if stops.load_state('candidate-namespace') is None and stops.load_state(SOURCE_INITIALIZATION) is None:
                _legacy_partial_initialization(argparse.Namespace(**{key: str(value) if isinstance(value, Path) else value
                    for key, value in expected.items()}))
            for segment in previous['segments']:
                for field in ('stdout', 'stderr', 'command_binding', 'invocation_binding'):
                    stops.read_cut(segment[field])
        else:
            previous = {**expected, 'segments': []}
            stops.save_state(entry_name, previous)
        pending = None
        command = ["unshare", "-m", "-n", sys.executable, "-B", "-m", ACTIVATION_MODULE,
            "--data", str(self.data), "--recorded", recorded, "--freeze", frozen, "--scenario", scenario,
            "--parent-namespaces", json.dumps({kind: os.readlink(f"/proc/self/ns/{kind}") for kind in ("mnt", "net")})]
        # 内层共用本方法的独立预算；留一分钟给外层收日志与清理，不能遗留旧 850 秒截断。
        method_seconds = (diagnostic_budget.diagnostic_seconds() or
                          certification.claude_fault_time_budget("vc-chain.claude-recovery-chain")["method_timeout_seconds"])
        pending = stops.begin_command(command)
        try:
            completed = subprocess.run(command, cwd=full.vc1.REPOSITORY, capture_output=True, text=True,
                                       timeout=method_seconds - 60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired as error:
            stops.record_command(command, None, error.stdout or b'', error.stderr or b'', pending=pending)
            if stops.record_root() is None:
                for name, raw in (("stdout.log", error.stdout or b""), ("stderr.log", error.stderr or b"")):
                    (self.root / name).write_bytes(raw.encode() if isinstance(raw, str) else raw)
            else:
                previous['segments'].append(stops.load_state('command-observations')['commands'][-1])
                stops.save_state(entry_name, previous)
            raise
        stops.record_command(command, completed.returncode, completed.stdout, completed.stderr, pending=pending)
        if stops.record_root() is None:
            (self.root / "stdout.log").write_text(completed.stdout, encoding="utf-8")
            (self.root / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        else:
            previous['segments'].append(stops.load_state('command-observations')['commands'][-1])
            stops.save_state(entry_name, previous)
        self.assertEqual(completed.returncode, 0, completed.stderr[-12000:] + completed.stdout[-12000:])
        result = source.read(self.data / "control/candidate-recovery-case-result.json")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scenario_method"], SCENARIO_METHODS[scenario])
        return True

    @contextlib.contextmanager
    def source_context(self) -> Iterator[None]:
        with installed(self.state_path, self.state_digest,
                       read_only=getattr(self, '_read_only_revalidation', False)):
            yield

    def chain_patched(self) -> contextlib.AbstractContextManager[None]:
        return self.source_context()

    def revalidate_completed_method(self, expected: list[str]) -> list[dict[str, Any]]:
        """成功方法重绑原只读来源后验世界；不重进形成链、发命令或补写夹具。"""

        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        specifications = {
            SCENARIO_METHODS['reconcile-resume']: ('reconcile-resume', 'candidate-reconcile', 9, '候选真实对账链端到端'),
            SCENARIO_METHODS['tool-evolution']: ('tool-evolution', 'candidate-evolution', 15, '有候选 attempt 的整条工具演进预览'),
        }
        specification = specifications.get(self._testMethodName)
        if (not os.environ.get(stops.METHOD_RESUME_ENV) or specification is None
                or expected != [specification[3]] or not stops.restore_case(self)):
            raise replay.ReplayError('完成候选方法重验只接受原方法与原单个场景')
        scenario, cursor, phase, label = specification
        namespace = stops.load_state('candidate-namespace')
        entry = stops.load_state('candidate-entry:' + scenario)
        start = stops.load_state('candidate-vc5-start')
        saved = stops.load_state('cursor:' + cursor)
        if (namespace is None or entry is None or start is None or saved is None
                or namespace.get('scenario') != scenario or entry.get('scenario') != scenario
                or namespace.get('data') != self.data or entry.get('data') != self.data
                or namespace.get('state_path') != self.state_path or namespace.get('digest') != self.state_digest
                or start.get('state_binding') != namespace.get('state_binding')
                or start.get('status') != 'complete' or start.get('campaign') != self.campaign
                or saved.get('phase') != phase or saved.get('campaign') != self.campaign):
            raise replay.ReplayError('完成候选方法缺同一原 namespace、世界或终态游标')
        if source.bound(namespace['state_binding']) != self.state_path:
            raise replay.ReplayError('完成候选方法的原来源状态发生变化')
        if source.bound(namespace['site_binding'], root=self.data) != self.data / 'control/site/sitecustomize.py':
            raise replay.ReplayError('完成候选方法的原 site 坐标发生变化')
        roots = [str(Path(entry['recorded']).resolve().parent), str(Path(entry['freeze']).resolve())]
        if roots != namespace['roots']:
            raise replay.ReplayError('完成候选方法的原只读来源根发生变化')
        state = source.load_state(self.state_path, self.state_digest, isolation=False)
        if state['data_root'] != str(self.data) or self.root != self.data.parent:
            raise replay.ReplayError('完成候选方法的原数据根发生变化')
        fixture_admin_token(self.data, create=False)
        _restore_read_only_mounts(self.state_path, self.state_digest, roots, state['parent_namespaces'])
        # verify_case 会再次进入本对象的来源作用域，故只读约束覆盖全部嵌套消费者。
        with mock.patch.object(self, '_read_only_revalidation', True, create=True), \
                self.source_context(), stops.verified_read_only_world(self, self.campaign):
            original_enclosing_commands(self)
            restored = stops.resume_checkpoint(self, cursor)
            if restored is None or restored.get('phase') != phase:
                raise replay.ReplayError('完成候选方法的原终态游标已改变')
            return [ScenarioAudit.revalidate(self, label)]

    def candidate_world(self) -> Path:
        """重绑原 VC-4 起点及原账本；VC-5 开始事件只能追加一次，恢复逐字重验。"""
        binding = json.loads(os.environ[PREPARED_ENV])
        ready, world = prepared_world(self.state_path, self.state_digest, binding)
        campaign, manifest = world['campaign'], world['manifest']
        with self.source_context():
            self.ledger_dir = claude_upgrade._campaign_timing_ledger_dir(campaign, manifest)
            self.project_root = project.find_project_ledger(campaign, client='claude')
            self.tool_identity = manifest['tool_identity']
            self.tool_copy = self.data / 'tools/official_client_capture'
            self.tree = ExecutionToolCopy(self.data)
            self.failures_file = self.data / 'control/candidate-recovery/failures.json'
            self.call_log = self.data / 'control/candidate-recovery/calls.jsonl'
            expected = {'campaign': campaign, 'ready_binding': binding, 'state_binding': source.binding(self.state_path)}
            saved = stops.load_state('candidate-vc5-start')
            if saved is not None:
                if not os.environ.get(stops.METHOD_RESUME_ENV) or any(saved[key] != value for key, value in expected.items()):
                    raise replay.ReplayError('候选原阶段停点与本次世界不一致')
                if saved['status'] == 'complete':
                    stops.read_cut(saved['event_binding'])
            else:
                head = timing.phase_ledger_state(self.ledger_dir)
                if head['active_phase'] is not None or head['completed_phases'][-1:] != ['VC-4']:
                    raise replay.ReplayError('候选原起点时间账不是已完成 VC-4 的原账本')
                saved = {**expected, 'status': 'pending'}
                stops.save_state('candidate-vc5-start', saved)
                timing.append_event(self.ledger_dir, event_id='recorded-candidate-recovery-vc5-start', phase='VC-5',
                    event_type='stage_started', next_action='真实录制候选恢复首批开始', receipts=[])
            rows = [(event, raw) for event, raw in timing._load_events(self.ledger_dir)
                    if event['event_id'] == 'recorded-candidate-recovery-vc5-start']
            if len(rows) != 1 or rows[0][0]['event_type'] != 'stage_started' or rows[0][0]['phase'] != 'VC-5':
                raise replay.ReplayError('候选原 VC-5 开始事件缺失或多义，禁止补写或重置阶段')
            paths = [path for path in (self.ledger_dir / 'events').glob('*.json') if path.read_bytes() == rows[0][1]]
            if len(paths) != 1:
                raise replay.ReplayError('候选原 VC-5 开始事件原件不唯一')
            if saved['status'] == 'pending':
                saved.update(status='complete', event_binding=stops.file_cut(paths[0]))
                stops.save_state('candidate-vc5-start', saved)
            elif saved['event_binding'] != stops.file_cut(paths[0]):
                raise replay.ReplayError('候选原 VC-5 停点未绑定唯一的原账本事件')
            self.campaign = campaign
            stops.save_case(self)
        self.assert_recorded_sources_untouched()
        return campaign

    def fail_probe(self, probe: str, times: int) -> None:
        state = source.load_state(self.state_path, self.state_digest)
        configured = source.recovery_control(state)
        if configured is None or probe not in configured[1]["allowed_failure_probe_ids"] or type(times) is not int or times < 0:
            raise replay.ReplayError("故障不在冻结的候选白名单")
        self.failures_file.write_text(json.dumps({probe: times}), encoding="utf-8")

    def relay_probe_calls(self) -> list[str]:
        if not self.call_log.exists():
            return []
        rows = [json.loads(line) for line in self.call_log.read_text(encoding="utf-8").splitlines() if line]
        for row in rows:
            decision = source.read(source.bound(row["decision"], root=self.data))
            if decision["operation"] != row["operation"] or decision["probe_id"] != row["probe_id"] or decision["copy_completed"] is not True:
                raise replay.ReplayError("候选调用日志与真实复制决策不符")
        return [row["probe_id"] for row in rows]

    def candidate_run(self, campaign: Path, *, fail_refresh: bool = False) -> dict[str, Any]:
        self.fail_probe(REFRESH_PROBE, 1 if fail_refresh else 0)
        with self.source_context(), mock.patch.object(claude_upgrade, "JOB_RETRY_LIMIT", 0):
            code, result, stderr = self.run_main(["capture-candidate", "run", "--campaign-dir", str(campaign),
                "--candidate-id", CANDIDATE, "--acknowledge-live-requests"])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "failed" if fail_refresh else "awaiting_receipts"), stderr)
        self.fail_probe(REFRESH_PROBE, 0)
        return result

    def candidate_reconcile(self, campaign: Path, attempt_id: str, *extra: str) -> dict[str, Any]:
        return self.reconcile(campaign, attempt_id, *extra)

    def candidate_approve(self, campaign: Path, attempt_id: str, preview: dict[str, Any]) -> dict[str, Any]:
        return self.approve(campaign, attempt_id, preview)

    def candidate_resume(self, campaign: Path, preview_path: str) -> dict[str, Any]:
        with self.source_context():
            code, result, stderr = self.run_main(["resume", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE,
                "--rerun-failed", "--recovery-preview", preview_path, "--acknowledge-live-requests"])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
        return result

    def candidate_attempt(self, campaign: Path, attempt_id: str) -> tuple[Path, dict[str, Any]]:
        with self.source_context():
            return claude_upgrade._load_capture_attempt(campaign, "candidate", CANDIDATE, attempt_id)

    def candidate_job_roots(self, campaign: Path) -> dict[str, Path]:
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with self.source_context():
            layout = capture._capture_layout(campaign, manifest)
            return {job.job_id: capture._host_path(job.evidence_roots[0], layout)
                    for job in claude_upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=CANDIDATE)}

    def assert_recorded_sources_untouched(self) -> dict[str, Any]:
        state = source.load_state(self.state_path, self.state_digest)
        replay.assert_source_untouched(state["snapshot"])
        for path, expected in state["freeze"]["source_sentinel"].items():
            if replay._stat(Path(path)) != expected:
                raise replay.ReplayError("候选 B 组改变了历史冻结原件")
        index = replay.load_snapshot(state["snapshot"])
        for relative, expected in index["files"].items():
            source_id, _, member = relative.partition("/")
            origin_root = Path(index["sources"][source_id])
            original = origin_root / member
            replay._regular(original, origin_root)
            if replay._digest(original) != expected:
                raise replay.ReplayError("候选 B 组的历史来源原字节发生漂移")
        freeze_origin = next(Path(path).parent for path in state["freeze"]["source_sentinel"] if Path(path).name == "freeze.json")
        for relative, expected in state["freeze"]["files"].items():
            original = freeze_origin / relative
            replay._regular(original, freeze_origin)
            if replay._digest(original) != {name: expected[name] for name in ("sha256", "bytes")}:
                raise replay.ReplayError("候选 B 组的历史冻结原字节发生漂移")
        return {"fixture_only": True, "purpose": "regression_fixture", "snapshot": state["snapshot"],
            "source_file_count": len(index["files"]), "source_probe_count": len(index["probes"]),
            "source_sentinel_count": len(index["source_sentinel"]), "origin_platform": "linux/amd64",
            "target_version": source.VERSION, "network_requests_this_run": 0, "formal_version_semantics_certified": False}

    def recorded_audit_report(self, record: dict[str, Any]) -> None:
        from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as cases

        if not cases._records_complete([record], [record["scenario"]]):
            raise replay.ReplayError("候选录制恢复场景未完成六项真实检查")
        facts = self.assert_recorded_sources_untouched()
        payload = {
            "status": "passed", "scenario_method": self._testMethodName, "source": facts, "audit": record,
            "registered_chains": [], "full_vc0_vc6_certified": False,
        }
        path = self.data / 'control/candidate-recovery-case-result.json'
        if path.exists():
            if not os.environ.get(stops.METHOD_RESUME_ENV) or source.read(path) != payload:
                raise replay.ReplayError('候选原六项审计结果已存在但不一致，禁止覆盖或追认')
            source.binding(path)
        else:
            source.write(path, payload)


def run_scenario(state_path: Path, digest: str, scenario: str) -> dict[str, Any]:
    prepared = prepare(state_path, digest)
    from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as cases

    data = Path(source.load_state(state_path, digest)["data_root"])
    entry = stops.load_state('candidate-method-entry')
    if entry is None:
        if os.environ.get(stops.METHOD_RESUME_ENV) and (
                stops.load_state('candidate-vc5-start') is not None
                or any(stops.load_state('cursor:' + name) is not None for name in ('candidate-reconcile', 'candidate-evolution'))):
            raise replay.ReplayError('候选原方法交接停点已缺失，禁止重新授予首次进入许可')
        stops.save_state('candidate-method-entry', {'method': SCENARIO_METHODS[scenario], 'status': 'uninitialized'})
    elif entry['method'] != SCENARIO_METHODS[scenario]:
        raise replay.ReplayError('候选原方法交接身份发生改变')
    output = io.StringIO()
    with mock.patch.dict(os.environ, {PREPARED_ENV: json.dumps(source.binding(prepared))}):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.TestSuite([
            cases._CandidateFaultScenarios(SCENARIO_METHODS[scenario])]))
    log_path = (Path(tempfile.mkdtemp(prefix='candidate-test-run-', dir=data / 'control')) / 'test-run.json'
                if stops.record_root() is not None else data / 'control/candidate-recovery-test-run.json')
    path = source.write(log_path, {
        "fixture_only": True, "purpose": "regression_fixture", "scenario": scenario,
        "tests_run": result.testsRun, "skipped": len(result.skipped), "errors": len(result.errors),
        "failures": len(result.failures), "output": output.getvalue(),
    })
    if result.testsRun != 1 or result.skipped or not result.wasSuccessful():
        raise replay.ReplayError(f"候选 B 组真实恢复场景失败；凭证 {path}：{output.getvalue()}")
    return source.read(data / "control/candidate-recovery-case-result.json")


def _namespace_handoff(state_path: Path, digest: str, scenario: str) -> dict[str, Any]:
    """原 namespace 初始化完成后立即绑定原state/site；仅保存坐标，不签运行通过。"""
    state = source.load_state(state_path, digest)
    value = {'state_path': state_path, 'digest': digest, 'state_binding': source.binding(state_path),
             'data': Path(state['data_root']), 'roots': state['readonly_sources'],
             'site_binding': source.binding(Path(state['data_root']) / 'control/site/sitecustomize.py'), 'scenario': scenario}
    previous = stops.load_state('candidate-namespace')
    if previous is not None:
        if any(previous[key] != item for key, item in value.items()) or not os.environ.get(stops.METHOD_RESUME_ENV):
            raise replay.ReplayError('候选原 namespace 交接身份或来源发生改变')
    else:
        stops.save_state('candidate-namespace', {**value, 'prepare_started': False})
    return run_scenario(state_path, digest, scenario)


def _restore_read_only_mounts(state_path: Path, digest: str, roots: list[str],
                              parent_namespaces: dict[str, str]) -> None:
    """只恢复原绑定根的只读挂载；宿主、联网或来源漂移均在挂载前拒绝。"""

    state = source.load_state(state_path, digest, isolation=False)
    if roots != state['readonly_sources']:
        raise replay.ReplayError('候选只读来源根与原状态不同源')
    for root in roots:
        replay._regular(Path(root), Path(root), directory=True)
    source.require_isolation({'parent_namespaces': parent_namespaces, 'readonly_sources': []})
    subprocess.run(['mount', '--make-rprivate', '/'], check=True)
    for root in roots:
        subprocess.run(['mount', '--bind', root, root], check=True)
        subprocess.run(['mount', '-o', 'remount,bind,ro', root], check=True)
    source.load_state(state_path, digest)


def _resume_namespace(arguments: argparse.Namespace, saved: dict[str, Any]) -> dict[str, Any]:
    """新的真实断网namespace只重挂原只读来源，绑定原state/site/数据根；不复制或建场。"""
    data = Path(arguments.data).resolve(strict=True)
    roots = [str(Path(arguments.recorded).resolve().parent), str(Path(arguments.freeze).resolve())]
    if (data != saved['data'] or roots != saved['roots'] or saved.get('scenario') != arguments.scenario
            or source.bound(saved['state_binding']) != saved['state_path']):
        raise replay.ReplayError('候选 namespace 恢复坐标或只读来源发生改变')
    source.bound(saved['site_binding'], root=data)
    _restore_read_only_mounts(saved['state_path'], saved['digest'], roots, json.loads(arguments.parent_namespaces))
    from tools.official_client_capture.tests.project_ledger_fixture import FIXTURE_ONLY_ENV
    with mock.patch.dict(os.environ, {source.STATE_ENV: str(saved['state_path']), source.DIGEST_ENV: saved['digest'],
            FIXTURE_ONLY_ENV: '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONPATH': os.pathsep.join([str(data / 'control/site'), str(full.vc1.REPOSITORY)])}):
        return _namespace_handoff(saved['state_path'], saved['digest'], arguments.scenario)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="隔离录制候选 B 组恢复场景；不签正式认证")
    for name in ("data", "recorded", "freeze", "parent-namespaces"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIO_METHODS), required=True)
    arguments = parser.parse_args(argv)
    try:
        saved = stops.load_state('candidate-namespace')
        if saved is not None:
            if not os.environ.get(stops.METHOD_RESUME_ENV):
                raise replay.ReplayError('候选旧 namespace 只允许原恢复入口重绑')
            result = _resume_namespace(arguments, saved)
        else:
            result = full.vc1.namespace_main(arguments, inputs_builder=inputs,
                chain_runner=lambda path, digest: _namespace_handoff(path, digest, arguments.scenario),
                activation_module=ACTIVATION_MODULE, initialization_name=SOURCE_INITIALIZATION,
                resume_inputs=_legacy_partial_initialization, input_verifier=_verify_initialization_inputs)
    except BaseException as error:
        traceback.print_exc()
        print(json.dumps({"status": "unverified", "fixture_only": True, "registered_chains": [], "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
