"""Claude 官方取证封存的证据语义（Claude 升级控制面设计 B2-07）。

本模块只读证据文件并做确定性计算：不读 Campaign 总计划、不打开 Store、不写任何文件。编排器的 evidence 根
（``scan_evidence``、``_evidence_inventory``、``_seal_capture_attempt``）只转调这里，Claude 的 evidence 闭包因此只含封存
语义本身；seal 链的控制流程（锁、总计划、Store 写入、checkpoint、计时账本、请求计量）在 ``claude_upgrade_seal``，
改它们不会被判成 evidence 变化而追加评估纪元。闭包涉及的模块集合由 ``test_claude_upgrade_seal`` 锁定。

封存语义：
* **中继证据视图**（``relay_probe_view``／``scan_relay_evidence``）：每个 probe 目录的 ``relay-manifest.json`` 与
  ``relay/relay.json`` 归一成有效连接序列——中继登记为有效、且不在中继清单 ``relay_integrity.excluded_connections``
  （握手前断开；其中 live 空握手在 ``relay.json`` 里也记 ``valid: true``）里的连接，按连接号排序，每条带请求
  「方法 路径」（取中继清单 ``dimension_evidence.actual_wire_inventory``，去掉查询串）与合成干预动作。
* **capture manifest**：按证据标签声明（B2-05）逐 probe 匹配有效连接（``claude_upgrade_plan.match_relay_connections``），
  中继请求字节按「有效连接序号」贴标签，其余标签（中继清单、合成干预、P 通道 pcap、debug 日志）按声明的文件取。
* **CaptureIndex**：逐 Job、逐 probe 复核中继清单（完整状态、probe 身份、客户端身份、运行镜像、M 绑定、秘密扫描、
  清理、中继完整性、响应模式），并列出不经 host 预过滤的全部 host／path 观测。请求计量不在这里：VC-1 checkpoint 的
  ``live_request_count`` 由控制流程按 B1-11（``claude_upgrade_live_request_provenance``）同一口径核算。
* **DiscoveryInventory**（沿用 ``claude-code-fw-e-discovery-inventory/v1``）：目标 SinkInventory 的「未决」项
  （``claude_target_inventory.unresolved_discovery_items``，处置 unclassified），再套用人工显式处置；只要还有
  unclassified，闭合门禁就是 blocked。
* **跨来源矩阵**：逐条必需规则绑定覆盖它的场景、probe 与证据项，缺证据即缺口；附 SinkInventory 与发现项摘要。
* **EvidencePackage v3**：每个 probe 一个证据项（``EVIDENCE-<probe>``），只含 ``source_ids``（probe 所属场景）、外部
  证据绑定与适用条件，不含规则结论。深度扫描（``evidence_inventory``，seal 预览阶段唯一一次）之后才绑定摘要，
  并按 Store 的内容寻址算法预先算出流量观测策略、完整性清单与 EvidencePackage 的对象引用。

外部证据路径一律相对宿主数据根（Store 外部核验的 external root）。本文件被 Codex 身份策略忽略（``claude_`` 前缀），
在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层，经 evidence 根按符号进入 evidence 闭包。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_target_inventory
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.contracts import OBJECT_SCHEMA, validate_object_document
from tools.official_client_control.errors import ControlError

RELAY_MANIFEST_SCHEMA = "claude-code-fw-e-relay-run/v1"
RELAY_MANIFEST_FILENAME = "relay-manifest.json"
RELAY_INDEX_RELATIVE = Path("relay") / "relay.json"
CAPTURE_MANIFEST_SCHEMA = "claude-upgrade-capture-manifest/v1"
CAPTURE_INDEX_SCHEMA = "claude-upgrade-capture-index/v1"
DISCOVERY_INVENTORY_SCHEMA = "claude-code-fw-e-discovery-inventory/v1"
DISCOVERY_DISPOSITIONS_SCHEMA = "claude-upgrade-discovery-dispositions/v1"
CROSS_SOURCE_MATRIX_SCHEMA = "claude-upgrade-cross-source-matrix/v1"
EVIDENCE_INVENTORY_SCHEMA = "claude-upgrade-evidence-inventory/v1"
EVIDENCE_PACKAGE_V3_SCHEMA = "official-client-evidence-package/v3"
OPERATIONAL_EVIDENCE_SCHEMA = "official-client-operational-evidence/v1"
COMPARISON_POLICY_MANIFEST_ID = "claude-upgrade-traffic-observation-policy"
COMPLETENESS_MANIFEST_ID = "claude-upgrade-vc1-completeness"
DISCOVERY_SOURCE = "claude-upgrade-vc1-official-seal"
UNCLASSIFIED = "unclassified"
# 人工显式处置可选的终态（沿用 FW-E 交叉矩阵的处置词表，去掉 unclassified）。
EXPLICIT_DISPOSITIONS = frozenset(
    {"mapped_historical", "mapped_managed", "record_only_disabled", "mapped_validation", "out_of_scope_proven", "catalogued_context"}
)
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
# seal 草稿（写在 seal 目录的 drafts/ 下）：名字 → 文件名。
DRAFT_FILES = {
    "capture_manifest": "capture-manifest.json",
    "capture_index": "capture-index.json",
    "discovery_inventory": "discovery-inventory.json",
    "cross_source_matrix": "cross-source-matrix.json",
}
# 完整性清单的固定绑定用途：名字 → 用途说明。路径由控制流程按 Campaign 坐标给出（``view["completeness_paths"]``），
# 绑定哪些文件属于证据语义，在这里冻结；可选项为 None 时不绑定。
COMPLETENESS_PURPOSES = {
    "capture_manifest": "按证据标签逐连接贴标签的 capture manifest",
    "capture_index": "逐 Job／probe 复核中继清单与全 host／path 观测的 CaptureIndex",
    "discovery_inventory": "含目标 SinkInventory 未决项的 DiscoveryInventory",
    "cross_source_matrix": "必需规则 × 场景 × 证据项的跨来源覆盖矩阵",
    "target_inventory": "VC-1 静态分析产出的目标 SinkInventory",
    "dispositions": "发现项的人工显式处置",
    "attempt": "封存所依据的官方取证 attempt 收据",
    "evidence_labels": "Campaign 冻结的证据标签声明",
    "target_scenarios": "Campaign 冻结的目标场景清单",
}
OPTIONAL_COMPLETENESS = frozenset({"dispositions"})


class SealEvidenceError(ConfigurationError):
    """证据文件不可信、结构不闭合或与声明不一致：封存失败关闭。"""


# ---------------------------------------------------------------------------
# 基础读取
# ---------------------------------------------------------------------------


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SealEvidenceError(f"{label}缺失或不是可信普通文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SealEvidenceError(f"{label}不是合法 JSON：{path}") from error
    if not isinstance(value, dict):
        raise SealEvidenceError(f"{label}顶层必须是对象：{path}")
    return value


def _relative(path: Path, base: Path, label: str) -> str:
    try:
        return Path(path).relative_to(Path(base)).as_posix()
    except ValueError as error:
        raise SealEvidenceError(f"{label}不在宿主数据根内：{path}") from error


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _walk_files(root: Path) -> list[Path]:
    """目录树内全部普通文件（排序）；根本身可以是单个文件。遇到符号链接或特殊文件即拒绝。"""

    if root.is_symlink() or not root.exists():
        raise SealEvidenceError(f"证据根缺失或是符号链接：{root}")
    if root.is_file():
        return [root]
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        directories.sort()
        for name in [*directories, *names]:
            path = Path(current) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SealEvidenceError(f"证据树含符号链接：{path}")
            if name in names:
                if not stat.S_ISREG(mode):
                    raise SealEvidenceError(f"证据树含非普通文件：{path}")
                files.append(path)
    return sorted(files)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------------------
# 中继证据视图与深度扫描
# ---------------------------------------------------------------------------


def _connection_requests(manifest: Mapping[str, Any]) -> dict[int, list[str]]:
    """中继清单逐请求记录 → 连接号 → 按流偏移排序的「方法 路径」（去掉查询串）。"""

    inventory = _mapping(manifest.get("dimension_evidence")).get("actual_wire_inventory")
    if not isinstance(inventory, list):
        raise SealEvidenceError("中继清单缺少 dimension_evidence.actual_wire_inventory。")
    rows: dict[int, list[tuple[int, str]]] = {}
    for item in inventory:
        if not isinstance(item, Mapping):
            raise SealEvidenceError("中继清单的请求记录必须是对象。")
        connection_id = item.get("connection_id")
        method = item.get("method")
        target = item.get("request_target")
        offset = item.get("stream_offset")
        if (
            not isinstance(connection_id, int)
            or isinstance(connection_id, bool)
            or not isinstance(method, str)
            or not isinstance(target, str)
            or not isinstance(offset, int)
            or isinstance(offset, bool)
        ):
            raise SealEvidenceError("中继清单的请求记录字段非法。")
        rows.setdefault(connection_id, []).append((offset, f"{method} {target.split('?', 1)[0]}"))
    return {connection_id: [line for _offset, line in sorted(values)] for connection_id, values in rows.items()}


def relay_probe_view(probe_dir: Path, *, base: Path) -> dict[str, Any]:
    """一个 probe 目录的中继证据视图：清单要点、有效连接序列与目录内文件清单（不算摘要）。

    有效连接 = ``relay.json`` 登记 ``valid: true`` 且不在中继清单 ``excluded_connections`` 里的连接（标签匹配前先剔除
    握手前断开的连接，设计 B2-05）；被剔除的连接上不得有请求。
    """

    manifest = _read_json(probe_dir / RELAY_MANIFEST_FILENAME, "中继清单")
    relay = _read_json(probe_dir / RELAY_INDEX_RELATIVE, "中继连接索引")
    connections = relay.get("connections")
    if not isinstance(connections, list):
        raise SealEvidenceError(f"中继连接索引缺少 connections：{probe_dir}")
    integrity = _mapping(manifest.get("relay_integrity"))
    excluded: set[int] = set()
    for item in integrity.get("excluded_connections") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("connection_id"), int):
            raise SealEvidenceError(f"中继清单的握手前断开连接记录非法：{probe_dir}")
        excluded.add(int(item["connection_id"]))
    registered: dict[int, Any] = {}
    valid: list[int] = []
    for item in connections:
        if not isinstance(item, Mapping) or not isinstance(item.get("connection_id"), int) or isinstance(item.get("connection_id"), bool):
            raise SealEvidenceError(f"中继连接记录非法：{probe_dir}")
        connection_id = int(item["connection_id"])
        if connection_id in registered:
            raise SealEvidenceError(f"中继连接号重复：{probe_dir} {connection_id}")
        registered[connection_id] = item.get("intervention")
        if item.get("valid") is True and connection_id not in excluded:
            valid.append(connection_id)
    if not excluded <= set(registered):
        raise SealEvidenceError(f"中继清单登记的握手前断开连接不在连接索引里：{probe_dir}")
    requests = _connection_requests(manifest)
    stray = sorted(set(requests) - set(valid))
    if stray:
        raise SealEvidenceError(f"请求落在无效或握手前断开的连接上：{probe_dir} {stray}")
    client = _mapping(manifest.get("client"))
    runtime = _mapping(manifest.get("runtime"))
    upstream = _mapping(manifest.get("upstream"))
    cleanup = _mapping(manifest.get("cleanup"))
    return {
        "probe_id": manifest.get("probe_id"),
        "directory": _relative(probe_dir, base, "probe 证据目录"),
        "manifest": {
            "schema_version": manifest.get("schema_version"),
            "status": manifest.get("status"),
            "fw_f_v4_probe": manifest.get("fw_f_v4_probe"),
            "response_plan": manifest.get("response_plan"),
            "client_version": client.get("version"),
            "client_sha256": client.get("sha256"),
            "runtime_image": runtime.get("runtime_image_reference"),
            "host_runtime_receipt_sha256": _mapping(runtime.get("host_runtime_receipt")).get("sha256"),
            "upstream_host": upstream.get("host"),
            "live_requests": upstream.get("live_requests"),
            "m_binding_complete": _mapping(manifest.get("m_binding")).get("complete"),
            "secret_scan_passed": _mapping(manifest.get("secret_scan")).get("passed"),
            "cleanup": {"relay_stopped": cleanup.get("relay_stopped"), "hosts_restored": cleanup.get("hosts_restored")},
            "relay_integrity_result": integrity.get("result"),
        },
        "connections": [
            {"connection_id": connection_id, "requests": requests.get(connection_id, []), "intervention": registered[connection_id]}
            for connection_id in sorted(valid)
        ],
        "excluded_connection_ids": sorted(excluded),
        "files": [_relative(path, probe_dir, "probe 证据文件") for path in _walk_files(probe_dir)],
    }


def _probe_directories(root: Path) -> list[Path]:
    """证据根下的 probe 目录：根自身含中继清单时就是 probe 目录，否则取直接子目录中含中继清单的。"""

    if root.is_symlink() or not root.is_dir():
        raise SealEvidenceError(f"证据根缺失或不是可信目录：{root}")
    if (root / RELAY_MANIFEST_FILENAME).exists():
        return [root]
    return sorted(
        child for child in root.iterdir() if child.is_dir() and not child.is_symlink() and (child / RELAY_MANIFEST_FILENAME).exists()
    )


def scan_relay_evidence(paths: Iterable[Path], label: str, *, base: Path) -> dict[str, Any]:
    """在证据根里找全部 probe 目录，返回逐 probe 视图与不经 host 预过滤的全 host／path 出站面。"""

    probes: dict[str, dict[str, Any]] = {}
    for root in paths:
        for probe_dir in _probe_directories(Path(root)):
            view = relay_probe_view(probe_dir, base=base)
            probe_id = str(view["probe_id"])
            if probe_dir.name != probe_id:
                raise SealEvidenceError(f"probe 目录名与中继清单的 probe_id 不一致：{probe_dir}")
            if probe_id in probes:
                raise SealEvidenceError(f"同一 probe 出现在多个证据根：{probe_id}")
            probes[probe_id] = view
    surfaces: Counter[str] = Counter()
    for view in probes.values():
        host = view["manifest"]["upstream_host"]
        for connection in view["connections"]:
            for line in connection["requests"]:
                surfaces[f"{host} {line}"] += 1
    return {
        "label": label,
        "probes": dict(sorted(probes.items())),
        "surfaces": [{"surface": surface, "count": count} for surface, count in sorted(surfaces.items())],
    }


def evidence_inventory(roots: Iterable[Path], *, base: Path) -> dict[str, Any]:
    """深度扫描：根下全部文件的相对路径（相对宿主数据根）、字节数与 SHA-256，按路径排序并给出整体摘要。"""

    entries: dict[str, dict[str, Any]] = {}
    for root in roots:
        for path in _walk_files(Path(root)):
            relative = _relative(path, base, "证据文件")
            entries[relative] = {"path": relative, "bytes": path.stat().st_size, "sha256": _file_sha256(path)}
    ordered = [entries[key] for key in sorted(entries)]
    return {
        "schema_version": EVIDENCE_INVENTORY_SCHEMA,
        "entry_count": len(ordered),
        "total_bytes": sum(item["bytes"] for item in ordered),
        "entries": ordered,
        "digest": canonical_sha256({"entries": ordered}),
    }


# ---------------------------------------------------------------------------
# capture manifest 与 CaptureIndex
# ---------------------------------------------------------------------------


def _probe_scenarios(scenarios: Mapping[str, Any]) -> dict[str, list[str]]:
    result: dict[str, set[str]] = {}
    for item in scenarios["evidence_scenarios"]:
        for probe in item["probe_ids"]:
            result.setdefault(probe, set()).add(item["scenario_id"])
    return {probe: sorted(values) for probe, values in result.items()}


def _label_artifacts(job_id: str, view: Mapping[str, Any], plan: Mapping[str, Any], rules: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """一个 probe 的全部带标签证据：中继请求字节逐有效连接匹配，其余按声明的文件取；不一致抛 ``LabelSequenceMismatch``。"""

    probe = str(view["probe_id"])
    directory = str(view["directory"])
    files = set(view["files"])
    artifacts: list[dict[str, Any]] = []
    for match in claude_upgrade_plan.match_relay_connections(plan, view["connections"]):
        rule = rules[match["rule_index"]]
        relative = f"relay/conn{match['connection_id']:03d}.client_to_upstream.bin"
        if relative not in files:
            raise claude_upgrade_plan.LabelSequenceMismatch(f"{probe} 缺少有效连接 {match['connection_id']} 的请求字节文件。")
        artifact = {
            "job_id": job_id,
            "probe_id": probe,
            "path": f"{directory}/{relative}",
            "kind": rule["kind"],
            "parser": rule["parser"],
            "labels": dict(rule["labels"]),
            "scenario_ids": list(rule["scenario_ids"]),
            "connection": {"ordinal": match["ordinal"], "connection_id": match["connection_id"]},
        }
        if "derive" in rule:
            artifact["derive"] = dict(rule["derive"])
        artifacts.append(artifact)
    for rule in rules:
        glob = str(rule["glob"])
        if "connection" in rule or not glob.startswith(f"{probe}/"):
            continue
        matched = sorted(name for name in files if fnmatch.fnmatchcase(f"{probe}/{name}", glob))
        if not matched:
            raise claude_upgrade_plan.LabelSequenceMismatch(f"{probe} 缺少标签声明的证据：{glob}")
        artifacts.extend(
            {
                "job_id": job_id,
                "probe_id": probe,
                "path": f"{directory}/{name}",
                "kind": rule["kind"],
                "parser": rule["parser"],
                "labels": dict(rule["labels"]),
                "scenario_ids": list(rule["scenario_ids"]),
            }
            for name in matched
        )
    return artifacts


def _synthetic_probes(labels: Mapping[str, Any]) -> set[str]:
    return {
        str(rule["labels"]["probe"])
        for entry in labels["document"]["entries"]
        for rule in entry["rules"]
        if rule["labels"].get("response_mode") == "synthetic"
    }


def _probe_checks(view: Mapping[str, Any], *, probe: str, context: Mapping[str, Any], synthetic: bool) -> list[str]:
    """CaptureIndex 的逐 probe 复核：返回未通过的检查名（空表示通过）。"""

    manifest = view["manifest"]
    version = str(manifest.get("client_version") or "").split(" ", 1)[0]
    checks = {
        "relay_manifest_schema": manifest.get("schema_version") == RELAY_MANIFEST_SCHEMA,
        "status_complete": manifest.get("status") == "complete",
        "probe_identity": manifest.get("fw_f_v4_probe") == probe and view.get("probe_id") == probe,
        "client_identity": manifest.get("client_sha256") == context["target_sha256"] and version == context["target_version"],
        "runtime_image": manifest.get("runtime_image") == context["runtime_image"],
        "m_binding_complete": manifest.get("m_binding_complete") is True,
        "secret_scan_passed": manifest.get("secret_scan_passed") is True,
        "cleanup": manifest.get("cleanup") == {"relay_stopped": True, "hosts_restored": True},
        "relay_integrity_passed": manifest.get("relay_integrity_result") == "passed",
        "response_mode": bool(manifest.get("response_plan")) == synthetic and manifest.get("live_requests") is (not synthetic),
    }
    return sorted(name for name, passed in checks.items() if not passed)


# ---------------------------------------------------------------------------
# DiscoveryInventory 与跨来源矩阵
# ---------------------------------------------------------------------------


def build_discovery_inventory(view: Mapping[str, Any]) -> dict[str, Any]:
    """目标 SinkInventory 的未决项（unclassified）套用人工显式处置；按 discovery_id 排序。"""

    static = view["static"]
    inventory = static["target_inventory"]
    if inventory.get("target_version") != view["target_version"]:
        raise SealEvidenceError("目标 SinkInventory 的版本与 Campaign 目标版本不一致。")
    try:
        unresolved = claude_target_inventory.unresolved_discovery_items(dict(inventory), [static["target_inventory_path"]])
    except (claude_target_inventory.TargetInventoryError, KeyError, TypeError) as error:
        raise SealEvidenceError(f"目标 SinkInventory 不能给出未决发现项：{error}") from error
    items: dict[str, dict[str, Any]] = {}
    for raw in unresolved:
        if set(raw) != set(DISCOVERY_ITEM_FIELDS) or raw["discovery_id"] in items:
            raise SealEvidenceError(f"未决发现项字段不闭合或重复：{raw.get('discovery_id')}")
        items[str(raw["discovery_id"])] = {field: raw[field] for field in DISCOVERY_ITEM_FIELDS}
    dispositions = static.get("dispositions")
    if dispositions is not None:
        if (
            set(dispositions) != {"schema_version", "target_version", "items"}
            or dispositions.get("schema_version") != DISCOVERY_DISPOSITIONS_SCHEMA
            or dispositions.get("target_version") != view["target_version"]
            or not isinstance(dispositions.get("items"), list)
        ):
            raise SealEvidenceError("发现项显式处置文件的合同名、版本或 items 非法。")
        seen: set[str] = set()
        for raw in dispositions["items"]:
            if not isinstance(raw, Mapping) or set(raw) != {"discovery_id", "disposition", "rationale"}:
                raise SealEvidenceError("发现项显式处置条目字段不闭合。")
            discovery_id = str(raw["discovery_id"])
            if discovery_id in seen or discovery_id not in items:
                raise SealEvidenceError(f"显式处置指向重复或不存在的发现项：{discovery_id}")
            if raw["disposition"] not in EXPLICIT_DISPOSITIONS or not isinstance(raw["rationale"], str) or not raw["rationale"].strip():
                raise SealEvidenceError(f"发现项 {discovery_id} 的显式处置非法（处置须是终态、理由不能为空）。")
            seen.add(discovery_id)
            items[discovery_id] = {
                **items[discovery_id],
                "disposition": raw["disposition"],
                "rationale": raw["rationale"],
                "evidence_paths": sorted({*items[discovery_id]["evidence_paths"], str(static["dispositions_path"])}),
            }
    ordered = [items[key] for key in sorted(items)]
    return {
        "schema_version": DISCOVERY_INVENTORY_SCHEMA,
        "target_version": view["target_version"],
        "item_count": len(ordered),
        "counts_by_source_kind": dict(sorted(Counter(str(item["source_kind"]) for item in ordered).items())),
        "counts_by_disposition": dict(sorted(Counter(str(item["disposition"]) for item in ordered).items())),
        "items": ordered,
        "rule_generation": "forbidden",
    }


def _cross_source_matrix(view: Mapping[str, Any], evidence_probes: Mapping[str, Any], discovery: Mapping[str, Any]) -> dict[str, Any]:
    scenarios = view["scenarios"]["evidence_scenarios"]
    rows = []
    for spec_id in view["required_rules"]:
        scenario_ids = sorted(item["scenario_id"] for item in scenarios if spec_id in item["covers"])
        probe_ids = sorted({probe for item in scenarios if item["scenario_id"] in scenario_ids for probe in item["probe_ids"]})
        evidenced = [probe for probe in probe_ids if probe in evidence_probes]
        rows.append(
            {
                "spec_id": spec_id,
                "scenario_ids": scenario_ids,
                "probe_ids": probe_ids,
                "evidence_ids": [f"EVIDENCE-{probe}" for probe in evidenced],
                "status": "covered" if evidenced else "gap",
            }
        )
    inventory = view["static"]["target_inventory"]
    sinks = inventory.get("sinks") if isinstance(inventory.get("sinks"), list) else []
    return {
        "schema_version": CROSS_SOURCE_MATRIX_SCHEMA,
        "campaign_id": view["campaign_id"],
        "target_version": view["target_version"],
        "attempt_id": view["attempt"]["attempt_id"],
        "sources": ["official_runtime_capture", "target_sink_inventory"],
        "rules": rows,
        "rule_gaps": [row["spec_id"] for row in rows if row["status"] == "gap"],
        "sinks": {
            "inventory_path": view["static"]["target_inventory_path"],
            "sink_count": len(sinks),
            "unresolved_entry_count": len(inventory.get("unresolved_entries") or []),
        },
        "discovery": {
            "item_count": discovery["item_count"],
            "counts_by_disposition": discovery["counts_by_disposition"],
            "unclassified_ids": [item["discovery_id"] for item in discovery["items"] if item["disposition"] == UNCLASSIFIED],
        },
    }


# ---------------------------------------------------------------------------
# 封存草稿与绑定
# ---------------------------------------------------------------------------


def comparison_policy_payload(persona: Mapping[str, Any]) -> dict[str, Any]:
    """流量观测比较策略（operational_evidence）：只有 essential 进入严格 wire 范围，零遥测与零非必要流量不构成差异。"""

    return {
        "schema_version": OPERATIONAL_EVIDENCE_SCHEMA,
        "persona": dict(persona),
        "manifest_id": COMPARISON_POLICY_MANIFEST_ID,
        "entries": [
            {"id": "essential", "facts": {"disposition": "strict_wire_pair_scope", "traffic_presence_comparison": "disabled"}},
            {
                "id": "nonessential",
                "facts": {
                    "disposition": "record_only",
                    "official_control": "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                    "absence_policy": "conformant_not_a_difference",
                },
            },
            {
                "id": "telemetry",
                "facts": {"disposition": "record_only", "official_control": "DISABLE_TELEMETRY", "absence_policy": "conformant_not_a_difference"},
            },
        ],
    }


def _object_reference(kind: str, payload: Mapping[str, Any]) -> dict[str, str]:
    """Store 对象的内容寻址引用（与 ``ControlStore.seal_object`` 同一算法，可在封存前确定性算出）。"""

    document = {"schema_version": OBJECT_SCHEMA, "object_kind": kind, "payload": dict(payload)}
    try:
        validate_object_document(document)
    except (ControlError, ValueError) as error:
        raise SealEvidenceError(f"{kind} 草稿不满足 Store 合同：{error}") from error
    return {"object_kind": kind, "sha256": sha256_bytes(canonical_json_bytes(document))}


def _applicability(artifacts: Sequence[Mapping[str, Any]], store: Mapping[str, Any]) -> list[str]:
    """证据项的适用条件：probe、响应模式与合成计划（取自标签），平台与隐私条件（取自 Store Campaign 的冻结身份）。"""

    def single(name: str) -> str | None:
        values = {str(item["labels"][name]) for item in artifacts if name in item["labels"]}
        if len(values) > 1:
            raise SealEvidenceError(f"同一 probe 的证据标签 {name} 不唯一：{sorted(values)}")
        return next(iter(values), None)

    probe = single("probe")
    mode = single("response_mode")
    if probe is None or mode is None:
        raise SealEvidenceError("证据标签缺少 probe 或 response_mode。")
    values = {f"probe={probe}", f"response_mode={mode}", f"platform={store['platform']}", f"privacy={store['privacy_mode']}"}
    plan = single("response_plan")
    if plan is not None:
        values.add(f"response_plan={plan}")
    values.update(f"entrypoint={item['labels']['entrypoint']}" for item in artifacts if "entrypoint" in item["labels"])
    return sorted(values)


def build_seal_drafts(view: Mapping[str, Any], *, inventory: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """由一次 attempt 的证据视图确定性生成封存草稿；给出深度扫描清单时再绑定摘要、算出 Store 写入计划。

    ``view`` 字段（由 ``claude_upgrade_seal`` 按 Campaign 坐标组装，路径除 ``labels_path`` 外一律相对宿主数据根）：
    ``campaign_id``、``target_version``、``target_sha256``、``runtime_image``；``attempt``（``attempt_id``、``run_nonce``、
    ``started_at_utc``、``completed_at_utc``）；``jobs``（``job_id``、``evidence_root``、``probe_ids``）；``required_rules``；
    ``scenarios``（冻结的场景清单）与 ``labels_path``（冻结的证据标签，绝对路径）；``probes``／``surfaces``
    （``scan_relay_evidence`` 的结果）；``static``（``target_inventory``、``target_inventory_path``、可选的 ``dispositions``
    与 ``dispositions_path``）；``store``（Store Campaign 的 ``persona``、``official_artifacts``、``platforms``、
    ``entrypoints``、``default_conditions`` 与导出的 ``platform``、``privacy_mode``）；``tool_files_sha256``（Store
    有效工具身份）；``completeness_paths``（``COMPLETENESS_PURPOSES`` 各项的文件路径，可选项可为 None）。
    """

    labels = claude_upgrade_plan.load_evidence_labels(
        Path(view["labels_path"]), expected_version=view["target_version"], scenario_payload=view["scenarios"]
    )
    probe_scenarios = _probe_scenarios(view["scenarios"])
    job_rules = {entry["job_id"]: entry["rules"] for entry in labels["document"]["entries"]}
    synthetic = _synthetic_probes(labels)
    artifacts: list[dict[str, Any]] = []
    mismatches: list[dict[str, str]] = []
    failures: list[dict[str, Any]] = []
    index_jobs: list[dict[str, Any]] = []
    evidence_probes: dict[str, list[dict[str, Any]]] = {}
    for job in view["jobs"]:
        job_id = str(job["job_id"])
        index_probes = []
        for probe in job["probe_ids"]:
            probe_view = view["probes"].get(probe)
            if probe_view is None:
                failures.append({"job_id": job_id, "probe_id": probe, "failed_checks": ["evidence_missing"]})
                index_probes.append({"probe_id": probe, "result": "failed", "failed_checks": ["evidence_missing"]})
                continue
            failed = _probe_checks(probe_view, probe=probe, context=view, synthetic=probe in synthetic)
            if not probe_scenarios.get(probe):
                failed = sorted({*failed, "scenario_membership"})
            if failed:
                failures.append({"job_id": job_id, "probe_id": probe, "failed_checks": failed})
            probe_artifacts: list[dict[str, Any]] = []
            try:
                probe_artifacts = _label_artifacts(job_id, probe_view, labels["plans"][job_id][probe], job_rules[job_id])
            except claude_upgrade_plan.LabelSequenceMismatch as error:
                mismatches.append({"job_id": job_id, "probe_id": probe, "reason": str(error)})
            artifacts.extend(probe_artifacts)
            if probe_artifacts and not failed:
                evidence_probes[probe] = probe_artifacts
            index_probes.append(
                {
                    "probe_id": probe,
                    "directory": probe_view["directory"],
                    "result": "failed" if failed else "passed",
                    "failed_checks": failed,
                    "response_mode": "synthetic" if probe in synthetic else "live",
                    "response_plan": probe_view["manifest"]["response_plan"],
                    "host_runtime_receipt_sha256": probe_view["manifest"]["host_runtime_receipt_sha256"],
                    "upstream_host": probe_view["manifest"]["upstream_host"],
                    "connections": probe_view["connections"],
                    "excluded_connection_ids": probe_view["excluded_connection_ids"],
                }
            )
        index_jobs.append({"job_id": job_id, "evidence_root": job["evidence_root"], "probes": index_probes})
    artifacts.sort(key=lambda item: (item["path"], json.dumps(item["labels"], sort_keys=True)))
    discovery = build_discovery_inventory(view)
    matrix = _cross_source_matrix(view, evidence_probes, discovery)
    reasons = [
        reason
        for reason, present in (
            ("capture_index_failed", bool(failures)),
            ("label_mismatch", bool(mismatches)),
            ("discovery_unclassified", bool(matrix["discovery"]["unclassified_ids"])),
            ("rule_gap", bool(matrix["rule_gaps"])),
        )
        if present
    ]
    closure = {"status": "blocked" if reasons else "closed", "reasons": reasons}
    matrix["closure"] = dict(closure)
    attempt = dict(view["attempt"])
    drafts = {
        "capture_manifest": {
            "schema_version": CAPTURE_MANIFEST_SCHEMA,
            "campaign_id": view["campaign_id"],
            "target_version": view["target_version"],
            "attempt": attempt,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "label_mismatches": mismatches,
        },
        "capture_index": {
            "schema_version": CAPTURE_INDEX_SCHEMA,
            "campaign_id": view["campaign_id"],
            "target_version": view["target_version"],
            "target_sha256": view["target_sha256"],
            "runtime_image": view["runtime_image"],
            "attempt": attempt,
            "jobs": index_jobs,
            "failures": failures,
            "network_inventory": {"host_prefilter_disabled": True, "surfaces": list(view["surfaces"])},
            "result": "failed" if failures else "passed",
        },
        "discovery_inventory": discovery,
        "cross_source_matrix": matrix,
    }
    evidence_items = [
        {
            "evidence_id": f"EVIDENCE-{probe}",
            "source_ids": list(probe_scenarios[probe]),
            "evidence_paths": sorted({item["path"] for item in probe_artifacts}),
            "applicability": _applicability(probe_artifacts, view["store"]),
        }
        for probe, probe_artifacts in sorted(evidence_probes.items())
    ]
    result: dict[str, Any] = {
        "drafts": drafts,
        "evidence_items": evidence_items,
        "closure": closure,
        "metrics": {
            "probe_count": sum(len(job["probe_ids"]) for job in view["jobs"]),
            "artifact_count": len(artifacts),
            "evidence_item_count": len(evidence_items),
            "discovery_item_count": discovery["item_count"],
        },
    }
    if inventory is not None:
        result["seal"] = _bind_seal_plan(view, evidence_items, inventory)
    return result


def _inventory_binding(index: Mapping[str, Mapping[str, Any]], path: str, label: str) -> dict[str, Any]:
    entry = index.get(path)
    if entry is None:
        raise SealEvidenceError(f"深度扫描清单缺少{label}：{path}")
    return {"path": entry["path"], "sha256": entry["sha256"], "bytes": entry["bytes"]}


def _bind_seal_plan(view: Mapping[str, Any], evidence_items: Sequence[Mapping[str, Any]], inventory: Mapping[str, Any]) -> dict[str, Any]:
    """绑定摘要：证据项的外部证据、完整性清单与比较策略、EvidencePackage v3，以及 Discovery 事实 payload。"""

    if inventory.get("schema_version") != EVIDENCE_INVENTORY_SCHEMA or not isinstance(inventory.get("entries"), list):
        raise SealEvidenceError("深度扫描清单合同名不符。")
    index = {entry["path"]: entry for entry in inventory["entries"]}
    store = view["store"]
    items = [
        {
            "evidence_id": item["evidence_id"],
            "source_ids": list(item["source_ids"]),
            "evidence_refs": [_inventory_binding(index, path, "证据项文件") for path in item["evidence_paths"]],
            "applicability": list(item["applicability"]),
        }
        for item in evidence_items
    ]
    if not items:
        raise SealEvidenceError("没有任何可封存的证据项（全部 probe 缺证据或复核未通过）。")
    paths = view["completeness_paths"]
    if set(paths) != set(COMPLETENESS_PURPOSES):
        raise SealEvidenceError("完整性清单的绑定用途与登记不一致。")
    bindings: list[tuple[str, dict[str, Any]]] = []
    for name, purpose in COMPLETENESS_PURPOSES.items():
        path = paths[name]
        if path is None:
            if name not in OPTIONAL_COMPLETENESS:
                raise SealEvidenceError(f"完整性清单缺少必需绑定：{name}")
            continue
        bindings.append((purpose, _inventory_binding(index, str(path), f"完整性绑定 {name}")))
    bindings.sort(key=lambda pair: pair[1]["path"])
    completeness = {
        "schema_version": OPERATIONAL_EVIDENCE_SCHEMA,
        "persona": dict(store["persona"]),
        "manifest_id": COMPLETENESS_MANIFEST_ID,
        "entries": [
            {"id": f"binding-{position:04d}", "facts": {"binding": binding, "purpose": purpose}}
            for position, (purpose, binding) in enumerate(bindings, start=1)
        ],
    }
    policy = comparison_policy_payload(store["persona"])
    policy_ref = _object_reference("operational_evidence", policy)
    completeness_ref = _object_reference("operational_evidence", completeness)
    package = {
        "schema_version": EVIDENCE_PACKAGE_V3_SCHEMA,
        "persona": dict(store["persona"]),
        "version": view["target_version"],
        "official_artifacts": [dict(item) for item in store["official_artifacts"]],
        "platforms": list(store["platforms"]),
        "entrypoints": list(store["entrypoints"]),
        "default_conditions": list(store["default_conditions"]),
        "comparison_policy_ref": policy_ref,
        "completeness_ref": completeness_ref,
        "producer_tool_sha256": view["tool_files_sha256"],
        "evidence_items": items,
    }
    package_ref = _object_reference("evidence_package", package)
    return {
        "objects": [
            {"name": "comparison_policy", "kind": "operational_evidence", "payload": policy, "ref": policy_ref},
            {"name": "completeness", "kind": "operational_evidence", "payload": completeness, "ref": completeness_ref},
            {"name": "evidence_package", "kind": "evidence_package", "payload": package, "ref": package_ref},
        ],
        "evidence_package_ref": package_ref,
        "discovery_payload": {
            "version": view["target_version"],
            "source": DISCOVERY_SOURCE,
            "discovered_at_utc": view["attempt"]["completed_at_utc"],
            "tool_sha256": view["tool_files_sha256"],
            "artifact_refs": [dict(item) for item in store["official_artifacts"]],
        },
        "inventory_digest": inventory["digest"],
        "scanned_bytes": inventory["total_bytes"],
    }
