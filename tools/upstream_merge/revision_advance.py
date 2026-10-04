"""源码 revision 一条命令推进（UM-9 第 3 项）。

v0.2.10 合并的 Plan 004 走了 4 个 revision，每轮要依次调用约 12 条子命令，靠 u23_round.sh 串起；
仍遇到两次输入类拒绝：r2 ``surface-seal`` 报“Inventory 未绑定当前 SourceCandidate revision”（工具只对
零差异的 Codex 出站提供 carry-forward），r4 填 ChangeDecision 时台账 revision 新生成的收据“理由未归类”。

``revision-advance`` 按序执行 source-seal → surface-scan → Inventory 绑定 → surface-seal →
impact-generate → impact-suggest → impact-seal → revision-preflight，机械字段全部由工具填写：

- SourceChangeInput 可只给 ``entries``（路径与理由），其余字段由工具补齐并签名；
- 发送面差异与上一轮完全相同（delta_id 集合一致）时，四份 Inventory 按本轮 revision 原样续用，
  SurfaceDecision 沿用上一轮逐条处置并绑定本轮 SurfaceDelta；
- ChangeDecision 按框架 §5.2.3“同 diff 的文件复用已封存的决定”：本轮 diff_sha256 与上一轮相同的文件、
  delta_id 相同的发送面差异复用上一轮签名决定，工具可安全自动分类的照常自动。

遇到需要人工理由的条目（本轮新增或 diff 变化的文件，含工具新生成的收据）就停下并列出，写好草稿；
补完后 ``--resume`` 续跑。每一步都先看本轮制品是否已存在：已完成的不重做，不重复编号，不覆盖旧制品。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import (
    artifact_binding,
    bind_identity,
    expect_exact_fields,
    expect_object,
    expect_string,
    load_json,
    resolve_within,
    sha256_file,
    validate_file_binding,
    validate_identity,
    write_once,
)
from .contracts import (
    CLIENT_KEYS,
    LoadedPlan,
    _validate_inventory_payload,
    inventory_revision_number,
    latest_revision,
    next_inventory_path,
    stage_paths,
)
from .errors import UpstreamMergeError
from .gitops import status_paths
from .plan_inputs import AWAITING_MANUAL_INPUT, plan_inputs_root, write_json_input
from .workflow import (
    CHANGE_DECISION_INPUT_SCHEMA,
    CHANGE_DECISION_RECEIPT_SCHEMA,
    IMPACT_MATRIX_SCHEMA,
    SOURCE_CHANGE_INPUT_SCHEMA,
    SURFACE_DECISION_SCHEMA,
    SURFACE_DELTA_SCHEMA,
    SURFACE_RECEIPT_SCHEMA,
    _load_merge_candidate,
    _load_source_candidate,
    _worktree_root,
    generate_change_decision_suggestion,
    generate_impact_matrix,
    preflight_revisions,
    scan_surfaces,
    seal_change_decision,
    seal_source_candidate,
    seal_surfaces,
)

INVENTORY_KINDS = ("ingress", "egress")
SOURCE_CHANGE_DRAFT_FIELDS = {
    "entries",
    "schema_version",
    "plan_id",
    "plan_identity_sha256",
    "merge_commit",
    "base_source_commit",
    "revision",
}
# 复用上一轮决定时照搬的字段；component_ownership 另行核对与本轮矩阵一致。
DECISION_FILE_FIELDS = (
    "categories",
    "rationale",
    "required_actions",
    "official_client_identity_changed",
    "evidence_semantics_changed",
    "decision_source",
    "component_ownership",
    "auto_reason",
)


def _input_name(kind: str, revision: int, suffix: str = "") -> str:
    return f"{kind}-revision-{revision:03d}{suffix}.json"


def _revision_document(plan: LoadedPlan, key: str, revision: int, schema: str, label: str) -> tuple[Path, dict[str, Any]]:
    """读取某一轮已写入的阶段制品（该轮封存时已严格校验，这里核对身份与摘要）。"""

    paths = stage_paths(plan, key)
    if revision < 1 or revision > len(paths):
        raise UpstreamMergeError(f"缺少第 {revision:03d} 轮 {label}")
    path = paths[revision - 1]
    document = expect_object(load_json(path, label), label)
    if document.get("schema_version") != schema:
        raise UpstreamMergeError(f"{label} schema_version 非法：{path}")
    validate_identity(document, label)
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError(f"{label} 不属于本 Plan：{path}")
    return path, document


def _bound_json(binding: Any, label: str) -> tuple[Path, dict[str, Any]]:
    """读取收据以绝对路径绑定的输入文件，并核对摘要未变。"""

    validated = validate_file_binding(binding, label)
    path = Path(validated["path"])
    if sha256_file(path) != validated["sha256"]:
        raise UpstreamMergeError(f"{label} 绑定的文件已被改动：{path}")
    return path, expect_object(load_json(path, label), label)


def _prepare_source_change_input(plan: LoadedPlan, supplied: Path, revision: int, inputs_root: Path) -> tuple[Path, str]:
    """已签名的 SourceChangeInput 原样交给 source-seal；只含 entries 的草稿由工具补齐机械字段并签名。"""

    document = expect_object(load_json(supplied, "SourceChangeInput"), "SourceChangeInput")
    if "identity_sha256" in document:
        return supplied, "signed_input"
    extra = sorted(set(document) - SOURCE_CHANGE_DRAFT_FIELDS)
    if extra:
        raise UpstreamMergeError(f"SourceChangeInput 草稿含未知字段：{extra}")
    computed = {
        "schema_version": SOURCE_CHANGE_INPUT_SCHEMA,
        "plan_id": plan.plan_id,
        "plan_identity_sha256": plan.identity,
        "merge_commit": _load_merge_candidate(plan)["merge_commit"],
        "base_source_commit": _load_source_candidate(plan)["source_commit"],
        "revision": revision,
    }
    for field, value in computed.items():
        if field in document and document[field] != value:
            raise UpstreamMergeError(f"SourceChangeInput 草稿的 {field} 与本 Plan 当前轮次不一致：应为 {value}")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise UpstreamMergeError("SourceChangeInput 草稿的 entries 必须是非空数组")
    ordered = sorted(
        (expect_object(item, "SourceChangeInput.entries[]") for item in entries),
        key=lambda item: str(item.get("path", "")),
    )
    # 签名前先按 source-seal 的规则核对，避免写出一份注定被拒、又不能覆盖的输入。
    for item in ordered:
        expect_exact_fields(item, {"path", "reason"}, "SourceChangeInput.entries[]")
        if len(expect_string(item.get("reason"), "SourceChangeInput.entries[].reason")) < 12:
            raise UpstreamMergeError(f"SourceChangeInput 草稿 {item.get('path')} 的 reason 必须说明为何属于本 changeset")
    declared = [str(item["path"]) for item in ordered]
    changed = status_paths(_worktree_root(plan))
    if declared != sorted(set(declared)) or declared != changed:
        raise UpstreamMergeError(
            f"SourceChangeInput 草稿未闭合 worktree 当前变化：worktree={changed} 草稿={declared}"
        )
    signed = bind_identity({**computed, "entries": ordered})
    target = inputs_root / _input_name("source-change", revision)
    write_json_input(target, signed, "SourceChangeInput")
    return target, "tool_filled"


def _revision_complete(plan: LoadedPlan, revision: int) -> bool:
    return latest_revision(plan, "impact_receipt") >= revision


def _awaiting(stage: str, revision: int, pending: list[dict[str, Any]], steps: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "result": AWAITING_MANUAL_INPUT,
        "stage": stage,
        "revision": revision,
        "completed_steps": steps,
        "pending": pending,
        **extra,
    }


def _inventory_state(plan: LoadedPlan, revision: int) -> dict[tuple[str, str], Path | None]:
    """本轮四份 Inventory 是否已写入（None 表示未写）。"""

    state: dict[tuple[str, str], Path | None] = {}
    for client in CLIENT_KEYS:
        for kind in INVENTORY_KINDS:
            latest = plan.inventory_output(client, kind)
            state[(client, kind)] = (
                latest
                if latest.exists() and inventory_revision_number(plan, client, kind, latest) == revision
                else None
            )
    return state


def _previous_inventories(plan: LoadedPlan, previous_receipt: dict[str, Any]) -> dict[tuple[str, str], Path]:
    """上一轮 SurfaceReceipt 绑定的四份 Inventory，核对摘要未变。"""

    bindings = expect_object(previous_receipt.get("candidate_inventories"), "上一轮 SurfaceReceipt.candidate_inventories")
    result: dict[tuple[str, str], Path] = {}
    for client in CLIENT_KEYS:
        pair = expect_object(bindings.get(client), f"上一轮 SurfaceReceipt.candidate_inventories.{client}")
        for kind in INVENTORY_KINDS:
            binding = expect_object(pair.get(kind), f"上一轮 SurfaceReceipt.candidate_inventories.{client}.{kind}")
            source = resolve_within(plan.evidence_root, str(binding["path"]), "上一轮 Inventory")
            if sha256_file(source) != binding.get("sha256"):
                raise UpstreamMergeError(f"上一轮 {client}/{kind} Inventory 已被改动：{source}")
            result[(client, kind)] = source
    return result


def _carry_inventories(
    plan: LoadedPlan,
    previous: dict[tuple[str, str], Path],
    revision: int,
    skip: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """发送面差异与上一轮相同：Inventory 按本轮 revision 原样续用上一轮封存的版本（已续用的跳过）。"""

    carried: list[dict[str, Any]] = []
    for client in CLIENT_KEYS:
        for kind in INVENTORY_KINDS:
            if (client, kind) in skip:
                continue
            source = previous[(client, kind)]
            target = next_inventory_path(plan, client, kind, revision=revision)
            write_once(target, source.read_bytes())
            _validate_inventory_payload(
                target,
                kind,
                plan.document["official_clients"][client]["persona"],
                f"candidate {client}/{kind} Inventory",
            )
            carried.append({"client": client, "kind": kind, **artifact_binding(plan.evidence_root, target)})
    return carried


def _surface_delta_ids(document: dict[str, Any]) -> list[str]:
    return sorted(str(item["delta_id"]) for item in document["deltas"])


def _fill_change_decision(
    suggestion: dict[str, Any],
    matrix: dict[str, Any],
    previous_matrix: dict[str, Any],
    previous_decision: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    """同 diff 的文件与同一发送面差异复用上一轮签名决定；返回草稿、待人工清单与计数。"""

    current_entries = {item["path"]: item for item in matrix["file_changes"]}
    previous_entries = {item["path"]: item for item in previous_matrix["file_changes"]}
    previous_files = {item["path"]: item for item in previous_decision["files"]}
    previous_deltas = {item["delta_id"]: item for item in previous_decision["surface_deltas"]}
    files: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    counts = {"auto": 0, "mechanical": 0, "reused": 0, "pending": 0}
    for item in suggestion["files"]:
        path = item["path"]
        current = current_entries[path]
        before = previous_entries.get(path)
        decided = previous_files.get(path)
        same_diff = before is not None and all(
            before.get(field) == current.get(field) for field in ("diff_sha256", "old_path", "status")
        )
        reusable = same_diff and decided is not None and decided.get("decision_source", "manual") == "manual"
        same_ownership = reusable and (
            decided.get("component_ownership", current["component_ownership"]) == current["component_ownership"]
        )
        if reusable and same_ownership:
            # 人工决定优先于自动建议：上一轮人工改判过的条目不因本轮可自动分类而丢失理由。
            files.append({"path": path, **{field: decided[field] for field in DECISION_FILE_FIELDS if field in decided}})
            counts["reused"] += 1
            continue
        if item["decision_source"] in {"auto", "mechanical"}:
            # 自动分类与机械决定（UM-22）都由本轮建议按当前事实重新生成，不沿用上一轮。
            files.append(item)
            counts[item["decision_source"]] += 1
            continue
        if reusable:
            reason = "组件映射与上一轮不同"
        elif before is None:
            reason = "本轮新增的变化文件（含工具新生成的收据）"
        elif not same_diff:
            reason = "diff 与上一轮不同"
        else:
            reason = "上一轮没有可复用的人工决定"
        files.append(item)
        pending.append({"kind": "file", "path": path, "reason": reason})
        counts["pending"] += 1
    deltas: list[dict[str, Any]] = []
    for item in suggestion["surface_deltas"]:
        decided = previous_deltas.get(item["delta_id"])
        if decided is not None and decided.get("decision_source", "manual") == "manual":
            deltas.append(
                {
                    "delta_id": item["delta_id"],
                    "rationale": decided["rationale"],
                    "required_actions": decided["required_actions"],
                    "decision_source": "manual",
                }
            )
            counts["reused"] += 1
            continue
        deltas.append(item)
        pending.append({"kind": "surface_delta", "delta_id": item["delta_id"], "reason": "上一轮没有这个发送面差异的决定"})
        counts["pending"] += 1
    draft = {key: value for key, value in suggestion.items() if key != "identity_sha256"}
    draft.update({"files": files, "surface_deltas": deltas})
    return _recount(draft), pending, counts


def _recount(draft: dict[str, Any]) -> dict[str, Any]:
    """按逐条状态重算草稿的计数字段，人工只需改条目本身。"""

    files = draft["files"]
    deltas = draft["surface_deltas"]
    unresolved = sorted(item["path"] for item in files if item.get("decision_source", "manual") == "manual_required")
    manual_required = len(unresolved) + sum(
        1 for item in deltas if item.get("decision_source", "manual") == "manual_required"
    )
    result = dict(draft)
    result.update(
        {
            "auto_accepted_count": sum(1 for item in files if item.get("decision_source", "manual") == "auto"),
            "manual_required_count": manual_required,
            "unresolved_paths": unresolved,
            "result": "ready_for_review" if manual_required else "ready_to_seal",
        }
    )
    return result


def _pending_in_draft(draft: dict[str, Any]) -> list[dict[str, Any]]:
    pending = [
        {"kind": "file", "path": item["path"], "reason": "草稿中仍为 manual_required"}
        for item in draft.get("files", [])
        if item.get("decision_source", "manual") == "manual_required"
    ]
    pending.extend(
        {"kind": "surface_delta", "delta_id": item["delta_id"], "reason": "草稿中仍为 manual_required"}
        for item in draft.get("surface_deltas", [])
        if item.get("decision_source", "manual") == "manual_required"
    )
    return pending


def advance_revision(plan: LoadedPlan, source_changes: Path | None, *, resume: bool) -> dict[str, Any]:
    """推进一个源码 revision 并封存到 U-3；停在人工输入时返回 awaiting_manual_input。"""

    inputs_root = plan_inputs_root(plan)
    latest = latest_revision(plan, "source_candidate")
    if latest < 1:
        raise UpstreamMergeError("revision-advance 只推进已封存的 revision；首轮 r1 按 §5.2.3 逐步执行")
    steps: list[str] = []
    source_input_mode: str | None = None
    if resume:
        if source_changes is not None:
            raise UpstreamMergeError("--resume 续跑本轮，不接受 --source-changes（本轮源码已封存）")
        if _revision_complete(plan, latest):
            raise UpstreamMergeError(f"revision {latest:03d} 已封存完毕，没有需要续跑的轮次")
        if latest < 2:
            raise UpstreamMergeError("--resume 只续跑 revision-advance 推进的轮次（002 起）；首轮按 §5.2.3 逐步执行")
        revision = latest
    else:
        if not _revision_complete(plan, latest):
            raise UpstreamMergeError(
                f"revision {latest:03d} 尚未封存到 U-3；用 --resume 续跑，不能另起新编号"
            )
        if source_changes is None:
            raise UpstreamMergeError("推进新 revision 需要 --source-changes（本轮源码修复的路径与理由）")
        revision = latest + 1
        source_input, source_input_mode = _prepare_source_change_input(plan, source_changes, revision, inputs_root)
        seal_source_candidate(plan, source_input)
        steps.append("source-seal")
    previous = revision - 1

    # U-2 发送面扫描
    if latest_revision(plan, "surface_delta") < revision:
        scan_surfaces(plan)
        steps.append("surface-scan")
    delta_path, delta = _revision_document(plan, "surface_delta", revision, SURFACE_DELTA_SCHEMA, "SurfaceDelta")
    _, previous_delta = _revision_document(plan, "surface_delta", previous, SURFACE_DELTA_SCHEMA, "上一轮 SurfaceDelta")
    _, previous_surface = _revision_document(plan, "surface_receipt", previous, SURFACE_RECEIPT_SCHEMA, "上一轮 SurfaceReceipt")
    current_ids = _surface_delta_ids(delta)
    previous_ids = _surface_delta_ids(previous_delta)
    same_surface = current_ids == previous_ids
    surface_difference = {
        "added_delta_ids": sorted(set(current_ids) - set(previous_ids)),
        "removed_delta_ids": sorted(set(previous_ids) - set(current_ids)),
    }

    # U-2 Inventory 绑定与发送面封存
    reuse: dict[str, Any] = {}
    if latest_revision(plan, "surface_receipt") < revision:
        state = _inventory_state(plan, revision)
        written = {key: path for key, path in state.items() if path is not None}
        previous_inventories = _previous_inventories(plan, previous_surface)
        # 与上一轮逐字节相同的视为工具已续用（例如上次续用中途中断），其余是人工按专用流程写的。
        human_written = [key for key, path in written.items() if path.read_bytes() != previous_inventories[key].read_bytes()]
        if not written and not same_surface:
            targets = [str(next_inventory_path(plan, client, kind, revision=revision)) for client, kind in state]
            return _awaiting(
                "U-2 Inventory",
                revision,
                [{"kind": "surface_delta_changed", **surface_difference}],
                steps,
                inventory_targets=targets,
                surface_decision=str(inputs_root / _input_name("surface-decision", revision)),
                next="按 U-2 专用流程为本轮写出四份 Inventory 与签名 SurfaceDecision 后 --resume",
            )
        if len(written) != len(state) and same_surface and not human_written:
            reuse["inventories"] = _carry_inventories(plan, previous_inventories, revision, set(written))
            steps.append("inventory-carry")
        elif len(written) != len(state):
            missing = sorted(f"{client}/{kind}" for (client, kind), path in state.items() if path is None)
            return _awaiting(
                "U-2 Inventory",
                revision,
                [{"kind": "inventory_missing", "inventories": missing}],
                steps,
                next="补齐本轮缺少的 Inventory 后 --resume",
            )
        decision_path: Path | None = None
        if delta["deltas"]:
            decision_path = inputs_root / _input_name("surface-decision", revision)
            if not decision_path.exists():
                previous_binding = previous_surface.get("surface_decision")
                if not same_surface or previous_binding is None:
                    return _awaiting(
                        "U-2 SurfaceDecision",
                        revision,
                        [{"kind": "surface_decision_missing", **surface_difference}],
                        steps,
                        surface_decision=str(decision_path),
                        next="写出本轮签名 SurfaceDecision 后 --resume",
                    )
                _, previous_decision = _bound_json(previous_binding, "上一轮 SurfaceDecision")
                source = _load_source_candidate(plan)
                write_json_input(
                    decision_path,
                    bind_identity(
                        {
                            "schema_version": SURFACE_DECISION_SCHEMA,
                            "plan_id": plan.plan_id,
                            "plan_identity_sha256": plan.identity,
                            "source_tree": source["source_tree"],
                            "surface_delta_sha256": sha256_file(delta_path),
                            "decisions": previous_decision["decisions"],
                        }
                    ),
                    "SurfaceDecision",
                )
                reuse["surface_decision"] = len(previous_decision["decisions"])
                steps.append("surface-decision-carry")
        seal_surfaces(plan, decision_path)
        steps.append("surface-seal")

    # U-3 影响矩阵与 ChangeDecision
    if latest_revision(plan, "impact_matrix") < revision:
        generate_impact_matrix(plan)
        steps.append("impact-generate")
    counts: dict[str, int] | None = None
    if not _revision_complete(plan, revision):
        signed_path = inputs_root / _input_name("change-decision", revision)
        draft_path = inputs_root / _input_name("change-decision", revision, "-draft")
        if not signed_path.exists():
            matrix_path, matrix = _revision_document(plan, "impact_matrix", revision, IMPACT_MATRIX_SCHEMA, "ImpactMatrix")
            if draft_path.exists():
                draft = expect_object(load_json(draft_path, "ChangeDecision 草稿"), "ChangeDecision 草稿")
                if draft.get("impact_matrix_sha256") != sha256_file(matrix_path):
                    raise UpstreamMergeError(f"ChangeDecision 草稿未绑定本轮 ImpactMatrix：{draft_path}")
                draft.pop("identity_sha256", None)
                draft = _recount(draft)
                pending = _pending_in_draft(draft)
                if pending:
                    return _awaiting(
                        "U-3 ChangeDecision",
                        revision,
                        pending,
                        steps,
                        draft=str(draft_path),
                        next="在草稿里把这些条目改为 decision_source=manual 并写明理由后 --resume",
                    )
            else:
                suggestion = generate_change_decision_suggestion(plan)
                write_json_input(
                    inputs_root / _input_name("change-decision", revision, "-suggested"),
                    suggestion,
                    "ChangeDecision 建议稿",
                )
                steps.append("impact-suggest")
                _, previous_matrix = _revision_document(
                    plan, "impact_matrix", previous, IMPACT_MATRIX_SCHEMA, "上一轮 ImpactMatrix"
                )
                _, previous_receipt = _revision_document(
                    plan, "impact_receipt", previous, CHANGE_DECISION_RECEIPT_SCHEMA, "上一轮 ChangeDecisionReceipt"
                )
                _, previous_decision = _bound_json(previous_receipt["change_decision"], "上一轮 ChangeDecision")
                if previous_decision.get("schema_version") != CHANGE_DECISION_INPUT_SCHEMA:
                    raise UpstreamMergeError("上一轮 ChangeDecision schema_version 非法")
                draft, pending, counts = _fill_change_decision(suggestion, matrix, previous_matrix, previous_decision)
                if pending:
                    write_json_input(draft_path, draft, "ChangeDecision 草稿")
                    return _awaiting(
                        "U-3 ChangeDecision",
                        revision,
                        pending,
                        steps,
                        draft=str(draft_path),
                        reuse_counts=counts,
                        next="在草稿里把这些条目改为 decision_source=manual 并写明理由后 --resume",
                    )
            write_json_input(signed_path, bind_identity(draft), "ChangeDecision")
            steps.append("change-decision-fill")
        seal_change_decision(plan, signed_path)
        steps.append("impact-seal")
    preflight = preflight_revisions(plan)
    steps.append("revision-preflight")
    return {
        "result": "advanced",
        "revision": revision,
        "source_change_input": source_input_mode,
        "completed_steps": steps,
        "same_surface_as_previous": same_surface,
        "reuse": {**reuse, **({"change_decision": counts} if counts else {})},
        "preflight": preflight,
    }
