"""U-1～U-6 上游合并阶段实现。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
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
    stage_binding,
)
from .errors import UpstreamMergeError
from .gitops import (
    assert_clean,
    assert_git_repository,
    changed_paths,
    commit_tree,
    current_branch_ref,
    executable_identity,
    git_output,
    rev_parse,
    route_snapshot,
    run_egress_snapshot,
    run_git,
    run_process,
    status_paths,
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
    return artifact_document(
        plan.output_path("source_candidate"),
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
    )


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
                raise UpstreamMergeError(
                    f"{relative} 声明使用 {resolution} 处置，但实际 index 对象不一致"
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
) -> dict[str, Any]:
    document = expect_object(load_json(path, "SourceChangeInput"), "SourceChangeInput")
    expect_exact_fields(
        document,
        {
            "schema_version",
            "plan_id",
            "plan_identity_sha256",
            "merge_commit",
            "entries",
            "identity_sha256",
        },
        "SourceChangeInput",
    )
    if document.get("schema_version") != SOURCE_CHANGE_INPUT_SCHEMA:
        raise UpstreamMergeError("SourceChangeInput schema_version 非法")
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError("SourceChangeInput 计划身份不一致")
    if document.get("merge_commit") != merge_commit:
        raise UpstreamMergeError("SourceChangeInput merge_commit 不一致")
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
    """生成 overlay，并将所有额外源码处置作为一个可审阅 source candidate 封存。"""

    merge_candidate = _load_merge_candidate(plan)
    worktree = _worktree_root(plan)
    if rev_parse(worktree, "HEAD^{commit}") != merge_candidate["merge_commit"]:
        raise UpstreamMergeError("source seal 前 HEAD 不是 U-1 merge commit")
    overlay_path = _generate_overlay(plan, worktree)
    paths = status_paths(worktree)
    overlay_relative = overlay_path.relative_to(worktree).as_posix()
    if overlay_relative not in paths:
        raise UpstreamMergeError("Codex overlay 未进入 source candidate 变化闭集")
    if paths == [overlay_relative] and source_change_input is None:
        change_input_binding = None
    else:
        additional_paths = [relative for relative in paths if relative != overlay_relative]
        if source_change_input is None:
            raise UpstreamMergeError("除自动 overlay 外存在额外变化，必须提供 SourceChangeInput")
        _load_source_change_input(
            source_change_input,
            plan,
            merge_candidate["merge_commit"],
            additional_paths,
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
        f"chore: seal Sub2API {plan.document['upstream']['tag']} overlay",
    )
    source_commit = rev_parse(worktree, "HEAD^{commit}")
    source_tree = commit_tree(worktree, source_commit)
    assert_clean(worktree, "U-2 source candidate")
    document = _stage_document(
        plan,
        SOURCE_CANDIDATE_SCHEMA,
        {
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
    write_json_once(plan.output_path("source_candidate"), document)
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
    route_path = plan.output_path("surface_route_snapshot")
    write_json_once(
        route_path,
        route_snapshot(worktree, source["source_commit"], source["source_tree"]),
    )
    egress_path = plan.output_path("surface_egress_snapshot")
    run_egress_snapshot(worktree, egress_path)

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
    candidate_route = load_json(route_path, "U-2 route snapshot")
    candidate_egress = load_json(egress_path, "U-2 source-to-sink snapshot")
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
            "source_candidate": stage_binding(plan, "source_candidate"),
            "baseline_route_snapshot": baseline_route_binding,
            "candidate_route_snapshot": artifact_binding(plan.evidence_root, route_path),
            "baseline_source_to_sink_snapshot": baseline_egress_binding,
            "candidate_source_to_sink_snapshot": artifact_binding(plan.evidence_root, egress_path),
            "route_delta_count": len(route_deltas),
            "egress_delta_count": len(egress_deltas),
            "deltas": sorted(route_deltas + egress_deltas, key=lambda item: item["delta_id"]),
        },
    )
    write_json_once(plan.output_path("surface_delta"), document)
    return document


def _load_surface_delta(plan: LoadedPlan) -> dict[str, Any]:
    return artifact_document(
        plan.output_path("surface_delta"),
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
        raise UpstreamMergeError(
            f"{client}/{kind} 发现分母有变化，必须由专用流程重新生成 Inventory：{blocking}"
        )
    field = "production_ingress_inventory" if kind == "ingress" else "egress_disposition_inventory"
    baseline = plan.document["baselines"][field][client]
    source_path = Path(baseline["path"])
    output = plan.inventory_output(client, kind)
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
        or document.get("surface_delta_sha256") != sha256_file(plan.output_path("surface_delta"))
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
    inventories: dict[str, dict[str, dict[str, Any]]] = {}
    inventory_bindings: dict[str, dict[str, Any]] = {}
    for client in CLIENT_KEYS:
        inventories[client] = {}
        inventory_bindings[client] = {}
        for kind in ("ingress", "egress"):
            path = plan.inventory_output(client, kind)
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
    write_json_once(plan.output_path("surface_receipt"), document)
    return document


def _load_surface_receipt(plan: LoadedPlan) -> dict[str, Any]:
    return artifact_document(
        plan.output_path("surface_receipt"),
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
    if not categories:
        categories.add("out_of_scope_product")
    return sorted(categories)


def generate_impact_matrix(plan: LoadedPlan) -> dict[str, Any]:
    """生成逐文件和逐发送面差异分母；最终分类必须由独立 ChangeDecision 完成。"""

    surface_receipt = _load_surface_receipt(plan)
    if surface_receipt["result"] != "closed":
        raise UpstreamMergeError("U-2 SurfaceRecalculationReceipt 未闭合")
    source = _load_source_candidate(plan)
    worktree = _worktree_root(plan)
    if rev_parse(worktree, "HEAD^{commit}") != source["source_commit"]:
        raise UpstreamMergeError("impact matrix 的 HEAD 与 SourceCandidate 不一致")
    assert_clean(worktree, "U-3 impact matrix")
    entries: list[dict[str, Any]] = []
    for change in changed_paths(worktree, plan.fork_head, source["source_commit"]):
        relative = change["path"]
        hints = _diff_risk_hints(worktree, plan.fork_head, source["source_commit"], relative)
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
                "suggested_categories": _suggest_categories(relative, hints),
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
            "source_candidate": stage_binding(plan, "source_candidate"),
            "surface_receipt": stage_binding(plan, "surface_receipt"),
            "file_change_count": len(entries),
            "file_changes": entries,
            "surface_delta_count": len(delta["deltas"]),
            "surface_deltas": delta["deltas"],
            "classification_rule": (
                "路径与差异关键字只生成风险提示；每个文件和发送面 delta 必须由 ChangeDecision "
                "显式分类，不能按目录名自动缩小范围"
            ),
            "result": "pending_change_decision",
        },
    )
    write_json_once(plan.output_path("impact_matrix"), document)
    return document


def _load_impact_matrix(plan: LoadedPlan) -> dict[str, Any]:
    return artifact_document(
        plan.output_path("impact_matrix"),
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
    )


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
    decision = expect_object(load_json(decision_path, "ChangeDecision"), "ChangeDecision")
    expect_exact_fields(
        decision,
        {
            "schema_version",
            "plan_id",
            "plan_identity_sha256",
            "source_tree",
            "impact_matrix_sha256",
            "files",
            "surface_deltas",
            "identity_sha256",
        },
        "ChangeDecision",
    )
    if decision.get("schema_version") != CHANGE_DECISION_INPUT_SCHEMA:
        raise UpstreamMergeError("ChangeDecision schema_version 非法")
    if (
        decision.get("plan_id") != plan.plan_id
        or decision.get("plan_identity_sha256") != plan.identity
        or decision.get("source_tree") != source["source_tree"]
        or decision.get("impact_matrix_sha256") != sha256_file(plan.output_path("impact_matrix"))
    ):
        raise UpstreamMergeError("ChangeDecision 身份或 ImpactMatrix 绑定不一致")
    validate_identity(decision, "ChangeDecision")
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
        expect_exact_fields(
            item,
            {
                "path",
                "categories",
                "rationale",
                "required_actions",
                "official_client_identity_changed",
                "evidence_semantics_changed",
            },
            label,
        )
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
        hints = set(expected_files[relative]["risk_hints"])
        if hints & {"account", "billing", "group", "key", "quota_usage", "route"} and (
            "key_group_routing_billing" not in normalized_categories
        ):
            raise UpstreamMergeError(f"{label} 未承接 Key/Group/路由/计费风险提示")
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
        if _apply_client_impact(
            set(normalized_categories),
            semantics_changed,
            client_impacts,
            client_campaigns,
        ):
            shared_contract_required = True
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
        expect_exact_fields(item, {"delta_id", "rationale", "required_actions"}, label)
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
        for client in expected_deltas[delta_id]["clients"]:
            client_impacts[client] = True
    if seen_deltas != set(expected_deltas):
        raise UpstreamMergeError(
            f"ChangeDecision.surface_deltas 未闭合：missing={sorted(set(expected_deltas) - seen_deltas)}"
        )
    receipt = _stage_document(
        plan,
        CHANGE_DECISION_RECEIPT_SCHEMA,
        {
            "impact_matrix": stage_binding(plan, "impact_matrix"),
            "change_decision": file_binding(decision_path.resolve(strict=True)),
            "file_decision_count": len(seen_files),
            "surface_decision_count": len(seen_deltas),
            "client_impacts": client_impacts,
            "successor_campaign_required": client_campaigns,
            "shared_contract_required": shared_contract_required,
            "unclassified_count": 0,
            "official_client_identity_change_count": 0,
            "result": "closed",
        },
    )
    write_json_once(plan.output_path("impact_receipt"), receipt)
    return receipt


def _load_impact_receipt(plan: LoadedPlan) -> dict[str, Any]:
    return artifact_document(
        plan.output_path("impact_receipt"),
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
    )


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


def _run_verification_gates_in_worktree(
    plan: LoadedPlan,
    attempt_id: str,
    execution_worktree: Path,
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
    attempt_root_relative = f"{plan.output_relative('gate_attempts_root')}/{attempt}"
    attempt_root = resolve_within(plan.evidence_root, attempt_root_relative, "gate attempt")
    if attempt_root.exists():
        raise UpstreamMergeError(f"门禁 attempt 已存在，禁止覆盖：{attempt}")
    attempt_root.mkdir(parents=True, mode=0o700)
    attempt_root.chmod(0o700)
    results: list[dict[str, Any]] = []
    for gate in plan.document["gates"]:
        receipt_path: Path | None = None
        receipt_binding: dict[str, Any] | None = None
        if gate["mode"] == "receipt_replay":
            receipt_path = resolve_within(
                plan.evidence_root,
                gate["receipt"],
                f"gate {gate['id']} receipt",
            )
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise UpstreamMergeError(
                    f"receipt_replay 门禁缺少候选专属收据：{gate['id']}={receipt_path}"
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
            for item in gate["argv"]
        ]
        cwd = _gate_cwd(worktree, gate["cwd"])
        executable = executable_identity(argv[0], cwd)
        started = time.monotonic()
        completed = run_process(
            argv,
            cwd=cwd,
            check=False,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "UPSTREAM_MERGE_PLAN": str(plan.path),
            },
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_path = attempt_root / f"{gate['id']}.stdout.txt"
        stderr_path = attempt_root / f"{gate['id']}.stderr.txt"
        write_once(stdout_path, completed.stdout.encode("utf-8"))
        write_once(stderr_path, completed.stderr.encode("utf-8"))
        results.append(
            {
                "id": gate["id"],
                "category": gate["category"],
                "mode": gate["mode"],
                "cwd": gate["cwd"],
                "argv": gate["argv"],
                "expanded_argv_sha256": sha256_bytes(canonical_bytes(argv)),
                "executable": executable,
                "source_receipt": receipt_binding,
                "exit_code": completed.returncode,
                "duration_ms": duration_ms,
                "stdout": artifact_binding(plan.evidence_root, stdout_path),
                "stderr": artifact_binding(plan.evidence_root, stderr_path),
                "status": "passed" if completed.returncode == 0 else "failed",
            }
        )
    dirty_paths = status_paths(worktree)
    failed = [item["id"] for item in results if item["status"] != "passed"]
    result = "passed" if not failed and not dirty_paths else "blocked"
    document = _stage_document(
        plan,
        VERIFICATION_RECEIPT_SCHEMA,
        {
            "attempt_id": attempt,
            "source_candidate": stage_binding(plan, "source_candidate"),
            "impact_receipt": stage_binding(plan, "impact_receipt"),
            "required_categories": list(REQUIRED_GATE_CATEGORIES),
            "gate_count": len(results),
            "failed_gate_ids": failed,
            "skipped_gate_count": 0,
            "worktree_status_paths": dirty_paths,
            "gates": results,
            "result": result,
        },
    )
    receipt_path = attempt_root / "receipt.json"
    write_json_once(receipt_path, document)
    return document


def run_verification_gates(plan: LoadedPlan, attempt_id: str) -> dict[str, Any]:
    """在计划隔离树执行 U-4 全部门禁并保留原始输出。"""

    return _run_verification_gates_in_worktree(
        plan,
        attempt_id,
        plan.worktree,
    )


def load_verification_receipt(
    plan: LoadedPlan,
    path: Path,
    *,
    require_passed: bool,
) -> dict[str, Any]:
    document = expect_object(load_json(path, "VerificationReceipt"), "VerificationReceipt")
    expect_exact_fields(
        document,
        {
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
        },
        "VerificationReceipt",
    )
    if document.get("schema_version") != VERIFICATION_RECEIPT_SCHEMA:
        raise UpstreamMergeError("VerificationReceipt schema_version 非法")
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError("VerificationReceipt 计划身份不一致")
    validate_identity(document, "VerificationReceipt")
    if document.get("source_candidate") != stage_binding(plan, "source_candidate"):
        raise UpstreamMergeError("VerificationReceipt SourceCandidate 绑定漂移")
    if document.get("impact_receipt") != stage_binding(plan, "impact_receipt"):
        raise UpstreamMergeError("VerificationReceipt ChangeDecisionReceipt 绑定漂移")
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
    for planned in plan.document["gates"]:
        actual = by_id[planned["id"]]
        expect_exact_fields(
            actual,
            {
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
            },
            f"VerificationReceipt.gates.{planned['id']}",
        )
        if any(actual.get(field) != planned[field] for field in ("id", "category", "mode", "cwd", "argv")):
            raise UpstreamMergeError(f"VerificationReceipt 门禁定义漂移：{planned['id']}")
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
        executable = expect_object(
            actual.get("executable"),
            f"VerificationReceipt.gates.{planned['id']}.executable",
        )
        expect_exact_fields(
            executable,
            {"command", "resolved_path", "sha256", "bytes"},
            f"VerificationReceipt.gates.{planned['id']}.executable",
        )
        if executable.get("command") != expanded[0]:
            raise UpstreamMergeError(f"VerificationReceipt 可执行文件身份漂移：{planned['id']}")
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
    expected_result = "passed" if not failed and not normalized_dirty else "blocked"
    if document.get("result") != expected_result:
        raise UpstreamMergeError("VerificationReceipt result 与门禁／工作树结果矛盾")
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
