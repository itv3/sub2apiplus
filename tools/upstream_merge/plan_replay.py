"""前序 Plan 的机械重放（UM-9 第 1、2 项）。

v0.2.10 合并先在试验 worktree 解决 77 个冲突并完成源码适配，再由正式 Plan 机械重放；Plan 002～004
三次重放都靠临时脚本（replay_plan.py 合成候选树、gen_conflict_input.py 复用冲突决策）。这里收编为：

1. ``plan-replay``：把试验区定型树与 ``trial-base..fork_head`` 的主干新增提交合成候选，写入本 Plan
   合并 worktree 的 index 与工作文件，之后照常 ``merge-seal``。只在两边互不干扰时机械合成：
   试验区基点与上游的 merge-base 必须与本 Plan 相同；上游或试验区改动了主干新增路径都拒绝。
2. ``merge-seal --replay-from <前序 Plan evidence root>``：三方 stage 对象与解决结果都与前序冲突台账
   一致的条目复用前序理由，处置类型按本 Plan index 判定，理由末尾标注来源 Plan；其余条目交人工，
   由 ``--conflict-decisions`` 只补这些路径。合成后的 ConflictResolutionInput 写到 ``inputs/`` 并由
   ``merge-seal`` 原样校验封存，冲突台账格式不变。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .canonical import (
    bind_identity,
    expect_object,
    load_json,
    resolve_within,
    safe_relative_path,
    sha256_file,
    validate_identity,
)
from .contracts import LoadedPlan, artifact_document
from .errors import UpstreamMergeError
from .gitops import merge_base, rev_parse, run_git, unmerged_entries
from .plan_inputs import AWAITING_MANUAL_INPUT, plan_inputs_root, write_json_input
from .workflow import (
    CONFLICT_INPUT_SCHEMA,
    CONFLICT_LEDGER_SCHEMA,
    MERGE_CANDIDATE_SCHEMA,
    MERGE_START_SCHEMA,
    _index_path_state,
    _infer_conflict_resolution,
    _load_conflict_input,
    _load_merge_start,
    _validated_linked_worktree,
    _worktree_root,
    seal_merge,
)

REPLAY_NOTE_RE = re.compile(r"〔重放自 [^〕]*〕$")
REPLAYED_INPUT_NAME = "conflict-decisions-replayed.json"
REMAINING_DRAFT_NAME = "conflict-decisions-remaining-draft.json"
MERGE_START_FIELDS = {
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
}
CONFLICT_LEDGER_FIELDS = {
    "plan_id",
    "plan_identity_sha256",
    "merge_start",
    "conflict_count",
    "conflict_paths",
    "resolution_input",
    "resolutions",
    "result",
}
MERGE_CANDIDATE_FIELDS = {
    "plan_id",
    "plan_identity_sha256",
    "merge_start",
    "conflict_ledger",
    "parents",
    "merge_commit",
    "candidate_tree",
    "changed_paths",
    "protected_objects_unchanged",
}


def _prior_plan(prior_root: Path) -> tuple[Path, dict[str, Any]]:
    """读取前序 Plan 的 plan.json（只做身份与输出位置核对，不按当前工具闭集重算）。"""

    if not prior_root.is_absolute():
        raise UpstreamMergeError(f"前序 Plan evidence root 必须是绝对路径：{prior_root}")
    if prior_root.is_symlink() or not prior_root.is_dir():
        raise UpstreamMergeError(f"前序 Plan evidence root 不可信：{prior_root}")
    root = prior_root.resolve(strict=True)
    plan = expect_object(load_json(root / "plan.json", "前序 Plan"), "前序 Plan")
    validate_identity(plan, "前序 Plan")
    outputs = expect_object(plan.get("outputs"), "前序 Plan.outputs")
    for key in ("merge_start", "conflict_ledger", "merge_candidate"):
        if not isinstance(outputs.get(key), str):
            raise UpstreamMergeError(f"前序 Plan.outputs.{key} 缺失")
    return root, plan


def _prior_output(root: Path, plan: dict[str, Any], key: str) -> Path:
    relative = safe_relative_path(plan["outputs"][key], f"前序 Plan.outputs.{key}")
    return resolve_within(root, relative, f"前序 Plan.outputs.{key}")


def _prior_document(root: Path, plan: dict[str, Any], key: str, label: str, schema: str, fields: set[str]) -> dict[str, Any]:
    document = artifact_document(_prior_output(root, plan, key), label, schema, fields)
    if (
        document.get("plan_id") != plan.get("plan_id")
        or document.get("plan_identity_sha256") != plan.get("identity_sha256")
    ):
        raise UpstreamMergeError(f"{label} 不属于前序 Plan {plan.get('plan_id')}")
    return document


def _stages_by_path(stages: list[dict[str, Any]]) -> dict[str, dict[int, tuple[str, str]]]:
    result: dict[str, dict[int, tuple[str, str]]] = {}
    for item in stages:
        result.setdefault(str(item["path"]), {})[int(item["stage"])] = (
            str(item["mode"]),
            str(item["object_id"]),
        )
    return result


def _replay_rationale(rationale: str, source_plan_id: str) -> str:
    """前序理由末尾的旧重放标注换成本次来源，避免多次重放后标注层层叠加。"""

    base = REPLAY_NOTE_RE.sub("", rationale).rstrip()
    return f"{base}〔重放自 {source_plan_id}：三方 stage 对象与解决结果均与其冲突台账一致，处置类型按本 Plan index 判定〕"


def conflict_replay(plan: LoadedPlan, prior_root: Path) -> dict[str, Any]:
    """逐条比对本 Plan 冲突与前序台账，返回可重放条目与交人工条目（只读，不写任何文件）。"""

    start = _load_merge_start(plan)
    worktree = _worktree_root(plan)
    root, prior_plan = _prior_plan(prior_root)
    if prior_plan.get("plan_id") == plan.plan_id:
        raise UpstreamMergeError("--replay-from 不能指向本 Plan 自己")
    prior_start = _prior_document(root, prior_plan, "merge_start", "前序 MergeStart", MERGE_START_SCHEMA, MERGE_START_FIELDS)
    prior_ledger = _prior_document(
        root, prior_plan, "conflict_ledger", "前序 ConflictResolutionLedger", CONFLICT_LEDGER_SCHEMA, CONFLICT_LEDGER_FIELDS
    )
    if prior_ledger["result"] != "closed":
        raise UpstreamMergeError("前序 ConflictResolutionLedger 未闭合，不能作为重放来源")
    start_binding = expect_object(prior_ledger["merge_start"], "前序 ConflictResolutionLedger.merge_start")
    if start_binding.get("sha256") != sha256_file(_prior_output(root, prior_plan, "merge_start")):
        raise UpstreamMergeError("前序 ConflictResolutionLedger 未绑定前序 MergeStart")
    source_plan_id = str(prior_plan["plan_id"])
    current_stages = _stages_by_path(start["conflict_stages"])
    prior_stages = _stages_by_path(prior_start["conflict_stages"])
    prior_resolutions = {str(item["path"]): item for item in prior_ledger["resolutions"]}
    unresolved = {item["path"] for item in unmerged_entries(worktree)}
    replayed: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for path in start["conflict_paths"]:
        prior = prior_resolutions.get(path)
        if path in unresolved:
            reason = "冲突尚未解决（index 仍有多阶段条目）"
        elif prior is None:
            reason = f"前序 Plan {source_plan_id} 的冲突台账没有这个路径"
        elif current_stages.get(path) != prior_stages.get(path):
            now, before = current_stages.get(path, {}), prior_stages.get(path, {})
            differing = sorted(stage for stage in {1, 2, 3} if now.get(stage) != before.get(stage))
            reason = "三方 stage 对象与前序不同：stage " + "、".join(str(stage) for stage in differing)
        elif _index_path_state(worktree, path) != prior["resolved_state"]:
            reason = "解决结果与前序台账记录的对象不同"
        else:
            resolution = _infer_conflict_resolution(worktree, start["conflict_stages"], path)
            if resolution != prior["resolution"]:
                reason = f"按本 Plan index 判定为 {resolution}，与前序 {prior['resolution']} 不同"
            else:
                replayed.append(
                    {
                        "path": path,
                        "resolution": resolution,
                        "rationale": _replay_rationale(str(prior["rationale"]), source_plan_id),
                    }
                )
                continue
        remaining.append({"path": path, "reason": reason})
    return {"source_plan_id": source_plan_id, "replayed": replayed, "remaining": remaining}


def seal_merge_with_replay(plan: LoadedPlan, prior_root: Path, conflict_input: Path | None) -> dict[str, Any]:
    """重放前序冲突决策后封存 U-1；有交人工条目且未提供其决策时停下并生成草稿。"""

    start = _load_merge_start(plan)
    if not start["conflict_paths"]:
        raise UpstreamMergeError("本 Plan 合并无冲突，不需要 --replay-from")
    replay = conflict_replay(plan, prior_root)
    remaining = [item["path"] for item in replay["remaining"]]
    inputs_root = plan_inputs_root(plan)
    manual: dict[str, dict[str, Any]] = {}
    if remaining:
        if conflict_input is None:
            draft_path: Path | None = None
            if not any(item["reason"].startswith("冲突尚未解决") for item in replay["remaining"]):
                worktree = _worktree_root(plan)
                draft_path = inputs_root / REMAINING_DRAFT_NAME
                if not draft_path.exists():
                    # 草稿只含交人工的路径；机械字段与处置类型由工具填写，理由留给人工。
                    write_json_input(
                        draft_path,
                        {
                            "schema_version": CONFLICT_INPUT_SCHEMA,
                            "plan_id": plan.plan_id,
                            "plan_identity_sha256": plan.identity,
                            "merge_start_sha256": sha256_file(plan.output_path("merge_start")),
                            "resolutions": [
                                {
                                    "path": path,
                                    "resolution": _infer_conflict_resolution(worktree, start["conflict_stages"], path),
                                    "rationale": "",
                                }
                                for path in remaining
                            ],
                        },
                        "交人工冲突决策草稿",
                    )
            return {
                "result": AWAITING_MANUAL_INPUT,
                "stage": "U-1 merge-seal",
                "replay_source_plan_id": replay["source_plan_id"],
                "replayed_count": len(replay["replayed"]),
                "remaining": replay["remaining"],
                "draft": str(draft_path) if draft_path else None,
                "next": (
                    "先解决仍有多阶段条目的冲突再重跑；其余条目在草稿中逐条写明取舍理由，"
                    "identity-seal 签名后以 --replay-from 加 --conflict-decisions 重跑 merge-seal"
                ),
            }
        manual = _load_conflict_input(conflict_input, plan, sorted(remaining))
    elif conflict_input is not None:
        raise UpstreamMergeError("全部冲突都可重放，不得再附带 --conflict-decisions")
    replayed = {item["path"]: item for item in replay["replayed"]}
    combined = bind_identity(
        {
            "schema_version": CONFLICT_INPUT_SCHEMA,
            "plan_id": plan.plan_id,
            "plan_identity_sha256": plan.identity,
            "merge_start_sha256": sha256_file(plan.output_path("merge_start")),
            "resolutions": [replayed.get(path) or manual[path] for path in start["conflict_paths"]],
        }
    )
    combined_path = inputs_root / REPLAYED_INPUT_NAME
    write_json_input(combined_path, combined, "重放合成的 ConflictResolutionInput")
    candidate = seal_merge(plan, combined_path)
    return {
        **candidate,
        "conflict_replay": {
            "source_plan_id": replay["source_plan_id"],
            "replayed_count": len(replayed),
            "manual_count": len(manual),
            "resolution_input": str(combined_path),
        },
    }


def _tree_entries(worktree: Path, tree: str, paths: list[str]) -> dict[str, tuple[str, str] | None]:
    """路径在树中的 (mode, object)；不存在记为 None。"""

    result: dict[str, tuple[str, str] | None] = {path: None for path in paths}
    for start in range(0, len(paths), 200):
        chunk = paths[start : start + 200]
        raw = subprocess.check_output(["git", "ls-tree", "-z", tree, "--", *chunk], cwd=worktree)
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, _kind, object_id = metadata.decode("ascii").split(" ")
            result[raw_path.decode("utf-8")] = (mode, object_id)
    return result


def _index_entries(worktree: Path, *, stage_zero_only: bool) -> dict[str, tuple[str, str]]:
    raw = subprocess.check_output(["git", "ls-files", "-s", "-z"], cwd=worktree)
    result: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split(" ")
        if stage_zero_only and stage != "0":
            continue
        result[raw_path.decode("utf-8")] = (mode, object_id)
    return result


def _name_diff(worktree: Path, left: str, right: str) -> list[str]:
    raw = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", left, right], cwd=worktree
    )
    return sorted(item.decode("utf-8") for item in raw.split(b"\0") if item)


def replay_trial_tree(
    plan: LoadedPlan,
    trial_tree: str,
    trial_base: str,
    previous_plan: Path | None = None,
) -> dict[str, Any]:
    """把试验区定型树与主干新增提交合成本 Plan 的合并候选（只改合并 worktree，不写 evidence）。"""

    start = _load_merge_start(plan)
    if plan.output_path("merge_candidate").exists():
        raise UpstreamMergeError("U-1 已封存，不能再重放试验区树")
    # 合并进行中上游可能改了闭集文件（如 Makefile），闭集要等定型树覆盖后才恢复，合成完成后再核对。
    worktree = _validated_linked_worktree(plan, plan.worktree, check_tool_bundle=False)
    fork_head = plan.fork_head
    upstream = plan.upstream_commit
    planned_base = str(plan.document["repository"]["merge_base"])
    if rev_parse(worktree, "HEAD^{commit}") != fork_head:
        raise UpstreamMergeError("合并 worktree 的 HEAD 不是本 Plan fork HEAD")
    if rev_parse(worktree, "MERGE_HEAD^{commit}") != upstream:
        raise UpstreamMergeError("合并 worktree 不在 merge-start 状态（MERGE_HEAD 不是上游提交）")
    tree = rev_parse(worktree, f"{trial_tree}^{{tree}}")
    base = rev_parse(worktree, f"{trial_base}^{{commit}}")
    if run_git(worktree, "merge-base", "--is-ancestor", base, fork_head, check=False).returncode != 0:
        raise UpstreamMergeError("试验区基点不是本 Plan fork HEAD 的祖先")
    if merge_base(worktree, base, upstream) != planned_base:
        raise UpstreamMergeError(
            "试验区基点与上游的 merge-base 不同于本 Plan，试验区合并结果不能机械复用"
        )
    delta = _name_diff(worktree, base, fork_head)
    upstream_touched = _name_diff(worktree, planned_base, upstream)
    touched = sorted(set(delta) & set(upstream_touched))
    if touched:
        raise UpstreamMergeError(f"上游也改动了主干新增路径，需在本 Plan 人工处置：{touched}")
    trial_state = _tree_entries(worktree, tree, delta)
    base_state = _tree_entries(worktree, base, delta)
    trial_changed = sorted(path for path in delta if trial_state[path] != base_state[path])
    if trial_changed:
        raise UpstreamMergeError(
            f"试验区改动了主干新增路径，机械重放会丢掉试验区的改动，需人工合并：{trial_changed}"
        )
    auto_merged = _index_entries(worktree, stage_zero_only=True)
    run_git(worktree, "read-tree", "--reset", "-u", tree)
    fork_state = _tree_entries(worktree, fork_head, delta)
    for path in delta:
        if fork_state[path] is not None:
            run_git(worktree, "checkout", fork_head, "--", path)
        else:
            run_git(worktree, "rm", "-q", "--cached", "--ignore-unmatch", "--", path)
            (worktree / path).unlink(missing_ok=True)
    result_tree = run_git(worktree, "write-tree").stdout.strip()
    unexpected = sorted(set(_name_diff(worktree, tree, result_tree)) - set(delta))
    if unexpected:
        raise UpstreamMergeError(f"合成候选出现主干新增之外的差异：{unexpected}")
    if _tree_entries(worktree, result_tree, delta) != fork_state:
        raise UpstreamMergeError("主干新增路径的对象与 fork HEAD 不一致")
    if unmerged_entries(worktree):
        raise UpstreamMergeError("合成后仍有未解决冲突")
    if run_git(worktree, "diff", "--quiet", check=False).returncode != 0:
        raise UpstreamMergeError("合成后存在未暂存修改")
    others = run_git(worktree, "ls-files", "--others", "--exclude-standard").stdout.split()
    if others:
        raise UpstreamMergeError(f"合成后存在未登记文件：{others[:10]}")
    try:
        _worktree_root(plan)
    except UpstreamMergeError as error:
        raise UpstreamMergeError(
            f"合成后的工具闭集与计划冻结不一致，按预检决定恢复受保护版本后再 merge-seal：{error}"
        ) from error
    replayed_index = _index_entries(worktree, stage_zero_only=True)
    conflicts = set(start["conflict_paths"])
    adjustments = sorted(
        path
        for path in set(auto_merged) | set(replayed_index)
        if path not in conflicts and auto_merged.get(path) != replayed_index.get(path)
    )
    previous: dict[str, Any] | None = None
    if previous_plan is not None:
        root, prior_plan = _prior_plan(previous_plan)
        prior_candidate = _prior_document(
            root, prior_plan, "merge_candidate", "前序 MergeCandidateTree", MERGE_CANDIDATE_SCHEMA, MERGE_CANDIDATE_FIELDS
        )
        differing = _name_diff(worktree, str(prior_candidate["candidate_tree"]), result_tree)
        previous = {
            "plan_id": prior_plan["plan_id"],
            "candidate_tree": prior_candidate["candidate_tree"],
            "diff_paths": differing,
            "all_within_mainline_delta": set(differing) <= set(delta),
        }
    return {
        "result": "replayed",
        "trial_tree": tree,
        "trial_base": base,
        "mainline_delta_paths": delta,
        "candidate_tree": result_tree,
        "conflict_count": len(conflicts),
        "non_conflict_adjustments": adjustments,
        "previous_plan": previous,
        "next": "核对差异清单后执行 merge-seal（可加 --replay-from 复用前序冲突决策）",
    }
