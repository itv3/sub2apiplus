"""预检报告的五项独立计算。

预检在试合并之外还必须回答四个问题：request 模板是否过期、上游是否改动了工具闭集、
上游改动命中了多少冻结台账路径、上游是否新增了扫描器要分类的发送点。v0.2.3 合并
时这四个问题都是到 U-1／U-4 才暴露的，每次都作废一个 Plan。这里把它们做成纯函数，
由 ``run_preflight`` 组装进报告；因冲突而 blocked 时前四项仍然输出。
"""

from __future__ import annotations

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


def scanner_coverage(
    fork_snapshot: dict[str, Any] | None,
    candidate_snapshot: dict[str, Any] | None,
    *,
    deferred_reason: str | None = None,
) -> dict[str, Any]:
    """候选树相对 fork 新增／移除的发送点；新增项是 §5.2.1 分类修复的输入。"""

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


def conflict_closure(conflict_paths: list[str], upstream_changed_path_count: int) -> dict[str, Any]:
    return {
        "conflict_count": len(conflict_paths),
        "conflict_paths": list(conflict_paths),
        "upstream_changed_path_count": upstream_changed_path_count,
    }
