"""U-1～U-6 上游合并阶段实现。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import (
    artifact_binding,
    bind_identity,
    canonical_bytes,
    expect_exact_fields,
    expect_git_object,
    expect_object,
    expect_safe_id,
    expect_sha256,
    expect_string,
    file_binding,
    load_json,
    resolve_within,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
    validate_artifact_binding,
    validate_file_binding,
    validate_identity,
    validate_string_enum,
    write_json_once,
    write_once,
)
from .contracts import (
    CLIENT_KEYS,
    REQUIRED_GATE_CATEGORIES,
    LoadedPlan,
    _validate_inventory_payload,
    artifact_document,
    inventory_revision_number,
    load_request,
    latest_revision,
    latest_stage_path,
    next_inventory_path,
    next_stage_path,
    resolve_covered_tags,
    revision_number,
    stage_paths,
    stage_binding,
    upstream_range_tags,
)
from .ci_jobs import ci_job_coverage
from .errors import UpstreamMergeError
from .preflight_report import (
    candidate_sink_diff,
    conflict_closure,
    freeze_coverage,
    load_request_template,
    parse_scanner_check_output,
    scanner_coverage,
    template_validity,
    tool_bundle_disturbance,
)
from .gitops import (
    assert_clean,
    assert_git_repository,
    changed_paths,
    commit_tree,
    current_branch_ref,
    executable_identity,
    git_output,
    merge_base,
    remote_url,
    rev_parse,
    route_snapshot,
    run_egress_snapshot,
    run_git,
    run_process,
    status_paths,
    tag_commit,
    unmerged_entries,
    validate_protected_objects,
    validate_tool_bundle,
)
MERGE_START_SCHEMA = "official-egress-upstream-merge-start/v1"
MERGE_CANDIDATE_SCHEMA = "official-egress-upstream-merge-candidate-tree/v1"
CONFLICT_INPUT_SCHEMA = "official-egress-upstream-conflict-resolution-input/v1"
CONFLICT_LEDGER_SCHEMA = "official-egress-upstream-conflict-resolution-ledger/v1"
SOURCE_CHANGE_INPUT_SCHEMA = "official-egress-upstream-source-change-input/v1"
SOURCE_CANDIDATE_SCHEMA = "official-egress-upstream-source-candidate/v1"
SURFACE_DELTA_SCHEMA = "official-egress-upstream-surface-delta/v1"
SURFACE_DECISION_SCHEMA = "official-egress-upstream-surface-decision/v1"
SURFACE_RECEIPT_SCHEMA = "official-egress-upstream-surface-recalculation-receipt/v1"
IMPACT_MATRIX_SCHEMA = "official-egress-upstream-impact-matrix/v1"
CHANGE_DECISION_INPUT_SCHEMA = "official-egress-upstream-change-decision-input/v1"
CHANGE_DECISION_RECEIPT_SCHEMA = "official-egress-upstream-change-decision-receipt/v1"
VERIFICATION_RECEIPT_SCHEMA = "official-egress-upstream-verification-receipt/v1"
CANDIDATE_DISPOSITION_INPUT_SCHEMA = "official-egress-upstream-candidate-disposition-input/v1"
CANDIDATE_DISPOSITION_SCHEMA = "official-egress-upstream-candidate-disposition/v1"
BRANCH_APPLY_SCHEMA = "official-egress-upstream-branch-apply/v1"
UPSTREAM_RECEIPT_SCHEMA = "official-egress-upstream-merge-receipt/v1"
SOURCE_TRANSITION_SCHEMA = "official-egress-upstream-source-transition/v2"

REVISION_STAGE_SCHEMAS = {
    "source_candidate": SOURCE_CANDIDATE_SCHEMA,
    "surface_delta": SURFACE_DELTA_SCHEMA,
    "surface_receipt": SURFACE_RECEIPT_SCHEMA,
    "impact_matrix": IMPACT_MATRIX_SCHEMA,
    "impact_receipt": CHANGE_DECISION_RECEIPT_SCHEMA,
}

REVISION_STAGE_FIELDS = {
    "source_candidate": {
        "plan_id",
        "plan_identity_sha256",
        "merge_candidate",
        "source_commit",
        "source_tree",
        "changed_paths",
        "source_change_input",
        "codex_overlay_ledger",
    },
    "surface_delta": {
        "plan_id",
        "plan_identity_sha256",
        "source_candidate",
        "baseline_route_snapshot",
        "candidate_route_snapshot",
        "baseline_source_to_sink_snapshot",
        "candidate_source_to_sink_snapshot",
        "route_delta_count",
        "egress_delta_count",
        "deltas",
    },
    "surface_receipt": {
        "plan_id",
        "plan_identity_sha256",
        "source_candidate",
        "surface_delta",
        "route_snapshot",
        "source_to_sink_snapshot",
        "candidate_inventories",
        "surface_decision",
        "unknown_oauth_egress_count",
        "unclassified_delta_count",
        "result",
    },
    "impact_matrix": {
        "plan_id",
        "plan_identity_sha256",
        "source_candidate",
        "surface_receipt",
        "file_change_count",
        "file_changes",
        "surface_delta_count",
        "surface_deltas",
        "classification_rule",
        "result",
    },
    "impact_receipt": {
        "plan_id",
        "plan_identity_sha256",
        "impact_matrix",
        "change_decision",
        "file_decision_count",
        "surface_decision_count",
        "client_impacts",
        "successor_campaign_required",
        "shared_contract_required",
        "unclassified_count",
        "official_client_identity_change_count",
        "result",
    },
}

REVISION_STAGE_OPTIONAL_FIELDS = {
    "source_candidate": set(),
    "surface_delta": set(),
    "surface_receipt": set(),
    "impact_matrix": {"component_mapping"},
    "impact_receipt": {
        "component_mapping_schema",
        "component_mapping_version",
        "component_mapping_sha256",
        "auto_accepted_count",
        "manual_decision_count",
        "unknown_component_count",
    },
}

RESOLUTION_KINDS = {"fork", "manual", "upstream"}
FILE_IMPACT_CATEGORIES = {
    "claude_persona",
    "codex_persona",
    "key_group_routing_billing",
    "out_of_scope_product",
    "protocol_adapter",
    "repository_support",
    "shared_control",
}

# 组件映射是安全策略的一部分；其版本和摘要会随 ImpactMatrix 一起封存。
COMPONENT_OWNERSHIP_SCHEMA = "official-egress-upstream-component-ownership/v1"
COMPONENT_OWNERSHIP_VERSION = "2026-09-08"
COMPONENT_OWNERSHIP_MANIFEST: dict[str, Any] = {
    "schema_version": COMPONENT_OWNERSHIP_SCHEMA,
    "version": COMPONENT_OWNERSHIP_VERSION,
    "components": [
        {
            "id": "officialegress",
            "prefixes": ["backend/internal/officialegress/"],
            "owner": "official-egress",
            "dependencies": ["shared_control"],
            "risk": "high",
            "categories": ["shared_control"],
        },
        {
            "id": "service",
            "prefixes": ["backend/internal/service/"],
            "owner": "service",
            "dependencies": ["officialegress", "shared_control"],
            "risk": "high",
            "categories": ["protocol_adapter"],
        },
        {
            "id": "handler",
            "prefixes": [
                "backend/internal/handler/",
                "backend/internal/server/routes/",
                "backend/cmd/server/",
            ],
            "owner": "server-routing",
            "dependencies": ["service", "officialegress"],
            "risk": "high",
            "categories": ["protocol_adapter", "key_group_routing_billing"],
        },
        {
            "id": "repository",
            "prefixes": ["backend/internal/repository/"],
            "owner": "repository",
            "dependencies": ["service"],
            "risk": "high",
            "categories": ["key_group_routing_billing"],
        },
        {
            "id": "database_migration",
            "prefixes": ["backend/migrations/", "backend/internal/migration/"],
            "owner": "database",
            "dependencies": ["service"],
            "risk": "high",
            "categories": ["key_group_routing_billing"],
        },
        {
            "id": "backend_dependency_manifest",
            "prefixes": ["backend/go.mod", "backend/go.sum", "go.mod", "go.sum"],
            "owner": "build-system",
            "dependencies": ["service", "officialegress"],
            "risk": "high",
            "categories": ["shared_control"],
        },
        {
            "id": "egress_scanner",
            "prefixes": ["backend/cmd/egressscan/"],
            "owner": "egress-security",
            "dependencies": ["officialegress", "shared_control"],
            "risk": "high",
            "categories": ["shared_control"],
        },
        {
            "id": "upstream_merge_tool",
            "prefixes": [
                "tools/upstream_merge/",
                "tools/upstream_merge_plan.schema.json",
                "tools/upstream_merge_request.schema.json",
                "tools/upstream_merge_artifacts.schema.json",
                "tools/check_ledger_completeness.py",
                "Makefile",
            ],
            "owner": "release-engineering",
            "dependencies": [],
            "risk": "high",
            "categories": ["shared_control"],
        },
        {
            "id": "egress_governance",
            "prefixes": ["docs/egress/"],
            "owner": "egress-security",
            "dependencies": ["shared_control"],
            "risk": "high",
            "categories": ["shared_control"],
        },
        {
            "id": "repository_support",
            "prefixes": [
                "frontend/",
                ".github/",
                "deploy/",
                "docs/",
                ".golangci.yml",
                "package.json",
                "pnpm-lock.yaml",
            ],
            "owner": "repository-support",
            "dependencies": [],
            "risk": "low",
            "categories": ["repository_support"],
        },
    ],
    "unknown_policy": "manual_and_fail_closed",
}
COMPONENT_OWNERSHIP_SHA256 = sha256_bytes(canonical_bytes(COMPONENT_OWNERSHIP_MANIFEST))
AUTO_CLASSIFICATION_BLOCKERS = {
    "account",
    "billing",
    "group",
    "key",
    "quota_usage",
    "route",
    "selector",
    "wire",
}
AUTO_CLASSIFICATION_CATEGORIES = {"out_of_scope_product", "repository_support"}
# 依赖既可以指向组件，也可以指向受控的抽象控制面；后者必须显式列入白名单。
KNOWN_ABSTRACT_DEPENDENCIES = {"shared_control"}


def _stage_document(plan: LoadedPlan, schema: str, payload: dict[str, Any]) -> dict[str, Any]:
    return bind_identity(
        {
            "schema_version": schema,
            "plan_id": plan.plan_id,
            "plan_identity_sha256": plan.identity,
            **payload,
        }
    )


def _load_merge_start(plan: LoadedPlan) -> dict[str, Any]:
    return artifact_document(
        plan.output_path("merge_start"),
        "MergeStart",
        MERGE_START_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "fork_head",
            "upstream_commit",
            "merge_base",
            "worktree",
            "merge_exit_code",
            "status",
            "conflict_paths",
            "conflict_stages",
            "stdout",
            "stderr",
        },
    )


def _load_merge_candidate(plan: LoadedPlan) -> dict[str, Any]:
    return artifact_document(
        plan.output_path("merge_candidate"),
        "MergeCandidateTree",
        MERGE_CANDIDATE_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "merge_start",
            "conflict_ledger",
            "parents",
            "merge_commit",
            "candidate_tree",
            "changed_paths",
            "protected_objects_unchanged",
        },
    )


def _load_source_candidate(plan: LoadedPlan) -> dict[str, Any]:
    path = latest_stage_path(plan, "source_candidate")
    document = artifact_document(
        path,
        "SourceCandidate",
        SOURCE_CANDIDATE_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "merge_candidate",
            "source_commit",
            "source_tree",
            "changed_paths",
            "source_change_input",
            "codex_overlay_ledger",
        },
        optional_fields={"revision", "predecessor"},
    )
    _validate_revision_metadata(plan, "source_candidate", path, document)
    return document


def _load_revision_artifact(
    plan: LoadedPlan,
    key: str,
    label: str,
    schema: str,
    fields: set[str],
    *,
    optional_fields: set[str] | None = None,
) -> dict[str, Any]:
    """按最新 revision 读取阶段制品，并允许追加式轮次 metadata。"""

    path = latest_stage_path(plan, key)
    document = artifact_document(
        path,
        label,
        schema,
        fields,
        optional_fields={"revision", "predecessor"} | (optional_fields or set()),
    )
    _validate_revision_metadata(plan, key, path, document)
    return document


def _validate_current_stage_binding(
    plan: LoadedPlan,
    value: Any,
    key: str,
    label: str,
) -> dict[str, Any]:
    """验证新格式制品内部引用确实指向该阶段的最新 revision。"""

    binding = validate_artifact_binding(plan.evidence_root, value, label)
    path = resolve_within(plan.evidence_root, binding["path"], f"{label}.path")
    expected = latest_stage_path(plan, key).resolve(strict=True)
    if path.resolve(strict=True) != expected:
        raise UpstreamMergeError(
            f"{label} 未绑定当前 {key} revision：expected={expected} actual={path}"
        )
    return binding


def _validate_revision_metadata(
    plan: LoadedPlan,
    key: str,
    path: Path,
    document: dict[str, Any],
) -> None:
    """验证追加式制品的编号和前序绑定；旧制品首轮仍保持只读兼容。"""

    path_revision = revision_number(path, plan, key)
    has_revision = "revision" in document
    has_predecessor = "predecessor" in document
    if has_revision != has_predecessor:
        raise UpstreamMergeError(f"{key} revision/predecessor metadata 必须成对出现")
    source_revision = (
        latest_revision(plan, "source_candidate")
        if key != "source_candidate"
        else path_revision
    )
    if not has_revision:
        # 旧格式只允许作为第 1 轮兼容读取；一旦当前 SourceCandidate 已进入
        # 追加轮次，所有依赖阶段都必须显式绑定同一 revision。
        if path_revision > 1 or (key != "source_candidate" and source_revision > 1):
            raise UpstreamMergeError(
                f"{key} revision {path_revision} 缺少 revision/predecessor metadata"
            )
        return
    revision = document.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise UpstreamMergeError(f"{key} revision metadata 非法")
    if path_revision != revision:
        raise UpstreamMergeError(f"{key} revision 与文件名不一致")
    if key != "source_candidate":
        if source_revision and revision != source_revision:
            raise UpstreamMergeError(
                f"{key} revision 未绑定当前 SourceCandidate："
                f"expected={source_revision} actual={revision}"
            )
    all_paths = stage_paths(plan, key)
    current_index = next(
        (index for index, item in enumerate(all_paths) if item == path),
        None,
    )
    predecessor = document.get("predecessor")
    if revision == 1:
        if predecessor is not None:
            raise UpstreamMergeError(f"{key} 首轮不得包含 predecessor")
        return
    if current_index is None or current_index == 0:
        raise UpstreamMergeError(f"{key} revision 缺少前序制品")
    previous_path = all_paths[current_index - 1]
    if revision_number(previous_path, plan, key) != revision - 1:
        raise UpstreamMergeError(f"{key} revision 前序编号不连续")
    expected = artifact_binding(plan.evidence_root, previous_path)
    if predecessor != expected:
        raise UpstreamMergeError(f"{key} predecessor 绑定漂移")
    _validate_revision_chain_node(plan, key, previous_path, revision - 1, set())


def _validate_revision_chain_node(
    plan: LoadedPlan,
    key: str,
    path: Path,
    expected_revision: int,
    seen: set[Path],
) -> None:
    """递归检查追加式制品链，防止伪造中间节点或 predecessor 环。"""

    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"{key} revision predecessor 不是可信普通文件")
    resolved = path.resolve(strict=False)
    if resolved in seen:
        raise UpstreamMergeError(f"{key} revision predecessor 存在循环")
    seen.add(resolved)
    actual_revision = revision_number(path, plan, key)
    if actual_revision != expected_revision:
        raise UpstreamMergeError(
            f"{key} revision predecessor 编号不一致："
            f"expected={expected_revision} actual={actual_revision}"
        )
    document = artifact_document(
        path,
        f"{key} revision predecessor",
        REVISION_STAGE_SCHEMAS[key],
        REVISION_STAGE_FIELDS[key],
        optional_fields={"revision", "predecessor"}
        | REVISION_STAGE_OPTIONAL_FIELDS[key],
    )
    has_revision = "revision" in document
    has_predecessor = "predecessor" in document
    if has_revision != has_predecessor:
        raise UpstreamMergeError(f"{key} revision predecessor metadata 不成对")
    if expected_revision == 1:
        # 第 1 轮允许历史旧格式；若带 metadata，则仍需保证其值自洽。
        if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
            raise UpstreamMergeError(f"{key} 首轮制品身份不一致")
        if not has_revision:
            return
        if has_revision:
            value = document.get("revision")
            if isinstance(value, bool) or value != 1 or document.get("predecessor") is not None:
                raise UpstreamMergeError(f"{key} 首轮 predecessor metadata 非法")
        return
    if not has_revision:
        raise UpstreamMergeError(f"{key} revision {expected_revision} 缺少 metadata")
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError(f"{key} revision predecessor 计划身份不一致")
    value = document.get("revision")
    if isinstance(value, bool) or value != expected_revision:
        raise UpstreamMergeError(f"{key} revision predecessor 编号漂移")
    previous = document.get("predecessor")
    if previous is None:
        raise UpstreamMergeError(f"{key} revision {expected_revision} 缺少 predecessor")
    paths = stage_paths(plan, key)
    index = next((item_index for item_index, item in enumerate(paths) if item == path), None)
    if index is None or index == 0:
        raise UpstreamMergeError(f"{key} revision predecessor 链断裂")
    previous_path = paths[index - 1]
    if artifact_binding(plan.evidence_root, previous_path) != previous:
        raise UpstreamMergeError(f"{key} revision predecessor 绑定漂移")
    _validate_revision_chain_node(plan, key, previous_path, expected_revision - 1, seen)


def _revision_metadata(plan: LoadedPlan, key: str, revision: int) -> dict[str, Any]:
    """生成不改变阶段语义的 revision 链接 metadata。"""

    previous = stage_paths(plan, key)
    predecessor = (
        artifact_binding(plan.evidence_root, previous[-1]) if previous else None
    )
    return {"revision": revision, "predecessor": predecessor}


def _validated_linked_worktree(plan: LoadedPlan, path: Path) -> Path:
    root = assert_git_repository(path)
    common = Path(git_output(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (root / common).resolve()
    main_common = Path(git_output(plan.repository_root, "rev-parse", "--git-common-dir"))
    if not main_common.is_absolute():
        main_common = (plan.repository_root / main_common).resolve()
    if common.resolve() != main_common.resolve():
        raise UpstreamMergeError("隔离 worktree 不属于计划仓库")
    validate_tool_bundle(root, plan.document["tool_bundle"])
    return root


def _worktree_root(plan: LoadedPlan) -> Path:
    return _validated_linked_worktree(plan, plan.worktree)


def _remove_temporary_worktree(
    plan: LoadedPlan,
    worktree: Path,
    *,
    strict: bool,
) -> None:
    completed = run_git(
        plan.repository_root,
        "worktree",
        "remove",
        "--force",
        str(worktree),
        check=False,
    )
    if completed.returncode == 0:
        return
    if worktree.exists():
        shutil.rmtree(worktree)
    run_git(plan.repository_root, "worktree", "prune", check=False)
    if strict:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise UpstreamMergeError(f"无法移除临时隔离 worktree：{detail}")


@contextmanager
def _temporary_detached_worktree(
    plan: LoadedPlan,
    commit: str,
) -> Iterator[Path]:
    """为重放创建一次性 detached worktree，无论成败均清理 Git 登记。"""

    expect_git_object(commit, "temporary worktree commit")
    temporary_root = Path(tempfile.mkdtemp(prefix="sub2api-upstream-replay-"))
    worktree = temporary_root / "worktree"
    added = False
    try:
        run_git(
            plan.repository_root,
            "worktree",
            "add",
            "--detach",
            str(worktree),
            commit,
        )
        added = True
        root = _validated_linked_worktree(plan, worktree)
        if rev_parse(root, "HEAD^{commit}") != commit:
            raise UpstreamMergeError("临时隔离 worktree 未停在指定 commit")
        yield root
    except BaseException:
        if added:
            _remove_temporary_worktree(plan, worktree, strict=False)
        raise
    else:
        if added:
            _remove_temporary_worktree(plan, worktree, strict=True)
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)


PREFLIGHT_SCHEMA = "official-egress-upstream-preflight/v1"


def _preflight_check(
    check_id: str,
    argv: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """执行一个只读预检命令并保留可比较的结果。"""

    started = time.monotonic()
    effective_env = env or {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "GIT_TERMINAL_PROMPT": "0",
    }
    completed = run_process(
        argv,
        cwd=cwd,
        check=False,
        env=effective_env,
    )
    return {
        "id": check_id,
        "argv": argv,
        "cwd": str(cwd),
        "exit_code": completed.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout_sha256": sha256_bytes(completed.stdout.encode("utf-8")),
        "stderr_sha256": sha256_bytes(completed.stderr.encode("utf-8")),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "status": "passed" if completed.returncode == 0 else "failed",
    }


def _preflight_freeze_coverage(
    repository_root: Path,
    merge_base_value: str,
    upstream_commit: str,
    conflict_paths: list[str],
    blockers: list[str],
) -> dict[str, Any]:
    try:
        return freeze_coverage(repository_root, merge_base_value, upstream_commit, conflict_paths)
    except UpstreamMergeError as error:
        blockers.append("冻结覆盖检查失败：" + str(error))
        return {"status": "failed", "reason": str(error)}


def _preflight_covered_tags(
    repository_root: Path,
    upstream: dict[str, Any],
    merge_base_value: str,
    blockers: list[str],
) -> dict[str, Any]:
    """列出 merge-base 之后到目标的全部上游 tag，及每个 tag 相对前一个的提交数与变化文件数。

    与 plan-create 使用同一复算规则：区间内有多个 tag 时 request 必须逐个登记 covered_tags。
    """

    findings: list[str] = []
    try:
        ranged = upstream_range_tags(repository_root, merge_base_value, upstream["commit"])
    except UpstreamMergeError as error:
        blockers.append("覆盖区间无法复算：" + str(error))
        return {"status": "failed", "tags": [], "findings": [str(error)]}
    try:
        resolve_covered_tags(
            repository_root,
            upstream,
            merge_base_value,
            label="UpstreamMergeRequest.upstream",
            require_explicit=True,
        )
    except UpstreamMergeError as error:
        findings.append(str(error))
        blockers.append("覆盖区间检查失败：" + str(error))
    rows: list[dict[str, Any]] = []
    previous = merge_base_value
    for item in ranged:
        rows.append(
            {
                "tag": item["tag"],
                "commit": item["commit"],
                "commit_count": int(
                    git_output(repository_root, "rev-list", "--count", f"{previous}..{item['commit']}")
                ),
                "changed_file_count": len(changed_paths(repository_root, previous, item["commit"])),
            }
        )
        previous = item["commit"]
    return {"status": "passed" if not findings else "failed", "tags": rows, "findings": findings}


PREFLIGHT_GIT_IDENTITY = (
    "-c",
    "user.name=Sub2API Upstream Preflight",
    "-c",
    "user.email=upstream-preflight@sub2apiplus.invalid",
)
# 试扫描树专用：给第一轮扫描报"未匹配任何分类规则"的上游新增发送点临时补 out-of-scope 分类，
# 让扫描器能走到基线比较、一次列全"[新增]"。只写进一次性试扫描树，永不提交。
SCAN_ONLY_RULES_RELATIVE = "backend/cmd/egressscan/zz_preflight_scan_only_rules.go"
SCANNER_SOURCE_RELATIVE = "backend/cmd/egressscan"
SCANNER_SUCCESSOR_RELATIVE = "docs/egress/maintenance/scanner-algorithm-successor.json"
SCANNER_LOCK_RELATIVE = "docs/egress/maintenance/bootstrap-inventory-lock.json"
SCANNER_SUCCESSOR_SCHEMA = "official-egress-scanner-algorithm-successor/v1"


def _scanner_algorithm_digest(directory: Path) -> str:
    """与 egressscan 的 scannerAlgorithmDigest 同一算法：非测试 .go 按文件名排序，逐个累加"名\\0内容\\0"。"""

    digest = hashlib.sha256()
    names = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.name.endswith(".go") and not path.name.endswith("_test.go")
    )
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((directory / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_scan_only_rules(scan_tree: Path, identifiers: list[str]) -> None:
    """在试扫描树写入按完整 ID 精确匹配的临时分类，并让扫描器算法后继指向新摘要。"""

    entries = "".join(
        "\t\tclassifyRule{\n"
        f"\t\t\tcandidateExact: {json.dumps(identifier)},\n"
        '\t\t\tpersona: "out-of-scope", backend: "-", state: "not_applicable", owner: "-", changeset: "-",\n'
        '\t\t\trationale: "上游合并预检试扫描临时分类：上游新增、尚未在主干分类与登记",\n'
        "\t\t},\n"
        for identifier in identifiers
    )
    source = (
        "// 上游合并预检试扫描临时文件：只存在于一次性试扫描树，永不提交。\n"
        "package main\n\n"
        "func init() {\n"
        "\tclassifyRules = append(classifyRules,\n"
        f"{entries}"
        "\t)\n"
        "}\n"
    )
    (scan_tree / SCAN_ONLY_RULES_RELATIVE).write_text(source, encoding="utf-8")
    successor_path = scan_tree / SCANNER_SUCCESSOR_RELATIVE
    if successor_path.is_file():
        successor = expect_object(load_json(successor_path, "scanner algorithm successor"), "scanner algorithm successor")
    else:
        lock = expect_object(load_json(scan_tree / SCANNER_LOCK_RELATIVE, "bootstrap inventory lock"), "bootstrap inventory lock")
        successor = {
            "schema_version": SCANNER_SUCCESSOR_SCHEMA,
            "from_sha256": lock.get("scanner_algorithm_sha256"),
            "source_transition": "upstream-merge-preflight-scan-only",
            "reviewed_by": "upstream-merge-preflight",
            "reason": "试扫描树临时分类，永不提交",
        }
    successor["to_sha256"] = _scanner_algorithm_digest(scan_tree / SCANNER_SOURCE_RELATIVE)
    successor_path.write_text(json.dumps(successor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _preflight_upstream_sink_scan(
    root: Path,
    fork_head: str,
    upstream_commit: str,
    temporary_root: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    """UM-13：不依赖试合并结果，列出尚未在主干预先登记的上游新增发送点。

    在临时 detached worktree 里以 ``-X ours`` 合入目标提交：非冲突区完整带入上游改动（上游新增的
    文件与函数都在），冲突块一律取 fork 侧，保证 fork 的扫描器、分类规则与登记清单原样可用；
    ``-X ours`` 解决不了的修改／删除等冲突按 fork 侧处理（fork 有则保留，fork 删了则删除）。
    这棵树只供扫描，不提交、不进入任何收据；随后运行 ``make egress-scanner-check``，由调用方
    把"未匹配任何分类规则"与"[新增]"列为待预先登记的发送点。
    """

    scan_tree = temporary_root / "upstream-sink-scan"
    run_git(root, "worktree", "add", "--detach", str(scan_tree), fork_head)
    try:
        run_git(
            scan_tree,
            *PREFLIGHT_GIT_IDENTITY,
            "merge",
            "--no-ff",
            "--no-commit",
            "-X",
            "ours",
            upstream_commit,
            check=False,
        )
        stages: dict[str, set[int]] = {}
        for entry in unmerged_entries(scan_tree):
            stages.setdefault(entry["path"], set()).add(entry["stage"])
        for path, present in sorted(stages.items()):
            if 2 in present:
                run_git(scan_tree, "checkout", "--ours", "--", path)
                run_git(scan_tree, "add", "--", path)
            else:
                run_git(scan_tree, "rm", "--quiet", "--cached", "--ignore-unmatch", "--", path)
                target = scan_tree / path
                if target.is_file() or target.is_symlink():
                    target.unlink()
        def check() -> dict[str, Any]:
            completed = run_process(
                ("make", "--no-print-directory", "egress-scanner-check"),
                cwd=scan_tree,
                check=False,
                env=env,
            )
            return parse_scanner_check_output(completed.stdout, completed.stderr, completed.returncode)

        parsed = check()
        unclassified = parsed["unclassified"]
        if parsed["outcome"] == "scan_failed" and unclassified and (scan_tree / SCANNER_SOURCE_RELATIVE).is_dir():
            # 第一轮因缺分类规则在基线比较前失败：临时补分类后第二轮一次列全"[新增]"，
            # 缺分类规则的名单保留第一轮的结果。
            _write_scan_only_rules(scan_tree, [item["scan_candidate_id"] for item in unclassified])
            parsed = check()
            parsed["unclassified"] = unclassified
            parsed["temporarily_classified"] = [item["scan_candidate_id"] for item in unclassified]
        parsed["unresolved_conflict_count"] = len(stages)
        return parsed
    finally:
        run_git(root, "worktree", "remove", "--force", str(scan_tree), check=False)


def run_preflight(
    request_path: Path,
    repository_root: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """在创建正式 U-0 计划前执行离线合并预检。

    预检不会写入主仓库、不会 fetch/push，也不会创建权威阶段制品。它只在临时
    detached worktree 中试合并，并将冲突、发送面扫描和快速构建检查汇总为报告。
    """

    root = assert_git_repository(repository_root)
    if output_path is not None:
        if not output_path.is_absolute():
            raise UpstreamMergeError("preflight 输出必须是绝对路径")
        normalized_output = output_path.resolve(strict=False)
        if normalized_output.is_relative_to(root):
            raise UpstreamMergeError("preflight 报告不得写入主仓库内部")
        if output_path.exists() or output_path.is_symlink():
            raise UpstreamMergeError(f"preflight 报告输出已存在，禁止覆盖：{output_path}")
    request = load_request(request_path)
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    offline_env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "GIT_TERMINAL_PROMPT": "0",
    }

    # 预检应尽早报告工作树问题，但不修改它；正式 plan-create 仍会再次严格校验。
    status = run_git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout
    if status:
        blockers.append("主仓库工作树不是干净状态；正式 U-0 仍会拒绝")

    managed_ref = request["repository"]["managed_ref"]
    if current_branch_ref(root) != managed_ref:
        blockers.append("当前分支不是请求指定的受维护分支")
    fork_head = rev_parse(root, f"{managed_ref}^{{commit}}")
    upstream = request["upstream"]
    if remote_url(root, upstream["remote"]) != upstream["url"]:
        blockers.append("本地 remote URL 与请求不一致")
    if tag_commit(root, upstream["tag"]) != upstream["commit"]:
        blockers.append("上游 tag 与请求 commit 不一致")
    merge_base_value = merge_base(root, fork_head, upstream["commit"])
    covered_tags_report = _preflight_covered_tags(root, upstream, merge_base_value, blockers)

    # §5.2.2 的六项报告：模板有效性、闭集受扰、冲突闭集、冻结覆盖、扫描器覆盖、CI 作业覆盖。
    # 六项都不依赖试合并结果，因冲突而 blocked 时仍然输出；扫描器覆盖另建 -X ours 试扫描树（UM-13），
    # CI 作业覆盖比对上游两棵树的工作流与 ci_job_coverage.json 登记表（UM-14）。
    report: dict[str, Any] = {}
    try:
        template = load_request_template(root)
        report["template_validity"] = template_validity(root, request, template)
    except UpstreamMergeError as error:
        report["template_validity"] = {"status": "failed", "findings": [str(error)]}
    if report["template_validity"]["status"] != "passed":
        blockers.append(
            "模板有效性检查失败：" + "；".join(report["template_validity"].get("findings", []))
        )
    report["tool_bundle_disturbance"] = tool_bundle_disturbance(root, merge_base_value, upstream["commit"])
    upstream_changed_path_count = len(changed_paths(root, merge_base_value, upstream["commit"]))
    fork_snapshot: dict[str, Any] | None = None
    scanner_deferred_reason: str | None = None

    temporary_root = Path(tempfile.mkdtemp(prefix="sub2api-upstream-preflight-"))
    worktree = temporary_root / "worktree"
    scanner_snapshot: dict[str, Any] | None = None
    conflict_paths: list[str] = []
    merge_exit_code: int | None = None
    upstream_scan: dict[str, Any] | None = None
    try:
        try:
            upstream_scan = _preflight_upstream_sink_scan(
                root, fork_head, upstream["commit"], temporary_root, offline_env
            )
        except (OSError, UpstreamMergeError) as error:
            checks.append({"id": "upstream-sink-scan", "status": "failed", "error": str(error)})
        run_git(root, "worktree", "add", "--detach", str(worktree), fork_head)
        completed = run_git(
            worktree,
            "-c",
            "user.name=Sub2API Upstream Preflight",
            "-c",
            "user.email=upstream-preflight@sub2apiplus.invalid",
            "-c",
            "rerere.enabled=true",
            "-c",
            "rerere.autoupdate=false",
            "merge",
            "--no-ff",
            "--no-commit",
            upstream["commit"],
            check=False,
        )
        merge_exit_code = completed.returncode
        conflict_paths = sorted({entry["path"] for entry in unmerged_entries(worktree)})
        if conflict_paths:
            blockers.append(f"试合并存在 {len(conflict_paths)} 个冲突文件")
            scanner_deferred_reason = "试合并存在冲突；扫描器覆盖在冲突解决后由 U-2 surface-scan 承担"
            run_git(worktree, "merge", "--abort", check=False)
        elif completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            blockers.append("试合并失败且没有可审计冲突：" + detail)
            scanner_deferred_reason = "试合并失败"
        else:
            # fork 树快照来自主仓库自身：预检开头已确认它是干净的受维护分支 HEAD。
            fork_output = temporary_root / "fork-source-to-sink.json"
            try:
                run_egress_snapshot(root, fork_output, env=offline_env)
                fork_snapshot = expect_object(
                    load_json(fork_output, "preflight fork snapshot"),
                    "preflight fork snapshot",
                )
            except (OSError, UpstreamMergeError) as error:
                checks.append({"id": "egressscan-fork", "status": "failed", "error": str(error)})
                blockers.append("fork 发送面快照失败：" + str(error))
            # 临时提交只存在于 detached worktree，便于让 scanner/build 看到干净树。
            run_git(
                worktree,
                "-c",
                "user.name=Sub2API Upstream Preflight",
                "-c",
                "user.email=upstream-preflight@sub2apiplus.invalid",
                "commit",
                "--no-verify",
                "-m",
                f"preflight: Sub2API {upstream['tag']}",
            )
            merge_commit = rev_parse(worktree, "HEAD^{commit}")
            merge_tree = commit_tree(worktree, merge_commit)
            # scanner 失败必须成为预检阻断，不能把“未识别发送点”当作通过。
            scanner_output = temporary_root / "source-to-sink.json"
            try:
                run_egress_snapshot(worktree, scanner_output, env=offline_env)
                scanner_snapshot = expect_object(
                    load_json(scanner_output, "preflight source-to-sink snapshot"),
                    "preflight source-to-sink snapshot",
                )
                sink_count = scanner_snapshot.get("sink_count")
                if isinstance(sink_count, bool) or not isinstance(sink_count, int) or sink_count < 0:
                    raise UpstreamMergeError("egressscan 预检输出 sink_count 非法")
                checks.append(
                    {
                        "id": "egressscan",
                        "status": "passed",
                        "source_commit": merge_commit,
                        "source_tree": merge_tree,
                        "sink_count": sink_count,
                        "snapshot_sha256": sha256_file(scanner_output),
                    }
                )
            except (OSError, UpstreamMergeError) as error:
                checks.append({"id": "egressscan", "status": "failed", "error": str(error)})
                blockers.append("egressscan 预检失败：" + str(error))

            command_specs = [
                ("go-build", ["go", "build", "./..."], worktree / "backend"),
                ("go-vet", ["go", "vet", "./..."], worktree / "backend"),
                (
                    "official-egress-tests",
                    ["go", "test", "./internal/officialegress/...", "-count=1"],
                    worktree / "backend",
                ),
            ]
            for check_id, argv, cwd in command_specs:
                if not (cwd / "go.mod").is_file():
                    checks.append(
                        {
                            "id": check_id,
                            "argv": argv,
                            "cwd": str(cwd),
                            "status": "skipped",
                            "reason": "预检 worktree 不含 backend/go.mod",
                        }
                    )
                    blockers.append(f"{check_id} 预检无法执行：缺少 backend/go.mod")
                    continue
                result = _preflight_check(check_id, argv, cwd, env=offline_env)
                checks.append(result)
                if result["status"] != "passed":
                    blockers.append(f"{check_id} 预检失败")
    finally:
        run_git(root, "worktree", "remove", "--force", str(worktree), check=False)
        run_git(root, "worktree", "prune", check=False)
        shutil.rmtree(temporary_root, ignore_errors=True)

    coverage = scanner_coverage(
        upstream_scan,
        candidate_sink_diff(fork_snapshot, scanner_snapshot, deferred_reason=scanner_deferred_reason),
    )
    if coverage["status"] == "blocked":
        blockers.append(
            f"上游新增 {coverage['unregistered_added_count']} 个发送点尚未在主干预先登记"
            f"（其中缺分类规则 {len(coverage['unclassified_sinks'])} 个），须在 plan-create 前登记并重封基线"
        )
    elif coverage["status"] == "failed":
        blockers.append("上游发送点试扫描未得出结论：" + str(coverage.get("error") or coverage.get("reason") or "分类不完整"))
    # UM-14：上游新增或改动的 CI 作业必须在登记表里有本机覆盖方式，否则建 Plan 前先处置。
    ci_jobs_report = ci_job_coverage(root, merge_base_value, upstream["commit"], fork_head)
    if ci_jobs_report["status"] == "blocked":
        listed = "、".join(ci_jobs_report["unregistered_jobs"] + ci_jobs_report["fork_unregistered_jobs"])
        blockers.append(f"CI 作业未登记本机覆盖方式（纳入本机门禁或登记为只在 CI 跑）：{listed}")
    elif ci_jobs_report["status"] == "failed":
        blockers.append("上游 CI 作业差异未得出结论：" + ci_jobs_report.get("error", ""))
    result = _stage_document(
        # preflight 没有 LoadedPlan，使用显式输入摘要构造独立 envelope。
        # 该摘要不参与任何正式 U-0 身份绑定。
        type("PreflightPlan", (), {"plan_id": request["plan_id"], "identity": sha256_bytes(canonical_bytes(request))})(),
        PREFLIGHT_SCHEMA,
        {
            "request": file_binding(request_path.resolve(strict=True)),
            "repository": {
                "managed_ref": managed_ref,
                "fork_head": fork_head,
                "upstream_commit": upstream["commit"],
                "merge_base": merge_base_value,
            },
            "merge_exit_code": merge_exit_code,
            "conflict_paths": conflict_paths,
            "covered_tags": covered_tags_report,
            "checks": checks,
            "report": {
                "conflict_closure": conflict_closure(conflict_paths, upstream_changed_path_count),
                "template_validity": report["template_validity"],
                "tool_bundle_disturbance": report["tool_bundle_disturbance"],
                "freeze_coverage": _preflight_freeze_coverage(
                    root, merge_base_value, upstream["commit"], conflict_paths, blockers
                ),
                "scanner_coverage": coverage,
                "ci_job_coverage": ci_jobs_report,
            },
            "scanner_snapshot": (
                {
                    "sink_count": scanner_snapshot.get("sink_count"),
                    "snapshot_sha256": next(
                        (
                            item["snapshot_sha256"]
                            for item in checks
                            if item.get("id") == "egressscan" and item.get("status") == "passed"
                        ),
                        "",
                    ),
                }
                if scanner_snapshot is not None
                else None
            ),
            "blockers": sorted(set(blockers)),
            "non_authoritative": True,
            "result": "ready" if not blockers else "blocked",
        },
    )
    if output_path is not None:
        write_json_once(output_path, result)
    return result


def _git_blob_digest(repository_root: Path, commit: str, relative: str) -> str | None:
    """读取提交中路径对象内容的 SHA-256；删除或不存在的路径返回 null。"""

    object_result = run_git(
        repository_root,
        "rev-parse",
        "--verify",
        f"{commit}:{relative}",
        check=False,
    )
    if object_result.returncode != 0:
        return None
    object_id = object_result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", object_id):
        return None
    object_type_result = run_git(
        repository_root,
        "cat-file",
        "-t",
        object_id,
        check=False,
    )
    if object_type_result.returncode != 0:
        return None
    object_type = object_type_result.stdout.strip()
    if object_type not in {"blob", "tree", "commit", "tag"}:
        return None
    content = subprocess.run(
        ["git", "cat-file", object_type, object_id],
        cwd=repository_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if content.returncode != 0:
        return None
    return sha256_bytes(content.stdout)


def _validate_transition_node(
    path: Path,
    *,
    expected_current: str | None = None,
    _seen: set[Path] | None = None,
) -> dict[str, Any]:
    # 必须先检查原始路径，再做 resolve；否则符号链接会被解析成普通文件而绕过
    # evidence 边界检查。
    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError("SourceTransition 必须是可信普通文件")
    resolved_path = path.resolve(strict=True)
    seen = _seen if _seen is not None else set()
    if resolved_path in seen:
        raise UpstreamMergeError("SourceTransition predecessor 存在循环")
    seen.add(resolved_path)
    document = expect_object(load_json(resolved_path, "SourceTransition"), "SourceTransition")
    required = {
        "schema_version",
        "base_commit",
        "current_commit",
        "base_tree",
        "current_tree",
        "chain_sequence",
        "predecessor_register",
        "entries",
        "entry_count",
        "reason_policy",
        "result",
        "identity_sha256",
    }
    expect_exact_fields(document, required, "SourceTransition")
    if document["schema_version"] != SOURCE_TRANSITION_SCHEMA:
        raise UpstreamMergeError("SourceTransition schema_version 非法")
    validate_identity(document, "SourceTransition")
    expect_git_object(document["base_commit"], "SourceTransition.base_commit")
    expect_git_object(document["current_commit"], "SourceTransition.current_commit")
    expect_git_object(document["base_tree"], "SourceTransition.base_tree")
    expect_git_object(document["current_tree"], "SourceTransition.current_tree")
    sequence = document["chain_sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise UpstreamMergeError("SourceTransition.chain_sequence 非法")
    if expected_current is not None and document["current_commit"] != expected_current:
        raise UpstreamMergeError("SourceTransition 链尾前序提交不连续")
    entries = document["entries"]
    entry_count = document["entry_count"]
    if (
        isinstance(entry_count, bool)
        or not isinstance(entry_count, int)
        or entry_count <= 0
        or not isinstance(entries, list)
        or entry_count != len(entries)
    ):
        raise UpstreamMergeError("SourceTransition entries/entry_count 不一致")
    paths: list[str] = []
    for index, raw in enumerate(entries):
        label = f"SourceTransition.entries[{index}]"
        item = expect_object(raw, label)
        expect_exact_fields(
            item,
            {
                "path",
                "old_path",
                "status",
                "predecessor_sha256",
                "current_sha256",
                "reason",
            },
            label,
        )
        relative = safe_relative_path(item["path"], f"{label}.path")
        old_relative = item["old_path"]
        if not isinstance(old_relative, str):
            raise UpstreamMergeError(f"{label}.old_path 必须是字符串")
        if old_relative:
            old_relative = safe_relative_path(old_relative, f"{label}.old_path")
        status = validate_string_enum(
            item["status"], {"A", "M", "D", "R", "C", "T"}, f"{label}.status"
        )
        for field in ("predecessor_sha256", "current_sha256"):
            value = item[field]
            if value is not None:
                expect_sha256(value, f"{label}.{field}")
        if len(expect_string(item["reason"], f"{label}.reason")) < 12:
            raise UpstreamMergeError(f"{label}.reason 必须说明变化来源")
        predecessor_sha = item["predecessor_sha256"]
        current_sha = item["current_sha256"]
        if status == "A" and (predecessor_sha is not None or current_sha is None):
            raise UpstreamMergeError(f"{label} 新增文件必须仅有 current_sha256")
        if status == "D" and (predecessor_sha is None or current_sha is not None):
            raise UpstreamMergeError(f"{label} 删除文件必须仅有 predecessor_sha256")
        if status in {"M", "R", "C", "T"} and (
            predecessor_sha is None or current_sha is None
        ):
            raise UpstreamMergeError(f"{label} {status} 必须同时有前后摘要")
        if status in {"R", "C"} and not old_relative:
            raise UpstreamMergeError(f"{label} 重命名/复制必须记录 old_path")
        if status not in {"R", "C"} and old_relative:
            raise UpstreamMergeError(f"{label} 非重命名条目不得记录 old_path")
        paths.append(relative)
    if paths != sorted(set(paths)):
        raise UpstreamMergeError("SourceTransition entries 路径必须排序且不得重复")
    predecessor = document["predecessor_register"]
    if predecessor is not None:
        validated_predecessor = validate_file_binding(
            predecessor,
            "SourceTransition.predecessor_register",
        )
        if sequence < 2:
            raise UpstreamMergeError("SourceTransition 有 predecessor 时 chain_sequence 必须大于 1")
        predecessor_path = Path(validated_predecessor["path"])
        if str(predecessor_path) != str(predecessor_path.resolve(strict=True)):
            raise UpstreamMergeError("SourceTransition predecessor 路径必须规范化")
        if predecessor_path.is_symlink() or not predecessor_path.is_file():
            raise UpstreamMergeError("SourceTransition predecessor 不是可信普通文件")
        prior = _validate_transition_node(
            predecessor_path,
            expected_current=document["base_commit"],
            _seen=seen,
        )
        if prior["chain_sequence"] + 1 != sequence:
            raise UpstreamMergeError("SourceTransition chain_sequence 不连续")
    elif sequence != 1:
        raise UpstreamMergeError("SourceTransition 无 predecessor 时 chain_sequence 必须为 1")
    expect_string(document["reason_policy"], "SourceTransition.reason_policy")
    if document["result"] != "generated":
        raise UpstreamMergeError("SourceTransition result 非法")
    return document


def _validate_transition_git_node(
    repository_root: Path,
    path: Path,
    document: dict[str, Any],
    seen: set[Path],
) -> None:
    """复算 transition 当前节点及全部 predecessor 节点的 Git 事实。"""

    resolved = path.resolve(strict=True)
    if resolved in seen:
        raise UpstreamMergeError("SourceTransition predecessor 存在循环")
    seen.add(resolved)
    for commit, label in (
        (document["base_commit"], "base_commit"),
        (document["current_commit"], "current_commit"),
    ):
        if git_output(repository_root, "cat-file", "-t", commit) != "commit":
            raise UpstreamMergeError(f"SourceTransition {label} 不是 commit")
    if commit_tree(repository_root, document["base_commit"]) != document["base_tree"]:
        raise UpstreamMergeError("SourceTransition base_tree 漂移")
    if commit_tree(repository_root, document["current_commit"]) != document["current_tree"]:
        raise UpstreamMergeError("SourceTransition current_tree 漂移")
    ancestry = run_git(
        repository_root,
        "merge-base",
        "--is-ancestor",
        document["base_commit"],
        document["current_commit"],
        check=False,
    )
    if ancestry.returncode != 0:
        raise UpstreamMergeError("SourceTransition current_commit 不是 base_commit 的后继")
    expected_changes = changed_paths(
        repository_root,
        document["base_commit"],
        document["current_commit"],
    )
    if not expected_changes:
        raise UpstreamMergeError("SourceTransition 不得记录无文件变化的提交区间")
    actual_changes = [
        {
            "status": item["status"],
            "path": item["path"],
            "old_path": item["old_path"],
        }
        for item in document["entries"]
    ]
    if actual_changes != expected_changes:
        raise UpstreamMergeError("SourceTransition entries 未完整覆盖 Git 差异")
    for item in document["entries"]:
        predecessor_path = item["old_path"] or item["path"]
        predecessor_sha = (
            None
            if item["status"] == "A"
            else _git_blob_digest(
                repository_root,
                document["base_commit"],
                predecessor_path,
            )
        )
        current_sha = (
            None
            if item["status"] == "D"
            else _git_blob_digest(
                repository_root,
                document["current_commit"],
                item["path"],
            )
        )
        if item["predecessor_sha256"] != predecessor_sha or item["current_sha256"] != current_sha:
            raise UpstreamMergeError(f"SourceTransition 文件摘要漂移：{item['path']}")
    predecessor_binding = document["predecessor_register"]
    if predecessor_binding is not None:
        predecessor_path = Path(predecessor_binding["path"])
        if predecessor_path.is_symlink() or not predecessor_path.is_file():
            raise UpstreamMergeError("SourceTransition predecessor 不是可信普通文件")
        prior_path = predecessor_path.resolve(strict=True)
        if file_binding(prior_path) != predecessor_binding:
            raise UpstreamMergeError("SourceTransition predecessor 绑定内容或路径漂移")
        prior_document = _validate_transition_node(
            prior_path,
            expected_current=document["base_commit"],
        )
        _validate_transition_git_node(repository_root, prior_path, prior_document, seen)


def _assert_no_interval_bound_receipts(
    repository_root: Path,
    before: str,
    after: str,
    entries: list[dict[str, Any]],
) -> None:
    """拒绝把绑定本区间的收据记进 transition。

    successor／transition 收据若在它们描述的提交区间内被提交，就会出现
    “收据摘要进入 transition，transition 摘要又被收据绑定”的循环。规则固定为：
    先封存源码提交，再单独提交收据；这里只在生成阶段拦截，历史节点保持只读。
    """

    for item in entries:
        relative = item["path"]
        if item["status"] == "D" or not relative.startswith("docs/egress/maintenance/"):
            continue
        if not relative.endswith(".json"):
            continue
        content = subprocess.run(
            ["git", "cat-file", "blob", f"{after}:{relative}"],
            cwd=repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if content.returncode != 0:
            continue
        try:
            document = json.loads(content.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        for key in ("base_commit", "current_commit"):
            bound = document.get(key)
            if not isinstance(bound, str) or not re.fullmatch(r"[0-9a-f]{40}", bound) or bound == before:
                continue
            inside = run_git(repository_root, "merge-base", "--is-ancestor", before, bound, check=False)
            reaches_after = run_git(repository_root, "merge-base", "--is-ancestor", bound, after, check=False)
            if inside.returncode == 0 and reaches_after.returncode == 0:
                raise UpstreamMergeError(
                    "SourceTransition 区间内包含绑定本区间的收据，存在自引用循环："
                    f"{relative}（{key}={bound[:12]}）；请先封存源码提交，再单独提交收据"
                )


def generate_source_transition(
    repository_root: Path,
    before_commit: str,
    after_commit: str,
    output_path: Path | None = None,
    *,
    predecessor_register: Path | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """从 Git 差异生成单一追加式 source-transition 链尾节点。"""

    root = assert_git_repository(repository_root)
    before = expect_git_object(before_commit, "source transition before commit")
    after = expect_git_object(after_commit, "source transition after commit")
    if before == after:
        raise UpstreamMergeError("source-transition before/after 不得相同")
    for commit, label in ((before, "before"), (after, "after")):
        if git_output(root, "cat-file", "-t", commit) != "commit":
            raise UpstreamMergeError(f"source-transition {label} 必须是 commit")
    ancestry = run_git(
        root,
        "merge-base",
        "--is-ancestor",
        before,
        after,
        check=False,
    )
    if ancestry.returncode != 0:
        raise UpstreamMergeError("source-transition after commit 必须是 before 的后继")
    # 让 Git 自己确认两个对象存在，并固定提交树摘要。
    base_tree = commit_tree(root, before)
    current_tree = commit_tree(root, after)
    entries: list[dict[str, Any]] = []
    default_reason = reason or "由 Git 差异自动生成；高风险条目须在评审中补充具体影响。"
    for change in changed_paths(root, before, after):
        relative = change["path"]
        old_relative = change.get("old_path") or relative
        predecessor = (
            None
            if change["status"] == "A"
            else _git_blob_digest(root, before, old_relative)
        )
        current = (
            None
            if change["status"] == "D"
            else _git_blob_digest(root, after, relative)
        )
        entries.append(
            {
                "path": relative,
                "old_path": change.get("old_path", ""),
                "status": change["status"],
                "predecessor_sha256": predecessor,
                "current_sha256": current,
                "reason": f"{default_reason} path={relative}",
            }
        )
    entries.sort(key=lambda item: item["path"])
    if not entries:
        raise UpstreamMergeError("source-transition before/after 没有文件变化")
    _assert_no_interval_bound_receipts(root, before, after, entries)
    sequence = 1
    predecessor_binding: dict[str, Any] | None = None
    if predecessor_register is not None:
        if not predecessor_register.is_absolute():
            raise UpstreamMergeError("source-transition 前序登记表必须使用绝对路径")
        if predecessor_register.is_symlink() or not predecessor_register.is_file():
            raise UpstreamMergeError("source-transition 前序登记表必须是普通文件")
        prior = predecessor_register.resolve(strict=True)
        prior_document = _validate_transition_node(prior)
        _validate_transition_git_node(root, prior, prior_document, set())
        if prior_document["current_commit"] != before:
            raise UpstreamMergeError("source-transition 前序登记表与 before commit 不连续")
        sequence = prior_document["chain_sequence"] + 1
        predecessor_binding = file_binding(prior)
    document = bind_identity(
        {
            "schema_version": SOURCE_TRANSITION_SCHEMA,
            "base_commit": before,
            "current_commit": after,
            "base_tree": base_tree,
            "current_tree": current_tree,
            "chain_sequence": sequence,
            "predecessor_register": predecessor_binding,
            "entries": entries,
            "entry_count": len(entries),
            "reason_policy": (
                "每个路径由 Git 差异确定；predecessor/current 摘要不可手填。"
                "reason 可统一提供，但官方 Persona、wire、selector 和共享控制面仍需人工补充。"
            ),
            "result": "generated",
        }
    )
    if output_path is not None:
        normalized_output = Path(os.path.normpath(str(output_path)))
        if not output_path.is_absolute() or output_path != normalized_output:
            raise UpstreamMergeError("source-transition 输出必须是规范绝对路径")
        write_json_once(output_path, document)
    return document


def validate_source_transition(
    repository_root: Path,
    transition_path: Path,
) -> dict[str, Any]:
    """校验 transition 链尾并复算两端文件摘要。"""

    root = assert_git_repository(repository_root)
    if not transition_path.is_absolute():
        raise UpstreamMergeError("SourceTransition 路径必须是绝对路径")
    if transition_path.is_symlink() or not transition_path.is_file():
        raise UpstreamMergeError("SourceTransition 必须是可信普通文件")
    path = transition_path.resolve(strict=True)
    document = _validate_transition_node(path)
    _validate_transition_git_node(root, path, document, set())
    return {
        "result": "valid",
        "path": str(path),
        "chain_sequence": document["chain_sequence"],
        "entry_count": document["entry_count"],
        "current_commit": document["current_commit"],
    }


def _write_log(plan: LoadedPlan, relative: str, raw: str) -> dict[str, Any]:
    path = resolve_within(plan.evidence_root, relative, relative)
    write_once(path, raw.encode("utf-8"))
    return artifact_binding(plan.evidence_root, path)


def start_merge(plan: LoadedPlan) -> dict[str, Any]:
    """创建 detached 隔离 worktree，并只合入计划指定 commit。"""

    if plan.worktree.exists():
        raise UpstreamMergeError(f"隔离 worktree 已存在：{plan.worktree}")
    if rev_parse(plan.repository_root, f"{plan.managed_ref}^{{commit}}") != plan.fork_head:
        raise UpstreamMergeError("受维护分支已偏离计划 fork HEAD")
    run_git(
        plan.repository_root,
        "worktree",
        "add",
        "--detach",
        str(plan.worktree),
        plan.fork_head,
    )
    worktree = _worktree_root(plan)
    completed = run_git(
        worktree,
        "-c",
        "user.name=Sub2API Upstream Merge",
        "-c",
        "user.email=upstream-merge@sub2apiplus.invalid",
        "merge",
        "--no-ff",
        "--no-commit",
        plan.upstream_commit,
        check=False,
    )
    stages = unmerged_entries(worktree)
    conflict_paths = sorted({entry["path"] for entry in stages})
    if completed.returncode == 0 and conflict_paths:
        raise UpstreamMergeError("Git 报告合并成功但仍存在未解决冲突")
    if completed.returncode != 0 and not conflict_paths:
        raise UpstreamMergeError(
            "Git 合并失败但没有可审计冲突："
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    merge_head = rev_parse(worktree, "MERGE_HEAD^{commit}")
    if merge_head != plan.upstream_commit or rev_parse(worktree, "HEAD^{commit}") != plan.fork_head:
        raise UpstreamMergeError("隔离合并父提交与计划不一致")
    stdout_binding = _write_log(plan, "u1/merge.stdout.txt", completed.stdout)
    stderr_binding = _write_log(plan, "u1/merge.stderr.txt", completed.stderr)
    document = _stage_document(
        plan,
        MERGE_START_SCHEMA,
        {
            "fork_head": plan.fork_head,
            "upstream_commit": plan.upstream_commit,
            "merge_base": plan.document["repository"]["merge_base"],
            "worktree": str(worktree),
            "merge_exit_code": completed.returncode,
            "status": "conflicts_pending" if conflict_paths else "ready_to_seal",
            "conflict_paths": conflict_paths,
            "conflict_stages": stages,
            "stdout": stdout_binding,
            "stderr": stderr_binding,
        },
    )
    write_json_once(plan.output_path("merge_start"), document)
    return document


def _recompute_merge_start(
    plan: LoadedPlan,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在全新隔离 worktree 重做原始合并，复算冲突分母。"""

    start = expected if expected is not None else _load_merge_start(plan)
    expected_identity = {
        "fork_head": plan.fork_head,
        "upstream_commit": plan.upstream_commit,
        "merge_base": plan.document["repository"]["merge_base"],
        "worktree": str(plan.worktree.resolve(strict=True)),
    }
    for field, value in expected_identity.items():
        if start.get(field) != value:
            raise UpstreamMergeError(f"MergeStart {field} 与 U-0 计划不一致")

    with _temporary_detached_worktree(plan, plan.fork_head) as worktree:
        completed = run_git(
            worktree,
            "-c",
            "user.name=Sub2API Upstream Merge Replay",
            "-c",
            "user.email=upstream-merge-replay@sub2apiplus.invalid",
            "merge",
            "--no-ff",
            "--no-commit",
            plan.upstream_commit,
            check=False,
        )
        stages = unmerged_entries(worktree)
        conflict_paths = sorted({item["path"] for item in stages})
        if completed.returncode == 0 and conflict_paths:
            raise UpstreamMergeError("独立重放中 Git 报告合并成功但仍有冲突")
        if completed.returncode != 0 and not conflict_paths:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise UpstreamMergeError(f"独立重放合并失败且无可审计冲突：{detail}")
        if (
            rev_parse(worktree, "HEAD^{commit}") != plan.fork_head
            or rev_parse(worktree, "MERGE_HEAD^{commit}") != plan.upstream_commit
        ):
            raise UpstreamMergeError("独立重放的合并父提交与计划不一致")
        reconstructed = {
            "merge_exit_code": completed.returncode,
            "status": "conflicts_pending" if conflict_paths else "ready_to_seal",
            "conflict_paths": conflict_paths,
            "conflict_stages": stages,
        }

    for field, value in reconstructed.items():
        if start.get(field) != value:
            raise UpstreamMergeError(
                f"MergeStart {field} 无法由 fork HEAD 与 upstream commit 独立复算"
            )
    return reconstructed


def _load_conflict_input(
    path: Path,
    plan: LoadedPlan,
    expected_paths: list[str],
) -> dict[str, dict[str, Any]]:
    document = expect_object(load_json(path, "ConflictResolutionInput"), "ConflictResolutionInput")
    expect_exact_fields(
        document,
        {
            "schema_version",
            "plan_id",
            "plan_identity_sha256",
            "merge_start_sha256",
            "resolutions",
            "identity_sha256",
        },
        "ConflictResolutionInput",
    )
    if document.get("schema_version") != CONFLICT_INPUT_SCHEMA:
        raise UpstreamMergeError("ConflictResolutionInput schema_version 非法")
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError("ConflictResolutionInput 计划身份不一致")
    if document.get("merge_start_sha256") != sha256_file(plan.output_path("merge_start")):
        raise UpstreamMergeError("ConflictResolutionInput 未绑定本次 MergeStart")
    validate_identity(document, "ConflictResolutionInput")
    values = document.get("resolutions")
    if not isinstance(values, list):
        raise UpstreamMergeError("ConflictResolutionInput.resolutions 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(values):
        label = f"ConflictResolutionInput.resolutions[{index}]"
        item = expect_object(raw, label)
        expect_exact_fields(item, {"path", "resolution", "rationale"}, label)
        relative = safe_relative_path(item.get("path"), f"{label}.path")
        resolution = validate_string_enum(
            item.get("resolution"), RESOLUTION_KINDS, f"{label}.resolution"
        )
        rationale = expect_string(item.get("rationale"), f"{label}.rationale")
        if len(rationale) < 12:
            raise UpstreamMergeError(f"{label}.rationale 必须说明具体取舍")
        if relative in result:
            raise UpstreamMergeError(f"冲突路径重复处置：{relative}")
        result[relative] = {
            "path": relative,
            "resolution": resolution,
            "rationale": rationale,
        }
    if sorted(result) != expected_paths:
        raise UpstreamMergeError(
            f"冲突处置未闭合：expected={expected_paths} actual={sorted(result)}"
        )
    return result


def _infer_conflict_resolution(
    worktree: Path,
    conflict_stages: list[dict[str, Any]],
    relative: str,
) -> str:
    """按 index 对象事实推断处置类型；整文件取某一侧才是 fork/upstream，其余为 manual。"""

    resolved_state = _index_path_state(worktree, relative)
    for stage, name in ((2, "fork"), (3, "upstream")):
        try:
            staged = _conflict_stage_state(worktree, conflict_stages, relative, stage)
        except UpstreamMergeError:
            continue
        if resolved_state == staged:
            return name
    return "manual"


def _index_path_state(worktree: Path, relative: str) -> dict[str, Any]:
    completed = run_git(worktree, "ls-files", "-s", "--", relative, check=False)
    rows = [line for line in completed.stdout.splitlines() if line]
    if not rows:
        return {"existence": "absent", "mode": "", "object_id": "", "sha256": ""}
    if len(rows) != 1:
        raise UpstreamMergeError(f"已解决冲突路径仍有多阶段 index：{relative}")
    metadata, actual_path = rows[0].split("\t", 1)
    mode, object_id, stage = metadata.split(" ")
    if actual_path != relative or stage != "0":
        raise UpstreamMergeError(f"已解决冲突 index 身份异常：{relative}")
    blob = subprocess.check_output(["git", "cat-file", "blob", object_id], cwd=worktree)
    return {
        "existence": "present",
        "mode": mode,
        "object_id": expect_git_object(object_id, "resolved blob"),
        "sha256": sha256_bytes(blob),
    }


def _conflict_stage_state(
    repository_root: Path,
    stages: list[dict[str, Any]],
    relative: str,
    stage: int,
) -> dict[str, Any]:
    matches = [
        item
        for item in stages
        if item.get("path") == relative and item.get("stage") == stage
    ]
    if not matches:
        return {"existence": "absent", "mode": "", "object_id": "", "sha256": ""}
    if len(matches) != 1:
        raise UpstreamMergeError(f"冲突路径阶段身份不唯一：{relative} stage={stage}")
    entry = matches[0]
    object_id = expect_git_object(entry.get("object_id"), "conflict stage object")
    mode = expect_string(entry.get("mode"), "conflict stage mode")
    blob = subprocess.check_output(
        ["git", "cat-file", "blob", object_id],
        cwd=repository_root,
    )
    return {
        "existence": "present",
        "mode": mode,
        "object_id": object_id,
        "sha256": sha256_bytes(blob),
    }


def seal_merge(plan: LoadedPlan, conflict_input: Path | None) -> dict[str, Any]:
    """在全部冲突有理由且 index 闭合后生成真实双父 merge commit。"""

    start = _load_merge_start(plan)
    _recompute_merge_start(plan, start)
    worktree = _worktree_root(plan)
    if rev_parse(worktree, "HEAD^{commit}") != plan.fork_head:
        raise UpstreamMergeError("merge seal 前 HEAD 已漂移")
    if rev_parse(worktree, "MERGE_HEAD^{commit}") != plan.upstream_commit:
        raise UpstreamMergeError("merge seal 前 MERGE_HEAD 已漂移")
    remaining = unmerged_entries(worktree)
    if remaining:
        raise UpstreamMergeError(
            "仍有未解决冲突：" + ", ".join(sorted({item["path"] for item in remaining}))
        )
    conflict_paths = list(start["conflict_paths"])
    if conflict_paths:
        if conflict_input is None:
            raise UpstreamMergeError("存在冲突时必须提供 ConflictResolutionInput")
        decisions = _load_conflict_input(conflict_input, plan, conflict_paths)
        decision_binding: dict[str, Any] | None = file_binding(conflict_input.resolve(strict=True))
    else:
        if conflict_input is not None:
            raise UpstreamMergeError("无冲突合并不得附带伪造的 ConflictResolutionInput")
        decisions = {}
        decision_binding = None
    unstaged = run_git(worktree, "diff", "--quiet", check=False)
    if unstaged.returncode != 0:
        raise UpstreamMergeError("merge seal 前存在未暂存修改")
    untracked = git_output(worktree, "ls-files", "--others", "--exclude-standard")
    if untracked:
        raise UpstreamMergeError("merge seal 前存在未登记文件")
    candidate_tree = git_output(worktree, "write-tree")
    expect_git_object(candidate_tree, "merge candidate tree")
    validate_protected_objects(
        worktree,
        candidate_tree,
        plan.document["repository"]["protected_objects"],
    )
    resolutions: list[dict[str, Any]] = []
    for relative in conflict_paths:
        resolved_state = _index_path_state(worktree, relative)
        resolution = decisions[relative]["resolution"]
        if resolution in {"fork", "upstream"}:
            expected_stage = 2 if resolution == "fork" else 3
            expected_state = _conflict_stage_state(
                worktree,
                start["conflict_stages"],
                relative,
                expected_stage,
            )
            if resolved_state != expected_state:
                actual = _infer_conflict_resolution(worktree, start["conflict_stages"], relative)
                raise UpstreamMergeError(
                    f"{relative} 声明使用 {resolution} 处置，但实际 index 对象不一致；"
                    f"按当前 index 应为 {actual}"
                )
        resolutions.append({**decisions[relative], "resolved_state": resolved_state})
    conflict_document = _stage_document(
        plan,
        CONFLICT_LEDGER_SCHEMA,
        {
            "merge_start": stage_binding(plan, "merge_start"),
            "conflict_count": len(conflict_paths),
            "conflict_paths": conflict_paths,
            "resolution_input": decision_binding,
            "resolutions": resolutions,
            "result": "closed",
        },
    )
    write_json_once(plan.output_path("conflict_ledger"), conflict_document)
    run_git(
        worktree,
        "-c",
        "user.name=Sub2API Upstream Merge",
        "-c",
        "user.email=upstream-merge@sub2apiplus.invalid",
        "commit",
        "--no-verify",
        "-m",
        f"merge: integrate Sub2API {plan.document['upstream']['tag']}",
    )
    merge_commit = rev_parse(worktree, "HEAD^{commit}")
    actual_tree = commit_tree(worktree, merge_commit)
    if actual_tree != candidate_tree:
        raise UpstreamMergeError("merge commit tree 与封存候选 tree 不一致")
    parent_line = git_output(worktree, "rev-list", "--parents", "-n", "1", merge_commit).split()
    parents = parent_line[1:]
    if parents != [plan.fork_head, plan.upstream_commit]:
        raise UpstreamMergeError(f"merge commit 父提交不闭合：{parents}")
    changed = changed_paths(worktree, plan.fork_head, merge_commit)
    document = _stage_document(
        plan,
        MERGE_CANDIDATE_SCHEMA,
        {
            "merge_start": stage_binding(plan, "merge_start"),
            "conflict_ledger": stage_binding(plan, "conflict_ledger"),
            "parents": parents,
            "merge_commit": merge_commit,
            "candidate_tree": actual_tree,
            "changed_paths": changed,
            "protected_objects_unchanged": True,
        },
    )
    write_json_once(plan.output_path("merge_candidate"), document)
    return document


def _load_source_change_input(
    path: Path,
    plan: LoadedPlan,
    merge_commit: str,
    expected_paths: list[str],
    *,
    base_source_commit: str | None = None,
) -> dict[str, Any]:
    document = expect_object(load_json(path, "SourceChangeInput"), "SourceChangeInput")
    expected_fields = {
        "schema_version",
        "plan_id",
        "plan_identity_sha256",
        "merge_commit",
        "entries",
        "identity_sha256",
    }
    optional_fields = {"base_source_commit", "revision"}
    actual_fields = set(document)
    if actual_fields - (expected_fields | optional_fields) or expected_fields - actual_fields:
        raise UpstreamMergeError(
            "SourceChangeInput 字段不闭合："
            f"缺失={sorted(expected_fields - actual_fields)}，"
            f"多余={sorted(actual_fields - expected_fields)}"
        )
    if document.get("schema_version") != SOURCE_CHANGE_INPUT_SCHEMA:
        raise UpstreamMergeError("SourceChangeInput schema_version 非法")
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError("SourceChangeInput 计划身份不一致")
    if document.get("merge_commit") != merge_commit:
        raise UpstreamMergeError("SourceChangeInput merge_commit 不一致")
    declared_base = document.get("base_source_commit")
    if declared_base is not None:
        expect_git_object(declared_base, "SourceChangeInput.base_source_commit")
    if base_source_commit is not None:
        if declared_base is not None and declared_base != base_source_commit:
            raise UpstreamMergeError("SourceChangeInput base_source_commit 不一致")
        if declared_base is None:
            # 旧格式仍可用于首轮之后的修复，但调用方必须在收据中记录实际父提交。
            pass
    validate_identity(document, "SourceChangeInput")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise UpstreamMergeError("SourceChangeInput.entries 必须是非空数组")
    paths: list[str] = []
    for index, raw in enumerate(entries):
        label = f"SourceChangeInput.entries[{index}]"
        item = expect_object(raw, label)
        expect_exact_fields(item, {"path", "reason"}, label)
        paths.append(safe_relative_path(item.get("path"), f"{label}.path"))
        if len(expect_string(item.get("reason"), f"{label}.reason")) < 12:
            raise UpstreamMergeError(f"{label}.reason 必须说明为何属于本 changeset")
    if paths != sorted(set(paths)) or paths != expected_paths:
        raise UpstreamMergeError(
            f"SourceChangeInput 未闭合当前变化：expected={expected_paths} actual={paths}"
        )
    return document


def _generate_overlay(plan: LoadedPlan, worktree: Path) -> Path:
    relative = plan.document["outputs"]["codex_overlay_ledger"]
    output = worktree / PurePosixPath(relative)
    if output.exists() or output.is_symlink():
        raise UpstreamMergeError(f"新版本 Codex overlay 输出已存在：{relative}")
    completed = run_process(
        (
            "python3",
            "tools/check_ledger_completeness.py",
            "--upstream-merge-plan",
            str(plan.path),
            "--write-upstream-merge-ledger",
        ),
        cwd=worktree,
        check=False,
    )
    if completed.returncode != 0:
        raise UpstreamMergeError(
            "Codex overlay 生成失败：" + (completed.stderr.strip() or completed.stdout.strip())
        )
    if output.is_symlink() or not output.is_file():
        raise UpstreamMergeError("Codex overlay 生成器没有创建计划输出")
    return output


def seal_source_candidate(
    plan: LoadedPlan,
    source_change_input: Path | None,
) -> dict[str, Any]:
    """生成 overlay，并以追加式 revision 封存 source candidate。

    首轮从 U-1 merge commit 开始；后续轮次允许从当前最新 SourceCandidate
    继续提交修复。每一轮都生成独立不可变制品，不覆盖历史文件。
    """

    merge_candidate = _load_merge_candidate(plan)
    worktree = _worktree_root(plan)
    current_head = rev_parse(worktree, "HEAD^{commit}")
    previous_paths = stage_paths(plan, "source_candidate")
    previous_source = _load_source_candidate(plan) if previous_paths else None
    if previous_source is None:
        if current_head != merge_candidate["merge_commit"]:
            raise UpstreamMergeError("首轮 source seal 前 HEAD 不是 U-1 merge commit")
        revision, output_path = next_stage_path(plan, "source_candidate", revision=1)
        overlay_path = _generate_overlay(plan, worktree)
        overlay_relative = overlay_path.relative_to(worktree).as_posix()
        expected_overlay_paths = {overlay_relative}
        base_source_commit = merge_candidate["merge_commit"]
    else:
        if current_head != previous_source["source_commit"]:
            raise UpstreamMergeError(
                "后续 source revision 必须从当前最新 SourceCandidate HEAD 开始"
            )
        revision, output_path = next_stage_path(plan, "source_candidate")
        overlay_relative = str(previous_source["codex_overlay_ledger"]["path"])
        overlay_path = worktree / PurePosixPath(overlay_relative)
        if overlay_path.is_symlink() or not overlay_path.is_file():
            raise UpstreamMergeError("历史 Codex overlay 在后续 revision 中不可复用")
        if sha256_file(overlay_path) != previous_source["codex_overlay_ledger"]["sha256"]:
            raise UpstreamMergeError("历史 Codex overlay 内容漂移，禁止继续 revision")
        expected_overlay_paths = set()
        base_source_commit = previous_source["source_commit"]
    paths = status_paths(worktree)
    if previous_source is None and overlay_relative not in paths:
        raise UpstreamMergeError("Codex overlay 未进入首轮 source candidate 变化闭集")
    if previous_source is not None and overlay_relative in paths:
        raise UpstreamMergeError("后续 revision 不得修改已封存 Codex overlay")
    additional_paths = [relative for relative in paths if relative not in expected_overlay_paths]
    if not paths:
        raise UpstreamMergeError("当前 SourceCandidate 没有新的源码修复")
    if previous_source is None and paths == [overlay_relative] and source_change_input is None:
        change_input_binding = None
    else:
        if source_change_input is None:
            raise UpstreamMergeError("源码修复必须提供 SourceChangeInput")
        _load_source_change_input(
            source_change_input,
            plan,
            merge_candidate["merge_commit"],
            additional_paths,
            base_source_commit=base_source_commit,
        )
        change_input_binding = file_binding(source_change_input.resolve(strict=True))
    run_git(worktree, "add", "--all", "--", *paths)
    if status_paths(worktree) != paths:
        raise UpstreamMergeError("source candidate 暂存后变化路径漂移")
    run_git(
        worktree,
        "-c",
        "user.name=Sub2API Upstream Merge",
        "-c",
        "user.email=upstream-merge@sub2apiplus.invalid",
        "commit",
        "--no-verify",
        "-m",
        f"chore: seal Sub2API {plan.document['upstream']['tag']} source revision {revision:03d}",
    )
    source_commit = rev_parse(worktree, "HEAD^{commit}")
    source_tree = commit_tree(worktree, source_commit)
    assert_clean(worktree, "U-2 source candidate")
    document = _stage_document(
        plan,
        SOURCE_CANDIDATE_SCHEMA,
        {
            **_revision_metadata(plan, "source_candidate", revision),
            "merge_candidate": stage_binding(plan, "merge_candidate"),
            "source_commit": source_commit,
            "source_tree": source_tree,
            "changed_paths": changed_paths(worktree, plan.fork_head, source_commit),
            "source_change_input": change_input_binding,
            "codex_overlay_ledger": {
                "path": overlay_relative,
                "sha256": sha256_file(overlay_path),
                "bytes": overlay_path.stat().st_size,
            },
        },
    )
    write_json_once(output_path, document)
    return document


def _snapshot_rows(document: dict[str, Any], surface: str) -> dict[str, dict[str, Any]]:
    if surface == "route":
        if document.get("schema_version") != "official-egress-upstream-route-snapshot/v1":
            raise UpstreamMergeError("route snapshot schema_version 非法")
        identity_field = "route_fingerprint"
        values = document.get("entries")
    else:
        if (
            document.get("schema_version")
            != "official-egress-upstream-source-to-sink-snapshot/v1"
        ):
            raise UpstreamMergeError("source-to-sink snapshot schema_version 非法")
        identity_field = "scan_candidate_id"
        values = document.get("sinks")
    if not isinstance(values, list):
        raise UpstreamMergeError(f"{surface} snapshot 条目必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(values):
        row = expect_object(raw, f"{surface} snapshot[{index}]")
        identity = expect_string(row.get(identity_field), f"{surface} snapshot identity")
        if identity in result:
            raise UpstreamMergeError(f"{surface} snapshot 身份重复：{identity}")
        normalized = {
            key: value
            for key, value in row.items()
            if key not in {"line", "line_hint"}
        }
        result[identity] = normalized
    return result


def _sink_clients(row: dict[str, Any] | None) -> list[str]:
    if row is None:
        return []
    searchable = " ".join(
        str(row.get(key, ""))
        for key in ("persona", "purpose", "runtime_sink_id", "package", "file")
    ).lower()
    result: list[str] = []
    if any(token in searchable for token in ("claude", "anthropic")):
        result.append("claude")
    if any(token in searchable for token in ("codex", "openai")):
        result.append("codex")
    return sorted(result)


def _surface_delta_rows(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    surface: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for identity in sorted(set(before) | set(after)):
        raw_left = before.get(identity)
        raw_right = after.get(identity)
        left = (
            {key: value for key, value in raw_left.items() if key not in {"line", "line_hint"}}
            if raw_left is not None
            else None
        )
        right = (
            {key: value for key, value in raw_right.items() if key not in {"line", "line_hint"}}
            if raw_right is not None
            else None
        )
        if left == right:
            continue
        if left is None:
            change = "added"
        elif right is None:
            change = "removed"
        else:
            change = "changed"
        clients = list(CLIENT_KEYS) if surface == "route" else sorted(
            set(_sink_clients(left)) | set(_sink_clients(right))
        )
        delta_id = sha256_bytes(
            canonical_bytes(
                {
                    "surface": surface,
                    "identity": identity,
                    "change": change,
                    "before": left,
                    "after": right,
                }
            )
        )
        rows.append(
            {
                "delta_id": delta_id,
                "surface": surface,
                "identity": identity,
                "change": change,
                "clients": clients,
                "before_sha256": sha256_bytes(canonical_bytes(left)) if left is not None else None,
                "after_sha256": sha256_bytes(canonical_bytes(right)) if right is not None else None,
                "oauth_related": bool(
                    surface == "egress"
                    and (
                        (left and (left.get("official_host") or _sink_clients(left)))
                        or (right and (right.get("official_host") or _sink_clients(right)))
                    )
                ),
            }
        )
    return rows


def scan_surfaces(plan: LoadedPlan) -> dict[str, Any]:
    """在干净 source candidate 上复算入口路由和全部网络发送点。"""

    source = _load_source_candidate(plan)
    worktree = _worktree_root(plan)
    if rev_parse(worktree, "HEAD^{commit}") != source["source_commit"]:
        raise UpstreamMergeError("surface scan 的 HEAD 与 SourceCandidate 不一致")
    assert_clean(worktree, "U-2 surface scan")
    source_revision = source.get("revision", latest_revision(plan, "source_candidate"))
    if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 1:
        raise UpstreamMergeError("SourceCandidate revision 非法")
    _, route_path = next_stage_path(
        plan,
        "surface_route_snapshot",
        revision=source_revision,
    )
    _, egress_path = next_stage_path(
        plan,
        "surface_egress_snapshot",
        revision=source_revision,
    )

    # 两个扫描都成功且差异已计算前，只写入 evidence root 下的临时目录；这样
    # egressscan 失败时不会留下半成品 route snapshot，后续可直接重试同一轮。
    with tempfile.TemporaryDirectory(
        prefix="surface-scan-",
        dir=plan.evidence_root,
    ) as temporary:
        temporary_root = Path(temporary)
        temporary_route = temporary_root / "route-snapshot.json"
        temporary_egress = temporary_root / "source-to-sink-snapshot.json"
        candidate_route = route_snapshot(
            worktree,
            source["source_commit"],
            source["source_tree"],
        )
        write_json_once(temporary_route, candidate_route)
        run_egress_snapshot(worktree, temporary_egress)
        candidate_egress = load_json(
            temporary_egress,
            "U-2 source-to-sink snapshot",
        )

        def pending_binding(final_path: Path, temporary_path: Path) -> dict[str, Any]:
            resolved_root = plan.evidence_root.resolve(strict=True)
            resolved_final = final_path.resolve(strict=False)
            if not resolved_final.is_relative_to(resolved_root):
                raise UpstreamMergeError("U-2 扫描制品越过 evidence root")
            temporary_binding = artifact_binding(plan.evidence_root, temporary_path)
            return {
                "path": resolved_final.relative_to(resolved_root).as_posix(),
                "sha256": temporary_binding["sha256"],
                "bytes": temporary_binding["bytes"],
            }

        route_binding = pending_binding(route_path, temporary_route)
        egress_binding = pending_binding(egress_path, temporary_egress)

    baseline_route_binding = plan.document["discovery_baseline"]["route_snapshot"]
    baseline_egress_binding = plan.document["discovery_baseline"]["source_to_sink_snapshot"]
    baseline_route = load_json(
        resolve_within(plan.evidence_root, baseline_route_binding["path"], "baseline route"),
        "U-0 route snapshot",
    )
    baseline_egress = load_json(
        resolve_within(plan.evidence_root, baseline_egress_binding["path"], "baseline egress"),
        "U-0 source-to-sink snapshot",
    )
    if (
        candidate_route.get("source_commit") != source["source_commit"]
        or candidate_route.get("source_tree") != source["source_tree"]
        or candidate_egress.get("source_commit") != source["source_commit"]
        or candidate_egress.get("source_tree") != source["source_tree"]
    ):
        raise UpstreamMergeError("U-2 快照未绑定 SourceCandidate commit/tree")
    route_deltas = _surface_delta_rows(
        _snapshot_rows(baseline_route, "route"),
        _snapshot_rows(candidate_route, "route"),
        "route",
    )
    egress_deltas = _surface_delta_rows(
        _snapshot_rows(baseline_egress, "egress"),
        _snapshot_rows(candidate_egress, "egress"),
        "egress",
    )
    document = _stage_document(
        plan,
        SURFACE_DELTA_SCHEMA,
        {
            **_revision_metadata(plan, "surface_delta", source_revision),
            "source_candidate": stage_binding(plan, "source_candidate"),
            "baseline_route_snapshot": baseline_route_binding,
            "candidate_route_snapshot": route_binding,
            "baseline_source_to_sink_snapshot": baseline_egress_binding,
            "candidate_source_to_sink_snapshot": egress_binding,
            "route_delta_count": len(route_deltas),
            "egress_delta_count": len(egress_deltas),
            "deltas": sorted(route_deltas + egress_deltas, key=lambda item: item["delta_id"]),
        },
    )
    _, delta_path = next_stage_path(
        plan,
        "surface_delta",
        revision=source_revision,
    )
    # 仅在所有输入通过校验后落盘不可变扫描证据。
    write_json_once(route_path, candidate_route)
    write_json_once(egress_path, candidate_egress)
    write_json_once(delta_path, document)
    return document


def _load_surface_delta(plan: LoadedPlan) -> dict[str, Any]:
    document = _load_revision_artifact(
        plan,
        "surface_delta",
        "SurfaceDelta",
        SURFACE_DELTA_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "source_candidate",
            "baseline_route_snapshot",
            "candidate_route_snapshot",
            "baseline_source_to_sink_snapshot",
            "candidate_source_to_sink_snapshot",
            "route_delta_count",
            "egress_delta_count",
            "deltas",
        },
    )
    revision = document.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        _validate_current_stage_binding(
            plan,
            document["source_candidate"],
            "source_candidate",
            "SurfaceDelta.source_candidate",
        )
        for field, key in (
            ("candidate_route_snapshot", "surface_route_snapshot"),
            ("candidate_source_to_sink_snapshot", "surface_egress_snapshot"),
        ):
            binding = validate_artifact_binding(
                plan.evidence_root,
                document[field],
                f"SurfaceDelta.{field}",
            )
            path = resolve_within(plan.evidence_root, binding["path"], f"SurfaceDelta.{field}.path")
            if revision_number(path, plan, key) != revision:
                raise UpstreamMergeError(f"SurfaceDelta {field} 未绑定当前 revision")
    return document


def carry_forward_inventory(plan: LoadedPlan, client: str, kind: str) -> dict[str, Any]:
    """仅在相应发现分母零差异时逐字节沿用当前 Inventory。"""

    if client not in CLIENT_KEYS or kind not in {"ingress", "egress"}:
        raise UpstreamMergeError("inventory carry-forward 的 client/kind 非法")
    delta = _load_surface_delta(plan)
    blocking = []
    for item in delta["deltas"]:
        if kind == "ingress" and item["surface"] == "route":
            blocking.append(item["delta_id"])
        if kind == "egress" and item["surface"] == "egress" and client in item["clients"]:
            blocking.append(item["delta_id"])
    if blocking:
        source_revision = latest_revision(plan, "source_candidate")
        try:
            if source_revision > 1:
                target = next_inventory_path(plan, client, kind, revision=source_revision)
            else:
                target = resolve_within(
                    plan.evidence_root,
                    plan.document["outputs"]["candidate_inventories"][client][kind],
                    f"outputs.candidate_inventories.{client}.{kind}",
                )
            hint = f"；本轮应写入 {target}"
        except UpstreamMergeError:
            hint = ""
        raise UpstreamMergeError(
            f"{client}/{kind} 发现分母有变化，必须由专用流程重新生成 Inventory：{blocking}{hint}"
        )
    field = "production_ingress_inventory" if kind == "ingress" else "egress_disposition_inventory"
    baseline = plan.document["baselines"][field][client]
    validated_baseline = validate_file_binding(
        baseline,
        f"baselines.{field}.{client}",
    )
    source_path = Path(validated_baseline["path"])
    source_revision = latest_revision(plan, "source_candidate")
    if source_revision > 1:
        output = next_inventory_path(
            plan,
            client,
            kind,
            revision=source_revision,
        )
    else:
        output = resolve_within(
            plan.evidence_root,
            plan.document["outputs"]["candidate_inventories"][client][kind],
            f"outputs.candidate_inventories.{client}.{kind}",
        )
        if output.exists() or output.is_symlink():
            raise UpstreamMergeError(f"候选 Inventory 输出已存在，禁止覆盖：{output}")
    write_once(output, source_path.read_bytes())
    payload = _validate_inventory_payload(
        output,
        kind,
        plan.document["official_clients"][client]["persona"],
        f"candidate {client}/{kind} Inventory",
    )
    return {
        "client": client,
        "kind": kind,
        "result": "carried_forward_zero_surface_delta",
        "output": artifact_binding(plan.evidence_root, output),
        "entry_count": len(payload["entries"]),
    }


def _inventory_entry_ids(payload: dict[str, Any], kind: str) -> set[str]:
    field = "logical_ingress_id" if kind == "ingress" else "egress_id"
    return {str(item[field]) for item in payload["entries"]}


def _load_surface_decisions(
    path: Path,
    plan: LoadedPlan,
    delta: dict[str, Any],
    inventories: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    document = expect_object(load_json(path, "SurfaceDecision"), "SurfaceDecision")
    expect_exact_fields(
        document,
        {
            "schema_version",
            "plan_id",
            "plan_identity_sha256",
            "source_tree",
            "surface_delta_sha256",
            "decisions",
            "identity_sha256",
        },
        "SurfaceDecision",
    )
    if document.get("schema_version") != SURFACE_DECISION_SCHEMA:
        raise UpstreamMergeError("SurfaceDecision schema_version 非法")
    source = _load_source_candidate(plan)
    if (
        document.get("plan_id") != plan.plan_id
        or document.get("plan_identity_sha256") != plan.identity
        or document.get("source_tree") != source["source_tree"]
        or document.get("surface_delta_sha256")
        != sha256_file(latest_stage_path(plan, "surface_delta"))
    ):
        raise UpstreamMergeError("SurfaceDecision 身份或 SurfaceDelta 绑定不一致")
    validate_identity(document, "SurfaceDecision")
    expected = {item["delta_id"]: item for item in delta["deltas"]}
    values = document.get("decisions")
    if not isinstance(values, list):
        raise UpstreamMergeError("SurfaceDecision.decisions 必须是数组")
    seen: set[str] = set()
    for index, raw in enumerate(values):
        label = f"SurfaceDecision.decisions[{index}]"
        item = expect_object(raw, label)
        expect_exact_fields(
            item,
            {"delta_id", "disposition", "inventory_entries", "rationale"},
            label,
        )
        delta_id = expect_sha256(item.get("delta_id"), f"{label}.delta_id")
        if delta_id not in expected or delta_id in seen:
            raise UpstreamMergeError(f"{label} 引用未知或重复 delta：{delta_id}")
        seen.add(delta_id)
        target = expected[delta_id]
        if target["surface"] == "route":
            allowed = {
                "explicitly_retired",
                "migrated_strict",
                "out_of_scope",
                "rerouted",
                "retained_legacy",
            }
            kind = "ingress"
        else:
            allowed = {"denied", "non_persona_managed", "persona_strict"}
            kind = "egress"
        disposition = validate_string_enum(
            item.get("disposition"), allowed, f"{label}.disposition"
        )
        if target["surface"] == "egress" and target["oauth_related"] and not target["clients"]:
            raise UpstreamMergeError(
                f"{label} 发现无法归属 Persona 的 OAuth 发送点，必须先补充扫描器/人工身份映射"
            )
        if target["oauth_related"] and target["change"] == "added" and disposition == "out_of_scope":
            raise UpstreamMergeError("新增 OAuth 发送点不得声明为范围外透传")
        rationale = expect_string(item.get("rationale"), f"{label}.rationale")
        if len(rationale) < 16:
            raise UpstreamMergeError(f"{label}.rationale 必须说明证据与处置")
        mapping = expect_object(item.get("inventory_entries"), f"{label}.inventory_entries")
        expected_clients = set(target["clients"])
        if set(mapping) != expected_clients:
            raise UpstreamMergeError(
                f"{label}.inventory_entries 未覆盖受影响 Persona：expected={sorted(expected_clients)}"
            )
        for client, raw_ids in mapping.items():
            if not isinstance(raw_ids, list):
                raise UpstreamMergeError(f"{label}.inventory_entries.{client} 必须是数组")
            ids = [expect_string(value, f"{label}.inventory_entries.{client}") for value in raw_ids]
            if ids != sorted(set(ids)):
                raise UpstreamMergeError(f"{label}.inventory_entries.{client} 必须排序且不得重复")
            if target["change"] != "removed" and client in CLIENT_KEYS and not ids:
                raise UpstreamMergeError(f"{label} 非删除变化必须绑定候选 Inventory 条目")
            available = _inventory_entry_ids(inventories[client][kind], kind)
            missing = sorted(set(ids) - available)
            if missing:
                raise UpstreamMergeError(f"{label} 引用未知候选 Inventory 条目：{missing}")
    if seen != set(expected):
        raise UpstreamMergeError(
            f"SurfaceDecision 未闭合全部 delta：missing={sorted(set(expected) - seen)}"
        )
    return document


def seal_surfaces(plan: LoadedPlan, decisions_path: Path | None) -> dict[str, Any]:
    """验证两个 Persona 的入口/出站 Inventory，并封存 U-2 闭集。"""

    delta = _load_surface_delta(plan)
    source_revision = latest_revision(plan, "source_candidate")
    inventories: dict[str, dict[str, dict[str, Any]]] = {}
    inventory_bindings: dict[str, dict[str, Any]] = {}
    for client in CLIENT_KEYS:
        inventories[client] = {}
        inventory_bindings[client] = {}
        for kind in ("ingress", "egress"):
            if source_revision == 1:
                path = resolve_within(
                    plan.evidence_root,
                    plan.document["outputs"]["candidate_inventories"][client][kind],
                    f"outputs.candidate_inventories.{client}.{kind}",
                )
            else:
                path = plan.inventory_output(client, kind)
                if inventory_revision_number(plan, client, kind, path) != source_revision:
                    raise UpstreamMergeError(
                        f"{client}/{kind} Inventory 未绑定当前 SourceCandidate revision"
                    )
            payload = _validate_inventory_payload(
                path,
                kind,
                plan.document["official_clients"][client]["persona"],
                f"candidate {client}/{kind} Inventory",
            )
            inventories[client][kind] = payload
            inventory_bindings[client][kind] = artifact_binding(plan.evidence_root, path)
    if delta["deltas"]:
        if decisions_path is None:
            raise UpstreamMergeError("发送面存在变化时必须提供 SurfaceDecision")
        _load_surface_decisions(decisions_path, plan, delta, inventories)
        decision_binding = file_binding(decisions_path.resolve(strict=True))
    else:
        if decisions_path is not None:
            raise UpstreamMergeError("发送面零差异时不得附带无关 SurfaceDecision")
        decision_binding = None
    document = _stage_document(
        plan,
        SURFACE_RECEIPT_SCHEMA,
        {
            **_revision_metadata(plan, "surface_receipt", source_revision),
            "source_candidate": stage_binding(plan, "source_candidate"),
            "surface_delta": stage_binding(plan, "surface_delta"),
            "route_snapshot": stage_binding(plan, "surface_route_snapshot"),
            "source_to_sink_snapshot": stage_binding(plan, "surface_egress_snapshot"),
            "candidate_inventories": inventory_bindings,
            "surface_decision": decision_binding,
            "unknown_oauth_egress_count": 0,
            "unclassified_delta_count": 0,
            "result": "closed",
        },
    )
    _, receipt_path = next_stage_path(
        plan,
        "surface_receipt",
        revision=source_revision,
    )
    write_json_once(receipt_path, document)
    return document


def _load_surface_receipt(plan: LoadedPlan) -> dict[str, Any]:
    document = _load_revision_artifact(
        plan,
        "surface_receipt",
        "SurfaceRecalculationReceipt",
        SURFACE_RECEIPT_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "source_candidate",
            "surface_delta",
            "route_snapshot",
            "source_to_sink_snapshot",
            "candidate_inventories",
            "surface_decision",
            "unknown_oauth_egress_count",
            "unclassified_delta_count",
            "result",
        },
    )
    revision = document.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        _validate_current_stage_binding(
            plan,
            document["source_candidate"],
            "source_candidate",
            "SurfaceReceipt.source_candidate",
        )
        _validate_current_stage_binding(
            plan,
            document["surface_delta"],
            "surface_delta",
            "SurfaceReceipt.surface_delta",
        )
        for field, key in (
            ("route_snapshot", "surface_route_snapshot"),
            ("source_to_sink_snapshot", "surface_egress_snapshot"),
        ):
            binding = validate_artifact_binding(
                plan.evidence_root,
                document[field],
                f"SurfaceReceipt.{field}",
            )
            path = resolve_within(plan.evidence_root, binding["path"], f"SurfaceReceipt.{field}.path")
            if revision_number(path, plan, key) != revision:
                raise UpstreamMergeError(f"SurfaceReceipt {field} 未绑定当前 revision")
        inventories = expect_object(document["candidate_inventories"], "SurfaceReceipt.candidate_inventories")
        for client in CLIENT_KEYS:
            pair = expect_object(inventories.get(client), f"SurfaceReceipt.candidate_inventories.{client}")
            for kind in ("ingress", "egress"):
                binding = validate_artifact_binding(
                    plan.evidence_root,
                    pair.get(kind),
                    f"SurfaceReceipt.candidate_inventories.{client}.{kind}",
                )
                path = resolve_within(
                    plan.evidence_root,
                    binding["path"],
                    f"SurfaceReceipt.candidate_inventories.{client}.{kind}.path",
                )
                if inventory_revision_number(plan, client, kind, path) != revision:
                    raise UpstreamMergeError(
                        f"SurfaceReceipt {client}/{kind} Inventory 未绑定当前 revision"
                    )
    return document


def _diff_risk_hints(worktree: Path, before: str, after: str, relative: str) -> list[str]:
    completed = run_git(
        worktree,
        "diff",
        "--unified=0",
        before,
        after,
        "--",
        relative,
        check=False,
    )
    text = completed.stdout.lower()
    hints: list[str] = []
    patterns = {
        "account": r"\baccount(?:id|_id|s)?\b",
        "billing": r"\bbill(?:ing|able)?\b|\bprice|\bcost",
        "group": r"\bgroup(?:id|_id|s)?\b",
        "key": r"\bapi[_ -]?key\b|\bkeyid\b|\bkey_id\b",
        "quota_usage": r"\bquota\b|\busage\b|\bcredit",
        "route": r"\broute\b|\.get\(|\.post\(|\.put\(|\.delete\(|\.patch\(",
        "selector": r"\bselector\b|\bactive\b|\brollback\b|\bprevious\b",
        "wire": r"\bheader\b|\bbody\b|\bwebsocket\b|\btls\b|\bendpoint\b",
    }
    for name, pattern in patterns.items():
        if re.search(pattern, text):
            hints.append(name)
    return sorted(hints)


def _suggest_categories(relative: str, risk_hints: list[str]) -> list[str]:
    lower = relative.lower()
    categories: set[str] = set()
    if any(token in lower for token in ("claude", "anthropic")):
        categories.add("claude_persona")
    if any(token in lower for token in ("codex", "openai")):
        categories.add("codex_persona")
    if any(token in lower for token in ("adapter", "gateway", "handler", "protocol", "router", "routes/")):
        categories.add("protocol_adapter")
    if (
        lower.startswith("backend/internal/officialegress/")
        and not ({"claude_persona", "codex_persona"} & categories)
    ) or lower.startswith("tools/official_client_control/"):
        categories.add("shared_control")
    if any(item in risk_hints for item in ("account", "billing", "group", "key", "quota_usage", "route")):
        categories.add("key_group_routing_billing")
    if lower.startswith(("frontend/", "deploy/", ".github/")):
        categories.add("repository_support")
    # wire/selector 提示必须被 Persona、协议适配或共享控制面之一承接（见 ChangeDecision 校验）。
    # 建议端若不产生，草稿在自身校验下天然不合规；按路径归属给出与校验同域的默认承接。
    if any(item in risk_hints for item in ("wire", "selector")) and not (
        categories & {"claude_persona", "codex_persona", "protocol_adapter", "shared_control"}
    ):
        if lower.startswith(("backend/internal/handler/", "backend/internal/service/", "backend/internal/server/")):
            categories.add("protocol_adapter")
        else:
            categories.add("shared_control")
    if not categories:
        categories.add("out_of_scope_product")
    return sorted(categories)


def _component_for_path(relative: str) -> dict[str, Any] | None:
    """按最长前缀解析组件；未命中时保持未知而不是猜测。"""

    matches: list[tuple[int, dict[str, Any]]] = []
    for component in COMPONENT_OWNERSHIP_MANIFEST["components"]:
        for prefix in component["prefixes"]:
            if relative == prefix or relative.startswith(prefix):
                matches.append((len(prefix), component))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]["id"]))
    return matches[0][1]


def _component_ownership(relative: str, old_relative: str = "") -> dict[str, Any]:
    paths = [relative]
    if old_relative:
        paths.append(old_relative)
    components = [item for item in (_component_for_path(path) for path in paths) if item]
    component_ids = sorted({item["id"] for item in components})
    owner_ids = sorted({item["owner"] for item in components})
    dependency_ids = sorted(
        {
            dependency
            for item in components
            for dependency in item["dependencies"]
        }
    )
    known_components = {
        item["id"] for item in COMPONENT_OWNERSHIP_MANIFEST["components"]
    }
    unknown_dependencies = sorted(
        set(dependency_ids) - (known_components | KNOWN_ABSTRACT_DEPENDENCIES)
    )
    known = bool(components) and not unknown_dependencies and len(components) == len(paths)
    risks = sorted({item["risk"] for item in components})
    categories = sorted(
        {
            category
            for item in components
            for category in item["categories"]
        }
    )
    return {
        "mapping_schema": COMPONENT_OWNERSHIP_SCHEMA,
        "mapping_version": COMPONENT_OWNERSHIP_VERSION,
        "mapping_sha256": COMPONENT_OWNERSHIP_SHA256,
        "status": "known" if known else "unknown",
        "component_ids": component_ids,
        "owner_ids": owner_ids,
        "dependency_ids": dependency_ids,
        "unknown_dependency_ids": unknown_dependencies,
        "risk_levels": risks,
        "suggested_categories": categories,
    }


def _auto_classification(entry: dict[str, Any]) -> dict[str, Any]:
    ownership = entry["component_ownership"]
    hints = set(entry.get("risk_hints", []))
    categories = set(entry.get("suggested_categories", []))
    eligible = (
        ownership["status"] == "known"
        and not ownership["unknown_dependency_ids"]
        and categories
        and categories <= AUTO_CLASSIFICATION_CATEGORIES
        and not (hints & AUTO_CLASSIFICATION_BLOCKERS)
        and "high" not in ownership["risk_levels"]
    )
    reason = (
        "组件映射、依赖关系和差异提示均为低风险；未命中 wire、selector、"
        "Persona、共享控制面或 Key/Group/路由/计费风险。"
        if eligible
        else "必须人工确认组件所有权、依赖关系或行为风险；工具不得自动放行。"
    )
    return {
        "eligible": eligible,
        "decision_source": "auto" if eligible else "manual_required",
        "reason": reason,
    }


def _suggested_change_decision_item(entry: dict[str, Any]) -> dict[str, Any]:
    auto = _auto_classification(entry)
    categories = entry["suggested_categories"]
    if auto["eligible"]:
        rationale = (
            f"自动分类（映射 {COMPONENT_OWNERSHIP_VERSION}/{COMPONENT_OWNERSHIP_SHA256[:12]}）："
            "低风险仓库支撑文件，未发现官方 Persona 或共享合同影响。"
        )
        actions = ["保留现有官方客户端合同", "运行公共终态门禁"]
    else:
        rationale = "待人工审查：" + auto["reason"]
        actions = ["人工确认组件所有权与直接依赖", "人工确认是否触及官方客户端或共享合同"]
    return {
        "path": entry["path"],
        "categories": categories,
        "rationale": rationale,
        "required_actions": sorted(set(actions)),
        "official_client_identity_changed": False,
        "evidence_semantics_changed": False,
        "decision_source": auto["decision_source"],
        "component_ownership": entry["component_ownership"],
        "auto_reason": auto["reason"],
    }


def generate_change_decision_suggestion(
    plan: LoadedPlan,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """生成带安全分级的 ChangeDecision 草稿；高风险条目保持待人工状态。"""

    matrix = _load_impact_matrix(plan)
    files = [_suggested_change_decision_item(entry) for entry in matrix["file_changes"]]
    surface_deltas = [
        {
            "delta_id": item["delta_id"],
            "rationale": "待人工确认发送面变化、Inventory 映射及 OAuth 处置。",
            "required_actions": ["人工确认发送面处置", "完成受影响 Persona 门禁"],
            "decision_source": "manual_required",
        }
        for item in matrix["surface_deltas"]
    ]
    unresolved = [item["path"] for item in files if item["decision_source"] != "auto"]
    document = _stage_document(
        plan,
        CHANGE_DECISION_INPUT_SCHEMA,
        {
            "source_tree": _load_source_candidate(plan)["source_tree"],
            "impact_matrix_sha256": sha256_file(latest_stage_path(plan, "impact_matrix")),
            "files": files,
            "surface_deltas": surface_deltas,
            "mapping_schema": COMPONENT_OWNERSHIP_SCHEMA,
            "mapping_version": COMPONENT_OWNERSHIP_VERSION,
            "mapping_sha256": COMPONENT_OWNERSHIP_SHA256,
            "auto_accepted_count": len(files) - len(unresolved),
            "manual_required_count": len(unresolved) + len(surface_deltas),
            "unresolved_paths": sorted(unresolved),
            "result": "ready_for_review" if unresolved or surface_deltas else "ready_to_seal",
        },
    )
    if output_path is not None:
        if not output_path.is_absolute():
            raise UpstreamMergeError("ChangeDecision 草稿输出必须是绝对路径")
        write_json_once(output_path, document)
    return document


def generate_impact_matrix(plan: LoadedPlan) -> dict[str, Any]:
    """生成逐文件和逐发送面差异分母；最终分类必须由独立 ChangeDecision 完成。"""

    surface_receipt = _load_surface_receipt(plan)
    if surface_receipt["result"] != "closed":
        raise UpstreamMergeError("U-2 SurfaceRecalculationReceipt 未闭合")
    source = _load_source_candidate(plan)
    source_revision = source.get("revision", latest_revision(plan, "source_candidate"))
    if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 1:
        raise UpstreamMergeError("SourceCandidate revision 非法")
    worktree = _worktree_root(plan)
    if rev_parse(worktree, "HEAD^{commit}") != source["source_commit"]:
        raise UpstreamMergeError("impact matrix 的 HEAD 与 SourceCandidate 不一致")
    assert_clean(worktree, "U-3 impact matrix")
    entries: list[dict[str, Any]] = []
    for change in changed_paths(worktree, plan.fork_head, source["source_commit"]):
        relative = change["path"]
        hints = _diff_risk_hints(worktree, plan.fork_head, source["source_commit"], relative)
        ownership = _component_ownership(relative, change.get("old_path", ""))
        suggested_categories = sorted(
            set(_suggest_categories(relative, hints))
            | set(ownership.get("suggested_categories", []))
        )
        entries.append(
            {
                **change,
                "diff_sha256": sha256_bytes(
                    run_git(
                        worktree,
                        "diff",
                        "--binary",
                        plan.fork_head,
                        source["source_commit"],
                        "--",
                        relative,
                    ).stdout.encode("utf-8")
                ),
                "risk_hints": hints,
                "suggested_categories": suggested_categories,
                "component_ownership": ownership,
                "auto_classification": _auto_classification(
                    {
                        "path": relative,
                        "risk_hints": hints,
                        "suggested_categories": suggested_categories,
                        "component_ownership": ownership,
                    }
                ),
                "classification_status": "pending_human_decision",
            }
        )
    if not entries:
        raise UpstreamMergeError("上游 changeset 没有任何源码变化")
    delta = _load_surface_delta(plan)
    document = _stage_document(
        plan,
        IMPACT_MATRIX_SCHEMA,
        {
            **_revision_metadata(plan, "impact_matrix", source_revision),
            "source_candidate": stage_binding(plan, "source_candidate"),
            "surface_receipt": stage_binding(plan, "surface_receipt"),
            "file_change_count": len(entries),
            "file_changes": entries,
            "surface_delta_count": len(delta["deltas"]),
            "surface_deltas": delta["deltas"],
            "component_mapping": {
                "schema": COMPONENT_OWNERSHIP_SCHEMA,
                "version": COMPONENT_OWNERSHIP_VERSION,
                "sha256": COMPONENT_OWNERSHIP_SHA256,
            },
            "classification_rule": (
                "组件所有权和依赖映射只允许低风险条目自动建议；未知路径、未知依赖、"
                "Persona、wire、selector、共享控制面及 Key/Group/路由/计费风险必须人工分类"
            ),
            "result": "pending_change_decision",
        },
    )
    _, matrix_path = next_stage_path(
        plan,
        "impact_matrix",
        revision=source_revision,
    )
    write_json_once(matrix_path, document)
    return document


def _load_impact_matrix(plan: LoadedPlan) -> dict[str, Any]:
    document = _load_revision_artifact(
        plan,
        "impact_matrix",
        "ImpactMatrix",
        IMPACT_MATRIX_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "source_candidate",
            "surface_receipt",
            "file_change_count",
            "file_changes",
            "surface_delta_count",
            "surface_deltas",
            "classification_rule",
            "result",
        },
        optional_fields={"component_mapping"},
    )
    revision = document.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        _validate_current_stage_binding(
            plan,
            document["source_candidate"],
            "source_candidate",
            "ImpactMatrix.source_candidate",
        )
        _validate_current_stage_binding(
            plan,
            document["surface_receipt"],
            "surface_receipt",
            "ImpactMatrix.surface_receipt",
        )
    return document


def _apply_client_impact(
    categories: set[str],
    semantics_changed: bool,
    client_impacts: dict[str, bool],
    client_campaigns: dict[str, bool],
) -> bool:
    """把逐文件分类收敛为 Persona、Campaign 和共享合同后继动作。"""

    shared_contract_required = "shared_control" in categories
    if shared_contract_required or "protocol_adapter" in categories:
        client_impacts.update({"claude": True, "codex": True})
        if semantics_changed:
            client_campaigns.update({"claude": True, "codex": True})
    for client, category in (("claude", "claude_persona"), ("codex", "codex_persona")):
        if category in categories:
            client_impacts[client] = True
            if semantics_changed:
                client_campaigns[client] = True
    return shared_contract_required


def seal_change_decision(plan: LoadedPlan, decision_path: Path) -> dict[str, Any]:
    """要求每个变化文件及调用边都有唯一分类和后继动作。"""

    matrix = _load_impact_matrix(plan)
    source = _load_source_candidate(plan)
    source_revision = source.get("revision", latest_revision(plan, "source_candidate"))
    if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 1:
        raise UpstreamMergeError("SourceCandidate revision 非法")
    decision = expect_object(load_json(decision_path, "ChangeDecision"), "ChangeDecision")
    required_decision_fields = {
        "schema_version",
        "plan_id",
        "plan_identity_sha256",
        "source_tree",
        "impact_matrix_sha256",
        "files",
        "surface_deltas",
        "identity_sha256",
    }
    optional_decision_fields = {
        "mapping_schema",
        "mapping_version",
        "mapping_sha256",
        "auto_accepted_count",
        "manual_required_count",
        "unresolved_paths",
        "result",
    }
    if set(decision) - (required_decision_fields | optional_decision_fields) or required_decision_fields - set(decision):
        raise UpstreamMergeError(
            "ChangeDecision 字段不闭合："
            f"缺失={sorted(required_decision_fields - set(decision))}，"
            f"多余={sorted(set(decision) - required_decision_fields - optional_decision_fields)}"
        )
    if decision.get("schema_version") != CHANGE_DECISION_INPUT_SCHEMA:
        raise UpstreamMergeError("ChangeDecision schema_version 非法")
    if (
        decision.get("plan_id") != plan.plan_id
        or decision.get("plan_identity_sha256") != plan.identity
        or decision.get("source_tree") != source["source_tree"]
        or decision.get("impact_matrix_sha256")
        != sha256_file(latest_stage_path(plan, "impact_matrix"))
    ):
        raise UpstreamMergeError("ChangeDecision 身份或 ImpactMatrix 绑定不一致")
    validate_identity(decision, "ChangeDecision")
    if "mapping_schema" in decision and decision["mapping_schema"] != COMPONENT_OWNERSHIP_SCHEMA:
        raise UpstreamMergeError("ChangeDecision 组件映射 schema 漂移")
    if "mapping_version" in decision and decision["mapping_version"] != COMPONENT_OWNERSHIP_VERSION:
        raise UpstreamMergeError("ChangeDecision 组件映射版本漂移")
    if "mapping_sha256" in decision and decision["mapping_sha256"] != COMPONENT_OWNERSHIP_SHA256:
        raise UpstreamMergeError("ChangeDecision 组件映射摘要漂移")
    expected_files = {item["path"]: item for item in matrix["file_changes"]}
    raw_files = decision.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise UpstreamMergeError("ChangeDecision.files 必须是非空数组")
    seen_files: set[str] = set()
    client_impacts = {"claude": False, "codex": False}
    client_campaigns = {"claude": False, "codex": False}
    shared_contract_required = False
    for index, raw in enumerate(raw_files):
        label = f"ChangeDecision.files[{index}]"
        item = expect_object(raw, label)
        required_item_fields = {
            "path",
            "categories",
            "rationale",
            "required_actions",
            "official_client_identity_changed",
            "evidence_semantics_changed",
        }
        optional_item_fields = {
            "decision_source",
            "component_ownership",
            "auto_reason",
        }
        if set(item) - (required_item_fields | optional_item_fields) or required_item_fields - set(item):
            raise UpstreamMergeError(f"{label} 字段不闭合")
        relative = safe_relative_path(item.get("path"), f"{label}.path")
        if relative not in expected_files or relative in seen_files:
            raise UpstreamMergeError(f"{label} 引用未知或重复变化文件：{relative}")
        seen_files.add(relative)
        categories = item.get("categories")
        if not isinstance(categories, list) or not categories:
            raise UpstreamMergeError(f"{label}.categories 必须是非空数组")
        normalized_categories = [
            validate_string_enum(value, FILE_IMPACT_CATEGORIES, f"{label}.categories")
            for value in categories
        ]
        if normalized_categories != sorted(set(normalized_categories)):
            raise UpstreamMergeError(f"{label}.categories 必须排序且不得重复")
        expected_entry = expected_files[relative]
        hints = set(expected_entry["risk_hints"])
        if hints & {"account", "billing", "group", "key", "quota_usage", "route"} and (
            "key_group_routing_billing" not in normalized_categories
        ):
            raise UpstreamMergeError(f"{label} 未承接 Key/Group/路由/计费风险提示")
        if hints & {"wire", "selector"} and not (
            set(normalized_categories)
            & {"claude_persona", "codex_persona", "protocol_adapter", "shared_control"}
        ):
            raise UpstreamMergeError(f"{label} 未承接 wire/selector 风险提示")
        rationale = expect_string(item.get("rationale"), f"{label}.rationale")
        if len(rationale) < 16:
            raise UpstreamMergeError(f"{label}.rationale 必须说明实际调用影响")
        actions = item.get("required_actions")
        if not isinstance(actions, list) or not actions:
            raise UpstreamMergeError(f"{label}.required_actions 必须是非空数组")
        normalized_actions = [expect_string(value, f"{label}.required_actions") for value in actions]
        if normalized_actions != sorted(set(normalized_actions)):
            raise UpstreamMergeError(f"{label}.required_actions 必须排序且不得重复")
        identity_changed = item.get("official_client_identity_changed")
        semantics_changed = item.get("evidence_semantics_changed")
        if not isinstance(identity_changed, bool) or not isinstance(semantics_changed, bool):
            raise UpstreamMergeError(f"{label} 两个 changed 字段必须是布尔值")
        if identity_changed:
            raise UpstreamMergeError(
                f"{relative} 改变官方客户端身份；必须停止 §5.2 并拆分为 §5.3 Campaign"
            )
        decision_source = item.get("decision_source", "manual")
        if decision_source not in {"manual", "auto", "manual_required"}:
            raise UpstreamMergeError(f"{label}.decision_source 非法")
        expected_ownership = expected_entry.get(
            "component_ownership",
            _component_ownership(relative, expected_entry.get("old_path", "")),
        )
        supplied_ownership = item.get("component_ownership")
        if supplied_ownership is not None and supplied_ownership != expected_ownership:
            raise UpstreamMergeError(f"{label}.component_ownership 与 ImpactMatrix 不一致")
        if decision_source == "manual_required":
            raise UpstreamMergeError(f"{relative} 仍处于人工待决状态，不能封存 U-3")
        if decision_source == "auto":
            auto = expected_entry.get("auto_classification")
            if not isinstance(auto, dict) or auto.get("eligible") is not True:
                raise UpstreamMergeError(f"{relative} 不满足安全自动分类条件")
            if identity_changed or semantics_changed:
                raise UpstreamMergeError(f"{relative} 自动分类不得改变官方身份或证据语义")
            if set(normalized_categories) != set(expected_entry.get("suggested_categories", [])):
                raise UpstreamMergeError(f"{relative} 自动分类未采用受管建议类别")
            if expected_ownership.get("status") != "known" or expected_ownership.get("unknown_dependency_ids"):
                raise UpstreamMergeError(f"{relative} 组件所有权或依赖未知，禁止自动分类")
        if expected_ownership.get("risk_levels") and "high" in expected_ownership["risk_levels"]:
            required_from_ownership = set(expected_ownership.get("suggested_categories", []))
            if not required_from_ownership.issubset(set(normalized_categories)):
                raise UpstreamMergeError(f"{relative} 未承接高风险组件映射类别")
        auto_accepted = decision_source == "auto"
        if _apply_client_impact(
            set(normalized_categories),
            semantics_changed,
            client_impacts,
            client_campaigns,
        ):
            shared_contract_required = True
        if auto_accepted:
            # 仅统计；真正的安全条件已在上面 fail-close 校验。
            pass
    if seen_files != set(expected_files):
        raise UpstreamMergeError(
            f"ChangeDecision.files 未闭合：missing={sorted(set(expected_files) - seen_files)}"
        )

    expected_deltas = {item["delta_id"]: item for item in matrix["surface_deltas"]}
    raw_deltas = decision.get("surface_deltas")
    if not isinstance(raw_deltas, list):
        raise UpstreamMergeError("ChangeDecision.surface_deltas 必须是数组")
    seen_deltas: set[str] = set()
    for index, raw in enumerate(raw_deltas):
        label = f"ChangeDecision.surface_deltas[{index}]"
        item = expect_object(raw, label)
        required_delta_fields = {"delta_id", "rationale", "required_actions"}
        optional_delta_fields = {"decision_source"}
        if set(item) - (required_delta_fields | optional_delta_fields) or required_delta_fields - set(item):
            raise UpstreamMergeError(f"{label} 字段不闭合")
        delta_id = expect_sha256(item.get("delta_id"), f"{label}.delta_id")
        if delta_id not in expected_deltas or delta_id in seen_deltas:
            raise UpstreamMergeError(f"{label} 引用未知或重复 surface delta")
        seen_deltas.add(delta_id)
        if len(expect_string(item.get("rationale"), f"{label}.rationale")) < 16:
            raise UpstreamMergeError(f"{label}.rationale 必须说明调用边影响")
        actions = item.get("required_actions")
        if not isinstance(actions, list) or not actions:
            raise UpstreamMergeError(f"{label}.required_actions 必须是非空数组")
        normalized = [expect_string(value, f"{label}.required_actions") for value in actions]
        if normalized != sorted(set(normalized)):
            raise UpstreamMergeError(f"{label}.required_actions 必须排序且不得重复")
        decision_source = item.get("decision_source", "manual")
        if decision_source not in {"manual", "auto", "manual_required"}:
            raise UpstreamMergeError(f"{label}.decision_source 非法")
        if decision_source == "auto":
            raise UpstreamMergeError(f"{label} 发送面变化不得自动分类")
        if decision_source == "manual_required":
            raise UpstreamMergeError(f"{label} 仍处于人工待决状态，不能封存 U-3")
        for client in expected_deltas[delta_id]["clients"]:
            client_impacts[client] = True
    if seen_deltas != set(expected_deltas):
        raise UpstreamMergeError(
            f"ChangeDecision.surface_deltas 未闭合：missing={sorted(set(expected_deltas) - seen_deltas)}"
        )
    auto_count = sum(1 for item in raw_files if item.get("decision_source", "manual") == "auto")
    manual_decision_count = sum(
        1 for item in raw_files if item.get("decision_source", "manual") != "auto"
    ) + len(raw_deltas)
    manual_required_count = sum(
        1
        for item in raw_files
        if item.get("decision_source", "manual") == "manual_required"
    ) + sum(
        1
        for item in raw_deltas
        if item.get("decision_source", "manual") == "manual_required"
    )
    if "auto_accepted_count" in decision:
        value = decision["auto_accepted_count"]
        if isinstance(value, bool) or not isinstance(value, int) or value != auto_count:
            raise UpstreamMergeError(
            f"ChangeDecision.auto_accepted_count 不一致：应为 {auto_accepted_count}"
        )
    if "manual_required_count" in decision:
        value = decision["manual_required_count"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value != manual_required_count
        ):
            raise UpstreamMergeError(
            f"ChangeDecision.manual_required_count 不一致：应为 {manual_required_count}"
        )
    if "unresolved_paths" in decision:
        unresolved_paths = decision["unresolved_paths"]
        if not isinstance(unresolved_paths, list) or unresolved_paths != sorted(set(unresolved_paths)):
            raise UpstreamMergeError("ChangeDecision.unresolved_paths 必须排序且不重复")
        expected_unresolved = sorted(
            item["path"]
            for item in raw_files
            if item.get("decision_source", "manual") == "manual_required"
        )
        if unresolved_paths != expected_unresolved:
            raise UpstreamMergeError("ChangeDecision.unresolved_paths 与逐文件状态不一致")
    if "result" in decision:
        expected_draft_result = (
            "ready_for_review" if manual_required_count else "ready_to_seal"
        )
        if decision["result"] != expected_draft_result:
            raise UpstreamMergeError(
            f"ChangeDecision.result 与决策计数不一致：应为 {expected_draft_result}"
        )
    receipt = _stage_document(
        plan,
        CHANGE_DECISION_RECEIPT_SCHEMA,
        {
            **_revision_metadata(plan, "impact_receipt", source_revision),
            "impact_matrix": stage_binding(plan, "impact_matrix"),
            "change_decision": file_binding(decision_path.resolve(strict=True)),
            "file_decision_count": len(seen_files),
            "surface_decision_count": len(seen_deltas),
            "client_impacts": client_impacts,
            "successor_campaign_required": client_campaigns,
            "shared_contract_required": shared_contract_required,
            "unclassified_count": 0,
            "official_client_identity_change_count": 0,
            "component_mapping_schema": COMPONENT_OWNERSHIP_SCHEMA,
            "component_mapping_version": COMPONENT_OWNERSHIP_VERSION,
            "component_mapping_sha256": COMPONENT_OWNERSHIP_SHA256,
            "auto_accepted_count": sum(
                1 for item in raw_files if item.get("decision_source") == "auto"
            ),
            "manual_decision_count": manual_decision_count,
            "unknown_component_count": sum(
                1
                for item in matrix["file_changes"]
                if item.get("component_ownership", {}).get("status") != "known"
            ),
            "result": "closed",
        },
    )
    _, receipt_path = next_stage_path(
        plan,
        "impact_receipt",
        revision=source_revision,
    )
    write_json_once(receipt_path, receipt)
    return receipt


def _load_impact_receipt(plan: LoadedPlan) -> dict[str, Any]:
    document = _load_revision_artifact(
        plan,
        "impact_receipt",
        "ChangeDecisionReceipt",
        CHANGE_DECISION_RECEIPT_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "impact_matrix",
            "change_decision",
            "file_decision_count",
            "surface_decision_count",
            "client_impacts",
            "successor_campaign_required",
            "shared_contract_required",
            "unclassified_count",
            "official_client_identity_change_count",
            "result",
        },
        optional_fields={
            "component_mapping_schema",
            "component_mapping_version",
            "component_mapping_sha256",
            "auto_accepted_count",
            "manual_decision_count",
            "unknown_component_count",
        },
    )
    mapping_fields = {
        "component_mapping_schema": COMPONENT_OWNERSHIP_SCHEMA,
        "component_mapping_version": COMPONENT_OWNERSHIP_VERSION,
        "component_mapping_sha256": COMPONENT_OWNERSHIP_SHA256,
    }
    for field, expected in mapping_fields.items():
        if field in document and document[field] != expected:
            raise UpstreamMergeError(f"ChangeDecisionReceipt {field} 漂移")
    mapping_presence = [field in document for field in mapping_fields]
    if any(mapping_presence) and not all(mapping_presence):
        raise UpstreamMergeError("ChangeDecisionReceipt 组件映射字段不完整")
    for field in ("file_decision_count", "surface_decision_count", "unclassified_count", "official_client_identity_change_count"):
        value = document[field]
        minimum = 1 if field == "file_decision_count" else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise UpstreamMergeError(f"ChangeDecisionReceipt {field} 非法")
    if document["unclassified_count"] != 0 or document["official_client_identity_change_count"] != 0:
        raise UpstreamMergeError("ChangeDecisionReceipt 仍有未分类或身份变更项")
    for field in ("client_impacts", "successor_campaign_required"):
        value = document[field]
        if not isinstance(value, dict) or set(value) != set(CLIENT_KEYS) or any(
            not isinstance(item, bool) for item in value.values()
        ):
            raise UpstreamMergeError(f"ChangeDecisionReceipt.{field} 必须是两 Persona 布尔映射")
    if not isinstance(document["shared_contract_required"], bool):
        raise UpstreamMergeError("ChangeDecisionReceipt.shared_contract_required 必须是布尔值")
    for field in ("auto_accepted_count", "manual_decision_count", "unknown_component_count"):
        if field in document:
            value = document[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise UpstreamMergeError(f"ChangeDecisionReceipt.{field} 非法")
    if "unknown_component_count" in document and document["unknown_component_count"] > document["file_decision_count"]:
        raise UpstreamMergeError("ChangeDecisionReceipt.unknown_component_count 超出文件决策数")
    if "auto_accepted_count" in document and "manual_decision_count" in document:
        if document["auto_accepted_count"] + document["manual_decision_count"] != (
            document["file_decision_count"] + document["surface_decision_count"]
        ):
            raise UpstreamMergeError("ChangeDecisionReceipt 自动/人工计数不闭合")
    matrix_binding = validate_artifact_binding(
        plan.evidence_root,
        document["impact_matrix"],
        "ChangeDecisionReceipt.impact_matrix",
    )
    matrix_path = resolve_within(
        plan.evidence_root,
        matrix_binding["path"],
        "ChangeDecisionReceipt.impact_matrix.path",
    )
    decision_binding = validate_file_binding(
        document["change_decision"],
        "ChangeDecisionReceipt.change_decision",
    )
    decision_path = Path(decision_binding["path"])
    decision_document = expect_object(
        load_json(decision_path, "ChangeDecision"),
        "ChangeDecision",
    )
    if decision_document.get("schema_version") != CHANGE_DECISION_INPUT_SCHEMA:
        raise UpstreamMergeError("ChangeDecisionReceipt.change_decision schema_version 非法")
    revision = document.get("revision")
    is_revision_receipt = isinstance(revision, int) and not isinstance(revision, bool)
    if "identity_sha256" in decision_document:
        validate_identity(decision_document, "ChangeDecisionReceipt.change_decision")
    elif is_revision_receipt:
        raise UpstreamMergeError("新格式 ChangeDecision 必须包含 identity_sha256")
    if is_revision_receipt:
        source = _load_source_candidate(plan)
        if (
            decision_document.get("plan_id") != plan.plan_id
            or decision_document.get("plan_identity_sha256") != plan.identity
            or decision_document.get("source_tree") != source["source_tree"]
            or decision_document.get("impact_matrix_sha256") != sha256_file(matrix_path)
        ):
            raise UpstreamMergeError("新格式 ChangeDecision 与当前计划/ImpactMatrix 绑定不一致")
        files = decision_document.get("files")
        surface_deltas = decision_document.get("surface_deltas")
        if not isinstance(files, list) or not isinstance(surface_deltas, list):
            raise UpstreamMergeError("新格式 ChangeDecision files/surface_deltas 必须是数组")
        if document["file_decision_count"] != len(files):
            raise UpstreamMergeError("ChangeDecisionReceipt.file_decision_count 与决策文件数不一致")
        if document["surface_decision_count"] != len(surface_deltas):
            raise UpstreamMergeError("ChangeDecisionReceipt.surface_decision_count 与决策发送面数不一致")
        identity_changes = sum(
            1
            for item in files
            if isinstance(item, dict) and item.get("official_client_identity_changed") is True
        )
        if document["official_client_identity_change_count"] != identity_changes:
            raise UpstreamMergeError("ChangeDecisionReceipt 身份变更计数不一致")
        auto_count = sum(
            1
            for item in files
            if isinstance(item, dict) and item.get("decision_source", "manual") == "auto"
        )
        manual_count = sum(
            1
            for item in files
            if not isinstance(item, dict) or item.get("decision_source", "manual") != "auto"
        ) + len(surface_deltas)
        if "auto_accepted_count" in document and document["auto_accepted_count"] != auto_count:
            raise UpstreamMergeError("ChangeDecisionReceipt.auto_accepted_count 与决策不一致")
        if "manual_decision_count" in document and document["manual_decision_count"] != manual_count:
            raise UpstreamMergeError("ChangeDecisionReceipt.manual_decision_count 与决策不一致")
        matrix_document = _load_impact_matrix(plan)
        matrix_changes = matrix_document.get("file_changes", [])
        unknown_count = sum(
            1
            for item in matrix_changes
            if not isinstance(item, dict)
            or not isinstance(item.get("component_ownership"), dict)
            or item["component_ownership"].get("status") != "known"
        )
        if "unknown_component_count" in document and document["unknown_component_count"] != unknown_count:
            raise UpstreamMergeError("ChangeDecisionReceipt.unknown_component_count 与 ImpactMatrix 不一致")
    if is_revision_receipt:
        _validate_current_stage_binding(
            plan,
            document["impact_matrix"],
            "impact_matrix",
            "ChangeDecisionReceipt.impact_matrix",
        )
    return document


def preflight_revisions(
    plan: LoadedPlan,
    transition_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """在进入 U-4 前一次性复核当前 U-2/U-3 收据和源码 transition 链。

    该检查只读，不生成或改写任何制品。它把最常见的“源码已经修好，但
    Surface/Impact/Transition 仍绑定上一轮”问题提前挡住，避免把失败拖到
    长时间的 U-4 门禁之后才发现。
    """

    source = _load_source_candidate(plan)
    surface = _load_surface_receipt(plan)
    impact = _load_impact_matrix(plan)
    decision = _load_impact_receipt(plan)
    if surface.get("result") != "closed":
        raise UpstreamMergeError("U-2 SurfaceRecalculationReceipt 尚未闭合")
    if impact.get("result") not in {"pending_change_decision", "closed"}:
        raise UpstreamMergeError("U-3 ImpactMatrix 结果非法")
    if decision.get("result") != "closed":
        raise UpstreamMergeError("U-3 ChangeDecisionReceipt 尚未闭合")
    worktree = _worktree_root(plan)
    if rev_parse(worktree, "HEAD^{commit}") != source["source_commit"]:
        raise UpstreamMergeError("revision preflight 的 worktree HEAD 与 SourceCandidate 不一致")
    assert_clean(worktree, "U-2/U-3 revision preflight")

    validated_transitions: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for raw_path in transition_paths or ():
        if not raw_path.is_absolute():
            raise UpstreamMergeError("revision preflight transition 路径必须是绝对路径")
        try:
            path = raw_path.resolve(strict=True)
        except OSError as error:
            raise UpstreamMergeError(
                f"revision preflight transition 不存在或不可读取：{raw_path}"
            ) from error
        if path in seen_paths:
            raise UpstreamMergeError(f"revision preflight transition 不得重复：{path}")
        seen_paths.add(path)
        validated_transitions.append(validate_source_transition(plan.repository_root, path))

    source_revision = source.get("revision", latest_revision(plan, "source_candidate"))
    return {
        "result": "ready",
        "source_revision": source_revision,
        "source_commit": source["source_commit"],
        "surface_revision": surface.get("revision", source_revision),
        "impact_revision": impact.get("revision", source_revision),
        "transition_count": len(validated_transitions),
        "transitions": validated_transitions,
    }


def _expand_gate_value(
    value: str,
    *,
    plan: LoadedPlan,
    source: dict[str, Any],
    receipt: Path | None,
    repository: Path,
) -> str:
    replacements = {
        "{candidate_commit}": source["source_commit"],
        "{candidate_tree}": source["source_tree"],
        "{evidence_root}": str(plan.evidence_root),
        "{plan}": str(plan.path),
        "{repository}": str(repository),
    }
    if receipt is not None:
        replacements["{receipt}"] = str(receipt)
    result = value
    for marker, replacement in replacements.items():
        result = result.replace(marker, replacement)
    if re.search(r"\{[a-z_]+\}", result):
        raise UpstreamMergeError(f"门禁 argv 有未解析占位符：{value}")
    return result


def _gate_cwd(worktree: Path, raw: str) -> Path:
    if raw == ".":
        return worktree
    relative = safe_relative_path(raw, "gate.cwd")
    path = (worktree / PurePosixPath(relative)).resolve()
    if not path.is_relative_to(worktree.resolve()) or path.is_symlink() or not path.is_dir():
        raise UpstreamMergeError(f"门禁 cwd 不可信：{raw}")
    return path


CLIENT_GATE_CATEGORIES = frozenset(
    {
        "claude_active_wire",
        "claude_ingress_matrix",
        "claude_rollback_wire",
        "codex_active_wire",
        "codex_ingress_matrix",
        "codex_rollback_wire",
    }
)
CLIENT_GATE_RECEIPT_SCHEMA = "official-egress-upstream-client-gate-receipt/v2"

# 门禁编排脚本（tools/upstream_merge/gate_runner.py）的机器可读结果。
RUNNER_STATUS_SCHEMA = "official-egress-upstream-gate-runner-status/v1"
RUNNER_STATUS_FIELDS = {"schema_version", "mode", "jobs", "elapsed_seconds", "steps", "failed", "not_executed", "result"}
RUNNER_STEP_FIELDS = {"lane", "step", "status", "exit_code", "duration_seconds", "reason"}
RUNNER_STEP_STATUSES = {"passed", "failed", "not_executed"}
# 本机未执行、可由同一候选提交的 CI 证据补齐的检查闭集；其余未执行一律拒绝。
CI_COVERABLE_CHECKS = frozenset({"go-tests/go-test-integration"})
CI_WORKFLOW_PATH = ".github/workflows/backend-ci.yml"
CI_REQUIRED_JOBS = (
    "capture-tools (1)",
    "capture-tools (2)",
    "capture-tools (3)",
    "capture-tools (4)",
    "egress-spec-gates",
    "frontend",
    "golangci-lint",
    "shell",
    "test",
)
CI_EVIDENCE_FIELDS = {
    "provider",
    "repository",
    "run_id",
    "run_attempt",
    "head_sha",
    "workflow_path",
    "event",
    "head_branch",
    "status",
    "conclusion",
    "jobs",
    "run_document",
    "jobs_document",
    "test_job_log",
    "covers",
}
CI_BRANCH_PREFIX = "upstream-merge/"
# CI test 作业先跑 make test-unit、再跑 make test-integration；integration 真实执行的判据是
# 该命令之后出现 repository 包的 ok 行（缺 Docker 时 CI=true 让它失败）。日志行可带时间戳前缀。
CI_INTEGRATION_COMMAND = "go test -tags=integration ./..."
CI_INTEGRATION_OK_RE = re.compile(r"(?:^|\s)ok\s+github\.com/Wei-Shaw/sub2api/internal/repository\s")
GITHUB_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _load_runner_status(
    plan: LoadedPlan,
    binding: Any,
    label: str,
    *,
    exit_code: int,
) -> dict[str, Any]:
    """校验编排脚本的状态文件：字段闭合、逐步状态与汇总一致、与退出码一致。"""

    validate_artifact_binding(plan.evidence_root, binding, label)
    path = resolve_within(plan.evidence_root, binding["path"], label)
    document = expect_object(load_json(path, label), label)
    expect_exact_fields(document, RUNNER_STATUS_FIELDS, label)
    if document.get("schema_version") != RUNNER_STATUS_SCHEMA:
        raise UpstreamMergeError(f"{label} schema_version 非法")
    steps = document.get("steps")
    if not isinstance(steps, list) or not steps:
        raise UpstreamMergeError(f"{label}.steps 必须是非空数组")
    failed: list[str] = []
    not_executed: list[str] = []
    for index, raw in enumerate(steps):
        step = expect_object(raw, f"{label}.steps[{index}]")
        expect_exact_fields(step, RUNNER_STEP_FIELDS, f"{label}.steps[{index}]")
        status = validate_string_enum(step.get("status"), RUNNER_STEP_STATUSES, f"{label}.steps[{index}].status")
        key = f"{expect_string(step.get('lane'), 'lane')}/{expect_string(step.get('step'), 'step')}"
        if status == "failed":
            failed.append(key)
        elif status == "not_executed":
            if step.get("exit_code") is not None:
                raise UpstreamMergeError(f"{label}.steps[{index}] 未执行却带退出码")
            not_executed.append(key)
        elif step.get("exit_code") != 0:
            raise UpstreamMergeError(f"{label}.steps[{index}] 通过但退出码非 0")
    if document.get("failed") != failed or document.get("not_executed") != not_executed:
        raise UpstreamMergeError(f"{label} failed／not_executed 与逐步状态不一致")
    expected = "failed" if failed else ("awaiting_ci" if not_executed else "passed")
    if document.get("result") != expected:
        raise UpstreamMergeError(f"{label}.result 与逐步状态不一致")
    if (expected == "failed") != (exit_code != 0):
        raise UpstreamMergeError(f"{label} 与门禁退出码矛盾")
    uncovered = sorted(set(not_executed) - CI_COVERABLE_CHECKS)
    if uncovered:
        raise UpstreamMergeError(f"{label} 存在 CI 无法补齐的未执行项：{uncovered}")
    return document


def _not_executed_checks(plan: LoadedPlan, gates: Sequence[dict[str, Any]]) -> list[str]:
    """汇总各执行组状态文件里的未执行项（同组成员共享同一份状态文件）。"""

    collected: set[str] = set()
    seen: set[str] = set()
    for gate in gates:
        binding = gate.get("runner_status")
        if binding is None or binding["path"] in seen:
            continue
        seen.add(binding["path"])
        document = _load_runner_status(plan, binding, f"runner_status {gate['id']}", exit_code=gate["exit_code"])
        collected.update(document["not_executed"])
    return sorted(collected)


def _ci_integration_executed(log_text: str) -> bool:
    """test 作业日志里，integration 命令之后必须出现 repository 包的 ok 行。"""

    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        if CI_INTEGRATION_COMMAND in line:
            return any(CI_INTEGRATION_OK_RE.search(later) for later in lines[index + 1 :])
    return False


def _check_ci_facts(
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
    log_text: str,
    *,
    run_id: int,
    head_sha: str,
) -> None:
    """同一候选提交、指定工作流、全部作业成功、integration 真实执行；任一不满足即拒绝。"""

    if run.get("id") != run_id:
        raise UpstreamMergeError("CI run id 与请求不一致")
    if run.get("head_sha") != head_sha:
        raise UpstreamMergeError(f"CI run 的 head_sha 不是候选提交：{run.get('head_sha')} != {head_sha}")
    if run.get("path") != CI_WORKFLOW_PATH:
        raise UpstreamMergeError(f"CI run 不是 {CI_WORKFLOW_PATH}：{run.get('path')}")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise UpstreamMergeError(f"CI run 未成功完成：status={run.get('status')} conclusion={run.get('conclusion')}")
    names = [job.get("name") for job in jobs]
    missing = sorted(set(CI_REQUIRED_JOBS) - set(names))
    if missing:
        raise UpstreamMergeError(f"CI run 缺少必需作业：{missing}")
    unsuccessful = sorted(str(job.get("name")) for job in jobs if job.get("conclusion") != "success")
    if unsuccessful:
        raise UpstreamMergeError(f"CI run 存在未成功的作业：{unsuccessful}")
    if not _ci_integration_executed(log_text):
        raise UpstreamMergeError("CI test 作业日志里没有 integration 真实执行的证据")


def _validate_ci_evidence(
    plan: LoadedPlan,
    value: Any,
    *,
    head_sha: str,
    not_executed: list[str],
) -> dict[str, Any]:
    """重新读取收据绑定的 CI 原始记录复核全部事实，使导入后的收据可离线重放。"""

    evidence = expect_object(value, "VerificationReceipt.ci_evidence")
    expect_exact_fields(evidence, CI_EVIDENCE_FIELDS, "VerificationReceipt.ci_evidence")
    if evidence.get("provider") != "github-actions":
        raise UpstreamMergeError("ci_evidence.provider 非法")
    repository = expect_string(evidence.get("repository"), "ci_evidence.repository")
    if not GITHUB_SLUG_RE.fullmatch(repository):
        raise UpstreamMergeError("ci_evidence.repository 非法")
    run_id = evidence.get("run_id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise UpstreamMergeError("ci_evidence.run_id 非法")
    documents: dict[str, Path] = {}
    for field in ("run_document", "jobs_document", "test_job_log"):
        validate_artifact_binding(plan.evidence_root, evidence.get(field), f"ci_evidence.{field}")
        documents[field] = resolve_within(plan.evidence_root, evidence[field]["path"], f"ci_evidence.{field}")
    run = expect_object(load_json(documents["run_document"], "ci run"), "ci run")
    jobs_document = expect_object(load_json(documents["jobs_document"], "ci jobs"), "ci jobs")
    jobs = jobs_document.get("jobs")
    if not isinstance(jobs, list):
        raise UpstreamMergeError("ci jobs 文档缺少 jobs 数组")
    log_text = documents["test_job_log"].read_text(encoding="utf-8", errors="replace")
    _check_ci_facts(run, jobs, log_text, run_id=run_id, head_sha=head_sha)
    summary = {
        "run_attempt": run.get("run_attempt"),
        "head_sha": run.get("head_sha"),
        "workflow_path": run.get("path"),
        "event": run.get("event"),
        "head_branch": run.get("head_branch"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "jobs": _ci_job_summary(jobs),
    }
    for field, expected in summary.items():
        if evidence.get(field) != expected:
            raise UpstreamMergeError(f"ci_evidence.{field} 与绑定的原始记录不一致")
    if evidence.get("covers") != not_executed or not not_executed:
        raise UpstreamMergeError("ci_evidence.covers 必须恰好等于本机未执行项")
    return evidence


def _ci_job_summary(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        ({"name": job.get("name"), "id": job.get("id"), "conclusion": job.get("conclusion")} for job in jobs),
        key=lambda item: (str(item["name"]), str(item["id"])),
    )


def _gate_group(gate: dict[str, Any]) -> str:
    return str(gate.get("execution_group") or gate["id"])


def _gate_signature(gate: dict[str, Any]) -> tuple[Any, ...]:
    """同一 execution_group 必须确实执行同一条命令。"""

    return (
        gate["mode"],
        gate["cwd"],
        tuple(gate["argv"]),
        gate.get("receipt") if gate["mode"] == "receipt_replay" else None,
    )


def _gate_groups(plan: LoadedPlan) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for gate in plan.document["gates"]:
        groups.setdefault(_gate_group(gate), []).append(gate)
    for values in groups.values():
        values.sort(key=lambda item: item["id"])
        signatures = {_gate_signature(item) for item in values}
        if len(signatures) != 1:
            raise UpstreamMergeError(
                "同一 execution_group 的门禁定义不一致："
                + ",".join(item["id"] for item in values)
            )
    return groups


def _gate_ids_from_selector(
    plan: LoadedPlan,
    selector: str | Sequence[str] | None,
) -> set[str] | None:
    if selector is None:
        return None
    values: list[str] = []
    if isinstance(selector, str):
        values = [item.strip() for item in selector.split(",") if item.strip()]
    else:
        for item in selector:
            if not isinstance(item, str):
                raise UpstreamMergeError("--only 选择器必须是字符串")
            values.extend(part.strip() for part in item.split(",") if part.strip())
    if not values:
        raise UpstreamMergeError("--only 不能为空")
    by_id = {gate["id"]: gate for gate in plan.document["gates"]}
    by_category = {gate["category"]: gate for gate in plan.document["gates"]}
    selected: set[str] = set()
    for value in values:
        gate = by_id.get(value) or by_category.get(value)
        if gate is None:
            raise UpstreamMergeError(f"--only 引用了未知门禁：{value}")
        selected.add(gate["id"])
    return selected


def _resolve_attempt_receipt(plan: LoadedPlan, reference: str | Path) -> Path:
    raw = Path(reference) if isinstance(reference, str) else reference
    if raw.is_absolute():
        if raw.is_symlink():
            raise UpstreamMergeError("--from-attempt 不得指向符号链接")
        candidate = raw.resolve(strict=False)
        if not candidate.is_relative_to(plan.evidence_root.resolve()):
            raise UpstreamMergeError("--from-attempt 必须位于 evidence root 内")
    else:
        text = str(raw)
        try:
            safe_id = expect_safe_id(text, "from_attempt")
            candidate = resolve_within(
                plan.evidence_root,
                f"{plan.output_relative('gate_attempts_root')}/{safe_id}/receipt.json",
                "from_attempt",
            )
        except UpstreamMergeError:
            relative = safe_relative_path(text, "from_attempt")
            raw_candidate = plan.evidence_root / PurePosixPath(relative)
            if raw_candidate.is_symlink():
                raise UpstreamMergeError("--from-attempt 不得指向符号链接")
            candidate = resolve_within(plan.evidence_root, relative, "from_attempt")
    if candidate.is_dir():
        candidate = candidate / "receipt.json"

    if candidate.is_symlink() or not candidate.is_file():
        raise UpstreamMergeError(f"上一 attempt 收据不存在：{candidate}")
    resolved = candidate.resolve(strict=True)
    attempts_root = resolve_within(
        plan.evidence_root,
        plan.output_relative("gate_attempts_root"),
        "gate attempts root",
    ).resolve(strict=True)
    if resolved.name != "receipt.json" or resolved.parent.parent != attempts_root:
        raise UpstreamMergeError("--from-attempt 必须指向 evidence root/u4/attempts/<attempt>/receipt.json")
    attempt_name = resolved.parent.name
    expect_safe_id(attempt_name, "from_attempt.attempt_id")
    return resolved


def _validate_attempt_receipt_binding(
    plan: LoadedPlan,
    value: Any,
    label: str,
    *,
    expected_attempt: str | None = None,
    expected_client_category: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """校验 attempt 收据绑定必须落在标准 evidence 路径。"""

    binding = expect_object(value, label)
    relative = safe_relative_path(binding.get("path"), f"{label}.path")
    raw_path = plan.evidence_root / PurePosixPath(relative)
    if raw_path.is_symlink():
        raise UpstreamMergeError(f"{label} 不得指向符号链接")
    validated = validate_artifact_binding(plan.evidence_root, binding, label)
    path = resolve_within(plan.evidence_root, relative, f"{label}.path")
    attempts_root = resolve_within(
        plan.evidence_root,
        plan.output_relative("gate_attempts_root"),
        "gate attempts root",
    ).resolve(strict=True)
    if expected_client_category is None:
        if path.name != "receipt.json" or path.parent.parent.resolve() != attempts_root:
            raise UpstreamMergeError(
                f"{label} 必须指向 evidence root/u4/attempts/<attempt>/receipt.json"
            )
        attempt_id = expect_safe_id(path.parent.name, f"{label}.attempt_id")
    else:
        expected_client_category = validate_string_enum(
            expected_client_category,
            CLIENT_GATE_CATEGORIES,
            f"{label}.category",
        )
        attempt_id = expect_safe_id(path.parent.parent.name, f"{label}.attempt_id")
        expected_path = (
            attempts_root
            / attempt_id
            / "client-receipts"
            / f"{expected_client_category}.json"
        ).resolve(strict=False)
        if path != expected_path:
            raise UpstreamMergeError(
                f"{label} 必须指向 evidence root/u4/attempts/<attempt>/client-receipts/{expected_client_category}.json"
            )
    if expected_attempt is not None and attempt_id != expected_attempt:
        raise UpstreamMergeError(
            f"{label} attempt_id 不一致：expected={expected_attempt} actual={attempt_id}"
        )
    return validated, path


def _client_receipt_document(
    plan: LoadedPlan,
    attempt: str,
    gate: dict[str, Any],
    result: dict[str, Any],
    attempt_root: Path,
) -> tuple[dict[str, Any], Path]:
    category = gate["category"]
    output = attempt_root / "client-receipts" / f"{category}.json"
    document = _stage_document(
        plan,
        CLIENT_GATE_RECEIPT_SCHEMA,
        {
            "attempt_id": attempt,
            "category": category,
            "gate_id": gate["id"],
            "source_candidate": stage_binding(plan, "source_candidate"),
            "impact_receipt": stage_binding(plan, "impact_receipt"),
            "gate_definition_sha256": sha256_bytes(canonical_bytes(gate)),
            "execution_group": _gate_group(gate),
            "execution_status": result.get("execution_status", "executed"),
            "result": result["status"],
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "executable": result["executable"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        },
    )
    write_json_once(output, document)
    return document, output


FRONTEND_LOCKFILE_RELATIVE = "frontend/pnpm-lock.yaml"
# pnpm 安装后把实际使用的锁文件存在这里；与 frontend/pnpm-lock.yaml 字节一致即说明依赖按当前锁文件装好。
FRONTEND_CURRENT_LOCKFILE_RELATIVE = "frontend/node_modules/.pnpm/lock.yaml"


def _assert_frontend_dependencies(worktree: Path) -> None:
    """UM-16：门禁执行树的前端依赖缺失或与锁文件不一致时拒绝开跑，不生成 attempt。

    v0.2.10 合并时 Plan 003 的工作树没装依赖，前端三条检查线与采集工具检查线开跑 2 秒内即失败，
    这一轮的这些检查线白跑。候选树没有 ``frontend/pnpm-lock.yaml`` 时不适用。
    """

    wanted = worktree / FRONTEND_LOCKFILE_RELATIVE
    if not wanted.is_file():
        return
    current = worktree / FRONTEND_CURRENT_LOCKFILE_RELATIVE
    hint = f"先执行 pnpm --dir {worktree / 'frontend'} install --frozen-lockfile 再开跑"
    if not current.is_file():
        raise UpstreamMergeError(
            f"门禁执行树未安装前端依赖（缺 {FRONTEND_CURRENT_LOCKFILE_RELATIVE}），前端与采集工具检查线必然失败；{hint}"
        )
    if current.read_bytes() != wanted.read_bytes():
        raise UpstreamMergeError(f"门禁执行树的前端依赖与 {FRONTEND_LOCKFILE_RELATIVE} 不一致；{hint}")


def _install_frontend_dependencies_offline(worktree: Path) -> None:
    """replay --rerun-gates 的临时执行树由工具创建，人来不及装依赖：用本机 pnpm 存储离线安装，不联网。"""

    if not (worktree / FRONTEND_LOCKFILE_RELATIVE).is_file():
        return
    try:
        completed = run_process(
            ("pnpm", "--dir", str(worktree / "frontend"), "install", "--frozen-lockfile", "--offline"),
            cwd=worktree,
            check=False,
        )
    except OSError as error:
        raise UpstreamMergeError(f"重跑门禁的临时执行树无法离线安装前端依赖：{error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr.strip() or completed.stdout.strip())[-800:]
        raise UpstreamMergeError(f"重跑门禁的临时执行树无法离线安装前端依赖：{detail}")


def _run_verification_gates_in_worktree(
    plan: LoadedPlan,
    attempt_id: str,
    execution_worktree: Path,
    *,
    only: str | Sequence[str] | None = None,
    from_attempt: str | Path | None = None,
) -> dict[str, Any]:
    attempt = expect_safe_id(attempt_id, "attempt_id")
    impact = _load_impact_receipt(plan)
    if impact["result"] != "closed" or impact["unclassified_count"] != 0:
        raise UpstreamMergeError("U-3 ChangeDecisionReceipt 未闭合")
    source = _load_source_candidate(plan)
    worktree = _validated_linked_worktree(plan, execution_worktree)
    if rev_parse(worktree, "HEAD^{commit}") != source["source_commit"]:
        raise UpstreamMergeError("U-4 门禁执行树与 SourceCandidate 不一致")
    assert_clean(worktree, "U-4 门禁执行树")
    _assert_frontend_dependencies(worktree)
    attempt_root_relative = f"{plan.output_relative('gate_attempts_root')}/{attempt}"
    attempt_root = resolve_within(plan.evidence_root, attempt_root_relative, "gate attempt")
    if attempt_root.exists():
        raise UpstreamMergeError(f"门禁 attempt 已存在，禁止覆盖：{attempt}")

    groups = _gate_groups(plan)
    planned_by_id = {gate["id"]: gate for gate in plan.document["gates"]}
    selected = _gate_ids_from_selector(plan, only)
    previous_path: Path | None = None
    previous: dict[str, Any] | None = None
    if from_attempt is not None:
        previous_path = _resolve_attempt_receipt(plan, from_attempt)
        if previous_path.parent.name == attempt:
            raise UpstreamMergeError("新 attempt 不得引用自身收据")
        previous = load_verification_receipt(plan, previous_path, require_passed=False)
        if str(previous.get("attempt_id")) != previous_path.parent.name:
            raise UpstreamMergeError("上一 attempt 收据路径与 attempt_id 不一致")
        previous_by_id = {item["id"]: item for item in previous["gates"]}
        failed_previous = {item["id"] for item in previous["gates"] if item["status"] != "passed"}
        if selected is None:
            selected = failed_previous
            if not selected:
                raise UpstreamMergeError("上一 attempt 没有失败门禁；如需重跑请显式提供 --only")
        elif not failed_previous.issubset(selected):
            raise UpstreamMergeError(
                "--only 未覆盖上一 attempt 的全部失败门禁："
                + ",".join(sorted(failed_previous - selected))
            )
    else:
        previous_by_id = {}
        if selected is None:
            selected = set(planned_by_id)

    # 选择一个组即执行整个组，保证逻辑门禁仍全部有收据；没有上一收据时不得留下空洞。
    selected_groups = {
        _gate_group(planned_by_id[gate_id]) for gate_id in selected
    }
    expanded_selected = {
        gate["id"] for group in selected_groups for gate in groups[group]
    }
    if previous is None and expanded_selected != set(planned_by_id):
        raise UpstreamMergeError("首次 gates-run 必须覆盖全部 12 类门禁")
    for gate_id, prior in previous_by_id.items():
        if gate_id not in expanded_selected and prior["status"] != "passed":
            raise UpstreamMergeError(f"未选择的失败门禁不能被复用：{gate_id}")

    # 所有输入和复用关系都通过校验后才创建目录；参数错误不会留下伪 attempt。
    attempt_root.mkdir(parents=True, mode=0o700)
    attempt_root.chmod(0o700)

    results_by_id: dict[str, dict[str, Any]] = {}
    executed_groups = 0
    reused_gate_count = 0
    for group_name in sorted(groups):
        group = groups[group_name]
        leader = group[0]
        if group_name not in selected_groups:
            for gate in group:
                prior = previous_by_id.get(gate["id"])
                if prior is None or prior["status"] != "passed":
                    raise UpstreamMergeError(f"门禁没有可复用的通过收据：{gate['id']}")
                copied = dict(prior)
                copied["execution_group"] = group_name
                copied["execution_leader_id"] = prior.get("execution_leader_id", gate["id"])
                copied["execution_status"] = "attempt_reused"
                copied["reused_from"] = artifact_binding(plan.evidence_root, previous_path) if previous_path else None
                results_by_id[gate["id"]] = copied
                reused_gate_count += 1
            continue

        receipt_path: Path | None = None
        receipt_binding: dict[str, Any] | None = None
        if leader["mode"] == "receipt_replay":
            receipt_path = resolve_within(
                plan.evidence_root,
                leader["receipt"],
                f"gate {leader['id']} receipt",
            )
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise UpstreamMergeError(
                    f"receipt_replay 门禁缺少候选专属收据：{leader['id']}={receipt_path}"
                )
            receipt_binding = artifact_binding(plan.evidence_root, receipt_path)
        argv = [
            _expand_gate_value(
                item,
                plan=plan,
                source=source,
                receipt=receipt_path,
                repository=worktree,
            )
            for item in leader["argv"]
        ]
        cwd = _gate_cwd(worktree, leader["cwd"])
        executable = executable_identity(argv[0], cwd)
        # 编排脚本把逐步结果写到这里；本机未执行的检查据此标为 awaiting_ci，而不是算作通过。
        status_path = attempt_root / f"{leader['id']}.runner-status.json"
        started = time.monotonic()
        completed = run_process(
            argv,
            cwd=cwd,
            check=False,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "UPSTREAM_MERGE_PLAN": str(plan.path),
                "UPSTREAM_GATE_STATUS_FILE": str(status_path),
            },
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        runner_status: dict[str, Any] | None = None
        if status_path.is_symlink() or (status_path.exists() and not status_path.is_file()):
            raise UpstreamMergeError(f"门禁状态文件不可信：{status_path}")
        if status_path.is_file():
            runner_status = artifact_binding(plan.evidence_root, status_path)
            _load_runner_status(
                plan,
                runner_status,
                f"门禁 {leader['id']} 状态文件",
                exit_code=completed.returncode,
            )
        stdout_path = attempt_root / f"{leader['id']}.stdout.txt"
        stderr_path = attempt_root / f"{leader['id']}.stderr.txt"
        write_once(stdout_path, completed.stdout.encode("utf-8"))
        write_once(stderr_path, completed.stderr.encode("utf-8"))
        stdout_binding = artifact_binding(plan.evidence_root, stdout_path)
        stderr_binding = artifact_binding(plan.evidence_root, stderr_path)
        status = "passed" if completed.returncode == 0 else "failed"
        executed_groups += 1
        for gate in group:
            results_by_id[gate["id"]] = {
                "id": gate["id"],
                "category": gate["category"],
                "mode": gate["mode"],
                "cwd": gate["cwd"],
                "argv": gate["argv"],
                "expanded_argv_sha256": sha256_bytes(canonical_bytes(argv)),
                "executable": executable,
                "source_receipt": receipt_binding,
                "exit_code": completed.returncode,
                "duration_ms": duration_ms if gate["id"] == leader["id"] else 0,
                "stdout": stdout_binding,
                "stderr": stderr_binding,
                "status": status,
                "execution_group": group_name,
                "execution_leader_id": leader["id"],
                "execution_status": "executed" if gate["id"] == leader["id"] else "group_reused",
                "reused_from": None,
                "runner_status": runner_status,
            }

    results = [results_by_id[gate["id"]] for gate in plan.document["gates"]]
    client_receipts: dict[str, dict[str, Any]] = {}
    for gate in plan.document["gates"]:
        if gate["category"] not in CLIENT_GATE_CATEGORIES:
            continue
        _, client_path = _client_receipt_document(
            plan,
            attempt,
            gate,
            results_by_id[gate["id"]],
            attempt_root,
        )
        client_receipts[gate["category"]] = artifact_binding(plan.evidence_root, client_path)

    dirty_paths = status_paths(worktree)
    failed = sorted(item["id"] for item in results if item["status"] != "passed")
    not_executed = _not_executed_checks(plan, results)
    if failed or dirty_paths:
        result = "blocked"
    elif not_executed:
        # 本机没执行的检查不能算通过：等 gates-import-ci 绑定同一候选提交的 CI 证据。
        result = "awaiting_ci"
    else:
        result = "passed"
    document = _stage_document(
        plan,
        VERIFICATION_RECEIPT_SCHEMA,
        {
            "attempt_id": attempt,
            **(
                {"baseline_acceptance": plan.baseline_acceptance}
                if plan.baseline_acceptance is not None
                else {}
            ),
            "source_candidate": stage_binding(plan, "source_candidate"),
            "impact_receipt": stage_binding(plan, "impact_receipt"),
            "required_categories": list(REQUIRED_GATE_CATEGORIES),
            "gate_count": len(results),
            "failed_gate_ids": failed,
            "skipped_gate_count": 0,
            "worktree_status_paths": dirty_paths,
            "gates": results,
            "client_receipts": client_receipts,
            "executed_gate_count": sum(
                1 for item in results if item.get("execution_status") == "executed"
            ),
            "reused_gate_count": reused_gate_count,
            "execution_group_count": executed_groups,
            "from_attempt": artifact_binding(plan.evidence_root, previous_path)
            if previous_path
            else None,
            "selected_gate_ids": sorted(expanded_selected),
            "not_executed_checks": not_executed,
            "ci_evidence": None,
            "result": result,
        },
    )
    receipt_path = attempt_root / "receipt.json"
    write_json_once(receipt_path, document)
    return document


class GitHubActionsApi:
    """用 gh CLI 只读访问 GitHub Actions；测试以假实现替换。"""

    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root

    def _get(self, endpoint: str) -> bytes:
        completed = subprocess.run(
            ["gh", "api", endpoint],
            cwd=self.repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise UpstreamMergeError(f"gh api {endpoint} 失败：{detail}")
        return completed.stdout

    def run(self, repository: str, run_id: int) -> dict[str, Any]:
        return expect_object(json.loads(self._get(f"repos/{repository}/actions/runs/{run_id}")), "CI run")

    def jobs(self, repository: str, run_id: int) -> list[dict[str, Any]]:
        document = expect_object(
            json.loads(self._get(f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100")),
            "CI jobs",
        )
        jobs = document.get("jobs")
        if not isinstance(jobs, list) or document.get("total_count") != len(jobs):
            raise UpstreamMergeError("CI jobs 列表不完整")
        return jobs

    def job_log(self, repository: str, job_id: int) -> str:
        return self._get(f"repos/{repository}/actions/jobs/{job_id}/logs").decode("utf-8", errors="replace")

    def find_run(self, repository: str, head_sha: str) -> int | None:
        document = expect_object(
            json.loads(self._get(f"repos/{repository}/actions/runs?head_sha={head_sha}&per_page=50")),
            "CI runs",
        )
        runs = [
            item
            for item in document.get("workflow_runs", [])
            if isinstance(item, dict) and item.get("path") == CI_WORKFLOW_PATH and item.get("head_sha") == head_sha
        ]
        if not runs:
            return None
        runs.sort(key=lambda item: (item.get("status") == "completed", str(item.get("created_at"))))
        return int(runs[-1]["id"])


def _origin_slug(repository_root: Path, remote: str = "origin") -> str:
    url = remote_url(repository_root, remote)
    match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    if match is None:
        raise UpstreamMergeError(f"{remote} 不是 GitHub 仓库地址：{url}")
    return match.group(1)


def _ci_branch(plan: LoadedPlan) -> str:
    return f"{CI_BRANCH_PREFIX}{plan.plan_id}"


def push_candidate_for_ci(plan: LoadedPlan, *, remote: str = "origin") -> dict[str, Any]:
    """U-4：把封存的候选提交推到 upstream-merge/<plan_id>，让 CI 在同一提交上执行。

    只推这个临时分支，不推送受维护分支；CI 结论由 gates-import-ci 按提交 SHA 绑定。
    """

    expect_safe_id(remote, "remote")
    source = _load_source_candidate(plan)
    branch = _ci_branch(plan)
    run_git(plan.repository_root, "push", remote, f"{source['source_commit']}:refs/heads/{branch}")
    return {"result": "pushed", "remote": remote, "branch": branch, "commit": source["source_commit"]}


def delete_ci_branch(plan: LoadedPlan, *, remote: str = "origin") -> dict[str, Any]:
    """U-6 之后删除临时 CI 分支；分支已不存在时如实返回 absent。"""

    expect_safe_id(remote, "remote")
    branch = _ci_branch(plan)
    # 先确认分支存在：不同传输方式下删除不存在的分支，git 的退出码并不一致。
    listed = run_git(plan.repository_root, "ls-remote", "--exit-code", remote, f"refs/heads/{branch}", check=False)
    if listed.returncode == 2:
        return {"result": "absent", "remote": remote, "branch": branch}
    if listed.returncode != 0:
        raise UpstreamMergeError(f"无法查询临时 CI 分支：{listed.stderr.strip()}")
    run_git(plan.repository_root, "push", remote, "--delete", f"refs/heads/{branch}")
    return {"result": "deleted", "remote": remote, "branch": branch}


def import_ci_evidence(
    plan: LoadedPlan,
    attempt_id: str,
    from_attempt: str | Path,
    *,
    run_id: int | None = None,
    repository_slug: str | None = None,
    api: Any | None = None,
) -> dict[str, Any]:
    """U-4：把同一候选提交的 CI 证据导入为新 attempt，只补齐本机未执行的检查。

    前序 attempt 必须是 awaiting_ci：有本机失败或工作树污染（blocked）时一律拒绝，CI 全绿也
    不能覆盖本机结论。全部校验在写任何文件之前完成；新 attempt 原样复用前序门禁结果，另绑定
    CI run、作业清单与 test 作业日志的原始记录，保证收据可离线复算。
    """

    attempt = expect_safe_id(attempt_id, "attempt_id")
    attempt_root = resolve_within(
        plan.evidence_root,
        f"{plan.output_relative('gate_attempts_root')}/{attempt}",
        "gate attempt",
    )
    if attempt_root.exists():
        raise UpstreamMergeError(f"门禁 attempt 已存在，禁止覆盖：{attempt}")
    previous_path = _resolve_attempt_receipt(plan, from_attempt)
    if previous_path.parent.name == attempt:
        raise UpstreamMergeError("新 attempt 不得引用自身收据")
    previous = load_verification_receipt(plan, previous_path, require_passed=False)
    if previous["result"] == "blocked":
        raise UpstreamMergeError(
            "上一 attempt 有本机失败或执行后污染工作树；CI 证据只补齐本机未执行的检查，不能覆盖本机失败"
        )
    if previous["result"] != "awaiting_ci":
        raise UpstreamMergeError("上一 attempt 已通过，无需导入 CI 证据")
    not_executed = list(previous["not_executed_checks"])
    head_sha = _load_source_candidate(plan)["source_commit"]
    slug = repository_slug or _origin_slug(plan.repository_root)
    if not GITHUB_SLUG_RE.fullmatch(slug):
        raise UpstreamMergeError(f"GitHub 仓库标识非法：{slug}")
    api = api if api is not None else GitHubActionsApi(plan.repository_root)
    if run_id is None:
        run_id = api.find_run(slug, head_sha)
        if run_id is None:
            raise UpstreamMergeError(
                f"找不到候选提交 {head_sha} 的 {CI_WORKFLOW_PATH} 运行；先用 ci-push 推送候选并等 CI 结束"
            )
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise UpstreamMergeError("CI run id 非法")
    run = api.run(slug, run_id)
    jobs = api.jobs(slug, run_id)
    test_job = next((job for job in jobs if job.get("name") == "test"), None)
    if test_job is None:
        raise UpstreamMergeError("CI run 缺少 test 作业")
    log_text = api.job_log(slug, test_job["id"])
    _check_ci_facts(run, jobs, log_text, run_id=run_id, head_sha=head_sha)

    attempt_root.mkdir(parents=True, mode=0o700)
    attempt_root.chmod(0o700)
    ci_root = attempt_root / "ci"
    ci_root.mkdir(mode=0o700)
    run_path = ci_root / "run.json"
    jobs_path = ci_root / "jobs.json"
    log_path = ci_root / "test-job.log"
    write_json_once(run_path, run)
    write_json_once(jobs_path, {"jobs": jobs})
    write_once(log_path, log_text.encode("utf-8"))
    evidence = {
        "provider": "github-actions",
        "repository": slug,
        "run_id": run_id,
        "run_attempt": run.get("run_attempt"),
        "head_sha": run.get("head_sha"),
        "workflow_path": run.get("path"),
        "event": run.get("event"),
        "head_branch": run.get("head_branch"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "jobs": _ci_job_summary(jobs),
        "run_document": artifact_binding(plan.evidence_root, run_path),
        "jobs_document": artifact_binding(plan.evidence_root, jobs_path),
        "test_job_log": artifact_binding(plan.evidence_root, log_path),
        "covers": not_executed,
    }
    previous_binding = artifact_binding(plan.evidence_root, previous_path)
    previous_by_id = {item["id"]: item for item in previous["gates"]}
    results_by_id: dict[str, dict[str, Any]] = {}
    for group_name, group in _gate_groups(plan).items():
        for gate in group:
            prior = previous_by_id[gate["id"]]
            copied = dict(prior)
            copied["execution_group"] = group_name
            copied["execution_leader_id"] = prior.get("execution_leader_id", gate["id"])
            copied["execution_status"] = "attempt_reused"
            copied["reused_from"] = previous_binding
            results_by_id[gate["id"]] = copied
    results = [results_by_id[gate["id"]] for gate in plan.document["gates"]]
    client_receipts: dict[str, dict[str, Any]] = {}
    for gate in plan.document["gates"]:
        if gate["category"] not in CLIENT_GATE_CATEGORIES:
            continue
        _, client_path = _client_receipt_document(plan, attempt, gate, results_by_id[gate["id"]], attempt_root)
        client_receipts[gate["category"]] = artifact_binding(plan.evidence_root, client_path)
    document = _stage_document(
        plan,
        VERIFICATION_RECEIPT_SCHEMA,
        {
            "attempt_id": attempt,
            **(
                {"baseline_acceptance": plan.baseline_acceptance}
                if plan.baseline_acceptance is not None
                else {}
            ),
            "source_candidate": stage_binding(plan, "source_candidate"),
            "impact_receipt": stage_binding(plan, "impact_receipt"),
            "required_categories": list(REQUIRED_GATE_CATEGORIES),
            "gate_count": len(results),
            "failed_gate_ids": [],
            "skipped_gate_count": 0,
            "worktree_status_paths": previous["worktree_status_paths"],
            "gates": results,
            "client_receipts": client_receipts,
            "executed_gate_count": 0,
            "reused_gate_count": len(results),
            "execution_group_count": 0,
            "from_attempt": previous_binding,
            "selected_gate_ids": [],
            "not_executed_checks": not_executed,
            "ci_evidence": evidence,
            "result": "passed",
        },
    )
    receipt_path = attempt_root / "receipt.json"
    write_json_once(receipt_path, document)
    # 自检：新收据必须能被 U-5 使用的同一套标准校验接受。
    load_verification_receipt(plan, receipt_path, require_passed=True)
    return document


def run_verification_gates(
    plan: LoadedPlan,
    attempt_id: str,
    *,
    only: str | Sequence[str] | None = None,
    from_attempt: str | Path | None = None,
) -> dict[str, Any]:
    """执行 U-4；失败重跑只执行失败组，其余门禁以绑定收据复用。"""

    return _run_verification_gates_in_worktree(
        plan,
        attempt_id,
        plan.worktree,
        only=only,
        from_attempt=from_attempt,
    )


def _validate_client_gate_receipt(
    plan: LoadedPlan,
    binding: Any,
    expected_category: str,
    expected_gate: dict[str, Any],
    expected_result: dict[str, Any],
    attempt_id: str,
) -> None:
    _validated, path = _validate_attempt_receipt_binding(
        plan,
        binding,
        f"VerificationReceipt.client_receipts.{expected_category}",
        expected_attempt=attempt_id,
        expected_client_category=expected_category,
    )
    document = artifact_document(
        path,
        "ClientGateReceipt",
        CLIENT_GATE_RECEIPT_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "attempt_id",
            "category",
            "gate_id",
            "source_candidate",
            "impact_receipt",
            "gate_definition_sha256",
            "execution_group",
            "execution_status",
            "result",
            "exit_code",
            "duration_ms",
            "executable",
            "stdout",
            "stderr",
        },
    )
    if (
        document["plan_id"] != plan.plan_id
        or document["plan_identity_sha256"] != plan.identity
    ):
        raise UpstreamMergeError(f"客户端门禁自动收据计划身份不一致：{expected_category}")
    if document["execution_status"] not in {
        "executed",
        "group_reused",
        "attempt_reused",
    }:
        raise UpstreamMergeError(f"客户端门禁自动收据 execution_status 非法：{expected_category}")
    duration_ms = document["duration_ms"]
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise UpstreamMergeError(f"客户端门禁自动收据 duration_ms 非法：{expected_category}")
    exit_code = document["exit_code"]
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise UpstreamMergeError(f"客户端门禁自动收据 exit_code 非法：{expected_category}")
    if document["result"] != ("passed" if exit_code == 0 else "failed"):
        raise UpstreamMergeError(f"客户端门禁自动收据 result 与 exit_code 矛盾：{expected_category}")
    expect_safe_id(document["attempt_id"], f"ClientGateReceipt.{expected_category}.attempt_id")
    for stream in ("stdout", "stderr"):
        validate_artifact_binding(
            plan.evidence_root,
            document[stream],
            f"ClientGateReceipt.{expected_category}.{stream}",
        )
    expected_executable = _expected_gate_executable(
        plan,
        expected_gate,
        expected_result,
        f"ClientGateReceipt.{expected_category}.executable",
        expanded_command=expected_result["executable"]["command"],
    )
    if (
        document["attempt_id"] != attempt_id
        or document["category"] != expected_category
        or document["gate_id"] != expected_gate["id"]
        or document["source_candidate"] != stage_binding(plan, "source_candidate")
        or document["impact_receipt"] != stage_binding(plan, "impact_receipt")
        or document["gate_definition_sha256"] != sha256_bytes(canonical_bytes(expected_gate))
        or document["execution_group"] != _gate_group(expected_gate)
        or (
            expected_result.get("execution_status") is not None
            and document["execution_status"] != expected_result["execution_status"]
        )
        or document["result"] != expected_result["status"]
        or document["exit_code"] != expected_result["exit_code"]
        or document["duration_ms"] != expected_result["duration_ms"]
        or document["executable"] != expected_executable
        or document["stdout"] != expected_result["stdout"]
        or document["stderr"] != expected_result["stderr"]
    ):
        raise UpstreamMergeError(f"客户端门禁自动收据绑定不一致：{expected_category}")


def _expected_gate_executable(
    plan: LoadedPlan,
    planned: dict[str, Any],
    actual: dict[str, Any],
    label: str,
    *,
    expanded_command: str | None = None,
) -> dict[str, Any]:
    """复算门禁首个 argv 的实际可执行文件身份。"""

    executable = expect_object(actual.get("executable"), label)
    expect_exact_fields(executable, {"command", "resolved_path", "sha256", "bytes"}, label)
    command = expanded_command or planned["argv"][0]
    if executable.get("command") != command:
        raise UpstreamMergeError(f"{label} command 与计划不一致")
    # 相对可执行文件需要在执行树中解析；PATH 命令不依赖 worktree 是否仍保留。
    if "/" in command and not Path(command).is_absolute():
        if not plan.worktree.exists() or not plan.worktree.is_dir():
            raise UpstreamMergeError(f"{label} 无法复算相对可执行文件：执行树不存在")
        cwd = _gate_cwd(plan.worktree, planned["cwd"])
    else:
        cwd = plan.worktree if plan.worktree.is_dir() else plan.repository_root
    expected = executable_identity(command, cwd)
    if executable != expected:
        raise UpstreamMergeError(f"{label} 摘要或解析路径漂移")
    return expected


def _validate_recorded_executable(value: Any, label: str) -> dict[str, Any]:
    """仅校验历史收据中可执行文件身份的结构，不依赖已清理的旧运行环境。"""

    executable = expect_object(value, label)
    expect_exact_fields(executable, {"command", "resolved_path", "sha256", "bytes"}, label)
    expect_string(executable.get("command"), f"{label}.command")
    resolved_path = expect_string(executable.get("resolved_path"), f"{label}.resolved_path")
    path = Path(resolved_path)
    if not path.is_absolute() or str(path) != os.path.normpath(resolved_path):
        raise UpstreamMergeError(f"{label}.resolved_path 必须是规范绝对路径")
    digest = executable.get("sha256")
    size = executable.get("bytes")
    if digest is not None:
        expect_sha256(digest, f"{label}.sha256")
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or size < 0
    ):
        raise UpstreamMergeError(f"{label}.bytes 非法")
    if (digest is None) != (size is None):
        raise UpstreamMergeError(f"{label}.sha256/bytes 必须同时为空或同时存在")
    return executable


def load_verification_receipt(
    plan: LoadedPlan,
    path: Path,
    *,
    require_passed: bool,
    _seen_paths: set[Path] | None = None,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"VerificationReceipt 不是可信普通文件：{path}")
    resolved_receipt_path = path.resolve(strict=True)
    seen_paths = _seen_paths if _seen_paths is not None else set()
    if resolved_receipt_path in seen_paths:
        raise UpstreamMergeError("VerificationReceipt from_attempt 存在循环")
    seen_paths.add(resolved_receipt_path)
    # U-4 主收据也必须位于标准 attempt 目录；这样外部传入的任意 JSON 不能被
    # 当作通过凭证注入后续 U-5/U-6。
    _validate_attempt_receipt_binding(
        plan,
        artifact_binding(plan.evidence_root, resolved_receipt_path),
        "VerificationReceipt",
    )
    document = expect_object(
        load_json(resolved_receipt_path, "VerificationReceipt"),
        "VerificationReceipt",
    )
    required_fields = {
        "schema_version",
        "plan_id",
        "plan_identity_sha256",
        "attempt_id",
        "source_candidate",
        "impact_receipt",
        "required_categories",
        "gate_count",
        "failed_gate_ids",
        "skipped_gate_count",
        "worktree_status_paths",
        "gates",
        "result",
        "identity_sha256",
    }
    optional_fields = {
        "baseline_acceptance",
        "client_receipts",
        "executed_gate_count",
        "reused_gate_count",
        "execution_group_count",
        "from_attempt",
        "selected_gate_ids",
        "not_executed_checks",
        "ci_evidence",
    }
    actual_fields = set(document)
    if actual_fields - (required_fields | optional_fields) or required_fields - actual_fields:
        raise UpstreamMergeError(
            "VerificationReceipt 字段不闭合："
            f"缺失={sorted(required_fields - actual_fields)}，"
            f"多余={sorted(actual_fields - required_fields - optional_fields)}"
        )
    execution_metadata_fields = {
        "client_receipts",
        "executed_gate_count",
        "reused_gate_count",
        "execution_group_count",
        "from_attempt",
        "selected_gate_ids",
    }
    gate_metadata_fields = {
        "execution_group",
        "execution_leader_id",
        "execution_status",
        "reused_from",
    }
    raw_gate_values = document.get("gates", [])
    gate_metadata_present = (
        isinstance(raw_gate_values, list)
        and any(
            isinstance(item, dict) and set(item) & gate_metadata_fields
            for item in raw_gate_values
        )
    )
    has_execution_metadata = bool(set(document) & execution_metadata_fields) or gate_metadata_present
    if has_execution_metadata and "client_receipts" not in document:
        raise UpstreamMergeError(
            "新格式 VerificationReceipt 必须生成六类 client_receipts"
        )
    if document.get("schema_version") != VERIFICATION_RECEIPT_SCHEMA:
        raise UpstreamMergeError("VerificationReceipt schema_version 非法")
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError("VerificationReceipt 计划身份不一致")
    expect_safe_id(document.get("attempt_id"), "VerificationReceipt.attempt_id")
    validate_identity(document, "VerificationReceipt")
    if document.get("source_candidate") != stage_binding(plan, "source_candidate"):
        raise UpstreamMergeError("VerificationReceipt SourceCandidate 绑定漂移")
    if document.get("impact_receipt") != stage_binding(plan, "impact_receipt"):
        raise UpstreamMergeError("VerificationReceipt ChangeDecisionReceipt 绑定漂移")
    if plan.baseline_acceptance is not None:
        if document.get("baseline_acceptance") != plan.baseline_acceptance:
            raise UpstreamMergeError("VerificationReceipt 基线验收收据绑定漂移")
    if document.get("required_categories") != list(REQUIRED_GATE_CATEGORIES):
        raise UpstreamMergeError("VerificationReceipt 固定门禁类别漂移")
    gates = document.get("gates")
    if not isinstance(gates, list) or len(gates) != len(plan.document["gates"]):
        raise UpstreamMergeError("VerificationReceipt 门禁数量不闭合")
    if document.get("gate_count") != len(gates):
        raise UpstreamMergeError("VerificationReceipt gate_count 与逐门禁结果不一致")
    by_id = {item["id"]: item for item in gates if isinstance(item, dict) and "id" in item}
    if set(by_id) != {item["id"] for item in plan.document["gates"]}:
        raise UpstreamMergeError("VerificationReceipt 门禁身份不闭合")
    source = _load_source_candidate(plan)
    groups = _gate_groups(plan)
    from_attempt_binding = document.get("from_attempt")
    from_attempt_path: Path | None = None
    if has_execution_metadata and from_attempt_binding is not None:
        _, from_attempt_path = _validate_attempt_receipt_binding(
            plan,
            from_attempt_binding,
            "VerificationReceipt.from_attempt",
        )
        if from_attempt_path.parent.name == str(document["attempt_id"]):
            raise UpstreamMergeError("VerificationReceipt.from_attempt 不得指向自身")
    previous_by_id: dict[str, dict[str, Any]] = {}
    previous_document: dict[str, Any] | None = None
    if from_attempt_path is not None:
        previous_document = load_verification_receipt(
            plan,
            from_attempt_path,
            require_passed=False,
            _seen_paths=set(seen_paths),
        )
        if str(previous_document.get("attempt_id")) != from_attempt_path.parent.name:
            raise UpstreamMergeError("VerificationReceipt.from_attempt attempt_id 与路径不一致")
        previous_by_id = {
            item["id"]: item
            for item in previous_document.get("gates", [])
            if isinstance(item, dict) and "id" in item
        }
    for planned in plan.document["gates"]:
        actual = by_id[planned["id"]]
        base_fields = {
            "id",
            "category",
            "mode",
            "cwd",
            "argv",
            "expanded_argv_sha256",
            "executable",
            "source_receipt",
            "exit_code",
            "duration_ms",
            "stdout",
            "stderr",
            "status",
        }
        optional_gate_fields = {
            "execution_group",
            "execution_leader_id",
            "execution_status",
            "reused_from",
            "runner_status",
        }
        if set(actual) - (base_fields | optional_gate_fields) or base_fields - set(actual):
            raise UpstreamMergeError(f"VerificationReceipt 门禁字段不闭合：{planned['id']}")
        if any(actual.get(field) != planned[field] for field in ("id", "category", "mode", "cwd", "argv")):
            raise UpstreamMergeError(f"VerificationReceipt 门禁定义漂移：{planned['id']}")
        expected_group = _gate_group(planned)
        if "execution_group" in actual and actual["execution_group"] != expected_group:
            raise UpstreamMergeError(f"VerificationReceipt execution_group 漂移：{planned['id']}")
        if "execution_leader_id" in actual:
            leader = actual["execution_leader_id"]
            if leader not in {item["id"] for item in groups[expected_group]}:
                raise UpstreamMergeError(f"VerificationReceipt 执行组 leader 非法：{planned['id']}")
        receipt_path: Path | None = None
        if planned["mode"] == "receipt_replay":
            receipt_path = resolve_within(
                plan.evidence_root,
                planned["receipt"],
                f"gate {planned['id']} receipt",
            )
        expanded = [
            _expand_gate_value(
                value,
                plan=plan,
                source=source,
                receipt=receipt_path,
                repository=plan.worktree.resolve(strict=False),
            )
            for value in planned["argv"]
        ]
        if actual.get("expanded_argv_sha256") != sha256_bytes(canonical_bytes(expanded)):
            raise UpstreamMergeError(f"VerificationReceipt 门禁展开命令漂移：{planned['id']}")
        if has_execution_metadata:
            _expected_gate_executable(
                plan,
                planned,
                actual,
                f"VerificationReceipt.gates.{planned['id']}.executable",
                expanded_command=expanded[0],
            )
        else:
            _validate_recorded_executable(
                actual.get("executable"),
                f"VerificationReceipt.gates.{planned['id']}.executable",
            )
        exit_code = actual.get("exit_code")
        duration_ms = actual.get("duration_ms")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise UpstreamMergeError(f"VerificationReceipt 退出码非法：{planned['id']}")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise UpstreamMergeError(f"VerificationReceipt 执行时长非法：{planned['id']}")
        expected_status = "passed" if exit_code == 0 else "failed"
        if actual.get("status") != expected_status:
            raise UpstreamMergeError(f"VerificationReceipt 门禁状态与退出码矛盾：{planned['id']}")
        for stream in ("stdout", "stderr"):
            validate_artifact_binding(
                plan.evidence_root,
                actual.get(stream),
                f"VerificationReceipt.gates.{planned['id']}.{stream}",
            )
        if planned["mode"] == "receipt_replay":
            if receipt_path is None:
                raise UpstreamMergeError(f"receipt_replay 门禁缺少收据：{planned['id']}")
            expected_binding = artifact_binding(plan.evidence_root, receipt_path)
            if actual.get("source_receipt") != expected_binding:
                raise UpstreamMergeError(f"receipt_replay 来源收据漂移：{planned['id']}")
        elif actual.get("source_receipt") is not None:
            raise UpstreamMergeError(f"command 门禁不得伪造来源收据：{planned['id']}")
        if has_execution_metadata and any(
            field not in actual for field in gate_metadata_fields
        ):
            raise UpstreamMergeError(
                f"新格式 VerificationReceipt 门禁 execution metadata 不完整：{planned['id']}"
            )
        if "execution_status" in actual and actual["execution_status"] not in {
            "executed",
            "group_reused",
            "attempt_reused",
        }:
            raise UpstreamMergeError(f"VerificationReceipt execution_status 非法：{planned['id']}")
        if actual.get("execution_status") == "attempt_reused":
            if from_attempt_binding is None:
                raise UpstreamMergeError(
                    f"attempt_reused 门禁缺少 top-level from_attempt：{planned['id']}"
                )
            reused_binding, _ = _validate_attempt_receipt_binding(
                plan,
                actual.get("reused_from"),
                f"VerificationReceipt.gates.{planned['id']}.reused_from",
            )
            if reused_binding != from_attempt_binding:
                raise UpstreamMergeError(
                    f"门禁 reused_from 未绑定 top-level from_attempt：{planned['id']}"
                )
            prior = previous_by_id.get(planned["id"])
            if prior is None or prior.get("status") != "passed":
                raise UpstreamMergeError(
                    f"attempt_reused 门禁没有对应的已通过前序结果：{planned['id']}"
                )
            for field in (
                "mode",
                "cwd",
                "argv",
                "expanded_argv_sha256",
                "executable",
                "source_receipt",
                "exit_code",
                "duration_ms",
                "stdout",
                "stderr",
                "status",
                "runner_status",
            ):
                if actual.get(field) != prior.get(field):
                    raise UpstreamMergeError(
                        f"attempt_reused 门禁结果未完整复用前序收据：{planned['id']}"
                    )
        elif "reused_from" in actual and actual.get("reused_from") is not None:
            raise UpstreamMergeError(
                f"非 attempt_reused 门禁不得带 reused_from：{planned['id']}"
            )
    # 同一执行组只能有一个真实执行者；其余逻辑类别必须共享同一结果和产物。
    for group_name, group in groups.items():
        group_results = [by_id[item["id"]] for item in group]
        statuses = {item.get("status") for item in group_results}
        if len(statuses) != 1:
            raise UpstreamMergeError(f"VerificationReceipt 执行组结果不一致：{group_name}")
        common_fields = (
            "mode",
            "cwd",
            "argv",
            "expanded_argv_sha256",
            "executable",
            "source_receipt",
            "exit_code",
            "stdout",
            "stderr",
            "status",
            "runner_status",
        )
        for field in common_fields:
            if len(
                {
                    sha256_bytes(canonical_bytes(item.get(field)))
                    for item in group_results
                }
            ) != 1:
                raise UpstreamMergeError(
                    f"VerificationReceipt 执行组 {field} 不一致：{group_name}"
                )
        execution_statuses = [
            item.get("execution_status")
            for item in group_results
            if "execution_status" in item
        ]
        group_metadata_presence = {
            field: [field in item for item in group_results]
            for field in ("execution_group", "execution_leader_id", "execution_status")
        }
        if any(
            any(presence) and not all(presence)
            for presence in group_metadata_presence.values()
        ):
            raise UpstreamMergeError(f"VerificationReceipt 执行组 metadata 不完整：{group_name}")
        if has_execution_metadata and any(
            not all(presence) for presence in group_metadata_presence.values()
        ):
            raise UpstreamMergeError(f"新格式 VerificationReceipt 执行组 metadata 不完整：{group_name}")
        if execution_statuses:
            if len(execution_statuses) != len(group_results):
                raise UpstreamMergeError(f"VerificationReceipt 执行组 execution_status 不完整：{group_name}")
            leaders = {
                item.get("execution_leader_id") for item in group_results
            }
            if len(leaders) != 1 or next(iter(leaders)) not in {item["id"] for item in group}:
                raise UpstreamMergeError(f"VerificationReceipt 执行组 leader 不闭合：{group_name}")
            leader_id = next(iter(leaders))
            if execution_statuses.count("executed") == 1:
                if leader_id != next(
                    item["id"]
                    for item in group_results
                    if item.get("execution_status") == "executed"
                ):
                    raise UpstreamMergeError(f"VerificationReceipt 执行组 leader 身份不一致：{group_name}")
                if any(
                    item.get("execution_status") != "group_reused"
                    for item in group_results
                    if item["id"] != leader_id
                ):
                    raise UpstreamMergeError(f"VerificationReceipt 执行组复用标记不一致：{group_name}")
                if any(
                    item.get("duration_ms") != 0
                    for item in group_results
                    if item["id"] != leader_id
                ):
                    raise UpstreamMergeError(
                        f"VerificationReceipt 执行组成员 duration_ms 必须为 0：{group_name}"
                    )
            elif all(status == "attempt_reused" for status in execution_statuses):
                for item in group_results:
                    reused_binding, _ = _validate_attempt_receipt_binding(
                        plan,
                        item.get("reused_from"),
                        f"VerificationReceipt.gates.{item['id']}.reused_from",
                    )
                    if from_attempt_binding is None or reused_binding != from_attempt_binding:
                        raise UpstreamMergeError(
                            f"VerificationReceipt 执行组 reused_from 未统一：{group_name}"
                        )
            else:
                raise UpstreamMergeError(f"VerificationReceipt 执行组 execution_status 不合法：{group_name}")
            for item in group_results:
                if item.get("execution_status") == "group_reused" and item.get("reused_from") is not None:
                    raise UpstreamMergeError(
                        f"VerificationReceipt group_reused 不得带 reused_from：{item['id']}"
                    )
        elif any(
            field in group_results[0]
            for field in ("execution_group", "execution_leader_id", "reused_from")
        ):
            raise UpstreamMergeError(f"VerificationReceipt 执行组 metadata 缺少 execution_status：{group_name}")
    failed = sorted(item["id"] for item in gates if item.get("status") != "passed")
    if document.get("failed_gate_ids") != failed:
        raise UpstreamMergeError("VerificationReceipt failed_gate_ids 与逐门禁结果不一致")
    if document.get("skipped_gate_count") != 0:
        raise UpstreamMergeError("VerificationReceipt 不允许跳过门禁")
    dirty_paths = document.get("worktree_status_paths")
    if not isinstance(dirty_paths, list):
        raise UpstreamMergeError("VerificationReceipt worktree_status_paths 必须是数组")
    normalized_dirty = [
        safe_relative_path(value, f"VerificationReceipt.worktree_status_paths[{index}]")
        for index, value in enumerate(dirty_paths)
    ]
    if normalized_dirty != sorted(set(normalized_dirty)):
        raise UpstreamMergeError("VerificationReceipt worktree_status_paths 必须排序且不重复")
    not_executed = _not_executed_checks(plan, gates)
    if "not_executed_checks" in document:
        if document["not_executed_checks"] != not_executed:
            raise UpstreamMergeError("VerificationReceipt.not_executed_checks 与门禁状态文件不一致")
    elif not_executed:
        raise UpstreamMergeError("VerificationReceipt 缺少 not_executed_checks")
    ci_evidence = document.get("ci_evidence")
    if ci_evidence is not None:
        # 导入收据只补齐未执行项：全部门禁原样复用自一个 awaiting_ci 的前序 attempt。
        if from_attempt_path is None or previous_document is None or any(
            item.get("execution_status") != "attempt_reused" for item in gates
        ):
            raise UpstreamMergeError("带 CI 证据的 VerificationReceipt 必须全部复用前序 attempt")
        if previous_document.get("result") != "awaiting_ci" or previous_document.get(
            "not_executed_checks"
        ) != not_executed:
            raise UpstreamMergeError("CI 证据只能补齐 awaiting_ci 前序 attempt 的同一组未执行项")
        _validate_ci_evidence(
            plan,
            ci_evidence,
            head_sha=source["source_commit"],
            not_executed=not_executed,
        )
    if failed or normalized_dirty:
        expected_result = "blocked"
    elif not_executed and ci_evidence is None:
        expected_result = "awaiting_ci"
    else:
        expected_result = "passed"
    if document.get("result") != expected_result:
        raise UpstreamMergeError("VerificationReceipt result 与门禁／工作树结果矛盾")

    client_receipts = document.get("client_receipts")
    if has_execution_metadata:
        if not isinstance(client_receipts, dict) or set(client_receipts) != set(CLIENT_GATE_CATEGORIES):
            raise UpstreamMergeError(
                "新格式 VerificationReceipt.client_receipts 必须恰好覆盖六类客户端门禁"
            )
    if client_receipts is not None:
        if not isinstance(client_receipts, dict) or set(client_receipts) != set(CLIENT_GATE_CATEGORIES):
            raise UpstreamMergeError("VerificationReceipt.client_receipts 必须恰好覆盖六类客户端门禁")
        for planned in plan.document["gates"]:
            category = planned["category"]
            if category not in CLIENT_GATE_CATEGORIES:
                continue
            _validate_client_gate_receipt(
                plan,
                client_receipts[category],
                category,
                planned,
                by_id[planned["id"]],
                str(document["attempt_id"]),
            )
    for field in ("executed_gate_count", "reused_gate_count", "execution_group_count"):
        if field in document:
            value = document[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise UpstreamMergeError(f"VerificationReceipt.{field} 非法")
    if "selected_gate_ids" in document:
        selected_ids = document["selected_gate_ids"]
        if not isinstance(selected_ids, list) or selected_ids != sorted(set(selected_ids)):
            raise UpstreamMergeError("VerificationReceipt.selected_gate_ids 必须排序且不重复")
        for value in selected_ids:
            if value not in by_id:
                raise UpstreamMergeError("VerificationReceipt.selected_gate_ids 引用未知门禁")
        selected_set = set(selected_ids)
        for group_name, group in groups.items():
            members = {item["id"] for item in group}
            if selected_set & members and selected_set & members != members:
                raise UpstreamMergeError(
                    f"VerificationReceipt.selected_gate_ids 未按 execution_group 闭合：{group_name}"
                )
    if "from_attempt" in document and document["from_attempt"] is not None:
        if from_attempt_path is None:
            _, from_attempt_path = _validate_attempt_receipt_binding(
                plan,
                document["from_attempt"],
                "VerificationReceipt.from_attempt",
            )
    if "executed_gate_count" in document:
        expected_executed = sum(
            1 for item in gates if item.get("execution_status") == "executed"
        )
        if document["executed_gate_count"] != expected_executed:
            raise UpstreamMergeError("VerificationReceipt.executed_gate_count 不一致")
    if "reused_gate_count" in document:
        expected_reused = sum(
            1 for item in gates if item.get("execution_status") == "attempt_reused"
        )
        if document["reused_gate_count"] != expected_reused:
            raise UpstreamMergeError("VerificationReceipt.reused_gate_count 不一致")
    if "execution_group_count" in document:
        expected_groups = sum(
            1
            for group in groups.values()
            if any(item.get("execution_status") == "executed" for item in (by_id[g["id"]] for g in group))
        )
        if document["execution_group_count"] != expected_groups:
            raise UpstreamMergeError("VerificationReceipt.execution_group_count 不一致")
    if require_passed and document.get("result") == "awaiting_ci":
        raise UpstreamMergeError(
            "U-4 本机未执行 "
            + "、".join(not_executed)
            + "：先用 ci-push 推送候选跑 CI，再用 gates-import-ci 导入同一提交的 CI 证据"
        )
    if require_passed and (
        document.get("result") != "passed"
        or failed
        or normalized_dirty
    ):
        raise UpstreamMergeError("U-4 门禁未全部通过或执行后污染 source tree")
    return document


def _nullable_json_path(value: Any, label: str) -> Path | None:
    if value is None:
        return None
    raw = expect_string(value, label)
    path = Path(raw)
    if not path.is_absolute():
        raise UpstreamMergeError(f"{label} 必须是绝对路径或 null")
    load_json(path, label)
    return path.resolve(strict=True)


def seal_candidate_disposition(
    plan: LoadedPlan,
    input_path: Path,
    verification_receipt_path: Path,
) -> dict[str, Any]:
    """按 U-3 影响强制绑定新 candidate、后继 Campaign 及原业务验收。"""

    verification = load_verification_receipt(
        plan,
        verification_receipt_path,
        require_passed=True,
    )
    impact = _load_impact_receipt(plan)
    source = _load_source_candidate(plan)
    document = expect_object(
        load_json(input_path, "CandidateDispositionInput"),
        "CandidateDispositionInput",
    )
    expect_exact_fields(
        document,
        {
            "schema_version",
            "plan_id",
            "plan_identity_sha256",
            "source_tree",
            "purpose",
            "clients",
            "shared_contract_receipt_path",
            "original_business_receipt_path",
            "identity_sha256",
        },
        "CandidateDispositionInput",
    )
    if document.get("schema_version") != CANDIDATE_DISPOSITION_INPUT_SCHEMA:
        raise UpstreamMergeError("CandidateDispositionInput schema_version 非法")
    if (
        document.get("plan_id") != plan.plan_id
        or document.get("plan_identity_sha256") != plan.identity
        or document.get("source_tree") != source["source_tree"]
    ):
        raise UpstreamMergeError("CandidateDispositionInput 身份不一致")
    validate_identity(document, "CandidateDispositionInput")
    purpose = validate_string_enum(
        document.get("purpose"),
        {"production_replacement", "validation_only"},
        "CandidateDispositionInput.purpose",
    )
    clients = expect_object(document.get("clients"), "CandidateDispositionInput.clients")
    expect_exact_fields(clients, set(CLIENT_KEYS), "CandidateDispositionInput.clients")
    sealed_clients: dict[str, Any] = {}
    for client in CLIENT_KEYS:
        label = f"CandidateDispositionInput.clients.{client}"
        raw = expect_object(clients[client], label)
        expect_exact_fields(
            raw,
            {"mode", "campaign_path", "candidate_path", "approval_path", "acceptance_path"},
            label,
        )
        mode = validate_string_enum(
            raw.get("mode"),
            {"new_candidate", "none", "successor_campaign"},
            f"{label}.mode",
        )
        paths = {
            field: _nullable_json_path(raw.get(field), f"{label}.{field}")
            for field in ("campaign_path", "candidate_path", "approval_path", "acceptance_path")
        }
        impacted = bool(impact["client_impacts"][client])
        requires_campaign = bool(impact["successor_campaign_required"][client])
        expected_mode = "successor_campaign" if requires_campaign else (
            "new_candidate" if impacted else "none"
        )
        if mode != expected_mode:
            raise UpstreamMergeError(
                f"{client} 候选处置模式不符合 U-3：expected={expected_mode} actual={mode}"
            )
        if mode == "none":
            if any(path is not None for path in paths.values()):
                raise UpstreamMergeError(f"{client} 无影响时不得借用历史 Campaign/candidate 收据")
            bindings = {field.removesuffix("_path"): None for field in paths}
        else:
            missing = [field for field, path in paths.items() if path is None]
            if missing:
                raise UpstreamMergeError(f"{client} {mode} 缺少绑定：{missing}")
            bindings = {
                field.removesuffix("_path"): file_binding(path)
                for field, path in paths.items()
                if path is not None
            }
        sealed_clients[client] = {
            "impacted": impacted,
            "mode": mode,
            **bindings,
        }
    shared_path = _nullable_json_path(
        document.get("shared_contract_receipt_path"),
        "CandidateDispositionInput.shared_contract_receipt_path",
    )
    if impact["shared_contract_required"] and shared_path is None:
        raise UpstreamMergeError("共享控制合同受影响，必须绑定 Framework §5.4 后继合同收据")
    if not impact["shared_contract_required"] and shared_path is not None:
        raise UpstreamMergeError("共享控制合同未受影响，不得附带无关 §5.4 收据")
    original_business_path = _nullable_json_path(
        document.get("original_business_receipt_path"),
        "CandidateDispositionInput.original_business_receipt_path",
    )
    if original_business_path is None:
        raise UpstreamMergeError("每次上游合并都必须绑定原 Sub2API 业务回归收据")
    receipt = _stage_document(
        plan,
        CANDIDATE_DISPOSITION_SCHEMA,
        {
            "source_candidate": stage_binding(plan, "source_candidate"),
            "impact_receipt": stage_binding(plan, "impact_receipt"),
            "verification_receipt": artifact_binding(
                plan.evidence_root,
                verification_receipt_path.resolve(strict=True),
            ),
            "disposition_input": file_binding(input_path.resolve(strict=True)),
            "purpose": purpose,
            "clients": sealed_clients,
            "shared_contract_receipt": file_binding(shared_path) if shared_path else None,
            "original_business_receipt": file_binding(original_business_path),
            "result": "closed",
        },
    )
    write_json_once(plan.output_path("candidate_disposition"), receipt)
    return receipt


def _load_candidate_disposition(plan: LoadedPlan) -> dict[str, Any]:
    return artifact_document(
        plan.output_path("candidate_disposition"),
        "CandidateDisposition",
        CANDIDATE_DISPOSITION_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "source_candidate",
            "impact_receipt",
            "verification_receipt",
            "disposition_input",
            "purpose",
            "clients",
            "shared_contract_receipt",
            "original_business_receipt",
            "result",
        },
    )


def apply_candidate_to_managed_branch(plan: LoadedPlan) -> dict[str, Any]:
    """显式把已闭合 candidate 快进到受维护分支；不推送远端。"""

    disposition = _load_candidate_disposition(plan)
    if disposition["result"] != "closed":
        raise UpstreamMergeError("U-5 CandidateDisposition 未闭合")
    source = _load_source_candidate(plan)
    verification_path = resolve_within(
        plan.evidence_root,
        disposition["verification_receipt"]["path"],
        "verification receipt",
    )
    load_verification_receipt(plan, verification_path, require_passed=True)
    worktree = _worktree_root(plan)
    if rev_parse(worktree, "HEAD^{commit}") != source["source_commit"]:
        raise UpstreamMergeError("隔离 worktree 未停在已封存 SourceCandidate")
    assert_clean(worktree, "隔离 SourceCandidate")
    assert_clean(plan.repository_root, "受维护分支工作树")
    if current_branch_ref(plan.repository_root) != plan.managed_ref:
        raise UpstreamMergeError("当前分支不是计划受维护分支")
    before = rev_parse(plan.repository_root, "HEAD^{commit}")
    if before != plan.fork_head:
        raise UpstreamMergeError("受维护分支已偏离计划 fork HEAD，禁止应用旧 candidate")
    run_git(
        plan.repository_root,
        "merge",
        "--ff-only",
        source["source_commit"],
    )
    after = rev_parse(plan.repository_root, "HEAD^{commit}")
    after_tree = commit_tree(plan.repository_root, after)
    if after != source["source_commit"] or after_tree != source["source_tree"]:
        raise UpstreamMergeError("受维护分支没有精确快进到 SourceCandidate")
    document = _stage_document(
        plan,
        BRANCH_APPLY_SCHEMA,
        {
            "managed_ref": plan.managed_ref,
            "before_commit": before,
            "after_commit": after,
            "after_tree": after_tree,
            "operation": "git_merge_ff_only",
            "remote_push_performed": False,
            "result": "applied",
        },
    )
    write_json_once(plan.output_path("branch_apply"), document)
    return document


def _load_branch_apply(plan: LoadedPlan) -> dict[str, Any]:
    return artifact_document(
        plan.output_path("branch_apply"),
        "BranchApply",
        BRANCH_APPLY_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "managed_ref",
            "before_commit",
            "after_commit",
            "after_tree",
            "operation",
            "remote_push_performed",
            "result",
        },
    )


def _validate_stage_chain(plan: LoadedPlan) -> dict[str, Any]:
    """只读复算 U-1～U-6 前置制品和 Git 对象关系。"""

    start = _load_merge_start(plan)
    merge_candidate = _load_merge_candidate(plan)
    conflict = artifact_document(
        plan.output_path("conflict_ledger"),
        "ConflictResolutionLedger",
        CONFLICT_LEDGER_SCHEMA,
        {
            "plan_id",
            "plan_identity_sha256",
            "merge_start",
            "conflict_count",
            "conflict_paths",
            "resolution_input",
            "resolutions",
            "result",
        },
    )
    source = _load_source_candidate(plan)
    surface = _load_surface_receipt(plan)
    impact = _load_impact_receipt(plan)
    disposition = _load_candidate_disposition(plan)
    branch_apply = _load_branch_apply(plan)
    merge_reconstruction = _recompute_merge_start(plan, start)
    if merge_candidate["merge_start"] != stage_binding(plan, "merge_start"):
        raise UpstreamMergeError("MergeCandidateTree 未绑定本次 MergeStart")
    if merge_candidate["conflict_ledger"] != stage_binding(plan, "conflict_ledger"):
        raise UpstreamMergeError("MergeCandidateTree 未绑定本次 ConflictResolutionLedger")
    if conflict["merge_start"] != stage_binding(plan, "merge_start") or conflict["result"] != "closed":
        raise UpstreamMergeError("ConflictResolutionLedger 未闭合")
    if conflict["conflict_paths"] != start["conflict_paths"]:
        raise UpstreamMergeError("ConflictResolutionLedger 冲突分母漂移")
    if (
        conflict["conflict_count"] != len(conflict["conflict_paths"])
        or conflict["conflict_count"] != len(conflict["resolutions"])
    ):
        raise UpstreamMergeError("ConflictResolutionLedger 处置数量不闭合")
    merge_commit = merge_candidate["merge_commit"]
    if commit_tree(plan.repository_root, merge_commit) != merge_candidate["candidate_tree"]:
        raise UpstreamMergeError("MergeCandidateTree Git tree 无法复算")
    parent_line = git_output(
        plan.repository_root, "rev-list", "--parents", "-n", "1", merge_commit
    ).split()
    if parent_line[1:] != [plan.fork_head, plan.upstream_commit]:
        raise UpstreamMergeError("MergeCandidateTree 双父身份漂移")
    validate_protected_objects(
        plan.repository_root,
        merge_candidate["candidate_tree"],
        plan.document["repository"]["protected_objects"],
    )
    if commit_tree(plan.repository_root, source["source_commit"]) != source["source_tree"]:
        raise UpstreamMergeError("SourceCandidate Git tree 无法复算")
    if surface["result"] != "closed" or surface["unknown_oauth_egress_count"] != 0:
        raise UpstreamMergeError("U-2 发送面仍有未知 OAuth 出站")
    if impact["result"] != "closed" or impact["unclassified_count"] != 0:
        raise UpstreamMergeError("U-3 影响分类仍有未决项")
    verification_path = resolve_within(
        plan.evidence_root,
        disposition["verification_receipt"]["path"],
        "verification receipt",
    )
    verification = load_verification_receipt(plan, verification_path, require_passed=True)
    if disposition["result"] != "closed":
        raise UpstreamMergeError("U-5 CandidateDisposition 未闭合")
    if (
        branch_apply["result"] != "applied"
        or branch_apply["before_commit"] != plan.fork_head
        or branch_apply["after_commit"] != source["source_commit"]
        or branch_apply["after_tree"] != source["source_tree"]
        or branch_apply["remote_push_performed"] is not False
    ):
        raise UpstreamMergeError("U-6 受维护分支应用收据不一致")
    current = rev_parse(plan.repository_root, f"{plan.managed_ref}^{{commit}}")
    if current != source["source_commit"] or commit_tree(plan.repository_root, current) != source["source_tree"]:
        raise UpstreamMergeError("受维护分支当前 tree 与封存 SourceCandidate 不一致")
    return {
        "merge_start": start,
        "merge_candidate": merge_candidate,
        "conflict": conflict,
        "source": source,
        "surface": surface,
        "impact": impact,
        "verification": verification,
        "verification_path": verification_path,
        "disposition": disposition,
        "branch_apply": branch_apply,
        "merge_reconstruction": merge_reconstruction,
    }


def _expected_upstream_receipt(plan: LoadedPlan) -> dict[str, Any]:
    chain = _validate_stage_chain(plan)
    return _stage_document(
        plan,
        UPSTREAM_RECEIPT_SCHEMA,
        {
            "upstream": plan.document["upstream"],
            "repository": {
                "managed_ref": plan.managed_ref,
                "fork_head": plan.fork_head,
                "fork_tree": plan.document["repository"]["fork_tree"],
                "merge_base": plan.document["repository"]["merge_base"],
                "merge_commit": chain["merge_candidate"]["merge_commit"],
                "merge_tree": chain["merge_candidate"]["candidate_tree"],
                "final_commit": chain["source"]["source_commit"],
                "final_tree": chain["source"]["source_tree"],
            },
            "merge_start": stage_binding(plan, "merge_start"),
            "merge_candidate": stage_binding(plan, "merge_candidate"),
            "conflict_ledger": stage_binding(plan, "conflict_ledger"),
            "source_candidate": stage_binding(plan, "source_candidate"),
            "surface_receipt": stage_binding(plan, "surface_receipt"),
            "impact_matrix": stage_binding(plan, "impact_matrix"),
            "change_decision_receipt": stage_binding(plan, "impact_receipt"),
            "verification_receipt": artifact_binding(
                plan.evidence_root,
                chain["verification_path"],
            ),
            "candidate_disposition": stage_binding(plan, "candidate_disposition"),
            "branch_apply": stage_binding(plan, "branch_apply"),
            "production_baselines": plan.document["baselines"],
            "official_clients": plan.document["official_clients"],
            "tool_bundle_sha256": plan.document["tool_bundle"]["bundle_sha256"],
            "current_tool_blocker_count": 0,
            "result": "upstream_source_baseline_updated",
        },
    )


def finalize_upstream_merge(plan: LoadedPlan) -> dict[str, Any]:
    """签发确定性 UpstreamMergeReceipt；不部署也不推送远端。"""

    document = _expected_upstream_receipt(plan)
    write_json_once(plan.output_path("upstream_merge_receipt"), document)
    return document


def replay_upstream_merge(
    plan: LoadedPlan,
    receipt_path: Path,
    rerun_gate_attempt: str | None = None,
) -> dict[str, Any]:
    """独立重建 U-0～U-6 收据，可选在全新隔离树重跑全部门禁。"""

    actual = expect_object(load_json(receipt_path, "UpstreamMergeReceipt"), "UpstreamMergeReceipt")
    expected = _expected_upstream_receipt(plan)
    if actual != expected:
        raise UpstreamMergeError("UpstreamMergeReceipt 无法由当前计划、Git 对象和阶段制品独立重建")
    rerun_binding: dict[str, Any] | None = None
    if rerun_gate_attempt is not None:
        source_commit = expected["repository"]["final_commit"]
        with _temporary_detached_worktree(plan, source_commit) as worktree:
            _install_frontend_dependencies_offline(worktree)
            rerun_receipt = _run_verification_gates_in_worktree(
                plan,
                rerun_gate_attempt,
                worktree,
            )
        rerun_path = resolve_within(
            plan.evidence_root,
            f"{plan.output_relative('gate_attempts_root')}/{rerun_gate_attempt}/receipt.json",
            "replay gate receipt",
        )
        rerun_binding = artifact_binding(plan.evidence_root, rerun_path)
        if rerun_receipt["result"] != "passed":
            raise UpstreamMergeError("独立重放的 U-4 门禁未全部通过")
    start = _load_merge_start(plan)
    return {
        "schema_version": "official-egress-upstream-merge-replay-result/v1",
        "plan_id": plan.plan_id,
        "receipt_sha256": sha256_file(receipt_path),
        "final_commit": expected["repository"]["final_commit"],
        "final_tree": expected["repository"]["final_tree"],
        "merge_conflict_count": len(start["conflict_paths"]),
        "merge_conflicts_recomputed": True,
        "rerun_verification_receipt": rerun_binding,
        "result": "passed",
    }
