"""完整 UpstreamMergePlan v2、计划请求和公共制品合同。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.official_client_control.contracts import (
    validate_object_document,
    validate_persona,
)
from tools.official_client_control.errors import ControlError

from .baseline import validate_baseline_acceptance
from .canonical import (
    TAG_RE,
    VERSION_RE,
    artifact_binding,
    bind_identity,
    ensure_private_directory,
    expect_exact_fields,
    expect_git_object,
    expect_object,
    expect_safe_id,
    expect_string,
    file_binding,
    load_json,
    resolve_within,
    safe_relative_path,
    validate_artifact_binding,
    validate_file_binding,
    validate_identity,
    validate_string_enum,
    write_json_once,
)
from .errors import UpstreamMergeError
from .gitops import (
    assert_clean,
    assert_git_repository,
    command_environment,
    commit_tree,
    current_branch_ref,
    merge_base,
    protected_objects,
    remote_url,
    rev_parse,
    route_snapshot,
    run_egress_snapshot,
    run_git,
    tag_commit,
    tool_bundle,
    validate_protected_objects,
    validate_tool_bundle,
)


LEGACY_REQUEST_SCHEMA = "official-egress-upstream-merge-request/v1"
REQUEST_SCHEMA = "official-egress-upstream-merge-request/v2"
PLAN_SCHEMA = "official-egress-upstream-merge-plan/v2"
PLAN_PURPOSE = "upstream_merge"
CLIENT_KEYS = ("claude", "codex")

REQUIRED_GATE_CATEGORIES = (
    "claude_active_wire",
    "claude_ingress_matrix",
    "claude_rollback_wire",
    "codex_active_wire",
    "codex_ingress_matrix",
    "codex_rollback_wire",
    "cross_persona",
    "inventory_closure",
    "original_business",
    "secret_scan",
    "shared_full_regression",
    "shared_static",
)

REQUIRED_PROTECTED_PATHS = (
    "backend/internal/officialegress/catalogdata/claude",
    "backend/internal/officialegress/catalogdata/runtime",
    "backend/internal/officialegress/claude_production_release.go",
    "backend/internal/officialegress/persona_release_catalog.go",
    "backend/internal/service/official_client_profile_registry.go",
    "docs/egress",
)

OUTPUT_KEYS = {
    "branch_apply",
    "candidate_disposition",
    "candidate_inventories",
    "codex_overlay_ledger",
    "conflict_ledger",
    "gate_attempts_root",
    "impact_matrix",
    "impact_receipt",
    "merge_candidate",
    "merge_start",
    "source_candidate",
    "surface_delta",
    "surface_egress_snapshot",
    "surface_receipt",
    "surface_route_snapshot",
    "upstream_merge_receipt",
}

# 新计划为可迭代的 U-2/U-3 制品增加追加式输出根。它们不是旧计划的必填字段，
# 这样历史 v2 计划仍可只读回放；新建计划会自动写入这些根。
REVISION_OUTPUT_KEYS = {
    "source_candidate_revisions_root",
    "surface_revisions_root",
    "impact_revisions_root",
    "candidate_inventories_revisions_root",
}

REVISION_STAGE_ROOTS = {
    "source_candidate": "source_candidate_revisions_root",
    "surface_route_snapshot": "surface_revisions_root",
    "surface_egress_snapshot": "surface_revisions_root",
    "surface_delta": "surface_revisions_root",
    "surface_receipt": "surface_revisions_root",
    "impact_matrix": "impact_revisions_root",
    "impact_receipt": "impact_revisions_root",
}

REVISION_STAGE_STEMS = {
    "source_candidate": "source-candidate",
    "surface_route_snapshot": "route-snapshot",
    "surface_egress_snapshot": "source-to-sink-snapshot",
    "surface_delta": "surface-delta",
    "surface_receipt": "surface-receipt",
    "impact_matrix": "impact-matrix",
    "impact_receipt": "change-decision-receipt",
}

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_ALLOWED_PLACEHOLDERS = {
    "candidate_commit",
    "candidate_tree",
    "evidence_root",
    "plan",
    "receipt",
    "repository",
}


@dataclass(frozen=True)
class LoadedPlan:
    """经过严格复算的完整计划及其受控路径。"""

    document: dict[str, Any]
    path: Path
    repository_root: Path
    evidence_root: Path
    worktree: Path

    @property
    def plan_id(self) -> str:
        return str(self.document["plan_id"])

    @property
    def identity(self) -> str:
        return str(self.document["identity_sha256"])

    @property
    def fork_head(self) -> str:
        return str(self.document["repository"]["fork_head"])

    @property
    def upstream_commit(self) -> str:
        return str(self.document["upstream"]["commit"])

    @property
    def managed_ref(self) -> str:
        return str(self.document["repository"]["managed_ref"])

    def output_relative(self, key: str) -> str:
        if key not in OUTPUT_KEYS | REVISION_OUTPUT_KEYS or key in {"candidate_inventories"}:
            raise UpstreamMergeError(f"未知或非标量输出：{key}")
        return str(self.document["outputs"][key])

    def output_path(self, key: str) -> Path:
        return resolve_within(self.evidence_root, self.output_relative(key), f"outputs.{key}")

    def inventory_output(self, client: str, kind: str) -> Path:
        if client not in CLIENT_KEYS or kind not in {"ingress", "egress"}:
            raise UpstreamMergeError(f"候选 Inventory 位置非法：{client}/{kind}")
        relative = self.document["outputs"]["candidate_inventories"][client][kind]
        revision_root = self.document["outputs"].get(
            "candidate_inventories_revisions_root"
        )
        if isinstance(revision_root, str):
            return latest_inventory_path(self, client, kind, fallback=relative)
        return resolve_within(
            self.evidence_root,
            relative,
            f"outputs.candidate_inventories.{client}.{kind}",
        )

    @property
    def plan_binding(self) -> dict[str, Any]:
        return file_binding(self.path)

    @property
    def baseline_acceptance(self) -> dict[str, Any] | None:
        """新计划的基线验收绑定；历史旧计划可能没有该字段。"""

        value = self.document.get("baseline_acceptance")
        return value if isinstance(value, dict) else None


def _safe_absolute_path(value: Any, label: str) -> Path:
    raw = expect_string(value, label)
    path = Path(raw)
    if not path.is_absolute():
        raise UpstreamMergeError(f"{label} 必须是绝对路径")
    normalized = Path(os.path.normpath(raw))
    if str(normalized) != raw:
        raise UpstreamMergeError(f"{label} 必须是规范绝对路径")
    return path.resolve(strict=False)


def _assert_separate_roots(
    repository_root: Path,
    worktree: Path,
    evidence_root: Path,
    *,
    allow_repository_worktree: bool = False,
) -> None:
    repo = repository_root.resolve(strict=True)
    user_home = Path.home().resolve(strict=True)
    forbidden = {Path("/"), repo, user_home}
    for path, label in ((worktree, "workspace.worktree"), (evidence_root, "workspace.evidence_root")):
        execution_root = (
            label == "workspace.worktree"
            and allow_repository_worktree
            and path == repo
        )
        if path in forbidden and not execution_root:
            raise UpstreamMergeError(f"{label} 不能指向系统根、仓库根或用户主目录")
        resolved_parent = path.parent.resolve(strict=True)
        if resolved_parent == Path("/") and len(path.parts) <= 2:
            raise UpstreamMergeError(f"{label} 路径过宽：{path}")
        if (path == repository_root or path.is_relative_to(repository_root)) and not execution_root:
            raise UpstreamMergeError(f"{label} 必须位于主仓库之外")
    if worktree == evidence_root or worktree.is_relative_to(evidence_root) or evidence_root.is_relative_to(worktree):
        raise UpstreamMergeError("隔离 worktree 与 evidence root 不得互相嵌套")


def _validate_persona(value: Any, label: str) -> dict[str, Any]:
    try:
        return validate_persona(value, label)
    except ControlError as error:
        raise UpstreamMergeError(str(error)) from error


def _validate_inventory_payload(
    path: Path,
    kind: str,
    expected_persona: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    payload = load_json(path, label)
    object_kind = (
        "production_ingress_inventory" if kind == "ingress" else "egress_disposition_inventory"
    )
    try:
        validate_object_document(
            {
                "schema_version": "official-client-control-object/v1",
                "object_kind": object_kind,
                "payload": payload,
            }
        )
    except ControlError as error:
        raise UpstreamMergeError(f"{label} 不符合受管 Inventory 合同：{error}") from error
    if payload.get("persona") != expected_persona:
        raise UpstreamMergeError(f"{label} Persona 与计划不一致")
    return payload


def _validate_json_binding(value: Any, label: str) -> dict[str, Any]:
    binding = validate_file_binding(value, label)
    load_json(Path(binding["path"]), label)
    return binding


def _validate_client_request(value: Any, label: str) -> dict[str, Any]:
    client = expect_object(value, label)
    expect_exact_fields(
        client,
        {"persona", "target_version", "active_path", "rollback_path"},
        label,
    )
    _validate_persona(client.get("persona"), f"{label}.persona")
    version = expect_string(client.get("target_version"), f"{label}.target_version")
    if not VERSION_RE.fullmatch(version):
        raise UpstreamMergeError(f"{label}.target_version 不是三段式版本")
    for field in ("active_path", "rollback_path"):
        path = _safe_absolute_path(client.get(field), f"{label}.{field}")
        load_json(path, f"{label}.{field}")
    return client


def _validate_client_plan(value: Any, label: str) -> dict[str, Any]:
    client = expect_object(value, label)
    expect_exact_fields(client, {"persona", "target_version", "active", "rollback"}, label)
    _validate_persona(client.get("persona"), f"{label}.persona")
    version = expect_string(client.get("target_version"), f"{label}.target_version")
    if not VERSION_RE.fullmatch(version):
        raise UpstreamMergeError(f"{label}.target_version 不是三段式版本")
    _validate_json_binding(client.get("active"), f"{label}.active")
    _validate_json_binding(client.get("rollback"), f"{label}.rollback")
    return client


def _validate_cwd(value: Any, label: str) -> str:
    text = expect_string(value, label)
    if text == ".":
        return text
    return safe_relative_path(text, label)


def _validate_argv(value: Any, label: str, *, require_receipt: bool) -> list[str]:
    if not isinstance(value, list) or not value:
        raise UpstreamMergeError(f"{label} 必须是非空 argv 数组")
    result: list[str] = []
    placeholders: set[str] = set()
    for index, item in enumerate(value):
        text = expect_string(item, f"{label}[{index}]")
        if "\x00" in text or "\n" in text or "\r" in text:
            raise UpstreamMergeError(f"{label}[{index}] 含控制字符")
        found = set(_PLACEHOLDER_RE.findall(text))
        unknown = found - _ALLOWED_PLACEHOLDERS
        if unknown:
            raise UpstreamMergeError(f"{label}[{index}] 含未知占位符：{sorted(unknown)}")
        placeholders.update(found)
        result.append(text)
    if require_receipt and "receipt" not in placeholders:
        raise UpstreamMergeError(f"{label} 的 receipt_replay 必须显式使用 {{receipt}}")
    return result


def _validate_gates(value: Any, label: str = "gates") -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise UpstreamMergeError(f"{label} 必须是非空数组")
    ids: list[str] = []
    categories: list[str] = []
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_label = f"{label}[{index}]"
        gate = expect_object(raw, item_label)
        mode = validate_string_enum(gate.get("mode"), {"command", "receipt_replay"}, f"{item_label}.mode")
        expected = {"id", "category", "mode", "cwd", "argv"}
        if mode == "receipt_replay":
            expected.add("receipt")
        optional = {"execution_group"}
        actual = set(gate)
        if actual - (expected | optional) or expected - actual:
            raise UpstreamMergeError(
                f"{item_label} 字段不闭合：缺失={sorted(expected - actual)}，"
                f"多余={sorted(actual - expected - optional)}"
            )
        gate_id = expect_safe_id(gate.get("id"), f"{item_label}.id")
        category = validate_string_enum(
            gate.get("category"), REQUIRED_GATE_CATEGORIES, f"{item_label}.category"
        )
        _validate_cwd(gate.get("cwd"), f"{item_label}.cwd")
        _validate_argv(
            gate.get("argv"),
            f"{item_label}.argv",
            require_receipt=mode == "receipt_replay",
        )
        if mode == "receipt_replay":
            safe_relative_path(gate.get("receipt"), f"{item_label}.receipt")
        if "execution_group" in gate:
            expect_safe_id(gate.get("execution_group"), f"{item_label}.execution_group")
        ids.append(gate_id)
        categories.append(category)
        normalized.append(gate)
    if ids != sorted(set(ids)):
        raise UpstreamMergeError(f"{label} 必须按 id 排序且不得重复")
    if sorted(categories) != list(REQUIRED_GATE_CATEGORIES):
        missing = sorted(set(REQUIRED_GATE_CATEGORIES) - set(categories))
        extra = sorted(set(categories) - set(REQUIRED_GATE_CATEGORIES))
        duplicate = sorted({item for item in categories if categories.count(item) > 1})
        raise UpstreamMergeError(
            f"{label} 必须恰好覆盖固定门禁类别：缺失={missing}，多余={extra}，重复={duplicate}"
        )
    return normalized


UPSTREAM_FIELDS = {"remote", "url", "tag", "commit"}
COVERED_TAGS_FIELD = "covered_tags"
_TAG_MINOR_RE = re.compile(r"^v([0-9]+)\.([0-9]+)\.")


def _tag_minor(tag: str) -> tuple[int, int]:
    match = _TAG_MINOR_RE.match(tag)
    if match is None:
        raise UpstreamMergeError(f"无法解析 tag 的 minor 版本：{tag}")
    return int(match.group(1)), int(match.group(2))


def _expect_upstream_fields(upstream: dict[str, Any], label: str) -> None:
    """upstream 固定四个字段，covered_tags 可选；其余字段一律拒绝。"""

    actual = set(upstream)
    allowed = UPSTREAM_FIELDS | {COVERED_TAGS_FIELD}
    if UPSTREAM_FIELDS - actual or actual - allowed:
        raise UpstreamMergeError(
            f"{label} 字段不闭合：缺失={sorted(UPSTREAM_FIELDS - actual)}，"
            f"多余={sorted(actual - allowed)}"
        )


def _validate_covered_tags_shape(upstream: dict[str, Any], label: str) -> list[dict[str, str]] | None:
    """covered_tags 的结构校验，不访问 Git；缺省时返回 None。

    covered_tags 按祖先顺序列出本次一次合入的全部上游版本 tag，每项冻结 tag 名与 commit，
    最后一项必须就是目标 tag 与 commit；全部 tag 与目标同一 minor，跨 minor 的合并仍须分 Plan。
    """

    if COVERED_TAGS_FIELD not in upstream:
        return None
    raw = upstream[COVERED_TAGS_FIELD]
    if not isinstance(raw, list) or not raw:
        raise UpstreamMergeError(f"{label}.covered_tags 必须是非空数组")
    target_minor = _tag_minor(expect_string(upstream.get("tag"), f"{label}.tag"))
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, value in enumerate(raw):
        item_label = f"{label}.covered_tags[{index}]"
        item = expect_object(value, item_label)
        expect_exact_fields(item, {"tag", "commit"}, item_label)
        tag = expect_string(item.get("tag"), f"{item_label}.tag")
        if not TAG_RE.fullmatch(tag):
            raise UpstreamMergeError(f"{item_label}.tag 不是受支持的版本 tag")
        if tag in seen:
            raise UpstreamMergeError(f"{item_label}.tag 重复：{tag}")
        if _tag_minor(tag) != target_minor:
            raise UpstreamMergeError(f"{item_label}.tag 与目标 tag 不在同一 minor，跨 minor 必须分 Plan：{tag}")
        seen.add(tag)
        entries.append({"tag": tag, "commit": expect_git_object(item.get("commit"), f"{item_label}.commit")})
    if entries[-1] != {"tag": upstream.get("tag"), "commit": upstream.get("commit")}:
        raise UpstreamMergeError(f"{label}.covered_tags 最后一项必须是目标 tag 与 commit")
    return entries


def upstream_range_tags(repository_root: Path, base_commit: str, target_commit: str) -> list[dict[str, str]]:
    """base_commit 之后（不含）到 target_commit（含）的全部版本 tag，按祖先顺序排列。

    只统计符合版本格式的 tag；相邻两个 tag 必须在同一条祖先链上，否则区间不是线性的
    上游历史，fail-close。
    """

    listed = run_git(
        repository_root,
        "tag",
        "--list",
        "--merged",
        target_commit,
        "--no-merged",
        base_commit,
    ).stdout.splitlines()
    ranked: list[tuple[int, str, str]] = []
    for tag in sorted({item.strip() for item in listed if item.strip()}):
        if not TAG_RE.fullmatch(tag):
            continue
        commit = tag_commit(repository_root, tag)
        depth = int(run_git(repository_root, "rev-list", "--count", commit).stdout.strip())
        ranked.append((depth, tag, commit))
    ranked.sort()
    ordered = [{"tag": tag, "commit": commit} for _depth, tag, commit in ranked]
    for previous, current in zip(ordered, ordered[1:]):
        if previous["commit"] == current["commit"]:
            continue
        ancestry = run_git(
            repository_root,
            "merge-base",
            "--is-ancestor",
            previous["commit"],
            current["commit"],
            check=False,
        )
        if ancestry.returncode != 0:
            raise UpstreamMergeError(
                f"区间内上游 tag 不在同一条祖先链上：{previous['tag']} → {current['tag']}"
            )
    return ordered


def resolve_covered_tags(
    repository_root: Path,
    upstream: dict[str, Any],
    base_commit: str,
    *,
    label: str,
    require_explicit: bool,
) -> list[dict[str, str]] | None:
    """用 Git 复算 covered_tags：必须与 merge-base 之后到目标 commit 的全部版本 tag 逐项一致。

    省略 covered_tags 时：区间内只有目标 tag，视为只合这一个 tag，返回 None；区间内有多个
    tag 且 require_explicit 为真（新建 Plan），一律拒绝——一次合入多个 tag 必须显式登记。
    """

    declared = _validate_covered_tags_shape(upstream, label)
    target = {"tag": upstream["tag"], "commit": upstream["commit"]}
    actual = upstream_range_tags(repository_root, base_commit, str(upstream["commit"]))
    if not actual or actual[-1] != target:
        raise UpstreamMergeError(f"目标 tag 不是 merge-base 之后上游区间的末端：{upstream['tag']}")
    target_minor = _tag_minor(str(upstream["tag"]))
    crossed = [item["tag"] for item in actual if _tag_minor(item["tag"]) != target_minor]
    if crossed:
        raise UpstreamMergeError(f"merge-base 之后的上游区间跨 minor，必须分 Plan：{crossed}")
    if declared is None:
        if len(actual) > 1 and require_explicit:
            raise UpstreamMergeError(
                f"merge-base 之后到目标共有 {len(actual)} 个上游 tag，必须在 upstream.covered_tags 逐个登记："
                + ", ".join(item["tag"] for item in actual)
            )
        return None
    if declared != actual:
        raise UpstreamMergeError(
            f"{label}.covered_tags 与 Git 复算不一致："
            f"登记={[item['tag'] for item in declared]} 实际={[item['tag'] for item in actual]}"
        )
    return declared


def load_request(path: Path) -> dict[str, Any]:
    request = expect_object(load_json(path, "UpstreamMergeRequest"), "UpstreamMergeRequest")
    expect_exact_fields(
        request,
        {
            "schema_version",
            "plan_id",
            "upstream",
            "repository",
            "workspace",
            "official_clients",
            "baselines",
            "protected_repository_paths",
            "gates",
        },
        "UpstreamMergeRequest",
    )
    request_schema = request.get("schema_version")
    if request_schema not in {LEGACY_REQUEST_SCHEMA, REQUEST_SCHEMA}:
        raise UpstreamMergeError("UpstreamMergeRequest schema_version 非法")
    expect_safe_id(request.get("plan_id"), "UpstreamMergeRequest.plan_id")
    upstream = expect_object(request.get("upstream"), "UpstreamMergeRequest.upstream")
    _expect_upstream_fields(upstream, "UpstreamMergeRequest.upstream")
    expect_safe_id(upstream.get("remote"), "upstream.remote")
    url = expect_string(upstream.get("url"), "upstream.url")
    if not url.startswith("https://"):
        raise UpstreamMergeError("upstream.url 必须使用 HTTPS")
    tag = expect_string(upstream.get("tag"), "upstream.tag")
    if not TAG_RE.fullmatch(tag):
        raise UpstreamMergeError("upstream.tag 不是受支持的版本 tag")
    expect_git_object(upstream.get("commit"), "upstream.commit")
    # 只做结构校验；与 Git 历史的逐项复算在 plan-create 与 load_plan 中进行。
    _validate_covered_tags_shape(upstream, "UpstreamMergeRequest.upstream")
    repository = expect_object(request.get("repository"), "UpstreamMergeRequest.repository")
    expect_exact_fields(repository, {"managed_ref"}, "UpstreamMergeRequest.repository")
    managed_ref = expect_string(repository.get("managed_ref"), "repository.managed_ref")
    if not managed_ref.startswith("refs/heads/"):
        raise UpstreamMergeError("repository.managed_ref 必须是本地分支完整引用")
    workspace = expect_object(request.get("workspace"), "UpstreamMergeRequest.workspace")
    expect_exact_fields(workspace, {"worktree", "evidence_root"}, "UpstreamMergeRequest.workspace")
    _safe_absolute_path(workspace.get("worktree"), "workspace.worktree")
    _safe_absolute_path(workspace.get("evidence_root"), "workspace.evidence_root")
    clients = expect_object(request.get("official_clients"), "official_clients")
    expect_exact_fields(clients, set(CLIENT_KEYS), "official_clients")
    for client in CLIENT_KEYS:
        _validate_client_request(clients[client], f"official_clients.{client}")
    if clients["codex"]["persona"] == clients["claude"]["persona"]:
        raise UpstreamMergeError("Codex 与 Claude Persona 不得共用身份")
    baselines = expect_object(request.get("baselines"), "baselines")
    baseline_fields = {
        "production_ingress_inventory",
        "egress_disposition_inventory",
        "runtime_state_path",
        "recovery_point_path",
    }
    if request_schema == REQUEST_SCHEMA:
        baseline_fields.add("baseline_acceptance_path")
    expect_exact_fields(baselines, baseline_fields, "baselines")
    for kind_field, kind in (
        ("production_ingress_inventory", "ingress"),
        ("egress_disposition_inventory", "egress"),
    ):
        inventory_map = expect_object(baselines.get(kind_field), f"baselines.{kind_field}")
        expect_exact_fields(inventory_map, set(CLIENT_KEYS), f"baselines.{kind_field}")
        for client in CLIENT_KEYS:
            inventory_path = _safe_absolute_path(
                inventory_map[client], f"baselines.{kind_field}.{client}"
            )
            _validate_inventory_payload(
                inventory_path,
                kind,
                clients[client]["persona"],
                f"baselines.{kind_field}.{client}",
            )
    for field in ("runtime_state_path", "recovery_point_path"):
        value_path = _safe_absolute_path(baselines.get(field), f"baselines.{field}")
        load_json(value_path, f"baselines.{field}")
    if request_schema == REQUEST_SCHEMA:
        baseline_path = _safe_absolute_path(
            baselines.get("baseline_acceptance_path"),
            "baselines.baseline_acceptance_path",
        )
        if baseline_path.is_symlink() or not baseline_path.is_file():
            raise UpstreamMergeError("baselines.baseline_acceptance_path 不是可信普通文件")
    protected = request.get("protected_repository_paths")
    if not isinstance(protected, list) or not protected:
        raise UpstreamMergeError("protected_repository_paths 必须是非空数组")
    normalized_paths = [
        safe_relative_path(item, f"protected_repository_paths[{index}]")
        for index, item in enumerate(protected)
    ]
    if normalized_paths != sorted(set(normalized_paths)):
        raise UpstreamMergeError("protected_repository_paths 必须排序且不得重复")
    missing_protected = sorted(set(REQUIRED_PROTECTED_PATHS) - set(normalized_paths))
    if missing_protected:
        raise UpstreamMergeError(f"protected_repository_paths 缺少固定边界：{missing_protected}")
    _validate_gates(request.get("gates"))
    return request


def _output_layout(upstream_tag: str) -> dict[str, Any]:
    return {
        "codex_overlay_ledger": (
            f"docs/egress/maintenance/upstream-{upstream_tag}-egress-merge-ledger.json"
        ),
        "merge_start": "u1/merge-start.json",
        "merge_candidate": "u1/merge-candidate.json",
        "conflict_ledger": "u1/conflict-resolution-ledger.json",
        "source_candidate": "u2/source-candidate.json",
        "surface_route_snapshot": "u2/route-snapshot.json",
        "surface_egress_snapshot": "u2/source-to-sink-snapshot.json",
        "surface_delta": "u2/surface-delta.json",
        "surface_receipt": "u2/surface-recalculation-receipt.json",
        "candidate_inventories": {
            "claude": {
                "ingress": "u2/claude-production-ingress-inventory.json",
                "egress": "u2/claude-egress-disposition-inventory.json",
            },
            "codex": {
                "ingress": "u2/codex-production-ingress-inventory.json",
                "egress": "u2/codex-egress-disposition-inventory.json",
            },
        },
        "impact_matrix": "u3/impact-matrix.json",
        "impact_receipt": "u3/change-decision-receipt.json",
        "gate_attempts_root": "u4/attempts",
        "candidate_disposition": "u5/candidate-disposition.json",
        "branch_apply": "u6/branch-apply.json",
        "upstream_merge_receipt": "u6/upstream-merge-receipt.json",
        "source_candidate_revisions_root": "u2/source-candidates",
        "surface_revisions_root": "u2/surface-revisions",
        "impact_revisions_root": "u3/impact-revisions",
        "candidate_inventories_revisions_root": "u2/inventory-revisions",
    }


def _validate_baseline_binding(
    root: Path,
    binding: dict[str, Any],
    fork_head: str,
    fork_tree: str,
    *,
    require_current: bool,
) -> None:
    """校验基线收据，并确保它就是计划冻结的 fork HEAD/tree。

    ``require_current`` 只在 U-0 创建计划时使用。计划进入后续阶段后，受维护
    分支可能已经快进；历史计划仍应能够只读回放，因此不能把“当前 HEAD”误当
    成基线身份。
    """

    path = Path(binding["path"])
    receipt = validate_baseline_acceptance(
        root,
        path,
        require_current=require_current,
    )
    repository = expect_object(receipt.get("repository"), "BaselineAcceptance.repository")
    if repository.get("commit") != fork_head or repository.get("tree") != fork_tree:
        raise UpstreamMergeError(
            "BaselineAcceptance 未绑定计划 fork HEAD/tree："
            f"expected={fork_head}/{fork_tree} "
            f"actual={repository.get('commit')}/{repository.get('tree')}"
        )


def _validate_outputs(value: Any, upstream_tag: str) -> dict[str, Any]:
    outputs = expect_object(value, "outputs")
    actual_keys = set(outputs)
    required_keys = set(OUTPUT_KEYS)
    allowed_keys = required_keys | REVISION_OUTPUT_KEYS
    if actual_keys != required_keys and not (
        required_keys <= actual_keys <= allowed_keys
    ):
        raise UpstreamMergeError(
            "outputs 字段不闭合："
            f"缺失={sorted(required_keys - actual_keys)}，"
            f"多余={sorted(actual_keys - allowed_keys)}"
        )
    revision_keys = actual_keys & REVISION_OUTPUT_KEYS
    if revision_keys and revision_keys != REVISION_OUTPUT_KEYS:
        raise UpstreamMergeError(
            "outputs revision 根必须全部声明或全部省略："
            f"缺失={sorted(REVISION_OUTPUT_KEYS - revision_keys)}"
        )
    paths: list[str] = []
    for key, raw in outputs.items():
        if key == "candidate_inventories":
            mapping = expect_object(raw, "outputs.candidate_inventories")
            expect_exact_fields(mapping, set(CLIENT_KEYS), "outputs.candidate_inventories")
            for client in CLIENT_KEYS:
                pair = expect_object(mapping[client], f"outputs.candidate_inventories.{client}")
                expect_exact_fields(pair, {"ingress", "egress"}, f"outputs.candidate_inventories.{client}")
                for kind in ("ingress", "egress"):
                    paths.append(
                        safe_relative_path(
                            pair[kind], f"outputs.candidate_inventories.{client}.{kind}"
                        )
                    )
            continue
        relative = safe_relative_path(raw, f"outputs.{key}")
        if key in REVISION_OUTPUT_KEYS:
            # revision 根只允许作为目录前缀，禁止以绝对路径或含点段的形式出现；
            # 具体文件名由本模块的 revision 辅助函数统一生成。
            if relative.endswith(".json"):
                raise UpstreamMergeError(f"outputs.{key} 必须是 revision 目录")
            paths.append(relative)
            continue
        if key == "codex_overlay_ledger":
            expected = (
                f"docs/egress/maintenance/upstream-{upstream_tag}-egress-merge-ledger.json"
            )
            if relative != expected:
                raise UpstreamMergeError(f"outputs.codex_overlay_ledger 必须是 {expected}")
        else:
            paths.append(relative)
    if paths != list(dict.fromkeys(paths)) or len(paths) != len(set(paths)):
        raise UpstreamMergeError("evidence 输出路径不得重复")
    return outputs


def create_plan(request_path: Path, repository_root: Path) -> LoadedPlan:
    """从显式请求生成 U-0 完整计划及两类发现基线。"""

    root = assert_git_repository(repository_root)
    request = load_request(request_path)
    if request.get("schema_version") != REQUEST_SCHEMA:
        raise UpstreamMergeError(
            "新建正式 Plan 必须使用 request/v2，并提供基线验收收据；"
            "旧 request/v1 只能迁移后使用"
        )
    assert_clean(root, "U-0 主仓库")
    managed_ref = request["repository"]["managed_ref"]
    if current_branch_ref(root) != managed_ref:
        raise UpstreamMergeError("当前分支与 request.repository.managed_ref 不一致")
    fork_head = rev_parse(root, f"{managed_ref}^{{commit}}")
    if rev_parse(root, "HEAD^{commit}") != fork_head:
        raise UpstreamMergeError("当前 HEAD 与受维护分支不一致")
    upstream = request["upstream"]
    if remote_url(root, upstream["remote"]) != upstream["url"]:
        raise UpstreamMergeError("Git remote URL 与计划请求不一致")
    if tag_commit(root, upstream["tag"]) != upstream["commit"]:
        raise UpstreamMergeError("upstream tag 与固定 commit 不一致")
    run_git(root, "cat-file", "-e", f"{upstream['commit']}^{{commit}}")
    already_merged = run_git(
        root,
        "merge-base",
        "--is-ancestor",
        upstream["commit"],
        fork_head,
        check=False,
    )
    if already_merged.returncode == 0:
        raise UpstreamMergeError("目标 upstream commit 已包含在当前 fork HEAD 中")
    if already_merged.returncode not in {0, 1}:
        raise UpstreamMergeError("无法判断 upstream commit 与 fork HEAD 的祖先关系")
    planned_merge_base = merge_base(root, fork_head, upstream["commit"])
    covered_tags = resolve_covered_tags(
        root,
        upstream,
        planned_merge_base,
        label="UpstreamMergeRequest.upstream",
        require_explicit=True,
    )
    # Plan 总是显式登记覆盖区间；只合一个 tag 时即目标 tag 本身。
    plan_upstream = {
        **upstream,
        COVERED_TAGS_FIELD: covered_tags or [{"tag": upstream["tag"], "commit": upstream["commit"]}],
    }
    worktree = _safe_absolute_path(request["workspace"]["worktree"], "workspace.worktree")
    evidence_requested = _safe_absolute_path(
        request["workspace"]["evidence_root"], "workspace.evidence_root"
    )
    baseline_acceptance_path = _safe_absolute_path(
        request["baselines"]["baseline_acceptance_path"],
        "baselines.baseline_acceptance_path",
    )
    _validate_baseline_binding(
        root,
        file_binding(baseline_acceptance_path),
        fork_head,
        commit_tree(root, fork_head),
        require_current=True,
    )
    if worktree.exists():
        raise UpstreamMergeError(f"隔离 worktree 目标必须不存在：{worktree}")
    if evidence_requested.exists() and any(evidence_requested.iterdir()):
        raise UpstreamMergeError("新计划的 evidence root 必须不存在或为空")
    evidence_root = ensure_private_directory(evidence_requested, create=True)
    _assert_separate_roots(root, worktree, evidence_root)

    route_path = resolve_within(evidence_root, "u0/route-snapshot.json", "U-0 route snapshot")
    fork_tree = commit_tree(root, fork_head)
    write_json_once(route_path, route_snapshot(root, fork_head, fork_tree))
    egress_path = resolve_within(
        evidence_root,
        "u0/source-to-sink-snapshot.json",
        "U-0 source-to-sink snapshot",
    )
    run_egress_snapshot(root, egress_path)
    environment_path = resolve_within(evidence_root, "u0/environment.json", "U-0 environment")
    write_json_once(
        environment_path,
        {
            "schema_version": "official-egress-upstream-tool-environment/v1",
            "tools": command_environment(),
        },
    )

    clients: dict[str, Any] = {}
    for client in CLIENT_KEYS:
        source = request["official_clients"][client]
        clients[client] = {
            "persona": source["persona"],
            "target_version": source["target_version"],
            "active": file_binding(Path(source["active_path"])),
            "rollback": file_binding(Path(source["rollback_path"])),
        }
    baselines: dict[str, Any] = {
        "production_ingress_inventory": {},
        "egress_disposition_inventory": {},
        "runtime_state": file_binding(Path(request["baselines"]["runtime_state_path"])),
        "recovery_point": file_binding(Path(request["baselines"]["recovery_point_path"])),
    }
    for field in ("production_ingress_inventory", "egress_disposition_inventory"):
        for client in CLIENT_KEYS:
            baselines[field][client] = file_binding(Path(request["baselines"][field][client]))

    document: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "plan_id": request["plan_id"],
        "purpose": PLAN_PURPOSE,
        "upstream": plan_upstream,
        "repository": {
            "managed_ref": managed_ref,
            "fork_head": fork_head,
            "fork_tree": fork_tree,
            "merge_base": planned_merge_base,
            "protected_objects": protected_objects(
                root,
                fork_head,
                request["protected_repository_paths"],
            ),
        },
        "workspace": {
            "worktree": str(worktree),
            "evidence_root": str(evidence_root),
        },
        "official_clients": clients,
        "baselines": baselines,
        "baseline_acceptance": file_binding(baseline_acceptance_path),
        "discovery_baseline": {
            "route_snapshot": artifact_binding(evidence_root, route_path),
            "source_to_sink_snapshot": artifact_binding(evidence_root, egress_path),
        },
        "tool_bundle": tool_bundle(root),
        "environment": artifact_binding(evidence_root, environment_path),
        "gates": request["gates"],
        "outputs": _output_layout(upstream["tag"]),
    }
    document = bind_identity(document)
    plan_path = evidence_root / "plan.json"
    write_json_once(plan_path, document)
    return load_plan(plan_path, root)


def load_plan(
    path: Path,
    repository_root: Path,
    *,
    allow_execution_worktree: bool = False,
) -> LoadedPlan:
    """加载、复算并交叉验证完整 UpstreamMergePlan v2。"""

    root = assert_git_repository(repository_root)
    plan = expect_object(load_json(path, "UpstreamMergePlan"), "UpstreamMergePlan")
    required_plan_fields = {
        "schema_version",
        "plan_id",
        "purpose",
        "upstream",
        "repository",
        "workspace",
        "official_clients",
        "baselines",
        "discovery_baseline",
        "tool_bundle",
        "environment",
        "gates",
        "outputs",
        "identity_sha256",
    }
    optional_plan_fields = {"baseline_acceptance"}
    actual_plan_fields = set(plan)
    if actual_plan_fields - (required_plan_fields | optional_plan_fields) or (
        required_plan_fields - actual_plan_fields
    ):
        raise UpstreamMergeError(
            "UpstreamMergePlan 字段不闭合："
            f"缺失={sorted(required_plan_fields - actual_plan_fields)}，"
            f"多余={sorted(actual_plan_fields - (required_plan_fields | optional_plan_fields))}"
        )
    if plan.get("schema_version") != PLAN_SCHEMA or plan.get("purpose") != PLAN_PURPOSE:
        raise UpstreamMergeError("只接受完整 upstream_merge UpstreamMergePlan v2")
    expect_safe_id(plan.get("plan_id"), "UpstreamMergePlan.plan_id")
    validate_identity(plan, "UpstreamMergePlan")

    upstream = expect_object(plan.get("upstream"), "upstream")
    _expect_upstream_fields(upstream, "upstream")
    remote = expect_safe_id(upstream.get("remote"), "upstream.remote")
    url = expect_string(upstream.get("url"), "upstream.url")
    if not url.startswith("https://") or remote_url(root, remote) != url:
        raise UpstreamMergeError("upstream URL 与本地 remote 不一致")
    tag = expect_string(upstream.get("tag"), "upstream.tag")
    if not TAG_RE.fullmatch(tag):
        raise UpstreamMergeError("upstream.tag 非法")
    commit = expect_git_object(upstream.get("commit"), "upstream.commit")
    if tag_commit(root, tag) != commit:
        raise UpstreamMergeError("upstream tag/commit 漂移")

    repository = expect_object(plan.get("repository"), "repository")
    expect_exact_fields(
        repository,
        {"managed_ref", "fork_head", "fork_tree", "merge_base", "protected_objects"},
        "repository",
    )
    managed_ref = expect_string(repository.get("managed_ref"), "repository.managed_ref")
    if not managed_ref.startswith("refs/heads/"):
        raise UpstreamMergeError("repository.managed_ref 非法")
    fork_head = expect_git_object(repository.get("fork_head"), "repository.fork_head")
    fork_tree = expect_git_object(repository.get("fork_tree"), "repository.fork_tree")
    planned_base = expect_git_object(repository.get("merge_base"), "repository.merge_base")
    if commit_tree(root, fork_head) != fork_tree:
        raise UpstreamMergeError("repository.fork_tree 漂移")
    if merge_base(root, fork_head, commit) != planned_base:
        raise UpstreamMergeError("repository.merge_base 漂移")
    # 早期 Plan 没有 covered_tags，按单 tag 合并解释；登记了就必须能由 Git 逐项复算。
    if COVERED_TAGS_FIELD in upstream:
        resolve_covered_tags(root, upstream, planned_base, label="upstream", require_explicit=False)
    validate_protected_objects(root, fork_head, repository.get("protected_objects"))

    workspace = expect_object(plan.get("workspace"), "workspace")
    expect_exact_fields(workspace, {"worktree", "evidence_root"}, "workspace")
    worktree = _safe_absolute_path(workspace.get("worktree"), "workspace.worktree")
    evidence_requested = _safe_absolute_path(
        workspace.get("evidence_root"), "workspace.evidence_root"
    )
    evidence_root = ensure_private_directory(evidence_requested, create=False)
    execution_root = (
        allow_execution_worktree
        and worktree.exists()
        and worktree.resolve(strict=True) == root
    )
    _assert_separate_roots(
        root,
        worktree,
        evidence_root,
        allow_repository_worktree=execution_root,
    )
    resolved_plan = path.resolve(strict=True)
    if resolved_plan != evidence_root / "plan.json":
        raise UpstreamMergeError("完整计划必须固定为 evidence_root/plan.json")

    clients = expect_object(plan.get("official_clients"), "official_clients")
    expect_exact_fields(clients, set(CLIENT_KEYS), "official_clients")
    for client in CLIENT_KEYS:
        _validate_client_plan(clients[client], f"official_clients.{client}")
    if clients["codex"]["persona"] == clients["claude"]["persona"]:
        raise UpstreamMergeError("Codex 与 Claude Persona 身份重叠")

    baselines = expect_object(plan.get("baselines"), "baselines")
    expect_exact_fields(
        baselines,
        {
            "production_ingress_inventory",
            "egress_disposition_inventory",
            "runtime_state",
            "recovery_point",
        },
        "baselines",
    )
    for field, kind in (
        ("production_ingress_inventory", "ingress"),
        ("egress_disposition_inventory", "egress"),
    ):
        mapping = expect_object(baselines.get(field), f"baselines.{field}")
        expect_exact_fields(mapping, set(CLIENT_KEYS), f"baselines.{field}")
        for client in CLIENT_KEYS:
            binding = validate_file_binding(mapping[client], f"baselines.{field}.{client}")
            _validate_inventory_payload(
                Path(binding["path"]),
                kind,
                clients[client]["persona"],
                f"baselines.{field}.{client}",
            )
    _validate_json_binding(baselines.get("runtime_state"), "baselines.runtime_state")
    _validate_json_binding(baselines.get("recovery_point"), "baselines.recovery_point")

    discovery = expect_object(plan.get("discovery_baseline"), "discovery_baseline")
    expect_exact_fields(
        discovery,
        {"route_snapshot", "source_to_sink_snapshot"},
        "discovery_baseline",
    )
    route_binding = validate_artifact_binding(
        evidence_root, discovery.get("route_snapshot"), "discovery_baseline.route_snapshot"
    )
    egress_binding = validate_artifact_binding(
        evidence_root,
        discovery.get("source_to_sink_snapshot"),
        "discovery_baseline.source_to_sink_snapshot",
    )
    route_document = load_json(
        resolve_within(evidence_root, route_binding["path"], "route snapshot"),
        "U-0 route snapshot",
    )
    egress_document = load_json(
        resolve_within(evidence_root, egress_binding["path"], "source-to-sink snapshot"),
        "U-0 source-to-sink snapshot",
    )
    if (
        route_document.get("source_commit") != fork_head
        or route_document.get("source_tree") != fork_tree
        or egress_document.get("source_commit") != fork_head
        or egress_document.get("source_tree") != fork_tree
    ):
        raise UpstreamMergeError("U-0 发现基线未绑定 fork HEAD/tree")

    validate_tool_bundle(root, plan.get("tool_bundle"))
    if "baseline_acceptance" in plan:
        baseline_binding = validate_file_binding(
            plan.get("baseline_acceptance"), "baseline_acceptance"
        )
        _validate_baseline_binding(
            root,
            baseline_binding,
            fork_head,
            fork_tree,
            require_current=False,
        )
    environment_binding = validate_artifact_binding(
        evidence_root, plan.get("environment"), "environment"
    )
    environment_document = load_json(
        resolve_within(evidence_root, environment_binding["path"], "environment"),
        "U-0 environment",
    )
    if environment_document.get("schema_version") != "official-egress-upstream-tool-environment/v1":
        raise UpstreamMergeError("U-0 environment schema_version 非法")
    _validate_gates(plan.get("gates"))
    _validate_outputs(plan.get("outputs"), tag)
    return LoadedPlan(
        document=plan,
        path=resolved_plan,
        repository_root=root,
        evidence_root=evidence_root,
        worktree=worktree,
    )


def artifact_document(
    path: Path,
    label: str,
    schema: str,
    fields: set[str],
    *,
    optional_fields: set[str] | None = None,
) -> dict[str, Any]:
    """加载带自摘要的不可变阶段制品。

    ``optional_fields`` 仅用于向后兼容追加式 metadata；未知字段仍然失败关闭。
    """

    document = expect_object(load_json(path, label), label)
    expected = fields | {"schema_version", "identity_sha256"}
    allowed = expected | (optional_fields or set())
    actual = set(document)
    if actual - allowed or expected - actual:
        raise UpstreamMergeError(
            f"{label} 字段不闭合：缺失={sorted(expected - actual)}，"
            f"多余={sorted(actual - allowed)}"
        )
    if document.get("schema_version") != schema:
        raise UpstreamMergeError(f"{label} schema_version 非法")
    validate_identity(document, label)
    return document


def _revision_root_path(plan: LoadedPlan, key: str) -> Path | None:
    root_key = REVISION_STAGE_ROOTS.get(key)
    if root_key is None:
        raise UpstreamMergeError(f"阶段不支持 revision：{key}")
    raw = plan.document.get("outputs", {}).get(root_key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise UpstreamMergeError(f"计划 revision 输出根类型非法：outputs.{root_key}")
    relative = safe_relative_path(raw, f"outputs.{root_key}")
    raw_path = plan.evidence_root / Path(relative)
    if raw_path.is_symlink():
        raise UpstreamMergeError(f"revision 输出根不得是符号链接：{raw_path}")
    return resolve_within(
        plan.evidence_root,
        relative,
        f"outputs.{root_key}",
    )


def _revision_pattern(key: str) -> re.Pattern[str]:
    stem = re.escape(REVISION_STAGE_STEMS[key])
    return re.compile(rf"^{stem}-(?P<revision>[0-9]+)\.json$")


def revision_number(path: Path, plan: LoadedPlan, key: str) -> int:
    """返回阶段文件的 revision 编号；原兼容路径固定视为第 1 轮。"""

    if path.resolve(strict=False) == plan.output_path(key).resolve(strict=False):
        return 1
    match = _revision_pattern(key).fullmatch(path.name)
    if match is None:
        raise UpstreamMergeError(f"阶段文件名不是受支持的 revision：{path.name}")
    number = int(match.group("revision"))
    if number < 2:
        raise UpstreamMergeError(f"revision 文件编号必须从 002 开始：{path.name}")
    return number


def stage_paths(plan: LoadedPlan, key: str, *, existing_only: bool = True) -> list[Path]:
    """按 revision 顺序返回某个阶段的全部不可变文件。"""

    if key not in REVISION_STAGE_ROOTS:
        path = plan.output_path(key)
        return [path] if (not existing_only or path.exists()) else []
    paths: list[Path] = []
    legacy = plan.output_path(key)
    if legacy.exists() or legacy.is_symlink() or not existing_only:
        paths.append(legacy)
    root = _revision_root_path(plan, key)
    if root is None:
        return paths
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise UpstreamMergeError(f"revision 输出根不可信：{root}")
        pattern = _revision_pattern(key)
        for candidate in root.iterdir():
            if pattern.fullmatch(candidate.name) is None:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise UpstreamMergeError(f"revision 阶段文件不可信：{candidate}")
            paths.append(candidate)
    paths.sort(key=lambda item: (revision_number(item, plan, key), item.as_posix()))
    numbers = [revision_number(path, plan, key) for path in paths]
    if numbers != sorted(set(numbers)):
        raise UpstreamMergeError(f"阶段 revision 编号重复：{key}")
    if numbers and numbers[0] != 1:
        raise UpstreamMergeError(f"阶段 revision 必须从第 1 轮开始：{key}")
    if numbers != list(range(1, len(numbers) + 1)):
        raise UpstreamMergeError(f"阶段 revision 链存在缺口：{key}={numbers}")
    return paths


def latest_stage_path(plan: LoadedPlan, key: str, *, required: bool = True) -> Path:
    paths = stage_paths(plan, key)
    if not paths:
        if required:
            raise UpstreamMergeError(f"缺少阶段制品：{key}")
        return plan.output_path(key)
    return paths[-1]


def latest_revision(plan: LoadedPlan, key: str = "source_candidate") -> int:
    paths = stage_paths(plan, key)
    return revision_number(paths[-1], plan, key) if paths else 0


def next_stage_path(
    plan: LoadedPlan,
    key: str,
    *,
    revision: int | None = None,
) -> tuple[int, Path]:
    """返回下一轮阶段输出路径，不创建或覆盖文件。"""

    current = latest_revision(plan, key)
    number = current + 1 if revision is None else revision
    if number < 1 or number != current + 1:
        raise UpstreamMergeError(
            f"新 revision 必须严格递增一轮：key={key} current={current} requested={number}"
        )
    if number == 1:
        path = plan.output_path(key)
    else:
        root = _revision_root_path(plan, key)
        if root is None:
            raise UpstreamMergeError(
                f"当前计划未声明 revision 输出根：{REVISION_STAGE_ROOTS[key]}"
            )
        path = root / f"{REVISION_STAGE_STEMS[key]}-{number:03d}.json"
    if path.exists() or path.is_symlink():
        raise UpstreamMergeError(f"revision 输出已存在，禁止覆盖：{path}")
    return number, path


def latest_inventory_path(
    plan: LoadedPlan,
    client: str,
    kind: str,
    *,
    fallback: str,
) -> Path:
    """返回某 Persona Inventory 的最新追加轮次或旧兼容路径。"""

    if client not in CLIENT_KEYS or kind not in {"ingress", "egress"}:
        raise UpstreamMergeError(f"Inventory revision 参数非法：{client}/{kind}")
    legacy = resolve_within(
        plan.evidence_root,
        safe_relative_path(fallback, f"outputs.candidate_inventories.{client}.{kind}"),
        f"outputs.candidate_inventories.{client}.{kind}",
    )
    raw_root = plan.document.get("outputs", {}).get(
        "candidate_inventories_revisions_root"
    )
    if not isinstance(raw_root, str):
        return legacy
    relative_root = safe_relative_path(
        raw_root,
        "outputs.candidate_inventories_revisions_root",
    )
    raw_root_path = plan.evidence_root / Path(relative_root)
    if raw_root_path.is_symlink():
        raise UpstreamMergeError(f"Inventory revision 根不得是符号链接：{raw_root_path}")
    root = resolve_within(
        plan.evidence_root,
        relative_root,
        "outputs.candidate_inventories_revisions_root",
    )
    if not root.exists():
        return legacy
    if root.is_symlink() or not root.is_dir():
        raise UpstreamMergeError(f"Inventory revision 根不可信：{root}")
    stem = f"{client}-{kind}-inventory"
    pattern = re.compile(rf"^{re.escape(stem)}-([0-9]+)\.json$")
    candidates: list[tuple[int, Path]] = []
    for path in root.iterdir():
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        if path.is_symlink() or not path.is_file():
            raise UpstreamMergeError(f"Inventory revision 文件不可信：{path}")
        candidates.append((int(match.group(1)), path))
    if not candidates:
        return legacy
    if not legacy.exists() or legacy.is_symlink() or not legacy.is_file():
        raise UpstreamMergeError(
            f"Inventory revision 缺少第 1 轮固定制品：{client}/{kind}"
        )
    candidates.sort(key=lambda item: item[0])
    numbers = [number for number, _ in candidates]
    if numbers != sorted(set(numbers)):
        raise UpstreamMergeError(f"Inventory revision 编号重复：{client}/{kind}")
    if numbers[0] < 2 or numbers != list(range(2, numbers[-1] + 1)):
        raise UpstreamMergeError(f"Inventory revision 链存在缺口：{client}/{kind}={numbers}")
    return candidates[-1][1]


def next_inventory_path(
    plan: LoadedPlan,
    client: str,
    kind: str,
    *,
    revision: int,
) -> Path:
    """返回指定 SourceCandidate 轮次的 Inventory 输出路径。"""

    raw_root = plan.document.get("outputs", {}).get(
        "candidate_inventories_revisions_root"
    )
    if not isinstance(raw_root, str):
        raise UpstreamMergeError(
            "当前计划未声明 candidate_inventories_revisions_root，无法追加 Inventory revision"
        )
    if revision < 2:
        raise UpstreamMergeError("追加 Inventory revision 必须从 002 开始")
    relative_root = safe_relative_path(
        raw_root,
        "outputs.candidate_inventories_revisions_root",
    )
    raw_root_path = plan.evidence_root / Path(relative_root)
    if raw_root_path.is_symlink():
        raise UpstreamMergeError(f"Inventory revision 根不得是符号链接：{raw_root_path}")
    root = resolve_within(
        plan.evidence_root,
        relative_root,
        "outputs.candidate_inventories_revisions_root",
    )
    if client not in CLIENT_KEYS or kind not in {"ingress", "egress"}:
        raise UpstreamMergeError(f"Inventory revision 参数非法：{client}/{kind}")
    # 每个 Persona/kind 的 Inventory 也必须按 001（旧固定路径）、002、003…连续追加。
    fallback = plan.document["outputs"]["candidate_inventories"][client][kind]
    latest = latest_inventory_path(plan, client, kind, fallback=fallback)
    current = inventory_revision_number(plan, client, kind, latest)
    if revision != current + 1:
        raise UpstreamMergeError(
            f"Inventory revision 必须连续追加：{client}/{kind} current={current} requested={revision}"
        )
    path = root / f"{client}-{kind}-inventory-{revision:03d}.json"
    if path.exists() or path.is_symlink():
        raise UpstreamMergeError(f"Inventory revision 输出已存在，禁止覆盖：{path}")
    return path


def inventory_revision_number(
    plan: LoadedPlan,
    client: str,
    kind: str,
    path: Path,
) -> int:
    """返回 Inventory 所属 SourceCandidate 轮次；旧固定路径视为第 1 轮。"""

    legacy = resolve_within(
        plan.evidence_root,
        plan.document["outputs"]["candidate_inventories"][client][kind],
        f"outputs.candidate_inventories.{client}.{kind}",
    )
    if path.resolve(strict=False) == legacy.resolve(strict=False):
        return 1
    raw_root = plan.document.get("outputs", {}).get(
        "candidate_inventories_revisions_root"
    )
    if not isinstance(raw_root, str):
        raise UpstreamMergeError("Inventory revision 根未声明")
    relative_root = safe_relative_path(
        raw_root,
        "outputs.candidate_inventories_revisions_root",
    )
    raw_root_path = plan.evidence_root / Path(relative_root)
    if raw_root_path.is_symlink():
        raise UpstreamMergeError(f"Inventory revision 根不得是符号链接：{raw_root_path}")
    stem = f"{client}-{kind}-inventory"
    match = re.fullmatch(rf"{re.escape(stem)}-([0-9]+)\.json", path.name)
    if match is None:
        raise UpstreamMergeError(f"Inventory 文件名不是受支持的 revision：{path.name}")
    number = int(match.group(1))
    if number < 2:
        raise UpstreamMergeError(f"Inventory revision 必须从 002 开始：{path.name}")
    expected_root = resolve_within(
        plan.evidence_root,
        relative_root,
        "outputs.candidate_inventories_revisions_root",
    )
    if not path.resolve(strict=False).is_relative_to(expected_root.resolve()):
        raise UpstreamMergeError("Inventory 不在 revision 输出根内")
    return number


def stage_binding(plan: LoadedPlan, key: str) -> dict[str, Any]:
    # 旧计划没有 revision 根时退回原固定路径；新计划始终绑定最新追加轮次。
    if key in {
        "source_candidate",
        "surface_route_snapshot",
        "surface_egress_snapshot",
        "surface_delta",
        "surface_receipt",
        "impact_matrix",
        "impact_receipt",
    } and REVISION_OUTPUT_KEYS & set(plan.document.get("outputs", {})):
        path = latest_stage_path(plan, key)
    else:
        path = plan.output_path(key)
    return artifact_binding(plan.evidence_root, path)
