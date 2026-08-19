#!/usr/bin/env python3
"""冻结 Claude Code 2.1.226 完整取证 Campaign 的五类候选分母。

该工具只读取已经封存的 FW-E v3 输入，生成一个新的追加式分母账本。五类集合保持
正交：目标版本发送点、历史源码机制、HitCC 文档线索、历史规则和语义候选族不能
彼此替代，也不能通过聚类减少逐项处置责任。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.claude_fw_f_v4 import (
    catalog_document,
)
from tools.official_client_capture.capturelib.identity import (
    capture_source_bundle_identity,
)


SCHEMA_VERSION = "claude-code-fw-e-complete-candidate-denominator/v1"
CAMPAIGN_SCHEMA_VERSION = "claude-code-fw-e-f-complete-campaign/v1"
TARGET_VERSION = "2.1.226"
EXPECTED_COUNTS = {
    "target_send_points": 331,
    "source_mechanisms_2_1_88": 102,
    "hitcc_documents_2_1_197": 71,
    "historical_rules": 57,
    "semantic_candidate_families": 32,
}

MATRIX_PATH = Path(
    "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/"
    "crosswalk-v8-semantic-closed-e577e144a/matrix.json"
)
DISCOVERY_PATH = Path(
    "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/"
    "semantic-closure-v1-e577e144a/discovery-inventory.json"
)
SEMANTIC_PATH = Path(
    "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/"
    "semantic-closure-v1-e577e144a/semantic-candidates.json"
)
SOURCE_PATH = Path(
    "tools/official_client_capture/claude_21220/source_2_1_88_coverage.json"
)
TOOL_PATH = Path(
    "tools/official_client_capture/claude_fw_e_complete_campaign.py"
)
FIXED_HISTORICAL_ROOTS = (
    Path("local-analysis/fw-f/claude-code-2.1.226/profile-approval-v2"),
    Path("local-analysis/fw-f/claude-code-2.1.226/profile-approval-v3"),
    Path("local-analysis/fw-f/claude-code-2.1.226/profile-v3-c91eb9acc776"),
)
COMPLETE_HISTORY_ROOT = Path("local-analysis/fw-f/claude-code-2.1.226")
COMPLETE_HISTORY_RE = re.compile(r"^complete-v[0-9]+-[a-f0-9]{12}$")


class CompleteCampaignError(RuntimeError):
    """表示完整候选分母发生缺失、漂移或覆盖。"""


def _historical_roots(repo_root: Path, output_dir: Path) -> tuple[Path, ...]:
    """自动纳入全部既有完整 Campaign，避免新增失败事实被后继冻结遗漏。"""

    roots = list(FIXED_HISTORICAL_ROOTS)
    complete_root = repo_root / COMPLETE_HISTORY_ROOT
    if not complete_root.is_dir() or complete_root.is_symlink():
        raise CompleteCampaignError("完整 Campaign 历史根不存在或不可信。")
    output_resolved = output_dir.resolve(strict=False)
    for path in sorted(complete_root.iterdir(), key=lambda item: item.name):
        if (
            path.is_dir()
            and not path.is_symlink()
            and COMPLETE_HISTORY_RE.fullmatch(path.name)
            and path.resolve() != output_resolved
        ):
            roots.append(path.relative_to(repo_root))
    if len(roots) != len(set(roots)):
        raise CompleteCampaignError("历史证据根存在重复。")
    return tuple(roots)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CompleteCampaignError(f"{label} 不是可信绝对普通文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompleteCampaignError(f"{label} 不是合法 JSON：{path}") from error
    if not isinstance(value, dict):
        raise CompleteCampaignError(f"{label} 顶层必须是对象。")
    return value


def _binding(repo_root: Path, relative: Path) -> dict[str, Any]:
    absolute = (repo_root / relative).resolve()
    if not absolute.is_relative_to(repo_root):
        raise CompleteCampaignError(f"输入越出仓库：{relative}")
    return {
        "path": relative.as_posix(),
        "sha256": _sha256_bytes(absolute.read_bytes()),
    }


def _tree_binding(repo_root: Path, relative: Path) -> dict[str, Any]:
    """对历史目录生成路径／内容清单摘要，但不复制或改写历史证据。"""

    absolute = (repo_root / relative).resolve()
    if not absolute.is_relative_to(repo_root) or absolute.is_symlink() or not absolute.is_dir():
        raise CompleteCampaignError(f"历史证据目录不可复核：{relative}")
    rows: list[dict[str, Any]] = []
    for path in sorted(absolute.rglob("*")):
        if path.is_symlink():
            raise CompleteCampaignError(f"历史证据含符号链接：{path}")
        if not path.is_file():
            continue
        rows.append(
            {
                "path": path.relative_to(absolute).as_posix(),
                "sha256": _sha256_bytes(path.read_bytes()),
                "bytes": path.stat().st_size,
            }
        )
    if not rows:
        raise CompleteCampaignError(f"历史证据目录为空：{relative}")
    return {
        "path": relative.as_posix(),
        "file_count": len(rows),
        "inventory_sha256": _sha256_bytes(_canonical_bytes(rows)),
        "preservation": "read_only_not_overwritten",
    }


def _require_unique(items: Iterable[dict[str, Any]], key: str, label: str) -> None:
    values = [str(item.get(key, "")) for item in items]
    if any(not value for value in values):
        raise CompleteCampaignError(f"{label} 存在空 {key}。")
    if len(values) != len(set(values)):
        raise CompleteCampaignError(f"{label} 存在重复 {key}。")


def _target_send_points(discovery: dict[str, Any]) -> list[dict[str, Any]]:
    raw = discovery.get("items")
    if not isinstance(raw, list):
        raise CompleteCampaignError("DiscoveryInventory.items 缺失。")
    selected = [
        item
        for item in raw
        if isinstance(item, dict) and item.get("source_kind") == "target_ast_call"
    ]
    result = [
        {
            "candidate_id": str(item.get("discovery_id", "")),
            "proposition": str(item.get("proposition", "")),
            "semantic_candidate_ids": sorted(
                str(value) for value in item.get("semantic_candidate_ids", [])
            ),
            "source_evidence_paths": sorted(
                str(value) for value in item.get("evidence_paths", [])
            ),
            "required_target_conclusion": True,
            "target_measurement_status": "pending",
        }
        for item in selected
    ]
    _require_unique(result, "candidate_id", "目标发送点")
    return sorted(result, key=lambda item: item["candidate_id"])


def _source_mechanisms(source: dict[str, Any]) -> list[dict[str, Any]]:
    raw = source.get("rules")
    if not isinstance(raw, list):
        raise CompleteCampaignError("2.1.88 源码机制集合缺失。")
    result = [
        {
            "candidate_id": str(item.get("source_rule_id", "")),
            "proposition": str(item.get("proposition", "")),
            "source_paths": sorted(str(value) for value in item.get("source_paths", [])),
            "historical_spec_ids": sorted(
                str(value) for value in item.get("spec_rule_ids", [])
            ),
            "required_target_conclusion": True,
            "target_measurement_status": "pending",
        }
        for item in raw
        if isinstance(item, dict)
    ]
    _require_unique(result, "candidate_id", "2.1.88 源码机制")
    return sorted(result, key=lambda item: item["candidate_id"])


def _hitcc_documents(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    raw = matrix.get("hitcc_documents")
    if not isinstance(raw, list):
        raise CompleteCampaignError("HitCC 文档线索集合缺失。")
    result = [
        {
            "candidate_id": f"HITCC-DOC-{index:03d}",
            "path": str(item.get("path", "")),
            "clue_ids": sorted(str(value) for value in item.get("clue_ids", [])),
            "historical_spec_ids": sorted(
                str(value) for value in item.get("spec_ids", [])
            ),
            "semantic_candidate_ids": sorted(
                str(value) for value in item.get("candidate_ids", [])
            ),
            "required_target_conclusion": True,
            "target_measurement_status": "pending",
        }
        for index, item in enumerate(
            sorted(
                (item for item in raw if isinstance(item, dict)),
                key=lambda item: str(item.get("path", "")),
            ),
            start=1,
        )
    ]
    _require_unique(result, "candidate_id", "HitCC 文档线索")
    paths = [item["path"] for item in result]
    if any(not path for path in paths) or len(paths) != len(set(paths)):
        raise CompleteCampaignError("HitCC 文档路径为空或重复。")
    return result


def _historical_rules(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    raw = matrix.get("target_rules")
    if not isinstance(raw, list):
        raise CompleteCampaignError("历史规则集合缺失。")
    result = [
        {
            "candidate_id": str(item.get("id", "")),
            "domain": str(item.get("domain", "")),
            "proposition": str(item.get("retained_claim", "")),
            "scope": str(item.get("scope", "")),
            "historical_evidence_level": str(item.get("baseline_disposition", "")),
            "required_target_conclusion": True,
            "target_measurement_status": "pending",
        }
        for item in raw
        if isinstance(item, dict)
    ]
    _require_unique(result, "candidate_id", "历史规则")
    return sorted(result, key=lambda item: item["candidate_id"])


def _semantic_families(semantic: dict[str, Any]) -> list[dict[str, Any]]:
    raw = semantic.get("candidates")
    if not isinstance(raw, list):
        raise CompleteCampaignError("语义候选族集合缺失。")
    result = [
        {
            "candidate_id": str(item.get("id", "")),
            "domain": str(item.get("domain", "")),
            "candidate_kind": str(item.get("candidate_kind", "")),
            "proposition": str(item.get("retained_claim", "")),
            "scope": str(item.get("scope", "")),
            "required_channels": sorted(
                str(value) for value in item.get("required_channels", [])
            ),
            "source_ids": sorted(str(value) for value in item.get("source_ids", [])),
            "required_target_conclusion": True,
            "target_measurement_status": "pending",
        }
        for item in raw
        if isinstance(item, dict)
    ]
    _require_unique(result, "candidate_id", "语义候选族")
    return sorted(result, key=lambda item: item["candidate_id"])


def validate_denominator(value: dict[str, Any]) -> None:
    """验证五类分母、稳定身份和禁止偷换范围的硬门禁。"""

    if value.get("schema_version") != SCHEMA_VERSION:
        raise CompleteCampaignError("候选分母 schema 不匹配。")
    if value.get("target_version") != TARGET_VERSION:
        raise CompleteCampaignError("目标版本不是 2.1.226。")
    groups = value.get("candidate_groups")
    if not isinstance(groups, dict) or set(groups) != set(EXPECTED_COUNTS):
        raise CompleteCampaignError("候选分组集合发生漂移。")
    counts: dict[str, int] = {}
    global_ids: set[str] = set()
    for group, expected in EXPECTED_COUNTS.items():
        items = groups.get(group)
        if not isinstance(items, list):
            raise CompleteCampaignError(f"{group} 不是数组。")
        _require_unique(items, "candidate_id", group)
        if len(items) != expected:
            raise CompleteCampaignError(
                f"{group} 分母应为 {expected}，实际为 {len(items)}。"
            )
        counts[group] = len(items)
        namespaced = {f"{group}:{item['candidate_id']}" for item in items}
        if global_ids & namespaced:
            raise CompleteCampaignError(f"{group} 命名空间内存在重复身份。")
        global_ids.update(namespaced)
        if any(item.get("required_target_conclusion") is not True for item in items):
            raise CompleteCampaignError(f"{group} 存在不要求目标实测结论的候选。")
    if value.get("counts") != counts:
        raise CompleteCampaignError("候选分母计数投影不一致。")
    if value.get("total_orthogonal_candidates") != sum(EXPECTED_COUNTS.values()):
        raise CompleteCampaignError("正交候选总数不一致。")
    policy = value.get("closure_policy")
    if not isinstance(policy, dict):
        raise CompleteCampaignError("闭合策略缺失。")
    if policy.get("allow_unmeasured_feature_boundary") is not False:
        raise CompleteCampaignError("禁止允许 unmeasured_feature_boundary。")
    if policy.get("telemetry_absence_generates_rule") is not False:
        raise CompleteCampaignError("关闭遥测不得生成零流量规则。")
    if policy.get("support_envelope_reduction_closes_candidate") is not False:
        raise CompleteCampaignError("禁止通过缩小 SupportEnvelope 闭合候选。")


def build_denominator(repo_root: Path, campaign_id: str) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    matrix = _load_json((repo_root / MATRIX_PATH).resolve(), "跨来源矩阵")
    discovery = _load_json((repo_root / DISCOVERY_PATH).resolve(), "DiscoveryInventory")
    semantic = _load_json((repo_root / SEMANTIC_PATH).resolve(), "语义候选族")
    source = _load_json((repo_root / SOURCE_PATH).resolve(), "2.1.88 源码机制")
    groups = {
        "target_send_points": _target_send_points(discovery),
        "source_mechanisms_2_1_88": _source_mechanisms(source),
        "hitcc_documents_2_1_197": _hitcc_documents(matrix),
        "historical_rules": _historical_rules(matrix),
        "semantic_candidate_families": _semantic_families(semantic),
    }
    counts = {name: len(items) for name, items in groups.items()}
    result = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "target_version": TARGET_VERSION,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "source_bindings": {
            "producer": _binding(repo_root, TOOL_PATH),
            "cross_source_matrix": _binding(repo_root, MATRIX_PATH),
            "discovery_inventory": _binding(repo_root, DISCOVERY_PATH),
            "semantic_candidates": _binding(repo_root, SEMANTIC_PATH),
            "source_2_1_88_coverage": _binding(repo_root, SOURCE_PATH),
        },
        "counts": counts,
        "total_orthogonal_candidates": sum(counts.values()),
        "candidate_groups": groups,
        "closure_policy": {
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
        },
    }
    result["candidate_groups_sha256"] = _sha256_bytes(_canonical_bytes(groups))
    validate_denominator(result)
    return result


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise CompleteCampaignError("--output 必须是非符号链接绝对路径。")
    if path.exists():
        raise CompleteCampaignError("输出已存在，拒绝覆盖历史账本。")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def freeze_campaign(repo_root: Path, campaign_id: str, output_dir: Path) -> dict[str, Any]:
    """原子冻结完整分母、真实场景目录和历史证据只读绑定。"""

    repo_root = repo_root.resolve()
    if not output_dir.is_absolute() or output_dir.is_symlink():
        raise CompleteCampaignError("--output-dir 必须是非符号链接绝对路径。")
    if output_dir.exists():
        raise CompleteCampaignError("Campaign 输出目录已存在，拒绝覆盖历史证据。")
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=parent))
    temporary.chmod(0o700)
    try:
        live_source_root = repo_root / "tools/official_client_capture"
        if live_source_root.is_symlink() or not live_source_root.is_dir():
            raise CompleteCampaignError("抓包执行源目录不存在或不可信。")
        symlinks = [path for path in live_source_root.rglob("*") if path.is_symlink()]
        if symlinks:
            raise CompleteCampaignError("抓包执行源含符号链接，拒绝冻结。")
        frozen_source_root = temporary / "source/tools/official_client_capture"
        shutil.copytree(
            live_source_root,
            frozen_source_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        live_source_binding = capture_source_bundle_identity(live_source_root)
        frozen_source_binding = capture_source_bundle_identity(frozen_source_root)
        if frozen_source_binding != live_source_binding:
            raise CompleteCampaignError("冻结执行源与工作区执行源摘要不一致。")
        denominator = build_denominator(repo_root, campaign_id)
        catalog = catalog_document()
        catalog["campaign_id"] = campaign_id
        denominator_path = temporary / "candidate-denominator.json"
        catalog_path = temporary / "scenario-catalog.json"
        _write_new_json(denominator_path, denominator)
        _write_new_json(catalog_path, catalog)
        history = [
            _tree_binding(repo_root, path)
            for path in _historical_roots(repo_root, output_dir)
        ]
        manifest = {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "target_version": TARGET_VERSION,
            "status": "frozen_not_executed",
            "created_at_utc": dt.datetime.now(dt.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "candidate_denominator": {
                "path": denominator_path.name,
                "sha256": _sha256_bytes(denominator_path.read_bytes()),
                "counts": denominator["counts"],
                "total": denominator["total_orthogonal_candidates"],
            },
            "scenario_catalog": {
                "path": catalog_path.name,
                "sha256": _sha256_bytes(catalog_path.read_bytes()),
                "probe_count": catalog["probe_count"],
                "required_matrix_dimension_count": catalog[
                    "required_matrix_dimension_count"
                ],
            },
            "capture_source_bundle": frozen_source_binding,
            "historical_evidence": history,
            "execution_policy": {
                "attempts_are_append_only": True,
                "failed_attempts_are_preserved": True,
                "production_container_or_network_mutation_allowed": False,
                "existing_capture_cli_reuse_allowed": False,
                "isolated_temporary_container_required": True,
                "all_candidates_require_target_measurement": True,
                "unmeasured_feature_boundary_allowed": False,
                "telemetry_or_nonessential_absence_generates_rule": False,
            },
        }
        _write_new_json(temporary / "campaign.json", manifest)
        os.rename(temporary, output_dir)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    outputs = parser.add_mutually_exclusive_group(required=True)
    outputs.add_argument("--output", type=Path)
    outputs.add_argument("--output-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _parser().parse_args(argv)
    try:
        if arguments.output_dir is not None:
            manifest = freeze_campaign(
                arguments.repo_root,
                arguments.campaign_id,
                arguments.output_dir,
            )
            value = {
                "counts": manifest["candidate_denominator"]["counts"],
                "probe_count": manifest["scenario_catalog"]["probe_count"],
                "result": "passed",
            }
        else:
            value = build_denominator(arguments.repo_root, arguments.campaign_id)
            _write_new_json(arguments.output, value)
    except (CompleteCampaignError, OSError) as error:
        print(f"Claude FW-E 完整分母拒绝：{error}", file=sys.stderr)
        return 2
    print(json.dumps({"counts": value["counts"], "probe_count": value.get("probe_count"), "result": "passed"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
