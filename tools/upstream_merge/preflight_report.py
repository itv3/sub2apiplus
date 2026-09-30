"""预检报告的前五项独立计算（第六项 CI 作业覆盖见 ``ci_jobs.py``）。

预检在试合并之外还必须回答四个问题：request 模板是否过期、上游是否改动了工具闭集、
上游改动命中了多少冻结台账路径、上游是否新增了扫描器要分类的发送点。v0.2.3 合并
时这四个问题都是到 U-1／U-4 才暴露的，每次都作废一个 Plan。这里把它们做成纯函数，
由 ``run_preflight`` 组装进报告；因冲突而 blocked 时五项都照常输出。

第五项（扫描器覆盖）不依赖试合并结果（UM-13）：v0.2.10 试合并有 77 个冲突，旧实现只标
deferred，上游新增的 4 个发送点直到 Plan 003 门禁才暴露。现在预检另建一棵 ``-X ours``
试扫描树（冲突块取 fork 侧，仅供扫描），在其上运行 ``make egress-scanner-check``，把
"未匹配任何分类规则"与"[新增]"两类列为尚未在主干预先登记的上游新增发送点。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import expect_object, load_json, sha256_file
from .errors import UpstreamMergeError
from .freeze import _receipt_path_index, _rule_matches, load_freeze_registry, load_frozen_edges
from .gitops import _tool_source_paths, changed_paths

REQUEST_TEMPLATE_RELATIVE = "tools/upstream_merge/request_template_v2.json"
REQUEST_TEMPLATE_SCHEMA = "official-egress-upstream-merge-request-template/v1"
EXPECTED_EXECUTION_GROUP_COUNT = 3
GATE_COMPARE_FIELDS = ("id", "category", "mode", "cwd", "execution_group", "argv")
OFFICIAL_EGRESS_ROOT = "backend/internal/officialegress"
CLAUDE_RELEASE_CATALOG = f"{OFFICIAL_EGRESS_ROOT}/catalogdata/claude/release-catalog.json"
RUNTIME_RELEASE_CATALOG = f"{OFFICIAL_EGRESS_ROOT}/catalogdata/runtime/release-catalog.json"
SINK_DETAIL_FIELDS = ("scan_candidate_id", "file", "func", "package", "sink_kind", "protocol", "sink_type")


def load_request_template(repository_root: Path) -> dict[str, Any]:
    """读取标准 request 模板；模板属于工具闭集，结构漂移即 fail-close。"""

    path = repository_root / PurePosixPath(REQUEST_TEMPLATE_RELATIVE)
    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"标准 request 模板不存在：{REQUEST_TEMPLATE_RELATIVE}")
    template = expect_object(load_json(path, "RequestTemplate"), "RequestTemplate")
    if template.get("schema_version") != REQUEST_TEMPLATE_SCHEMA:
        raise UpstreamMergeError("RequestTemplate schema_version 非法")
    request = expect_object(template.get("request"), "RequestTemplate.request")
    gates = request.get("gates")
    if not isinstance(gates, list) or not gates:
        raise UpstreamMergeError("RequestTemplate.request.gates 必须是非空数组")
    return template


def _render_repository(value: Any, repository_root: Path) -> Any:
    if isinstance(value, str):
        return value.replace("{repository}", str(repository_root))
    if isinstance(value, list):
        return [_render_repository(item, repository_root) for item in value]
    return value


def _relative_to_root(repository_root: Path, absolute: Any) -> str | None:
    if not isinstance(absolute, str) or not absolute:
        return None
    try:
        return Path(absolute).resolve(strict=False).relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return None


def _claude_release_findings(repository_root: Path, client: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    try:
        catalog = expect_object(
            load_json(repository_root / PurePosixPath(CLAUDE_RELEASE_CATALOG), "ClaudeReleaseCatalog"),
            "ClaudeReleaseCatalog",
        )
        selectors = expect_object(catalog.get("selectors"), "ClaudeReleaseCatalog.selectors")
        active = expect_object(selectors.get("production_active"), "selectors.production_active")
        release_sha = active.get("release_sha256")
        release = next(
            (item for item in catalog.get("releases", []) if isinstance(item, dict) and item.get("release_sha256") == release_sha),
            None,
        )
        if release is None:
            return ["Claude production_active 指向的 release 不在 releases 中"]
        profile = expect_object(release.get("profile"), "release.profile")
        expected_profile = f"{OFFICIAL_EGRESS_ROOT}/{profile['path']}"
        actual_profile = _relative_to_root(repository_root, client.get("active_path"))
        if actual_profile != expected_profile:
            findings.append(f"Claude active_path 不是当前 production_active 的 profile，期望 {expected_profile}")
        elif sha256_file(repository_root / PurePosixPath(actual_profile)) != profile.get("sha256"):
            findings.append("Claude active profile 内容摘要与 release catalog 不一致")
        if client.get("target_version") != release.get("version"):
            findings.append(f"Claude target_version 与 production_active release 不一致，期望 {release.get('version')}")
        rollback = selectors.get("production_rollback")
        receipt = None
        if isinstance(rollback, dict) and isinstance(rollback.get("deployment"), dict):
            receipt = rollback["deployment"].get("receipt")
        if isinstance(receipt, dict) and isinstance(receipt.get("path"), str):
            actual_rollback = _relative_to_root(repository_root, client.get("rollback_path"))
            if actual_rollback != receipt["path"]:
                findings.append(f"Claude rollback_path 不是当前 production_rollback 收据，期望 {receipt['path']}")
            elif sha256_file(repository_root / PurePosixPath(actual_rollback)) != receipt.get("sha256"):
                findings.append("Claude rollback 收据内容摘要与 release catalog 不一致")
    except (UpstreamMergeError, OSError, KeyError, TypeError, AttributeError) as error:
        findings.append(f"无法解析 Claude release catalog：{error}")
    return findings


def _codex_release_findings(repository_root: Path, client: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    try:
        runtime = expect_object(
            load_json(repository_root / PurePosixPath(RUNTIME_RELEASE_CATALOG), "RuntimeReleaseCatalog"),
            "RuntimeReleaseCatalog",
        )
        binding = expect_object(runtime.get("snapshot_catalog"), "RuntimeReleaseCatalog.snapshot_catalog")
        catalog_path = repository_root / PurePosixPath(OFFICIAL_EGRESS_ROOT) / PurePosixPath(binding["path"])
        if sha256_file(catalog_path) != binding.get("sha256"):
            findings.append("Codex snapshot catalog 内容摘要与 runtime release catalog 绑定不一致")
        snapshots = expect_object(load_json(catalog_path, "SnapshotCatalog"), "SnapshotCatalog").get("snapshots")
        if not isinstance(snapshots, list):
            return findings + ["Codex snapshot catalog 缺少 snapshots"]
        by_file = {
            f"{OFFICIAL_EGRESS_ROOT}/catalogdata/runtime/{item['file']}": item
            for item in snapshots
            if isinstance(item, dict) and isinstance(item.get("file"), str)
        }
        for field, label in (("active_path", "active"), ("rollback_path", "rollback")):
            relative = _relative_to_root(repository_root, client.get(field))
            snapshot = by_file.get(relative or "")
            if snapshot is None:
                findings.append(f"Codex {label} profile 不在当前 snapshot catalog 中：{relative}")
                continue
            digest = sha256_file(repository_root / PurePosixPath(relative))
            if digest not in {snapshot.get("digest"), snapshot.get("blob_sha256")}:
                findings.append(f"Codex {label} profile 内容摘要与 snapshot catalog 不一致：{relative}")
            if field == "active_path" and client.get("target_version") != snapshot.get("version"):
                findings.append(f"Codex target_version 与 active profile 版本不一致，期望 {snapshot.get('version')}")
    except (UpstreamMergeError, OSError, KeyError, TypeError, AttributeError) as error:
        findings.append(f"无法解析 Codex release catalog：{error}")
    return findings


def template_validity(repository_root: Path, request: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """比对 request 与标准模板：schema、门禁定义、执行组模式、Persona 与 release catalog 绑定。"""

    findings: list[str] = []
    expected_request = template["request"]
    if request.get("schema_version") != expected_request.get("schema_version"):
        findings.append(f"request schema_version 不是 {expected_request.get('schema_version')}")
    request_gates = {gate.get("id"): gate for gate in request.get("gates", []) if isinstance(gate, dict)}
    template_gates = {gate.get("id"): gate for gate in expected_request.get("gates", []) if isinstance(gate, dict)}
    missing = sorted(set(template_gates) - set(request_gates))
    extra = sorted(set(request_gates) - set(template_gates))
    if missing or extra:
        findings.append(f"门禁 id 集合与模板不一致：缺失={missing}，多余={extra}")
    for gate_id in sorted(set(request_gates) & set(template_gates)):
        for field in GATE_COMPARE_FIELDS:
            actual = _render_repository(request_gates[gate_id].get(field), repository_root)
            expected = _render_repository(template_gates[gate_id].get(field), repository_root)
            if actual != expected:
                findings.append(f"门禁 {gate_id} 的 {field} 与模板不一致")
    groups = {gate.get("execution_group") for gate in request_gates.values()}
    if any(gate.get("mode") != "command" for gate in request_gates.values()):
        findings.append("存在非 command 模式的门禁，receipt_replay 类型已退休")
    effective_groups = {group for group in groups if isinstance(group, str) and group}
    if None in groups or len(effective_groups) != len(groups) or len(effective_groups) != EXPECTED_EXECUTION_GROUP_COUNT:
        findings.append(
            f"执行组数量为 {len(effective_groups)}，要求 {EXPECTED_EXECUTION_GROUP_COUNT} 且每个门禁都有 execution_group"
        )
    clients = request.get("official_clients", {})
    template_clients = expected_request.get("official_clients", {})
    for client_key in ("claude", "codex"):
        actual_persona = (clients.get(client_key) or {}).get("persona")
        expected_persona = (template_clients.get(client_key) or {}).get("persona")
        if actual_persona != expected_persona:
            findings.append(f"{client_key} persona 与模板不一致")
    if isinstance(clients.get("claude"), dict):
        findings.extend(_claude_release_findings(repository_root, clients["claude"]))
    if isinstance(clients.get("codex"), dict):
        findings.extend(_codex_release_findings(repository_root, clients["codex"]))
    template_path = repository_root / PurePosixPath(REQUEST_TEMPLATE_RELATIVE)
    return {
        "status": "passed" if not findings else "failed",
        "template_path": REQUEST_TEMPLATE_RELATIVE,
        "template_sha256": sha256_file(template_path) if template_path.is_file() else "",
        "gate_count": len(request_gates),
        "execution_group_count": len(effective_groups),
        "findings": findings,
    }


def _upstream_changed_paths(repository_root: Path, merge_base: str, upstream_commit: str) -> tuple[list[dict[str, str]], set[str]]:
    changes = changed_paths(repository_root, merge_base, upstream_commit)
    touched: set[str] = set()
    for change in changes:
        touched.add(change["path"])
        if change.get("old_path"):
            touched.add(change["old_path"])
    return changes, touched


def tool_bundle_disturbance(repository_root: Path, merge_base: str, upstream_commit: str) -> dict[str, Any]:
    """上游相对 merge-base 改动了哪些工具闭集文件；有改动不阻断，但必须在 U-1 前决定处置。"""

    bundle = set(_tool_source_paths(repository_root))
    _changes, touched = _upstream_changed_paths(repository_root, merge_base, upstream_commit)
    hit = sorted(bundle & touched)
    return {
        "status": "clean" if not hit else "disturbed",
        "bundle_path_count": len(bundle),
        "changed_bundle_paths": hit,
    }


def freeze_coverage(
    repository_root: Path,
    merge_base: str,
    upstream_commit: str,
    conflict_paths: list[str],
) -> dict[str, Any]:
    """上游改动命中了多少冻结台账路径，以及哪些命中还需要注册表登记的额外动作。"""

    edges, registered, receipts = load_frozen_edges(repository_root)
    registry = load_freeze_registry(repository_root)
    receipt_index = _receipt_path_index(repository_root, receipts)
    changes, touched = _upstream_changed_paths(repository_root, merge_base, upstream_commit)
    hit = sorted(path for path in touched if path in registered)
    rule_hits: dict[str, list[str]] = {}
    for rule in registry["rules"]:
        matched = sorted(path for path in hit if _rule_matches(rule, path, receipt_index))
        if matched:
            rule_hits[rule["id"]] = matched
    return {
        "status": "computed",
        "frozen_path_count": len(registered),
        "frozen_edge_count": len(edges),
        "upstream_changed_path_count": len(changes),
        "frozen_hit_count": len(hit),
        "frozen_hit_paths": hit,
        "conflicting_frozen_paths": sorted(set(hit) & set(conflict_paths)),
        "registry_rule_hits": rule_hits,
    }


def _sink_detail(sink: dict[str, Any]) -> dict[str, Any]:
    return {field: sink.get(field) for field in SINK_DETAIL_FIELDS}


def candidate_sink_diff(
    fork_snapshot: dict[str, Any] | None,
    candidate_snapshot: dict[str, Any] | None,
    *,
    deferred_reason: str | None = None,
) -> dict[str, Any]:
    """试合并无冲突时，候选树相对 fork 新增／移除的发送点（有冲突时标 deferred）。"""

    if deferred_reason is not None:
        return {"status": "deferred", "reason": deferred_reason}
    if not isinstance(fork_snapshot, dict) or not isinstance(candidate_snapshot, dict):
        return {"status": "failed", "reason": "fork 或候选发送面快照缺失"}
    fork_sinks = {
        sink.get("scan_candidate_id"): sink
        for sink in fork_snapshot.get("sinks", [])
        if isinstance(sink, dict) and isinstance(sink.get("scan_candidate_id"), str)
    }
    candidate_sinks = {
        sink.get("scan_candidate_id"): sink
        for sink in candidate_snapshot.get("sinks", [])
        if isinstance(sink, dict) and isinstance(sink.get("scan_candidate_id"), str)
    }
    added = sorted(set(candidate_sinks) - set(fork_sinks))
    removed = sorted(set(fork_sinks) - set(candidate_sinks))
    return {
        "status": "computed",
        "fork_sink_count": len(fork_sinks),
        "candidate_sink_count": len(candidate_sinks),
        "added_sink_count": len(added),
        "removed_sink_count": len(removed),
        "added_sinks": [_sink_detail(candidate_sinks[item]) for item in added],
        "removed_sinks": [_sink_detail(fork_sinks[item]) for item in removed],
    }


# egressscan -mode check 的输出格式（backend/cmd/egressscan/main.go 与 classify.go 的 unclassifiedError）：
#   漂移："  [新增] <ID>  (<callee> @ <file>:<line>)"、"  [变更] <ID>  <说明>"、"  [消失] <ID>  (<callee>)"
#   扫描失败："N 条 sink 未匹配任何分类规则：" 下的 "  - <ID>  (...)"；"N 条分类结果不完整：" 下的 "  - <ID>：<问题>"
DRIFT_LINE_RE = re.compile(r"^\s+\[(新增|变更|消失)\]\s+(\S+)")
LIST_LINE_RE = re.compile(r"^\s+-\s+([^\s：]+)")
DRIFT_KEYS = {"新增": "added", "变更": "changed", "消失": "removed"}
UPSTREAM_SCAN_METHOD = (
    "受维护分支 HEAD 与目标 tag 以 -X ours 试合并（冲突块取 fork 侧，仅供扫描），在试扫描树上运行 "
    "make egress-scanner-check；冲突块内的上游新增发送点看不到，由试验区完整门禁兜底"
)
REGISTRATION_HINT = (
    "在主干 backend/cmd/egressscan/post_bootstrap_acceptance.go 的 reviewedPostBootstrapSinkAdditions "
    "按候选树实际分类预先登记（absentBeforeMerge＋同一 mergeGroup）；缺分类规则的先在 classify.go 补规则；"
    "登记后重封基线，再 plan-create；决定不接通的（如邀请好友按 B 方案），在试验区删除该调用、由 U-2 surface-scan "
    "确认即可，不必登记"
)


def parse_scanner_check_output(stdout: str, stderr: str, exit_code: int) -> dict[str, Any]:
    """解析扫描器基线检查的输出，按漂移与扫描失败两类归并发送点 ID。"""

    parsed: dict[str, Any] = {
        "exit_code": exit_code,
        "added": [],
        "changed": [],
        "removed": [],
        "unclassified": [],
        "classification_problems": [],
        "metadata_changes": [],
    }
    section: str | None = None
    for line in f"{stdout}\n{stderr}".splitlines():
        drift = DRIFT_LINE_RE.match(line)
        if drift:
            if drift.group(2).startswith("["):
                # "[变更] [基线元数据] …" 是扫描器对基线元数据（如加载包数）的比较，不是发送点。
                parsed["metadata_changes"].append(line.strip())
            else:
                parsed[DRIFT_KEYS[drift.group(1)]].append({"scan_candidate_id": drift.group(2), "line": line.strip()})
            continue
        if "条 sink 未匹配任何分类规则" in line:
            section = "unclassified"
            continue
        if "条分类结果不完整" in line:
            section = "classification_problems"
            continue
        listed = LIST_LINE_RE.match(line)
        if listed and section is not None:
            parsed[section].append({"scan_candidate_id": listed.group(1), "line": line.strip()})
        elif line.strip() and not line.startswith(" "):
            section = None
    for key in ("added", "changed", "removed", "unclassified", "classification_problems"):
        parsed[key] = sorted(
            {item["scan_candidate_id"]: item for item in parsed[key]}.values(),
            key=lambda item: item["scan_candidate_id"],
        )
    if exit_code == 0:
        parsed["outcome"] = "passed"
    elif parsed["unclassified"] or parsed["classification_problems"]:
        parsed["outcome"] = "scan_failed"
    elif parsed["added"] or parsed["changed"] or parsed["removed"] or parsed["metadata_changes"]:
        parsed["outcome"] = "drift"
    else:
        parsed["outcome"] = "error"
        parsed["error"] = (stderr.strip() or stdout.strip())[-2000:]
    return parsed


def _rename_candidates(removed: list[dict[str, Any]], added: list[dict[str, Any]]) -> list[dict[str, str]]:
    """按"同文件同发送类型、函数改名"或"同函数同发送类型、换文件"配对消失与新增的发送点。"""

    def split(identifier: str) -> tuple[str, str, str] | None:
        if "@" not in identifier or "#" not in identifier:
            return None
        function, location = identifier.split("@", 1)
        file_part, _, kind = location.partition("#")
        return function, file_part, kind

    pairs: list[dict[str, str]] = []
    used: set[str] = set()
    for old in removed:
        old_parts = split(old["scan_candidate_id"])
        if old_parts is None:
            continue
        for new in added:
            identifier = new["scan_candidate_id"]
            new_parts = split(identifier)
            if identifier in used or new_parts is None or old_parts[2] != new_parts[2]:
                continue
            if old_parts[1] == new_parts[1] or old_parts[0] == new_parts[0]:
                pairs.append({"before": old["scan_candidate_id"], "after": identifier})
                used.add(identifier)
                break
    return pairs


def scanner_coverage(upstream_scan: dict[str, Any] | None, candidate_merge: dict[str, Any]) -> dict[str, Any]:
    """第五项：上游新增发送点是否已在主干预先登记（UM-13），另附试合并无冲突时的 fork→候选差异。"""

    if not isinstance(upstream_scan, dict):
        return {
            "status": "failed",
            "method": UPSTREAM_SCAN_METHOD,
            "reason": "试扫描没有产出结果",
            "candidate_merge": candidate_merge,
        }
    # 同一发送点可能既缺分类规则（第一轮）又在补临时分类后报"[新增]"（第二轮），按 ID 合并。
    missing_classification = {item["scan_candidate_id"] for item in upstream_scan["unclassified"]}
    merged = {item["scan_candidate_id"]: item for item in upstream_scan["unclassified"] + upstream_scan["added"]}
    unregistered = [
        {**merged[identifier], "missing_classification": identifier in missing_classification}
        for identifier in sorted(merged)
    ]
    if upstream_scan["outcome"] == "error":
        status = "failed"
    elif unregistered:
        status = "blocked"
    elif upstream_scan["outcome"] == "scan_failed":
        # 只有分类不完整、没有未分类项时，扫描器不会进入基线比较，看不到[新增]，不能判为通过。
        status = "failed"
    else:
        status = "passed"
    report: dict[str, Any] = {
        "status": status,
        "method": UPSTREAM_SCAN_METHOD,
        "scan_exit_code": upstream_scan["exit_code"],
        "unregistered_added_count": len(unregistered),
        "unregistered_added_sinks": unregistered,
        "unclassified_sinks": upstream_scan["unclassified"],
        "temporarily_classified": upstream_scan.get("temporarily_classified", []),
        "classification_problems": upstream_scan["classification_problems"],
        "changed_sinks": upstream_scan["changed"],
        "metadata_changes": upstream_scan.get("metadata_changes", []),
        "removed_sinks": upstream_scan["removed"],
        "rename_candidates": _rename_candidates(upstream_scan["removed"], upstream_scan["added"]),
        "unresolved_conflict_count": upstream_scan.get("unresolved_conflict_count", 0),
        "candidate_merge": candidate_merge,
    }
    if unregistered:
        report["registration_hint"] = REGISTRATION_HINT
    if upstream_scan["outcome"] == "error":
        report["error"] = upstream_scan.get("error", "")
    return report


def conflict_closure(conflict_paths: list[str], upstream_changed_path_count: int) -> dict[str, Any]:
    return {
        "conflict_count": len(conflict_paths),
        "conflict_paths": list(conflict_paths),
        "upstream_changed_path_count": upstream_changed_path_count,
    }
