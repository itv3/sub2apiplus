"""Claude 候选证据封存（Claude 升级控制面设计 B4-02）：``capture-candidate seal`` 的预览、批准与 Store 写入层。

两层：第一至第三节是 Store 写入层（B4-02 第一步，不依赖证据格式，用 Store 夹具验收）；第四、五节是证据侧（B4-02
第二步，按 B4-01a 的候选清单合同）——预览（客户端检查点、候选证据视图与草稿、唯一一次深度扫描、评估用 manifest）、
批准（把证据侧算好的结果组装成第一节的请求，写 Store 后写候选阶段结果）与候选阶段结果的读侧。证据语义经编排器的
evidence 根在 ``claude_upgrade_evidence`` 的候选一节。run 不碰 Store：VC-5 入口的三类 Store 事实都在批准时一次写完。

一、写入链（``record_candidate_seal_chain``，顺序固定、每步幂等）
==================================================================
对象先封存（内容寻址、``--allow-existing``），事实后追加（幂等键＝逻辑动作＋payload 摘要），重跑同一请求逐步复用，被强杀
后重跑收敛到同一组事实：

1. 对象：每个 ScenarioPlan 场景的证据清单（capture 阶段）与封存清单（seal 阶段）、本次批准的审核清单（approve 阶段）、
   环境前后／恢复核验／秘密扫描四份 ``operational_evidence``、生产 selector 前态快照（``runtime_catalog_snapshot``）；
2. ``selector_observed``（前态，只读观察）；
3. ``validation_execution_plan`` 对象：外部门禁一律由 ``claude_upgrade_accept.candidate_external_gate_plan_entries`` 生成
   （与 B4-06a 的门禁定稿出自同一处定义），三条门禁的 gate_id 与本次执行的候选 Job 一起进 ``execute_item_ids``，复用的
   Job 进 ``reuse_item_ids``；``run_conditions`` 的工具摘要取 Store 当前有效工具身份；
4. ``validation_attempt_created``：attempt ID 是 ``<候选采集 attempt>-v<轮次>``（见第三节）；
5. 每个 ScenarioPlan 场景依次 ``scenario_prepared → captured → sealed → approved``（批准人与审核引用取 seal 预览的批准）；
6. ``candidate_evidence_package`` 对象。

调用方持有 Campaign 锁（与 B4-06a 的写入链同一约定，本层不加锁）。写入前做完整的零写入预检：候选属于 Store 当前
revision 且未作废、严格 v2 候选、Campaign 用途与候选一致、请求形状、场景精确覆盖 ScenarioPlan、证据绑定都在原始证据清单
里、四份运行证据全部通过、同一 attempt ID 已有不同的 ValidationAttempt 即拒绝、scenario-only 断言能映射到场景。

二、CandidateEvidencePackage 的取值（10-07 定，供 B4-06b）
===========================================================
* ``package_id``＝``<ValidationAttempt ID>-evidence``；``attempt_id`` 等于 ValidationAttempt 的 attempt_id。
* ``scenario_approval_refs``：每个 ScenarioPlan 场景一条批准，按 Store 规范摘要排序。
* ``scenario_assertion_results``：只覆盖 AtomicAssertionLedger 里 ``owner_kind == scenario_only`` 的断言（官方客户端的本地
  行为，VC-1 已核验、候选改不了），一律 ``source = reuse``、``result = pass``；场景批准引用的取法：该断言的
  ``evidence_item_ids`` → VC-1 EvidencePackage（台账经 RuleMigrationLedger 绑定的那一份）里这些证据项的 ``source_ids``
  （场景）→ 与 ScenarioPlan 求交 → 这些场景的候选批准。交集为空、或台账里这条断言本身不是 pass，闭合门禁 blocked、
  失败关闭（``plan_candidate_seal_chain`` 报 ``scenario_only_unmapped``／``scenario_only_not_passed``）。
* ``raw_evidence_inventory``：封存深度扫描到的全部文件（路径相对宿主数据根，按路径排序）；每个场景的证据绑定与 capture
  manifest 绑定都必须在里面。
* 返回值给出 ``scenario_approval_refs``（场景 → 批准引用）：B4-06b 填每条规则 PAIR 的场景批准时直接取 ScenarioPlan 里
  spec_ids 含该规则的场景，不必重算。

三、为门禁重试留的余地（这一层现在只接受第 1 轮）
================================================
Store 规定外部门禁失败后不能在原 attempt 重跑，必须新建 ValidationAttempt；Acceptance 又要求证据包与场景批准链绑定同一个
attempt。所以将来门禁失败时，要用同一份原始证据零请求地重新封存到新 attempt 上：

* 请求带 ``validation_round``（第 1 轮＝首次封存；第 N 轮＝第 N-1 次门禁失败后的重新封存），ValidationAttempt ID 是
  ``<候选采集 attempt>-v<N>``，逻辑动作都带这个 ID，所以每一轮的计划、attempt、场景四阶段与证据包都是新的事实与对象；
* 复用不换新的：候选采集 attempt、原始证据、各场景证据清单与封存清单、审核清单、四份运行证据（清单 ID 只带采集 attempt，
  内容相同即同一对象）、selector 前态事实（第 1 轮写下，后续轮次沿用同一引用）；
* 换新的：ValidationExecutionPlan（外部门禁按 Store 门禁收据历史生成：已通过的记 ``reuse``＋``reused_receipt_refs``，失败的
  记 ``execute``＋``previous_failed_receipt_refs``，仍经 ``candidate_external_gate_plan_entries``）、``validation_attempt_created``、
  场景四阶段、CandidateEvidencePackage；
* 入口将是 ``capture-candidate seal --attempt-id <采集 attempt> --revalidate``（零请求、不重新扫描，读第 1 轮的封存与 Store
  门禁收据历史组装请求）。本层收到 ``validation_round > 1`` 时报待实现。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_rule_assertion
from tools.official_client_capture import claude_upgrade_accept
from tools.official_client_capture import claude_upgrade_baseline
from tools.official_client_capture import claude_upgrade_evidence
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    expect_rfc3339,
    sha256_bytes,
    validate_external_binding,
)
from tools.official_client_control.contracts import (
    CANDIDATE_EVIDENCE_PACKAGE_SCHEMA,
    OBJECT_SCHEMA,
    SCENARIO_STAGE_V2_SCHEMA,
    VALIDATION_ATTEMPT_SCHEMA,
    VALIDATION_EXECUTION_PLAN_SCHEMA,
    validate_fact_payload,
    validate_object_document,
)
from tools.official_client_control.errors import ControlError

STORE_SCRATCH_RELATIVE = Path("control") / "store-inputs"
LOGICAL_ACTION_PREFIX = "claude-upgrade:capture-candidate:seal"
OPERATIONAL_EVIDENCE_SCHEMA = "official-client-operational-evidence/v1"
# 用户管理的生产环境状态（设计附录 G 例外④：字段名沿用 vircs_state）：候选验证不连接、不修改、不验证生产环境。
PRODUCTION_STATE = {"ownership": "operator_managed", "verification": "unverified", "action": "not_touched"}
SELECTOR_OBSERVATION_KIND = "read_only"
FIRST_ROUND = 1
REVALIDATE_WORK_ITEM = "B4-02 门禁重试链（外部门禁失败后零请求重新封存到新 ValidationAttempt）"
REQUEST_FIELDS = frozenset(
    {
        "candidate_id",
        "capture_attempt_id",
        "validation_round",
        "issued_at_utc",
        "run_conditions",
        "selector_before_snapshot",
        "gate_execution",
        "job_ids",
        "scenario_evidence",
        "seal",
        "raw_evidence_inventory",
        "operational_evidence",
    }
)
# 请求给出的运行条件（工具摘要由本层取 Store 当前有效工具身份补上）。
RUN_CONDITION_FIELDS = ("run_nonce", "environment_sha256", "configuration_sha256", "network_sha256", "account_model_sha256")
GATE_EXECUTION_FIELDS = frozenset({"host_id", "architecture"})
GATE_EXECUTION_OPTIONAL = frozenset({"overrides"})
SEAL_FIELDS = frozenset({"generation", "review_sha256", "reviewer", "evidence_manifest", "capture_manifest"})
# 四份运行证据：请求键 → CandidateEvidencePackage 字段。
OPERATIONAL_EVIDENCE = {
    "environment_before": "environment_before_ref",
    "environment_after": "environment_after_ref",
    "recovery": "recovery_evidence_ref",
    "secret_scan": "secret_scan_evidence_ref",
}
SCENARIO_STAGES = (
    ("prepare", "scenario-prepare", "scenario_prepared", "prepared"),
    ("capture", "scenario-capture", "scenario_captured", "pass"),
    ("seal", "scenario-seal", "scenario_sealed", "pass"),
    ("approve", "scenario-approve", "scenario_approved", "pass"),
)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 一、请求形状
# ---------------------------------------------------------------------------


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ConfigurationError(f"{label} 不是合法标识：{value!r}")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ConfigurationError(f"{label} 不是 SHA-256。")
    return value


def _binding(value: Any, label: str) -> dict[str, Any]:
    try:
        return dict(validate_external_binding(value, label))
    except ControlError as error:
        raise ConfigurationError(f"{label} 不是合法外部证据绑定：{error}") from error


def _bindings(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{label} 必须是非空的外部证据绑定数组。")
    bindings = [_binding(item, f"{label}[{index}]") for index, item in enumerate(value)]
    paths = [item["path"] for item in bindings]
    if paths != sorted(set(paths)):
        raise ConfigurationError(f"{label} 必须按路径排序且不得重复。")
    return bindings


def _ids(value: Any, label: str, *, non_empty: bool) -> list[str]:
    if not isinstance(value, list) or (non_empty and not value):
        raise ConfigurationError(f"{label} 必须是{'非空' if non_empty else ''}字符串数组。")
    items = [_safe_id(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if items != sorted(set(items)):
        raise ConfigurationError(f"{label} 必须排序且不得重复。")
    return items


def _entries(value: Any, label: str) -> list[dict[str, Any]]:
    """运行证据条目：``[{id, facts}]``，按 id 排序、不重复，每条 ``facts.result == pass``（Store 要求清单全部通过）。"""

    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{label} 必须是非空条目数组。")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"id", "facts"} or not isinstance(item["facts"], Mapping):
            raise ConfigurationError(f"{label}[{index}] 必须恰好是 {{id, facts}}。")
        _safe_id(item["id"], f"{label}[{index}].id")
        if item["facts"].get("result") != "pass":
            raise ConfigurationError(f"{label} 的条目 {item['id']} 没有通过：候选证据包只接受全部通过的运行证据。")
        entries.append({"id": str(item["id"]), "facts": dict(item["facts"])})
    ids = [entry["id"] for entry in entries]
    if ids != sorted(set(ids)):
        raise ConfigurationError(f"{label} 的条目必须按 id 排序且不得重复。")
    return entries


def normalize_request(request: Any) -> dict[str, Any]:
    """校验并规范化写入请求（字段闭集与各字段形状）；不读 Store。"""

    if not isinstance(request, Mapping) or set(request) != REQUEST_FIELDS:
        raise ConfigurationError(f"候选封存写入请求的字段必须恰好是 {'、'.join(sorted(REQUEST_FIELDS))}。")
    validation_round = request["validation_round"]
    if isinstance(validation_round, bool) or not isinstance(validation_round, int) or validation_round < 1:
        raise ConfigurationError("validation_round 必须是正整数。")
    if validation_round != FIRST_ROUND:
        raise _orchestrator()._pending(f"候选封存第 {validation_round} 轮（门禁重试）", REVALIDATE_WORK_ITEM)
    try:
        issued_at = expect_rfc3339(request["issued_at_utc"], "issued_at_utc")
    except ControlError as error:
        raise ConfigurationError(str(error)) from error
    conditions = request["run_conditions"]
    if not isinstance(conditions, Mapping) or set(conditions) != set(RUN_CONDITION_FIELDS):
        raise ConfigurationError(f"run_conditions 必须恰好是 {'、'.join(RUN_CONDITION_FIELDS)}（工具摘要由 Store 有效身份补）。")
    _safe_id(conditions["run_nonce"], "run_conditions.run_nonce")
    for key in RUN_CONDITION_FIELDS[1:]:
        _sha256(conditions[key], f"run_conditions.{key}")
    gate = request["gate_execution"]
    if not isinstance(gate, Mapping) or not GATE_EXECUTION_FIELDS <= set(gate) <= GATE_EXECUTION_FIELDS | GATE_EXECUTION_OPTIONAL:
        raise ConfigurationError("gate_execution 必须给出 host_id、architecture（可选 overrides）。")
    _safe_id(gate["host_id"], "gate_execution.host_id")
    if not isinstance(gate["architecture"], str) or not gate["architecture"]:
        raise ConfigurationError("gate_execution.architecture 非法。")
    jobs = request["job_ids"]
    if not isinstance(jobs, Mapping) or set(jobs) != {"execute", "reuse"}:
        raise ConfigurationError("job_ids 必须恰好是 {execute, reuse}。")
    execute = _ids(jobs["execute"], "job_ids.execute", non_empty=False)
    reuse = _ids(jobs["reuse"], "job_ids.reuse", non_empty=False)
    if not execute and not reuse:
        raise ConfigurationError("job_ids 不能同时为空：候选封存必须绑定至少一个候选 Job。")
    if set(execute) & set(reuse):
        raise ConfigurationError("job_ids 的 execute 与 reuse 不得相交。")
    scenarios = request["scenario_evidence"]
    if not isinstance(scenarios, Mapping) or not scenarios:
        raise ConfigurationError("scenario_evidence 必须是非空的「场景 → 证据绑定」映射。")
    scenario_evidence = {
        _safe_id(scenario, "scenario_evidence 的场景"): _bindings(bindings, f"scenario_evidence.{scenario}")
        for scenario, bindings in scenarios.items()
    }
    seal = request["seal"]
    if not isinstance(seal, Mapping) or set(seal) != SEAL_FIELDS:
        raise ConfigurationError(f"seal 必须恰好是 {'、'.join(sorted(SEAL_FIELDS))}。")
    _safe_id(seal["generation"], "seal.generation")
    _sha256(seal["review_sha256"], "seal.review_sha256")
    if not isinstance(seal["reviewer"], str) or not seal["reviewer"].strip():
        raise ConfigurationError("seal.reviewer 不能为空。")
    inventory = _bindings(request["raw_evidence_inventory"], "raw_evidence_inventory")
    operational = request["operational_evidence"]
    if not isinstance(operational, Mapping) or set(operational) != set(OPERATIONAL_EVIDENCE):
        raise ConfigurationError(f"operational_evidence 必须恰好是 {'、'.join(OPERATIONAL_EVIDENCE)}。")
    normalized = {
        "candidate_id": _safe_id(request["candidate_id"], "candidate_id"),
        "capture_attempt_id": _safe_id(request["capture_attempt_id"], "capture_attempt_id"),
        "validation_round": validation_round,
        "issued_at_utc": issued_at,
        "run_conditions": {key: str(conditions[key]) for key in RUN_CONDITION_FIELDS},
        "selector_before_snapshot": request["selector_before_snapshot"],
        "gate_execution": dict(gate),
        "job_ids": {"execute": execute, "reuse": reuse},
        "scenario_evidence": scenario_evidence,
        "seal": {
            "generation": str(seal["generation"]),
            "review_sha256": str(seal["review_sha256"]),
            "reviewer": str(seal["reviewer"]),
            "evidence_manifest": _binding(seal["evidence_manifest"], "seal.evidence_manifest"),
            "capture_manifest": _binding(seal["capture_manifest"], "seal.capture_manifest"),
        },
        "raw_evidence_inventory": inventory,
        "operational_evidence": {name: _entries(operational[name], f"operational_evidence.{name}") for name in OPERATIONAL_EVIDENCE},
    }
    inventory_paths = {item["path"]: item for item in inventory}
    for scenario, bindings in scenario_evidence.items():
        for binding in bindings:
            if inventory_paths.get(binding["path"]) != binding:
                raise ConfigurationError(f"场景 {scenario} 的证据 {binding['path']} 不在原始证据清单里（或摘要不同）。")
    capture = normalized["seal"]["capture_manifest"]
    if inventory_paths.get(capture["path"]) != capture:
        raise ConfigurationError("capture manifest 不在原始证据清单里（或摘要不同）。")
    return normalized


def validation_attempt_id(capture_attempt_id: str, validation_round: int) -> str:
    """ValidationAttempt ID：``<候选采集 attempt>-v<轮次>``（第三节）。"""

    return _safe_id(f"{capture_attempt_id}-v{int(validation_round)}", "ValidationAttempt ID")


# ---------------------------------------------------------------------------
# 二、Store 读取与零写入规划
# ---------------------------------------------------------------------------


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


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


def _object_ref(kind: str, payload: Mapping[str, Any]) -> dict[str, str]:
    """对象内容寻址引用（与 ``ControlStore.seal_object_outcome`` 同一算法），并按 Store 合同校验对象形状；不封存。"""

    document = {"schema_version": OBJECT_SCHEMA, "object_kind": kind, "payload": dict(payload)}
    try:
        validate_object_document(document)
    except ControlError as error:
        raise ConfigurationError(f"{kind} 对象不符合 Store 合同：{error}") from error
    return {"object_kind": kind, "sha256": sha256_bytes(canonical_json_bytes(document))}


def _sorted_refs(references: Any) -> list[dict[str, Any]]:
    """Store 引用数组的规范形态：按 ``canonical_sha256`` 严格排序、去重。"""

    unique = {canonical_sha256(dict(reference)): dict(reference) for reference in references}
    return [unique[key] for key in sorted(unique)]


def _with_identity(payload: dict[str, Any]) -> dict[str, Any]:
    payload["identity_sha256"] = ""
    payload["identity_sha256"] = canonical_sha256({key: value for key, value in payload.items() if key != "identity_sha256"})
    return payload


def _manifest(persona: Mapping[str, Any], manifest_id: str, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": OPERATIONAL_EVIDENCE_SCHEMA,
        "persona": dict(persona),
        "manifest_id": _safe_id(manifest_id, "证据清单编号"),
        "entries": sorted((dict(entry) for entry in entries), key=lambda entry: str(entry["id"])),
    }


def _binding_entries(prefix: str, bindings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": f"{prefix}-{index:04d}", "facts": {"result": "pass", **dict(binding)}}
        for index, binding in enumerate(bindings, start=1)
    ]


def _candidate_context(store: Any, campaign: Mapping[str, Any], manifest: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    """候选、ProfileApproval、ScenarioPlan、AtomicAssertionLedger、VC-1 EvidencePackage 与构建收据（零写入）。"""

    revision, revision_candidate = claude_upgrade_baseline._store_revision(store, campaign)
    if revision_candidate is not None and revision_candidate != candidate_id:
        raise ConfigurationError(f"Store 当前候选 revision r{revision} 的候选是 {revision_candidate}，不是 {candidate_id}。")
    try:
        if store_receipts.revision_invalidation(store, dict(campaign), revision) is not None:
            raise ConfigurationError(f"候选 revision r{revision} 已作废（revision_required）：作废候选不得封存证据。")
    except ControlError as error:
        raise ConfigurationError(f"Store 候选作废状态无法确定：{error}") from error
    candidate = claude_upgrade_baseline._store_candidate_fact(store, campaign, candidate_id, revision)
    if candidate is None:
        raise ConfigurationError(f"Store 没有候选 {candidate_id} 在 r{revision} 的冻结事实（先完成 VC-4 record-candidate-build）。")
    candidate_payload = dict(candidate["payload"])
    if candidate_payload.get("schema_version") is None:
        raise ConfigurationError("候选证据封存只适用于严格 v2 Candidate。")
    purpose = candidate_payload.get("candidate_purpose")
    if manifest.get("campaign_purpose") != purpose:
        raise ConfigurationError(f"Campaign 用途 {manifest.get('campaign_purpose')} 与候选用途 {purpose} 不一致。")
    approval_ref = candidate_payload["profile_approval_ref"]
    approval = _load_fact(store, approval_ref, "profile_approved", "ProfileApproval ")["payload"]
    evidence_approval = _load_fact(store, approval["evidence_approval_ref"], "evidence_approved", "EvidenceApproval ")["payload"]
    classification_ref = evidence_approval.get("classification_fact_ref")
    if classification_ref is None:
        raise ConfigurationError("候选证据封存要求 VC-2 AtomicAssertionLedger（EvidenceApproval 没有绑定分类事实）。")
    classification = _load_fact(store, classification_ref, "rule_classification_recorded", "VC-2 分类事实 ")["payload"]
    ledger_ref = dict(classification["atomic_assertion_ledger_ref"])
    ledger = _load_object(store, ledger_ref, "atomic_assertion_ledger", "AtomicAssertionLedger ")
    migration = _load_object(store, ledger["rule_migration_ledger_ref"], "rule_migration_ledger", "RuleMigrationLedger ")
    vc1_package = _load_object(store, migration["evidence_package_ref"], "evidence_package", "VC-1 EvidencePackage ")
    scenario_plan_ref = dict(approval["scenario_plan_ref"])
    scenario_plan = _load_object(store, scenario_plan_ref, "scenario_plan", "ScenarioPlan ")
    try:
        build = store.load_receipt(candidate_payload["candidate_build_receipt_ref"])
    except ControlError as error:
        raise ConfigurationError(f"CandidateBuildReceipt 无法读取：{error}") from error
    return {
        "revision": revision,
        "candidate_ref": dict(store.fact_ref(candidate)),
        "candidate_payload": candidate_payload,
        "approval_ref": dict(approval_ref),
        "scenario_plan_ref": scenario_plan_ref,
        "scenario_ids": [str(item["id"]) for item in scenario_plan["scenarios"]],
        "ledger_ref": ledger_ref,
        "ledger": ledger,
        "vc1_package": vc1_package,
        "validation_scope_sha256": str(build["validation_scope_sha256"]),
    }


def scenario_only_mapping(
    ledger: Mapping[str, Any],
    vc1_package: Mapping[str, Any],
    scenario_ids: Sequence[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """scenario-only 断言 → ``{owner_id, scenarios}``，以及闭合门禁的阻断原因（第二节的取法）。"""

    sources = {str(item["evidence_id"]): [str(value) for value in item["source_ids"]] for item in vc1_package["evidence_items"]}
    planned = set(scenario_ids)
    mapping: dict[str, dict[str, Any]] = {}
    reasons: set[str] = set()
    for assertion in ledger["assertions"]:
        if assertion["owner_kind"] != "scenario_only":
            continue
        assertion_id = str(assertion["assertion_id"])
        if assertion.get("result") not in {"pass", "passed"}:
            reasons.add("scenario_only_not_passed")
        scenarios = sorted({scenario for evidence_id in assertion["evidence_item_ids"] for scenario in sources.get(str(evidence_id), [])} & planned)
        if not scenarios:
            reasons.add("scenario_only_unmapped")
        mapping[assertion_id] = {"owner_id": str(assertion["owner_id"]), "scenarios": scenarios}
    return dict(sorted(mapping.items())), sorted(reasons)


def plan_candidate_seal_chain(campaign_dir: Path, manifest: Mapping[str, Any], request: Any) -> dict[str, Any]:
    """零写入规划：校验请求、读 Store 推出全部对象 payload 与事实骨架，并给出闭合门禁结论（``closure``）。

    事实引用要等写入后才知道，这里只给出不依赖它们的部分；``record_candidate_seal_chain`` 按同一规划逐步写入。
    """

    normalized = normalize_request(request)
    store, campaign = claude_upgrade_store.open_campaign_store(Path(campaign_dir), manifest)
    tool_files_sha256 = claude_upgrade_seal._require_store_tool_current(store, campaign)
    context = _candidate_context(store, campaign, manifest, normalized["candidate_id"])
    persona = dict(campaign["persona"])
    capture_attempt = normalized["capture_attempt_id"]
    attempt_id = validation_attempt_id(capture_attempt, normalized["validation_round"])
    scenario_ids = context["scenario_ids"]
    reasons: list[str] = []
    covered = sorted(normalized["scenario_evidence"])
    if covered != sorted(scenario_ids):
        missing = sorted(set(scenario_ids) - set(covered))
        extra = sorted(set(covered) - set(scenario_ids))
        raise ConfigurationError(
            f"候选场景证据没有精确覆盖 VC-3 ScenarioPlan：缺 {missing or '无'}，多 {extra or '无'}（Store 要求证据包唯一覆盖场景全集）。"
        )
    mapping, mapping_reasons = scenario_only_mapping(context["ledger"], context["vc1_package"], scenario_ids)
    reasons.extend(mapping_reasons)
    # 对象（内容寻址，ID 只带采集 attempt：门禁重试的后续轮次复用同一对象）。
    seal = normalized["seal"]
    objects: dict[str, tuple[str, dict[str, Any]]] = {}
    scenario_objects: dict[str, dict[str, str]] = {}
    review_payload = _manifest(
        persona,
        f"candidate-{capture_attempt}-seal-review",
        [
            {
                "id": "seal-approval",
                "facts": {
                    "result": "pass",
                    "generation": seal["generation"],
                    "review_sha256": seal["review_sha256"],
                    "reviewer": seal["reviewer"],
                },
            }
        ],
    )
    objects["seal-review"] = ("operational_evidence", review_payload)
    for scenario in scenario_ids:
        evidence_name, seal_name = f"scenario-{scenario}-evidence", f"scenario-{scenario}-seal"
        objects[evidence_name] = (
            "operational_evidence",
            _manifest(persona, f"candidate-{capture_attempt}-{scenario}-evidence", _binding_entries("artifact", normalized["scenario_evidence"][scenario])),
        )
        objects[seal_name] = (
            "operational_evidence",
            _manifest(
                persona,
                f"candidate-{capture_attempt}-{scenario}-seal",
                [
                    {"id": "capture-manifest", "facts": {"result": "pass", **seal["capture_manifest"]}},
                    {"id": "evidence-manifest", "facts": {"result": "pass", **seal["evidence_manifest"]}},
                ],
            ),
        )
        scenario_objects[scenario] = {"evidence": evidence_name, "seal": seal_name}
    for name in OPERATIONAL_EVIDENCE:
        objects[name] = (
            "operational_evidence",
            _manifest(persona, f"candidate-{capture_attempt}-{name.replace('_', '-')}", normalized["operational_evidence"][name]),
        )
    snapshot = normalized["selector_before_snapshot"]
    if not isinstance(snapshot, Mapping):
        raise ConfigurationError("selector_before_snapshot 必须是 runtime_catalog_snapshot 对象 payload。")
    objects["selector-before-snapshot"] = ("runtime_catalog_snapshot", dict(snapshot))
    refs = {name: _object_ref(kind, payload) for name, (kind, payload) in objects.items()}
    # 执行计划的外部门禁（与 B4-06a 定稿同一处定义）。
    gate = normalized["gate_execution"]
    external_gates = claude_upgrade_accept.candidate_external_gate_plan_entries(
        host_id=str(gate["host_id"]), architecture=str(gate["architecture"]), overrides=gate.get("overrides")
    )
    gate_execute = [item["gate_id"] for item in external_gates if item["source"] == "execute"]
    gate_reuse = [item["gate_id"] for item in external_gates if item["source"] == "reuse"]
    jobs = normalized["job_ids"]
    clash = sorted(set(jobs["execute"] + jobs["reuse"]) & {item["gate_id"] for item in external_gates})
    if clash:
        raise ConfigurationError(f"候选 Job ID 与外部门禁 ID 重名：{clash}。")
    run_conditions = {**normalized["run_conditions"], "tool_sha256": tool_files_sha256}
    run_conditions = {key: run_conditions[key] for key in ("run_nonce", "environment_sha256", "tool_sha256", "configuration_sha256", "network_sha256", "account_model_sha256")}
    candidate_payload = context["candidate_payload"]
    plan_skeleton = {
        "schema_version": VALIDATION_EXECUTION_PLAN_SCHEMA,
        "campaign_id": str(campaign["campaign_id"]),
        "persona": persona,
        "plan_id": f"{attempt_id}-plan",
        "candidate_id": normalized["candidate_id"],
        "candidate_ref": context["candidate_ref"],
        "candidate_build_receipt_ref": dict(candidate_payload["candidate_build_receipt_ref"]),
        "profile_approval_ref": context["approval_ref"],
        "scenario_plan_ref": context["scenario_plan_ref"],
        "atomic_assertion_ledger_ref": context["ledger_ref"],
        "validation_scope_sha256": context["validation_scope_sha256"],
        "execute_item_ids": sorted(jobs["execute"] + gate_execute),
        "reuse_item_ids": sorted(jobs["reuse"] + gate_reuse),
        "external_gates": external_gates,
        "run_conditions": run_conditions,
        "production_selector_before_ref": None,
        "vircs_state": dict(PRODUCTION_STATE),
    }
    existing = _existing_validation_attempt(store, campaign, attempt_id)
    return {
        "store": store,
        "campaign": campaign,
        "context": context,
        "request": normalized,
        "validation_attempt_id": attempt_id,
        "objects": objects,
        "object_refs": refs,
        "scenario_objects": scenario_objects,
        "scenario_only": mapping,
        "plan_skeleton": plan_skeleton,
        "existing_attempt": existing,
        "closure": {"status": "blocked" if reasons else "closed", "reasons": reasons},
    }


def _existing_validation_attempt(store: Any, campaign: Mapping[str, Any], attempt_id: str) -> dict[str, Any] | None:
    matches = [
        fact
        for fact in store.list_facts(str(campaign["campaign_id"]), "validation")
        if fact["fact_kind"] == "validation_attempt_created" and fact["payload"].get("attempt_id") == attempt_id
    ]
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# 三、写入链
# ---------------------------------------------------------------------------


def _logical_action(attempt_id: str, *parts: str) -> str:
    return ":".join((LOGICAL_ACTION_PREFIX, attempt_id, *parts))


def _validated_fact(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        validate_fact_payload(kind, dict(payload))
    except ControlError as error:
        raise ConfigurationError(f"{kind} 不符合 Store 合同：{error}") from error
    return dict(payload)


def record_candidate_seal_chain(campaign_dir: Path, manifest: Mapping[str, Any], request: Any) -> dict[str, Any]:
    """候选封存批准时的 Store 写入链（见模块说明第一节）：零写入规划 → 对象 → selector 前态 → 执行计划 → ValidationAttempt →
    场景四阶段 → CandidateEvidencePackage。调用方持有 Campaign 锁；重跑同一请求逐步复用、收敛到同一组事实。"""

    planned = plan_candidate_seal_chain(campaign_dir, manifest, request)
    if planned["closure"]["status"] != "closed":
        raise ConfigurationError(f"候选封存闭合门禁 blocked，不写 Store：{planned['closure']['reasons']}")
    store, campaign, context, normalized = planned["store"], planned["campaign"], planned["context"], planned["request"]
    attempt_id = planned["validation_attempt_id"]
    capture_attempt = normalized["capture_attempt_id"]
    issued_at = normalized["issued_at_utc"]
    session = claude_upgrade_seal._StoreSession(
        Path(store.root), str(campaign["campaign_id"]), Path(campaign_dir) / STORE_SCRATCH_RELATIVE
    )
    refs = planned["object_refs"]
    selector_payload = _validated_fact(
        "selector_observed",
        {"catalog_snapshot_ref": refs["selector-before-snapshot"], "observation_kind": SELECTOR_OBSERVATION_KIND},
    )
    # 预检：同一 attempt ID 已有 ValidationAttempt 时必须正是本请求会写出的那一条（否则一条不写）。
    existing = planned["existing_attempt"]
    if existing is not None:
        plan = _load_object(store, existing["payload"]["validation_execution_plan_ref"], "validation_execution_plan", "既有执行计划 ")
        expected = dict(planned["plan_skeleton"], production_selector_before_ref=plan["production_selector_before_ref"])
        if _with_identity(expected) != plan:
            raise ConfigurationError(
                f"ValidationAttempt {attempt_id} 已经存在且与本次封存的执行计划不同：同一采集 attempt 的同一轮只能封存一次。"
            )
    # 1. 对象。
    for name, (kind, payload) in planned["objects"].items():
        if session.seal_object(kind, payload) != refs[name]:
            raise ConfigurationError(f"Store 封存 {name} 得到的引用与规划不一致。")
    # 2. selector 前态（逻辑动作只带采集 attempt：门禁重试的后续轮次沿用同一事实）。
    selector = session.record_fact(
        "selector-observe",
        "selector_observed",
        selector_payload,
        issued_at=issued_at,
        logical_action=f"{LOGICAL_ACTION_PREFIX}:{capture_attempt}:selector-before",
    )
    # 3. 执行计划。
    plan_payload = _with_identity(dict(planned["plan_skeleton"], production_selector_before_ref=selector["fact_ref"]))
    plan_ref = session.seal_object("validation_execution_plan", plan_payload)
    # 4. ValidationAttempt。
    attempt_payload = _with_identity(
        {
            "schema_version": VALIDATION_ATTEMPT_SCHEMA,
            "attempt_id": attempt_id,
            "candidate_id": normalized["candidate_id"],
            "candidate_ref": context["candidate_ref"],
            "candidate_build_receipt_ref": dict(context["candidate_payload"]["candidate_build_receipt_ref"]),
            "validation_execution_plan_ref": plan_ref,
            "run_conditions": dict(plan_payload["run_conditions"]),
            "production_selector_before_ref": selector["fact_ref"],
            "vircs_state": dict(PRODUCTION_STATE),
            "result": "created",
        }
    )
    attempt = session.record_fact(
        "validation-attempt-create",
        "validation_attempt_created",
        _validated_fact("validation_attempt_created", attempt_payload),
        issued_at=issued_at,
        logical_action=_logical_action(attempt_id, "validation-attempt"),
    )
    # 5. 场景四阶段。
    seal = normalized["seal"]
    approvals: dict[str, dict[str, Any]] = {}
    scenario_facts: dict[str, dict[str, bool]] = {}
    for scenario in context["scenario_ids"]:
        artifacts = {
            "prepare": [],
            "capture": [refs[planned["scenario_objects"][scenario]["evidence"]]],
            "seal": [refs[planned["scenario_objects"][scenario]["seal"]]],
            "approve": [refs["seal-review"]],
        }
        previous: dict[str, Any] | None = None
        reused: dict[str, bool] = {}
        for stage, command, kind, result in SCENARIO_STAGES:
            payload: dict[str, Any] = {
                "schema_version": SCENARIO_STAGE_V2_SCHEMA,
                "candidate_id": normalized["candidate_id"],
                "scenario_id": scenario,
                "attempt_id": attempt_id,
                "validation_attempt_ref": attempt["fact_ref"],
                "stage": stage,
                "previous_stage_ref": previous,
                "artifact_refs": _sorted_refs(artifacts[stage]),
                "result": result,
            }
            if stage == "approve":
                payload |= {
                    "reviewer": seal["reviewer"],
                    "review_ref": f"{LOGICAL_ACTION_PREFIX}:{seal['generation']}:{seal['review_sha256']}",
                }
            written = session.record_fact(
                command,
                kind,
                _validated_fact(kind, payload),
                issued_at=issued_at,
                logical_action=_logical_action(attempt_id, "scenario", scenario, stage),
            )
            previous = written["fact_ref"]
            reused[stage] = bool(written["reused"])
        assert previous is not None
        approvals[scenario] = previous
        scenario_facts[scenario] = reused
    # 6. CandidateEvidencePackage。
    scenario_results = [
        {
            "assertion_id": assertion_id,
            "owner_id": item["owner_id"],
            "result": "pass",
            "source": "reuse",
            "scenario_approval_refs": _sorted_refs(approvals[scenario] for scenario in item["scenarios"]),
        }
        for assertion_id, item in planned["scenario_only"].items()
    ]
    inventory = normalized["raw_evidence_inventory"]
    package_payload = _with_identity(
        {
            "schema_version": CANDIDATE_EVIDENCE_PACKAGE_SCHEMA,
            "campaign_id": str(campaign["campaign_id"]),
            "persona": dict(campaign["persona"]),
            "target_version": str(campaign["target_version"]),
            "package_id": f"{attempt_id}-evidence",
            "candidate_id": normalized["candidate_id"],
            "attempt_id": attempt_id,
            "candidate_ref": context["candidate_ref"],
            "candidate_build_receipt_ref": dict(context["candidate_payload"]["candidate_build_receipt_ref"]),
            "validation_attempt_ref": attempt["fact_ref"],
            "atomic_assertion_ledger_ref": context["ledger_ref"],
            "scenario_approval_refs": _sorted_refs(approvals.values()),
            "scenario_assertion_results": scenario_results,
            "raw_evidence_inventory": inventory,
            **{field: refs[name] for name, field in OPERATIONAL_EVIDENCE.items()},
            "artifact_count": len(inventory),
            "inventory_sha256": canonical_sha256(inventory),
        }
    )
    package_ref = session.seal_object("candidate_evidence_package", package_payload)
    return {
        "candidate_id": normalized["candidate_id"],
        "capture_attempt_id": capture_attempt,
        "validation_round": normalized["validation_round"],
        "validation_attempt_id": attempt_id,
        "selector_before": selector,
        "selector_before_snapshot_ref": refs["selector-before-snapshot"],
        "validation_execution_plan_ref": plan_ref,
        "validation_attempt": attempt,
        "validation_attempt_ref": attempt["fact_ref"],
        "scenario_approval_refs": approvals,
        "scenario_facts_reused": scenario_facts,
        "scenario_assertion_results": scenario_results,
        "candidate_evidence_package_ref": package_ref,
        "operational_evidence_refs": {name: refs[name] for name in OPERATIONAL_EVIDENCE},
        "external_gates": [dict(item) for item in planned["plan_skeleton"]["external_gates"]],
        "closure": dict(planned["closure"]),
    }


# ---------------------------------------------------------------------------
# 四、证据侧：capture-candidate seal 的预览与批准（B4-02 第二步）
# ---------------------------------------------------------------------------
#
# 与官方 seal（``claude_upgrade_seal``）同一套四阶段：① 客户端检查点 → ② 证据视图与草稿 → ③ 唯一一次深度扫描与预览 →
# ④ 批准写 Store（第三节的写入链）与候选阶段结果。证据语义（逐 probe 复核、贴标签、闭合门禁、绑定）经编排器的 evidence
# 根转到 ``claude_upgrade_evidence`` 的候选路径；这里只做控制流程（锁、总计划、attempt 与 Store 读取、文件写入）。
#
# attempt 的两个接缝：``_load_capture_attempt``（phase=candidate）读 attempt，``_campaign_jobs``（phase=candidate，只传
# candidate_id）展开候选 Job，两者的候选分支都由 B4-01 b 段落实。attempt 里候选网关上下文的字段名（下面
# ``ATTEMPT_CANDIDATE_CONTEXT_FIELD`` 一组）与 ``restoration_checks`` 行形状、客户端证据目录名都是占位，b 段交回后对齐，
# 只改 ``_attempt_candidate_context``、``_verify_restoration_checks`` 与 ``CLIENT_EVIDENCE_DIRNAME`` 三处。

SEAL_DIRNAME = "seal"
CLIENT_CHECKPOINT_SCHEMA = "claude-upgrade-candidate-client-checkpoint/v1"
SEAL_DRAFT_SCHEMA = "claude-upgrade-candidate-seal-draft/v1"
SEAL_PREVIEW_SCHEMA = "claude-upgrade-candidate-seal-preview/v1"
SEAL_RESULT_SCHEMA = "claude-upgrade-candidate-seal-result/v1"
STAGE_RESULT_SCHEMA = "claude-upgrade-candidate-stage-result/v1"
STAGE_NAME = "capture-candidate"
CLIENT_CHECKPOINT_FILENAME = "client-checkpoint.json"
EVIDENCE_MANIFEST_FILENAME = "evidence-manifest.json"
EVALUATION_MANIFEST_FILENAME = "evaluation-manifest.json"
SEAL_DRAFT_FILENAME = "seal-draft.json"
SEAL_PREVIEW_FILENAME = "seal-preview.json"
DRAFTS_DIRNAME = "drafts"
STORE_CHECKPOINT = "candidate_sealed"
# 占位（B4-01 b 段对齐）：attempt 里的候选网关上下文、它的字段闭集与客户端证据目录（attempt 根下）。
ATTEMPT_CANDIDATE_CONTEXT_FIELD = "candidate_gateway"
ATTEMPT_CANDIDATE_CONTEXT_FIELDS = frozenset(
    {
        "base_url",
        "host",
        "run_conditions",
        "environment_before",
        "environment_after",
        "recovery_verification",
        "selector_before_snapshot",
    }
)
CLIENT_EVIDENCE_DIRNAME = "client-evidence"
# 评估用 manifest 的 assertion_context 字段（与 ``claude_upgrade_evaluation_recovery.candidate_failure_scope_inputs`` 同名）。
ASSERTION_CONTEXT_FIELD = "assertion_context"
EVALUATION_MANIFEST_FIELD = "evaluation_manifest"
STAGE_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "client",
        "campaign_id",
        "stage",
        "candidate_id",
        "status",
        "attempt",
        "seal",
        "store",
        "client_evidence",
        ASSERTION_CONTEXT_FIELD,
        "execute_item_ids",
        "reuse_item_ids",
        "live_request_count",
        "scanned_bytes",
        "sealed_at_utc",
    }
)


def add_seal_arguments(target: Any) -> None:
    """``capture-candidate seal`` 的参数（``--candidate-id`` 由编排器为候选级子命令统一注册）。"""

    target.add_argument("--attempt-id", help="seal：要封存的 awaiting_receipts 候选 attempt。")
    target.add_argument("--approve-seal-sha256", help="seal：批准预览输出的 review_sha256。")
    target.add_argument("--approved-by", help="seal 批准：批准人（记进每个场景的 scenario_approved）。")
    target.add_argument(
        "--client-evidence",
        action="append",
        default=[],
        metavar="CLIENT=PATH",
        help="seal：场景清单登记的客户端收据（claude-desktop／claude-vscode，Mac 侧驱动产出），文件必须在 attempt 的客户端证据目录内。",
    )


def seal_command(arguments: Any) -> tuple[dict[str, Any], int]:
    """``capture-candidate seal``：返回 (摘要, 退出码)。预览需要批准或闭合门禁 blocked 时退出码 2，封存完成为 0。"""

    orchestrator = _orchestrator()
    if getattr(arguments, "capture_action", "seal") != "seal":
        raise orchestrator._pending("capture-candidate run（候选采集 attempt）", "B4-01（候选采集 attempt，b 段）")
    candidate_id = _safe_id(getattr(arguments, "candidate_id", None), "--candidate-id")
    attempt_id = _safe_id(getattr(arguments, "attempt_id", None), "--attempt-id")
    approve = getattr(arguments, "approve_seal_sha256", None)
    approved_by = getattr(arguments, "approved_by", None)
    if approve is not None:
        _sha256(approve, "--approve-seal-sha256")
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise ConfigurationError("capture-candidate seal 批准必须给出 --approved-by。")
    elif approved_by is not None:
        raise ConfigurationError("--approved-by 只用于批准（与 --approve-seal-sha256 一起给出）。")
    campaign_dir = Path(arguments.campaign_dir)
    orchestrator._validate_existing_campaign_path(campaign_dir)
    # 与官方 seal 同一口径用解析后的真实路径（权限收口按路径字符串绑定内部根）。
    campaign_dir = campaign_dir.resolve(strict=True)
    orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        orchestrator._require_quiescent_campaign_lease(campaign_dir, action="seal", attempt_id=attempt_id)
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        return _seal_candidate_attempt(
            campaign_dir,
            manifest,
            candidate_id=candidate_id,
            attempt_id=attempt_id,
            client_evidence=list(getattr(arguments, "client_evidence", None) or []),
            approve=approve,
            approved_by=approved_by.strip() if isinstance(approved_by, str) else None,
        )


def _capture() -> Any:
    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture


def _attempt_candidate_context(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """attempt 的候选网关上下文（占位字段名，见本节开头）：网关 base URL、门禁执行主机、运行条件、环境前后与恢复核验
    条目、生产 selector 前态快照。"""

    value = attempt.get(ATTEMPT_CANDIDATE_CONTEXT_FIELD)
    if not isinstance(value, Mapping) or set(value) != ATTEMPT_CANDIDATE_CONTEXT_FIELDS:
        raise ConfigurationError(
            f"候选 attempt 缺少候选网关上下文 {ATTEMPT_CANDIDATE_CONTEXT_FIELD}（字段闭集 {sorted(ATTEMPT_CANDIDATE_CONTEXT_FIELDS)}）。"
        )
    base_url = value["base_url"]
    if not isinstance(base_url, str) or not base_url.startswith("http://") or base_url.endswith("/"):
        raise ConfigurationError("候选 attempt 的网关 base_url 必须是 http://<主机>:<端口>（不带结尾斜杠）。")
    host = value["host"]
    if not isinstance(host, Mapping) or set(host) != GATE_EXECUTION_FIELDS:
        raise ConfigurationError("候选 attempt 的门禁执行主机必须恰好是 host_id、architecture。")
    conditions = value["run_conditions"]
    if not isinstance(conditions, Mapping) or set(conditions) != set(RUN_CONDITION_FIELDS[1:]):
        raise ConfigurationError(f"候选 attempt 的运行条件必须恰好是 {'、'.join(RUN_CONDITION_FIELDS[1:])}。")
    snapshot = value["selector_before_snapshot"]
    if not isinstance(snapshot, Mapping):
        raise ConfigurationError("候选 attempt 的 selector 前态快照必须是 runtime_catalog_snapshot 对象 payload。")
    return {
        "base_url": base_url,
        "host": {key: host[key] for key in sorted(GATE_EXECUTION_FIELDS)},
        "run_conditions": {key: conditions[key] for key in RUN_CONDITION_FIELDS[1:]},
        "environment_before": _entries(value["environment_before"], "attempt 环境前态"),
        "environment_after": _entries(value["environment_after"], "attempt 环境后态"),
        "recovery_verification": _entries(value["recovery_verification"], "attempt 恢复核验"),
        "selector_before_snapshot": dict(snapshot),
    }


def _attempt_results(attempt: Mapping[str, Any], jobs: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    """attempt 结果与候选 Job 逐个对齐：全部完成、执行摘要一致、恰好一个证据根。"""

    orchestrator = _orchestrator()
    results = {str(item.get("id")): item for item in attempt.get("results") or [] if isinstance(item, Mapping)}
    if set(results) != {job.job_id for job in jobs}:
        raise ConfigurationError("attempt 的 Job 结果与 Campaign 冻结的候选 Job 不一致，禁止 seal。")
    for job in jobs:
        result = results[job.job_id]
        if result.get("status") != "complete" or result.get("execution_sha256") != orchestrator._job_execution_sha256(job):
            raise ConfigurationError(f"候选 Job {job.job_id} 未完成或执行摘要漂移，禁止 seal。")
        roots = result.get("evidence_roots")
        if not isinstance(roots, list) or len(roots) != 1:
            raise ConfigurationError(f"候选 Job {job.job_id} 必须恰好登记一个证据根。")
    return results


def _verify_restoration_checks(job: Any, result: Mapping[str, Any], layout: Any, job_root: Path) -> None:
    """run 时逐步核过的恢复报告（``restoration_checks``，行形状占位：``{probe_id, status, report{path, sha256}}``）在 seal
    时摘要不变，且正是宿主脚本在 Job 证据根下按 probe 写的那一份。"""

    probes = [str(step["probe_id"]) for step in job.steps]
    rows = result.get("restoration_checks") or []
    if [row.get("probe_id") for row in rows if isinstance(row, Mapping)] != probes or len(rows) != len(probes):
        raise ConfigurationError(f"候选 Job {job.job_id} 的恢复报告核对行与冻结步骤不一致。")
    for row in rows:
        report = row.get("report") if isinstance(row.get("report"), Mapping) else {}
        path = _capture()._host_path(str(report.get("path", "")), layout)
        expected = job_root / claude_upgrade_evidence.CANDIDATE_RESTORATION_DIRNAME / f"{row['probe_id']}.json"
        if (
            row.get("status") != "passed"
            or path != expected
            or path.is_symlink()
            or not path.is_file()
            or file_sha256(path) != report.get("sha256")
        ):
            raise ConfigurationError(f"候选 Job {job.job_id} 的 {row['probe_id']} 恢复报告缺失、未通过或在 run／seal 之间漂移。")


def _step_expectations(job: Any) -> dict[str, dict[str, Any]]:
    """候选步骤冻结的 run-id、候选账号与 API Key 的 ID、中继端口与触发方式（来自已展开步骤的 environment）。"""

    expectations: dict[str, dict[str, Any]] = {}
    for step in job.steps:
        environment = step.get("environment") if isinstance(step, Mapping) else None
        if not isinstance(environment, Mapping):
            raise ConfigurationError(f"候选 Job {job.job_id} 的步骤缺少 environment。")
        numbers: dict[str, int] = {}
        for key, name in (("ACCOUNT_ID", "account_id"), ("API_KEY_ID", "api_key_id"), ("RELAY_PORT", "relay_port")):
            value = str(environment.get(key, ""))
            if not value.isdecimal() or int(value) <= 0:
                raise ConfigurationError(f"候选 Job {job.job_id}／{step['probe_id']} 的 {key} 不是正整数（候选 Campaign 必须登记候选账号与 API Key）。")
            numbers[name] = int(value)
        expectations[str(step["probe_id"])] = {
            "run_id": str(environment.get("RUN_ID", "")),
            "trigger": str(environment.get("GATEWAY_TRIGGER", "")),
            **numbers,
        }
    return expectations


def _client_evidence(
    attempt_root: Path,
    scenarios: Mapping[str, Any],
    values: Sequence[str],
) -> dict[str, dict[str, Path]]:
    """``--client-evidence CLIENT=PATH`` 与 attempt 级边界核对收据（``<客户端证据目录>/<check>.json``，占位）的文件。"""

    directory = attempt_root / CLIENT_EVIDENCE_DIRNAME
    bindings: dict[str, Path] = {}
    for value in values:
        client_id, separator, raw = str(value).partition("=")
        if not separator or not SAFE_ID_RE.fullmatch(client_id) or client_id in bindings:
            raise ConfigurationError(f"--client-evidence 格式非法或重复：{value}")
        if client_id not in scenarios["required_client_bindings"]:
            raise ConfigurationError(f"--client-evidence 的 {client_id} 不在场景清单登记的客户端收据里。")
        path = Path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True).parent != directory.resolve(strict=False):
            raise ConfigurationError(f"客户端 {client_id} 的收据必须是 attempt 客户端证据目录内的普通文件：{raw}")
        bindings[client_id] = path.resolve(strict=True)
    checks: dict[str, Path] = {}
    for check in scenarios["required_boundary_checks"]:
        path = directory / f"{check}.json"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConfigurationError(f"边界核对收据不是可信普通文件：{path}")
        if path.is_file():
            checks[str(check)] = path
    return {"client_bindings": bindings, "boundary_checks": checks}


def _relative_binding(path: Path, base: Path) -> dict[str, Any]:
    return {"path": path.relative_to(base).as_posix(), "sha256": file_sha256(path), "bytes": path.stat().st_size}


def _stage_binding(path: Path, campaign_dir: Path) -> dict[str, str]:
    return {"path": path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(path)}


def _seal_candidate_attempt(
    campaign_dir: Path,
    manifest: dict[str, Any],
    *,
    candidate_id: str,
    attempt_id: str,
    client_evidence: Sequence[str],
    approve: str | None,
    approved_by: str | None,
) -> tuple[dict[str, Any], int]:
    orchestrator = _orchestrator()
    capture = _capture()
    orchestrator._reject_contaminated_campaign(campaign_dir)
    key = ("candidate", candidate_id, attempt_id)
    if key in orchestrator._conflict_quarantined_attempts(campaign_dir):
        raise ConfigurationError("该候选 attempt 已被证据根冲突隔离（证据不可信，永不复用、永不 seal）。")
    if key in capture._invalidated_attempts(campaign_dir):
        raise ConfigurationError("该候选 attempt 已被环境隔离或冲突隔离作废，永不 seal。")
    stage_path = orchestrator._stage_path(campaign_dir, STAGE_NAME, candidate_id, mode="write")[1]
    if stage_path.exists() or stage_path.is_symlink():
        sealed = orchestrator._read_json(stage_path, "候选阶段结果")
        binding = sealed.get("attempt") if isinstance(sealed.get("attempt"), Mapping) else {}
        if Path(str(binding.get("path", ""))).parent.name != attempt_id:
            raise ConfigurationError("候选证据已经由另一 attempt 封存，禁止覆盖。")
    attempt_root, attempt = orchestrator._load_capture_attempt(
        campaign_dir, "candidate", candidate_id, attempt_id, _verified_campaign_manifest=manifest
    )
    if attempt.get("status") != "awaiting_receipts":
        raise ConfigurationError("只有 awaiting_receipts 候选 attempt 可以 seal。")
    failed = orchestrator._failed_job_ids(attempt.get("results"))
    if failed:
        raise ConfigurationError("候选 attempt 仍有失败 Job，禁止 seal：" + "、".join(failed) + "；只能定向补跑失败闭集（B2-08）。")
    invalidated = orchestrator._attempt_evolution_invalidated_job_ids(
        campaign_dir, manifest, attempt_root, attempt, phase="candidate", candidate_id=candidate_id
    )
    if invalidated:
        raise ConfigurationError(
            f"候选 attempt {attempt_id} 有作业被工具演进作废（{'、'.join(invalidated)}）：先对账并按恢复预览只重跑失效作业，再对新 attempt 走 seal。"
        )
    jobs = orchestrator._campaign_jobs(campaign_dir, manifest, "candidate", candidate_id=candidate_id)
    results = _attempt_results(attempt, jobs)
    gateway = _attempt_candidate_context(attempt)
    current_tool = orchestrator._tool_identity(include_git=False)
    orchestrator._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action="capture-candidate seal")
    layout = capture._capture_layout(campaign_dir, manifest)
    data_root = Path(layout.host_data_root).resolve(strict=True)
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    tool_files_sha256 = claude_upgrade_seal._require_store_tool_current(store, store_campaign)
    context = _candidate_context(store, store_campaign, manifest, candidate_id)
    labels_path = claude_upgrade_seal._frozen_input(campaign_dir, manifest, "evidence_labels")
    scenarios_path = claude_upgrade_seal._frozen_input(campaign_dir, manifest, "target_scenarios")
    scenarios = claude_upgrade_plan.read_scenario_manifest(
        scenarios_path,
        expected_version=str(manifest["target_version"]),
        rules=tuple(manifest["required_rules"]),
        verify_source_spec=False,
    )
    client_files = _client_evidence(attempt_root, scenarios, client_evidence)
    attempt_path = attempt_root / "attempt.json"
    generation_key = {
        "candidate_id": candidate_id,
        "attempt": _relative_binding(attempt_path, data_root),
        "store_campaign_identity_sha256": store_campaign["identity_sha256"],
        "tool_files_sha256": tool_files_sha256,
        "evidence_semantics_sha256": current_tool["evidence_semantics_sha256"],
        "scenario_plan_ref": dict(context["scenario_plan_ref"]),
        "evidence_labels": _relative_binding(labels_path, data_root),
        "target_scenarios": _relative_binding(scenarios_path, data_root),
        "client_evidence": {
            group: {item: _relative_binding(path, data_root) for item, path in sorted(files.items())}
            for group, files in client_files.items()
        },
    }
    generation = claude_upgrade_seal._generation(generation_key)
    seal_dir = campaign_dir / "candidates" / candidate_id / SEAL_DIRNAME / generation
    if approve is not None:
        return _approve_candidate_seal(
            campaign_dir,
            manifest,
            seal_dir=seal_dir,
            generation=generation,
            approve=approve,
            approved_by=str(approved_by),
            jobs=jobs,
            attempt_path=attempt_path,
            stage_path=stage_path,
            candidate_id=candidate_id,
            data_root=data_root,
        ), 0
    return _preview_candidate_seal(
        campaign_dir,
        manifest,
        candidate_id=candidate_id,
        attempt=attempt,
        attempt_path=attempt_path,
        jobs=jobs,
        results=results,
        gateway=gateway,
        layout=layout,
        data_root=data_root,
        context=context,
        scenarios=scenarios,
        labels_path=labels_path,
        scenarios_path=scenarios_path,
        client_files=client_files,
        generation=generation,
        generation_key=generation_key,
        seal_dir=seal_dir,
    ), 2


def _preview_candidate_seal(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    candidate_id: str,
    attempt: Mapping[str, Any],
    attempt_path: Path,
    jobs: Sequence[Any],
    results: Mapping[str, Mapping[str, Any]],
    gateway: Mapping[str, Any],
    layout: Any,
    data_root: Path,
    context: Mapping[str, Any],
    scenarios: Mapping[str, Any],
    labels_path: Path,
    scenarios_path: Path,
    client_files: Mapping[str, Mapping[str, Path]],
    generation: str,
    generation_key: Mapping[str, Any],
    seal_dir: Path,
) -> dict[str, Any]:
    """①～③：客户端检查点、证据视图与草稿、唯一一次深度扫描、评估用 manifest 与预览；全部只写一次，同代重跑逐字复用。"""

    orchestrator = _orchestrator()
    capture = _capture()
    write_once = claude_upgrade_seal._write_once_or_reuse
    # ① 客户端检查点：候选取证由同一官方客户端驱动，run 与 seal 两次核验的二进制必须一致。
    binary = orchestrator._verify_official_binaries(manifest)
    if orchestrator._fingerprint(dict(binary)) != orchestrator._fingerprint(dict(attempt.get("binary_verification") or {})):
        raise ConfigurationError("官方客户端二进制在候选 run／seal 之间发生漂移。")
    checkpoint_path = seal_dir / CLIENT_CHECKPOINT_FILENAME
    write_once(
        checkpoint_path,
        {
            "schema_version": CLIENT_CHECKPOINT_SCHEMA,
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "attempt": generation_key["attempt"],
            "target_version": manifest["target_version"],
            "target_sha256": manifest["target_sha256"],
            "binary_verification_sha256": orchestrator._fingerprint(dict(binary)),
            "observed_at_utc": orchestrator._utc_now(),
        },
        ignore=("observed_at_utc",),
    )
    # ② 证据视图与草稿：relay 与恢复报告在 run／seal 之间不变，扫描只取候选 Job 证据根。
    view_jobs: list[dict[str, Any]] = []
    roots: list[Path] = []
    probe_dirs: list[Path] = []
    for job in jobs:
        result = results[job.job_id]
        root = capture._host_path(str(result["evidence_roots"][0]), layout)
        probe_dirs.extend(claude_upgrade_seal._verify_relay_checks(job, result, layout))
        _verify_restoration_checks(job, result, layout, root)
        roots.append(root)
        view_jobs.append(
            {
                "job_id": job.job_id,
                "evidence_root": root.relative_to(data_root).as_posix(),
                "probe_ids": [str(step["probe_id"]) for step in job.steps],
                "candidate_steps": _step_expectations(job),
            }
        )
    scanned = orchestrator.scan_evidence(roots, claude_upgrade_evidence.CANDIDATE_SIDE, base=data_root)
    drafts_dir = seal_dir / DRAFTS_DIRNAME
    draft_paths = {name: drafts_dir / filename for name, filename in claude_upgrade_evidence.CANDIDATE_DRAFT_FILES.items()}
    view = {
        "side": claude_upgrade_evidence.CANDIDATE_SIDE,
        "campaign_id": manifest["campaign_id"],
        "candidate_id": candidate_id,
        "target_version": manifest["target_version"],
        "target_sha256": manifest["target_sha256"],
        "runtime_image": layout.runtime_image,
        "attempt": {key: attempt[key] for key in ("attempt_id", "run_nonce", "started_at_utc", "completed_at_utc")},
        "jobs": view_jobs,
        "scenarios": scenarios,
        "labels_path": str(labels_path),
        "probes": scanned["probes"],
        "surfaces": scanned["surfaces"],
        "gateway_base_url": gateway["base_url"],
        "scenario_plan_ids": list(context["scenario_ids"]),
        "client_evidence": {
            group: {
                item: {"path": path.relative_to(data_root).as_posix(), "payload": orchestrator._read_json(path, f"{item} 收据")}
                for item, path in sorted(files.items())
            }
            for group, files in client_files.items()
        },
        "draft_paths": {name: path.relative_to(data_root).as_posix() for name, path in draft_paths.items()},
    }
    drafted = orchestrator._seal_capture_attempt(view)
    for name, path in draft_paths.items():
        write_once(path, drafted["drafts"][name])
    # ③ 唯一一次深度扫描：候选 Job 证据根、草稿、attempt 收据、冻结的标签与场景清单、客户端与边界核对收据。
    scan_roots = [
        *roots,
        drafts_dir,
        attempt_path,
        labels_path,
        scenarios_path,
        *[path for files in client_files.values() for path in files.values()],
    ]
    inventory = orchestrator._evidence_inventory(scan_roots, base=data_root)
    bound = orchestrator._seal_capture_attempt(view, inventory=inventory)
    if bound["drafts"] != drafted["drafts"]:
        raise ConfigurationError("同一证据视图两次生成的封存草稿不一致（证据在扫描期间被改动）。")
    try:
        counted = provenance.collect_root_provenance(probe_dirs)
    except (ValueError, RuntimeError, OSError) as error:
        raise ConfigurationError(f"候选真实请求核算失败（B1-11 口径）：{error}") from error
    if counted["unresolved_roots"]:
        raise ConfigurationError(f"候选真实请求核算有未决证据根：{counted['unresolved_roots']}")
    manifest_path = seal_dir / EVIDENCE_MANIFEST_FILENAME
    write_once(manifest_path, inventory)
    closure = {"status": bound["closure"]["status"], "reasons": list(bound["closure"]["reasons"])}
    evaluation_binding: dict[str, Any] | None = None
    if not bound["drafts"]["capture_manifest"]["label_mismatches"] and bound["drafts"]["capture_manifest"]["artifacts"]:
        # 评估用 manifest（B4-03 checker 的输入）：封存的 capture manifest 加深度扫描绑定合成，seal 写、compare 只读。
        index = {entry["path"]: entry for entry in inventory["entries"]}
        try:
            evaluation = claude_rule_assertion.bind_capture_manifest(
                bound["drafts"]["capture_manifest"], index, capture_id=str(attempt["attempt_id"])
            )
        except claude_rule_assertion.AssertionConfigurationError as error:
            raise ConfigurationError(f"候选评估用 manifest 合成失败：{error}") from error
        evaluation_path = seal_dir / EVALUATION_MANIFEST_FILENAME
        write_once(evaluation_path, evaluation)
        evaluation_binding = claude_upgrade_seal._binding(evaluation_path, campaign_dir)
    # Store 写入请求（批准人与审核摘要在批准时补）；闭合时零写入规划一次，Store 侧的阻断（scenario-only 断言）并入闭合门禁。
    seal_binding = bound["seal"]
    store_request = {
        "candidate_id": candidate_id,
        "capture_attempt_id": str(attempt["attempt_id"]),
        "validation_round": FIRST_ROUND,
        "issued_at_utc": str(attempt["completed_at_utc"]),
        "run_conditions": {"run_nonce": str(attempt["run_nonce"]), **gateway["run_conditions"]},
        "selector_before_snapshot": dict(gateway["selector_before_snapshot"]),
        "gate_execution": dict(gateway["host"]),
        "job_ids": {"execute": sorted(job.job_id for job in jobs), "reuse": []},
        "scenario_evidence": seal_binding["scenario_evidence"],
        "seal": {
            "generation": generation,
            "evidence_manifest": _relative_binding(manifest_path, data_root),
            "capture_manifest": seal_binding["capture_manifest"],
        },
        "raw_evidence_inventory": seal_binding["raw_evidence_inventory"],
        "operational_evidence": {
            "environment_before": list(gateway["environment_before"]),
            "environment_after": list(gateway["environment_after"]),
            "recovery": sorted(
                [*seal_binding["operational_evidence"]["recovery"], *gateway["recovery_verification"]], key=lambda item: item["id"]
            ),
            "secret_scan": list(seal_binding["operational_evidence"]["secret_scan"]),
        },
    }
    store_plan: dict[str, Any] | None = None
    if closure["status"] == "closed":
        provisional = {**store_request, "seal": {**store_request["seal"], "review_sha256": "0" * 64, "reviewer": "seal-preview"}}
        planned = plan_candidate_seal_chain(campaign_dir, manifest, provisional)
        closure["reasons"].extend(planned["closure"]["reasons"])
        closure["status"] = "blocked" if closure["reasons"] else "closed"
        store_plan = {
            "validation_attempt_id": planned["validation_attempt_id"],
            "existing_attempt": planned["existing_attempt"] is not None,
            "scenario_only": planned["scenario_only"],
            "external_gates": [dict(item) for item in planned["plan_skeleton"]["external_gates"]],
        }
    draft_path = seal_dir / SEAL_DRAFT_FILENAME
    write_once(
        draft_path,
        {
            "schema_version": SEAL_DRAFT_SCHEMA,
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "attempt_id": attempt["attempt_id"],
            "generation": generation,
            "generation_key": dict(generation_key),
            "drafts": {name: claude_upgrade_seal._binding(path, campaign_dir) for name, path in draft_paths.items()},
            "evaluation_manifest": evaluation_binding,
            "closure": closure,
            "metrics": bound["metrics"],
            "store_request": store_request,
            "store_plan": store_plan,
            "client_evidence": seal_binding["client_evidence"],
            "live_request_provenance": {
                key: counted[key]
                for key in ("schema_version", "counting_rule", "precise_total", "model_requests_by_endpoint", "auxiliary_by_category")
            },
        },
    )
    core = {
        "schema_version": SEAL_PREVIEW_SCHEMA,
        "campaign_id": manifest["campaign_id"],
        "candidate_id": candidate_id,
        "attempt_id": attempt["attempt_id"],
        "generation": generation,
        "client_checkpoint": claude_upgrade_seal._binding(checkpoint_path, campaign_dir),
        "evidence_manifest": claude_upgrade_seal._binding(manifest_path, campaign_dir),
        "evaluation_manifest": evaluation_binding,
        "seal_draft": claude_upgrade_seal._binding(draft_path, campaign_dir),
        "closure": closure,
        "metrics": bound["metrics"],
        "store_plan": store_plan,
        "live_request_count": int(counted["precise_total"]),
        "scanned_bytes": int(inventory["total_bytes"]),
    }
    preview = write_once(seal_dir / SEAL_PREVIEW_FILENAME, {**core, "review_sha256": orchestrator._fingerprint(core)})
    blocked = closure["status"] != "closed"
    return {
        "schema_version": SEAL_RESULT_SCHEMA,
        "status": "seal_blocked" if blocked else "awaiting_seal_approval",
        "campaign_id": manifest["campaign_id"],
        "candidate_id": candidate_id,
        "attempt_id": attempt["attempt_id"],
        "generation": generation,
        "review_sha256": preview["review_sha256"],
        "closure": closure,
        "metrics": bound["metrics"],
        "live_request_count": preview["live_request_count"],
        "scanned_bytes": preview["scanned_bytes"],
        "preview": str(seal_dir / SEAL_PREVIEW_FILENAME),
        "next_command": (
            f"闭合门禁 blocked：先处置阻断原因 {closure['reasons']}，再重新预览"
            if blocked
            else (
                f"capture-candidate seal --candidate-id {candidate_id} --attempt-id {attempt['attempt_id']} "
                f"--approve-seal-sha256 {preview['review_sha256']} --approved-by <批准人>"
            )
        ),
    }


def _approve_candidate_seal(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    seal_dir: Path,
    generation: str,
    approve: str,
    approved_by: str,
    jobs: Sequence[Any],
    attempt_path: Path,
    stage_path: Path,
    candidate_id: str,
    data_root: Path,
) -> dict[str, Any]:
    """④ 批准：核对冻结预览与草稿，经第三节写入链写 Store（到 candidate_sealed），再写候选阶段结果（只写一次）。

    批准不再扫描：证据边界由 ``_load_capture_attempt`` 的权限收口重放担保，草稿、深度扫描清单与评估用 manifest 按预览
    绑定的摘要核对。
    """

    orchestrator = _orchestrator()
    preview_path = seal_dir / SEAL_PREVIEW_FILENAME
    if preview_path.is_symlink() or not preview_path.is_file():
        raise ConfigurationError("当前输入（attempt、工具身份、ScenarioPlan 与客户端收据）没有冻结的候选 seal 预览：先不带 --approve-seal-sha256 运行预览。")
    preview = orchestrator._read_json(preview_path, "候选 seal 预览")
    core = {key: value for key, value in preview.items() if key != "review_sha256"}
    if preview.get("review_sha256") != orchestrator._fingerprint(core) or preview.get("generation") != generation:
        raise ConfigurationError("候选 seal 预览自摘要或代号不一致。")
    if approve != preview["review_sha256"]:
        raise ConfigurationError("批准摘要与当前冻结预览的 review_sha256 不一致（预览可能已因输入变化而换代）。")
    if preview["closure"].get("status") != "closed":
        raise ConfigurationError(f"闭合门禁 blocked，禁止批准：{preview['closure'].get('reasons')}")
    if stage_path.exists() or stage_path.is_symlink():
        # 阶段结果在 Store 写入链之后才写：已存在说明本次批准早已完成（Store 之后可能已被验收推进），逐字复用、不再写 Store。
        sealed = load_stage_result(campaign_dir, stage_path, candidate_id, manifest=manifest)
        if (sealed["seal"]["review_sha256"], sealed["seal"]["reviewer"]) != (preview["review_sha256"], approved_by):
            raise ConfigurationError("候选证据已按另一份预览或由另一批准人封存，禁止再批准。")
        return {
            "schema_version": SEAL_RESULT_SCHEMA,
            "status": "complete",
            "reused": True,
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "attempt_id": preview["attempt_id"],
            "generation": generation,
            "store": sealed["store"],
            "stage_result": str(stage_path),
            "live_request_count": sealed["live_request_count"],
            "scanned_bytes": sealed["scanned_bytes"],
        }
    verify = claude_upgrade_seal._verify_bound_file
    verify(campaign_dir, preview["client_checkpoint"], "客户端检查点")
    verify(campaign_dir, preview["evidence_manifest"], "深度扫描清单")
    if preview.get("evaluation_manifest") is None:
        raise ConfigurationError("候选 seal 预览没有评估用 manifest。")
    verify(campaign_dir, preview["evaluation_manifest"], "评估用 manifest")
    draft = orchestrator._read_json(verify(campaign_dir, preview["seal_draft"], "候选 seal 草稿"), "候选 seal 草稿")
    for name, binding in draft["drafts"].items():
        verify(campaign_dir, binding, f"封存草稿 {name}")
    execute = sorted(job.job_id for job in jobs)
    if execute != draft["store_request"]["job_ids"]["execute"]:
        raise ConfigurationError("Campaign 候选 Job 与 seal 草稿登记的执行项不一致。")
    attempt_binding = _stage_binding(attempt_path, campaign_dir)
    if attempt_binding["sha256"] != draft["generation_key"]["attempt"]["sha256"]:
        raise ConfigurationError("attempt 收据与 seal 草稿绑定的摘要不一致。")
    request = dict(draft["store_request"])
    request["seal"] = {**request["seal"], "review_sha256": preview["review_sha256"], "reviewer": approved_by}
    recorded = record_candidate_seal_chain(campaign_dir, manifest, request)
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    status = claude_upgrade_seal.store_cli(["status", "--store", Path(store.root), "--campaign", str(store_campaign["campaign_id"])])
    if status.get("checkpoint") != STORE_CHECKPOINT:
        raise ConfigurationError(f"Store 状态没有停在 {STORE_CHECKPOINT}：{status.get('checkpoint')}（候选证据包写入后应恰好到这里）。")
    store_summary = {
        "root": claude_upgrade_store.CAMPAIGN_STORE_DIRNAME,
        "campaign_id": str(store_campaign["campaign_id"]),
        "checkpoint": status["checkpoint"],
        "validation_attempt_id": recorded["validation_attempt_id"],
        "validation_attempt_ref": recorded["validation_attempt_ref"],
        "validation_execution_plan_ref": recorded["validation_execution_plan_ref"],
        "selector_before_ref": recorded["selector_before"]["fact_ref"],
        "candidate_evidence_package_ref": recorded["candidate_evidence_package_ref"],
        "scenario_approval_refs": {scenario: dict(ref) for scenario, ref in sorted(recorded["scenario_approval_refs"].items())},
    }
    client_bindings = {
        group: {item: _stage_binding(data_root / binding["path"], campaign_dir) for item, binding in sorted(bindings.items())}
        for group, bindings in draft["client_evidence"].items()
    }
    payload = {
        "schema_version": STAGE_RESULT_SCHEMA,
        "client": "claude",
        "campaign_id": manifest["campaign_id"],
        "stage": STAGE_NAME,
        "candidate_id": candidate_id,
        "status": "complete",
        "attempt": attempt_binding,
        "seal": {
            "generation": generation,
            "preview": claude_upgrade_seal._binding(preview_path, campaign_dir),
            "client_checkpoint": dict(preview["client_checkpoint"]),
            "evidence_manifest": dict(preview["evidence_manifest"]),
            "seal_draft": dict(preview["seal_draft"]),
            "review_sha256": preview["review_sha256"],
            "reviewer": approved_by,
        },
        "store": store_summary,
        "client_evidence": client_bindings,
        # 评估用 manifest 由 seal 合成并绑定（Campaign 相对路径＋摘要），compare 与失败定位
        # （``claude_upgrade_evaluation_recovery.candidate_failure_scope_inputs``）只读。
        ASSERTION_CONTEXT_FIELD: {
            EVALUATION_MANIFEST_FIELD: {key: preview["evaluation_manifest"][key] for key in ("path", "sha256")},
            "capture_manifest": {key: draft["drafts"]["capture_manifest"][key] for key in ("path", "sha256")},
        },
        "execute_item_ids": execute,
        "reuse_item_ids": [],
        "live_request_count": int(preview["live_request_count"]),
        "scanned_bytes": int(preview["scanned_bytes"]),
        "sealed_at_utc": orchestrator._utc_now(),
    }
    stage = claude_upgrade_seal._write_once_or_reuse(stage_path, payload, ignore=("sealed_at_utc",))
    return {
        "schema_version": SEAL_RESULT_SCHEMA,
        "status": "complete",
        "reused": False,
        "campaign_id": manifest["campaign_id"],
        "candidate_id": candidate_id,
        "attempt_id": preview["attempt_id"],
        "generation": generation,
        "store": store_summary,
        "stage_result": str(stage_path),
        "live_request_count": stage["live_request_count"],
        "scanned_bytes": stage["scanned_bytes"],
    }


# ---------------------------------------------------------------------------
# 五、候选阶段结果的读侧
# ---------------------------------------------------------------------------


def load_stage_result(campaign_dir: Path, path: Path, candidate_id: str, *, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取并核对候选阶段结果（``_load_stage_result("capture-candidate")`` 的 Claude 实现）。

    核对合同、字段闭集、Campaign、候选、阶段与完成状态，以及 attempt 收据与评估用 manifest 两个绑定的摘要。Store 写入在阶段
    结果之前完成（批准时先写 Store 再写阶段结果），所以阶段结果存在即 Store 已到 candidate_sealed；没有阶段结果时报
    「尚未封存」（共享流程按这几个字区分）。
    """

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError("候选阶段结果路径不可信。")
    if not path.exists():
        raise ConfigurationError(f"阶段尚未封存：{STAGE_NAME}（{path.relative_to(campaign_dir).as_posix()} 不存在）")
    stage = orchestrator._read_json(path, "候选阶段结果")
    campaign_id = (manifest or orchestrator._read_json(campaign_dir / "campaign.json", "Campaign 核心清单")).get("campaign_id")
    if (
        set(stage) != STAGE_RESULT_FIELDS
        or stage.get("schema_version") != STAGE_RESULT_SCHEMA
        or stage.get("client") != "claude"
        or stage.get("campaign_id") != campaign_id
        or stage.get("stage") != STAGE_NAME
        or stage.get("candidate_id") != candidate_id
        or stage.get("status") != "complete"
        or not orchestrator._is_rfc3339_timestamp(stage.get("sealed_at_utc"))
    ):
        raise ConfigurationError("候选阶段结果合同、Campaign、候选或状态不一致。")
    context = stage.get(ASSERTION_CONTEXT_FIELD)
    for label, binding in (
        ("候选阶段结果 attempt", stage.get("attempt")),
        ("候选评估用 manifest", context.get(EVALUATION_MANIFEST_FIELD) if isinstance(context, Mapping) else None),
    ):
        orchestrator._require_file_binding(binding, label)
        target = orchestrator._campaign_file(campaign_dir, str(binding["path"]))
        if target.is_symlink() or not target.is_file() or file_sha256(target) != binding["sha256"]:
            raise ConfigurationError(f"{label}缺失或摘要漂移。")
    return stage
