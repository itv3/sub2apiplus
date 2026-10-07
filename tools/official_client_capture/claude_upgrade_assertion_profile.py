"""Claude Campaign 的批准断言画像与六个分类读侧成员（Claude 升级控制面设计 B4-05a）。

一、批准断言画像（Campaign 级，VC-5 首次评估前冻结一次）
=====================================================
Codex 的断言画像（逐规则 check 的 selector／assertion）随 VC-2 批准包冻结，分类收据带 ``assertion_profile_manifest``。
Claude 的 VC-2 分类收据不带画像：画像由 ``claude_rule_assertion.build_expectations`` 从已批准的输入确定性生成——VC-2 批准的
AtomicAssertionLedger（Store 对象）、Campaign 冻结的目标场景清单、VC-2 批准的目标规则清单、VC-3 暂存 Release 的 profile／wire，
再加判据模板 ``claude_rule_expectations_templates.json``。判据模板是数据文件，不进 checker 摘要，如果每次现算，改模板就会悄悄
改变判定而绕开评估器授权链，所以画像在 Campaign 内冻结一次：

* ``freeze_approved_assertion_profile``：写 ``assertions/approved-assertion-profile.json``（画像本体）与
  ``assertions/approved-assertion-profile.binding.json``（绑定收据），两者都只写一次；重跑同一冻结逐字段复用（强杀后收敛）。
  绑定收据写明各输入的摘要：台账对象引用、场景清单、目标规则清单、Release profile／wire、VC-2 批准 review 摘要与 VC-3 联合
  摘要；审计项另记判据模板摘要与生成器 ``claude_rule_assertion.py`` 的摘要（即 checker_sha256）。由 VC-5 的 compare（B4-06b）
  首次调用；调用方持 Campaign 锁。
* ``load_approved_assertion_profile``：按绑定收据核对冻结文件摘要，并按 Campaign 当前状态复算台账、场景、规则、Release 与
  两项批准摘要，任一不符即失败关闭；模板与生成器摘要只用于审计，冻结之后不再比较。冻结之后要改 selector／assertion，只能
  走 ``evaluation-recover approval-revision`` 开基线（修订画像存在评估基线目录里）。
* ``classification_evaluation_fields``：服务面读取分类阶段结果（``_load_stage_result(classify)``）时附上的两项评估字段
  ``assertion_profile_manifest``（冻结画像）与 ``joint_manifest_sha256``（VC-3 联合摘要），与 Codex 分类收据同名同义；
  画像未冻结时不附。阶段结果文件本身不变，VC-2 checkpoint 绑定照旧。

二、六个分类读侧成员（服务面同名成员转调这里；Codex 的同名函数在评估读侧闭包内，不迁出，这里按 Claude 合同自实现）
==============================================================================================================
* ``approved_rules``：批准的目标规则（VC-2 批准副本 ``target-rules.json``，与分类阶段结果的 ``target_rules`` 逐项一致）；
  ``require_approved=False`` 时为 Campaign 冻结的基线规则；
* ``effective_assertion_profile``：评估基线 b<K> 生效的修订画像——沿 recovery 的 ``previous_baseline`` 找最近的 approval-revision
  基线；没有修订为 None（调用方退回分类视图里的冻结画像），判据与 Codex 相同；
* ``effective_classification_view``：按当前评估基线替换 ``assertion_profile_manifest``；画像未冻结即拒绝（VC-5 评估必须绑定
  冻结画像）；
* ``approval_revision_baselines``：已 COMMIT 的 approval-revision 基线编号；
* ``approval_revision_delta``：修订画像相对生效画像只能改规则与 check 的 description、check 的 select／assertion；验收契约
  （规则集、validation mode、check 全集、侧别覆盖）、``schema_version``、``claude_version``、``scenarios``、``generated_from``
  与其余顶层字段必须逐字相同，否则属批准包重做，须建后继 Campaign；
* ``classification_official_authority``：``candidate_profile`` 行的官方权威——冻结（或生效）画像摘要、VC-2 批准 review 摘要
  （对应 Codex 的 classification package digest）与 VC-3 联合摘要（对应 Codex 的批准联合摘要）。

本文件在 Claude 身份策略里按 ``claude_upgrade_`` 前缀归 control 层；Codex 身份策略忽略它（``claude_`` 前缀）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_acceptance_contract as acceptance_contract
from tools.official_client_capture import claude_rule_assertion
from tools.official_client_capture import claude_tool_identity_policy
from tools.official_client_capture import claude_upgrade_classify
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import claude_upgrade_profile
from tools.official_client_capture import claude_upgrade_release
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control.errors import ControlError

PROFILE_RELATIVE = Path("assertions") / "approved-assertion-profile.json"
BINDING_RELATIVE = Path("assertions") / "approved-assertion-profile.binding.json"
BINDING_SCHEMA = "claude-approved-assertion-profile-binding/v1"
TEMPLATES_PATH = Path(__file__).resolve().parent / "claude_rule_expectations_templates.json"
TEMPLATES_KEY = "official_client_capture/claude_rule_expectations_templates.json"
GENERATOR_KEY = "official_client_capture/claude_rule_assertion.py"
# 读取时与 Campaign 当前状态逐项复算比较的绑定字段（模板与生成器只审计，不比较）。
COMPARED_BINDING_FIELDS = ("campaign_id", "target_version", "inputs", "approvals")
# 修订画像相对生效画像允许变化的字段。
REVISABLE_RULE_FIELDS = frozenset({"description", "checks"})
REVISABLE_CHECK_FIELDS = frozenset({"select", "assertion", "description"})
FIXED_TOP_LEVEL_FIELDS = ("schema_version", "claude_version", "scenarios", "generated_from")


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _campaign_binding(campaign_dir: Path, path: Path) -> dict[str, str]:
    return {"path": path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(path)}


def _bound_file(campaign_dir: Path, binding: Any, label: str) -> Path:
    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
        raise ConfigurationError(f"{label}绑定形态非法。")
    path = _orchestrator()._campaign_file(Path(campaign_dir), str(binding["path"]))
    if path.is_symlink() or not path.is_file() or file_sha256(path) != binding.get("sha256"):
        raise ConfigurationError(f"{label}缺失或摘要漂移：{binding['path']}")
    return path


# ---------------------------------------------------------------------------
# 一、批准断言画像
# ---------------------------------------------------------------------------


def _profile_inputs(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Campaign 当前的画像输入：``(摘要与批准绑定, 载荷)``。各输入都按各自阶段的读侧核对后再取。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    classification = claude_upgrade_classify.load_stage_result(campaign_dir, manifest)
    stage = claude_upgrade_profile.load_stage_result(campaign_dir, manifest)
    ledger_ref = (classification.get("store") or {}).get("atomic_assertion_ledger_ref")
    if not isinstance(ledger_ref, Mapping):
        raise ConfigurationError("分类阶段结果缺少 AtomicAssertionLedger 引用。")
    store, _store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    try:
        ledger = dict(store.load_object(dict(ledger_ref))["payload"])
    except (ControlError, KeyError, TypeError, ValueError) as error:
        raise ConfigurationError(f"AtomicAssertionLedger 无法从 Store 读取：{error}") from error
    scenarios_path = claude_upgrade_seal._frozen_input(campaign_dir, manifest, "target_scenarios")
    rules_path = _bound_file(campaign_dir, (classification.get("approved") or {}).get("target_rules"), "VC-2 批准的目标规则清单")
    staged = claude_upgrade_release.load_staged_release(campaign_dir, stage.get("staged_release") or {})
    entry = staged["release_entry"]
    release_root = campaign_dir / claude_upgrade_release.STAGED_RELEASE_ROOT
    release_paths: dict[str, Path] = {}
    for key in ("profile", "wire"):
        reference = entry.get(key)
        if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str):
            raise ConfigurationError(f"暂存 Release 条目缺少 {key} 制品。")
        path = release_root / str(reference["path"])
        if path.is_symlink() or not path.is_file() or file_sha256(path) != reference.get("sha256"):
            raise ConfigurationError(f"暂存 Release 的 {key} 制品缺失或与 Release 条目摘要不一致。")
        release_paths[key] = path
    digests = {
        "inputs": {
            "atomic_assertion_ledger": {"ref": dict(ledger_ref)},
            "scenarios": _campaign_binding(campaign_dir, scenarios_path),
            "rule_manifest": _campaign_binding(campaign_dir, rules_path),
            "release_profile": _campaign_binding(campaign_dir, release_paths["profile"]),
            "release_wire": _campaign_binding(campaign_dir, release_paths["wire"]),
        },
        "approvals": {
            "classification_review_sha256": str(classification["review_sha256"]),
            "profile_joint_sha256": str(stage["joint_sha256"]),
        },
    }
    payloads = {
        "ledger": ledger,
        "scenarios": orchestrator._read_json(scenarios_path, "目标场景清单"),
        "rule_manifest": orchestrator._read_json(rules_path, "VC-2 批准的目标规则清单"),
        "release_profile": orchestrator._read_json(release_paths["profile"], "暂存 Release profile"),
        "release_wire": orchestrator._read_json(release_paths["wire"], "暂存 Release wire"),
    }
    return digests, payloads


def _audit_digests() -> dict[str, Any]:
    if TEMPLATES_PATH.is_symlink() or not TEMPLATES_PATH.is_file():
        raise ConfigurationError("判据模板库缺失或不可信。")
    try:
        checker = claude_tool_identity_policy.evaluator_dependency_digests()["checker_sha256"]
    except claude_tool_identity_policy.ToolIdentityPolicyError as error:
        raise ConfigurationError(f"evaluator 依赖摘要无法计算：{error}") from error
    return {
        "templates": {"path": TEMPLATES_KEY, "sha256": file_sha256(TEMPLATES_PATH)},
        "generator": {"path": GENERATOR_KEY, "checker_sha256": str(checker)},
    }


def freeze_approved_assertion_profile(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """冻结批准断言画像（write-once；重跑逐字段复用）；返回绑定收据。调用方持 Campaign 锁。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    binding_path = campaign_dir / BINDING_RELATIVE
    if binding_path.exists() or binding_path.is_symlink():
        # 已冻结：读取即按收据全量核对（冻结之后不再按模板重新生成）。
        load_approved_assertion_profile(campaign_dir, manifest)
        return orchestrator._read_json(binding_path, "批准断言画像绑定收据")
    digests, payloads = _profile_inputs(campaign_dir, manifest)
    try:
        document = claude_rule_assertion.build_expectations(
            claude_version=str(manifest["target_version"]),
            ledger=payloads["ledger"],
            scenarios_payload=payloads["scenarios"],
            templates=orchestrator._read_json(TEMPLATES_PATH, "判据模板库"),
            release_profile=payloads["release_profile"],
            release_wire=payloads["release_wire"],
            rule_manifest=payloads["rule_manifest"],
        )
    except claude_rule_assertion.AssertionConfigurationError as error:
        raise ConfigurationError(f"批准断言画像无法生成：{error}") from error
    profile_path = campaign_dir / PROFILE_RELATIVE
    claude_upgrade_seal._write_once_or_reuse(profile_path, document)
    binding = {
        "schema_version": BINDING_SCHEMA,
        "client": "claude",
        "campaign_id": str(manifest["campaign_id"]),
        "target_version": str(manifest["target_version"]),
        "profile": _campaign_binding(campaign_dir, profile_path),
        **digests,
        "audit": _audit_digests(),
        "frozen_at_utc": orchestrator._utc_now(),
    }
    return claude_upgrade_seal._write_once_or_reuse(binding_path, binding, ignore=("frozen_at_utc", "audit"))


def approved_assertion_profile_frozen(campaign_dir: Path) -> bool:
    path = Path(campaign_dir) / BINDING_RELATIVE
    return path.exists() or path.is_symlink()


def load_approved_assertion_profile(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """读取冻结的批准断言画像并按绑定收据核对（见模块说明一）；返回 ``{path, sha256, binding}``。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    binding_path = campaign_dir / BINDING_RELATIVE
    if not approved_assertion_profile_frozen(campaign_dir):
        raise ConfigurationError("批准断言画像尚未冻结：VC-5 首次评估前由 compare 冻结（freeze_approved_assertion_profile）。")
    if binding_path.is_symlink() or not binding_path.is_file():
        raise ConfigurationError("批准断言画像绑定收据不可信。")
    binding = orchestrator._read_json(binding_path, "批准断言画像绑定收据")
    if binding.get("schema_version") != BINDING_SCHEMA or binding.get("client") != "claude":
        raise ConfigurationError("批准断言画像绑定收据的合同名或客户端不符。")
    profile = binding.get("profile")
    if not isinstance(profile, Mapping) or profile.get("path") != PROFILE_RELATIVE.as_posix():
        raise ConfigurationError("批准断言画像绑定收据的画像路径不符。")
    _bound_file(campaign_dir, profile, "冻结的批准断言画像")
    digests, _payloads = _profile_inputs(campaign_dir, manifest)
    current = {
        "campaign_id": str(manifest["campaign_id"]),
        "target_version": str(manifest["target_version"]),
        **digests,
    }
    drift = [field for field in COMPARED_BINDING_FIELDS if binding.get(field) != current[field]]
    if drift:
        raise ConfigurationError(
            f"冻结批准断言画像之后输入已变化（{'、'.join(drift)}）：画像不能再按冻结时的输入重放，批准包变化须建后继 Campaign。"
        )
    return {"path": str(profile["path"]), "sha256": str(profile["sha256"]), "binding": binding}


def classification_evaluation_fields(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """服务面读分类阶段结果时附上的评估字段（与 Codex 分类收据同名同义）；画像未冻结时为空。"""

    if not approved_assertion_profile_frozen(campaign_dir):
        return {}
    loaded = load_approved_assertion_profile(campaign_dir, manifest)
    return {
        "assertion_profile_manifest": {"path": loaded["path"], "sha256": loaded["sha256"]},
        "joint_manifest_sha256": str(loaded["binding"]["approvals"]["profile_joint_sha256"]),
    }


# ---------------------------------------------------------------------------
# 二、六个分类读侧成员
# ---------------------------------------------------------------------------


def approved_rules(campaign_dir: Path, manifest: Mapping[str, Any], *, require_approved: bool) -> tuple[str, ...]:
    """服务面 ``_approved_rules``：批准的目标规则（按清单顺序）；``require_approved=False`` 时为冻结的基线规则。"""

    campaign_dir = Path(campaign_dir)
    if not require_approved:
        path = claude_upgrade_seal._frozen_input(campaign_dir, manifest, "baseline_rules")
        return claude_upgrade_plan.load_baseline_rules(path, str(manifest["baseline_version"]))
    classification = claude_upgrade_classify.load_stage_result(campaign_dir, manifest)
    if classification.get("status") != "complete":
        raise ConfigurationError("目标规则迁移尚未批准。")
    path = _bound_file(campaign_dir, (classification.get("approved") or {}).get("target_rules"), "VC-2 批准的目标规则清单")
    view = claude_upgrade_classify.rule_manifest_view(
        _orchestrator()._read_json(path, "VC-2 批准的目标规则清单"),
        expected_version=str(manifest["target_version"]),
        label="VC-2 批准的目标规则清单",
    )
    rules = tuple(str(rule) for rule in view["rules"])
    if list(rules) != list(classification.get("target_rules") or []):
        raise ConfigurationError("VC-2 批准副本的目标规则与分类阶段结果登记的不一致。")
    return rules


def approval_revision_baselines(campaign_dir: Path, candidate_id: str) -> list[int]:
    """服务面 ``_approval_revision_baselines``：候选内已 COMMIT 的 approval-revision 基线编号（升序）。"""

    orchestrator = _orchestrator()
    numbers: list[int] = []
    for number, flags in sorted(orchestrator._evaluation_baseline_states(Path(campaign_dir), candidate_id).items()):
        if not flags["commit"]:
            continue
        if orchestrator._load_evaluation_baseline_recovery(Path(campaign_dir), candidate_id, number)["kind"] == "approval-revision":
            numbers.append(number)
    return numbers


def effective_assertion_profile(campaign_dir: Path, candidate_id: str, baseline: int) -> dict[str, Any] | None:
    """服务面 ``_effective_assertion_profile``：基线 b<K> 生效的修订画像；没有修订为 None（判据与 Codex 相同）。"""

    orchestrator = _orchestrator()
    number = int(baseline)
    while number > 0:
        recovery = orchestrator._load_evaluation_baseline_recovery(Path(campaign_dir), candidate_id, number)
        if recovery["kind"] == "approval-revision":
            record_path = orchestrator._evaluation_baseline_dir(Path(campaign_dir), candidate_id, number) / orchestrator.APPROVAL_REVISION_FILENAME
            try:
                record = vc_artifacts.validate_approval_revision(orchestrator._read_json(record_path, "批准修订记录"))
            except vc_artifacts.VCArtifactError as error:
                raise ConfigurationError(f"评估基线 b{number} 的批准修订记录无法校验：{error}") from error
            if record["candidate_id"] != candidate_id or record["from_baseline"] != int(recovery["previous_baseline"]):
                raise ConfigurationError(f"评估基线 b{number} 的批准修订记录与 recovery 身份不一致。")
            return {
                "path": str(record["revised_profile"]["path"]),
                "sha256": str(record["revised_profile"]["sha256"]),
                "approval_revision": int(record["approval_revision"]),
                "evaluation_baseline": number,
            }
        number = int(recovery["previous_baseline"])
    return None


def effective_classification_view(campaign_dir: Path, candidate_id: str, classification: Mapping[str, Any]) -> dict[str, Any]:
    """服务面 ``_effective_classification_view``：按当前评估基线生效的画像投影分类视图（只替换 ``assertion_profile_manifest``）。"""

    orchestrator = _orchestrator()
    if not isinstance(classification.get("assertion_profile_manifest"), Mapping):
        raise ConfigurationError("分类视图没有冻结的批准断言画像：VC-5 评估之前必须先冻结（freeze_approved_assertion_profile）。")
    baseline, _commit = orchestrator._current_evaluation_baseline(Path(campaign_dir), candidate_id)
    effective = effective_assertion_profile(Path(campaign_dir), candidate_id, baseline)
    if effective is None:
        return dict(classification)
    path = orchestrator._campaign_file(Path(campaign_dir), effective["path"])
    if path.is_symlink() or not path.is_file() or file_sha256(path) != effective["sha256"]:
        raise ConfigurationError("批准修订画像在封存后漂移或丢失。")
    return {**dict(classification), "assertion_profile_manifest": {"path": effective["path"], "sha256": effective["sha256"]}}


def classification_official_authority(classification: Mapping[str, Any]) -> dict[str, str]:
    """服务面 ``_classification_official_authority``：画像摘要、VC-2 批准 review 摘要与 VC-3 联合摘要（键名与 Codex 相同）。"""

    orchestrator = _orchestrator()
    reference = classification.get("assertion_profile_manifest")
    if not isinstance(reference, Mapping) or not orchestrator.SHA256_RE.fullmatch(str(reference.get("sha256", ""))):
        raise ConfigurationError("分类视图缺少冻结的批准断言画像摘要。")
    review = str(classification.get("review_sha256") or "")
    joint = str(classification.get("joint_manifest_sha256") or "")
    if not orchestrator.SHA256_RE.fullmatch(review):
        raise ConfigurationError("分类阶段结果缺少 VC-2 批准 review 摘要。")
    if not orchestrator.SHA256_RE.fullmatch(joint):
        raise ConfigurationError("分类视图缺少 VC-3 联合摘要。")
    return {
        "assertion_profile_sha256": str(reference["sha256"]),
        "classification_package_digest": review,
        "review_sha256": joint,
    }


def approval_revision_delta(current_path: Path, revised_path: Path) -> dict[str, Any]:
    """服务面 ``_approval_revision_delta``：修订画像相对生效画像的变化只能落在 selector／assertion／description（见模块说明二）。"""

    try:
        current = acceptance_contract.load_profile(Path(current_path))
        revised = acceptance_contract.load_profile(Path(revised_path))
        current_contract = acceptance_contract.build_contract_payload(current)
        revised_contract = acceptance_contract.build_contract_payload(revised)
    except acceptance_contract.AcceptanceContractError as error:
        raise ConfigurationError(f"断言画像不可用：{error}") from error
    problems: list[str] = [field for field in FIXED_TOP_LEVEL_FIELDS if current.get(field) != revised.get(field)]
    if current_contract != revised_contract:
        problems.append("acceptance_contract（规则 id 集／validation mode／check 全集／侧别覆盖）")
    for key in sorted((set(current) | set(revised)) - {*FIXED_TOP_LEVEL_FIELDS, "rules"}):
        if current.get(key) != revised.get(key):
            problems.append(key)
    current_rules = {str(rule.get("rule_id")): rule for rule in current["rules"] if isinstance(rule, Mapping)}
    revised_rules = {str(rule.get("rule_id")): rule for rule in revised["rules"] if isinstance(rule, Mapping)}
    if set(current_rules) != set(revised_rules) or len(current_rules) != len(current["rules"]) or len(revised_rules) != len(revised["rules"]):
        problems.append("rules（规则 id 集合）")
    changed_rule_ids: list[str] = []
    changed_check_ids: list[str] = []
    for rule_id in sorted(set(current_rules) & set(revised_rules)):
        before, after = current_rules[rule_id], revised_rules[rule_id]
        if before == after:
            continue
        for key in sorted(set(before) | set(after)):
            if key not in REVISABLE_RULE_FIELDS and before.get(key) != after.get(key):
                problems.append(f"{rule_id}.{key}")
        checks_before = {str(check.get("id")): check for check in before.get("checks", []) if isinstance(check, Mapping)}
        checks_after = {str(check.get("id")): check for check in after.get("checks", []) if isinstance(check, Mapping)}
        if set(checks_before) != set(checks_after):
            problems.append(f"{rule_id}.checks（check id 集合）")
        for check_id in sorted(set(checks_before) & set(checks_after)):
            if checks_before[check_id] == checks_after[check_id]:
                continue
            for key in sorted(set(checks_before[check_id]) | set(checks_after[check_id])):
                if key not in REVISABLE_CHECK_FIELDS and checks_before[check_id].get(key) != checks_after[check_id].get(key):
                    problems.append(f"{rule_id}:{check_id}.{key}")
            changed_check_ids.append(f"{rule_id}:{check_id}")
        changed_rule_ids.append(rule_id)
    if problems:
        raise ConfigurationError(
            "approval-revision 拒绝：修订画像相对生效画像的变化不限于 selector／assertion："
            + "、".join(problems)
            + "；规则集／validation mode／场景／版本变化属批准包重做，须 close-campaign-ledger（显式停线）后从 VC-2 建后继 Campaign。"
        )
    if not changed_rule_ids:
        raise ConfigurationError("approval-revision 拒绝：修订画像与生效画像没有差异。")
    return {
        "changed_rule_ids": changed_rule_ids,
        "changed_check_ids": sorted(changed_check_ids),
        "contract_sha256": acceptance_contract.contract_sha256(revised_contract),
    }
