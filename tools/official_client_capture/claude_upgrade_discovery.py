"""Claude VC-1 发现补全（Claude 升级控制面设计 B2-07 补充项）：完整发现清单、语义候选与正交候选分母。

本模块只做确定性计算（读冻结的历史候选基线、不写文件、不读 Campaign 总计划），由 evidence 语义模块
``claude_upgrade_evidence`` 在 seal 草稿阶段调用；它在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层，经 evidence 根
按符号进入 evidence 闭包，改它即是 evidence 语义变化（追加评估纪元、登记演进即可续跑，原始字节不重采）。

一、三个产物（seal 的 ``drafts/`` 下，全部绑定进完整性清单，VC-2 直接从 VC-1 读取）
==================================================================================
* **DiscoveryInventory**（``claude-code-fw-e-discovery-inventory/v1``，与 2.1.226 同一合同、同一条目字段）：
  - ``target_ast_call``：目标 SinkInventory 的每个 AST 调用 sink。containment 判为精确调用（``exact_ast_call``）的，按
    2.1.226 语义闭合同一套固定词表（``claude_fw_e_validation_closure.classify_target_candidates``）多对一归并到语义候选，
    VC-1 处置为 ``mapped_semantic_candidate``；其余结构判定禁止自动归并，处置为 ``unclassified``；
  - ``target_lexical_only``：AST 之外的保守词法命中。containment 给出非流量结构证据（与 FW-E 显式处置同一闭集
    ``claude_fw_e_dispositions.NON_TRAFFIC_FINDINGS``）的处置为 ``out_of_scope_proven``，其余为 ``unclassified``；
  - ``target_unresolved_entry``：入口关系未决的模块、动态加载边与 sink（``claude_target_inventory.unresolved_discovery_items``，
    B2-04），处置为 ``unclassified``；
  - ``historical_source_2_1_88``、``hitcc_clue_2_1_197``、``hitcc_document_atom_2_1_197``：冻结的历史候选基线逐项重放
    （第二节），条目逐字等于 2.1.226 语义闭合当时的发现项；
  - 人工显式处置（``--discovery-dispositions``）可以覆盖任一发现项的 VC-1 处置；只要还有 ``unclassified``，seal 的闭合门禁
    就是 blocked。VC-1 处置只是事实整理，不是 VC-2 的终态：VC-2 仍要给每个发现项绑定指南 §4.2.2 的终态。
* **SemanticRuleCandidate**（``claude-code-fw-e-semantic-candidates/v1``）：固定目录的 32 个稳定 ``CAND-*`` 族一律列出
  （指南 §2.10「后继 Campaign 必须重放全部稳定 ID」），``source_ids`` 由发现项的 ``semantic_candidate_ids`` 反推，两侧双向
  闭合；有目标 AST 调用归并进来的族为 ``observed``，否则 ``blocked``；不进 RuleLedger、不得生产。
* **正交候选分母**（``claude-code-fw-e-complete-candidate-denominator/v1``，与 2.1.226 完整 Campaign 同一合同、同一五组）：
  ``target_send_points``（每个目标 AST 调用 sink）、``semantic_candidate_families``（32 族）、三组历史候选
  ``source_mechanisms_2_1_88``／``hitcc_documents_2_1_197``／``historical_rules``（冻结基线逐字重放）。分母不写死，
  一律由输入计算；五组不能互相替代，也不能用聚类减少逐项处置责任。

二、历史候选基线（指南 §2.10、§4.1.1）
=====================================
2.1.88 真源码与 HitCC 2.1.197 在首次纳管时已全量提取：2.1.226 的 FW-E 语义闭合（``claude_fw_e_validation_closure``，
e577e144a）产出 7368 个发现项，其中 7037 个来自历史资料（2.1.88 原子命题 29、HitCC 线索 71、HitCC Markdown 原子 6937）；
完整 Campaign 冻结（``claude_fw_e_complete_campaign``）产出五组 593 个正交候选，其中三组历史候选 102＋71＋57。原始目录与
提取合同没有变化，后继 Campaign 只重放这份不可变基线，不重扫 2.1.88 的 ``src／node_modules／vendor``、不逐篇重提 HitCC，
历史处置也不复制成新版本结论（VC-2 逐项重新处置）。

基线冻结为受管根内的 ``claude_fw_e_historical_candidate_baseline_v1.json``（Claude 策略 evidence 层整文件计入，内容变化即
evidence 语义变化）。它由本模块的 ``freeze-baseline`` 子命令从 2.1.226 历史产物一次性生成（只读，产物逐字可复算），
带提取收据：输入产物与提取工具的摘要、2.1.88 与 HitCC 原始目录摘要、语义候选目录摘要。语义候选目录与归并词表本身取自
``claude_fw_e_validation_closure``（evidence 层整文件），加载基线时核对目录摘要与冻结时一致。

三、与 2.1.226 历史计数的关系
============================
2.1.226 的 7368／593 是两层流程的产物：FW-E 先用人工策略显式处置了 174 个目标 sink（3 个 strict、17 个 managed、6 个
record-only、148 个范围外），只把剩下的 331 个精确 AST 调用放进发现清单与「目标发送点」组。新流程按指南 §4.2.2 让每个
目标 sink 都成为发现项、由 VC-2 给终态，所以同一份 2.1.226 输入在这里得到更大的分母（目标 AST 调用 358、词法命中 147）；
限定到那 331 个时，发现项与五组候选和历史产物逐项相同（``test_claude_upgrade_discovery`` 的 2.1.226 对照）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_fw_e_dispositions as fw_e_dispositions
from tools.official_client_capture import claude_fw_e_validation_closure as fw_e_closure
from tools.official_client_capture import claude_target_inventory
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256

# ---------------------------------------------------------------------------
# 合同名、来源类别与固定文本
# ---------------------------------------------------------------------------

DISCOVERY_INVENTORY_SCHEMA = "claude-code-fw-e-discovery-inventory/v1"
SEMANTIC_CANDIDATES_SCHEMA = fw_e_closure.SCHEMA_CANDIDATES
CANDIDATE_DENOMINATOR_SCHEMA = "claude-code-fw-e-complete-candidate-denominator/v1"
CONTAINMENT_SCHEMA = "claude-code-fw-e-sink-containment-evidence/v1"
DISCOVERY_DISPOSITIONS_SCHEMA = "claude-upgrade-discovery-dispositions/v1"
BASELINE_SCHEMA = "claude-upgrade-historical-candidate-baseline/v1"
BASELINE_FILENAME = "claude_fw_e_historical_candidate_baseline_v1.json"
# 基线在受管树里的固定位置（仓库相对路径；部署树与仓库同一布局），写进各产物的绑定。
BASELINE_RELATIVE = f"tools/official_client_capture/{BASELINE_FILENAME}"
BASELINE_ID = "fw-e-semantic-closure-e577e144a"

UNCLASSIFIED = "unclassified"
MAPPED_SEMANTIC = "mapped_semantic_candidate"
OUT_OF_SCOPE = "out_of_scope_proven"
EXACT_AST_CALL = "exact_ast_call"
TARGET_AST = "target_ast_call"
TARGET_LEXICAL = "target_lexical_only"
TARGET_UNRESOLVED = claude_target_inventory.UNRESOLVED_SOURCE_KIND
TARGET_SOURCE_KINDS = frozenset({TARGET_AST, TARGET_LEXICAL, TARGET_UNRESOLVED})
HISTORICAL_SOURCE_KINDS = ("historical_source_2_1_88", "hitcc_clue_2_1_197", "hitcc_document_atom_2_1_197")
# 人工显式处置可选的终态（沿用 FW-E 交叉矩阵的处置词表，去掉 unclassified）。
EXPLICIT_DISPOSITIONS = frozenset(
    {"mapped_historical", "mapped_managed", "record_only_disabled", "mapped_validation", "out_of_scope_proven", "catalogued_context"}
)
# 发现项的条目字段（与 2.1.226 语义闭合的发现项逐字相同）。
DISCOVERY_ITEM_FIELDS = (
    "discovery_id",
    "source_kind",
    "proposition",
    "disposition",
    "semantic_candidate_ids",
    "spec_ids",
    "evidence_paths",
    "rationale",
)
# 词法命中可据以判为非流量的 containment 结构结论：与 FW-E 显式处置工具同一闭集。
NON_TRAFFIC_FINDINGS = frozenset(fw_e_dispositions.NON_TRAFFIC_FINDINGS)

TARGET_SEND_POINTS = "target_send_points"
SEMANTIC_FAMILIES = "semantic_candidate_families"
HISTORICAL_GROUPS = ("source_mechanisms_2_1_88", "hitcc_documents_2_1_197", "historical_rules")
DENOMINATOR_GROUPS = (TARGET_SEND_POINTS, SEMANTIC_FAMILIES, *HISTORICAL_GROUPS)
# 正交候选的闭合策略：与 2.1.226 完整 Campaign 冻结的同一组约束（逐字）。
CLOSURE_POLICY = {
    "each_candidate_requires_target_measurement": True,
    "allow_unmeasured_feature_boundary": False,
    "support_envelope_reduction_closes_candidate": False,
    "static_clue_can_enter_active_profile": False,
    "telemetry_absence_generates_rule": False,
    "nonessential_absence_generates_rule": False,
    "formal_rule_requires_positive_and_negative": True,
    "formal_rule_requires_r_and_m": True,
    "tls_rule_requires_p_and_m": True,
    "formal_rule_requires_independent_pair": True,
}

# 目标原生发现的固定理由（精确 AST 调用一句与 2.1.226 语义闭合逐字相同，词法非流量一句与 FW-E 显式处置逐字相同）。
TARGET_AST_RATIONALE = "只证明调用存在；通过稳定候选身份多对一归并，不生成 SPEC，也不证明触发条件、流量类别或目标 wire。"
TARGET_AST_OPEN_RATIONALE = (
    "containment 结构证据不是精确 AST 调用：禁止按固定词表自动归并，必须由人工或运行证据显式处置后才能闭合。"
)
LEXICAL_PROVEN_RATIONALE = (
    "AST containment 将该词法命中唯一判为 {finding}；它不是独立发送调用点，实际调用仍由 AST sink 分母单独覆盖。"
)
LEXICAL_OPEN_RATIONALE = (
    "词法命中没有可排除的结构证据（{finding}）：既不能证明是发送调用，也不能证明不是，必须显式处置后才能闭合。"
)


class DiscoveryCompletionError(ConfigurationError):
    """发现补全的输入不可信、不闭合，或冻结基线与语义候选目录不一致：失败关闭。"""


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise DiscoveryCompletionError(f"{label} 必须是非空字符串数组。")
    return list(value)


def catalog_sha256() -> str:
    """语义候选目录（32 个稳定 ``CAND-*`` 族的定义）的规范摘要；冻结基线记录同一摘要，加载时核对。"""

    return canonical_sha256({identity: dict(definition) for identity, definition in fw_e_closure.SEMANTIC_CANDIDATE_CATALOG.items()})


def _check_item(item: Mapping[str, Any], label: str) -> dict[str, Any]:
    """发现项条目字段闭合、类型正确（只核结构，不判语义）。"""

    if not isinstance(item, Mapping) or set(item) != set(DISCOVERY_ITEM_FIELDS):
        raise DiscoveryCompletionError(f"{label} 的字段不闭合。")
    for field in ("discovery_id", "source_kind", "proposition", "disposition", "rationale"):
        if not isinstance(item[field], str) or not item[field]:
            raise DiscoveryCompletionError(f"{label} 的 {field} 必须是非空字符串。")
    for field in ("semantic_candidate_ids", "spec_ids", "evidence_paths"):
        values = item[field]
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values) or values != sorted(set(values)):
            raise DiscoveryCompletionError(f"{label} 的 {field} 必须是排序去重的字符串数组。")
    return {field: item[field] for field in DISCOVERY_ITEM_FIELDS}


# ---------------------------------------------------------------------------
# 冻结的历史候选基线
# ---------------------------------------------------------------------------


def baseline_path() -> Path:
    return Path(__file__).with_name(BASELINE_FILENAME)


def _decode_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """基线的紧凑行 → 发现项（字段顺序见 ``item_fields``，四张表去重保存来源类别、处置、理由与证据路径）。"""

    tables = payload.get("tables")
    if not isinstance(tables, Mapping) or set(tables) != {"source_kinds", "dispositions", "rationales", "evidence_paths"}:
        raise DiscoveryCompletionError("历史候选基线的 tables 不闭合。")
    kinds, dispositions, rationales, paths = (
        _string_list(tables[name], f"基线 tables.{name}") for name in ("source_kinds", "dispositions", "rationales", "evidence_paths")
    )
    if payload.get("item_fields") != list(DISCOVERY_ITEM_FIELDS):
        raise DiscoveryCompletionError("历史候选基线的 item_fields 与发现项字段不一致。")
    rows = payload.get("items")
    if not isinstance(rows, list):
        raise DiscoveryCompletionError("历史候选基线缺少 items。")

    def pick(table: list[str], index: Any, label: str) -> str:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(table):
            raise DiscoveryCompletionError(f"历史候选基线的 {label} 索引越界：{index!r}")
        return table[index]

    items: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(DISCOVERY_ITEM_FIELDS):
            raise DiscoveryCompletionError(f"历史候选基线第 {position} 行形状非法。")
        identity, kind, proposition, disposition, candidates, specs, evidence, rationale = row
        if not isinstance(evidence, list):
            raise DiscoveryCompletionError(f"历史候选基线第 {position} 行的证据路径索引必须是数组。")
        item = {
            "discovery_id": identity,
            "source_kind": pick(kinds, kind, "source_kind"),
            "proposition": proposition,
            "disposition": pick(dispositions, disposition, "disposition"),
            "semantic_candidate_ids": candidates,
            "spec_ids": specs,
            "evidence_paths": [pick(paths, index, "evidence_path") for index in evidence],
            "rationale": pick(rationales, rationale, "rationale"),
        }
        items.append(_check_item(item, f"历史发现项 {identity!r}"))
    return items


def load_historical_baseline(path: Path | None = None) -> dict[str, Any]:
    """读取并核对冻结的历史候选基线；返回 (身份绑定, 发现项, 三组历史候选)。任何不一致都失败关闭。"""

    source = Path(path) if path is not None else baseline_path()
    if source.is_symlink() or not source.is_file():
        raise DiscoveryCompletionError(f"历史候选基线缺失或不是可信普通文件：{source}")
    raw = source.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiscoveryCompletionError("历史候选基线不是合法 JSON。") from error
    expected_fields = {
        "schema_version", "baseline_id", "catalog_sha256", "counts", "extraction_receipt", "tables", "item_fields", "items",
        "candidate_groups",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields or payload.get("schema_version") != BASELINE_SCHEMA:
        raise DiscoveryCompletionError("历史候选基线的合同名或顶层字段不符。")
    if payload["catalog_sha256"] != catalog_sha256():
        raise DiscoveryCompletionError("历史候选基线冻结时的语义候选目录与当前目录不一致：目录变化须重新冻结基线并全量重审。")
    items = _decode_items(payload)
    identities = [item["discovery_id"] for item in items]
    if identities != sorted(set(identities)):
        raise DiscoveryCompletionError("历史发现项身份必须排序且不重复。")
    catalog = set(fw_e_closure.SEMANTIC_CANDIDATE_CATALOG)
    for item in items:
        if item["source_kind"] not in HISTORICAL_SOURCE_KINDS:
            raise DiscoveryCompletionError(f"历史发现项 {item['discovery_id']} 的来源类别不是历史资料：{item['source_kind']}")
        unknown = sorted(set(item["semantic_candidate_ids"]) - catalog)
        if unknown:
            raise DiscoveryCompletionError(f"历史发现项 {item['discovery_id']} 引用了目录外的语义候选：{unknown}")
    groups = payload["candidate_groups"]
    if not isinstance(groups, Mapping) or set(groups) != set(HISTORICAL_GROUPS):
        raise DiscoveryCompletionError("历史候选基线的候选组不是固定的三组。")
    seen: set[str] = set()
    for group in HISTORICAL_GROUPS:
        rows = groups[group]
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise DiscoveryCompletionError(f"历史候选组 {group} 必须是对象数组。")
        ids = [row.get("candidate_id") for row in rows]
        if any(not isinstance(value, str) or not value for value in ids) or ids != sorted(set(ids)) or seen & set(ids):
            raise DiscoveryCompletionError(f"历史候选组 {group} 的候选身份为空、未排序、重复或跨组重复。")
        seen.update(ids)
    counts = {
        "discovery_items": len(items),
        "discovery_by_source_kind": dict(sorted(Counter(item["source_kind"] for item in items).items())),
        "candidate_groups": {group: len(groups[group]) for group in HISTORICAL_GROUPS},
    }
    if payload["counts"] != counts:
        raise DiscoveryCompletionError(f"历史候选基线声明的计数与实际不一致：声明 {payload['counts']}，实际 {counts}")
    return {
        "binding": {"baseline_id": str(payload["baseline_id"]), "path": BASELINE_RELATIVE, "sha256": _sha256(raw)},
        "items": items,
        "candidate_groups": {group: [dict(row) for row in groups[group]] for group in HISTORICAL_GROUPS},
    }


# ---------------------------------------------------------------------------
# 目标原生发现
# ---------------------------------------------------------------------------


def containment_index(
    containment: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
    inventory_sha256: str,
    target_version: str,
) -> dict[str, Mapping[str, Any]]:
    """核对 sink containment 证据与目标 SinkInventory 同源且无遗漏，返回 sink_id → 证据行。"""

    if containment.get("schema_version") != CONTAINMENT_SCHEMA or containment.get("target_version") != target_version:
        raise DiscoveryCompletionError("sink containment 证据的合同名或目标版本不符。")
    binding = containment.get("inventory")
    if not isinstance(binding, Mapping) or binding.get("sha256") != inventory_sha256:
        raise DiscoveryCompletionError("sink containment 证据不是对本次目标 SinkInventory 生成的（inventory 摘要不符）。")
    completeness = containment.get("completeness")
    if not isinstance(completeness, Mapping) or completeness.get("result") != "passed" or completeness.get("unmatched_sink_ids") != []:
        raise DiscoveryCompletionError("sink containment 证据没有形成无遗漏闭集（completeness 不是 passed 或有未匹配 sink）。")
    rows = containment.get("evidence")
    if not isinstance(rows, list):
        raise DiscoveryCompletionError("sink containment 证据缺少 evidence。")
    index: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        sink_id = row.get("sink_id") if isinstance(row, Mapping) else None
        if not isinstance(sink_id, str) or not sink_id or sink_id in index:
            raise DiscoveryCompletionError(f"sink containment 证据行的 sink_id 缺失或重复：{sink_id!r}")
        index[sink_id] = row
    sinks = inventory.get("sinks")
    if not isinstance(sinks, list):
        raise DiscoveryCompletionError("目标 SinkInventory 缺少 sinks。")
    sink_ids = {sink.get("sink_id") for sink in sinks if isinstance(sink, Mapping)}
    if len(sink_ids) != len(sinks) or sink_ids != set(index):
        raise DiscoveryCompletionError("sink containment 证据与目标 SinkInventory 的 sink 集合不一致。")
    return index


def target_discovery_items(
    *,
    target_version: str,
    inventory: Mapping[str, Any],
    inventory_path: str,
    containment: Mapping[str, Mapping[str, Any]],
    containment_path: str,
) -> list[dict[str, Any]]:
    """目标原生发现：每个 sink 一项（AST 调用与词法命中），加上入口关系未决项；见模块说明第一节。"""

    evidence_paths = sorted({inventory_path, containment_path})
    items: list[dict[str, Any]] = []
    for sink in inventory["sinks"]:
        sink_id = str(sink["sink_id"])
        row = containment[sink_id]
        kind = sink.get("source_kind")
        module = sink.get("module")
        where = f"目标 {target_version} bundle" + (f" 模块 {module}" if module else "")
        finding = str(row.get("structural_finding"))
        if kind == "ast_call":
            call = row.get("call")
            if finding == EXACT_AST_CALL and isinstance(call, Mapping) and isinstance(call.get("excerpt"), str):
                item = {
                    "source_kind": TARGET_AST,
                    "proposition": f"{where} 存在 {row.get('category')} 精确 AST 调用：{call['excerpt']}",
                    "disposition": MAPPED_SEMANTIC,
                    "semantic_candidate_ids": fw_e_closure.classify_target_candidates(dict(row)),
                    "rationale": TARGET_AST_RATIONALE,
                }
            else:
                item = {
                    "source_kind": TARGET_AST,
                    "proposition": f"{where} 存在 {sink.get('category')} AST 调用，containment 结构判定为 {finding}。",
                    "disposition": UNCLASSIFIED,
                    "semantic_candidate_ids": [],
                    "rationale": TARGET_AST_OPEN_RATIONALE,
                }
        elif kind == "lexical_only":
            proven = finding in NON_TRAFFIC_FINDINGS
            item = {
                "source_kind": TARGET_LEXICAL,
                "proposition": (
                    f"{where} 偏移 {sink.get('source_start')} 处有 {sink.get('category')} 词法命中"
                    f"（最近符号 {sink.get('owner_symbol')}）。"
                ),
                "disposition": OUT_OF_SCOPE if proven else UNCLASSIFIED,
                "semantic_candidate_ids": [],
                "rationale": (LEXICAL_PROVEN_RATIONALE if proven else LEXICAL_OPEN_RATIONALE).format(finding=finding),
            }
        else:
            raise DiscoveryCompletionError(f"目标 sink {sink_id} 的来源类别未知：{kind!r}")
        items.append({"discovery_id": sink_id, **item, "spec_ids": [], "evidence_paths": list(evidence_paths)})
    try:
        unresolved = claude_target_inventory.unresolved_discovery_items(dict(inventory), [inventory_path])
    except (claude_target_inventory.TargetInventoryError, KeyError, TypeError) as error:
        raise DiscoveryCompletionError(f"目标 SinkInventory 不能给出未决发现项：{error}") from error
    items.extend(unresolved)
    return [_check_item(item, f"目标发现项 {item.get('discovery_id')!r}") for item in items]


def apply_explicit_dispositions(
    items: dict[str, dict[str, Any]],
    dispositions: Mapping[str, Any] | None,
    *,
    dispositions_path: str | None,
    target_version: str,
) -> None:
    """人工显式处置（``--discovery-dispositions``）：逐项覆盖 VC-1 处置，理由不能为空，证据路径并入处置文件。"""

    if dispositions is None:
        return
    if (
        set(dispositions) != {"schema_version", "target_version", "items"}
        or dispositions.get("schema_version") != DISCOVERY_DISPOSITIONS_SCHEMA
        or dispositions.get("target_version") != target_version
        or not isinstance(dispositions.get("items"), list)
    ):
        raise DiscoveryCompletionError("发现项显式处置文件的合同名、版本或 items 非法。")
    seen: set[str] = set()
    for raw in dispositions["items"]:
        if not isinstance(raw, Mapping) or set(raw) != {"discovery_id", "disposition", "rationale"}:
            raise DiscoveryCompletionError("发现项显式处置条目字段不闭合。")
        discovery_id = str(raw["discovery_id"])
        if discovery_id in seen or discovery_id not in items:
            raise DiscoveryCompletionError(f"显式处置指向重复或不存在的发现项：{discovery_id}")
        if raw["disposition"] not in EXPLICIT_DISPOSITIONS or not isinstance(raw["rationale"], str) or not raw["rationale"].strip():
            raise DiscoveryCompletionError(f"发现项 {discovery_id} 的显式处置非法（处置须是终态、理由不能为空）。")
        seen.add(discovery_id)
        items[discovery_id] = {
            **items[discovery_id],
            "disposition": raw["disposition"],
            "rationale": raw["rationale"],
            "evidence_paths": sorted({*items[discovery_id]["evidence_paths"], str(dispositions_path)}),
        }


# ---------------------------------------------------------------------------
# 三个产物
# ---------------------------------------------------------------------------


def discovery_inventory_document(items: Sequence[Mapping[str, Any]], *, target_version: str) -> dict[str, Any]:
    ordered = sorted((dict(item) for item in items), key=lambda item: item["discovery_id"])
    return {
        "schema_version": DISCOVERY_INVENTORY_SCHEMA,
        "target_version": target_version,
        "item_count": len(ordered),
        "counts_by_source_kind": dict(sorted(Counter(str(item["source_kind"]) for item in ordered).items())),
        "counts_by_disposition": dict(sorted(Counter(str(item["disposition"]) for item in ordered).items())),
        "items": ordered,
        "rule_generation": "forbidden",
    }


def semantic_candidates_document(
    items: Sequence[Mapping[str, Any]],
    *,
    target_version: str,
    baseline: Mapping[str, Any],
    target_inputs: Mapping[str, str],
) -> dict[str, Any]:
    """32 个稳定语义候选族：``source_ids`` 由发现项反推（双向闭合），目标 AST 调用归并进来即 observed。"""

    catalog = fw_e_closure.SEMANTIC_CANDIDATE_CATALOG
    sources: dict[str, set[str]] = {identity: set() for identity in catalog}
    evidence: dict[str, set[str]] = {identity: set() for identity in catalog}
    observed: set[str] = set()
    for item in items:
        for identity in item["semantic_candidate_ids"]:
            if identity not in catalog:
                raise DiscoveryCompletionError(f"发现项 {item['discovery_id']} 引用了目录外的语义候选：{identity}")
            sources[identity].add(str(item["discovery_id"]))
            evidence[identity].update(item["evidence_paths"])
            if item["source_kind"] == TARGET_AST:
                observed.add(identity)
    candidates = [
        {
            "id": identity,
            **dict(catalog[identity]),
            "evidence_level": "observed" if identity in observed else "blocked",
            "evidence_paths": sorted(evidence[identity]),
            "source_ids": sorted(sources[identity]),
        }
        for identity in sorted(catalog)
    ]
    return {
        "schema_version": SEMANTIC_CANDIDATES_SCHEMA,
        "target_version": target_version,
        "input_bindings": {"historical_baseline": dict(baseline["binding"]), **{name: {"path": path} for name, path in target_inputs.items()}},
        "candidate_count": len(candidates),
        "counts_by_kind": dict(sorted(Counter(row["candidate_kind"] for row in candidates).items())),
        "counts_by_evidence_level": dict(sorted(Counter(row["evidence_level"] for row in candidates).items())),
        "candidates": candidates,
        "rule_ledger_membership": "denied",
        "production_eligibility": "denied",
    }


def candidate_denominator_document(
    items: Sequence[Mapping[str, Any]],
    semantic: Mapping[str, Any],
    *,
    campaign_id: str,
    target_version: str,
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """五组正交候选分母（字段与 2.1.226 完整 Campaign 的分母逐组相同；不带生成时刻，同一输入逐字节相同）。"""

    send_points = [
        {
            "candidate_id": item["discovery_id"],
            "proposition": item["proposition"],
            "semantic_candidate_ids": list(item["semantic_candidate_ids"]),
            "source_evidence_paths": list(item["evidence_paths"]),
            "required_target_conclusion": True,
            "target_measurement_status": "pending",
        }
        for item in sorted(items, key=lambda value: value["discovery_id"])
        if item["source_kind"] == TARGET_AST
    ]
    families = [
        {
            "candidate_id": row["id"],
            "domain": row["domain"],
            "candidate_kind": row["candidate_kind"],
            "proposition": row["retained_claim"],
            "scope": row["scope"],
            "required_channels": sorted(row["required_channels"]),
            "source_ids": list(row["source_ids"]),
            "required_target_conclusion": True,
            "target_measurement_status": "pending",
        }
        for row in semantic["candidates"]
    ]
    groups = {TARGET_SEND_POINTS: send_points, SEMANTIC_FAMILIES: families, **{group: baseline["candidate_groups"][group] for group in HISTORICAL_GROUPS}}
    seen: set[str] = set()
    for group in DENOMINATOR_GROUPS:
        ids = {row["candidate_id"] for row in groups[group]}
        if len(ids) != len(groups[group]) or seen & ids:
            raise DiscoveryCompletionError(f"候选组 {group} 的候选身份重复或与其它组重复。")
        seen.update(ids)
    counts = {group: len(groups[group]) for group in sorted(groups)}
    return {
        "schema_version": CANDIDATE_DENOMINATOR_SCHEMA,
        "campaign_id": campaign_id,
        "target_version": target_version,
        "source_bindings": {"historical_baseline": dict(baseline["binding"])},
        "counts": counts,
        "total_orthogonal_candidates": sum(counts.values()),
        "candidate_groups": {group: groups[group] for group in sorted(groups)},
        "candidate_groups_sha256": canonical_sha256({group: groups[group] for group in sorted(groups)}),
        "closure_policy": dict(CLOSURE_POLICY),
    }


def build_discovery_layers(
    *,
    campaign_id: str,
    target_version: str,
    inventory: Mapping[str, Any],
    inventory_path: str,
    inventory_sha256: str,
    containment: Mapping[str, Any],
    containment_path: str,
    dispositions: Mapping[str, Any] | None = None,
    dispositions_path: str | None = None,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """VC-1 发现补全的入口：返回 DiscoveryInventory、语义候选、正交候选分母与历史基线绑定（路径一律相对宿主数据根）。"""

    if inventory.get("target_version") != target_version:
        raise DiscoveryCompletionError("目标 SinkInventory 的版本与 Campaign 目标版本不一致。")
    history = baseline if baseline is not None else load_historical_baseline()
    index = containment_index(containment, inventory=inventory, inventory_sha256=inventory_sha256, target_version=target_version)
    target = target_discovery_items(
        target_version=target_version,
        inventory=inventory,
        inventory_path=inventory_path,
        containment=index,
        containment_path=containment_path,
    )
    items: dict[str, dict[str, Any]] = {}
    for item in [*target, *history["items"]]:
        if item["discovery_id"] in items:
            raise DiscoveryCompletionError(f"发现项身份重复（目标与历史基线或目标内部撞号）：{item['discovery_id']}")
        items[item["discovery_id"]] = dict(item)
    apply_explicit_dispositions(items, dispositions, dispositions_path=dispositions_path, target_version=target_version)
    ordered = [items[key] for key in sorted(items)]
    semantic = semantic_candidates_document(
        ordered,
        target_version=target_version,
        baseline=history,
        target_inputs={"target_inventory": inventory_path, "sink_containment": containment_path},
    )
    return {
        "discovery_inventory": discovery_inventory_document(ordered, target_version=target_version),
        "semantic_candidates": semantic,
        "candidate_denominator": candidate_denominator_document(
            ordered, semantic, campaign_id=campaign_id, target_version=target_version, baseline=history
        ),
        "historical_baseline": dict(history["binding"]),
    }


# ---------------------------------------------------------------------------
# 冻结历史候选基线（一次性；只读 2.1.226 历史产物，不在 VC-1 运行时调用）
# ---------------------------------------------------------------------------


def _read_frozen_json(repository_root: Path, relative: str, label: str) -> tuple[dict[str, Any], dict[str, str]]:
    path = Path(repository_root) / relative
    if path.is_symlink() or not path.is_file():
        raise DiscoveryCompletionError(f"{label}缺失或不是可信普通文件：{relative}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiscoveryCompletionError(f"{label}不是合法 JSON：{relative}") from error
    if not isinstance(value, dict):
        raise DiscoveryCompletionError(f"{label}顶层必须是对象：{relative}")
    return value, {"path": relative, "sha256": _sha256(raw)}


def _table_index(values: Iterable[str]) -> tuple[list[str], dict[str, int]]:
    table = sorted(set(values))
    return table, {value: position for position, value in enumerate(table)}


def build_historical_baseline(
    repository_root: Path,
    *,
    semantic_closure_dir: str,
    candidate_denominator: str,
) -> bytes:
    """由 2.1.226 历史产物生成历史候选基线（规范 JSON 字节）；输入路径相对 ``repository_root``，逐个核对相互绑定。"""

    closure_dir = semantic_closure_dir.rstrip("/")
    discovery, discovery_binding = _read_frozen_json(repository_root, f"{closure_dir}/discovery-inventory.json", "历史 DiscoveryInventory")
    semantic, semantic_binding = _read_frozen_json(repository_root, f"{closure_dir}/semantic-candidates.json", "历史语义候选")
    review, review_binding = _read_frozen_json(repository_root, f"{closure_dir}/closure-review.json", "历史语义闭合收据")
    _atoms, atoms_binding = _read_frozen_json(repository_root, f"{closure_dir}/document-atoms.json", "历史 HitCC 文档原子")
    denominator, denominator_binding = _read_frozen_json(repository_root, candidate_denominator, "历史完整候选分母")
    source_coverage, source_binding = _read_frozen_json(
        repository_root, "tools/official_client_capture/claude_21220/source_2_1_88_coverage.json", "2.1.88 源码覆盖"
    )
    hitcc_coverage, hitcc_binding = _read_frozen_json(
        repository_root, "tools/official_client_capture/claude_21220/hitcc_2_1_197_coverage.json", "HitCC 覆盖"
    )
    rules, rules_binding = _read_frozen_json(repository_root, "tools/official_client_capture/claude_21220/rules_2_1_220.json", "2.1.220 历史规则")
    if discovery.get("schema_version") != DISCOVERY_INVENTORY_SCHEMA or semantic.get("schema_version") != SEMANTIC_CANDIDATES_SCHEMA:
        raise DiscoveryCompletionError("历史 DiscoveryInventory 或语义候选的合同名不符。")
    if denominator.get("schema_version") != CANDIDATE_DENOMINATOR_SCHEMA:
        raise DiscoveryCompletionError("历史完整候选分母的合同名不符。")
    for name, binding in (("discovery_inventory", discovery_binding), ("semantic_candidates", semantic_binding)):
        if (denominator.get("source_bindings") or {}).get(name, {}).get("sha256") != binding["sha256"]:
            raise DiscoveryCompletionError(f"历史候选分母绑定的 {name} 与给定的语义闭合产物不是同一份。")
    if (review.get("output_bindings") or {}).get("discovery_inventory", {}).get("sha256") != discovery_binding["sha256"]:
        raise DiscoveryCompletionError("历史语义闭合收据绑定的 DiscoveryInventory 与给定的不是同一份。")
    # 语义候选目录：历史产物里的 32 族定义必须逐字等于当前代码目录（目录是归并键，变化即须全量重审）。
    for row in semantic.get("candidates") or []:
        definition = fw_e_closure.SEMANTIC_CANDIDATE_CATALOG.get(str(row.get("id")))
        if definition is None or any(row.get(key) != value for key, value in definition.items()):
            raise DiscoveryCompletionError(f"历史语义候选 {row.get('id')} 与当前目录定义不一致。")
    if len(semantic.get("candidates") or []) != len(fw_e_closure.SEMANTIC_CANDIDATE_CATALOG):
        raise DiscoveryCompletionError("历史语义候选的族数与当前目录不一致。")
    historical = [
        _check_item(item, f"历史发现项 {item.get('discovery_id')!r}")
        for item in discovery.get("items") or []
        if isinstance(item, Mapping) and item.get("source_kind") != TARGET_AST
    ]
    if any(item["source_kind"] not in HISTORICAL_SOURCE_KINDS for item in historical):
        raise DiscoveryCompletionError("历史 DiscoveryInventory 含未知来源类别。")
    historical.sort(key=lambda item: item["discovery_id"])
    # 双向闭合（只看历史项）：历史项的正向归并必须正好等于语义候选里历史来源的反向登记。
    historical_ids = {item["discovery_id"] for item in historical}
    forward = {(item["discovery_id"], cand) for item in historical for cand in item["semantic_candidate_ids"]}
    backward = {
        (source, str(row["id"])) for row in semantic["candidates"] for source in row.get("source_ids") or [] if source in historical_ids
    }
    if forward != backward:
        raise DiscoveryCompletionError("历史发现项与语义候选的来源引用不双向闭合。")
    groups = denominator.get("candidate_groups")
    if not isinstance(groups, Mapping) or any(not isinstance(groups.get(group), list) for group in HISTORICAL_GROUPS):
        raise DiscoveryCompletionError("历史完整候选分母缺少三组历史候选。")
    candidate_groups = {group: sorted((dict(row) for row in groups[group]), key=lambda row: str(row["candidate_id"])) for group in HISTORICAL_GROUPS}
    kinds, kind_index = _table_index(item["source_kind"] for item in historical)
    dispositions, disposition_index = _table_index(item["disposition"] for item in historical)
    rationales, rationale_index = _table_index(item["rationale"] for item in historical)
    paths, path_index = _table_index(path for item in historical for path in item["evidence_paths"])
    rows = [
        [
            item["discovery_id"],
            kind_index[item["source_kind"]],
            item["proposition"],
            disposition_index[item["disposition"]],
            item["semantic_candidate_ids"],
            item["spec_ids"],
            [path_index[path] for path in item["evidence_paths"]],
            rationale_index[item["rationale"]],
        ]
        for item in historical
    ]
    forensic = source_coverage.get("forensic_root_inventory") or {}
    hitcc_source = hitcc_coverage.get("source_inventory") or {}
    payload = {
        "schema_version": BASELINE_SCHEMA,
        "baseline_id": BASELINE_ID,
        "catalog_sha256": catalog_sha256(),
        "counts": {
            "discovery_items": len(historical),
            "discovery_by_source_kind": dict(sorted(Counter(item["source_kind"] for item in historical).items())),
            "candidate_groups": {group: len(candidate_groups[group]) for group in HISTORICAL_GROUPS},
        },
        "extraction_receipt": {
            "contract": (
                "历史发现项＝FW-E 语义闭合 DiscoveryInventory 中目标 AST 调用以外的全部条目，逐字冻结；三组历史候选＝完整 Campaign "
                "候选分母的同名三组，逐字冻结；语义候选目录与归并词表取自 claude_fw_e_validation_closure（catalog_sha256 冻结）"
            ),
            "inputs": {
                "discovery_inventory": discovery_binding,
                "semantic_candidates": semantic_binding,
                "semantic_closure_review": review_binding,
                "document_atoms": atoms_binding,
                "candidate_denominator": denominator_binding,
                "source_2_1_88_coverage": source_binding,
                "hitcc_2_1_197_coverage": hitcc_binding,
                "historical_rules_2_1_220": rules_binding,
            },
            "producers": {
                "semantic_closure": dict((semantic.get("input_bindings") or {}).get("producer") or {}),
                "candidate_denominator": dict((denominator.get("source_bindings") or {}).get("producer") or {}),
            },
            "raw_sources": {
                "claude_code_2_1_88": {key: forensic.get(key) for key in ("root", "file_count", "byte_count", "path_and_content_sha256")},
                "hitcc_2_1_197": {
                    key: hitcc_source.get(key)
                    for key in ("git_commit", "git_worktree_clean", "markdown_files_total", "markdown_byte_count", "markdown_path_and_content_sha256")
                },
            },
            "historical_rule_count": sum(len(rules.get(key) or []) for key in ("rules", "replacement_rules", "additional_rules")),
        },
        "tables": {"source_kinds": kinds, "dispositions": dispositions, "rationales": rationales, "evidence_paths": paths},
        "item_fields": list(DISCOVERY_ITEM_FIELDS),
        "items": rows,
        "candidate_groups": candidate_groups,
    }
    if payload["extraction_receipt"]["historical_rule_count"] != len(candidate_groups["historical_rules"]):
        raise DiscoveryCompletionError("2.1.220 历史规则数与候选分母的历史规则组不一致。")
    return canonical_json_bytes(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude VC-1 发现补全：冻结历史候选基线（一次性，只读历史产物）。")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze-baseline", help="由 2.1.226 历史产物生成历史候选基线")
    freeze.add_argument("--repository-root", type=Path, required=True, help="含 local-analysis 历史产物的仓库根（只读）")
    freeze.add_argument("--semantic-closure-dir", required=True, help="FW-E 语义闭合目录（相对仓库根）")
    freeze.add_argument("--candidate-denominator", required=True, help="完整 Campaign 的候选分母（相对仓库根）")
    freeze.add_argument("--output", type=Path, required=True, help="基线输出路径（必须不存在）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        data = build_historical_baseline(
            arguments.repository_root,
            semantic_closure_dir=arguments.semantic_closure_dir,
            candidate_denominator=arguments.candidate_denominator,
        )
        output = Path(arguments.output)
        if output.exists() or output.is_symlink():
            raise DiscoveryCompletionError(f"输出已存在，拒绝覆盖冻结基线：{output}")
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    except (DiscoveryCompletionError, OSError) as error:
        print(f"冻结历史候选基线失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output), "sha256": _sha256(data), "bytes": len(data)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
