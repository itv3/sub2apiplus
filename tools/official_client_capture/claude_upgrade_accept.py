"""Claude VC-5 验收的 Store 写入层（Claude 升级控制面设计 B4-06a）：候选外部门禁收据、PAIR、门禁定稿、AcceptanceFact、
生产 selector 后态与 VC-5 完成事实。

VC-5 的验收结论最后要落成 Store 的严格事实链（Claude 指南 §4.5.5～§4.5.7）：逐规则 PAIR → 外部门禁收据 → AcceptanceFact →
生产 selector 后态观察 → ``validation_completed``。本模块只负责这条写入链和候选外部门禁收据的重放核对：不读编排器侧的候选
阶段结果、不运行任何命令、不发请求。编排器侧的 compare／accept 读侧根（B4-06b）读完封存证据、跑完逐规则断言后，把规则行、
对象引用与外部门禁收据位置交给 ``record_acceptance_chain``。

一、候选外部门禁（``codex_upgrade_gate_receipt`` 原样复用，不修改）
====================================================================
三条候选门禁（``check-egress-spec``、``full-regression``、``target-platform``）的命令、工作目录与 subject 字段对两客户端相同，
v4 收据由同一模块 ``finalize``／``replay`` 生成与重放（``_is_complete_vc_target`` 以 0.154.0 为门槛，Claude 的 2.x 版本号恒走
完整合同，合同测试锁定）。这里补 Claude 自己的三件事：

* ``candidate_external_gate_binding``：重放收据（``replay`` 逐门禁复核证据与环境连续性），要求候选阶段、``passed``、没有失败
  门禁，subject 的 13 个身份字段与期望值逐字相等（Codex 的同名函数在评估读侧闭包内，不迁出，这里自实现；比 Codex 多核
  ``target_architecture``）。期望值由 ``expected_gate_subject`` 从 Campaign 总计划、Store 候选冻结事实与调用方给的五项候选
  身份（画像 ID／摘要、候选证据包摘要、镜像 ID／引用——只有编排器侧候选阶段结果知道）拼出；
* ``candidate_external_gate_plan_entries``：ValidationExecutionPlan 里三条候选门禁的冻结形状（命令、工作目录、主机、架构、
  需求摘要与复用／前序失败绑定），供建 VC-5 attempt（B4-01）时写进执行计划——计划与定稿出自同一处定义；
* ``gate_result_inputs``：v4 收据的有效门禁（``effective_gates``，含重试时携带的已通过门禁）逐条转成 ``validation-gate-finalize``
  的输入。命令、工作目录、主机、架构与需求摘要必须与执行计划逐项相等；计划里 ``execute`` 的门禁必须是本轮执行的
  （``executed``），``reuse`` 的必须是携带的（``carried``）。输出摘要只取执行本身的内容（stdout／stderr 摘要、计数与证据绑定），
  不含处置标记，同一次执行在原收据与携带它的重试收据里摘要相同——Store 复用门禁要求字段逐字等于原收据，由此自然成立。

二、Store 写入链（顺序固定，每步幂等）
====================================
``record_acceptance_chain``：PAIR（本评估基线重跑的规则逐条写）→ 门禁定稿（执行计划里的每条门禁）→ AcceptanceFact →
selector 后态 → ``validation-finalize``。

* 合同版本与基线字段一律取 ``claude_upgrade_baseline.result_baseline_fields``（编排器与 Store 两份登记一致才给）：b0 写 PAIR／
  Acceptance v2、不带基线字段；b<K> 写 v3。b0 的规则行必须覆盖批准的目标规则全集；b<K> 只写本基线重跑的规则，其余规则的
  有效 PAIR 取 Store 里基线不大于 K 的最新一份（与 Store 门禁 ``_require_effective_pairs`` 同一判据）。
* 事实写入都带幂等键（逻辑动作＋payload 摘要），重跑同一步返回原事实；selector 后态的逻辑动作带 attempt 与基线编号，同一快照
  在不同基线各观察一次。外部门禁收据没有幂等键：同一 attempt 同一门禁已有收据时逐字段比对，结果相同即复用，不同失败关闭
  （门禁重跑只能新建 ValidationAttempt，Store 规则）。
* 写入前零写入预检：候选、attempt、CandidateEvidencePackage 与 ProfileApproval 身份；Campaign 用途；外部门禁收据的重放、subject
  与执行计划；门禁收据历史（新 attempt 不得重跑已通过门禁、重试必须绑定最近一份失败收据，与 Store 定稿同一规则）；规则行
  合同与覆盖；selector 前后态的 Active／Rollback／Catalog 不变；inventory 断言对象存在。任一不符即拒绝、一条不写。

调用方必须持有 Campaign 锁（写入链本身不加锁，避免与 accept 读侧根的锁嵌套）。

本文件在 Claude 身份策略里按 ``claude_upgrade_`` 前缀归 control 层；Codex 身份策略忽略它（``claude_`` 前缀）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_baseline
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_gate_receipt as gate_receipt
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.contracts import (
    fact_idempotency_key,
    payload_evaluation_baseline,
    validate_fact_payload,
)
from tools.official_client_control.errors import ControlError

STORE_SCRATCH_RELATIVE = Path("control") / "store-inputs"
PAIR_LOGICAL_ACTION = "claude-upgrade:accept:pair"
ACCEPTANCE_LOGICAL_ACTION = "claude-upgrade:accept:acceptance"
SELECTOR_AFTER_LOGICAL_ACTION = "claude-upgrade:accept:selector-after"
VALIDATION_FINALIZE_LOGICAL_ACTION = "claude-upgrade:accept:validation-finalize"
GATE_REQUIREMENT_SCHEMA = "claude-candidate-external-gate-requirement/v1"
GATE_OUTPUT_SCHEMA = "claude-candidate-external-gate-output/v1"
SELECTOR_OBSERVATION_KIND = "read_only"
# Store 外部门禁收据定稿的输入字段（``build_validation_gate_receipt`` 的闭合键集）。
GATE_RESULT_FIELDS = ("gate_id", "started_at_utc", "completed_at_utc", "exit_code", "output_sha256", "result")
# v4 门禁 status → Store 门禁 result。
GATE_STATUS_RESULTS = {"passed": "pass", "failed": "failed"}
# 执行计划的门禁字段 → v4 门禁字段（逐项相等）。
GATE_PLAN_TO_RECEIPT = (("command", "command"), ("working_directory", "working_directory"), ("host_id", "host"), ("architecture", "architecture"))
# 计划来源 → v4 有效门禁的处置。
GATE_SOURCE_DISPOSITIONS = {"execute": "executed", "reuse": "carried"}
# 外部门禁 subject 里由调用方给出的候选身份：只有编排器侧候选阶段结果知道这五项。
EXTERNAL_GATE_IDENTITY_FIELDS = (
    "profile_id",
    "profile_digest",
    "candidate_package_digest",
    "candidate_image_id",
    "candidate_image_reference",
)
# subject 里逐字核对的全部字段；另三项生产与 promotion 身份在候选阶段恒为 null，由收据重放强制。
EXTERNAL_GATE_SUBJECT_FIELDS = (
    "campaign_id",
    "campaign_mode",
    "campaign_purpose",
    "candidate_id",
    "candidate_purpose",
    "target_version",
    "target_architecture",
    "candidate_source_tree_sha256",
    *EXTERNAL_GATE_IDENTITY_FIELDS,
)
# 调用方给的一条规则行（逐规则断言的结论）；候选通用引用、合同版本与基线字段由写入层补。
PAIR_ROW_FIELDS = frozenset(
    {
        "spec_id",
        "condition_sha256",
        "scenario_approval_refs",
        "official_result",
        "candidate_result",
        "third_party_results",
        "dynamic_field_checks",
        "assertion_results",
        "implementation_anchors",
        "test_anchors",
        "result_source",
        "comparison_mode",
    }
)
# ``record_acceptance_chain`` 的请求键集。
ACCEPT_REQUEST_FIELDS = frozenset(
    {
        "candidate_id",
        "validation_attempt_ref",
        "candidate_evidence_package_ref",
        "pair_rows",
        "external_gate",
        "inventory_assertion_refs",
        "selector_after_snapshot_ref",
        "issued_at_utc",
    }
)
EXTERNAL_GATE_REQUEST_FIELDS = frozenset({"evidence_root", "receipt_path", "identity"})
# selector 前后态必须不变的快照字段（与 Store ``validation_completed`` 门禁同一组）。
SELECTOR_STABLE_FIELDS = ("catalog_digest", "production_active_ref", "production_rollback_ref")


def _sorted_refs(values: Any) -> Any:
    """引用列表按 Store 规范摘要排序（Store 合同要求引用数组按内容严格排序）；不是列表时原样交给合同校验报错。"""

    if not isinstance(values, list):
        return values
    return sorted((dict(item) if isinstance(item, Mapping) else item for item in values), key=canonical_sha256)


# ---------------------------------------------------------------------------
# 一、候选外部门禁
# ---------------------------------------------------------------------------


def candidate_gate_requirement_sha256(gate_id: str) -> str:
    """一条候选外部门禁的需求摘要：门禁 ID、冻结命令与工作目录、候选阶段与 v4 收据合同。"""

    if gate_id not in gate_receipt.CANDIDATE_COMMANDS:
        raise ConfigurationError(f"{gate_id} 不是候选外部门禁（只有 {'、'.join(sorted(gate_receipt.CANDIDATE_COMMANDS))}）。")
    working_directory, command = gate_receipt.CANDIDATE_COMMANDS[gate_id]
    return canonical_sha256(
        {
            "schema_version": GATE_REQUIREMENT_SCHEMA,
            "phase": gate_receipt.CANDIDATE_PHASE,
            "receipt_schema": gate_receipt.RECEIPT_SCHEMA,
            "gate_id": gate_id,
            "working_directory": working_directory,
            "command": list(command),
        }
    )


def candidate_external_gate_plan_entries(
    *,
    host_id: str,
    architecture: str,
    overrides: Mapping[str, Mapping[str, str]] | None = None,
    reused_receipt_refs: Mapping[str, Mapping[str, Any]] | None = None,
    previous_failed_receipt_refs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """ValidationExecutionPlan.external_gates 里三条候选门禁的冻结形状（按 gate_id 排序）。

    ``host_id``／``architecture`` 是门禁执行主机与架构的缺省值，``overrides`` 按门禁覆盖（``{gate_id: {host_id?, architecture?}}``）；
    ``reused_receipt_refs`` 登记复用前一 attempt 已通过的门禁收据（来源 ``reuse``），``previous_failed_receipt_refs`` 登记重跑门禁
    绑定的最近一份失败收据。建计划的一方还要把 ``execute`` 门禁放进 ``execute_item_ids``、``reuse`` 门禁放进 ``reuse_item_ids``
    （Store 计划合同）。
    """

    known = set(gate_receipt.CANDIDATE_COMMANDS)
    reused = {str(key): dict(value) for key, value in (reused_receipt_refs or {}).items()}
    previous = {str(key): dict(value) for key, value in (previous_failed_receipt_refs or {}).items()}
    patches = {str(key): dict(value) for key, value in (overrides or {}).items()}
    for label, mapping in (("复用收据", reused), ("前序失败收据", previous), ("主机覆盖", patches)):
        unknown = sorted(set(mapping) - known)
        if unknown:
            raise ConfigurationError(f"{label}登记了非候选外部门禁：{'、'.join(unknown)}。")
    both = sorted(set(reused) & set(previous))
    if both:
        raise ConfigurationError(f"同一门禁不能既复用又重跑：{'、'.join(both)}。")
    for gate_id, patch in patches.items():
        if not set(patch) <= {"host_id", "architecture"}:
            raise ConfigurationError(f"门禁 {gate_id} 的覆盖只能给 host_id 与 architecture。")
    entries: list[dict[str, Any]] = []
    for gate_id in sorted(known):
        working_directory, command = gate_receipt.CANDIDATE_COMMANDS[gate_id]
        patch = patches.get(gate_id, {})
        entries.append(
            {
                "gate_id": gate_id,
                "requirement_sha256": candidate_gate_requirement_sha256(gate_id),
                "source": "reuse" if gate_id in reused else "execute",
                "command": list(command),
                "working_directory": working_directory,
                "host_id": str(patch.get("host_id", host_id)),
                "architecture": str(patch.get("architecture", architecture)),
                "reused_receipt_ref": reused.get(gate_id),
                "previous_failed_receipt_ref": previous.get(gate_id),
            }
        )
    return entries


def expected_gate_subject(
    manifest: Mapping[str, Any],
    candidate_payload: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """候选外部门禁收据 subject 的期望值：总计划给 Campaign 身份，Store 候选冻结事实给目标架构与源码树，调用方给五项候选身份。"""

    if not isinstance(identity, Mapping) or set(identity) != set(EXTERNAL_GATE_IDENTITY_FIELDS):
        raise ConfigurationError(f"外部门禁候选身份必须恰好给出 {'、'.join(EXTERNAL_GATE_IDENTITY_FIELDS)}。")
    return {
        "campaign_id": manifest.get("campaign_id"),
        "campaign_mode": manifest.get("campaign_mode"),
        "campaign_purpose": manifest.get("campaign_purpose"),
        "candidate_id": candidate_payload.get("candidate_id"),
        "candidate_purpose": candidate_payload.get("candidate_purpose"),
        "target_version": manifest.get("target_version"),
        "target_architecture": candidate_payload.get("target_architecture"),
        "candidate_source_tree_sha256": candidate_payload.get("source_tree_sha256"),
        **{field: identity[field] for field in EXTERNAL_GATE_IDENTITY_FIELDS},
    }


def candidate_external_gate_binding(
    *,
    evidence_root: Path,
    receipt_path: Path,
    expected_subject: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """独立重放候选外部门禁收据，并与期望 subject 逐字段核对；返回 ``(绑定, 收据)``。

    绑定是 ``{evidence_root, receipt: {path, sha256, bytes}}``（根与相对路径，AcceptanceFact 侧可据此重放）。
    """

    if set(expected_subject) != set(EXTERNAL_GATE_SUBJECT_FIELDS):
        raise ConfigurationError("外部门禁 subject 期望值的字段不闭合。")
    try:
        root = Path(evidence_root).resolve(strict=True)
        receipt = Path(receipt_path)
        resolved = (receipt if receipt.is_absolute() else root / receipt).resolve(strict=True)
        relative = resolved.relative_to(root).as_posix()
        payload = gate_receipt.replay(root, relative)
    except (OSError, RuntimeError, ValueError, gate_receipt.GateReceiptError) as error:
        raise ConfigurationError(f"候选外部门禁收据无法重放：{error}") from error
    subject = payload.get("subject")
    if (
        payload.get("phase") != gate_receipt.CANDIDATE_PHASE
        or payload.get("status") != "passed"
        or payload.get("failed_gate_ids") != []
        or not isinstance(subject, Mapping)
    ):
        raise ConfigurationError("候选外部门禁收据不是全部通过的候选阶段收据。")
    drift = [field for field in EXTERNAL_GATE_SUBJECT_FIELDS if subject.get(field) != expected_subject[field]]
    if drift:
        raise ConfigurationError(f"候选外部门禁收据的 subject 与 Campaign／候选身份不一致（{'、'.join(drift)}）。")
    binding = {
        "evidence_root": str(root),
        "receipt": {"path": relative, "sha256": file_sha256(resolved), "bytes": resolved.stat().st_size},
    }
    return binding, payload


def replay_bound_candidate_external_gate(value: Any, *, expected_subject: Mapping[str, Any]) -> dict[str, Any]:
    """从已保存的绑定（根与相对路径）重放外部门禁收据；摘要或大小漂移即失败关闭。"""

    receipt = value.get("receipt") if isinstance(value, Mapping) else None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"evidence_root", "receipt"}
        or not isinstance(value.get("evidence_root"), str)
        or not isinstance(receipt, Mapping)
        or set(receipt) != {"path", "sha256", "bytes"}
        or not isinstance(receipt.get("path"), str)
    ):
        raise ConfigurationError("外部门禁收据绑定形态非法。")
    binding, payload = candidate_external_gate_binding(
        evidence_root=Path(value["evidence_root"]), receipt_path=Path(receipt["path"]), expected_subject=expected_subject
    )
    if binding != dict(value, receipt=dict(receipt)):
        raise ConfigurationError("外部门禁收据摘要或大小与绑定不一致。")
    return payload


def gate_output_sha256(gate: Mapping[str, Any]) -> str:
    """一次门禁执行的输出摘要：stdout／stderr 摘要、计数与证据绑定；不含处置标记（原收据与携带它的重试收据摘要相同）。"""

    return canonical_sha256(
        {
            "schema_version": GATE_OUTPUT_SCHEMA,
            "stdout_sha256": gate["stdout_sha256"],
            "stderr_sha256": gate["stderr_sha256"],
            "passed_count": gate["passed_count"],
            "failed_count": gate["failed_count"],
            "skipped_count": gate["skipped_count"],
            "evidence": [dict(item) for item in gate["evidence"]],
        }
    )


def gate_result_inputs(payload: Mapping[str, Any], plan_gates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """v4 收据的有效门禁逐条转成 ``validation-gate-finalize`` 的输入（按 gate_id 排序），与执行计划逐项核对。"""

    effective = payload.get("effective_gates")
    if not isinstance(effective, list):
        raise ConfigurationError("候选外部门禁收据缺少有效门禁集合。")
    by_id: dict[str, Mapping[str, Any]] = {}
    for gate in effective:
        gate_id = gate.get("gate_id") if isinstance(gate, Mapping) else None
        if not isinstance(gate_id, str) or gate_id in by_id:
            raise ConfigurationError("候选外部门禁收据的有效门禁不唯一或形态非法。")
        by_id[gate_id] = gate
    planned_ids = sorted(str(item.get("gate_id")) for item in plan_gates)
    if planned_ids != sorted(by_id):
        raise ConfigurationError(
            f"候选外部门禁收据的有效门禁集合与执行计划不一致（计划 {planned_ids}，收据 {sorted(by_id)}）。"
        )
    results: list[dict[str, Any]] = []
    for planned in sorted(plan_gates, key=lambda item: str(item["gate_id"])):
        gate_id = str(planned["gate_id"])
        gate = by_id[gate_id]
        if planned.get("requirement_sha256") != candidate_gate_requirement_sha256(gate_id):
            raise ConfigurationError(f"执行计划里门禁 {gate_id} 的需求摘要不是候选外部门禁的冻结需求。")
        mismatched = [plan_key for plan_key, gate_key in GATE_PLAN_TO_RECEIPT if planned.get(plan_key) != gate.get(gate_key)]
        if mismatched:
            raise ConfigurationError(f"门禁 {gate_id} 的执行与执行计划不一致（{'、'.join(mismatched)}）。")
        expected_disposition = GATE_SOURCE_DISPOSITIONS.get(str(planned.get("source")))
        if gate.get("disposition") != expected_disposition:
            raise ConfigurationError(
                f"门禁 {gate_id} 在执行计划里是 {planned.get('source')}，收据里的处置却是 {gate.get('disposition')}。"
            )
        status = gate.get("status")
        if status not in GATE_STATUS_RESULTS:
            raise ConfigurationError(f"门禁 {gate_id} 的状态非法：{status}")
        results.append(
            {
                "gate_id": gate_id,
                "started_at_utc": gate["started_at_utc"],
                "completed_at_utc": gate["completed_at_utc"],
                "exit_code": gate["exit_code"],
                "output_sha256": gate_output_sha256(gate),
                "result": GATE_STATUS_RESULTS[str(status)],
            }
        )
    return results


# ---------------------------------------------------------------------------
# 二、Store 写入链
# ---------------------------------------------------------------------------


class _AcceptStoreSession(claude_upgrade_seal._StoreSession):
    """在 seal 的 Store 会话之上补两条没有通用事实入口的写入：外部门禁收据定稿与 VC-5 完成事实。"""

    def finalize_gate(self, attempt_ref: Mapping[str, Any], result_input: Mapping[str, Any]) -> dict[str, Any]:
        def with_input(input_path: Path) -> Any:
            return self._with_input(
                attempt_ref,
                lambda attempt_path: claude_upgrade_seal.store_cli(
                    [
                        "validation-gate-finalize", "--store", self.root, "--campaign", self.campaign_id,
                        "--attempt-ref", attempt_path, "--input", input_path,
                    ]
                ),
            )

        return dict(self._with_input(result_input, with_input))

    def finalize_validation(
        self,
        acceptance_ref: Mapping[str, Any],
        selector_after_ref: Mapping[str, Any],
        *,
        issued_at: str,
        logical_action: str,
    ) -> dict[str, Any]:
        inputs = {"acceptance_ref": dict(acceptance_ref), "selector_after_ref": dict(selector_after_ref)}
        key = fact_idempotency_key(logical_action, canonical_sha256(inputs), "validation_completed")
        producer = claude_upgrade_seal._producer_run()

        def with_acceptance(acceptance_path: Path) -> Any:
            def with_selector(selector_path: Path) -> Any:
                argv: list[Any] = [
                    "validation-finalize", "--store", self.root, "--campaign", self.campaign_id,
                    "--acceptance-ref", acceptance_path, "--selector-after-ref", selector_path,
                    "--issued-at", issued_at, "--idempotency-key", key,
                ]
                if producer is None:
                    return claude_upgrade_seal.store_cli(argv)
                return self._with_input(
                    producer, lambda producer_path: claude_upgrade_seal.store_cli([*argv, "--producer-run", producer_path])
                )

            return self._with_input(inputs["selector_after_ref"], with_selector)

        outcome = self._with_input(inputs["acceptance_ref"], with_acceptance)
        return {"fact_ref": dict(outcome["fact_ref"]), "reused": bool(outcome["reused"]), "idempotency_key": key}


def _load_fact(store: Any, reference: Any, kind: str, label: str) -> dict[str, Any]:
    try:
        fact = store.load_fact(dict(reference))
    except (ControlError, TypeError, ValueError) as error:
        raise ConfigurationError(f"{label}引用无法读取：{error}") from error
    if fact.get("fact_kind") != kind:
        raise ConfigurationError(f"{label}引用的不是 {kind} 事实。")
    return fact


def _load_object(store: Any, reference: Any, kind: str, label: str) -> dict[str, Any]:
    if not isinstance(reference, Mapping) or reference.get("object_kind") != kind:
        raise ConfigurationError(f"{label}引用的不是 {kind} 对象。")
    try:
        return dict(store.load_object(dict(reference))["payload"])
    except (ControlError, KeyError, TypeError, ValueError) as error:
        raise ConfigurationError(f"{label}引用无法读取：{error}") from error


def _require_request(request: Any) -> None:
    if not isinstance(request, Mapping) or set(request) != ACCEPT_REQUEST_FIELDS:
        raise ConfigurationError(f"验收写入请求的字段必须恰好是 {'、'.join(sorted(ACCEPT_REQUEST_FIELDS))}。")
    gate = request["external_gate"]
    if not isinstance(gate, Mapping) or set(gate) != EXTERNAL_GATE_REQUEST_FIELDS:
        raise ConfigurationError(f"外部门禁请求的字段必须恰好是 {'、'.join(sorted(EXTERNAL_GATE_REQUEST_FIELDS))}。")
    if not isinstance(request["pair_rows"], list) or not isinstance(request["inventory_assertion_refs"], list):
        raise ConfigurationError("规则行与 inventory 断言引用必须是数组。")
    issued = request["issued_at_utc"]
    try:
        parsed = datetime.fromisoformat(str(issued).replace("Z", "+00:00"))
    except ValueError as error:
        raise ConfigurationError("issued_at_utc 不是 RFC3339 时间。") from error
    if parsed.tzinfo is None:
        raise ConfigurationError("issued_at_utc 必须带时区。")


def _acceptance_context(store: Any, campaign: Mapping[str, Any], manifest: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    """候选、attempt、执行计划、CandidateEvidencePackage、ProfileApproval 与 SupportEnvelope 的身份闭合（零写入）。"""

    candidate_id = str(request["candidate_id"])
    revision, revision_candidate = claude_upgrade_baseline._store_revision(store, campaign)
    if revision_candidate is not None and revision_candidate != candidate_id:
        raise ConfigurationError(f"Store 当前候选 revision r{revision} 的候选是 {revision_candidate}，不是 {candidate_id}。")
    candidate = claude_upgrade_baseline._store_candidate_fact(store, campaign, candidate_id, revision)
    if candidate is None:
        raise ConfigurationError(f"Store 没有候选 {candidate_id} 在 r{revision} 的冻结事实。")
    candidate_payload = dict(candidate["payload"])
    if candidate_payload.get("schema_version") is None:
        raise ConfigurationError("VC-5 验收只适用于严格 v2 Candidate。")
    candidate_ref = dict(store.fact_ref(candidate))
    purpose = candidate_payload.get("candidate_purpose")
    if manifest.get("campaign_purpose") != purpose:
        raise ConfigurationError(f"Campaign 用途 {manifest.get('campaign_purpose')} 与候选用途 {purpose} 不一致。")
    attempt_ref = dict(request["validation_attempt_ref"])
    attempt = _load_fact(store, attempt_ref, "validation_attempt_created", "ValidationAttempt ")
    if attempt["campaign_id"] != campaign["campaign_id"] or attempt["payload"]["candidate_ref"] != candidate_ref:
        raise ConfigurationError("ValidationAttempt 不属于本 Campaign 的当前候选。")
    plan = _load_object(store, attempt["payload"]["validation_execution_plan_ref"], "validation_execution_plan", "执行计划")
    package_ref = dict(request["candidate_evidence_package_ref"])
    package = _load_object(store, package_ref, "candidate_evidence_package", "CandidateEvidencePackage ")
    if (package.get("validation_attempt_ref"), package.get("candidate_ref"), package.get("candidate_build_receipt_ref")) != (
        attempt_ref,
        candidate_ref,
        candidate_payload["candidate_build_receipt_ref"],
    ):
        raise ConfigurationError("CandidateEvidencePackage 与 ValidationAttempt／候选／构建收据身份不一致。")
    approval = _load_fact(store, candidate_payload["profile_approval_ref"], "profile_approved", "ProfileApproval ")
    if approval["payload"].get("approval_purpose") != purpose:
        raise ConfigurationError("ProfileApproval 用途与候选用途不一致。")
    support = _load_object(store, approval["payload"]["support_envelope_ref"], "support_envelope", "SupportEnvelope ")
    return {
        "candidate_id": candidate_id,
        "candidate_ref": candidate_ref,
        "candidate_payload": candidate_payload,
        "attempt_ref": attempt_ref,
        "attempt_payload": dict(attempt["payload"]),
        "plan": plan,
        "package_ref": package_ref,
        "package": package,
        "approval_payload": dict(approval["payload"]),
        "support": support,
        "purpose": purpose,
    }


def _with_sorted_evidence(item: Any) -> Any:
    if isinstance(item, Mapping) and isinstance(item.get("evidence_refs"), list):
        return dict(item, evidence_refs=_sorted_refs(item["evidence_refs"]))
    return item


def _sorted_by(values: Any, key: str) -> Any:
    if isinstance(values, list) and all(isinstance(item, Mapping) and isinstance(item.get(key), str) for item in values):
        return sorted((dict(item) for item in values), key=lambda item: item[key])
    return values


def pair_payload(row: Mapping[str, Any], context: Mapping[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
    """一条规则行补齐为 PAIR payload（合同版本与基线字段取当前评估基线），按 Store 合同规范化排序并校验。"""

    if not isinstance(row, Mapping) or set(row) != PAIR_ROW_FIELDS:
        raise ConfigurationError(f"规则行的字段必须恰好是 {'、'.join(sorted(PAIR_ROW_FIELDS))}。")
    spec_id = str(row["spec_id"])
    candidate_payload = context["candidate_payload"]
    payload = {
        "schema_version": fields["pair_schema"],
        "pair_id": f"PAIR-{spec_id}",
        "spec_id": spec_id,
        "candidate_id": context["candidate_id"],
        "profile_approval_ref": dict(candidate_payload["profile_approval_ref"]),
        "release_artifact_ref": dict(candidate_payload["release_artifact_ref"]),
        "condition_sha256": row["condition_sha256"],
        "scenario_approval_refs": _sorted_refs(row["scenario_approval_refs"]),
        "candidate_evidence_package_ref": dict(context["package_ref"]),
        "atomic_assertion_ledger_ref": dict(context["package"]["atomic_assertion_ledger_ref"]),
        "official_result": _with_sorted_evidence(row["official_result"]),
        "candidate_result": _with_sorted_evidence(row["candidate_result"]),
        "third_party_results": _sorted_by(
            [_with_sorted_evidence(item) for item in row["third_party_results"]]
            if isinstance(row["third_party_results"], list)
            else row["third_party_results"],
            "protocol_class",
        ),
        "dynamic_field_checks": _sorted_by(row["dynamic_field_checks"], "id"),
        "assertion_results": _sorted_by(row["assertion_results"], "assertion_id"),
        "implementation_anchors": sorted(row["implementation_anchors"]) if isinstance(row["implementation_anchors"], list) else row["implementation_anchors"],
        "test_anchors": sorted(row["test_anchors"]) if isinstance(row["test_anchors"], list) else row["test_anchors"],
        "result_source": row["result_source"],
        "comparison_mode": row["comparison_mode"],
        **dict(fields["payload_fields"]),
    }
    try:
        validate_fact_payload("pair_recorded", payload)
    except ControlError as error:
        raise ConfigurationError(f"规则 {spec_id} 的 PAIR 不符合合同：{error}") from error
    return payload


def _pair_facts(store: Any, campaign: Mapping[str, Any], candidate_id: str) -> list[dict[str, Any]]:
    return [
        fact
        for fact in store.list_facts(str(campaign["campaign_id"]), "validation")
        if fact["fact_kind"] == "pair_recorded" and fact["payload"].get("candidate_id") == candidate_id
    ]


def effective_pairs(store: Any, campaign: Mapping[str, Any], candidate_id: str, baseline: int) -> dict[str, tuple[int, dict[str, Any]]]:
    """每条规则在评估基线 b<baseline> 下的有效 PAIR：基线编号不大于它的 PAIR 中基线最新的一份（``spec_id → (编号, 引用)``）。"""

    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for fact in _pair_facts(store, campaign, candidate_id):
        number = payload_evaluation_baseline(fact["payload"])
        if number > baseline:
            continue
        spec_id = str(fact["payload"]["spec_id"])
        if spec_id not in best or number > best[spec_id][0]:
            best[spec_id] = (number, dict(store.fact_ref(fact)))
    return best


def _require_pair_coverage(
    store: Any,
    campaign: Mapping[str, Any],
    context: Mapping[str, Any],
    pairs: Sequence[Mapping[str, Any]],
    baseline: int,
) -> None:
    """规则行覆盖：唯一、属于批准的目标全集；b0 必须覆盖全集，b<K> 与 Store 里已有的有效 PAIR 合起来覆盖全集。"""

    target = set(context["approval_payload"]["target_spec_ids"])
    specs = [str(item["spec_id"]) for item in pairs]
    duplicated = sorted({spec for spec in specs if specs.count(spec) > 1})
    if duplicated:
        raise ConfigurationError(f"规则行重复：{'、'.join(duplicated)}。")
    outside = sorted(set(specs) - target)
    if outside:
        raise ConfigurationError(f"规则行不属于批准的目标规则全集：{'、'.join(outside)}。")
    if baseline == 0:
        missing = sorted(target - set(specs))
        if missing:
            raise ConfigurationError(f"b0 的规则行必须覆盖批准的目标规则全集，缺 {'、'.join(missing)}。")
        return
    existing = effective_pairs(store, campaign, context["candidate_id"], baseline)
    missing = sorted(target - set(specs) - set(existing))
    if missing:
        raise ConfigurationError(f"评估基线 b{baseline} 下这些规则既没有本基线的规则行，也没有更早基线的有效 PAIR：{'、'.join(missing)}。")


def _existing_gate_receipts(store: Any, attempt_ref: Mapping[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    found: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for reference in store.list_receipt_refs("validation_gate"):
        receipt = store.load_receipt(reference)
        if receipt["validation_attempt_ref"] != dict(attempt_ref):
            continue
        if receipt["gate_id"] in found:
            raise ConfigurationError(f"Store 里同一 ValidationAttempt 的门禁 {receipt['gate_id']} 有多份收据，已不一致。")
        found[str(receipt["gate_id"])] = (dict(reference), receipt)
    return found


def _require_gate_history(store: Any, campaign_id: str, context: Mapping[str, Any], planned: Mapping[str, Any]) -> None:
    """新定稿门禁的历史规则（与 Store ``finalize_validation_gate`` 同一判据，在任何写入之前先核）。"""

    if planned["source"] != "execute":
        return
    failures: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for reference in store.list_receipt_refs("validation_gate"):
        receipt = store.load_receipt(reference)
        if (
            receipt["campaign_id"] != campaign_id
            or receipt["candidate_ref"] != context["candidate_ref"]
            or receipt["gate_id"] != planned["gate_id"]
            or receipt["validation_attempt_ref"] == context["attempt_ref"]
        ):
            continue
        if receipt["result"] != "failed":
            raise ConfigurationError(f"门禁 {planned['gate_id']} 已在别的 attempt 通过：必须复用原收据，不得重复执行。")
        failures.append((dict(reference), receipt))
    previous = planned["previous_failed_receipt_ref"]
    if failures:
        failures.sort(key=lambda item: datetime.fromisoformat(item[1]["completed_at_utc"].replace("Z", "+00:00")))
        if previous != failures[-1][0]:
            raise ConfigurationError(f"重跑门禁 {planned['gate_id']} 必须绑定最近一份前序失败收据。")
    elif previous is not None:
        raise ConfigurationError(f"门禁 {planned['gate_id']} 声明了不存在的前序失败收据。")


def _gate_actions(
    store: Any,
    campaign: Mapping[str, Any],
    context: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """每条门禁要新定稿还是复用已有收据（``[(定稿输入, 已有收据引用或 None)]``）；冲突在任何写入之前失败关闭。"""

    campaign_id = str(campaign["campaign_id"])
    existing = _existing_gate_receipts(store, context["attempt_ref"])
    planned_by_id = {str(item["gate_id"]): item for item in context["plan"]["external_gates"]}
    actions: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for item in inputs:
        gate_id = str(item["gate_id"])
        if item["result"] != "pass":
            raise ConfigurationError(f"外部门禁 {gate_id} 未通过，不能签发 AcceptanceFact。")
        current = existing.get(gate_id)
        if current is not None:
            reference, receipt = current
            drift = [key for key in GATE_RESULT_FIELDS[1:] if receipt[key] != item[key]]
            if drift:
                raise ConfigurationError(
                    f"ValidationAttempt 的外部门禁 {gate_id} 已有不同结果（{'、'.join(drift)}）：门禁收据不得覆盖，门禁重跑须新建 ValidationAttempt。"
                )
            actions.append((dict(item), reference))
            continue
        try:
            store_receipts.build_validation_gate_receipt(store, campaign_id, dict(context["attempt_ref"]), dict(item))
        except ControlError as error:
            raise ConfigurationError(f"外部门禁 {gate_id} 不能定稿：{error}") from error
        _require_gate_history(store, campaign_id, context, planned_by_id[gate_id])
        actions.append((dict(item), None))
    extra = sorted(set(existing) - {str(item["gate_id"]) for item in inputs})
    if extra:
        raise ConfigurationError(f"ValidationAttempt 已有执行计划之外的门禁收据：{'、'.join(extra)}。")
    return actions


def _selector_after_payload(store: Any, context: Mapping[str, Any], snapshot_ref: Any) -> dict[str, Any]:
    """selector 后态观察：快照的 Catalog 摘要、生产 Active 与 Rollback 必须与 attempt 冻结的前态相同。"""

    after = _load_object(store, snapshot_ref, "runtime_catalog_snapshot", "selector 后态快照")
    before_fact = _load_fact(store, context["attempt_payload"]["production_selector_before_ref"], "selector_observed", "selector 前态")
    before = _load_object(store, before_fact["payload"]["catalog_snapshot_ref"], "runtime_catalog_snapshot", "selector 前态快照")
    changed = [field for field in SELECTOR_STABLE_FIELDS if before.get(field) != after.get(field)]
    if changed:
        raise ConfigurationError(f"VC-5 期间生产 Active／Rollback／Catalog 发生变化（{'、'.join(changed)}），不能完成验收。")
    payload = {"catalog_snapshot_ref": dict(snapshot_ref), "observation_kind": SELECTOR_OBSERVATION_KIND}
    try:
        validate_fact_payload("selector_observed", payload)
    except ControlError as error:
        raise ConfigurationError(f"selector 后态观察不符合合同：{error}") from error
    return payload


def _inventory_refs(store: Any, references: Sequence[Any]) -> list[dict[str, Any]]:
    if not references:
        raise ConfigurationError("AcceptanceFact 必须绑定 inventory 断言证据。")
    resolved: list[dict[str, Any]] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            raise ConfigurationError("inventory 断言引用必须是对象引用。")
        try:
            store.load_object(dict(reference))
        except (ControlError, KeyError, TypeError, ValueError) as error:
            raise ConfigurationError(f"inventory 断言证据无法读取：{error}") from error
        resolved.append(dict(reference))
    return _sorted_refs(resolved)


def acceptance_payload(
    context: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    pair_refs: Sequence[Mapping[str, Any]],
    gate_receipt_refs: Sequence[Mapping[str, Any]],
    inventory_refs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """AcceptanceFact payload：引用全部来自已核过的上下文，用途与结果按 ProfileApproval 冻结的用途推出。"""

    candidate_payload = context["candidate_payload"]
    purpose = str(context["purpose"])
    payload: dict[str, Any] = {
        "schema_version": fields["acceptance_schema"],
        "candidate_id": context["candidate_id"],
        "profile_approval_ref": dict(candidate_payload["profile_approval_ref"]),
        "candidate_ref": dict(context["candidate_ref"]),
        "candidate_build_receipt_ref": dict(candidate_payload["candidate_build_receipt_ref"]),
        "validation_attempt_ref": dict(context["attempt_ref"]),
        "candidate_evidence_package_ref": dict(context["package_ref"]),
        "atomic_assertion_ledger_ref": dict(context["package"]["atomic_assertion_ledger_ref"]),
        "pair_refs": _sorted_refs([dict(item) for item in pair_refs]),
        "boundary_assertion_refs": _sorted_refs([dict(item) for item in context["support"]["boundary_assertion_refs"]]),
        "inventory_assertion_refs": _sorted_refs([dict(item) for item in inventory_refs]),
        "external_gate_receipt_refs": _sorted_refs([dict(item) for item in gate_receipt_refs]),
        "acceptance_purpose": purpose,
        "result": "accepted" if purpose == "production_replacement" else "validation_only",
        **dict(fields["payload_fields"]),
    }
    payload["identity_sha256"] = canonical_sha256(payload)
    try:
        validate_fact_payload("acceptance_recorded", payload)
    except ControlError as error:
        raise ConfigurationError(f"AcceptanceFact 不符合合同：{error}") from error
    return payload


def record_acceptance_chain(campaign_dir: Path, manifest: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    """VC-5 验收的 Store 写入链（见模块说明第二节）：零写入预检 → PAIR → 门禁定稿 → AcceptanceFact → selector 后态 →
    ``validation-finalize``。调用方持有 Campaign 锁；重跑同一请求逐步复用、收敛到同一组事实。"""

    _require_request(request)
    campaign_dir = Path(campaign_dir)
    candidate_id = str(request["candidate_id"])
    fields = claude_upgrade_baseline.result_baseline_fields(campaign_dir, candidate_id)
    baseline = int(fields["evaluation_baseline"])
    store, campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    claude_upgrade_seal._require_store_tool_current(store, campaign)

    # 零写入预检。
    context = _acceptance_context(store, campaign, manifest, request)
    external = request["external_gate"]
    subject = expected_gate_subject(manifest, context["candidate_payload"], external["identity"])
    gate_binding, gate_payload = candidate_external_gate_binding(
        evidence_root=Path(external["evidence_root"]), receipt_path=Path(external["receipt_path"]), expected_subject=subject
    )
    gate_inputs = gate_result_inputs(gate_payload, context["plan"]["external_gates"])
    pairs = [pair_payload(row, context, fields) for row in request["pair_rows"]]
    _require_pair_coverage(store, campaign, context, pairs, baseline)
    gate_actions = _gate_actions(store, campaign, context, gate_inputs)
    selector_payload = _selector_after_payload(store, context, request["selector_after_snapshot_ref"])
    inventory_refs = _inventory_refs(store, request["inventory_assertion_refs"])

    session = _AcceptStoreSession(Path(store.root), str(campaign["campaign_id"]), campaign_dir / STORE_SCRATCH_RELATIVE)
    issued_at = str(request["issued_at_utc"])
    # 1. PAIR（本基线重跑的规则）。
    written_pairs = [
        session.record_fact("pair-record", "pair_recorded", payload, issued_at=issued_at, logical_action=PAIR_LOGICAL_ACTION)
        for payload in pairs
    ]
    # 2. 外部门禁收据定稿（已有同结果收据即复用）。
    gate_records: list[dict[str, Any]] = []
    for item, existing in gate_actions:
        if existing is None:
            gate_records.append({"gate_id": item["gate_id"], "receipt_ref": session.finalize_gate(context["attempt_ref"], item), "reused": False})
        else:
            gate_records.append({"gate_id": item["gate_id"], "receipt_ref": existing, "reused": True})
    # 3. AcceptanceFact：每条目标规则取本基线下的有效 PAIR。
    effective = effective_pairs(store, campaign, candidate_id, baseline)
    target = sorted(context["approval_payload"]["target_spec_ids"])
    missing = [spec for spec in target if spec not in effective]
    if missing:
        raise ConfigurationError(f"写入 PAIR 之后仍有目标规则没有有效 PAIR：{'、'.join(missing)}。")
    acceptance = acceptance_payload(
        context,
        fields,
        pair_refs=[effective[spec][1] for spec in target],
        gate_receipt_refs=[record["receipt_ref"] for record in gate_records],
        inventory_refs=inventory_refs,
    )
    acceptance_record = session.record_fact(
        "acceptance-record", "acceptance_recorded", acceptance, issued_at=issued_at, logical_action=ACCEPTANCE_LOGICAL_ACTION
    )
    # 4. selector 后态（逻辑动作带 attempt 与基线：同一快照在不同基线各观察一次）。
    selector_record = session.record_fact(
        "selector-observe",
        "selector_observed",
        selector_payload,
        issued_at=issued_at,
        logical_action=f"{SELECTOR_AFTER_LOGICAL_ACTION}:{context['attempt_payload']['attempt_id']}:b{baseline}",
    )
    # 5. VC-5 完成事实。
    completed = session.finalize_validation(
        acceptance_record["fact_ref"],
        selector_record["fact_ref"],
        issued_at=issued_at,
        logical_action=VALIDATION_FINALIZE_LOGICAL_ACTION,
    )
    return {
        "candidate_id": candidate_id,
        "evaluation_baseline": baseline,
        "pair_schema": fields["pair_schema"],
        "acceptance_schema": fields["acceptance_schema"],
        "external_gate": gate_binding,
        "pairs": [{"spec_id": payload["spec_id"], **record} for payload, record in zip(pairs, written_pairs)],
        "effective_pair_refs": {spec: effective[spec][1] for spec in target},
        "gate_receipts": gate_records,
        "acceptance": acceptance_record,
        "selector_after": selector_record,
        "validation_completed": completed,
    }
