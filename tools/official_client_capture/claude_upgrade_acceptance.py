"""Claude VC-5 的 accept 读侧根（Claude 升级控制面设计 B4-06b）。

编排器服务面 ``claude_upgrade.accept_campaign`` 转调这里（Claude 身份策略的 ``evaluator.accept_reader_roots``）。与 B4-06a 的
``claude_upgrade_accept``（Store 写入层，不读编排器侧阶段结果）分工：本模块重放封存材料、组出写入层要的请求，再写编排器
侧的 accept 阶段结果、验收证据封印与 VC-5 checkpoint。不运行命令、不联网。

一、重放（全部在任何写入之前；任一不符即失败关闭、零写入）
==========================================================
* 前置门与 compare 相同（``claude_upgrade_compare.require_current_candidate``、``verify_reader_runtime_baseline``）。
* 候选阶段结果（最大轮次）与封存后的 attempt 边界、官方阶段结果、分类阶段结果与生效画像、比较收据（``_load_stage_result``
  读侧核对各自绑定）；比较收据必须就是当前评估基线、当前生效画像、当前官方阶段结果与当前 VC-4 构建收据下写出的那一份。
* 断言结果（第三节）：当前评估基线 ``assertions`` 目标里 builder 写出的 ``results.json`` 与 ``evaluation-run.json``，以及
  逐规则逐侧 checkpoint。
* 候选外部门禁（``codex_upgrade_gate_receipt`` 原样重放）：subject 的五项候选身份取 ``claude_upgrade_compare.candidate_gate_identity``。
  收据有失败门禁时转调 ``claude_upgrade_accept.record_failed_gate_round`` 把这一轮门禁定稿进 Store，返回 ``gate_failed``（不写
  accept 阶段结果；下一步是 ``capture-candidate seal --revalidate``）。
* 运行审计（第四节）：候选激活事实 → ``claude_build_runtime_audit.build_runtime_audit``（``validation_candidate`` Runtime）。
* 客户端收据（第四节）：两个官方客户端正例（Desktop、VS Code）与两项边界核对负例（Codex final wire 隔离、第三方与非法
  形态入口拒绝），文件按候选阶段结果的绑定逐个复核。CLI 正例就是 PAIR 本身的候选证据。

二、PAIR 规则行（``derive_pair_rows``，B4-06b 实施调整：设计文档原未给字段来源，路线二提议、主会话定）
====================================================================================================
每条批准规则一行，每个字段连同来源（取自哪份收据、哪个字段）写进 accept 阶段结果的 ``pair_rows`` 审计段：

1. ``condition_sha256``：builder ``evaluation-run.json`` 该规则行的 ``dependency_projection_sha256``；
2. ``scenario_approval_refs``：Store ScenarioPlan 里 ``spec_ids`` 含该规则的场景 → 候选阶段结果登记的当前轮次场景批准；
3. ``official_result``：``ingress_id`` 取 ProfileApproval 生产入口清单里 ``migrated_strict`` 的官方入口——按比较收据登记的
   官方侧该规则场景所含 probe 的请求路径匹配入口别名的 ``physical_route``，恰好命中一个即取它；没有命中（TLS、运行时类
   规则）或命中多个时取路由为 ``/v1/messages`` 的官方入口；都没有即失败关闭。``final_wire_sha256`` 取官方侧 checkpoint
   的 ``projection_sha256``（``candidate_profile`` 规则取官方权威里的画像摘要），``evidence_refs`` 是官方 EvidencePackage；
4. ``candidate_result``：``final_wire_sha256`` 取候选侧 checkpoint 的 ``projection_sha256``，``evidence_refs`` 是当前轮次
   CandidateEvidencePackage；
5. ``third_party_results``：每个 ``migrated_strict`` 第三方协议类别一条。Claude 现有 Campaign 没有这类入口，为空；出现时
   没有证据来源，报待设计（``ClaudeOrchestratorPending``）；
6. ``dynamic_field_checks``：一条 ``claude-rule-assertions``，四个维度齐全，结论跟随该规则 checker 是否全部通过；
7. ``assertion_results``：CandidateEvidencePackage 绑定的台账里该规则的全部 ``required_rule`` 原子断言记 pass，``source``
   与 ``result_source`` 按 RuleMigrationLedger 的 ``affected_rules`` 取 execute／reuse；``comparison_mode`` 取验收合同；
8. 锚点：``test_anchors`` 对受影响规则取 VC-4 门禁计划里该规则门禁（``affected-<规则小写>``）的
   ``<working_directory>:<test_id>``，继承规则取 ``inherited:<基线版本>:<规则>``；``implementation_anchors`` 取
   ``candidate-source-tree:<VC-4 构建收据源码树摘要>``，再加该规则在 VC-3 暂存 Release 画像里的严格端点
   ``release-profile-strict-endpoint:<binding_id>``。以后有更好的来源（例如 VC-4 实现边界清单）只改这一个函数。
9. ``inventory_assertion_refs``（AcceptanceFact）：运行审计结论加客户端正负例收据封成一份 ``operational_evidence`` 对象。

写进 Store 的规则行（``select_pair_rows``）：b0 写全部规则；b≥1 只写本基线执行过的规则与更早基线还没有有效 PAIR 的规则，
其余沿用更早基线的有效 PAIR（与 Store ``_require_effective_pairs`` 同一判据）。accept 结果的 ``pair_rows`` 记全部推导结果、
本次写入的规则与逐字段来源。

三、写入与产物（顺序固定，被打断后重跑同一命令收敛）
==================================================
accept 阶段目录（b0 为 ``acceptance/<候选>/``，b≥1 为评估基线 COMMIT 的写目标）里依次：``runtime-audit.json``、
``selector-after-snapshot.json``（只读观察的冻结值）、``store-request.json``（Store 写入请求的冻结值）→ inventory 与 selector
后态两个 Store 对象 → ``claude_upgrade_accept.record_acceptance_chain``（PAIR → 门禁定稿 → AcceptanceFact → selector 后态 →
``validation_completed``，每步幂等）→ ``evidence-seal.json`` → ``result.json``（accept 阶段结果）→ ``complete_candidate_phase``
写 VC-5 checkpoint（两种用途都在这里完成 VC-5，不做 canonical 交接，设计附录 G 例外①）。全部 write-once：重跑同一输入
逐字复用，输入漂移拒绝。

本文件在 Claude 身份策略里按 ``claude_upgrade_`` 前缀归 control 层；Codex 身份策略忽略它（``claude_`` 前缀）。
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_acceptance_contract as acceptance_contract
from tools.official_client_capture import claude_client_receipt_contract
from tools.official_client_capture import claude_build_runtime_audit as runtime_audit_module
from tools.official_client_capture import claude_upgrade_accept
from tools.official_client_capture import claude_upgrade_baseline
from tools.official_client_capture import claude_upgrade_candidate_capture
from tools.official_client_capture import claude_upgrade_candidate_seal
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_evidence
from tools.official_client_capture import claude_upgrade_profile
from tools.official_client_capture import claude_upgrade_release
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_gate_receipt as gate_receipt
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.errors import ControlError

ACCEPT_SCHEMA = "claude-upgrade-acceptance/v1"
EVIDENCE_SEAL_SCHEMA = "claude-upgrade-acceptance-evidence-seal/v1"
STAGE_NAME = "accept"
RUNTIME_AUDIT_FILENAME = "runtime-audit.json"
SELECTOR_AFTER_FILENAME = "selector-after-snapshot.json"
STORE_REQUEST_FILENAME = "store-request.json"
EVIDENCE_SEAL_FILENAME = "evidence-seal.json"
INVENTORY_MANIFEST_ID = "claude-upgrade-vc5-acceptance-inventory"
OPERATIONAL_EVIDENCE_SCHEMA = claude_upgrade_evidence.OPERATIONAL_EVIDENCE_SCHEMA
PRODUCTION_STATE = "not_activated"
# 候选激活事实在 attempt 里的位置（B4-01 c1 定形）与字段闭集。
ACTIVATION_FACT_CONTAINER = "gateway_environment"
ACTIVATION_FACT_FIELD = "activation_fact"
ACTIVATION_FACT_FIELDS = frozenset({"path", "sha256", "bytes", "source_path", "schema_version", "profile_mode", "event_id"})
# PAIR 规则行的推导常量（模块说明第二节）。
DYNAMIC_CHECK_ID = "claude-rule-assertions"
DYNAMIC_DIMENSIONS = ("format", "lifecycle", "relation", "source")
DEFAULT_OFFICIAL_ROUTE = "/v1/messages"
THIRD_PARTY_WORK_ITEM = "后续设计（Claude migrated_strict 第三方入口的 lossless 结果来源，B4-06b 实施调整登记）"
PAIR_INPUT_FIELDS = frozenset(
    {
        "rules",
        "modes",
        "evaluation",
        "official_authority",
        "ledger_assertions",
        "affected_rules",
        "scenario_plan",
        "scenario_approval_refs",
        "ingress_entries",
        "ingress_aliases",
        "ingress_target_dispositions",
        "rule_probes",
        "probe_request_paths",
        "official_package_ref",
        "candidate_package_ref",
        "gate_plan_gates",
        "source_tree_sha256",
        "strict_endpoints",
        "baseline_version",
        "sources",
    }
)
STAGE_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "client",
        "campaign_id",
        "stage",
        "candidate_id",
        "status",
        "accepted",
        "campaign_purpose",
        "candidate_purpose",
        "production_state",
        "candidate_revision",
        "validation_round",
        "evaluation_baseline",
        "comparison",
        "assertions",
        "external_gate",
        "runtime_audit",
        "client_evidence",
        "pair_rows",
        "store",
        "evidence_seal",
        "accepted_at_utc",
    }
)


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _read_json(path: Path, label: str) -> dict[str, Any]:
    return _orchestrator()._read_json(path, label)


def _sorted_refs(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(item) for item in values), key=canonical_sha256)


# ---------------------------------------------------------------------------
# 一、断言结果
# ---------------------------------------------------------------------------


def _checkpoint(campaign_dir: Path, binding: Any, *, rule: str, side: str) -> dict[str, Any]:
    path = compare._bound_campaign_file(campaign_dir, binding, f"{rule} {side} checkpoint")
    try:
        checkpoint = vc_artifacts.validate_evaluation_checkpoint(_read_json(path, f"{rule} {side} checkpoint"))
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"{rule} {side} checkpoint 不符合合同：{error}") from error
    if (checkpoint["rule"], checkpoint["side"], checkpoint["status"]) != (rule, side, "pass"):
        raise ConfigurationError(f"{rule} {side} checkpoint 的规则、侧别或结论不符（必须 pass）。")
    return checkpoint


def load_assertion_results(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    comparison: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    rules: Sequence[str],
    assertions_path: Path | None,
) -> dict[str, Any]:
    """当前评估基线 builder 写出的结果、评估运行索引与逐规则 checkpoint（见模块说明第一节）；返回逐规则评估摘要与绑定。"""

    builder = comparison["builder"]
    results_path = _orchestrator()._campaign_file(Path(campaign_dir), str(builder["output"]))
    if assertions_path is not None and Path(assertions_path).resolve(strict=False) != results_path.resolve(strict=False):
        raise ConfigurationError(f"--assertions 必须是比较收据登记的 builder 输出：{builder['output']}")
    if results_path.is_symlink() or not results_path.is_file():
        raise ConfigurationError(f"逐规则断言结果尚未生成（{builder['output']}）：先派发 VC-5 assert-rules 批次，且全部规则通过。")
    results = _read_json(results_path, "逐规则断言结果")
    identity = comparison["identity"]
    expected = {
        "schema_version": acceptance_contract.RESULTS_SCHEMA_V2,
        "document_kind": "results",
        "candidate_id": candidate_id,
        "target_version": str(manifest["target_version"]),
        "profile_id": identity["profile_id"],
        "profile_digest": identity["profile_digest"],
        "official_package_digest": str(comparison["official"]["package_ref"]["sha256"]),
        "candidate_package_digest": identity["candidate_package_digest"],
        "comparison_package_digest": comparison["package_digest"],
        "acceptance_contract_sha256": acceptance_contract.contract_sha256(contract),
    }
    drift = sorted(field for field, value in expected.items() if results.get(field) != value)
    if drift:
        raise ConfigurationError(f"逐规则断言结果与比较收据或验收合同不一致（{'、'.join(drift)}）。")
    rows = results.get("rules")
    if not isinstance(rows, list) or sorted(str(row.get("rule")) for row in rows) != sorted(rules):
        raise ConfigurationError("逐规则断言结果的规则集与批准规则不一致。")
    modes = contract["validation_modes"]
    for row in rows:
        if row.get("status") != "pass" or row.get("validation_mode") != modes.get(row.get("rule")):
            raise ConfigurationError(f"规则 {row.get('rule')} 的断言结果不是 pass 或验收模式与合同不符。")
    run_path = results_path.parent / "evaluation-run.json"
    if run_path.is_symlink() or not run_path.is_file():
        raise ConfigurationError("评估运行索引 evaluation-run.json 缺失。")
    try:
        run = vc_artifacts.validate_evaluation_run(_read_json(run_path, "评估运行索引"))
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"评估运行索引不符合合同：{error}") from error
    baseline = int(comparison["evaluation_baseline"])
    if (run["campaign_id"], run["candidate_id"], run["candidate_revision"], run["evaluation_baseline"], run["derived"]) != (
        manifest["campaign_id"],
        candidate_id,
        comparison["candidate_revision"],
        baseline,
        False,
    ):
        raise ConfigurationError("评估运行索引的 Campaign、候选、revision、评估基线或派生标记与比较收据不一致。")
    claude_upgrade_baseline.verify_evaluator_digests_authorized(
        Path(campaign_dir), manifest, candidate_id, baseline, run["evaluator"], label="accept 评估运行索引"
    )
    run_rows = {str(row["rule"]): row for row in run["rules"]}
    if sorted(run_rows) != sorted(rules):
        raise ConfigurationError("评估运行索引的规则集与批准规则不一致。")
    run_relative = run_path.relative_to(Path(campaign_dir)).as_posix()
    evaluation: dict[str, dict[str, Any]] = {}
    for rule in rules:
        row = run_rows[rule]
        if row["status"] != "pass" or row["validation_mode"] != modes[rule]:
            raise ConfigurationError(f"评估运行索引里规则 {rule} 不是 pass 或验收模式与合同不符。")
        candidate_checkpoint = _checkpoint(Path(campaign_dir), row["candidate_checkpoint"], rule=rule, side="candidate")
        official_checkpoint = None
        if modes[rule] == acceptance_contract.MODE_DUAL_WIRE:
            official_checkpoint = _checkpoint(Path(campaign_dir), row["official_checkpoint"], rule=rule, side="official")
        evaluation[rule] = {
            "reused": row["reused_from"] is not None,
            "dependency_projection_sha256": str(row["dependency_projection_sha256"]),
            "candidate_projection_sha256": str(candidate_checkpoint["projection_sha256"]),
            "official_projection_sha256": None if official_checkpoint is None else str(official_checkpoint["projection_sha256"]),
            "sources": {
                "dependency_projection_sha256": f"{run_relative}#rules[{rule}].dependency_projection_sha256",
                "candidate_projection_sha256": f"{row['candidate_checkpoint']['path']}#projection_sha256",
                "official_projection_sha256": None
                if official_checkpoint is None
                else f"{row['official_checkpoint']['path']}#projection_sha256",
            },
        }
    return {
        "results": {"path": results_path.relative_to(Path(campaign_dir)).as_posix(), "sha256": file_sha256(results_path)},
        "evaluation_run": {"path": run_relative, "sha256": file_sha256(run_path)},
        "evaluation": evaluation,
    }


# ---------------------------------------------------------------------------
# 二、运行审计与客户端收据
# ---------------------------------------------------------------------------


def runtime_audit(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    attempt_root: Path,
    attempt: Mapping[str, Any],
    build: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """候选激活事实 → 运行审计（``validation_candidate`` Runtime）；返回 ``(审计文档, 事实绑定)``。缺失、不可读或不是候选
    Runtime 即失败关闭。"""

    container = attempt.get(ACTIVATION_FACT_CONTAINER)
    record = container.get(ACTIVATION_FACT_FIELD) if isinstance(container, Mapping) else None
    if not isinstance(record, Mapping) or set(record) != ACTIVATION_FACT_FIELDS:
        raise ConfigurationError("候选 attempt 没有登记候选激活事实（gateway_environment.activation_fact），不能证明候选画像已生效。")
    if record["schema_version"] != runtime_audit_module.ACTIVATION_SCHEMA or record["profile_mode"] != runtime_audit_module.PROFILE_MODE_CANDIDATE:
        raise ConfigurationError("候选激活事实的合同名或 profile_mode 不是候选 Runtime（validation_candidate）。")
    relative = PurePosixPath(str(record["path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise ConfigurationError("候选激活事实路径越出 attempt 证据目录。")
    path = Path(attempt_root) / "evidence" / Path(*relative.parts)
    if path.is_symlink() or not path.is_file() or file_sha256(path) != record["sha256"] or path.stat().st_size != record["bytes"]:
        raise ConfigurationError(f"候选激活事实缺失、不可读或摘要与 attempt 登记不一致：{record['path']}")
    try:
        fact = runtime_audit_module.load_fact(path)
        if fact.get("event_id") != record["event_id"]:
            raise runtime_audit_module.RuntimeAuditError("事实的 event_id 与 attempt 登记不一致")
        audit = runtime_audit_module.build_runtime_audit(
            fact,
            campaign_id=str(manifest["campaign_id"]),
            attempt_id=str(attempt["attempt_id"]),
            run_nonce=str(attempt["run_nonce"]),
            candidate_id=candidate_id,
            target_version=str(manifest["target_version"]),
            profile_digest=str(build["profile"]["profile_digest"]),
            wire_digest=str(build["profile"]["wire_sha256"]),
            release_digest=str(build["profile"]["release_sha256"]),
            bundle_digest=str(build["profile"]["bundle_sha256"]),
            approval_digest="",
            image_id=str(build["image"]["image_id"]),
            image_reference=str(build["image"]["reference"]),
            source_tree_sha256=str(build["source"]["tree_sha256"]),
            build_id=str(build["build"]["build_id"]),
            deployed_version=str(build["deployed_version"]),
            profile_mode=runtime_audit_module.PROFILE_MODE_CANDIDATE,
        )
    except (runtime_audit_module.RuntimeAuditError, OSError, ValueError, KeyError) as error:
        raise ConfigurationError(f"候选运行审计失败：{error}") from error
    binding = {"path": path.relative_to(Path(campaign_dir)).as_posix(), "sha256": file_sha256(path), "event_id": str(record["event_id"])}
    return audit, binding


def client_receipts(campaign_dir: Path, manifest: Mapping[str, Any], candidate: Mapping[str, Any], attempt: Mapping[str, Any]) -> dict[str, Any]:
    """客户端正例与边界核对负例收据：按候选阶段结果的绑定逐个复核文件摘要、身份字段与结论；场景清单登记的项必须齐全。"""

    scenarios_path = claude_upgrade_seal._frozen_input(Path(campaign_dir), manifest, "target_scenarios")
    scenarios = _read_json(scenarios_path, "目标场景清单")
    given = candidate.get("client_evidence") if isinstance(candidate.get("client_evidence"), Mapping) else {}
    values = {
        "campaign_id": str(manifest["campaign_id"]),
        "candidate_id": str(candidate["candidate_id"]),
        "attempt_id": str(attempt["attempt_id"]),
        "run_nonce": str(attempt["run_nonce"]),
    }
    summary: dict[str, dict[str, Any]] = {}
    for group, required, identity, outcome in (
        ("client_bindings", scenarios.get("required_client_bindings"), "client_id", ("status", "success")),
        ("boundary_checks", scenarios.get("required_boundary_checks"), "check_id", ("result", "pass")),
    ):
        bindings = given.get(group) if isinstance(given.get(group), Mapping) else {}
        if not isinstance(required, list) or sorted(bindings) != sorted(str(item) for item in required):
            raise ConfigurationError(f"候选阶段结果的 {group} 与场景清单登记的不一致。")
        summary[group] = {}
        for item in sorted(bindings):
            path = compare._bound_campaign_file(Path(campaign_dir), bindings[item], f"{group} 收据 {item}")
            payload = _read_json(path, f"{group} 收据 {item}")
            wanted = {identity: item, **{field: values[field] for field in claude_upgrade_evidence.CANDIDATE_RECEIPT_BINDING_FIELDS}, outcome[0]: outcome[1]}
            failed = sorted(field for field, value in wanted.items() if payload.get(field) != value)
            if failed:
                raise ConfigurationError(f"{group} 收据 {item} 的字段不符：{'、'.join(failed)}。")
            if group == "client_bindings":
                claude_client_receipt_contract.verify_client_receipt(path.parent.parent, str(item), path)
            summary[group][item] = {"path": path.relative_to(Path(campaign_dir)).as_posix(), "sha256": file_sha256(path)}
    return summary


# ---------------------------------------------------------------------------
# 三、PAIR 规则行（模块说明第二节）
# ---------------------------------------------------------------------------


def _official_ingress(spec: str, inputs: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """规则的官方入口与匹配依据（模块说明第二节第 3 条）。"""

    targets = {str(item["logical_ingress_id"]): str(item["target_disposition"]) for item in inputs["ingress_target_dispositions"]}
    aliases = {str(item["alias_id"]): str(item["physical_route"]) for item in inputs["ingress_aliases"]}
    routes: dict[str, set[str]] = {}
    for entry in inputs["ingress_entries"]:
        ingress = str(entry["logical_ingress_id"])
        if entry.get("ingress_kind") != "official" or targets.get(ingress) != "migrated_strict":
            continue
        missing = [alias for alias in entry.get("physical_alias_ids") or [] if alias not in aliases]
        if missing:
            raise ConfigurationError(f"官方入口 {ingress} 的别名在入口观测里不存在：{missing}。")
        routes[ingress] = {aliases[alias] for alias in entry.get("physical_alias_ids") or []}
    if not routes:
        raise ConfigurationError("ProfileApproval 没有 migrated_strict 官方入口，PAIR 的官方结果无入口可记。")
    probes = sorted(inputs["rule_probes"].get(spec, []))
    paths = sorted({path for probe in probes for path in inputs["probe_request_paths"].get(probe, [])})
    matched = sorted(ingress for ingress, values in routes.items() if values & set(paths))
    if len(matched) == 1:
        return matched[0], {"basis": "request_path", "probes": probes, "request_paths": paths, "matched": matched}
    fallback = sorted(ingress for ingress, values in routes.items() if DEFAULT_OFFICIAL_ROUTE in values)
    if len(fallback) != 1:
        raise ConfigurationError(
            f"规则 {spec} 的官方入口无法确定：请求路径命中 {matched or '无'}，路由为 {DEFAULT_OFFICIAL_ROUTE} 的官方入口有 {fallback or '无'}。"
        )
    return fallback[0], {"basis": "default_messages_route", "probes": probes, "request_paths": paths, "matched": matched}


def _third_party_results(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    targets = {str(item["logical_ingress_id"]): str(item["target_disposition"]) for item in inputs["ingress_target_dispositions"]}
    third = sorted(
        str(entry["protocol_class"])
        for entry in inputs["ingress_entries"]
        if entry.get("ingress_kind") == "third_party" and targets.get(str(entry["logical_ingress_id"])) == "migrated_strict"
    )
    if third:
        raise _orchestrator()._pending(f"PAIR third_party_results（migrated_strict 第三方协议类别 {sorted(set(third))}）", THIRD_PARTY_WORK_ITEM)
    return []


def derive_pair_rows(inputs: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """按模块说明第二节的 9 条推导每条批准规则的 PAIR 规则行；返回 ``(规则行, 逐规则逐字段的取值来源)``。

    ``inputs`` 由 ``pair_row_inputs`` 从 Store 与封存材料组出（字段闭集 ``PAIR_INPUT_FIELDS``）；任一字段找不到来源即失败关闭。
    """

    if not isinstance(inputs, Mapping) or set(inputs) != PAIR_INPUT_FIELDS:
        raise ConfigurationError("PAIR 推导输入的字段不闭合。")
    sources = inputs["sources"]
    third_party = _third_party_results(inputs)
    affected = set(inputs["affected_rules"])
    gates = {str(item["gate_id"]): item for item in inputs["gate_plan_gates"]}
    rows: list[dict[str, Any]] = []
    provenance: dict[str, dict[str, Any]] = {}
    for spec in inputs["rules"]:
        mode = inputs["modes"].get(spec)
        evaluation = inputs["evaluation"].get(spec)
        if mode is None or evaluation is None:
            raise ConfigurationError(f"规则 {spec} 没有验收模式或评估运行行。")
        scenarios = sorted(str(item["id"]) for item in inputs["scenario_plan"] if spec in (item.get("spec_ids") or []))
        if not scenarios:
            raise ConfigurationError(f"规则 {spec} 不在 ScenarioPlan 的任何场景里。")
        missing = [scenario for scenario in scenarios if scenario not in inputs["scenario_approval_refs"]]
        if missing:
            raise ConfigurationError(f"规则 {spec} 的场景没有候选场景批准：{missing}。")
        assertions = sorted(
            str(item["assertion_id"])
            for item in inputs["ledger_assertions"]
            if item.get("owner_kind") == "required_rule" and item.get("owner_id") == spec
        )
        if not assertions:
            raise ConfigurationError(f"台账里没有规则 {spec} 的 required_rule 原子断言。")
        source = "execute" if spec in affected else "reuse"
        ingress, ingress_basis = _official_ingress(spec, inputs)
        if mode == acceptance_contract.MODE_DUAL_WIRE:
            official_wire = evaluation.get("official_projection_sha256")
            official_wire_source = evaluation["sources"].get("official_projection_sha256")
        else:
            official_wire = inputs["official_authority"].get("assertion_profile_sha256")
            official_wire_source = f"{sources['official_authority']}#assertion_profile_sha256"
        if not isinstance(official_wire, str) or not official_wire:
            raise ConfigurationError(f"规则 {spec} 的官方 final wire 摘要没有来源。")
        if source == "execute":
            gate_id = f"affected-{spec.lower()}"
            gate = gates.get(gate_id)
            if gate is None:
                raise ConfigurationError(f"受影响规则 {spec} 在 VC-4 门禁计划里没有门禁 {gate_id}。")
            test_anchors = [f"{gate['working_directory']}:{gate['test_id']}"]
            test_source = f"{sources['gate_plan']}#gates[{gate_id}].working_directory/test_id"
        else:
            test_anchors = [f"inherited:{inputs['baseline_version']}:{spec}"]
            test_source = f"campaign.json#baseline_version（继承规则：{sources['migration']}#affected_rules 不含 {spec}）"
        endpoints = sorted(str(item["binding_id"]) for item in inputs["strict_endpoints"] if spec in (item.get("spec_ids") or []))
        implementation = sorted([f"candidate-source-tree:{inputs['source_tree_sha256']}", *(f"release-profile-strict-endpoint:{item}" for item in endpoints)])
        row = {
            "spec_id": spec,
            "condition_sha256": evaluation["dependency_projection_sha256"],
            "scenario_approval_refs": _sorted_refs([inputs["scenario_approval_refs"][scenario] for scenario in scenarios]),
            "official_result": {
                "ingress_id": ingress,
                "translation": "lossless",
                "result": "pass",
                "final_wire_sha256": official_wire,
                "evidence_refs": [dict(inputs["official_package_ref"])],
            },
            "candidate_result": {
                "result": "pass",
                "final_wire_sha256": evaluation["candidate_projection_sha256"],
                "evidence_refs": [dict(inputs["candidate_package_ref"])],
            },
            "third_party_results": list(third_party),
            "dynamic_field_checks": [{"id": DYNAMIC_CHECK_ID, "dimensions": list(DYNAMIC_DIMENSIONS), "result": "pass"}],
            "assertion_results": [
                {"assertion_id": assertion, "owner_kind": "required_rule", "owner_id": spec, "result": "pass", "source": source}
                for assertion in assertions
            ],
            "implementation_anchors": implementation,
            "test_anchors": test_anchors,
            "result_source": source,
            "comparison_mode": mode,
        }
        rows.append(row)
        provenance[spec] = {
            "condition_sha256": evaluation["sources"]["dependency_projection_sha256"],
            "scenario_approval_refs": {"scenarios": scenarios, "source": f"{sources['scenario_plan']}#scenarios[spec_ids∋{spec}] → {sources['candidate_stage']}#store.scenario_approval_refs"},
            "official_result.ingress_id": {**ingress_basis, "source": f"{sources['ingress']} 与 {sources['comparison']}#official.probe_request_paths"},
            "official_result.final_wire_sha256": official_wire_source,
            "official_result.evidence_refs": f"{sources['official_stage']}#store.evidence_package_ref",
            "candidate_result.final_wire_sha256": evaluation["sources"]["candidate_projection_sha256"],
            "candidate_result.evidence_refs": f"{sources['candidate_stage']}#store.candidate_evidence_package_ref",
            "third_party_results": f"{sources['ingress']}：没有 migrated_strict 第三方入口",
            "dynamic_field_checks": f"{sources['assertions']}#rules[{spec}].status",
            "assertion_results": {"assertion_ids": assertions, "source": f"{sources['ledger']}#assertions[owner_id={spec}]"},
            "result_source": f"{sources['migration']}#affected_rules",
            "comparison_mode": f"{sources['profile']}（验收合同 validation_modes[{spec}]）",
            "implementation_anchors": {
                "strict_endpoint_binding_ids": endpoints,
                "source": f"{sources['build_receipt']}#source.tree_sha256 与 {sources['release_profile']}#strict_endpoints[spec_ids∋{spec}].binding_id",
            },
            "test_anchors": test_source,
        }
    return rows, provenance


def select_pair_rows(
    rows: Sequence[Mapping[str, Any]],
    evaluation: Mapping[str, Mapping[str, Any]],
    *,
    baseline: int,
    earlier_effective: Sequence[str],
) -> list[dict[str, Any]]:
    """本次写进 Store 的规则行：b0 写全部规则；b≥1 只写本基线执行过的规则（评估运行行没有 ``reused_from``）和更早基线还没有
    有效 PAIR 的规则（例如 b0 断言失败、从没写过 PAIR），其余规则沿用更早基线的有效 PAIR（与 Store ``_require_effective_pairs``
    同一判据）。``earlier_effective`` 只取基线小于 b<K> 的有效 PAIR，accept 被打断后重跑时选出的行不变。"""

    if int(baseline) == 0:
        return [dict(row) for row in rows]
    existing = set(earlier_effective)
    return [dict(row) for row in rows if not evaluation[str(row["spec_id"])]["reused"] or str(row["spec_id"]) not in existing]


def pair_row_inputs(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    store: Any,
    *,
    candidate: Mapping[str, Any],
    official: Mapping[str, Any],
    comparison: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    modes: Mapping[str, str],
    rules: Sequence[str],
    profile: Mapping[str, Any],
    build: Mapping[str, Any],
    sources: Mapping[str, str],
) -> dict[str, Any]:
    """从 Store、比较收据、VC-4 构建收据与 VC-3 暂存 Release 组出 ``derive_pair_rows`` 的输入。"""

    def load_fact(reference: Any, kind: str) -> dict[str, Any]:
        try:
            fact = store.load_fact(dict(reference))
        except (ControlError, TypeError, ValueError) as error:
            raise ConfigurationError(f"Store {kind} 事实无法读取：{error}") from error
        if fact.get("fact_kind") != kind:
            raise ConfigurationError(f"Store 引用的不是 {kind} 事实。")
        return dict(fact["payload"])

    def load_object(reference: Any, kind: str) -> dict[str, Any]:
        if not isinstance(reference, Mapping) or reference.get("object_kind") != kind:
            raise ConfigurationError(f"Store 引用的不是 {kind} 对象。")
        try:
            return dict(store.load_object(dict(reference))["payload"])
        except (ControlError, KeyError, TypeError, ValueError) as error:
            raise ConfigurationError(f"Store {kind} 对象无法读取：{error}") from error

    store_summary = candidate.get("store") or {}
    package_ref = dict(store_summary.get("candidate_evidence_package_ref") or {})
    package = load_object(package_ref, "candidate_evidence_package")
    ledger = load_object(package["atomic_assertion_ledger_ref"], "atomic_assertion_ledger")
    migration = load_object(ledger["rule_migration_ledger_ref"], "rule_migration_ledger")
    attempt = load_fact(store_summary.get("validation_attempt_ref"), "validation_attempt_created")
    candidate_fact = load_fact(attempt["candidate_ref"], "candidate_frozen")
    approval = load_fact(candidate_fact["profile_approval_ref"], "profile_approved")
    ingress = load_object(approval["production_ingress_inventory_ref"], "production_ingress_inventory")
    observation = load_object(ingress["observation_ref"], "ingress_observation")
    scenario_plan = load_object(approval["scenario_plan_ref"], "scenario_plan")
    # 规则 → 官方侧该规则场景所含 probe（比较收据登记的官方评估用 manifest）。
    official_manifest = _read_json(
        compare._bound_campaign_file(Path(campaign_dir), comparison["official"]["evaluation_manifest"], "官方评估用 manifest"),
        "官方评估用 manifest",
    )
    rule_scenarios = {str(rule["rule_id"]): set(rule.get("scenario_ids") or []) for rule in profile["rules"]}
    rule_probes = {
        spec: sorted({str(item["probe_id"]) for item in official_manifest["artifacts"] if rule_scenarios.get(spec, set()) & set(item.get("scenario_ids") or [])})
        for spec in rules
    }
    gate_plan_binding = build["gate_plan"]
    gate_plan_path = Path(str(gate_plan_binding["path"]))
    if gate_plan_path.is_symlink() or not gate_plan_path.is_file() or file_sha256(gate_plan_path) != gate_plan_binding["sha256"]:
        raise ConfigurationError("VC-4 门禁计划缺失或与构建收据登记的摘要不一致。")
    gate_plan = _read_json(gate_plan_path, "VC-4 门禁计划")
    stage = claude_upgrade_profile.load_stage_result(Path(campaign_dir), manifest)
    staged = claude_upgrade_release.load_staged_release(Path(campaign_dir), stage.get("staged_release") or {})
    entry = staged["release_entry"]
    release_profile_path = Path(campaign_dir) / claude_upgrade_release.STAGED_RELEASE_ROOT / Path(*PurePosixPath(str(entry["profile"]["path"])).parts)
    if release_profile_path.is_symlink() or not release_profile_path.is_file() or file_sha256(release_profile_path) != entry["profile"]["sha256"]:
        raise ConfigurationError("VC-3 暂存 Release 画像缺失或摘要漂移。")
    release_profile = _read_json(release_profile_path, "VC-3 暂存 Release 画像")
    return {
        "rules": list(rules),
        "modes": dict(modes),
        "evaluation": dict(evaluation),
        "official_authority": dict(comparison["official_authority"]),
        "ledger_assertions": [dict(item) for item in ledger["assertions"]],
        "affected_rules": sorted(str(item) for item in migration.get("affected_rules") or []),
        "scenario_plan": [dict(item) for item in scenario_plan["scenarios"]],
        "scenario_approval_refs": {str(key): dict(value) for key, value in (store_summary.get("scenario_approval_refs") or {}).items()},
        "ingress_entries": [dict(item) for item in ingress["entries"]],
        "ingress_aliases": [dict(item) for item in observation["aliases"]],
        "ingress_target_dispositions": [dict(item) for item in approval["ingress_target_dispositions"]],
        "rule_probes": rule_probes,
        "probe_request_paths": {str(key): list(value) for key, value in comparison["official"]["probe_request_paths"].items()},
        "official_package_ref": dict((official.get("store") or {})["evidence_package_ref"]),
        "candidate_package_ref": package_ref,
        "gate_plan_gates": [dict(item) for item in gate_plan.get("gates") or []],
        "source_tree_sha256": str(build["source"]["tree_sha256"]),
        "strict_endpoints": [dict(item) for item in release_profile.get("strict_endpoints") or [] if isinstance(item, Mapping)],
        "baseline_version": str(manifest["baseline_version"]),
        "sources": {
            **dict(sources),
            "ledger": f"Store atomic_assertion_ledger {package['atomic_assertion_ledger_ref']['sha256'][:12]}",
            "migration": f"Store rule_migration_ledger {ledger['rule_migration_ledger_ref']['sha256'][:12]}",
            "scenario_plan": f"Store scenario_plan {approval['scenario_plan_ref']['sha256'][:12]}",
            "ingress": f"Store production_ingress_inventory {approval['production_ingress_inventory_ref']['sha256'][:12]}（含入口观测别名）与 ProfileApproval.ingress_target_dispositions",
            "gate_plan": str(gate_plan_path),
            "release_profile": release_profile_path.relative_to(Path(campaign_dir)).as_posix(),
        },
    }


# ---------------------------------------------------------------------------
# 四、accept
# ---------------------------------------------------------------------------


def _gate_status(root: Path, receipt: Path) -> str:
    """只读看一眼候选外部门禁收据的结论（passed／failed），正式重放与核对由写入层各自完成。"""

    try:
        resolved_root = Path(root).resolve(strict=True)
        path = (receipt if Path(receipt).is_absolute() else resolved_root / receipt).resolve(strict=True)
        payload = gate_receipt.replay(resolved_root, path.relative_to(resolved_root).as_posix())
    except (OSError, RuntimeError, ValueError, gate_receipt.GateReceiptError) as error:
        raise ConfigurationError(f"候选外部门禁收据无法重放：{error}") from error
    return str(payload.get("status"))


def accept_campaign(
    campaign_dir: Path,
    candidate_id: str,
    *,
    assertions_path: Path | None,
    external_gate_root: Path,
    external_gate_receipt: Path,
) -> dict[str, Any]:
    """accept 读侧根（见模块说明）：Campaign 锁内重放封存材料、写 Store 验收链、accept 阶段结果、证据封印与 VC-5 checkpoint。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    orchestrator._validate_existing_campaign_path(campaign_dir)
    campaign_dir = campaign_dir.resolve(strict=True)
    if not isinstance(candidate_id, str) or not orchestrator.SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("accept 必须提供合法 --candidate-id。")
    orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        orchestrator._require_quiescent_campaign_lease(campaign_dir, action="accept")
        orchestrator._reject_contaminated_campaign(campaign_dir)
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        return _accept(
            campaign_dir,
            manifest,
            candidate_id,
            assertions_path=assertions_path,
            gate_root=Path(external_gate_root),
            gate_receipt_path=Path(external_gate_receipt),
        )


def _accept(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    *,
    assertions_path: Path | None,
    gate_root: Path,
    gate_receipt_path: Path,
) -> dict[str, Any]:
    orchestrator = _orchestrator()
    current = compare.require_current_candidate(campaign_dir, manifest, candidate_id, action="accept")
    compare.verify_reader_runtime_baseline(campaign_dir, candidate_id, label="accept")
    store = current["store"]
    candidate = orchestrator._load_stage_result(campaign_dir, "capture-candidate", candidate_id)
    attempt_root, attempt = orchestrator._capture_stage_attempt_context(campaign_dir, candidate, phase="candidate", candidate_id=candidate_id)
    official = orchestrator._load_stage_result(campaign_dir, "capture-official")
    comparison = orchestrator._load_stage_result(campaign_dir, "compare", candidate_id)
    classification = orchestrator._load_stage_result(campaign_dir, "classify")
    view = orchestrator._effective_classification_view(campaign_dir, candidate_id, classification)
    _profile_path, profile, profile_binding = compare._effective_profile(campaign_dir, view)
    rules = list(orchestrator._approved_rules(campaign_dir, manifest, require_approved=True))
    build, build_binding = compare.load_candidate_build_receipt(campaign_dir, manifest, candidate_id, current["revision"])
    identity = compare.candidate_gate_identity(campaign_dir, manifest, candidate_id, build)
    baseline, commit = orchestrator._current_evaluation_baseline(campaign_dir, candidate_id)
    # 比较收据必须就是当前状态下写出的那一份。
    expectations = {
        "candidate_revision": current["revision"],
        "evaluation_baseline": int(baseline),
        "baseline_commit_sha256": commit.get("commit_sha256") if isinstance(commit, Mapping) else None,
        "rules": rules,
        "identity": identity,
    }
    drift = sorted(field for field, value in expectations.items() if comparison.get(field) != value)
    inputs = comparison.get("inputs") or {}
    if inputs.get("assertion_profile") != profile_binding:
        drift.append("inputs.assertion_profile")
    if inputs.get("build_receipt") != build_binding:
        drift.append("inputs.build_receipt")
    if inputs.get("official_stage") != compare._campaign_binding(campaign_dir, campaign_dir / claude_upgrade_seal.STAGE_RESULT_RELATIVE):
        drift.append("inputs.official_stage")
    if comparison["candidate"]["evaluation_manifest"] != {
        key: candidate[claude_upgrade_candidate_seal.ASSERTION_CONTEXT_FIELD][claude_upgrade_candidate_seal.EVALUATION_MANIFEST_FIELD][key]
        for key in ("path", "sha256")
    }:
        drift.append("candidate.evaluation_manifest")
    if drift:
        raise ConfigurationError(f"比较收据不是当前状态下写出的（{'、'.join(sorted(drift))}）：先在当前评估基线重新 compare。")
    contract = acceptance_contract.build_contract_payload(profile)
    modes = dict(contract["validation_modes"])
    assertions = load_assertion_results(
        campaign_dir, manifest, candidate_id, comparison, contract=contract, rules=rules, assertions_path=assertions_path
    )
    # 外部门禁：有失败门禁的收据只把这一轮定稿进 Store，不写 accept 阶段结果。
    external = {"evidence_root": str(gate_root), "receipt_path": str(gate_receipt_path), "identity": dict(identity)}
    store_summary = candidate.get("store") or {}
    if _gate_status(gate_root, gate_receipt_path) != "passed":
        failed = claude_upgrade_accept.record_failed_gate_round(
            campaign_dir,
            manifest,
            {"candidate_id": candidate_id, "validation_attempt_ref": dict(store_summary["validation_attempt_ref"]), "external_gate": external},
        )
        return {
            "schema_version": ACCEPT_SCHEMA,
            "status": "gate_failed",
            "accepted": False,
            "candidate_id": candidate_id,
            "failed_round": failed,
            "next_command": (
                f"capture-candidate seal --campaign-dir {campaign_dir} --candidate-id {candidate_id} "
                f"--attempt-id {attempt['attempt_id']} --revalidate（零请求封存下一轮，失败门禁重跑）"
            ),
        }
    audit, activation_binding = runtime_audit(campaign_dir, manifest, candidate_id, attempt_root, attempt, build)
    if isinstance(candidate.get("recovery"), Mapping):
        from tools.official_client_capture import claude_upgrade_segment_seal

        segment_audits, segment_activations = [], []
        for segment_root, segment in claude_upgrade_segment_seal.audit_segments(campaign_dir, candidate_id, candidate):
            segment_audit, segment_activation = runtime_audit(campaign_dir, manifest, candidate_id, segment_root, segment, build)
            segment_audits.append(segment_audit)
            segment_activations.append(segment_activation)
        audit = {**audit, "recovery_segments": segment_audits}
        activation_binding = {**activation_binding, "recovery_segments": segment_activations}
    clients = client_receipts(campaign_dir, manifest, candidate, attempt)
    candidate_stage_binding = comparison["inputs"]["candidate_stage"]
    sources = {
        "assertions": assertions["results"]["path"],
        "official_authority": f"{comparison['inputs']['assertion_profile']['path']}（分类视图官方权威）",
        "official_stage": comparison["inputs"]["official_stage"]["path"],
        "candidate_stage": candidate_stage_binding["path"],
        "comparison": orchestrator._stage_path(campaign_dir, "compare", candidate_id)[1].relative_to(campaign_dir).as_posix(),
        "profile": profile_binding["path"],
        "build_receipt": build_binding["path"],
    }
    pair_inputs = pair_row_inputs(
        campaign_dir,
        manifest,
        store,
        candidate=candidate,
        official=official,
        comparison=comparison,
        evaluation=assertions["evaluation"],
        modes=modes,
        rules=rules,
        profile=profile,
        build=build,
        sources=sources,
    )
    rows, provenance = derive_pair_rows(pair_inputs)
    earlier = sorted(claude_upgrade_accept.effective_pairs(store, current["store_campaign"], candidate_id, int(baseline) - 1)) if int(baseline) > 0 else []
    written_rows = select_pair_rows(rows, assertions["evaluation"], baseline=int(baseline), earlier_effective=earlier)
    _canonical, stage_path = orchestrator._stage_path(campaign_dir, STAGE_NAME, candidate_id, mode="write")
    stage_dir = stage_path.parent
    # 冻结值：运行审计、selector 后态快照与 Store 写入请求（重跑逐字复用，免得时刻不同而写出第二份事实）。
    audit_path = stage_dir / RUNTIME_AUDIT_FILENAME
    claude_upgrade_seal._write_once_or_reuse(audit_path, audit)
    store_campaign = current["store_campaign"]
    observation = claude_upgrade_candidate_capture.observe_selectors(campaign_dir, manifest, required=True)
    snapshot = claude_upgrade_candidate_capture.runtime_catalog_snapshot(observation, store_campaign["persona"])
    snapshot = claude_upgrade_seal._write_once_or_reuse(stage_dir / SELECTOR_AFTER_FILENAME, snapshot, ignore=("observed_at_utc",))
    inventory_payload = {
        "schema_version": OPERATIONAL_EVIDENCE_SCHEMA,
        "persona": dict(store_campaign["persona"]),
        "manifest_id": INVENTORY_MANIFEST_ID,
        "entries": [
            {"id": "runtime-audit", "facts": {"result": "pass", "audit": dict(audit), "activation_fact": dict(activation_binding)}},
            *(
                {"id": f"{group.replace('_', '-')}-{item}", "facts": {"result": "pass", **binding}}
                for group in ("boundary_checks", "client_bindings")
                for item, binding in sorted(clients[group].items())
            ),
        ],
    }
    inventory_payload["entries"].sort(key=lambda item: item["id"])
    session = claude_upgrade_seal._StoreSession(Path(store.root), str(store_campaign["campaign_id"]), campaign_dir / claude_upgrade_accept.STORE_SCRATCH_RELATIVE)
    inventory_ref = session.seal_object("operational_evidence", inventory_payload)
    snapshot_ref = session.seal_object("runtime_catalog_snapshot", snapshot)
    request = {
        "candidate_id": candidate_id,
        "validation_attempt_ref": dict(store_summary["validation_attempt_ref"]),
        "candidate_evidence_package_ref": dict(store_summary["candidate_evidence_package_ref"]),
        "pair_rows": written_rows,
        "external_gate": external,
        "inventory_assertion_refs": [dict(inventory_ref)],
        "selector_after_snapshot_ref": dict(snapshot_ref),
        "issued_at_utc": orchestrator._utc_now(),
    }
    request = claude_upgrade_seal._write_once_or_reuse(stage_dir / STORE_REQUEST_FILENAME, request, ignore=("issued_at_utc",))
    chain = claude_upgrade_accept.record_acceptance_chain(campaign_dir, manifest, request)
    purpose = str(manifest["campaign_purpose"])
    store_record = {
        "validation_attempt_ref": dict(store_summary["validation_attempt_ref"]),
        "candidate_evidence_package_ref": dict(store_summary["candidate_evidence_package_ref"]),
        "pair_refs": chain["effective_pair_refs"],
        "gate_receipts": [{"gate_id": item["gate_id"], "receipt_ref": item["receipt_ref"]} for item in chain["gate_receipts"]],
        "acceptance_ref": chain["acceptance"]["fact_ref"],
        "selector_after_ref": chain["selector_after"]["fact_ref"],
        "validation_completed_ref": chain["validation_completed"]["fact_ref"],
        "inventory_assertion_ref": dict(inventory_ref),
        "pair_schema": chain["pair_schema"],
        "acceptance_schema": chain["acceptance_schema"],
    }
    seal_payload = {
        "schema_version": EVIDENCE_SEAL_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "candidate_id": candidate_id,
        "campaign_manifest_sha256": file_sha256(campaign_dir / "campaign.json"),
        "campaign_purpose": purpose,
        "candidate_purpose": purpose,
        "production_state": PRODUCTION_STATE,
        "evaluation_baseline": int(baseline),
        "official_package_digest": str(comparison["official"]["package_ref"]["sha256"]),
        "candidate_package_digest": identity["candidate_package_digest"],
        "comparison_package_digest": comparison["package_digest"],
        "assertion_result": assertions["results"],
        "evaluation_run": assertions["evaluation_run"],
        "candidate_external_gate": chain["external_gate"],
        "runtime_audit": compare._campaign_binding(campaign_dir, audit_path),
        "build_receipt": build_binding,
        "store": store_record,
    }
    seal_path = stage_dir / EVIDENCE_SEAL_FILENAME
    claude_upgrade_seal._write_once_or_reuse(seal_path, seal_payload)
    result = {
        "schema_version": ACCEPT_SCHEMA,
        "client": "claude",
        "campaign_id": str(manifest["campaign_id"]),
        "stage": STAGE_NAME,
        "candidate_id": candidate_id,
        "status": "complete",
        "accepted": True,
        "campaign_purpose": purpose,
        "candidate_purpose": purpose,
        "production_state": PRODUCTION_STATE,
        "candidate_revision": current["revision"],
        "validation_round": int(candidate.get("validation_round", claude_upgrade_candidate_seal.FIRST_ROUND)),
        "evaluation_baseline": int(baseline),
        "comparison": compare._campaign_binding(campaign_dir, orchestrator._stage_path(campaign_dir, "compare", candidate_id)[1]),
        "assertions": {"results": assertions["results"], "evaluation_run": assertions["evaluation_run"]},
        "external_gate": chain["external_gate"],
        "runtime_audit": {**compare._campaign_binding(campaign_dir, audit_path), "activation_fact": activation_binding},
        "client_evidence": clients,
        "pair_rows": {
            "rows": {row["spec_id"]: row for row in rows},
            "written": sorted(str(row["spec_id"]) for row in written_rows),
            "provenance": provenance,
        },
        "store": store_record,
        "evidence_seal": compare._campaign_binding(campaign_dir, seal_path),
        "accepted_at_utc": orchestrator._utc_now(),
    }
    sealed = claude_upgrade_seal._write_once_or_reuse(stage_path, result, ignore=("accepted_at_utc",))
    checkpoint = claude_upgrade_vc.complete_candidate_phase(
        campaign_dir,
        manifest,
        phase="VC-5",
        revision=current["revision"],
        stage_receipt_path=stage_path,
        completed_at_utc=str(sealed["accepted_at_utc"]),
        execute_item_ids=list(candidate.get("execute_item_ids") or []),
        reuse_item_ids=list(candidate.get("reuse_item_ids") or []),
        live_request_count=int(candidate.get("live_request_count") or 0),
        scanned_bytes=int(candidate.get("scanned_bytes") or 0),
    )
    return {
        **sealed,
        "stage_result": str(stage_path),
        "vc5_checkpoint": {
            "path": claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-5", revision=current["revision"]).relative_to(campaign_dir).as_posix(),
            "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        },
    }


# ---------------------------------------------------------------------------
# 五、accept 阶段结果的读侧
# ---------------------------------------------------------------------------


def load_stage_result(campaign_dir: Path, path: Path, candidate_id: str, *, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取并核对 accept 阶段结果（``_load_stage_result("accept")`` 的 Claude 实现）：合同、字段闭集、Campaign、候选、阶段与
    状态，比较收据、断言结果、运行审计与证据封印绑定的摘要。没有阶段结果时报「尚未封存」。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError("accept 阶段结果路径不可信。")
    if not path.exists():
        raise ConfigurationError(f"阶段尚未封存：{STAGE_NAME}（{path.relative_to(campaign_dir).as_posix()} 不存在）")
    result = _read_json(path, "accept 阶段结果")
    campaign_id = (manifest or _read_json(campaign_dir / "campaign.json", "Campaign 核心清单")).get("campaign_id")
    if (
        set(result) != STAGE_RESULT_FIELDS
        or result.get("schema_version") != ACCEPT_SCHEMA
        or result.get("client") != "claude"
        or result.get("campaign_id") != campaign_id
        or result.get("stage") != STAGE_NAME
        or result.get("candidate_id") != candidate_id
        or result.get("status") != "complete"
        or result.get("accepted") is not True
        or not orchestrator._is_rfc3339_timestamp(result.get("accepted_at_utc"))
    ):
        raise ConfigurationError("accept 阶段结果合同、Campaign、候选或状态不一致。")
    runtime = result.get("runtime_audit") or {}
    for label, binding in (
        ("比较收据", result.get("comparison")),
        ("逐规则断言结果", (result.get("assertions") or {}).get("results")),
        ("评估运行索引", (result.get("assertions") or {}).get("evaluation_run")),
        ("运行审计", {"path": runtime.get("path"), "sha256": runtime.get("sha256")}),
        ("验收证据封印", result.get("evidence_seal")),
    ):
        compare._bound_campaign_file(campaign_dir, binding, label)
    return result
