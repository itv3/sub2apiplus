"""U-1 冲突决策由理由文件生成（UM-22）。

v0.2.13 合并时 10 条冲突决策靠临时脚本拼出 ConflictResolutionInput（计划身份、MergeStart 绑定、处置类型），
再单独 identity-seal。现在人只写“路径 → 取舍理由”：

* ``merge-seal`` 遇到冲突却没给决策时，在 ``inputs/`` 写出理由草稿（每个冲突路径一项，理由留空），并列出
  工具按 index 事实推断的处置类型，以“待人工输入”（退出码 4）停下；
* ``merge-seal --conflict-rationale <理由文件>`` 按 index 事实推断每条的处置类型（整文件取我方为 fork、取上游
  为 upstream，其余为 manual，与封存时的复核同一判定），组装并签名 ConflictResolutionInput 写入 ``inputs/``，
  再照常封存。带 ``--replay-from`` 时理由文件只需覆盖不能重放、交人工的那些路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import bind_identity, expect_object, load_json, sha256_file
from .contracts import LoadedPlan
from .errors import UpstreamMergeError
from .gitops import unmerged_entries
from .plan_inputs import AWAITING_MANUAL_INPUT, plan_inputs_root, write_json_input
from .workflow import (
    CONFLICT_INPUT_SCHEMA,
    _infer_conflict_resolution,
    _load_merge_start,
    _worktree_root,
)

CONFLICT_RATIONALE_DRAFT_NAME = "conflict-rationale-draft.json"
CONFLICT_DECISIONS_NAME = "conflict-decisions.json"
CONFLICT_DECISIONS_REMAINING_NAME = "conflict-decisions-remaining.json"


def _inferred(plan: LoadedPlan, start: dict[str, Any], paths: list[str]) -> dict[str, str]:
    worktree = _worktree_root(plan)
    unresolved = sorted({item["path"] for item in unmerged_entries(worktree)} & set(paths))
    if unresolved:
        raise UpstreamMergeError("仍有未解决冲突，无法推断处置类型：" + ", ".join(unresolved))
    return {path: _infer_conflict_resolution(worktree, start["conflict_stages"], path) for path in paths}


def draft_conflict_rationale(plan: LoadedPlan) -> dict[str, Any] | None:
    """有冲突且没有决策时写理由草稿并返回“待人工输入”；无冲突返回 None，由调用方照常封存。"""

    start = _load_merge_start(plan)
    paths = list(start["conflict_paths"])
    if not paths:
        return None
    draft_path = plan_inputs_root(plan) / CONFLICT_RATIONALE_DRAFT_NAME
    write_json_input(draft_path, {path: "" for path in paths}, "冲突理由草稿")
    worktree = _worktree_root(plan)
    unresolved = sorted({item["path"] for item in unmerged_entries(worktree)} & set(paths))
    inferred = {
        path: _infer_conflict_resolution(worktree, start["conflict_stages"], path)
        for path in paths
        if path not in unresolved
    }
    return {
        "result": AWAITING_MANUAL_INPUT,
        "stage": "U-1 merge-seal",
        "draft": str(draft_path),
        "conflict_paths": paths,
        "unresolved_paths": unresolved,
        "inferred_resolutions": inferred,
        "next": (
            ("先解决仍有多阶段条目的冲突并 git add；" if unresolved else "")
            + "在草稿中逐条写明取舍理由（处置类型由工具按 index 事实推断），"
            + f"再执行 merge-seal --conflict-rationale {draft_path}"
        ),
    }


def conflict_input_from_rationale(plan: LoadedPlan, rationale_path: Path, paths: list[str], *, remaining: bool) -> Path:
    """按理由文件与 index 事实组装并签名 ConflictResolutionInput，写入 inputs/ 并返回路径。"""

    rationale = expect_object(load_json(rationale_path, "冲突理由文件"), "冲突理由文件")
    expected = sorted(paths)
    if sorted(rationale) != expected:
        raise UpstreamMergeError(
            f"冲突理由文件必须逐个覆盖{'交人工的' if remaining else '全部'}冲突路径："
            f"缺少={sorted(set(expected) - set(rationale))} 多余={sorted(set(rationale) - set(expected))}"
        )
    empty = sorted(path for path, text in rationale.items() if not isinstance(text, str) or len(text.strip()) < 12)
    if empty:
        raise UpstreamMergeError("冲突理由须逐条说明具体取舍（至少 12 个字符）：" + ", ".join(empty))
    start = _load_merge_start(plan)
    inferred = _inferred(plan, start, expected)
    document = bind_identity(
        {
            "schema_version": CONFLICT_INPUT_SCHEMA,
            "plan_id": plan.plan_id,
            "plan_identity_sha256": plan.identity,
            "merge_start_sha256": sha256_file(plan.output_path("merge_start")),
            "resolutions": [
                {"path": path, "resolution": inferred[path], "rationale": rationale[path].strip()}
                for path in expected
            ],
        }
    )
    output = plan_inputs_root(plan) / (CONFLICT_DECISIONS_REMAINING_NAME if remaining else CONFLICT_DECISIONS_NAME)
    write_json_input(output, document, "冲突决策")
    return output
