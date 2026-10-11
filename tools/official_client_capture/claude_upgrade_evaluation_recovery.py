"""Claude 评估失败局部恢复的 Claude 专属部分（Claude 升级控制面设计 B4-05a）：失败定位链、b0 派生索引与部署收据核对。

evaluation-recover 的 preview／apply／abandon、reevaluate、approval-revision、诊断制品、根因入账与二次判定、AUTHORIZATION 与
COMMIT 全部是共享流程（``upgrade_control_plane``）；B4-04 的 ``claude_upgrade_baseline.evaluation_recover`` 在外层负责 Store
两份登记。Claude 专属的只有下面三件：

一、失败定位链（服务面 ``_evaluation_failure_scope``，登记的薄壳分叉）
=================================================================
共享实现沿 Codex 的 assertion bundle provenance（``provenance.json``、派生收据、``results[].evidence_roots``）把失败证据定位
到 Job；Claude 没有这一层。Claude 的评估用 manifest（``claude-evaluation-capture-manifest/v1``，由
``claude_rule_assertion.bind_capture_manifest`` 合成）每个 artifact 直接带 ``job_id`` 与 ``probe_id``，伴随文件（上游响应
字节、``relay/relay.json``）沿用锚点的 ``job_id``；checker 失败 check 的 ``evidence_paths`` 就是这些 artifact 的 ``path``。
所以 Claude 的回溯链是：失败规则 → 失败 check → ``evidence_paths`` → 评估用 manifest 条目 → ``job_id`` → J*；R* 为引用了
J* 任一 artifact 的规则。每步失败关闭：路径不在 manifest 里、条目缺 ``job_id``、``job_id`` 不在原 attempt 的 Job 集合里，
都拒绝，不得退化为全量。输出形状与共享实现相同（``failed_rules``、``failed_checks``、``jobs``、``rules``、``official_refs``）。

**输入合同（候选封存 B4-02 按它对齐）**：``candidate_failure_scope_inputs``——

* 候选阶段结果 ``assertion_context.evaluation_manifest``：``{path, sha256}``，Campaign 相对路径，指向该候选 builder 使用的
  评估用 manifest，摘要必须一致；manifest 的每个 artifact 必须带 ``job_id``；
* 候选 attempt（``_capture_stage_attempt_context`` 返回的 attempt 文档）``results[].id``：原 attempt 的 Job 集合。

二、b0 派生评估索引（服务面 ``_derive_b0_evaluation_run``）
======================================================
Codex 的同名函数只用于历史 b0（旧 builder 首次失败即中断、没有 checkpoint）从单侧文档只读派生 ``evaluation-run.json``。Claude
的 builder（B4-03）批次结束必写 ``evaluation-run.json`` 与逐规则 checkpoint，没有这种历史形态：索引缺失说明批次没有跑到结束，
应同基线逐字重派该批次，这里明确拒绝。

三、修复部署收据核对（服务面 ``_verify_current_deployment_receipt``，登记的薄壳分叉）
==================================================================
evaluator-defect 的 apply 与 reevaluate 要求修复已受监督部署且正在被使用。共享实现按 Codex 部署收据的形状读顶层五摘要；
Claude 受管部署收据（``official-client-managed-deploy/v1``，B5a-01）的五摘要在 ``identity`` 子键里（适配器成员
``deploy_receipt_identity`` 同一口径）。判据与共享实现逐条相同：``status`` 为 passed；wire／策略摘要等于 Campaign 有效工具
身份（登记过演进时为最新演进的 to 身份）；五摘要等于当前受管工具树。

本文件在 Claude 身份策略里按 ``claude_upgrade_`` 前缀归 control 层；Codex 身份策略忽略它（``claude_`` 前缀）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_rule_assertion
from tools.official_client_capture import claude_upgrade_client_adapter
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256

ASSERTION_CONTEXT_FIELD = "assertion_context"
EVALUATION_MANIFEST_FIELD = "evaluation_manifest"
MACHINE_SIDES = ("candidate", "official")


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def candidate_failure_scope_inputs(campaign_dir: Path, candidate: Mapping[str, Any], attempt: Mapping[str, Any]) -> dict[str, Any]:
    """失败定位链的输入合同（见模块说明一）：返回 ``{artifact_jobs: {path: job_id}, attempt_jobs: [job_id…]}``。"""

    orchestrator = _orchestrator()
    if not isinstance(candidate, Mapping) or not isinstance(attempt, Mapping):
        raise ConfigurationError("失败定位需要候选阶段结果与候选 attempt 文档。")
    context = candidate.get(ASSERTION_CONTEXT_FIELD)
    binding = context.get(EVALUATION_MANIFEST_FIELD) if isinstance(context, Mapping) else None
    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
        raise ConfigurationError("候选阶段结果缺少 assertion_context.evaluation_manifest，无法定位失败证据。")
    path = orchestrator._campaign_file(Path(campaign_dir), str(binding["path"]))
    if path.is_symlink() or not path.is_file() or file_sha256(path) != binding.get("sha256"):
        raise ConfigurationError("候选评估用 manifest 缺失或摘要漂移，无法定位失败证据。")
    manifest = orchestrator._read_json(path, "候选评估用 manifest")
    try:
        validated = claude_rule_assertion.validate_evaluation_manifest(manifest, expected_claude_version=str(manifest.get("claude_version", "")))
        from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

        if validated["schema_version"] == recorded.EVALUATION_SCHEMA:
            from tools.official_client_capture import claude_upgrade_compare as compare

            claude_rule_assertion.verify_evaluation_sources(validated, compare.data_root(Path(campaign_dir)))
    except claude_rule_assertion.AssertionConfigurationError as error:
        raise ConfigurationError(f"候选评估用 manifest 无法校验：{error}") from error
    artifact_jobs: dict[str, str] = {}
    for artifact in validated["artifacts"]:
        job_id = artifact.get("job_id")
        if not isinstance(job_id, str) or not orchestrator.SAFE_ID_RE.fullmatch(job_id):
            raise ConfigurationError(f"评估用 manifest 条目 {artifact.get('path')} 缺少 job_id，定位链不完整。")
        previous = artifact_jobs.setdefault(str(artifact["path"]), job_id)
        if previous != job_id:
            raise ConfigurationError(f"评估用 manifest 条目 {artifact['path']} 同时归属多个 Job，定位链不唯一。")
    attempt_jobs = sorted(
        str(result["id"])
        for result in attempt.get("results", []) or []
        if isinstance(result, Mapping) and isinstance(result.get("id"), str)
    )
    if not attempt_jobs:
        raise ConfigurationError("候选 attempt 没有登记 Job（results[].id），无法定位失败证据。")
    return {"artifact_jobs": artifact_jobs, "attempt_jobs": attempt_jobs}


def evaluation_failure_scope(
    campaign_dir: Path,
    candidate: Mapping[str, Any],
    attempt: Mapping[str, Any],
    evaluation_run: Mapping[str, Any],
    assertions_root: Path,
) -> tuple[dict[str, Any], str]:
    """服务面 ``_evaluation_failure_scope`` 的 Claude 实现（见模块说明一）：返回 ``(failure_scope, failed_step)``。"""

    orchestrator = _orchestrator()
    if not isinstance(evaluation_run, Mapping) or not isinstance(evaluation_run.get("rules"), list):
        raise ConfigurationError("失败定位需要已校验的 evaluation-run.json。")
    inputs = candidate_failure_scope_inputs(Path(campaign_dir), candidate, attempt)
    artifact_jobs: dict[str, str] = inputs["artifact_jobs"]
    attempt_jobs = set(inputs["attempt_jobs"])
    root = Path(assertions_root) / "machine"
    failed_rules = sorted(str(row["rule"]) for row in evaluation_run["rules"] if row.get("status") == "fail")
    if not failed_rules:
        raise ConfigurationError("evaluation-run.json 没有 status=fail 的规则。")
    failed_checks: list[dict[str, Any]] = []
    jobs: set[str] = set()
    official_refs: set[str] = set()

    def document(side: str, rule: str) -> dict[str, Any] | None:
        path = root / side / f"{rule}.json"
        if path.is_symlink():
            raise ConfigurationError(f"{rule} 的{side}单规则文档不得是符号链接。")
        return orchestrator._read_json(path, f"{rule} {side} 单规则文档") if path.is_file() else None

    for rule in failed_rules:
        candidate_document = document("candidate", rule)
        if candidate_document is None:
            # 候选侧文档缺失（官方侧失败）：只列官方引用，不能定位候选 Job。
            official_document = document("official", rule)
            for check in (official_document or {}).get("checks", []):
                if isinstance(check, Mapping) and check.get("passed") is False:
                    official_refs.update(str(path) for path in check.get("evidence_paths", []))
            continue
        for check in candidate_document.get("checks", []):
            if not isinstance(check, Mapping) or check.get("passed") is not False:
                continue
            paths = [str(path) for path in check.get("evidence_paths", [])]
            check_jobs: set[str] = set()
            for path in paths:
                job_id = artifact_jobs.get(path)
                if job_id is None:
                    raise ConfigurationError(f"失败 check {rule}/{check.get('id')} 引用的证据 {path} 不在候选评估用 manifest 里。")
                if job_id not in attempt_jobs:
                    raise ConfigurationError(f"失败 check {rule}/{check.get('id')} 的证据归属 Job {job_id}，不在原 attempt 内。")
                check_jobs.add(job_id)
            failed_checks.append({"rule": rule, "check_id": str(check.get("id")), "evidence_paths": sorted(paths), "jobs": sorted(check_jobs)})
            jobs.update(check_jobs)
    affected_rules: set[str] = set(failed_rules)
    if jobs:
        for rule in (str(row["rule"]) for row in evaluation_run["rules"]):
            candidate_document = document("candidate", rule)
            for check in (candidate_document or {}).get("checks", []):
                if isinstance(check, Mapping) and any(artifact_jobs.get(str(path)) in jobs for path in check.get("evidence_paths", [])):
                    affected_rules.add(rule)
    scope = {
        "failed_rules": failed_rules,
        "failed_checks": failed_checks,
        "jobs": sorted(jobs),
        "rules": sorted(affected_rules),
        "official_refs": sorted(official_refs),
    }
    return scope, failed_rules[0]


def derive_b0_evaluation_run(*arguments: Any, **options: Any) -> dict[str, Any]:
    """服务面 ``_derive_b0_evaluation_run`` 的 Claude 实现：Claude 没有历史 b0 形态，一律拒绝（见模块说明二）。"""

    del arguments, options
    raise ConfigurationError(
        "b0 断言批次没有 evaluation-run.json：Claude 的 builder 批次结束必写该索引，缺失说明批次没有跑到结束；"
        "请先同基线逐字重派该断言批次，再 evaluation-recover。"
    )


def verify_current_deployment_receipt(campaign_dir: Path, manifest: Mapping[str, Any], receipt_path: Path) -> dict[str, Any]:
    """服务面 ``_verify_current_deployment_receipt`` 的 Claude 实现（见模块说明三）：返回部署收据原文。"""

    orchestrator = _orchestrator()
    if not isinstance(manifest, Mapping):
        raise ConfigurationError("Campaign 总计划必须是对象。")
    receipt = orchestrator._read_json(Path(receipt_path), "部署收据")
    if receipt.get("status") != "passed":
        raise ConfigurationError("部署收据 status 不是 passed。")
    identity = claude_upgrade_client_adapter.deploy_receipt_identity(receipt)
    if not identity:
        raise ConfigurationError("部署收据不是 Claude 受管部署收据（client 不是 claude 或缺少 identity）。")
    frozen = manifest.get("tool_identity") if isinstance(manifest.get("tool_identity"), Mapping) else {}
    # 基准是 Campaign 有效工具身份：登记过工具演进时为最新演进的 to 身份。
    if orchestrator._is_policy_v2_identity(frozen):
        frozen = orchestrator._campaign_effective_tool_identity(Path(campaign_dir), manifest)["identity"]
    for field in ("wire_producer_sha256", "policy_sha256"):
        frozen_value = frozen.get(field)
        if frozen_value is None or identity.get(field) != frozen_value:
            raise ConfigurationError(f"部署收据的 {field} 与 Campaign 有效工具身份不一致或缺失。")
    # 部署收据必须就是当前运行工具树的那一份：五摘要等于当前受管身份（修复已部署且正在被使用）。
    current = orchestrator._tool_identity(include_git=False)
    for field, current_field in (
        ("tool_files_sha256", "files_sha256"),
        ("policy_sha256", "policy_sha256"),
        ("wire_producer_sha256", "wire_producer_sha256"),
        ("evidence_semantics_sha256", "evidence_semantics_sha256"),
        ("control_sha256", "control_sha256"),
    ):
        if identity.get(field) != current.get(current_field):
            raise ConfigurationError(f"部署收据的 {field} 不等于当前受管工具树身份；修复尚未部署到当前树。")
    return dict(receipt)
