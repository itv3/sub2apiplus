"""Claude 候选证据封存（Claude 升级控制面设计 B4-02）：``capture-candidate seal`` 批准时的 Store 写入层。

本文件目前只有 Store 写入层（10-07 定：先做不依赖证据格式的部分，用 Store 夹具验收；证据侧——候选证据视图、capture
manifest、深度扫描与 EvidenceManifest、预览与批准入口、候选阶段结果——等路线一 B4-01 a 段的候选清单合同合入后再写，
届时 seal 批准步骤把证据侧算好的结果组装成本层的请求）。run 不碰 Store：VC-5 入口的三类 Store 事实都在这里一次写完。

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

from tools.official_client_capture import claude_upgrade_accept
from tools.official_client_capture import claude_upgrade_baseline
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture.capturelib.model import ConfigurationError
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
