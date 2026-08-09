#!/usr/bin/env python3
"""ACC-03 seal 断言门禁：在封存前把 accept 的证据前提全部失败关闭。

§10.8.6 的根因是 seal 不校验场景 artifact 覆盖，一份只登记 6 个 pcap 的
manifest 也能封存，缺陷拖到 accept 才暴露。本门禁在 seal 时按顺序执行：

1. **bundle provenance 重放**（ACC-02）：只读复制逐项摘要一致，bundle 内无
   未登记文件；
2. **派生收据重放**（ACC-02b，存在派生目录时）：同输入逐字节重现派生产物；
3. **manifest 解析**：capture manifest 必须位于 bundle 根，全部 artifact 摘要
   一致、解析非空、structured trace 来源闭合（复用断言器同一实现）；
4. **分侧 kind 覆盖**：按 ACC-01 分侧矩阵校验场景×artifact kind，缺失即拒绝；
5. **wire 观测互斥**：产 wire record 的 observation artifact，其
   ``source_artifacts`` 指向的原件必须以 ``opaque_bound_source`` 登记——同一
   字节流严禁既被直接解析又被派生解析，防止计数类判据双计数；
6. **selector 命中预检**：本侧应执行的每条规则的每个 check，其 select 必须
   至少命中一条观测——标签语义错位（k34 的 ``transport: direct``）、证据缺失
   都在此暴露，不再等到 accept 的 ``actual=[]``。

门禁只做存在性与一致性预检，不评估 assertion 判据——通过与否仍由 accept 的
离线重放决定。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.acceptance_contract import (  # noqa: E402
    MODE_DUAL_WIRE,
    WIRE_RECORD_TYPES,
    contract_sha256,
)
from tools.official_client_capture.build_assertion_bundle import (  # noqa: E402
    AssertionBundleError,
    PROVENANCE_FILENAME,
    verify_bundle,
    verify_manifest_kind_coverage,
)
from tools.official_client_capture.candidate_rule_assertion import (  # noqa: E402
    AssertionConfigurationError,
    _select_observations,
    load_observations,
)
from tools.official_client_capture.derive_official_observations import (  # noqa: E402
    DERIVED_PREFIX,
    ObservationDerivationError,
    verify_derivation,
)

BUNDLE_DIR_NAME = "assertion-bundle"
MANIFEST_FILENAME = "capture-manifest.json"
OBSERVATION_PARSERS = frozenset({"observation_json", "observation_jsonl"})
SIDES = frozenset({"official", "candidate"})


class AssertionGateError(RuntimeError):
    """seal 断言门禁失败，禁止封存。"""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gate_wrap(error: Exception, stage: str) -> AssertionGateError:
    return AssertionGateError(f"{stage}：{error}")


def _verify_wire_observation_exclusivity(
    manifest: Mapping[str, Any],
    observations: list[Any],
) -> None:
    parser_by_path = {
        artifact["path"]: artifact["parser"]
        for artifact in manifest["artifacts"]
    }
    for observation in observations:
        if observation.record_type not in WIRE_RECORD_TYPES:
            continue
        if parser_by_path.get(observation.artifact_path) not in OBSERVATION_PARSERS:
            continue
        for source in observation.evidence_paths[1:]:
            source_parser = parser_by_path.get(source)
            if source_parser != "opaque_bound_source":
                raise AssertionGateError(
                    "wire 观测的来源必须以 opaque_bound_source 登记，"
                    f"否则同一字节流会被双重解析计数：{source}"
                    f"（当前 parser={source_parser}）"
                )


def _verify_selector_reachability(
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    observations: list[Any],
    side: str,
) -> tuple[int, int]:
    modes = contract["validation_modes"]
    checked_rules = 0
    checked_checks = 0
    for rule in profile["rules"]:
        rule_id = rule["rule_id"]
        if side == "official" and modes[rule_id] != MODE_DUAL_WIRE:
            continue
        checked_rules += 1
        for check in rule["checks"]:
            matched = _select_observations(
                observations, check["select"], rule["scenario_ids"]
            )
            if not matched:
                raise AssertionGateError(
                    f"seal 预检：规则 {rule_id} 的 check {check['id']} 在"
                    f"{side} 侧无法命中任何观测——证据缺失或标签语义错位"
                )
            checked_checks += 1
    if not checked_rules:
        raise AssertionGateError(f"{side} 侧没有任何应执行规则")
    return checked_rules, checked_checks


def run_assertion_gate(
    *,
    bundle_dir: Path,
    source_roots: Mapping[str, Path],
    side: str,
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    target_version: str,
) -> dict[str, Any]:
    """执行全部门禁并返回可封存的 gate 收据；任何一步失败即抛错。"""

    if side not in SIDES:
        raise AssertionGateError(f"未知验收侧：{side}")
    if bundle_dir.name != BUNDLE_DIR_NAME:
        raise AssertionGateError(
            f"断言证据包目录必须名为 {BUNDLE_DIR_NAME}：{bundle_dir}"
        )
    try:
        bundle_provenance = verify_bundle(
            source_roots,
            bundle_dir,
            allowed_extra_prefixes=(DERIVED_PREFIX, MANIFEST_FILENAME),
        )
    except AssertionBundleError as error:
        raise _gate_wrap(error, "bundle provenance 重放失败") from error
    # bundle 位于 attempt 证据根内，必须禁止把 bundle 自身的内容再收口一次：
    # 自引用会让 provenance 看似闭环，实际未追溯到任何原始 job 证据。
    for entry in bundle_provenance["entries"]:
        if f"/{BUNDLE_DIR_NAME}/" in f"/{entry['source_path']}":
            raise AssertionGateError(
                "断言证据包禁止收口自身内容，来源必须是原始采集证据："
                f"{entry['source_inventory_path']}"
            )
    derived_provenance_sha256 = None
    if (bundle_dir / DERIVED_PREFIX.rstrip("/")).exists():
        try:
            derived = verify_derivation(bundle_dir)
        except (ObservationDerivationError, AssertionBundleError) as error:
            raise _gate_wrap(error, "派生收据重放失败") from error
        derived_provenance_sha256 = derived["provenance_sha256"]
    manifest_path = bundle_dir / MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AssertionGateError(
            f"capture manifest 必须位于 bundle 根：{manifest_path}"
        )
    try:
        manifest, observations = load_observations(
            manifest_path, bundle_dir, target_version
        )
    except AssertionConfigurationError as error:
        raise _gate_wrap(error, "capture manifest 校验失败") from error
    side_coverage = contract["side_coverage"].get(side)
    if not isinstance(side_coverage, dict) or not side_coverage:
        raise AssertionGateError(f"验收契约缺少 {side} 侧覆盖矩阵")
    try:
        verify_manifest_kind_coverage(manifest, side_coverage)
    except AssertionBundleError as error:
        raise _gate_wrap(error, "场景 artifact 覆盖不足") from error
    _verify_wire_observation_exclusivity(manifest, observations)
    checked_rules, checked_checks = _verify_selector_reachability(
        profile, contract, observations, side
    )
    return {
        "side": side,
        "bundle_dir_name": BUNDLE_DIR_NAME,
        "bundle_provenance_sha256": bundle_provenance["provenance_sha256"],
        "bundle_entry_count": bundle_provenance["entry_count"],
        "derived_provenance_sha256": derived_provenance_sha256,
        "capture_manifest": {
            "path": MANIFEST_FILENAME,
            "sha256": _file_sha256(manifest_path),
        },
        "acceptance_contract_sha256": contract_sha256(contract),
        "artifact_count": len(manifest["artifacts"]),
        "observation_count": len(observations),
        "checked_rule_count": checked_rules,
        "checked_check_count": checked_checks,
    }


def validate_gate_receipt(value: Any, *, side: str) -> dict[str, Any]:
    """校验 stage 文档中封存的 gate 收据结构；供 stage 契约消费。"""

    required = {
        "side",
        "bundle_dir_name",
        "bundle_provenance_sha256",
        "bundle_entry_count",
        "derived_provenance_sha256",
        "capture_manifest",
        "acceptance_contract_sha256",
        "artifact_count",
        "observation_count",
        "checked_rule_count",
        "checked_check_count",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise AssertionGateError("assertion gate 收据字段不闭合")
    if value.get("side") != side:
        raise AssertionGateError("assertion gate 收据侧别不一致")
    if value.get("bundle_dir_name") != BUNDLE_DIR_NAME:
        raise AssertionGateError("assertion gate 收据 bundle 目录名非法")
    manifest_binding = value.get("capture_manifest")
    if (
        not isinstance(manifest_binding, dict)
        or set(manifest_binding) != {"path", "sha256"}
        or manifest_binding.get("path") != MANIFEST_FILENAME
    ):
        raise AssertionGateError("assertion gate 收据 manifest 绑定非法")
    for field in (
        "bundle_entry_count",
        "artifact_count",
        "observation_count",
        "checked_rule_count",
        "checked_check_count",
    ):
        if not isinstance(value.get(field), int) or value[field] <= 0:
            raise AssertionGateError(f"assertion gate 收据 {field} 非法")
    return value


def gate_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
