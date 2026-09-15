"""冻结台账 successor 的一次性生成。

上游合并后，仓库内多套冻结摘要台账（transition／successor／ledger／receipt 收据）会
逐一失效。Go 与 Python 门禁承接这些失效的方式是同一张“可审计 successor 图”：从
``docs/egress/maintenance/*.json`` 中抽取显式登记的 ``path → from → to`` 摘要边，再计算
传递闭包。本模块按完全相同的抽边规则复算冻结覆盖集合，对一个提交区间（或工作树）
里命中冻结覆盖的路径一次性生成 successor 收据，并对少数需要额外动作的台账给出精确
待办。

设计约束：

- 抽边规则必须与 ``backend/internal/officialegress`` 中的 ``loadAuditedSourceSuccessorEdges``
  逐字一致，否则工具认为“已承接”而门禁仍会失败。
- 只承接“已登记摘要 → 当前摘要”的精确边；前序摘要不在已登记集合中时视为链断裂，
  一律 fail-close，不允许凭空补边。
- successor 收据本身不进入它所描述的提交；它引用的收据也不得在本区间内变化。
  这两条一起消除 v0.2.3 合并时出现的“移出、重算、加回”自引用循环。
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import (
    SAFE_ID_RE,
    expect_git_object,
    expect_object,
    load_json,
    pretty_bytes,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
    validate_identity,
    write_once,
)
from .errors import UpstreamMergeError
from .gitops import assert_git_repository, git_output, run_git

MAINTENANCE_ROOT = "docs/egress/maintenance"
FREEZE_REGISTRY_RELATIVE = f"{MAINTENANCE_ROOT}/freeze-registry.json"
FREEZE_REGISTRY_SCHEMA = "official-egress-freeze-registry/v1"
FREEZE_SUCCESSOR_SCHEMA = "official-egress-upstream-freeze-successor/v1"
# B1：带证明删除冻结文件。收据 result 为 passed_with_deletions 时必须携带
# deletion_proof：删除原因、每个删除路径的工作树无引用扫描、历史读取兼容证明。
FREEZE_RESULT_PASSED = "passed_local_evidence_successor"
FREEZE_RESULT_MANUAL = "manual_actions_required"
FREEZE_RESULT_PASSED_WITH_DELETIONS = "passed_with_deletions"
DELETION_PROOF_ALGORITHM = "deletion-proof/v1"
REFERENCE_SCAN_ALGORITHM = "reference-scan/v1"
# 引用扫描范围：Python import 与属性引用、Shell／Makefile 调用、JSON 动作命令字段。
REFERENCE_SCAN_PYTHON_PATHSPECS = ("*.py",)
REFERENCE_SCAN_SHELL_PATHSPECS = ("*.sh", "Makefile", "*.mk")
REFERENCE_SCAN_JSON_PATHSPECS = ("*.json",)
REFERENCE_SCAN_JSON_COMMAND_KEYS = frozenset(
    {"command", "commands", "action", "argv", "args", "entrypoint", "script", "module", "tool"}
)
# 与 Go 侧 loadAuditedSourceSuccessorEdges 相同：只读取 schema_version 含这些标记的收据。
LEDGER_SCHEMA_MARKERS = ("successor", "transition", "ledger", "receipt")
PREDECESSOR_SCALAR_KEYS = ("predecessor_sha256", "from_sha256")
SUCCESSOR_SCALAR_KEYS = ("to_sha256", "current_sha256", "head_sha256")
FREEZE_VERIFICATION = [
    "go test ./internal/officialegress -run 'Frozen|Transition|Successor|Drift|Retirement' -count=1",
    "go test ./internal/service -run 'Frozen|Transition|Successor|Drift' -count=1",
    "make check-egress-spec-ci",
]
REGISTRY_ACTION_KINDS = {
    "python_receipt_list",
    "script_constant",
    "single_hop_file",
    "manual_required",
}


def gate_compatible_identity(document: dict[str, Any]) -> str:
    """按 Python 工作区门禁的算法计算自摘要：紧凑、键排序、无尾换行。

    Go 侧通用 successor 图不校验 identity；Python 门禁
    ``test_codex_0151_worktree_successor.py`` 会校验，且用的是无尾换行形态。
    freeze successor 必须能被两侧同时接受，因此统一采用门禁形态。
    """

    unsigned = {key: value for key, value in document.items() if key != "identity_sha256"}
    return sha256_bytes(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def bind_gate_compatible_identity(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    result["identity_sha256"] = gate_compatible_identity(result)
    return result


@dataclass(frozen=True)
class FrozenEdge:
    """一条显式登记的摘要边。"""

    path: str
    from_sha256: str
    to_sha256: str


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_strict_json(path: Path) -> Any | None:
    """按 Go Decoder 语义读取：解析失败或尾部有多余 JSON 都视为不可用。"""

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


class _EdgeCollector:
    """复刻 Go 侧的递归抽边：任何带非空 path 与 reason 的对象都可能登记边。"""

    def __init__(self) -> None:
        self.edges: dict[tuple[str, str, str], set[str]] = {}
        # known 只由成对边建立，对应 Go 侧 knownDigestsByPath，用于快照展开。
        self.known: dict[str, set[str]] = {}
        # registered 收集每条路径出现过的全部合法摘要（含只有后继、没有前序的
        # 新增文件条目），用于“前序摘要是否被登记过”的链断裂判定。
        self.registered: dict[str, set[str]] = {}
        self.receipts: dict[str, set[str]] = {}
        self.snapshots: list[tuple[str, str]] = []

    def register(self, path: str, digest: str, receipt: str) -> None:
        if not path.strip() or not _is_sha256(digest):
            return
        self.registered.setdefault(path, set()).add(digest)
        self.receipts.setdefault(path, set()).add(receipt)

    def add(self, path: str, from_digest: str, to_digest: str, receipt: str) -> None:
        if not path.strip() or not _is_sha256(from_digest) or not _is_sha256(to_digest):
            return
        if from_digest == to_digest:
            return
        key = (path, from_digest, to_digest)
        self.edges.setdefault(key, set()).add(receipt)
        known = self.known.setdefault(path, set())
        known.add(from_digest)
        known.add(to_digest)

    def visit(self, value: Any, receipt: str) -> None:
        if isinstance(value, list):
            for item in value:
                self.visit(item, receipt)
            return
        if not isinstance(value, dict):
            return
        path = value.get("path")
        reason = value.get("reason")
        if isinstance(path, str) and isinstance(reason, str) and path.strip() and reason.strip():
            predecessors: list[str] = []
            successors: list[str] = []
            listed = value.get("predecessor_sha256s")
            if isinstance(listed, list):
                predecessors.extend(item for item in listed if isinstance(item, str))
            for key in PREDECESSOR_SCALAR_KEYS:
                digest = value.get(key)
                if isinstance(digest, str):
                    predecessors.append(digest)
            for key in SUCCESSOR_SCALAR_KEYS:
                digest = value.get(key)
                if isinstance(digest, str):
                    successors.append(digest)
            before = value.get("before")
            if isinstance(before, dict) and isinstance(before.get("sha256"), str):
                predecessors.append(before["sha256"])
            after = value.get("after")
            if isinstance(after, dict) and isinstance(after.get("sha256"), str):
                successors.append(after["sha256"])
                if "existence" in after:
                    self.snapshots.append((path, after["sha256"]))
            for digest in (*predecessors, *successors):
                self.register(path, digest, receipt)
            for predecessor in predecessors:
                for successor in successors:
                    self.add(path, predecessor, successor, receipt)
        for child in value.values():
            self.visit(child, receipt)


def load_frozen_edges(repository_root: Path) -> tuple[list[FrozenEdge], dict[str, set[str]], dict[str, set[str]]]:
    """返回（边列表，路径→已登记摘要集合，路径→登记收据集合）。

    边列表与 Go 侧 loadAuditedSourceSuccessorEdges 逐条一致；已登记摘要集合比 Go 的
    knownDigestsByPath 更宽，额外包含只有后继摘要的新增文件条目，因为这些路径下次
    被修改时同样需要一条从其登记摘要出发的承接边。
    """

    root = assert_git_repository(repository_root)
    maintenance = root / MAINTENANCE_ROOT
    if not maintenance.is_dir():
        raise UpstreamMergeError(f"冻结台账目录不存在：{maintenance}")
    collector = _EdgeCollector()
    for candidate in sorted(maintenance.glob("*.json")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        document = _load_strict_json(candidate)
        if not isinstance(document, dict):
            continue
        schema = document.get("schema_version")
        if not isinstance(schema, str) or not any(marker in schema for marker in LEDGER_SCHEMA_MARKERS):
            continue
        receipt = candidate.relative_to(root).as_posix()
        collector.visit(document, receipt)
    # Codex CLI 0.151 worktree successor 是一个已封存快照：任何已登记历史摘要都可
    # 承接到该快照。Go 侧把这一语义展开为有限边，这里保持一致。
    snapshot_receipt = f"{MAINTENANCE_ROOT}/codex-cli-0151-worktree-successor.json"
    for path, after in collector.snapshots:
        for digest in sorted(collector.known.get(path, set())):
            collector.add(path, digest, after, snapshot_receipt)
    edges = [
        FrozenEdge(path=path, from_sha256=from_digest, to_sha256=to_digest)
        for (path, from_digest, to_digest) in sorted(collector.edges)
    ]
    registered = {path: set(digests) for path, digests in collector.registered.items()}
    for path, digests in collector.known.items():
        registered.setdefault(path, set()).update(digests)
    receipts = {path: set(sources) for path, sources in collector.receipts.items()}
    for (path, _from, _to), sources in collector.edges.items():
        receipts.setdefault(path, set()).update(sources)
    return edges, registered, receipts


def _diff_name_status(repository_root: Path, *refs: str) -> list[dict[str, str]]:
    """解析 ``git diff --name-status``；只传 before 时表示与当前工作树比较。"""

    raw = subprocess.check_output(
        ["git", "diff", "--name-status", "--find-renames", "-z", *refs],
        cwd=repository_root,
    )
    fields = [field.decode("utf-8", errors="strict") for field in raw.split(b"\0") if field]
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        status_code = fields[index]
        index += 1
        kind = status_code[0]
        if kind in {"R", "C"}:
            if index + 1 >= len(fields):
                raise UpstreamMergeError("Git rename/copy 差异记录不完整")
            old_path, path = fields[index], fields[index + 1]
            index += 2
            safe_relative_path(old_path, "changed old path")
            safe_relative_path(path, "changed path")
            changes.append({"status": kind, "path": path, "old_path": old_path})
            continue
        if index >= len(fields):
            raise UpstreamMergeError("Git 差异记录缺少路径")
        path = fields[index]
        index += 1
        safe_relative_path(path, "changed path")
        if kind not in {"A", "D", "M", "T"}:
            raise UpstreamMergeError(f"不支持的 Git 差异状态：{status_code} {path}")
        changes.append({"status": kind, "path": path, "old_path": ""})
    changes.sort(key=lambda item: item["path"])
    return changes


def _blob_digest_at(repository_root: Path, commit: str, relative: str) -> str | None:
    """提交中普通文件内容的 SHA-256；路径不存在或不是 blob 时返回 None。"""

    verify = run_git(repository_root, "rev-parse", "--verify", f"{commit}:{relative}", check=False)
    if verify.returncode != 0:
        return None
    object_id = verify.stdout.strip()
    if git_output(repository_root, "cat-file", "-t", object_id) != "blob":
        return None
    content = subprocess.run(
        ["git", "cat-file", "blob", object_id],
        cwd=repository_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if content.returncode != 0:
        return None
    return sha256_bytes(content.stdout)


def _worktree_digest(repository_root: Path, relative: str) -> str | None:
    path = repository_root / Path(*relative.split("/"))
    if path.is_symlink() or not path.is_file():
        return None
    return sha256_file(path)


def load_freeze_registry(repository_root: Path) -> dict[str, Any]:
    """读取并校验冻结台账注册表；缺失或身份漂移都 fail-close。"""

    root = assert_git_repository(repository_root)
    path = root / Path(*FREEZE_REGISTRY_RELATIVE.split("/"))
    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"冻结台账注册表不存在：{FREEZE_REGISTRY_RELATIVE}")
    document = expect_object(load_json(path, "FreezeRegistry"), "FreezeRegistry")
    if document.get("schema_version") != FREEZE_REGISTRY_SCHEMA:
        raise UpstreamMergeError("FreezeRegistry schema_version 非法")
    validate_identity(document, "FreezeRegistry")
    rules = document.get("rules")
    if not isinstance(rules, list) or not rules:
        raise UpstreamMergeError("FreezeRegistry.rules 必须是非空数组")
    seen: set[str] = set()
    for index, raw in enumerate(rules):
        rule = expect_object(raw, f"FreezeRegistry.rules[{index}]")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not SAFE_ID_RE.match(rule_id) or rule_id in seen:
            raise UpstreamMergeError(f"FreezeRegistry.rules[{index}].id 非法或重复")
        seen.add(rule_id)
        match = expect_object(rule.get("match"), f"FreezeRegistry.rules[{index}].match")
        if not any(isinstance(match.get(key), list) and match[key] for key in ("prefixes", "paths", "receipt_paths")):
            raise UpstreamMergeError(f"FreezeRegistry.rules[{index}].match 没有任何匹配条件")
        action = expect_object(rule.get("action"), f"FreezeRegistry.rules[{index}].action")
        if action.get("kind") not in REGISTRY_ACTION_KINDS:
            raise UpstreamMergeError(f"FreezeRegistry.rules[{index}].action.kind 非法")
        if not isinstance(action.get("instruction"), str) or not action["instruction"].strip():
            raise UpstreamMergeError(f"FreezeRegistry.rules[{index}].action.instruction 不能为空")
        verification = rule.get("verification")
        if not isinstance(verification, list) or not all(isinstance(item, str) and item for item in verification):
            raise UpstreamMergeError(f"FreezeRegistry.rules[{index}].verification 非法")
    return document


def _receipt_path_index(repository_root: Path, receipts_by_path: dict[str, set[str]]) -> dict[str, set[str]]:
    """反查：登记收据 → 它登记过的路径集合。"""

    index: dict[str, set[str]] = {}
    for path, receipts in receipts_by_path.items():
        for receipt in receipts:
            index.setdefault(receipt, set()).add(path)
    return index


def _rule_matches(rule: dict[str, Any], path: str, receipt_index: dict[str, set[str]]) -> bool:
    match = rule["match"]
    for suffix in match.get("exclude_suffixes", []) or []:
        if isinstance(suffix, str) and path.endswith(suffix):
            return False
    for prefix in match.get("exclude_prefixes", []) or []:
        if isinstance(prefix, str) and path.startswith(prefix):
            return False
    if any(isinstance(prefix, str) and path.startswith(prefix) for prefix in match.get("prefixes", []) or []):
        return True
    if path in (match.get("paths") or []):
        return True
    for receipt in match.get("receipt_paths", []) or []:
        if isinstance(receipt, str) and path in receipt_index.get(receipt, set()):
            return True
    return False


def plan_freeze_successor(
    repository_root: Path,
    before_commit: str,
    after_commit: str | None = None,
    *,
    extra_worktree_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """计算 before..after（或 before..工作树）中命中冻结覆盖的路径及其精确边。

    ``extra_worktree_paths`` 只在 commit 模式使用：把工作树中已定稿、但要与收据同一
    提交落地的文件（典型是引用本收据的 Python 门禁文件）以“before 提交摘要 → 当前
    工作树摘要”追加进来，避免为它再开一份收据。
    """

    root = assert_git_repository(repository_root)
    extras = [safe_relative_path(item, "extra worktree path") for item in extra_worktree_paths]
    if extras and after_commit is None:
        raise UpstreamMergeError("--extra-worktree-path 只能与 --after 一起使用")
    before = expect_git_object(before_commit, "freeze before commit")
    if git_output(root, "cat-file", "-t", before) != "commit":
        raise UpstreamMergeError("freeze before 必须是 commit")
    after: str | None = None
    if after_commit is not None:
        after = expect_git_object(after_commit, "freeze after commit")
        if git_output(root, "cat-file", "-t", after) != "commit":
            raise UpstreamMergeError("freeze after 必须是 commit")
        if before == after:
            raise UpstreamMergeError("freeze before/after 不得相同")
        ancestry = run_git(root, "merge-base", "--is-ancestor", before, after, check=False)
        if ancestry.returncode != 0:
            raise UpstreamMergeError("freeze after commit 必须是 before 的后继")
        changes = _diff_name_status(root, before, after)
        committed = {change["path"] for change in changes}
        for extra in extras:
            if extra in committed:
                raise UpstreamMergeError(f"追加的工作树路径已在 before..after 中变化，不得重复登记：{extra}")
            changes.append({"status": "M", "path": extra, "old_path": ""})
        changes.sort(key=lambda item: item["path"])
    else:
        changes = _diff_name_status(root, before)
    edges, known, receipts_by_path = load_frozen_edges(root)
    registry = load_freeze_registry(root)
    receipt_index = _receipt_path_index(root, receipts_by_path)

    hits: list[dict[str, Any]] = []
    unregistered: list[str] = []
    broken: list[dict[str, Any]] = []
    manual_actions: list[dict[str, Any]] = []
    deleted_frozen: list[str] = []
    deleted_paths: list[dict[str, Any]] = []

    def note_rule_actions(path: str) -> None:
        # 注册表规则描述的是“目录级摘要”等收据边之外的冻结，必须对每个变化路径
        # 判定，而不只对已登记收据边的路径判定；否则改一个目录内的新文件会漏报。
        for rule in registry["rules"]:
            if _rule_matches(rule, path, receipt_index):
                manual_actions.append(
                    {
                        "rule_id": rule["id"],
                        "path": path,
                        "action_kind": rule["action"]["kind"],
                        "file": rule["action"].get("file"),
                        "instruction": rule["action"]["instruction"],
                        "verification": list(rule["verification"]),
                    }
                )

    for change in changes:
        path = change["path"]
        old_path = change["old_path"] or path
        note_rule_actions(path)
        frozen_paths = [candidate for candidate in {path, old_path} if candidate in known]
        if change["status"] == "D":
            # 通用 successor 图只能表达“摘要到摘要”；删除路径不产生边，只登记
            # 删除事实，冻结路径的删除还必须由 deletion_proof 携带证明。
            deleted_paths.append(
                {
                    "path": path,
                    "frozen": bool(frozen_paths),
                    "last_sha256": _blob_digest_at(root, before, old_path),
                }
            )
            if frozen_paths:
                deleted_frozen.append(path)
            else:
                unregistered.append(path)
            continue
        if not frozen_paths:
            unregistered.append(path)
            continue
        before_digest = _blob_digest_at(root, before, old_path)
        if after is not None and path not in extras:
            after_digest = _blob_digest_at(root, after, path)
        else:
            after_digest = _worktree_digest(root, path)
        if after_digest is None:
            raise UpstreamMergeError(f"无法读取冻结路径的当前内容：{path}")
        if before_digest is None or before_digest not in known.get(old_path, set()):
            broken.append(
                {
                    "path": path,
                    "old_path": change["old_path"],
                    "before_sha256": before_digest,
                    "registered_sha256_count": len(known.get(old_path, set())),
                }
            )
            continue
        if before_digest == after_digest:
            continue
        source_receipts = sorted(receipts_by_path.get(old_path, set()) | receipts_by_path.get(path, set()))
        hits.append(
            {
                "path": path,
                "old_path": change["old_path"],
                "status": change["status"],
                "predecessor_sha256s": [before_digest],
                "to_sha256": after_digest,
                "source_receipts": source_receipts,
            }
        )
    hits.sort(key=lambda item: item["path"])
    manual_actions.sort(key=lambda item: (item["rule_id"], item["path"]))
    return {
        "mode": "commit" if after is not None else "worktree",
        "before_commit": before,
        "after_commit": after,
        "extra_worktree_paths": extras,
        "frozen_path_count": len(known),
        "frozen_edge_count": len(edges),
        "changed_path_count": len(changes),
        "frozen_hits": hits,
        "unregistered_paths": sorted(unregistered),
        "deleted_frozen_paths": sorted(deleted_frozen),
        "deleted_paths": sorted(deleted_paths, key=lambda item: item["path"]),
        "broken_chain": broken,
        "required_manual_actions": manual_actions,
        "registry_rule_count": len(registry["rules"]),
    }


def _assert_no_self_binding(
    repository_root: Path,
    plan: dict[str, Any],
    output_relative: str | None,
) -> None:
    """successor 不得进入其描述的提交，也不得引用本区间内变化的收据。"""

    changed = {item["path"] for item in plan["frozen_hits"]} | set(plan["unregistered_paths"]) | set(plan["deleted_frozen_paths"])
    changed |= {item["path"] for item in plan["broken_chain"]}
    referenced: set[str] = set()
    for hit in plan["frozen_hits"]:
        referenced.update(hit["source_receipts"])
    overlap = sorted(referenced & changed)
    if overlap:
        raise UpstreamMergeError(
            "冻结 successor 引用的收据在本区间内发生变化，存在自引用风险；"
            "请先封存源码提交，再单独提交收据：" + ", ".join(overlap)
        )
    if output_relative is None:
        return
    if plan["after_commit"] is not None:
        probe = run_git(
            repository_root,
            "rev-parse",
            "--verify",
            f"{plan['after_commit']}:{output_relative}",
            check=False,
        )
        if probe.returncode == 0:
            raise UpstreamMergeError(f"输出收据已存在于 after 提交树中，违反“先源码后收据”顺序：{output_relative}")
    if output_relative in changed:
        raise UpstreamMergeError(f"输出收据不得出现在它描述的变化集合中：{output_relative}")


def _git_grep_references(
    repository_root: Path,
    after_commit: str | None,
    patterns: Sequence[str],
    pathspecs: Sequence[str],
    *,
    word_boundary: bool,
) -> list[dict[str, Any]]:
    """用 ``git grep`` 在 after 提交树（或工作树含未跟踪文件）内查找字面模式。"""

    command = ["git", "grep", "-n", "-I", "-F"]
    if word_boundary:
        command.append("-w")
    for pattern in patterns:
        command.extend(["-e", pattern])
    if after_commit is not None:
        command.append(after_commit)
    else:
        command.append("--untracked")
    command.append("--")
    command.extend(pathspecs)
    command.append(f":(exclude){MAINTENANCE_ROOT}/")
    completed = subprocess.run(
        command,
        cwd=repository_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise UpstreamMergeError(
            "引用扫描 git grep 失败：" + completed.stderr.decode("utf-8", errors="replace").strip()
        )
    hits: list[dict[str, Any]] = []
    for raw_line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        line = raw_line
        if after_commit is not None and line.startswith(after_commit + ":"):
            line = line[len(after_commit) + 1 :]
        path, _separator, remainder = line.partition(":")
        line_number, _separator, content = remainder.partition(":")
        if not path or not line_number.isdigit():
            continue
        hits.append({"path": path, "line": int(line_number), "content": content.strip()[:200]})
    return hits


def _read_repository_text(repository_root: Path, after_commit: str | None, relative: str) -> str | None:
    if after_commit is not None:
        completed = subprocess.run(
            ["git", "show", f"{after_commit}:{relative}"],
            cwd=repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            return None
        raw = completed.stdout
    else:
        path = repository_root / Path(*relative.split("/"))
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _json_command_references(value: Any, patterns: Sequence[str], *, key: str | None = None) -> bool:
    """递归查找 JSON 动作命令字段（command／argv 等）中的字面引用。"""

    if isinstance(value, dict):
        return any(_json_command_references(child, patterns, key=str(name)) for name, child in value.items())
    if isinstance(value, list):
        return any(_json_command_references(child, patterns, key=key) for child in value)
    if isinstance(value, str) and key in REFERENCE_SCAN_JSON_COMMAND_KEYS:
        return any(pattern in value for pattern in patterns)
    return False


def scan_deleted_path_references(
    repository_root: Path,
    after_commit: str | None,
    deleted_path: str,
    *,
    ignored_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """为一个删除路径生成 ``reference-scan/v1`` 结果：三类引用全部为空才算无引用。"""

    root = assert_git_repository(repository_root)
    name = deleted_path.rsplit("/", 1)[-1]
    stem, _dot, suffix = name.rpartition(".")
    patterns = [name]
    python_patterns = [name]
    if suffix == "py" and stem:
        # 模块名用于 import x／from pkg import x／pkg.x 属性引用。
        python_patterns.append(stem)
    ignored = set(ignored_paths) | {deleted_path}
    references: list[dict[str, Any]] = []
    for hit in _git_grep_references(
        root, after_commit, python_patterns, REFERENCE_SCAN_PYTHON_PATHSPECS, word_boundary=True
    ):
        if hit["path"] not in ignored:
            references.append({"kind": "python", **hit})
    for hit in _git_grep_references(
        root, after_commit, patterns, REFERENCE_SCAN_SHELL_PATHSPECS, word_boundary=False
    ):
        if hit["path"] not in ignored:
            references.append({"kind": "shell", **hit})
    json_candidates = sorted(
        {
            hit["path"]
            for hit in _git_grep_references(
                root, after_commit, patterns, REFERENCE_SCAN_JSON_PATHSPECS, word_boundary=False
            )
            if hit["path"] not in ignored
        }
    )
    for candidate in json_candidates:
        text = _read_repository_text(root, after_commit, candidate)
        try:
            document = json.loads(text) if text is not None else None
        except ValueError:
            document = None
        if document is None or _json_command_references(document, patterns):
            references.append({"kind": "json", "path": candidate, "line": 0, "content": "command 字段引用"})
    references.sort(key=lambda item: (item["kind"], item["path"], item["line"]))
    return {
        "algorithm": REFERENCE_SCAN_ALGORITHM,
        "patterns": sorted(set(patterns) | set(python_patterns)),
        "scopes": ["python:import-and-attribute", "shell:invocation", "json:command-fields"],
        "references": references,
    }


def build_deletion_proof(
    repository_root: Path,
    plan: dict[str, Any],
    *,
    deletion_reason: str,
    historical_readers: Sequence[str],
) -> dict[str, Any]:
    """为 plan 中全部删除路径生成删除证明；任一引用残留或读取器缺失即拒绝。"""

    root = assert_git_repository(repository_root)
    if not isinstance(deletion_reason, str) or not deletion_reason.strip():
        raise UpstreamMergeError("删除冻结路径必须提供非空 --deletion-reason")
    deleted = plan.get("deleted_paths") or []
    if not deleted:
        raise UpstreamMergeError("区间内没有删除路径，无需删除证明")
    after = plan.get("after_commit")
    deleted_names = [item["path"] for item in deleted]
    entries: list[dict[str, Any]] = []
    for item in deleted:
        scan = scan_deleted_path_references(root, after, item["path"], ignored_paths=deleted_names)
        if scan["references"]:
            listed = "; ".join(
                f"{ref['kind']}:{ref['path']}:{ref['line']}" for ref in scan["references"][:8]
            )
            raise UpstreamMergeError(f"删除路径仍被引用，拒绝生成删除证明：{item['path']} ← {listed}")
        entries.append({**item, "reference_scan": scan})
    readers: list[dict[str, str]] = []
    for reader in historical_readers:
        relative = safe_relative_path(reader, "historical reader")
        if not relative.endswith(".py") or relative in deleted_names:
            raise UpstreamMergeError(f"历史读取器必须是仍存在的 Python 模块：{relative}")
        if after is not None:
            digest = _blob_digest_at(root, after, relative)
        else:
            digest = _worktree_digest(root, relative)
        if digest is None:
            raise UpstreamMergeError(f"历史读取器不存在或不是普通文件：{relative}")
        readers.append({"path": relative, "sha256": digest})
    readers.sort(key=lambda item: item["path"])
    if any(item["path"].endswith(".py") for item in deleted) and not readers:
        raise UpstreamMergeError("删除 Python 模块必须以 --historical-reader 登记承接历史读取的模块")
    return {
        "algorithm": DELETION_PROOF_ALGORITHM,
        "reason": deletion_reason.strip(),
        "deleted_paths": entries,
        "historical_readers": readers,
    }


def generate_freeze_successor(
    repository_root: Path,
    before_commit: str,
    after_commit: str | None,
    output_path: Path | None,
    *,
    tag: str,
    reason: str | None = None,
    dry_run: bool = False,
    extra_worktree_paths: Sequence[str] = (),
    deletion_reason: str | None = None,
    historical_readers: Sequence[str] = (),
) -> dict[str, Any]:
    """在最终 revision 一次性生成全部冻结台账的 successor 收据。

    区间内删除了冻结路径时必须给出 ``deletion_reason``（以及 Python 模块的
    ``historical_readers``），收据 result 为 ``passed_with_deletions`` 并携带
    ``deletion_proof``；没有证明的冻结删除保持 fail-close，不再降级为人工待办。
    """

    root = assert_git_repository(repository_root)
    if not isinstance(tag, str) or not SAFE_ID_RE.match(tag):
        raise UpstreamMergeError("freeze tag 必须是安全标识")
    plan = plan_freeze_successor(root, before_commit, after_commit, extra_worktree_paths=extra_worktree_paths)
    output_relative: str | None = None
    if output_path is not None:
        if not output_path.is_absolute():
            raise UpstreamMergeError("freeze successor 输出必须是绝对路径")
        resolved_output = Path(output_path.parent.resolve(strict=False) / output_path.name)
        try:
            output_relative = resolved_output.relative_to(root.resolve()).as_posix()
        except ValueError:
            output_relative = None
        if output_relative is not None and not output_relative.startswith(MAINTENANCE_ROOT + "/"):
            raise UpstreamMergeError(f"仓库内的 freeze successor 只能写入 {MAINTENANCE_ROOT}/")
    elif not dry_run:
        raise UpstreamMergeError("非 dry-run 模式必须指定 --output")

    if plan["broken_chain"]:
        listed = ", ".join(item["path"] for item in plan["broken_chain"])
        raise UpstreamMergeError(
            "冻结路径的前序摘要不在任何已登记收据中，链断裂，禁止凭空补边：" + listed
        )
    _assert_no_self_binding(root, plan, output_relative)

    default_reason = reason or (
        f"按上游合并流程规则在最终 revision 一次性登记冻结台账的精确后继摘要（{tag}）；"
        "旧收据保持只读，不改变官方客户端画像、Persona、wire 或生产代码。"
    )
    transitions = [
        {
            "path": hit["path"],
            "old_path": hit["old_path"],
            "status": hit["status"],
            "predecessor_sha256s": hit["predecessor_sha256s"],
            "to_sha256": hit["to_sha256"],
            "source_receipts": hit["source_receipts"],
            "reason": f"{default_reason} path={hit['path']}",
        }
        for hit in plan["frozen_hits"]
    ]
    verification = list(FREEZE_VERIFICATION)
    for action in plan["required_manual_actions"]:
        for command in action["verification"]:
            if command not in verification:
                verification.append(command)
    deletion_proof: dict[str, Any] | None = None
    if plan["deleted_paths"] and deletion_reason is not None:
        deletion_proof = build_deletion_proof(
            root, plan, deletion_reason=deletion_reason, historical_readers=historical_readers
        )
    elif plan["deleted_frozen_paths"]:
        raise UpstreamMergeError(
            "区间内删除了冻结路径，必须提供 --deletion-reason（及 --historical-reader）生成删除证明："
            + ", ".join(plan["deleted_frozen_paths"])
        )
    if plan["required_manual_actions"]:
        result = FREEZE_RESULT_MANUAL
    elif plan["deleted_frozen_paths"]:
        result = FREEZE_RESULT_PASSED_WITH_DELETIONS
    else:
        result = FREEZE_RESULT_PASSED
    document = bind_gate_compatible_identity(
        {
            "schema_version": FREEZE_SUCCESSOR_SCHEMA,
            "issued_at_utc": time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime()),
            "base_commit": plan["before_commit"],
            "current_commit": plan["after_commit"],
            "scope": f"upstream-{tag}-freeze-successor",
            "mode": plan["mode"],
            "extra_worktree_paths": plan["extra_worktree_paths"],
            "frozen_path_count": plan["frozen_path_count"],
            "frozen_edge_count": plan["frozen_edge_count"],
            "changed_path_count": plan["changed_path_count"],
            "transitions": transitions,
            "unregistered_path_count": len(plan["unregistered_paths"]),
            "unregistered_paths": plan["unregistered_paths"],
            "deleted_frozen_paths": plan["deleted_frozen_paths"],
            "required_manual_actions": plan["required_manual_actions"],
            "verification": verification,
            "safety": {
                "live_account_used": False,
                "official_egress_profile_changed": False,
                "production_config_changed": False,
                "wire_or_persona_selection_changed": False,
                "deployment_performed": False,
            },
            "result": result,
            **({"deletion_proof": deletion_proof} if deletion_proof is not None else {}),
        }
    )
    if dry_run:
        return {"dry_run": True, "output": None, **document}
    if not transitions and not plan["required_manual_actions"] and not plan["deleted_frozen_paths"]:
        raise UpstreamMergeError("区间内没有命中冻结覆盖的路径变化，也没有注册表待办，无需生成 successor")
    assert output_path is not None
    write_once(output_path, pretty_bytes(document), mode=0o644)
    return {
        "result": result,
        "output": str(output_path),
        "scope": document["scope"],
        "identity_sha256": document["identity_sha256"],
        "transition_count": len(transitions),
        "unregistered_path_count": len(plan["unregistered_paths"]),
        "deleted_frozen_path_count": len(plan["deleted_frozen_paths"]),
        "deleted_path_count": len(plan["deleted_paths"]),
        "deletion_proof": deletion_proof is not None,
        "manual_action_count": len(plan["required_manual_actions"]),
        "verification": verification,
    }
