#!/usr/bin/env python3
"""由冻结证据、规则台账和当前 Inventory 生成 Claude FW-E v2 封存计划。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    expect_rfc3339,
)
from tools.official_client_control.errors import ControlError  # noqa: E402


PLAN_SCHEMA = "official-client-fw-e-seal-plan/v3"
ASSESSMENTS_SCHEMA = "claude-code-fw-e-rule-assessments/v2"
FREEZE_SCHEMA = "claude-code-fw-e-official-freeze/v1"


class SealPlanError(RuntimeError):
    """表示封存计划输入不完整、身份漂移或当前 Inventory 不闭合。"""


def load_json(path: Path, label: str) -> dict[str, Any]:
    """读取可信普通 JSON 文件。"""

    if path.is_symlink() or not path.is_file():
        raise SealPlanError(f"{label} 不是可信普通文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SealPlanError(f"无法读取 {label}：{path}") from error
    if not isinstance(value, dict):
        raise SealPlanError(f"{label} 顶层必须是对象")
    return value


def relative_file(workspace_root: Path, path: Path, label: str) -> str:
    """返回工作区相对路径并拒绝越界或符号链接。"""

    if path.is_symlink() or not path.is_file():
        raise SealPlanError(f"{label} 不是可信普通文件：{path}")
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError as error:
        raise SealPlanError(f"{label} 位于工作区外：{path}") from error


def unique_relatives(
    workspace_root: Path, paths: list[Path], label: str
) -> list[str]:
    """生成去重排序的证据路径列表。"""

    return sorted({relative_file(workspace_root, path, label) for path in paths})


def write_private_json(path: Path, value: Any) -> None:
    """写入仅当前用户可读的规范 JSON。"""

    if path.exists():
        raise SealPlanError(f"输出已存在，禁止覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(canonical_json_bytes(value))
    os.chmod(path, 0o600)


def validate_ingress_catalog(
    workspace_root: Path, path: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Path]]:
    """复核物理路由标记、别名闭集和逻辑入口反向引用。"""

    catalog = load_json(path, "FW-E ingress catalog")
    if set(catalog) != {
        "schema_version",
        "campaign_stage",
        "source_bindings",
        "aliases",
        "entries",
    } or catalog.get("schema_version") != 1 or catalog.get("campaign_stage") != "FW-E":
        raise SealPlanError("FW-E ingress catalog Schema 不匹配")
    source_bindings = catalog.get("source_bindings")
    if not isinstance(source_bindings, list) or not source_bindings:
        raise SealPlanError("FW-E ingress catalog 缺少 source_bindings")
    source_paths: list[Path] = []
    for raw in source_bindings:
        if not isinstance(raw, dict) or set(raw) != {"path", "required_markers"}:
            raise SealPlanError("FW-E ingress source binding 字段不闭合")
        source_path = workspace_root / str(raw.get("path"))
        relative_file(workspace_root, source_path, "FW-E ingress source")
        markers = raw.get("required_markers")
        if not isinstance(markers, list) or not markers or not all(
            isinstance(marker, str) and marker for marker in markers
        ):
            raise SealPlanError("FW-E ingress required_markers 非法")
        source_text = source_path.read_text(encoding="utf-8")
        missing = sorted(marker for marker in markers if marker not in source_text)
        if missing:
            raise SealPlanError(f"FW-E ingress 物理路由标记漂移：{missing}")
        source_paths.append(source_path)

    aliases = catalog.get("aliases")
    entries = catalog.get("entries")
    if not isinstance(aliases, list) or not aliases or not isinstance(entries, list) or not entries:
        raise SealPlanError("FW-E ingress aliases／entries 必须是非空数组")
    alias_ids: list[str] = []
    aliases_by_logical: dict[str, set[str]] = {}
    alias_callers_by_logical: dict[str, set[str]] = {}
    for raw in aliases:
        if not isinstance(raw, dict) or set(raw) != {
            "alias_id",
            "logical_ingress_id",
            "physical_route",
            "caller_ids",
        }:
            raise SealPlanError("FW-E ingress alias 字段不闭合")
        alias_id = raw.get("alias_id")
        logical_id = raw.get("logical_ingress_id")
        caller_ids = raw.get("caller_ids")
        if not isinstance(alias_id, str) or not isinstance(logical_id, str):
            raise SealPlanError("FW-E ingress alias 身份非法")
        if (
            not isinstance(caller_ids, list)
            or not caller_ids
            or not all(isinstance(item, str) and item for item in caller_ids)
            or caller_ids != sorted(set(caller_ids))
        ):
            raise SealPlanError(f"FW-E ingress alias caller_ids 未排序或重复：{alias_id}")
        alias_ids.append(alias_id)
        aliases_by_logical.setdefault(logical_id, set()).add(alias_id)
        alias_callers_by_logical.setdefault(logical_id, set()).update(caller_ids)
    if alias_ids != sorted(set(alias_ids)):
        raise SealPlanError("FW-E ingress aliases 必须排序且不得重复")

    entry_ids: list[str] = []
    referenced_aliases: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != {
            "logical_ingress_id",
            "physical_alias_ids",
            "caller_ids",
            "adapter_id",
            "route_id",
            "ingress_kind",
            "protocol_class",
            "current_disposition",
        }:
            raise SealPlanError("FW-E ingress entry 字段不闭合")
        logical_id = raw.get("logical_ingress_id")
        physical_ids = raw.get("physical_alias_ids")
        caller_ids = raw.get("caller_ids")
        if not isinstance(logical_id, str) or not isinstance(physical_ids, list):
            raise SealPlanError("FW-E ingress entry 身份或 alias 引用非法")
        if physical_ids != sorted(set(physical_ids)):
            raise SealPlanError(f"FW-E ingress physical_alias_ids 未排序或重复：{logical_id}")
        if (
            not isinstance(caller_ids, list)
            or not caller_ids
            or not all(isinstance(item, str) and item for item in caller_ids)
            or caller_ids != sorted(set(caller_ids))
        ):
            raise SealPlanError(f"FW-E ingress entry caller_ids 未排序或重复：{logical_id}")
        if set(physical_ids) != aliases_by_logical.get(logical_id, set()):
            raise SealPlanError(f"FW-E ingress alias 反向引用不闭合：{logical_id}")
        if caller_ids != sorted(alias_callers_by_logical.get(logical_id, set())):
            raise SealPlanError(f"FW-E ingress caller_ids 与物理别名不闭合：{logical_id}")
        entry_ids.append(logical_id)
        referenced_aliases.update(str(item) for item in physical_ids)
    if entry_ids != sorted(set(entry_ids)) or referenced_aliases != set(alias_ids):
        raise SealPlanError("FW-E ingress entries 未唯一覆盖全部 aliases")
    return aliases, entries, source_paths


def safe_egress_id(raw_sink_id: str) -> str:
    """把观察 Sink 身份转换为控制面安全身份。"""

    suffix = raw_sink_id.removeprefix("unclassified.claude.").replace("_", "-")
    return f"egress-claude-{suffix}"


def build_egress_inventory(
    workspace_root: Path,
    path: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[Path],
]:
    """把已冻结的 9 条遗留发送点转换为 observation 与 managed 当前事实。"""

    catalog = load_json(path, "FW-E egress catalog")
    if set(catalog) != {"schema_version", "campaign_stage", "entries"}:
        raise SealPlanError("FW-E egress catalog 顶层字段不闭合")
    if catalog.get("schema_version") != 1 or catalog.get("campaign_stage") != "FW-E":
        raise SealPlanError("FW-E egress catalog Schema 不匹配")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SealPlanError("FW-E egress catalog 缺少 entries")
    observed: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    source_paths: set[Path] = set()
    identities: list[str] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise SealPlanError("FW-E egress catalog 条目非法")
        if set(raw) != {
            "sink_id",
            "purpose",
            "source_ref",
            "endpoint_evidence",
            "routes",
            "target_backend",
            "legacy_backends",
            "owner",
            "expiry_condition",
        }:
            raise SealPlanError("FW-E egress catalog 条目字段不闭合")
        raw_sink_id = raw.get("sink_id")
        routes = raw.get("routes")
        if not isinstance(raw_sink_id, str) or not isinstance(routes, list) or not routes:
            raise SealPlanError("FW-E egress catalog 缺少 sink_id 或 routes")
        if raw.get("owner") != "official-client-fw-e":
            raise SealPlanError(f"FW-E egress owner 漂移：{raw_sink_id}")
        source_ref = raw.get("source_ref")
        if not isinstance(source_ref, str) or ":" not in source_ref:
            raise SealPlanError(f"FW-E egress source_ref 非法：{raw_sink_id}")
        source_path = workspace_root / source_ref.split(":", 1)[0]
        relative_file(workspace_root, source_path, f"FW-E egress source {raw_sink_id}")
        source_paths.add(source_path)
        egress_id = safe_egress_id(raw_sink_id)
        identities.append(egress_id)
        purpose = str(raw.get("purpose", ""))
        kind = (
            "inference"
            if purpose.endswith("messages_inference")
            else "lifecycle"
            if any(token in purpose for token in ("oauth_", "cookie_"))
            else "auxiliary"
        )
        endpoints: list[str] = []
        for route in routes:
            if not isinstance(route, dict) or not all(
                isinstance(route.get(key), str)
                for key in ("method", "host", "path", "protocol")
            ):
                raise SealPlanError(f"FW-E egress route 非法：{raw_sink_id}")
            scheme = "https" if route["protocol"] == "http" else route["protocol"]
            endpoints.append(
                f"{route['method']} {scheme}://{route['host']}{route['path']}"
            )
        route_id = f"route-{egress_id}"
        sink_id = f"sink-{egress_id}"
        observed.append(
            {
                "egress_id": egress_id,
                "route_id": route_id,
                "sink_id": sink_id,
                "oauth_related": True,
                "kind": kind,
            }
        )
        authentication = (
            "claude.ai-session-cookie"
            if "cookie_" in purpose
            else "claude.ai-oauth"
        )
        dispositions.append(
            {
                "egress_id": egress_id,
                "current_disposition": "non_persona_managed",
                "current_guard_state": "legacy_observe",
                "spec_ids": [],
                "managed_policy": {
                    "authentication": authentication,
                    "endpoint": " | ".join(sorted(endpoints)),
                    "client": str(raw.get("target_backend")),
                    "timeout_policy": "legacy-code-defined",
                    "retry_policy": "legacy-code-defined",
                    "secret_policy": "redacted",
                    "audit_policy": "metadata-only",
                },
            }
        )
        target_disposition = (
            "persona_strict" if kind == "inference" else "non_persona_managed"
        )
        proposals.append(
            {
                "id": egress_id,
                "kind": "egress",
                "target_disposition": target_disposition,
                "rationale": (
                    "FW-F 必须以目标 stable 证据决定 strict 范围；当前仅保存未批准目标。"
                    if kind == "inference"
                    else "辅助／生命周期路径继续采用显式 managed 策略，除非后继证据批准晋升。"
                ),
            }
        )
    if len(identities) != len(set(identities)):
        raise SealPlanError("FW-E egress 身份重复")
    return (
        sorted(observed, key=lambda row: row["egress_id"]),
        sorted(dispositions, key=lambda row: row["egress_id"]),
        sorted(proposals, key=lambda row: row["id"]),
        sorted(source_paths),
    )


def ingress_proposals(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成不覆盖当前事实的 FW-F 入口目标提案。"""

    proposals: list[dict[str, Any]] = []
    for entry in entries:
        logical_id = str(entry["logical_ingress_id"])
        if entry["current_disposition"] == "rerouted":
            target = "rerouted"
            rationale = "该物理入口属于 Codex 产品路径，继续与 Claude Persona 隔离。"
        elif logical_id == "count-tokens-oauth":
            target = "retained_legacy"
            rationale = "count_tokens 在取得独立官方 wire 证据前继续保留受管遗留路径。"
        else:
            target = "migrated_strict"
            rationale = "仅当 FW-F 证明无损映射并批准 SupportEnvelope 后才允许迁移 strict。"
        proposals.append(
            {
                "id": logical_id,
                "kind": "ingress",
                "target_disposition": target,
                "rationale": rationale,
            }
        )
    return sorted(proposals, key=lambda row: row["id"])


def build_seal_plan(
    *,
    workspace_root: Path,
    campaign_id: str,
    source_commit: str,
    freeze_root: Path,
    rule_assessments_path: Path,
    target_inventory_paths: list[Path],
    cross_source_matrix_path: Path,
    completeness_closure_path: Path,
    capture_index_path: Path,
    ingress_catalog_path: Path,
    egress_catalog_path: Path,
    contract_source_paths: list[Path],
    fw_c_receipt_paths: list[Path],
    runtime_catalog_paths: list[Path],
    inventory_evidence_paths: list[Path],
    platforms: list[str],
    created_at_utc: str,
    discovered_at_utc: str,
    inventory_observed_at_utc: str,
    output_path: Path,
) -> dict[str, Any]:
    """构造完整但未批准的 FW-E seal plan。"""

    for value, label in (
        (created_at_utc, "created_at_utc"),
        (discovered_at_utc, "discovered_at_utc"),
        (inventory_observed_at_utc, "inventory_observed_at_utc"),
    ):
        try:
            expect_rfc3339(value, label)
        except (ValueError, ControlError) as error:
            raise SealPlanError(str(error)) from error
    if len(source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise SealPlanError("source_commit 必须是完整小写 Git commit")
    freeze_path = freeze_root / "freeze.json"
    freeze = load_json(freeze_path, "FW-E official freeze")
    if freeze.get("schema_version") != FREEZE_SCHEMA:
        raise SealPlanError("FW-E official freeze Schema 不匹配")
    target_version = freeze.get("target_version")
    if not isinstance(target_version, str) or freeze.get("stable_at_start") != target_version or freeze.get("stable_at_end") != target_version:
        raise SealPlanError("FW-E official stable 身份不闭合")
    if not set(platforms).issubset(set(freeze.get("platforms", []))):
        raise SealPlanError("seal plan 平台超出官方冻结范围")
    assessments = load_json(rule_assessments_path, "FW-E rule assessments")
    rules = assessments.get("rules")
    if (
        assessments.get("schema_version") != ASSESSMENTS_SCHEMA
        or assessments.get("target_version") != target_version
        or not isinstance(rules, list)
        or assessments.get("rule_count") != len(rules)
    ):
        raise SealPlanError("FW-E rule assessments 不闭合")
    spec_ids = [row.get("spec_id") for row in rules if isinstance(row, dict)]
    if len(spec_ids) != len(rules) or spec_ids != sorted(set(spec_ids)):
        raise SealPlanError("FW-E rule assessments 身份未排序或重复")

    aliases, ingress_entries, ingress_sources = validate_ingress_catalog(
        workspace_root, ingress_catalog_path
    )
    (
        egress_observed,
        egress_entries,
        egress_targets,
        egress_sources,
    ) = build_egress_inventory(workspace_root, egress_catalog_path)
    proposals = sorted(
        ingress_proposals(ingress_entries) + egress_targets,
        key=lambda row: row["id"],
    )
    proposal_ids = [row["id"] for row in proposals]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise SealPlanError("FW-E target proposal 身份冲突")

    artifact_paths = [freeze_path, freeze_root / str(freeze["registry_snapshot_path"])]
    artifacts = freeze.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise SealPlanError("FW-E official freeze 缺少 artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(
            artifact.get("tarball_path"), str
        ):
            raise SealPlanError("FW-E official artifact 条目非法")
        artifact_paths.append(freeze_root / artifact["tarball_path"])

    inventory_paths = [
        ingress_catalog_path,
        egress_catalog_path,
        *ingress_sources,
        *egress_sources,
        *inventory_evidence_paths,
    ]
    plan = {
        "schema_version": PLAN_SCHEMA,
        "campaign_id": campaign_id,
        "persona": {
            "provider": "anthropic",
            "official_product": "claude-code",
            "auth_family": "oauth",
            "upstream_route_family": "anthropic-api",
        },
        "target_version": target_version,
        "platforms": sorted(set(platforms)),
        "entrypoints": [str(freeze.get("entrypoint"))],
        "default_conditions": sorted(str(item) for item in freeze.get("default_conditions", [])),
        "traffic_observation_policy": {
            "traffic_presence_comparison": "disabled",
            "strict_wire_traffic_classes": ["essential"],
            "record_only_traffic_classes": ["nonessential", "telemetry"],
            "absence_of_record_only_traffic": "conformant_not_a_difference",
        },
        "created_at_utc": created_at_utc,
        "discovered_at_utc": discovered_at_utc,
        "discovery_source": "official-npm-stable",
        "source_commit": source_commit,
        "contract_source_paths": unique_relatives(
            workspace_root, contract_source_paths, "contract source"
        ),
        "fw_c_receipt_paths": unique_relatives(
            workspace_root, fw_c_receipt_paths, "FW-C receipt"
        ),
        "runtime_catalog_paths": unique_relatives(
            workspace_root, runtime_catalog_paths, "runtime catalog"
        ),
        "official_artifact_paths": unique_relatives(
            workspace_root, artifact_paths, "official artifact"
        ),
        "target_sink_inventory_paths": unique_relatives(
            workspace_root, target_inventory_paths, "target sink inventory"
        ),
        "cross_source_matrix_path": relative_file(
            workspace_root, cross_source_matrix_path, "cross-source matrix"
        ),
        "completeness_closure_path": relative_file(
            workspace_root, completeness_closure_path, "completeness closure"
        ),
        "capture_index_path": relative_file(
            workspace_root, capture_index_path, "capture index"
        ),
        "rules": rules,
        "inventory_observed_at_utc": inventory_observed_at_utc,
        "inventory_evidence_paths": unique_relatives(
            workspace_root, inventory_paths, "inventory evidence"
        ),
        "ingress_aliases": aliases,
        "ingress_entries": ingress_entries,
        "egress_observed": egress_observed,
        "egress_entries": egress_entries,
        "target_proposals": proposals,
    }
    write_private_json(output_path, plan)
    return plan


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--freeze-root", required=True, type=Path)
    parser.add_argument("--rule-assessments", required=True, type=Path)
    parser.add_argument("--target-inventory", required=True, action="append", type=Path)
    parser.add_argument("--cross-source-matrix", required=True, type=Path)
    parser.add_argument("--completeness-closure", required=True, type=Path)
    parser.add_argument("--capture-index", required=True, type=Path)
    parser.add_argument("--ingress-catalog", required=True, type=Path)
    parser.add_argument("--egress-catalog", required=True, type=Path)
    parser.add_argument("--contract-source", required=True, action="append", type=Path)
    parser.add_argument("--fw-c-receipt", required=True, action="append", type=Path)
    parser.add_argument("--runtime-catalog", required=True, action="append", type=Path)
    parser.add_argument("--inventory-evidence", action="append", default=[], type=Path)
    parser.add_argument("--platform", required=True, action="append")
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--discovered-at", required=True)
    parser.add_argument("--inventory-observed-at", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    """运行 FW-E seal plan 生成。"""

    arguments = build_parser().parse_args()
    try:
        plan = build_seal_plan(
            workspace_root=arguments.workspace_root,
            campaign_id=arguments.campaign_id,
            source_commit=arguments.source_commit,
            freeze_root=arguments.freeze_root,
            rule_assessments_path=arguments.rule_assessments,
            target_inventory_paths=arguments.target_inventory,
            cross_source_matrix_path=arguments.cross_source_matrix,
            completeness_closure_path=arguments.completeness_closure,
            capture_index_path=arguments.capture_index,
            ingress_catalog_path=arguments.ingress_catalog,
            egress_catalog_path=arguments.egress_catalog,
            contract_source_paths=arguments.contract_source,
            fw_c_receipt_paths=arguments.fw_c_receipt,
            runtime_catalog_paths=arguments.runtime_catalog,
            inventory_evidence_paths=arguments.inventory_evidence,
            platforms=arguments.platform,
            created_at_utc=arguments.created_at,
            discovered_at_utc=arguments.discovered_at,
            inventory_observed_at_utc=arguments.inventory_observed_at,
            output_path=arguments.output,
        )
    except (SealPlanError, OSError, ValueError) as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1
    print(
        "FW-E seal plan 已生成："
        f"rules={len(plan['rules'])} ingress={len(plan['ingress_entries'])} "
        f"egress={len(plan['egress_entries'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
