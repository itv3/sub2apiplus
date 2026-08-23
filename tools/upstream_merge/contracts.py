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


REQUEST_SCHEMA = "official-egress-upstream-merge-request/v1"
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
        if key not in OUTPUT_KEYS or key in {"candidate_inventories"}:
            raise UpstreamMergeError(f"未知或非标量输出：{key}")
        return str(self.document["outputs"][key])

    def output_path(self, key: str) -> Path:
        return resolve_within(self.evidence_root, self.output_relative(key), f"outputs.{key}")

    def inventory_output(self, client: str, kind: str) -> Path:
        if client not in CLIENT_KEYS or kind not in {"ingress", "egress"}:
            raise UpstreamMergeError(f"候选 Inventory 位置非法：{client}/{kind}")
        relative = self.document["outputs"]["candidate_inventories"][client][kind]
        return resolve_within(
            self.evidence_root,
            relative,
            f"outputs.candidate_inventories.{client}.{kind}",
        )

    @property
    def plan_binding(self) -> dict[str, Any]:
        return file_binding(self.path)


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
        expect_exact_fields(gate, expected, item_label)
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
    if request.get("schema_version") != REQUEST_SCHEMA:
        raise UpstreamMergeError("UpstreamMergeRequest schema_version 非法")
    expect_safe_id(request.get("plan_id"), "UpstreamMergeRequest.plan_id")
    upstream = expect_object(request.get("upstream"), "UpstreamMergeRequest.upstream")
    expect_exact_fields(upstream, {"remote", "url", "tag", "commit"}, "UpstreamMergeRequest.upstream")
    expect_safe_id(upstream.get("remote"), "upstream.remote")
    url = expect_string(upstream.get("url"), "upstream.url")
    if not url.startswith("https://"):
        raise UpstreamMergeError("upstream.url 必须使用 HTTPS")
    tag = expect_string(upstream.get("tag"), "upstream.tag")
    if not TAG_RE.fullmatch(tag):
        raise UpstreamMergeError("upstream.tag 不是受支持的版本 tag")
    expect_git_object(upstream.get("commit"), "upstream.commit")
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
    expect_exact_fields(
        baselines,
        {
            "production_ingress_inventory",
            "egress_disposition_inventory",
            "runtime_state_path",
            "recovery_point_path",
        },
        "baselines",
    )
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
    }


def _validate_outputs(value: Any, upstream_tag: str) -> dict[str, Any]:
    outputs = expect_object(value, "outputs")
    expect_exact_fields(outputs, OUTPUT_KEYS, "outputs")
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
    worktree = _safe_absolute_path(request["workspace"]["worktree"], "workspace.worktree")
    evidence_requested = _safe_absolute_path(
        request["workspace"]["evidence_root"], "workspace.evidence_root"
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
        "upstream": upstream,
        "repository": {
            "managed_ref": managed_ref,
            "fork_head": fork_head,
            "fork_tree": fork_tree,
            "merge_base": merge_base(root, fork_head, upstream["commit"]),
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
    expect_exact_fields(
        plan,
        {
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
        },
        "UpstreamMergePlan",
    )
    if plan.get("schema_version") != PLAN_SCHEMA or plan.get("purpose") != PLAN_PURPOSE:
        raise UpstreamMergeError("只接受完整 upstream_merge UpstreamMergePlan v2")
    expect_safe_id(plan.get("plan_id"), "UpstreamMergePlan.plan_id")
    validate_identity(plan, "UpstreamMergePlan")

    upstream = expect_object(plan.get("upstream"), "upstream")
    expect_exact_fields(upstream, {"remote", "url", "tag", "commit"}, "upstream")
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


def artifact_document(path: Path, label: str, schema: str, fields: set[str]) -> dict[str, Any]:
    """加载带自摘要的不可变阶段制品。"""

    document = expect_object(load_json(path, label), label)
    expect_exact_fields(document, fields | {"schema_version", "identity_sha256"}, label)
    if document.get("schema_version") != schema:
        raise UpstreamMergeError(f"{label} schema_version 非法")
    validate_identity(document, label)
    return document


def stage_binding(plan: LoadedPlan, key: str) -> dict[str, Any]:
    path = plan.output_path(key)
    return artifact_binding(plan.evidence_root, path)
