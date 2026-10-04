"""U-5 处置输入草稿（UM-9 第 4 项）。

v0.2.10 合并时 U-5 的三份输入由 gen_u5_inputs.py、gen_shared_contract.py 按 v0.2.4 先例生成，一度照抄
先例的 revision 002 被工具拒绝。``disposition-draft`` 从本 Plan 已封存的制品派生全部机械字段：

- 原业务回归收据：取验收收据中通过的 original_business 类门禁，绑定最新 SourceCandidate，签名后写入
  ``evidence/u5/original-business-receipt.json``；
- 共享合同后继收据草稿（U-3 判定共享控制合同受影响时）：逐个列出最新 ChangeDecision 的
  ``shared_control`` 路径及其在最新源码候选中的摘要，``purpose`` 由人工填写；每条的 ``change_kind`` 按路径预填
  （前端、台账、单测、依赖、数据访问、第三方协议等，见 ``CHANGE_KIND_RULES``，UM-22），单测、前端、台账等
  纯机械类同时预填评估，其余评估与没命中的类型留空由人工逐条填写，填好后 ``identity-seal`` 到
  ``evidence/u5/shared-contract-successor.json``；
- 处置输入：各客户端的 mode 按 U-3 判定，candidate／approval／acceptance 自动填本 Plan 制品，
  ``campaign_path`` 由人工用 ``--campaign client=路径`` 指定并按已登记的生产收据格式校验。

``--dry-run`` 只打印三份文档、不写文件，可对已完成的 Plan 做只读核对。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import (
    artifact_binding,
    bind_identity,
    expect_object,
    expect_safe_id,
    load_json,
    resolve_within,
)
from .contracts import CLIENT_KEYS, LoadedPlan, latest_stage_path
from .disposition_receipts import (
    SHARED_CONTRACT_FAIL_CLOSE,
    SHARED_CONTRACT_SUCCESSOR_SCHEMA,
    build_original_business_receipt,
    persona_baseline,
    shared_contract_verification,
    validate_campaign,
)
from .errors import UpstreamMergeError
from .plan_inputs import AWAITING_MANUAL_INPUT, plan_inputs_root, write_json_input
from .workflow import (
    CANDIDATE_DISPOSITION_INPUT_SCHEMA,
    _load_impact_receipt,
    _load_source_candidate,
    load_verification_receipt,
    shared_control_facts,
)

DISPOSITION_PURPOSES = ("validation_only", "production_replacement")
# 共享合同后继草稿的 change_kind 预填（UM-22）：按历次合并（v0.2.4、v0.2.10、v0.2.13）人工填写的口径，
# 只覆盖按路径就能判断的几类；有歧义的（如 http_upstream.go 既可能是传输参数也可能是第三方出站）留空给人工。
# 前一项命中即用，(前缀, 后缀, change_kind, 评估模板)；评估模板为空表示只预填类型、评估仍由人工写。
CHANGE_KIND_RULES: tuple[tuple[str, str, str, str], ...] = (
    ("frontend/", "", "ui_only", "前端界面或其测试，只在浏览器侧运行，不参与后端出站与官方 Persona 共享合同。"),
    ("docs/egress/maintenance/", ".json", "ledger_append", "本次合并追加的台账或承接收据，只追加记录，不改变运行时行为。"),
    ("", "_test.go", "test_only", "单元测试，只在测试中运行，不参与运行时出站。"),
    ("backend/go.mod", "", "dependency_bump", ""),
    ("backend/go.sum", "", "dependency_bump", ""),
    ("backend/internal/repository/http_upstream", "", "", ""),
    ("backend/internal/repository/", ".go", "data_access", ""),
    ("backend/internal/pkg/", ".go", "third_party_protocol_adapter", ""),
    ("docs/", ".md", "documentation", "文档改动，不参与运行时。"),
    ("deploy/", ".example.yaml", "config_example", "部署示例配置，不参与运行时出站。"),
    (".github/", "", "repository_support", ""),
)


def suggest_change_kind(path: str) -> tuple[str, str]:
    """按路径预填 change_kind 与评估模板；不命中任何一类时返回空串，由人工填写。"""

    for prefix, suffix, kind, assessment in CHANGE_KIND_RULES:
        if path.startswith(prefix) and path.endswith(suffix):
            return kind, assessment
    return "", ""
ORIGINAL_BUSINESS_NAME = "original-business-receipt.json"
SHARED_CONTRACT_NAME = "shared-contract-successor.json"
SHARED_CONTRACT_DRAFT_NAME = "shared-contract-successor-draft.json"
DISPOSITION_INPUT_NAME = "candidate-disposition-input.json"


def parse_campaigns(values: list[str] | None) -> dict[str, Path]:
    """解析重复的 ``--campaign client=绝对路径``。"""

    campaigns: dict[str, Path] = {}
    for raw in values or []:
        client, separator, value = raw.partition("=")
        if not separator or client not in CLIENT_KEYS:
            raise UpstreamMergeError(f"--campaign 格式应为 claude=路径 或 codex=路径：{raw}")
        if client in campaigns:
            raise UpstreamMergeError(f"--campaign 重复指定 {client}")
        path = Path(value)
        if not path.is_absolute():
            raise UpstreamMergeError(f"--campaign {client} 必须是绝对路径")
        campaigns[client] = path
    return campaigns


def draft_candidate_disposition(
    plan: LoadedPlan,
    attempt_id: str,
    campaigns: dict[str, Path],
    *,
    purpose: str = "validation_only",
    dry_run: bool = False,
) -> dict[str, Any]:
    """派生 U-5 三份输入；共享合同需要人工评估时返回 awaiting_manual_input。"""

    if purpose not in DISPOSITION_PURPOSES:
        raise UpstreamMergeError(f"--purpose 只能是 {DISPOSITION_PURPOSES}")
    attempt = expect_safe_id(attempt_id, "attempt_id")
    attempt_root = resolve_within(
        plan.evidence_root, f"{plan.output_relative('gate_attempts_root')}/{attempt}", "gate attempt"
    )
    receipt_path = attempt_root / "receipt.json"
    verification = load_verification_receipt(plan, receipt_path, require_passed=True)
    source = _load_source_candidate(plan)
    source_path = latest_stage_path(plan, "source_candidate")
    impact = _load_impact_receipt(plan)

    original = bind_identity(
        build_original_business_receipt(
            plan_id=plan.plan_id,
            plan_identity=plan.identity,
            verification=verification,
            source_binding=artifact_binding(plan.evidence_root, source_path),
        )
    )
    shared_required = bool(impact["shared_contract_required"])
    shared_draft: dict[str, Any] | None = None
    if shared_required:
        facts = shared_control_facts(plan)
        shared_draft = {
            "schema_version": SHARED_CONTRACT_SUCCESSOR_SCHEMA,
            "plan_id": plan.plan_id,
            "plan_identity_sha256": plan.identity,
            "source_tree": source["source_tree"],
            "source_commit": source["source_commit"],
            "scope": f"sub2api-{plan.document['upstream']['tag']}-upstream-merge",
            "purpose": "",
            "affected_paths": [
                {
                    "path": path,
                    "sha256": sha256,
                    "bytes": size,
                    "change_kind": suggest_change_kind(path)[0],
                    "assessment": suggest_change_kind(path)[1],
                }
                for path, (sha256, size) in sorted(facts.items())
            ],
            "fail_close_behavior": SHARED_CONTRACT_FAIL_CLOSE,
            "verification": shared_contract_verification(verification),
            "persona_baseline_unchanged": persona_baseline(
                plan.document["official_clients"], impact["official_client_identity_change_count"]
            ),
            "result": "closed",
        }

    u5_root = plan.output_path("candidate_disposition").parent
    original_path = u5_root / ORIGINAL_BUSINESS_NAME
    shared_path = u5_root / SHARED_CONTRACT_NAME
    clients: dict[str, dict[str, Any]] = {}
    for client in CLIENT_KEYS:
        if impact["successor_campaign_required"][client]:
            mode = "successor_campaign"
        elif impact["client_impacts"][client]:
            mode = "new_candidate"
        else:
            mode = "none"
        if mode == "none":
            if client in campaigns:
                raise UpstreamMergeError(f"{client} 未受影响（mode=none），不得指定 --campaign")
            clients[client] = {
                "mode": mode,
                "campaign_path": None,
                "candidate_path": None,
                "approval_path": None,
                "acceptance_path": None,
            }
            continue
        campaign = campaigns.get(client)
        if campaign is None:
            raise UpstreamMergeError(f"{client} 处置模式为 {mode}，必须用 --campaign {client}=路径 指定 Campaign")
        official = plan.document["official_clients"][client]
        validate_campaign(
            expect_object(load_json(campaign, f"{client} campaign"), f"{client} campaign"),
            client=client,
            mode=mode,
            persona=official["persona"],
            target_version=official["target_version"],
        )
        approval = attempt_root / "client-receipts" / f"{client}_active_wire.json"
        if approval.is_symlink() or not approval.is_file():
            raise UpstreamMergeError(f"验收 attempt 缺少 {client} active wire 客户端收据：{approval}")
        clients[client] = {
            "mode": mode,
            "campaign_path": str(campaign),
            "candidate_path": str(source_path),
            "approval_path": str(approval),
            "acceptance_path": str(receipt_path),
        }
    disposition_input = bind_identity(
        {
            "schema_version": CANDIDATE_DISPOSITION_INPUT_SCHEMA,
            "plan_id": plan.plan_id,
            "plan_identity_sha256": plan.identity,
            "source_tree": source["source_tree"],
            "purpose": purpose,
            "clients": clients,
            "shared_contract_receipt_path": str(shared_path) if shared_required else None,
            "original_business_receipt_path": str(original_path),
        }
    )
    if dry_run:
        return {
            "result": "dry_run",
            "original_business_receipt": original,
            "shared_contract_draft": shared_draft,
            "disposition_input": disposition_input,
        }

    inputs_root = plan_inputs_root(plan)
    write_json_input(original_path, original, "原业务回归收据")
    input_path = inputs_root / DISPOSITION_INPUT_NAME
    write_json_input(input_path, disposition_input, "CandidateDispositionInput")
    seal_command = (
        f"disposition-seal --input {input_path} --verification-receipt {receipt_path}"
    )
    if shared_draft is not None and not shared_path.exists():
        draft_path = inputs_root / SHARED_CONTRACT_DRAFT_NAME
        if not draft_path.exists():
            write_json_input(draft_path, shared_draft, "共享合同后继收据草稿")
        return {
            "result": AWAITING_MANUAL_INPUT,
            "stage": "U-5 共享合同",
            "original_business_receipt": str(original_path),
            "disposition_input": str(input_path),
            "shared_contract_draft": str(draft_path),
            "shared_contract_path_count": len(shared_draft["affected_paths"]),
            "next": (
                f"在草稿中填写 purpose 与每条 change_kind、assessment，identity-seal --input {draft_path} "
                f"--output {shared_path}，然后 {seal_command}"
            ),
        }
    return {
        "result": "drafted",
        "original_business_receipt": str(original_path),
        "disposition_input": str(input_path),
        "shared_contract_receipt": str(shared_path) if shared_required else None,
        "next": seal_command,
    }
