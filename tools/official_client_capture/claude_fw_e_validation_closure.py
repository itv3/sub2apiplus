#!/usr/bin/env python3
"""把 FW-E 已逐项审阅但证据不足的项封装为禁止生产的 validation-only 候选。

本工具只闭合“每个发现项接下来如何处置”的分类账，不证明目标 wire，也不签发任何批准。
目标 bundle 的精确 AST 调用只能证明静态调用存在；2.1.88 与 HitCC 资料只能保留为待补证候选。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)


SCHEMA_PRIOR = "claude-code-fw-e-cross-source-dispositions/v1"
SCHEMA_OUTPUT = "claude-code-fw-e-cross-source-dispositions/v2"
SCHEMA_SOURCE_TO_SINK = "claude-code-fw-e-validation-source-to-sink/v1"
SCHEMA_DOCUMENT_ATOMS = "claude-code-fw-e-hitcc-document-atoms/v1"
SCHEMA_CANDIDATES = "claude-code-fw-e-validation-candidates/v1"
SCHEMA_REVIEW = "claude-code-fw-e-validation-closure-review/v1"
PROVEN_NOT_TRAFFIC_SINK_ID = "TN-SINK-6f38bd6ba928e70c-1"
PROVEN_NOT_TRAFFIC_CALL = "r.currentTurn.files.get(l)"
MARKDOWN_LIST_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(.+?)\s*$")
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
SOURCE_LOCATION_RE = re.compile(r"^(.*?):\d+(?:-\d+)?$")


class ValidationClosureError(RuntimeError):
    """表示输入分母、证据绑定或 validation-only 边界不闭合。"""


def load_json(path: Path, label: str) -> dict[str, Any]:
    """读取普通 JSON 对象并拒绝符号链接。"""

    if path.is_symlink() or not path.is_file():
        raise ValidationClosureError(f"{label} 不是可信普通文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationClosureError(f"无法读取 {label}：{path}") from error
    if not isinstance(value, dict):
        raise ValidationClosureError(f"{label} 顶层必须是对象：{path}")
    return value


def workspace_relative(workspace_root: Path, path: Path, label: str) -> str:
    """把可信普通文件转换为工作区相对路径。"""

    if path.is_symlink() or not path.is_file():
        raise ValidationClosureError(f"{label} 不是可信普通文件：{path}")
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError as error:
        raise ValidationClosureError(f"{label} 位于工作区外：{path}") from error


def future_workspace_relative(
    workspace_root: Path, path: Path, label: str
) -> str:
    """转换尚未写出的输出路径，并要求输出仍位于工作区内。"""

    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError as error:
        raise ValidationClosureError(f"{label} 位于工作区外：{path}") from error


def binding(workspace_root: Path, path: Path, label: str) -> dict[str, str]:
    """生成带摘要的工作区输入绑定。"""

    return {
        "path": workspace_relative(workspace_root, path, label),
        "sha256": sha256_file(path),
    }


def write_private_json(path: Path, value: Any) -> None:
    """以规范 JSON 写入仅当前用户可读的文件。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(canonical_json_bytes(value))
    os.chmod(path, 0o600)


def indexed(rows: Any, key: str, label: str) -> dict[str, dict[str, Any]]:
    """把身份唯一的对象数组转换为索引。"""

    if not isinstance(rows, list):
        raise ValidationClosureError(f"{label} 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationClosureError(f"{label} 条目必须是对象")
        identity = row.get(key)
        if not isinstance(identity, str) or not identity or identity in result:
            raise ValidationClosureError(f"{label} 身份缺失或重复：{identity}")
        result[identity] = row
    return result


def stable_hash(value: str, length: int = 12) -> str:
    """生成用于稳定身份的短摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length].upper()


def target_candidate_id(sink_id: str) -> str:
    """从目标 sink 身份派生稳定且合法的 SPEC 身份。"""

    suffix = sink_id.removeprefix("TN-SINK-").upper()
    return f"SPEC-VAL-TSINK-{suffix}"


def source_candidate_id(source_id: str) -> str:
    """从 2.1.88 原子命题身份派生稳定 SPEC 身份。"""

    suffix = source_id.removeprefix("SRC2188-").upper()
    return f"SPEC-VAL-SRC2188-{suffix}"


def clue_candidate_id(clue_id: str) -> str:
    """从 HitCC clue 身份派生稳定 SPEC 身份。"""

    suffix = clue_id.removeprefix("HITCC-").upper()
    return f"SPEC-VAL-HITCC-{suffix}"


def document_candidate_id(path: str, line: int, text: str) -> str:
    """从文档路径、行号和原文派生稳定 SPEC 身份。"""

    return (
        f"SPEC-VAL-HDOC-{stable_hash(path)}-L{line}-"
        f"{stable_hash(text)}"
    )


def document_atom_id(path: str, line: int, text: str) -> str:
    """生成独立于 SPEC 的文档原子身份。"""

    return f"HDOC-{stable_hash(path)}-L{line}-{stable_hash(text)}"


def parse_markdown_list_atoms(path: Path, relative_path: str) -> list[dict[str, Any]]:
    """无截断提取 Markdown 非代码区的每个列表项及其续行。"""

    if path.is_symlink() or not path.is_file():
        raise ValidationClosureError(f"HitCC 文档不是可信普通文件：{path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    atoms: list[dict[str, Any]] = []
    headings: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            index += 1
            continue
        if in_fence:
            index += 1
            continue
        heading = MARKDOWN_HEADING_RE.match(raw)
        if heading:
            level = len(heading.group(1))
            headings = headings[: level - 1]
            headings.append(heading.group(2).strip())
            index += 1
            continue
        match = MARKDOWN_LIST_RE.match(raw)
        if not match:
            index += 1
            continue
        start = index + 1
        parts = [match.group(1).strip()]
        end = start
        lookahead = index + 1
        while lookahead < len(lines):
            continuation = lines[lookahead]
            if not continuation.strip():
                break
            if MARKDOWN_HEADING_RE.match(continuation) or MARKDOWN_LIST_RE.match(
                continuation
            ):
                break
            if continuation.lstrip().startswith(("```", "~~~")):
                break
            if continuation.startswith((" ", "\t")):
                parts.append(continuation.strip())
                end = lookahead + 1
                lookahead += 1
                continue
            break
        text = " ".join(part for part in parts if part).strip()
        if text:
            atom_id = document_atom_id(relative_path, start, text)
            atoms.append(
                {
                    "atom_id": atom_id,
                    "path": relative_path,
                    "line_start": start,
                    "line_end": end,
                    "heading": " / ".join(headings),
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
        index = max(index + 1, lookahead)
    identities = [row["atom_id"] for row in atoms]
    if len(identities) != len(set(identities)):
        raise ValidationClosureError(f"HitCC 文档原子身份重复：{relative_path}")
    return atoms


def add_candidate(
    candidates: dict[str, dict[str, Any]], candidate: dict[str, Any]
) -> None:
    """加入候选并拒绝任何身份复用或字段漂移。"""

    identity = candidate.get("id")
    if not isinstance(identity, str) or not identity:
        raise ValidationClosureError("candidate rule 缺少身份")
    previous = candidates.get(identity)
    if previous is not None:
        if previous != candidate:
            raise ValidationClosureError(f"candidate rule 定义冲突：{identity}")
        return
    candidates[identity] = candidate


def build_validation_closure(
    *,
    workspace_root: Path,
    prior_dispositions_path: Path,
    target_inventory_path: Path,
    sink_containment_path: Path,
    source_2188_path: Path,
    source_2188_root: Path,
    hitcc_path: Path,
    capture_index_path: Path,
    output_root: Path,
    producer_path: Path = Path(__file__),
) -> dict[str, Any]:
    """生成 v2 dispositions 与全部可复算的 validation-only 证据。"""

    if output_root.exists():
        raise ValidationClosureError("output-root 必须不存在，禁止覆盖")
    try:
        output_root.resolve().relative_to(workspace_root.resolve())
    except ValueError as error:
        raise ValidationClosureError("output-root 必须位于工作区内") from error

    prior = load_json(prior_dispositions_path, "上一版 dispositions")
    target_inventory = load_json(target_inventory_path, "目标 sink inventory")
    containment = load_json(sink_containment_path, "sink containment")
    source = load_json(source_2188_path, "2.1.88 coverage")
    hitcc = load_json(hitcc_path, "HitCC coverage")
    capture = load_json(capture_index_path, "capture index")
    target_version = prior.get("target_version")
    if prior.get("schema_version") != SCHEMA_PRIOR:
        raise ValidationClosureError("上一版 dispositions 必须是 v1 显式处置结果")
    if not isinstance(target_version, str) or not target_version:
        raise ValidationClosureError("上一版 dispositions 缺少目标版本")
    versions = {
        target_version,
        target_inventory.get("target_version"),
        containment.get("target_version"),
        capture.get("target_version"),
    }
    if versions != {target_version}:
        raise ValidationClosureError(f"目标版本绑定不一致：{sorted(str(v) for v in versions)}")
    if capture.get("result") != "passed":
        raise ValidationClosureError("capture index 尚未通过")
    completeness = containment.get("completeness")
    if (
        containment.get("schema_version")
        != "claude-code-fw-e-sink-containment-evidence/v1"
        or not isinstance(completeness, dict)
        or completeness.get("result") != "passed"
        or completeness.get("unmatched_sink_ids") != []
    ):
        raise ValidationClosureError("sink containment 未形成无遗漏闭集")

    target_rows = indexed(target_inventory.get("sinks"), "sink_id", "target inventory")
    containment_rows = indexed(containment.get("evidence"), "sink_id", "containment evidence")
    if set(target_rows) != set(containment_rows):
        raise ValidationClosureError("target inventory 与 containment 身份集合不一致")
    prior_target = indexed(prior.get("target_sinks"), "sink_id", "prior target_sinks")
    if set(prior_target) != set(target_rows):
        raise ValidationClosureError("上一版 target dispositions 与目标分母不一致")

    source_rows = indexed(source.get("rules"), "source_rule_id", "2.1.88 rules")
    prior_source = indexed(
        prior.get("historical_source_candidates"),
        "source_rule_id",
        "prior historical_source_candidates",
    )
    if set(prior_source) != set(source_rows):
        raise ValidationClosureError("上一版 2.1.88 dispositions 与分母不一致")
    clue_rows = indexed(hitcc.get("clues"), "clue_id", "HitCC clues")
    prior_clues = indexed(prior.get("hitcc_clues"), "clue_id", "prior hitcc_clues")
    if set(prior_clues) != set(clue_rows):
        raise ValidationClosureError("上一版 HitCC clue dispositions 与分母不一致")
    all_documents = indexed(
        hitcc.get("document_inventory"), "path", "HitCC document inventory"
    )
    expected_documents = {
        path: row
        for path, row in all_documents.items()
        if row.get("disposition") == "clue_source"
    }
    prior_documents = indexed(
        prior.get("hitcc_documents"), "path", "prior hitcc_documents"
    )
    if set(prior_documents) != set(expected_documents):
        raise ValidationClosureError("上一版 HitCC document dispositions 与分母不一致")

    output_root.mkdir(parents=True, mode=0o700)
    source_to_sink_path = output_root / "source-to-sink.json"
    document_atoms_path = output_root / "document-atoms.json"
    candidates_path = output_root / "candidate-rules.json"
    dispositions_path = output_root / "dispositions.json"
    review_path = output_root / "closure-review.json"
    source_to_sink_relative = future_workspace_relative(
        workspace_root, source_to_sink_path, "source-to-sink 输出"
    )
    document_atoms_relative = future_workspace_relative(
        workspace_root, document_atoms_path, "document-atoms 输出"
    )
    candidates_relative = future_workspace_relative(
        workspace_root, candidates_path, "candidate-rules 输出"
    )
    target_inventory_relative = workspace_relative(
        workspace_root, target_inventory_path, "目标 sink inventory"
    )
    containment_relative = workspace_relative(
        workspace_root, sink_containment_path, "sink containment"
    )
    source_relative = workspace_relative(workspace_root, source_2188_path, "2.1.88 coverage")
    hitcc_relative = workspace_relative(workspace_root, hitcc_path, "HitCC coverage")

    candidates: dict[str, dict[str, Any]] = {}
    source_to_sink_rows: list[dict[str, Any]] = []
    proven_not_traffic_sink_ids: list[str] = []
    next_target: list[dict[str, Any]] = []
    for sink_id in sorted(prior_target):
        previous = dict(prior_target[sink_id])
        if previous.get("disposition") != "unclassified":
            next_target.append(previous)
            continue
        evidence = containment_rows[sink_id]
        if evidence.get("structural_finding") != "exact_ast_call":
            raise ValidationClosureError(
                f"未分类目标项不是精确 AST 调用，禁止自动封装：{sink_id}"
            )
        call = evidence.get("call")
        if not isinstance(call, dict) or not isinstance(call.get("excerpt"), str):
            raise ValidationClosureError(f"精确 AST 调用缺少调用原文：{sink_id}")
        relevant_literals = call.get("relevant_literals", [])
        environment_keys = call.get("environment_keys", [])
        compact_call = {
            "kind": call.get("kind"),
            "sha256": call.get("sha256"),
            "excerpt": call.get("excerpt"),
            "excerpt_truncated": call.get("excerpt_truncated"),
            "callee_tail": call.get("callee_tail"),
            "argument_shapes": call.get("argument_shapes"),
            "privacy_keys": call.get("privacy_keys"),
            "environment_key_count": (
                len(environment_keys) if isinstance(environment_keys, list) else None
            ),
            "environment_keys_sha256": canonical_sha256(environment_keys),
            "relevant_literal_count": (
                len(relevant_literals) if isinstance(relevant_literals, list) else None
            ),
            "relevant_literals_sha256": canonical_sha256(relevant_literals),
        }
        compact_evidence = {
            "sink_id": sink_id,
            "category": evidence.get("category"),
            "source_start": evidence.get("source_start"),
            "source_end": evidence.get("source_end"),
            "semantic_sha256": evidence.get("semantic_sha256"),
            "owner_symbol": target_rows[sink_id].get("owner_symbol"),
            "structural_finding": evidence.get("structural_finding"),
            "structural_reason": evidence.get("structural_reason"),
            "source_window": evidence.get("source_window"),
            "call": compact_call,
        }
        if sink_id == PROVEN_NOT_TRAFFIC_SINK_ID:
            if (
                call.get("excerpt") != PROVEN_NOT_TRAFFIC_CALL
                or call.get("callee_tail") != ["r", "currentTurn", "files", "get"]
            ):
                raise ValidationClosureError("已证明非网络调用的结构证据发生漂移")
            compact_evidence["disposition"] = "out_of_scope_proven"
            compact_evidence["proof"] = "Map 状态读取，不是 Anthropic resource 发送"
            proven_not_traffic_sink_ids.append(sink_id)
            previous.update(
                {
                    "traffic_class": "not_traffic",
                    "disposition": "out_of_scope_proven",
                    "rationale": (
                        "目标 bundle 的精确调用原文为 r.currentTurn.files.get(l)，"
                        "callee 链证明它是当前 turn 文件 Map 的状态读取，不是网络发送。"
                    ),
                    "spec_ids": [],
                    "scenario_ids": [],
                    "evidence_paths": sorted(
                        set(previous.get("evidence_paths", []))
                        | {source_to_sink_relative, containment_relative}
                    ),
                }
            )
            next_target.append(previous)
            source_to_sink_rows.append(compact_evidence)
            continue
        candidate_id = target_candidate_id(sink_id)
        compact_evidence["disposition"] = "mapped_validation"
        compact_evidence["candidate_rule_id"] = candidate_id
        add_candidate(
            candidates,
            {
                "id": candidate_id,
                "domain": "transport_candidate",
                "retained_claim": (
                    f"目标 {target_version} bundle 在 "
                    f"{evidence.get('source_start')}:{evidence.get('source_end')} 存在 "
                    f"{evidence.get('category')} 精确 AST 调用：{call['excerpt']}。"
                    "该命题只证明静态调用存在，不证明触发条件、目标 wire 或生产适用性。"
                ),
                "scope": "target-static",
                "required_channels": [],
                "validation_evidence_level": "observed",
                "evidence_paths": sorted(
                    {
                        candidates_relative,
                        source_to_sink_relative,
                        target_inventory_relative,
                        containment_relative,
                    }
                ),
                "source_ids": [sink_id],
            },
        )
        previous.update(
            {
                "traffic_class": "unknown",
                "disposition": "mapped_validation",
                "rationale": (
                    "目标 bundle 已证明该精确 AST 调用存在，但 wire、触发条件和流量类别"
                    "尚未证明；仅登记为禁止生产的 validation-only 原子候选。"
                ),
                "spec_ids": [candidate_id],
                "scenario_ids": [],
                "evidence_paths": sorted(
                    set(previous.get("evidence_paths", []))
                    | {source_to_sink_relative, containment_relative}
                ),
                "migration_decision": "add",
            }
        )
        next_target.append(previous)
        source_to_sink_rows.append(compact_evidence)

    source_to_sink = {
        "schema_version": SCHEMA_SOURCE_TO_SINK,
        "target_version": target_version,
        "input_bindings": {
            "target_inventory": binding(
                workspace_root, target_inventory_path, "目标 sink inventory"
            ),
            "sink_containment": binding(
                workspace_root, sink_containment_path, "sink containment"
            ),
        },
        "entry_count": len(source_to_sink_rows),
        "entries": sorted(source_to_sink_rows, key=lambda row: row["sink_id"]),
        "counts": dict(
            sorted(Counter(row["disposition"] for row in source_to_sink_rows).items())
        ),
        "limitations": [
            "exact_ast_call 只证明目标 bundle 中存在该调用，不证明运行触发或 wire 语义。",
            "mapped_validation 永久拒绝进入本阶段的 production SupportEnvelope。",
        ],
    }
    write_private_json(source_to_sink_path, source_to_sink)

    next_source: list[dict[str, Any]] = []
    for source_id in sorted(prior_source):
        previous = dict(prior_source[source_id])
        if previous.get("disposition") != "unclassified":
            next_source.append(previous)
            continue
        raw = source_rows[source_id]
        source_evidence = [source_relative, candidates_relative]
        for location in raw.get("source_paths", []):
            if not isinstance(location, str):
                raise ValidationClosureError(f"2.1.88 source_paths 非法：{source_id}")
            match = SOURCE_LOCATION_RE.fullmatch(location)
            if match is None:
                raise ValidationClosureError(f"2.1.88 源码坐标非法：{source_id}={location}")
            source_path = source_2188_root / match.group(1)
            source_evidence.append(
                workspace_relative(workspace_root, source_path, f"2.1.88 源码 {source_id}")
            )
        candidate_id = source_candidate_id(source_id)
        add_candidate(
            candidates,
            {
                "id": candidate_id,
                "domain": "historical_source_candidate",
                "retained_claim": (
                    f"2.1.88 历史源码命题：{raw.get('proposition')} "
                    "尚未取得目标 stable 的语义证明。"
                ),
                "scope": "historical-source-2.1.88",
                "required_channels": [],
                "validation_evidence_level": "blocked",
                "evidence_paths": sorted(set(source_evidence)),
                "source_ids": [source_id],
            },
        )
        previous.update(
            {
                "disposition": "mapped_validation",
                "spec_ids": [candidate_id],
                "rationale": (
                    "历史源码命题已唯一编号并绑定原始源码，但尚无目标 stable 语义证明；"
                    "保持 blocked、validation-only 且禁止生产。"
                ),
                "evidence_paths": sorted(set(source_evidence)),
            }
        )
        next_source.append(previous)

    next_clues: list[dict[str, Any]] = []
    unclassified_clue_candidates: dict[str, str] = {}
    for clue_id in sorted(prior_clues):
        previous = dict(prior_clues[clue_id])
        if previous.get("disposition") != "unclassified":
            next_clues.append(previous)
            continue
        raw = clue_rows[clue_id]
        source_path_value = raw.get("source_path")
        if not isinstance(source_path_value, str) or not source_path_value:
            raise ValidationClosureError(f"HitCC clue 缺少来源：{clue_id}")
        source_path = workspace_root / source_path_value
        clue_evidence = [
            hitcc_relative,
            candidates_relative,
            workspace_relative(workspace_root, source_path, f"HitCC clue {clue_id}"),
        ]
        candidate_id = clue_candidate_id(clue_id)
        unclassified_clue_candidates[clue_id] = candidate_id
        add_candidate(
            candidates,
            {
                "id": candidate_id,
                "domain": "hitcc_clue_candidate",
                "retained_claim": (
                    f"HitCC 2.1.197 线索：{raw.get('proposition')} "
                    "尚未取得目标 stable 的语义证明。"
                ),
                "scope": "hitcc-clue-2.1.197",
                "required_channels": [],
                "validation_evidence_level": "blocked",
                "evidence_paths": sorted(set(clue_evidence)),
                "source_ids": [clue_id],
            },
        )
        previous.update(
            {
                "disposition": "mapped_validation",
                "spec_ids": [candidate_id],
                "rationale": (
                    "HitCC 原子线索已唯一编号并绑定原文，但目标 stable 语义仍未证明；"
                    "保持 blocked、validation-only 且禁止生产。"
                ),
                "evidence_paths": sorted(set(clue_evidence)),
            }
        )
        next_clues.append(previous)

    document_records: list[dict[str, Any]] = []
    next_documents: list[dict[str, Any]] = []
    for document_path_value in sorted(prior_documents):
        previous = dict(prior_documents[document_path_value])
        if previous.get("disposition") != "unclassified":
            next_documents.append(previous)
            continue
        raw = expected_documents[document_path_value]
        document_path = workspace_root / document_path_value
        document_relative = workspace_relative(
            workspace_root, document_path, "HitCC clue_source 文档"
        )
        clue_ids = [
            str(item) for item in raw.get("clue_ids", []) if str(item)
        ]
        candidate_ids = sorted(
            {
                unclassified_clue_candidates[clue_id]
                for clue_id in clue_ids
                if clue_id in unclassified_clue_candidates
            }
        )
        atoms: list[dict[str, Any]] = []
        if not candidate_ids:
            if raw.get("mapping_status") != "unmapped" or clue_ids:
                raise ValidationClosureError(
                    f"HitCC 文档没有可承接的 clue 或 unmapped 原子：{document_path_value}"
                )
            atoms = parse_markdown_list_atoms(document_path, document_relative)
            if not atoms:
                raise ValidationClosureError(
                    f"HitCC unmapped 文档没有可原子化列表项：{document_path_value}"
                )
            for atom in atoms:
                candidate_id = document_candidate_id(
                    document_relative, atom["line_start"], atom["text"]
                )
                candidate_ids.append(candidate_id)
                add_candidate(
                    candidates,
                    {
                        "id": candidate_id,
                        "domain": "hitcc_document_candidate",
                        "retained_claim": (
                            f"HitCC 2.1.197 文档原子命题：{atom['text']} "
                            "尚未取得目标 stable 的语义证明。"
                        ),
                        "scope": "hitcc-document-2.1.197",
                        "required_channels": [],
                        "validation_evidence_level": "blocked",
                        "evidence_paths": sorted(
                            {
                                candidates_relative,
                                document_atoms_relative,
                                document_relative,
                            }
                        ),
                        "source_ids": sorted(
                            {atom["atom_id"], document_relative}
                        ),
                    },
                )
        for candidate_id in candidate_ids:
            candidate = candidates.get(candidate_id)
            if candidate is None:
                raise ValidationClosureError(
                    f"HitCC 文档引用未知 validation candidate：{candidate_id}"
                )
            candidate["source_ids"] = sorted(
                set(candidate["source_ids"]) | {document_relative}
            )
        document_records.append(
            {
                "path": document_relative,
                "sha256": sha256_file(document_path),
                "mapping_status": raw.get("mapping_status"),
                "clue_ids": sorted(clue_ids),
                "atom_count": len(atoms),
                "atoms": atoms,
                "candidate_rule_ids": sorted(candidate_ids),
            }
        )
        previous.update(
            {
                "disposition": "mapped_validation",
                "spec_ids": sorted(candidate_ids),
                "rationale": (
                    "文档未闭合内容已通过现有 clue 或无截断 Markdown 列表原子逐项编号；"
                    "所有候选保持 blocked、validation-only 且禁止生产。"
                ),
                "evidence_paths": sorted(
                    set(previous.get("evidence_paths", []))
                    | {
                        candidates_relative,
                        document_atoms_relative,
                        document_relative,
                    }
                ),
            }
        )
        next_documents.append(previous)

    document_atoms = {
        "schema_version": SCHEMA_DOCUMENT_ATOMS,
        "source_version": hitcc.get("source_version"),
        "target_version": target_version,
        "input_binding": binding(workspace_root, hitcc_path, "HitCC coverage"),
        "document_count": len(document_records),
        "atom_count": sum(row["atom_count"] for row in document_records),
        "documents": document_records,
        "extraction_policy": {
            "unit": "markdown_list_item_outside_fenced_code",
            "continuations": "indented_lines_until_blank_heading_fence_or_next_item",
            "truncation": "forbidden",
            "semantic_status": "historical_candidate_only",
        },
    }
    write_private_json(document_atoms_path, document_atoms)

    ordered_candidates = [candidates[key] for key in sorted(candidates)]
    candidate_rules = {
        "schema_version": SCHEMA_CANDIDATES,
        "target_version": target_version,
        "input_bindings": {
            "producer": binding(
                workspace_root, producer_path, "validation closure producer"
            ),
            "prior_dispositions": binding(
                workspace_root, prior_dispositions_path, "上一版 dispositions"
            ),
            "target_inventory": binding(
                workspace_root, target_inventory_path, "目标 sink inventory"
            ),
            "sink_containment": binding(
                workspace_root, sink_containment_path, "sink containment"
            ),
            "source_2_1_88": binding(workspace_root, source_2188_path, "2.1.88 coverage"),
            "hitcc_2_1_197": binding(workspace_root, hitcc_path, "HitCC coverage"),
            "capture_index": binding(workspace_root, capture_index_path, "capture index"),
        },
        "rule_count": len(ordered_candidates),
        "counts_by_scope": dict(
            sorted(Counter(row["scope"] for row in ordered_candidates).items())
        ),
        "counts_by_evidence_level": dict(
            sorted(
                Counter(
                    row["validation_evidence_level"] for row in ordered_candidates
                ).items()
            )
        ),
        "rules": ordered_candidates,
        "production_eligibility": "denied",
    }
    write_private_json(candidates_path, candidate_rules)

    dispositions = {
        "schema_version": SCHEMA_OUTPUT,
        "target_version": target_version,
        "target_sinks": sorted(next_target, key=lambda row: row["sink_id"]),
        "historical_source_candidates": sorted(
            next_source, key=lambda row: row["source_rule_id"]
        ),
        "hitcc_clues": sorted(next_clues, key=lambda row: row["clue_id"]),
        "hitcc_documents": sorted(next_documents, key=lambda row: row["path"]),
        "runtime_observations": prior.get("runtime_observations"),
        "candidate_rules": ordered_candidates,
    }
    if not isinstance(dispositions["runtime_observations"], list):
        raise ValidationClosureError("上一版 runtime observations 非法")
    remaining = {
        key: sum(
            row.get("disposition") == "unclassified"
            for row in dispositions[key]
        )
        for key in (
            "target_sinks",
            "historical_source_candidates",
            "hitcc_clues",
            "hitcc_documents",
            "runtime_observations",
        )
    }
    if any(remaining.values()):
        raise ValidationClosureError(f"validation closure 仍含 unclassified：{remaining}")
    write_private_json(dispositions_path, dispositions)

    prior_unclassified = {
        key: sum(row.get("disposition") == "unclassified" for row in prior[key])
        for key in (
            "target_sinks",
            "historical_source_candidates",
            "hitcc_clues",
            "hitcc_documents",
            "runtime_observations",
        )
    }
    review = {
        "schema_version": SCHEMA_REVIEW,
        "target_version": target_version,
        "input_bindings": candidate_rules["input_bindings"],
        "output_bindings": {
            "source_to_sink": binding(
                workspace_root, source_to_sink_path, "source-to-sink 输出"
            ),
            "document_atoms": binding(
                workspace_root, document_atoms_path, "document-atoms 输出"
            ),
            "candidate_rules": binding(
                workspace_root, candidates_path, "candidate-rules 输出"
            ),
            "dispositions": binding(
                workspace_root, dispositions_path, "dispositions 输出"
            ),
        },
        "prior_unclassified_counts": prior_unclassified,
        "prior_unclassified_total": sum(prior_unclassified.values()),
        "final_unclassified_counts": remaining,
        "final_unclassified_total": sum(remaining.values()),
        "candidate_rule_count": len(ordered_candidates),
        "candidate_counts_by_scope": candidate_rules["counts_by_scope"],
        "candidate_counts_by_evidence_level": candidate_rules[
            "counts_by_evidence_level"
        ],
        "proven_not_traffic_sink_ids": sorted(proven_not_traffic_sink_ids),
        "production_eligibility": "denied",
        "result": "passed",
    }
    write_private_json(review_path, review)
    return review


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    parser.add_argument("--prior-dispositions", required=True, type=Path)
    parser.add_argument("--target-inventory", required=True, type=Path)
    parser.add_argument("--sink-containment", required=True, type=Path)
    parser.add_argument("--source-2188", required=True, type=Path)
    parser.add_argument("--source-2188-root", required=True, type=Path)
    parser.add_argument("--hitcc", required=True, type=Path)
    parser.add_argument("--capture-index", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main() -> int:
    """运行 validation-only 闭合集生成。"""

    arguments = build_parser().parse_args()
    try:
        review = build_validation_closure(
            workspace_root=arguments.workspace_root,
            prior_dispositions_path=arguments.prior_dispositions,
            target_inventory_path=arguments.target_inventory,
            sink_containment_path=arguments.sink_containment,
            source_2188_path=arguments.source_2188,
            source_2188_root=arguments.source_2188_root,
            hitcc_path=arguments.hitcc,
            capture_index_path=arguments.capture_index,
            output_root=arguments.output_root,
        )
    except (ValidationClosureError, OSError, ValueError) as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1
    print(
        "FW-E validation-only 闭合集已生成："
        f"candidate_rules={review['candidate_rule_count']} "
        f"unclassified={review['final_unclassified_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
