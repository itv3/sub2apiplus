"""从标准模板生成正式合并请求（UM-18）。

v0.2.13 合并时请求靠会话临时脚本渲染，模板里 ``CODEX_0_149_1_SOURCE_ROOT={repository}/...`` 原样留到
执行期，被 gates-run 渲染成候选工作树（被 git 忽略的源码树不在那里），本机门禁因此连红两轮；v0.2.10
的脚本则把门禁里的 ``{repository}`` 全部提前换成主仓库，secret-scan 扫的是主仓库而不是候选。这里把渲染
收进工具，口径固定为：

- 路径字段（official_clients、baselines、workspace）里的占位符在这里渲染成绝对路径；
- 门禁 argv 里的 ``{source_repository}`` 在这里渲染成主仓库（只读本地资源所在），``{repository}``、
  ``{plan}``、``{evidence_root}`` 等执行期占位符原样保留，由 gates-run 渲染；
- Claude 的 Active／Rollback 取 release catalog 的 production_active 与 production_rollback；Codex 取
  runtime 发布图里 mode=active／previous 节点的快照，再到 snapshot catalog 找文件；
- covered_tags 用 Git 复算（与 plan-create 同一函数），跨 minor 拒绝；
- 同时写出绑定当前 HEAD 的运行态与回退点。plan-create 之前可以重渲染覆盖，之后一律拒绝。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import ensure_private_directory, expect_object, load_json, pretty_bytes, sha256_file
from .contracts import _tag_minor, upstream_range_tags
from .errors import UpstreamMergeError
from .gitops import assert_clean, assert_git_repository, merge_base, rev_parse, tag_commit
from .preflight_report import (
    CLAUDE_RELEASE_CATALOG,
    OFFICIAL_EGRESS_ROOT,
    RUNTIME_RELEASE_CATALOG,
    SOURCE_REPOSITORY_PLACEHOLDER,
    load_request_template,
    template_validity,
)

RUNTIME_STATE_SCHEMA = "official-egress-upstream-runtime-state/v1"
RECOVERY_POINT_SCHEMA = "official-egress-upstream-recovery-point/v1"
# Plan 目录命名 <上游 tag>-<yyyymmdd>-<三位序号>，plan_id 由它派生，与历次 Plan 一致。
PLAN_ROOT_NAME = re.compile(r"(v[0-9]+\.[0-9]+\.[0-9]+)-([0-9]{8})-([0-9]{3})")
INVENTORY_FILES = (
    "u2/claude-production-ingress-inventory.json",
    "u2/codex-production-ingress-inventory.json",
    "u2/claude-egress-disposition-inventory.json",
    "u2/codex-egress-disposition-inventory.json",
)
FINALIZE_RECEIPT = "u6/upstream-merge-receipt.json"
# 门禁 argv 里留到执行期由 gates-run 渲染的占位符；其余占位符必须在这里全部替换。
EXECUTION_PLACEHOLDERS = ("{repository}", "{plan}", "{evidence_root}", "{candidate_commit}", "{candidate_tree}", "{receipt}")


def _claude_binding(repository: Path) -> dict[str, str]:
    catalog = expect_object(load_json(repository / PurePosixPath(CLAUDE_RELEASE_CATALOG), "ClaudeReleaseCatalog"), "ClaudeReleaseCatalog")
    selectors = expect_object(catalog.get("selectors"), "ClaudeReleaseCatalog.selectors")
    active_sha = expect_object(selectors.get("production_active"), "selectors.production_active").get("release_sha256")
    release = next(
        (item for item in catalog.get("releases", []) if isinstance(item, dict) and item.get("release_sha256") == active_sha),
        None,
    )
    if release is None:
        raise UpstreamMergeError("Claude production_active 指向的 release 不在 releases 中")
    profile = expect_object(release.get("profile"), "Claude release.profile")
    deployment = expect_object(
        expect_object(selectors.get("production_rollback"), "selectors.production_rollback").get("deployment"),
        "selectors.production_rollback.deployment",
    )
    receipt = expect_object(deployment.get("receipt"), "production_rollback.deployment.receipt")
    receipt_path = repository / PurePosixPath(str(receipt.get("path")))
    rollback_sha = sha256_file(receipt_path)
    if rollback_sha != receipt.get("sha256"):
        raise UpstreamMergeError("Claude 回滚收据内容摘要与 release catalog 登记不一致")
    profile_path = repository / PurePosixPath(OFFICIAL_EGRESS_ROOT) / PurePosixPath(str(profile.get("path")))
    if sha256_file(profile_path) != profile.get("sha256"):
        raise UpstreamMergeError("Claude active profile 内容摘要与 release catalog 登记不一致")
    return {
        "target_version": str(release.get("version")),
        "active_profile": str(profile["path"]),
        "active_release_sha256": str(active_sha),
        "rollback_receipt": str(receipt["path"]),
        "rollback_receipt_sha256": rollback_sha,
    }


def _codex_binding(repository: Path) -> dict[str, str]:
    egress = repository / PurePosixPath(OFFICIAL_EGRESS_ROOT)
    runtime = expect_object(load_json(repository / PurePosixPath(RUNTIME_RELEASE_CATALOG), "RuntimeReleaseCatalog"), "RuntimeReleaseCatalog")
    bound: dict[str, Any] = {}
    for key, label in (("release_graph", "ReleaseGraph"), ("snapshot_catalog", "SnapshotCatalog")):
        binding = expect_object(runtime.get(key), f"RuntimeReleaseCatalog.{key}")
        path = egress / PurePosixPath(str(binding.get("path")))
        if sha256_file(path) != binding.get("sha256"):
            raise UpstreamMergeError(f"Codex {label} 内容摘要与 runtime release catalog 绑定不一致")
        bound[key] = expect_object(load_json(path, label), label)
    # 发布图里每个用途（HTTP、WS）各有 active 与 previous 节点；同一角色的快照必须一致。
    roles: dict[str, set[tuple[str, str]]] = {"active": set(), "previous": set()}
    for node in bound["release_graph"].get("nodes", []):
        if isinstance(node, dict) and node.get("mode") in roles:
            snapshot = expect_object(node.get("snapshot"), "ReleaseGraph.node.snapshot")
            roles[node["mode"]].add((str(snapshot.get("version")), str(snapshot.get("digest"))))
    snapshots = bound["snapshot_catalog"].get("snapshots")
    if not isinstance(snapshots, list):
        raise UpstreamMergeError("Codex snapshot catalog 缺少 snapshots")
    result: dict[str, str] = {}
    for role in ("active", "previous"):
        if len(roles[role]) != 1:
            raise UpstreamMergeError(f"Codex 发布图的 {role} 节点不是唯一快照：{sorted(roles[role])}")
        version, digest = next(iter(roles[role]))
        entry = next((item for item in snapshots if isinstance(item, dict) and item.get("digest") == digest), None)
        if entry is None or entry.get("version") != version:
            raise UpstreamMergeError(f"Codex {role} 快照不在 snapshot catalog 中：{version} {digest}")
        result[f"{role}_version"] = version
        result[f"{role}_digest"] = digest
        result[f"{role}_file"] = str(entry["file"])
    return result


def _previous_evidence(plan_root: Path, explicit: Path | None) -> Path:
    """四份 Inventory 基线取上一次走完 U-6 的 Plan；未显式指定时在 Plan 目录的同级里找最近一次。"""

    if explicit is not None:
        candidates = [explicit]
    else:
        finished = [
            path / "evidence"
            for path in plan_root.parent.iterdir()
            if path.is_dir() and path != plan_root and (path / "evidence" / FINALIZE_RECEIPT).is_file()
        ]
        candidates = sorted(finished, key=lambda item: (item / FINALIZE_RECEIPT).stat().st_mtime, reverse=True)[:1]
        if not candidates:
            raise UpstreamMergeError(f"{plan_root.parent} 下没有走完 U-6 的前序 Plan，须用 --previous-evidence 指定")
    evidence = candidates[0]
    missing = [name for name in INVENTORY_FILES if not (evidence / name).is_file()]
    if missing:
        raise UpstreamMergeError(f"前序 evidence 缺少 Inventory 基线：{evidence} {missing}")
    return evidence


def _replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        return value
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def _write_private(path: Path, document: Any) -> None:
    """Plan 建立前的请求输入允许重渲染：原子替换写入 0600 文件，拒绝符号链接。"""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise UpstreamMergeError(f"请求输入路径不可信：{path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pretty_bytes(document))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def render_request(
    repository_root: Path,
    *,
    plan_root: Path,
    upstream_tag: str,
    baseline_acceptance: Path,
    previous_evidence: Path | None = None,
) -> dict[str, Any]:
    """按标准模板生成 inputs/request.json、runtime-state.json、recovery-point.json。"""

    repository = assert_git_repository(repository_root)
    assert_clean(repository, "request-render 的主仓库")
    match = PLAN_ROOT_NAME.fullmatch(plan_root.name)
    if not plan_root.is_absolute() or match is None:
        raise UpstreamMergeError(f"Plan 目录必须是绝对路径并命名为 <上游 tag>-<yyyymmdd>-<序号>：{plan_root}")
    if match.group(1) != upstream_tag:
        raise UpstreamMergeError(f"Plan 目录名里的 tag 与 --upstream-tag 不一致：{plan_root.name}")
    if (plan_root / "evidence" / "plan.json").exists():
        raise UpstreamMergeError("该 Plan 已经 plan-create，不得重渲染请求；需要改请求时新建 Plan 目录")
    if not baseline_acceptance.is_absolute() or not baseline_acceptance.is_file():
        raise UpstreamMergeError(f"基线收据不存在：{baseline_acceptance}")
    ensure_private_directory(plan_root, create=True)
    inputs = ensure_private_directory(plan_root / "inputs", create=True)
    ensure_private_directory(plan_root / "evidence", create=True)

    head = rev_parse(repository, "HEAD^{commit}")
    upstream_commit = tag_commit(repository, upstream_tag)
    covered = upstream_range_tags(repository, merge_base(repository, head, upstream_commit), upstream_commit)
    if not covered or covered[-1] != {"tag": upstream_tag, "commit": upstream_commit}:
        raise UpstreamMergeError(f"目标 tag 不是 merge-base 之后上游区间的末端：{upstream_tag}")
    crossed = [item["tag"] for item in covered if _tag_minor(item["tag"]) != _tag_minor(upstream_tag)]
    if crossed:
        raise UpstreamMergeError(f"merge-base 之后的上游区间跨 minor，必须分 Plan：{crossed}")

    claude = _claude_binding(repository)
    codex = _codex_binding(repository)
    evidence = _previous_evidence(plan_root, previous_evidence)
    runtime_state = inputs / "runtime-state.json"
    recovery_point = inputs / "recovery-point.json"

    template = load_request_template(repository)
    request = json.loads(json.dumps(template["request"]))
    gates = request.pop("gates")
    replacements = {
        "{plan_root}": str(plan_root),
        "{upstream_tag}": upstream_tag,
        "{upstream_commit}": upstream_commit,
        "{plan_id}": f"sub2api-{upstream_tag}-merge-{match.group(2)}-{match.group(3)}",
        "{claude_target_version}": claude["target_version"],
        "{claude_active_profile}": claude["active_profile"],
        "{claude_rollback_receipt}": claude["rollback_receipt"],
        "{codex_target_version}": codex["active_version"],
        "{codex_active_profile}": codex["active_file"],
        "{codex_rollback_profile}": codex["previous_file"],
        "{previous_evidence}": str(evidence),
        "{baseline_acceptance}": str(baseline_acceptance),
        "{runtime_state}": str(runtime_state),
        "{recovery_point}": str(recovery_point),
        "{repository}": str(repository),
        SOURCE_REPOSITORY_PLACEHOLDER: str(repository),
    }
    request = _replace(request, replacements)
    request["upstream"]["covered_tags"] = covered
    # 门禁只渲染只读源码根；执行期占位符留给 gates-run（{repository} 那时是候选工作树）。
    request["gates"] = _replace(gates, {SOURCE_REPOSITORY_PLACEHOLDER: str(repository)})
    leftover = sorted(set(re.findall(r"\{[a-z_]+\}", json.dumps(request, ensure_ascii=False))) - set(EXECUTION_PLACEHOLDERS))
    if leftover:
        raise UpstreamMergeError(f"request 仍有未渲染的占位符：{leftover}")
    findings = template_validity(repository, request, template)["findings"]
    if findings:
        raise UpstreamMergeError("渲染出的 request 未通过模板有效性检查：" + "；".join(findings))

    _write_private(runtime_state, {
        "schema_version": RUNTIME_STATE_SCHEMA,
        "captured_commit": head,
        "source": "repository-catalog",
        "claude": {"target_version": claude["target_version"], "active_release_sha256": claude["active_release_sha256"], "guard_state": "enforced"},
        "codex": {"target_version": codex["active_version"], "active_profile_sha256": codex["active_digest"], "guard_state": "enforced"},
    })
    _write_private(recovery_point, {
        "schema_version": RECOVERY_POINT_SCHEMA,
        "captured_commit": head,
        "source": "repository-catalog",
        "claude": {"rollback_receipt_sha256": claude["rollback_receipt_sha256"], "rollback_role": "operational-deployment"},
        "codex": {"previous_version": codex["previous_version"], "previous_profile_sha256": codex["previous_digest"]},
    })
    request_path = inputs / "request.json"
    _write_private(request_path, request)
    return {
        "result": "rendered",
        "request": str(request_path),
        "plan_id": request["plan_id"],
        "captured_commit": head,
        "upstream": {"tag": upstream_tag, "commit": upstream_commit, "covered_tags": [item["tag"] for item in covered]},
        "claude_target_version": claude["target_version"],
        "codex": {"active": codex["active_version"], "previous": codex["previous_version"]},
        "previous_evidence": str(evidence),
        "baseline_acceptance": str(baseline_acceptance),
    }
