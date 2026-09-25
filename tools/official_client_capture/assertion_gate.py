#!/usr/bin/env python3
"""ACC-03 seal 断言门禁：在封存前把 accept 的证据前提全部失败关闭。

此前 seal 不校验场景 artifact 覆盖，一份只登记 6 个 pcap 的
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
   都在此暴露，不再等到 accept 的 ``actual=[]``。唯一例外是官方侧 seal 遇到
   目标版本整体删除的端点（select 以 ``data.path`` 钉死、该路径在全部官方观测中
   零出现）：这类 check 登记为延后项写入收据，由 VC-2 批准画像裁决。

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
    check_applies_to_side,
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

# 候选侧 internal record type 的唯一来源目录：``candidate_test_trace.py`` 把
# 部署源码快照的 go test -json 事实按冻结映射投影到这里。它与 ``derived/`` 同类
# ——都是 bundle 内的派生产物，不由 ACC-02 收口 provenance 覆盖，而由各自的收据
# 自证。因此这里放行前缀，同时由 ``_verify_candidate_trace`` 强制重放其收据。
CANDIDATE_TRACE_PREFIX = "candidate-trace/"
TRACE_RECEIPT_RELATIVE_PATH = f"{CANDIDATE_TRACE_PREFIX}trace-receipt.json"
TRACE_RECEIPT_SCHEMA = "codex-candidate-test-trace-receipt/v1"
# 官方侧 seal 因目标版本整体删除端点而延后裁决的 check（见
# ``_verify_selector_reachability``）；只在非空时写入 gate 收据。
DEFERRED_UNREACHABLE_FIELD = "deferred_unreachable_checks"


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


def _verify_candidate_trace(bundle_dir: Path, manifest: Mapping[str, Any]) -> str | None:
    """重放候选结构化 trace 的收据；无该目录时返回 None。

    ``candidate-trace/`` 被排除在收口 provenance 之外，若不另行校验就等于在 bundle
    里开了一个不受约束的目录。这里强制四件事：收据存在且 schema 正确、状态为
    ``pass``、它声明的每份 trace 产物在 bundle 内摘要逐字一致、且这些产物都已登记进
    capture manifest。go test 日志本身的绑定由 ``candidate_test_trace`` 在生成时校验，
    其摘要随收据一并封存。
    """

    trace_dir = bundle_dir / CANDIDATE_TRACE_PREFIX.rstrip("/")
    if not trace_dir.exists():
        return None
    if trace_dir.is_symlink() or not trace_dir.is_dir():
        raise AssertionGateError(f"候选 trace 目录不可信：{trace_dir}")
    receipt_path = bundle_dir / TRACE_RECEIPT_RELATIVE_PATH
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise AssertionGateError(
            f"候选结构化 trace 缺少自证收据：{TRACE_RECEIPT_RELATIVE_PATH}"
        )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AssertionGateError(f"候选 trace 收据无法解析：{error}") from error
    if not isinstance(receipt, dict) or receipt.get("schema_version") != TRACE_RECEIPT_SCHEMA:
        raise AssertionGateError(
            f"候选 trace 收据 schema_version 必须是 {TRACE_RECEIPT_SCHEMA}"
        )
    if receipt.get("status") != "pass":
        raise AssertionGateError(
            f"候选 trace 收据状态不是 pass：{receipt.get('status')!r}"
        )
    generated = receipt.get("generated")
    if not isinstance(generated, dict):
        raise AssertionGateError("候选 trace 收据缺少 generated 段")
    trace_artifacts = generated.get("trace_artifacts")
    if not isinstance(trace_artifacts, list) or not trace_artifacts:
        raise AssertionGateError("候选 trace 收据未声明任何 trace 产物")

    manifest_digests = {
        artifact["path"]: artifact["sha256"] for artifact in manifest["artifacts"]
    }
    declared: set[str] = set()
    for artifact in trace_artifacts:
        if not isinstance(artifact, dict):
            raise AssertionGateError("候选 trace 产物条目必须是对象")
        relative = artifact.get("path")
        digest = artifact.get("sha256")
        if not isinstance(relative, str) or not relative.startswith(
            CANDIDATE_TRACE_PREFIX
        ):
            raise AssertionGateError(
                f"候选 trace 产物路径必须位于 {CANDIDATE_TRACE_PREFIX}：{relative!r}"
            )
        path = bundle_dir / relative
        if path.is_symlink() or not path.is_file():
            raise AssertionGateError(f"候选 trace 收据引用的产物不存在：{relative}")
        actual = _file_sha256(path)
        if actual != digest:
            raise AssertionGateError(
                f"候选 trace 产物相对收据发生漂移：{relative}"
            )
        if manifest_digests.get(relative) != actual:
            raise AssertionGateError(
                "候选 trace 产物未按同一摘要登记进 capture manifest："
                f"{relative}"
            )
        declared.add(relative)

    # 目录内不得存在收据未声明的额外文件——否则等于绕过 provenance 夹带证据。
    allowed = declared | {TRACE_RECEIPT_RELATIVE_PATH}
    manifest_binding = generated.get("capture_manifest")
    if isinstance(manifest_binding, dict) and isinstance(
        manifest_binding.get("path"), str
    ):
        allowed.add(manifest_binding["path"])
    for path in sorted(trace_dir.rglob("*")):
        if path.is_symlink():
            raise AssertionGateError(f"候选 trace 目录禁止符号链接：{path}")
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_dir).as_posix()
        if relative not in allowed:
            raise AssertionGateError(
                f"候选 trace 目录存在收据未声明的文件：{relative}"
            )
    return _file_sha256(receipt_path)


def _pinned_endpoint_paths(selector: Mapping[str, Any]) -> list[str]:
    """返回 select.where 用 ``data.path`` 的 equal／in 条件钉死的端点路径；未钉死返回空列表。"""

    paths: set[str] = set()
    for condition in selector.get("where") or []:
        if not isinstance(condition, Mapping) or condition.get("path") != "data.path":
            continue
        operator = condition.get("operator")
        value = condition.get("value")
        if operator == "equal" and isinstance(value, str) and value:
            paths.add(value)
        elif (
            operator == "in"
            and isinstance(value, list)
            and value
            and all(isinstance(item, str) and item for item in value)
        ):
            paths.update(value)
    return sorted(paths)


def _verify_selector_reachability(
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    observations: list[Any],
    side: str,
    *,
    defer_absent_endpoints: bool = False,
) -> tuple[int, int, list[dict[str, Any]]]:
    """逐 check 预检 select 至少命中一条观测，返回（规则数、check 数、延后项）。

    ``defer_absent_endpoints`` 只供官方侧 seal 使用：官方 seal 在 classify 之前执行，
    只能拿仓库冻结画像（基线行为）做预检。目标版本整体删除某个端点时（0.156.1 删除
    legacy compact：``/backend-api/codex/responses/compact``），冻结画像里钉死该路径的
    check 在官方证据上结构性不可达，强制命中会让 VC-1 永远无法封存。这类未命中同时满足
    下列条件时才延后裁决，逐条写入延后项：

    1. check 的 select.where 以 ``data.path`` 的 equal／in 条件钉死端点路径；
    2. 这些路径在**全部**官方观测（不分场景、记录类型与标签）中零出现——端点在目标
       版本官方流量里整体缺席，不可能是标签语义错位或单个场景漏采。

    其余未命中（标签错位、场景漏采、端点只在部分场景缺失）仍当场失败。延后项计入已
    核对的 check 数，但由 VC-2 批准画像裁决：批准画像若仍保留该 check，compare／accept
    的离线重放会因官方侧没有观测而失败关闭。
    """

    if defer_absent_endpoints and side != "official":
        raise AssertionGateError("端点整体缺席的延后裁决只适用于官方侧 seal")
    observed_paths = (
        {
            item.data.get("path")
            for item in observations
            if isinstance(item.data, Mapping) and isinstance(item.data.get("path"), str)
        }
        if defer_absent_endpoints
        else set()
    )
    modes = contract["validation_modes"]
    checked_rules = 0
    checked_checks = 0
    deferred: list[dict[str, Any]] = []
    for rule in profile["rules"]:
        rule_id = rule["rule_id"]
        if side == "official" and modes[rule_id] != MODE_DUAL_WIRE:
            continue
        checked_rules += 1
        for check in rule["checks"]:
            if not check_applies_to_side(contract, rule_id, check["id"], side):
                # 侧别限定 check：本侧结构性造不出该实验条件，可达性不适用。
                # 依据登记在 acceptance_contract.SIDE_RESTRICTED_CHECKS。
                continue
            assertion = check.get("assertion") or {}
            if (
                assertion.get("operator") == "count_equal"
                and assertion.get("value") == 0
            ):
                # 负向存在性判据（如 SPEC-EP-021 v2-no-legacy-call：默认 V2 批次
                # 不得请求 /responses/compact）的通过形态恰是 select 命中为空；
                # 对它强制"至少命中一条"会与判据语义互斥，可达性预检跳过，
                # 其真实评估仍由 accept 的离线重放完成。
                checked_checks += 1
                continue
            matched = _select_observations(
                observations, check["select"], rule["scenario_ids"]
            )
            if not matched:
                absent_paths = (
                    _pinned_endpoint_paths(check["select"])
                    if defer_absent_endpoints
                    else []
                )
                if absent_paths and not observed_paths.intersection(absent_paths):
                    deferred.append(
                        {
                            "rule_id": rule_id,
                            "check_id": check["id"],
                            "absent_paths": absent_paths,
                        }
                    )
                    checked_checks += 1
                    continue
                raise AssertionGateError(
                    f"seal 预检：规则 {rule_id} 的 check {check['id']} 在"
                    f"{side} 侧无法命中任何观测——证据缺失或标签语义错位"
                )
            checked_checks += 1
    if not checked_rules:
        raise AssertionGateError(f"{side} 侧没有任何应执行规则")
    return checked_rules, checked_checks, deferred


def run_assertion_gate(
    *,
    bundle_dir: Path,
    source_roots: Mapping[str, Path],
    side: str,
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    target_version: str,
    defer_absent_endpoints: bool = False,
) -> dict[str, Any]:
    """执行全部门禁并返回可封存的 gate 收据；任何一步失败即抛错。

    ``defer_absent_endpoints`` 只供官方侧 seal 打开，语义见
    ``_verify_selector_reachability``；延后项非空时写入收据的
    ``deferred_unreachable_checks``，为空时收据形状与旧版逐字一致。
    """

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
            allowed_extra_prefixes=(
                DERIVED_PREFIX,
                CANDIDATE_TRACE_PREFIX,
                MANIFEST_FILENAME,
            ),
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
    candidate_trace_receipt_sha256 = _verify_candidate_trace(bundle_dir, manifest)
    checked_rules, checked_checks, deferred = _verify_selector_reachability(
        profile,
        contract,
        observations,
        side,
        defer_absent_endpoints=defer_absent_endpoints,
    )
    receipt: dict[str, Any] = {
        "side": side,
        "bundle_dir_name": BUNDLE_DIR_NAME,
        "bundle_provenance_sha256": bundle_provenance["provenance_sha256"],
        "bundle_entry_count": bundle_provenance["entry_count"],
        "derived_provenance_sha256": derived_provenance_sha256,
        "candidate_trace_receipt_sha256": candidate_trace_receipt_sha256,
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
    if deferred:
        receipt[DEFERRED_UNREACHABLE_FIELD] = deferred
    return receipt


def _validate_deferred_unreachable_checks(value: Any) -> None:
    """延后项：非空数组，每项恰含 rule_id／check_id／absent_paths，(规则, check) 不重复。"""

    if not isinstance(value, list) or not value:
        raise AssertionGateError("assertion gate 收据延后项必须是非空数组")
    seen: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "rule_id",
            "check_id",
            "absent_paths",
        }:
            raise AssertionGateError("assertion gate 收据延后项字段不闭合")
        rule_id = entry.get("rule_id")
        check_id = entry.get("check_id")
        paths = entry.get("absent_paths")
        if (
            not isinstance(rule_id, str)
            or not rule_id
            or not isinstance(check_id, str)
            or not check_id
        ):
            raise AssertionGateError("assertion gate 收据延后项的规则或 check 非法")
        if (
            not isinstance(paths, list)
            or not paths
            or any(not isinstance(path, str) or not path.startswith("/") for path in paths)
            or paths != sorted(set(paths))
        ):
            raise AssertionGateError("assertion gate 收据延后项的缺席路径非法")
        key = (rule_id, check_id)
        if key in seen:
            raise AssertionGateError("assertion gate 收据延后项重复")
        seen.add(key)


def validate_gate_receipt(value: Any, *, side: str) -> dict[str, Any]:
    """校验 stage 文档中封存的 gate 收据结构；供 stage 契约消费。

    ``deferred_unreachable_checks`` 是可选字段：只允许出现在官方侧收据，且出现时必须
    非空；旧收据没有该字段，按原闭集校验。
    """

    required = {
        "side",
        "bundle_dir_name",
        "bundle_provenance_sha256",
        "bundle_entry_count",
        "derived_provenance_sha256",
        "candidate_trace_receipt_sha256",
        "capture_manifest",
        "acceptance_contract_sha256",
        "artifact_count",
        "observation_count",
        "checked_rule_count",
        "checked_check_count",
    }
    if not isinstance(value, dict) or set(value) - {DEFERRED_UNREACHABLE_FIELD} != required:
        raise AssertionGateError("assertion gate 收据字段不闭合")
    if value.get("side") != side:
        raise AssertionGateError("assertion gate 收据侧别不一致")
    if DEFERRED_UNREACHABLE_FIELD in value:
        if side != "official":
            raise AssertionGateError("只有官方侧 gate 收据可以登记延后的不可达 check")
        _validate_deferred_unreachable_checks(value[DEFERRED_UNREACHABLE_FIELD])
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
