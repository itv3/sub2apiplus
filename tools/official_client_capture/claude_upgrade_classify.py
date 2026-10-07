"""Claude VC-2 规则分类 producer（Claude 升级控制面设计 B3-02）：``claude_upgrade.py classify`` 的控制流程。

一、两次调用：草案与批准
========================
``classify`` 全程离线、零请求（``live_request_count = 0``），分草案与批准两次调用：

* **草案**（不带批准参数）：在 ``classification/draft/<rev>/`` 写一个只写一次的 revision（``rev-0001`` 起顺序编号），含
  六个文件与一份 revision 收据 ``draft.json``：

  - ``target-rules.json``：目标 RequiredRules 清单（与基线 ``claude_required_rules_<ver>.json`` 同一合同
    ``claude-code-required-rule-manifest/v1``，批准后即目标版本的 RequiredRules manifest）；
  - ``rule-migration.json``：RuleMigrationLedger 草案（逐规则迁移决定、证据等级、生命周期、兼容类别、证据项、适用条件
    与理由，加 ``affected_rules／inherited_rules``）；
  - ``atomic-assertion-ledger.json``：AtomicAssertionLedger 草案（逐断言唯一 owner、证据项、条件、P／R／J／M 通道与结果）；
  - ``discovery-disposition-ledger.json``：三个分区的终态处置——VC-1 DiscoveryInventory 的全部发现项、语义候选分母的
    全部 ``CAND-*`` 等候选（指南的 CandidateResolutionLedger 合在这里）、FW-F v5 的只记录事实（见第三节）；
  - ``scenarios.json``：目标场景清单（官方取证执行契约不得变化，``covers`` 随目标规则更新）；
  - ``profile-patches.json``：受影响规则的画像补丁计划（VC-3 由 B3-03 消费并复算 ``profile_diff_paths``）。

  两种草案：

  - **机器草案**（``--candidates``）：由输入确定性生成且全部未决——每条基线规则的迁移决定与证据等级都是 ``blocked``，
    每个发现项、候选与只记录事实的终态绑定都为空；不按旧结论、相同字符串或摘要相近预填 ``inherit``（指南 §4.2.1 第 2 步）。
    只预填不构成结论的线索：跨来源矩阵给出的规则证据项、这些证据项共有的适用条件，以及 VC-1 处置、语义候选与 SPEC
    关联（原样抄录为只读字段）。同一输入重跑返回同一 revision。
  - **修订**（``--from-revision`` + ``--revision-input``）：审核人把某个 revision 的六个文件复制出去逐项处置，再整目录交回；
    工具按字节原样写成新 revision，旧 revision 不覆盖（指南 §4.2.4「封存前修订必须写入新的 draft revision」），同内容
    重跑复用。父 revision 的输入已漂移时拒绝修订，须重新生成机器草案。

  每次草案调用都按当前工具对该 revision 复算清零门禁（第四节）并给出 ``review_sha256``（退出码 2：需要审核或批准）；
  门禁结果不写进 revision，工具演进后重跑草案调用即得到新口径下的结论与摘要。

* **批准**（``--approve-revision`` + ``--approve-review-sha256``）：核对 revision 文件未被改动、输入未漂移、门禁 closed、
  审核摘要与当前复算一致，然后经 ``official_client_control`` CLI 幂等写 Store：``artifact-seal --allow-existing`` 封存
  RuleMigrationLedger 与 AtomicAssertionLedger（由草案投影成 Store 合同，内容寻址），再 ``rule-classification-record``
  （带幂等键）把两者与 VC-1 的 EvidenceFact、EvidencePackage 绑定为 ``rule_classification_recorded``；Store 检查点必须
  仍是 ``evidence_recorded``（指南 §4.2.4：VC-2 不建画像检查点，不重做 ``evidence-record``、不替换 EvidencePackage）。
  随后写 ``classification/approved/``（被批准 revision 六个文件的逐字副本）、阶段结果 ``classification/result.json``
  （清零收据：分母、门禁计数、两个规则集合与 Store 引用）与 VC-2 checkpoint（``claude_upgrade_vc``；执行项为空、复用项为
  ``inherited_rules``、零请求、零扫描字节，与 Codex VC-2 同口径）。

二、输入与分母（一律由输入计算，不写死）
========================================
* VC-1：``control/vc/vc-1-checkpoint.json`` 逐级重放到 VC-0，它绑定的阶段结果 ``official/result.json`` 给出 Store 的
  EvidenceFact 与 EvidencePackage 引用；Store 检查点必须是 ``evidence_recorded``，有效工具身份必须等于当前受管代码。
  VC-1 封存对象一律经 Store 取：EvidencePackage v3 → 完整性清单（``completeness_ref``）→ 按用途找到 DiscoveryInventory、
  CaptureIndex、跨来源矩阵、目标 SinkInventory 与目标场景清单的绑定，按宿主数据根解析并逐个核摘要与字节数。采集路径与
  前序只读复用路径（完整性清单来自前序 Campaign）走同一条读法。
* 基线：总计划冻结了 ``inputs.baseline_rules`` 时用冻结副本，否则用受管工具根下的 ``claude_required_rules_<基线>.json``；
  规则编号必须与总计划 ``required_rules`` 逐条一致。
* 语义候选分母：``--candidates`` 给出的 Campaign 内文件（``claude-code-fw-e-complete-candidate-denominator/v1``，按组列出
  候选）。B2-07 的 VC-1 封存没有把语义候选纳入完整性清单，这里以显式输入承接，并把文件绑定写进每个 revision 的输入，
  修订与批准时重核。
* 分母：发现项数＝DiscoveryInventory 条目数；候选数＝分母文件各组候选 ID 之并（各组声明的计数与总数必须与实际一致）；
  原子断言数与规则数＝基线清单的原子断言归属与 RequiredRules（机器草案的起点）；只记录事实数＝封存证据里识别出的条目数。
  2.1.226 夹具的草案分母因此等于历史清零收据（7368／593／110／40），换版时随目标证据变化。

三、FW-F v5 的只记录事实
========================
v5 把 ``anthropic-usage-limit``、``anthropic-dispatch-id`` 两个灰度 Header 与 ``/api/oauth/usage`` 用量查询改成只记录、
不让取证失败，事实写在每个 probe 中继清单的 ``dimension_evidence.observed_only``。VC-2 这样承接：

* **识别**：逐个读取 EvidencePackage 每个证据项绑定的中继清单（``relay-manifest.json``，按 Store 封存的摘要核对），
  中继清单必须带 v5 维度证据；每条 ``observed_only`` 生成一条稳定 ID 的事实 ``OBSERVED-<probe>-<维度>``，原样保留
  是否可观测、是否出现、出现的取值、请求计数与条件，并附上以该维度命名的场景所覆盖的规则（``related_spec_ids``）。冻结的
  目标场景清单里以只记录维度命名的场景，其每个 probe 都必须产出对应维度的事实，缺一即失败关闭，不能静默跳过。
* **归类**：事实进入 ``discovery-disposition-ledger.json`` 的 ``observed_only_facts`` 分区，与发现项、候选同一套终态
  绑定，草案只给 ``suggested_bindings`` 建议、不代为绑定。门禁要求逐条绑定终态，并按指南 §2.1.2 第 7 条约束：未出现
  （合法零流量）只能是 ``supporting_fact_bound``（record_only 支撑事实）或 ``target_absent_proven``，不得 ``rule_bound``；
  不可观测的事实一律阻断（返回 VC-1 补证）；出现了的事实可以绑定到规则（需场景真实触发、实测分母完整）或支撑事实。
  事实对应的规则是否因此 ``change``／``condition_change``／``delete``，由审核人在迁移台账里逐条判定。

四、清零门禁（指南 §4.2.4）
==========================
发现项、候选、只记录事实：ID 集合与输入逐项相等、只读字段未被改写、每项至少一个合法终态绑定（终态与字段闭集见
``TERMINAL_BINDINGS``）、``rule_bound`` 只指向迁移台账内的规则、``duplicate_bound`` 指向同分区另一项且链无环、终点已解决；
发现项与语义候选的来源引用必须双向闭合（输入层问题，只能回 VC-1 修正）。迁移：基线规则全集固定，每条规则恰有一个
迁移决定（不得 ``blocked``）、``add／delete`` 与基线／目标成员关系一致、``delete`` 必须 ``superseded``、证据项都在只读
EvidencePackage 里、``affected_rules／inherited_rules`` 由决定复算、理由不得保留草案占位、``production_replacement``
用途下 ``observed``／``regressed_evidence`` 不得承担目标规则。原子断言：唯一 owner、每条目标规则至少一条画像断言、
scenario-only 组闭合、结果不得 ``blocked``、通道必须含 M 且含 R 或 P、条件与证据项非空；``inherit`` 规则的断言归属
必须与基线逐条相同。目标规则清单、迁移台账、断言台账、场景清单、补丁计划的 ID、计数与摘要互相一致；责任边界
（客户端本地职责、非 request_egress 兼容类别不得进入 RequiredRules）。全部通过后再按 Store 合同投影两份台账做一次合同
校验。任何一项不满足，批准即拒绝。

五、幂等与崩溃收敛
==================
* 运行在 Campaign 锁内，并要求 Campaign 静止（B2-08 的静止判据，与 seal 同一函数：停线租约视为静止，仍在执行或已过期的
  active 租约拒绝）；不取得 ``CampaignLease``，被强杀后不留需要对账的过期租约。revision 与批准目录都先写进同级临时目录、写完再整体改名发布，半成品只会是被下次调用清掉的临时目录。
* Store 写入经 ``claude_upgrade_seal._StoreSession``（进程内 CLI、对象 ``--allow-existing``、事实带
  ``fact_idempotency_key(逻辑动作, payload 摘要, 事实种类)``、父监督器上下文里带 ``--producer-run``）；事实时刻取被批准
  revision 的创建时刻（冻结值）。批准在任一步被强杀后，重跑同一批准命令逐步复用、事实数不增；已批准后再批准同一
  revision 逐字复用，批准另一 revision、或再生成草案一律拒绝（封存后变化须建立后继 Campaign，从 VC-2 继续）。

六、交给后续工作项的接口
========================
* B3-01（原子批次）：VC-2 的批准动作就是 ``claude_upgrade.py classify --campaign-dir <C> --approve-revision <rev>
  --approve-review-sha256 <sha>``（参数全是成对的旗标与值，离线零请求，可在 N+1 重派里逐字重跑并收敛）。阶段结果固定在
  ``classification/result.json``（``STAGE_RESULT_RELATIVE``，读侧 ``load_stage_result``），VC-2 checkpoint 由本模块经
  ``claude_upgrade_vc.complete_campaign_phase`` 写在 ``control/vc/vc-2-checkpoint.json``。批次身份、计时账本的阶段事件、
  ``_stage_path``／``_load_stage_result`` 的 classify 分支与 VC-2 阶段重派证明（``_campaign_stage_replay_facts``）归 B3-01。
* B3-03（画像）：从 ``load_stage_result`` 取被批准的目标规则、两个规则集合、``profile-patches.json`` 与 Store 引用；
  ``approved_rules`` 是服务面 ``_approved_rules`` 的现成实现，``_effective_classification_view`` 等仍归 B3-03。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层；它不在 wire 与
evidence 闭包里，改分类流程不要求重采，也不追加评估纪元。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_evidence
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256
from tools.official_client_control.contracts import (
    ATOMIC_ASSERTION_LEDGER_SCHEMA,
    COMPATIBILITY_CLASSES,
    EVIDENCE_LEVELS,
    MIGRATION_DECISIONS,
    RULE_LIFECYCLES,
    RULE_MIGRATION_LEDGER_SCHEMA,
    atomic_assertion_ledger_identity_sha256,
    rule_migration_ledger_identity_sha256,
)
from tools.official_client_control.errors import ControlError

# ---------------------------------------------------------------------------
# 坐标、合同名与词表
# ---------------------------------------------------------------------------

CLASSIFICATION_ROOT = Path("classification")
DRAFT_ROOT = CLASSIFICATION_ROOT / "draft"
APPROVED_ROOT = CLASSIFICATION_ROOT / "approved"
STAGE_RESULT_RELATIVE = CLASSIFICATION_ROOT / "result.json"
STORE_SCRATCH_RELATIVE = CLASSIFICATION_ROOT / "store-inputs"
DRAFT_RECEIPT_FILENAME = "draft.json"
REVISION_RE = re.compile(r"^rev-([0-9]{4})$")
# 发布前的临时目录前缀：只会出现在被强杀的调用之后，下次调用（Campaign 锁内）清掉。
DRAFT_TEMPORARY_PREFIX = ".tmp-rev-"
APPROVED_TEMPORARY_PREFIX = ".tmp-approved-"

# 草案六个文件：名字 → 文件名（设计 B3-02 固定）。
DRAFT_FILES: Mapping[str, str] = {
    "target_rules": "target-rules.json",
    "rule_migration": "rule-migration.json",
    "atomic_assertions": "atomic-assertion-ledger.json",
    "discovery_dispositions": "discovery-disposition-ledger.json",
    "scenarios": "scenarios.json",
    "profile_patches": "profile-patches.json",
}

COMMAND_RESULT_SCHEMA = "claude-upgrade-classify-result/v1"
DRAFT_RECEIPT_SCHEMA = "claude-upgrade-classification-draft/v1"
REVIEW_SCHEMA = "claude-upgrade-classification-review/v1"
STAGE_RESULT_SCHEMA = "claude-upgrade-classification-stage-result/v1"
RULE_MIGRATION_DRAFT_SCHEMA = "claude-upgrade-rule-migration-draft/v1"
ATOMIC_ASSERTION_DRAFT_SCHEMA = "claude-upgrade-atomic-assertion-draft/v1"
DISCOVERY_LEDGER_SCHEMA = "claude-upgrade-discovery-disposition-ledger/v1"
PROFILE_PATCHES_SCHEMA = "claude-upgrade-profile-patches/v1"
CANDIDATE_DENOMINATOR_SCHEMA = "claude-code-fw-e-complete-candidate-denominator/v1"
TARGET_RULES_SCHEMA = claude_upgrade_plan.BASELINE_RULE_SCHEMA
APPROVE_LOGICAL_ACTION = "claude-upgrade:classify:approve"
STORE_CHECKPOINT = claude_upgrade_seal.STORE_EVIDENCE_CHECKPOINT

MACHINE_DRAFT = "machine_draft"
REVIEW_INPUT = "review_input"
BLOCKED = "blocked"
EVIDENCE_PREFIX = "EVIDENCE-"
OBSERVED_PREFIX = "OBSERVED-"
RULE_PLACEHOLDER_RATIONALE = "草案占位：迁移决定与证据等级待按目标证据逐条判定（不得因旧结论、相同字符串或摘要相近填 inherit）。"
ITEM_PLACEHOLDER_RATIONALE = "草案占位：待按目标证据逐项绑定终态。"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = claude_upgrade_plan.SAFE_ID_RE
SPEC_ID_RE = claude_upgrade_plan.RULE_RE
CHANNEL_ORDER = ("P", "R", "J", "M")
CLIENT_LOCAL_RESPONSIBILITY = "client_local_scenario"
REVIEW_OUTCOMES = frozenset({"pass", "observed"})

# 终态绑定：类型 → (必填字段, 可选字段)，都不含 ``binding_type``（指南 §4.2.2 的六种终态）。
TERMINAL_BINDINGS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "rule_bound": (frozenset({"spec_ids", "evidence_role"}), frozenset()),
    "supporting_fact_bound": (frozenset({"fact_ids", "owner_kind", "evidence_role"}), frozenset({"owner_id", "managed_egress_ids"})),
    "managed_egress_bound": (frozenset({"managed_egress_ids", "disposition"}), frozenset()),
    "non_egress_proven": (frozenset({"proof_kind", "rationale", "evidence_paths"}), frozenset()),
    "target_absent_proven": (frozenset({"rationale", "evidence_paths"}), frozenset()),
    "duplicate_bound": (frozenset({"canonical_id"}), frozenset()),
}
# 支撑事实的归属：所属规则、画像事实、状态／条件／动态值事实，或只记录（合法零流量等）。
SUPPORTING_FACT_OWNERS = frozenset({"required_rule", "profile_fact", "condition_fact", "record_only"})
# 未出现的只记录事实（合法零流量）允许的终态：只记录支撑事实或目标缺失证明，不得生成规则。
OBSERVED_ABSENT_BINDINGS = frozenset({"supporting_fact_bound", "target_absent_proven"})

# VC-1 完整性清单里 VC-2 要读的封存对象（名字取 claude_upgrade_evidence.COMPLETENESS_PURPOSES）。
VC1_OBJECTS = ("discovery_inventory", "capture_index", "cross_source_matrix", "target_inventory", "target_scenarios")

RULE_MANIFEST_FIELDS = frozenset(
    {
        "schema_version", "target_version", "rule_standard", "atomic_assertion_ledger", "atomic_assertion_count",
        "profile_atomic_assertion_count", "scenario_only_assertion_count", "required_rule_count", "required_rules",
        "scenario_only_groups",
    }
)
REQUIRED_RULE_FIELDS = frozenset(
    {"spec_id", "title", "domain", "responsibility", "atomic_assertion_ids", "scope", "rule", "source", "measured", "implementation", "status"}
)
REQUIRED_RULE_TEXT_FIELDS = ("title", "domain", "responsibility", "scope", "rule", "source", "measured", "implementation", "status")
SCENARIO_GROUP_FIELDS = frozenset({"group_id", "title", "responsibility", "atomic_assertion_ids", "disposition"})

MIGRATION_DOC_FIELDS = frozenset(
    {"schema_version", "campaign_id", "baseline_version", "target_version", "baseline_spec_ids", "target_spec_ids", "rules", "affected_rules", "inherited_rules"}
)
STORE_RULE_FIELDS = ("spec_id", "migration_decision", "evidence_level", "rule_lifecycle", "compatibility_class", "evidence_item_ids", "applicability")
RULE_ROW_FIELDS = frozenset({*STORE_RULE_FIELDS, "rationale"})
ASSERTION_DOC_FIELDS = frozenset({"schema_version", "campaign_id", "target_version", "scenario_only_owner_ids", "assertions"})
STORE_ASSERTION_FIELDS = ("assertion_id", "owner_kind", "owner_id", "evidence_item_ids", "conditions", "channels", "result")
ASSERTION_ROW_FIELDS = frozenset({*STORE_ASSERTION_FIELDS, "rationale"})
LEDGER_DOC_FIELDS = frozenset({"schema_version", "campaign_id", "target_version", "sources", "discoveries", "candidates", "observed_only_facts"})
PATCH_DOC_FIELDS = frozenset({"schema_version", "campaign_id", "baseline_version", "target_version", "rule_patches"})
PATCH_ROW_FIELDS = frozenset({"spec_id", "migration_decision", "profile_paths", "rationale"})
DRAFT_RECEIPT_FIELDS = frozenset(
    {"schema_version", "campaign_id", "revision", "parent_revision", "origin", "inputs", "inputs_sha256", "files", "denominators", "created_at_utc"}
)

# 三个处置分区的只读字段（由输入生成，审核人只能改 ``bindings`` 与 ``rationale``）。
DISCOVERY_FIXED_FIELDS = ("discovery_id", "source_kind", "proposition", "vc1_disposition", "semantic_candidate_ids", "spec_ids")
CANDIDATE_FIXED_FIELDS = ("candidate_id", "candidate_group", "proposition", "source_ids")
OBSERVED_FIXED_FIELDS = (
    "fact_id", "dimension", "kind", "target", "probe_id", "evidence_id", "source", "evaluable", "observed", "observed_values",
    "observed_request_count", "conditions", "related_spec_ids", "suggested_bindings",
)
EDITABLE_ITEM_FIELDS = ("bindings", "rationale")
OBSERVED_ENTRY_FIELDS = frozenset(
    {"dimension", "kind", "target", "evaluable", "observed", "observed_values", "observed_request_count", "conditions"}
)


def _orchestrator() -> Any:
    """编排器模块（延迟导入，避免循环；测试对编排器属性的改写照样生效）。"""

    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _capture() -> Any:
    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture


def _observed_only_catalog() -> tuple[Mapping[str, Mapping[str, str]], str]:
    """FW-F v5 的只记录维度与维度证据合同名（与中继同一份实现，延迟导入）。"""

    from tools.official_client_capture.capturelib import claude_fw_f_v5

    return claude_fw_f_v5.OBSERVED_ONLY_DIMENSIONS, claude_fw_f_v5.SCHEMA_DIMENSION_EVIDENCE


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def add_classify_arguments(target: argparse.ArgumentParser) -> None:
    """``classify`` 的参数（``--campaign-dir`` 由编排器统一添加）。"""

    target.add_argument(
        "--candidates",
        type=Path,
        help=(
            f"机器草案：VC-1 语义候选分母（{CANDIDATE_DENOMINATOR_SCHEMA}，Campaign 目录内文件）；"
            "修订与批准沿用 revision 绑定的同一文件。"
        ),
    )
    target.add_argument("--from-revision", help="修订：父 revision 编号（rev-NNNN）。")
    target.add_argument("--revision-input", type=Path, help="修订：审核人处置后的六个草案文件所在目录（可另含 draft.json，忽略）。")
    target.add_argument("--approve-revision", help="批准：要批准的 revision 编号。")
    target.add_argument("--approve-review-sha256", help="批准：草案调用为该 revision 输出的 review_sha256。")


def _command_mode(arguments: argparse.Namespace) -> str:
    candidates = getattr(arguments, "candidates", None)
    parent = getattr(arguments, "from_revision", None)
    revision_input = getattr(arguments, "revision_input", None)
    approve = getattr(arguments, "approve_revision", None)
    approve_sha = getattr(arguments, "approve_review_sha256", None)
    if approve is not None or approve_sha is not None:
        if approve is None or approve_sha is None:
            raise ConfigurationError("批准必须同时给出 --approve-revision 与 --approve-review-sha256。")
        if candidates is not None or parent is not None or revision_input is not None:
            raise ConfigurationError("批准不接受 --candidates／--from-revision／--revision-input：输入一律取被批准 revision 的绑定。")
        return "approve"
    if parent is not None or revision_input is not None:
        if parent is None or revision_input is None:
            raise ConfigurationError("修订必须同时给出 --from-revision 与 --revision-input。")
        if candidates is not None:
            raise ConfigurationError("修订沿用父 revision 的语义候选分母绑定，不接受 --candidates。")
        return "revise"
    if candidates is None:
        raise ConfigurationError("机器草案必须提供 --candidates（VC-1 语义候选分母）。")
    return "draft"


def classify_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``classify``：返回 (摘要, 退出码)。草案与修订待审核或待批准，退出码 2；批准完成为 0。"""

    mode = _command_mode(arguments)
    orchestrator = _orchestrator()
    campaign_dir = Path(arguments.campaign_dir)
    orchestrator._validate_existing_campaign_path(campaign_dir)
    # 与 seal 同一口径用解析后的真实路径：完整性清单与证据绑定都按宿主数据根解析。
    campaign_dir = campaign_dir.resolve(strict=True)
    orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        # B2-08 定的静止判据（与 seal 同一函数）：停线租约（执行者已退出）视为静止，仍在执行或已过期的 active 租约拒绝。
        orchestrator._require_quiescent_campaign_lease(campaign_dir, action="classify")
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        if (campaign_dir / _capture().CONTAMINATION_MARKER).exists():
            raise ConfigurationError("Campaign 已记录环境污染：先走环境隔离与恢复（B2-08），禁止分类。")
        if mode == "approve":
            return _approve(campaign_dir, manifest, str(arguments.approve_revision), str(arguments.approve_review_sha256)), 0
        if mode == "revise":
            return _revise(campaign_dir, manifest, str(arguments.from_revision), Path(arguments.revision_input)), 2
        return _machine_draft(campaign_dir, manifest, Path(arguments.candidates)), 2


# ---------------------------------------------------------------------------
# 通用读写
# ---------------------------------------------------------------------------


def _read_json(path: Path, label: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label}缺失或不是可信普通文件：{path}")
    return _parse_json_object(path.read_bytes(), label)


def _parse_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法解析{label}：{error}") from error
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label}必须是 JSON 对象。")
    return payload


def _serialize(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_private_bytes(path: Path, data: bytes) -> None:
    """独占新建 0600 文件并落盘（目标已存在即失败，不跟随符号链接）。"""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _clear_temporaries(directory: Path, prefix: str) -> None:
    """清掉被强杀的调用留下的发布前临时目录（只认本模块的前缀，在 Campaign 锁内调用）。"""

    if not directory.is_dir():
        return
    for child in directory.iterdir():
        if child.name.startswith(prefix) and child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)


def _publish_directory(parent: Path, final_name: str, prefix: str, files: Mapping[str, bytes]) -> Path:
    """把一组文件写进同级临时目录，写完整体改名发布；目标已存在即拒绝（调用方在 Campaign 锁内）。"""

    final = parent / final_name
    if final.exists() or final.is_symlink():
        raise ConfigurationError(f"目标目录已经存在，禁止覆盖：{final}")
    temporary = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        os.chmod(temporary, 0o700)
        for filename, data in files.items():
            _write_private_bytes(temporary / filename, data)
        _fsync_directory(temporary)
        os.rename(temporary, final)
        _fsync_directory(parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return final


def _store_call(label: str, action: Callable[[], Any]) -> Any:
    try:
        return action()
    except ControlError as error:
        raise ConfigurationError(f"{label}失败：{error}") from error


def _sorted_unique_strings(value: Any, *, non_empty: bool = True, pattern: re.Pattern[str] | None = None) -> bool:
    return (
        isinstance(value, list)
        and (bool(value) or not non_empty)
        and all(isinstance(item, str) and item and item == item.strip() for item in value)
        and value == sorted(set(value))
        and (pattern is None or all(pattern.fullmatch(item) for item in value))
    )


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _relative_to(path: Path, base: Path) -> str | None:
    try:
        return Path(path).resolve(strict=True).relative_to(Path(base).resolve(strict=True)).as_posix()
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# RequiredRules 清单（基线与目标同一合同）
# ---------------------------------------------------------------------------


def rule_manifest_view(payload: Any, *, expected_version: str, label: str) -> dict[str, Any]:
    """校验 ``claude-code-required-rule-manifest/v1`` 并给出规则、断言归属与计数；结构不闭合即 ``ConfigurationError``。"""

    if not isinstance(payload, dict) or set(payload) != RULE_MANIFEST_FIELDS or payload.get("schema_version") != TARGET_RULES_SCHEMA:
        raise ConfigurationError(f"{label}字段不闭合或合同名不符。")
    if payload["target_version"] != expected_version:
        raise ConfigurationError(f"{label}版本 {payload['target_version']!r} 不是 {expected_version}。")
    if not _text(payload["rule_standard"]) or not _text(payload["atomic_assertion_ledger"]):
        raise ConfigurationError(f"{label} rule_standard 或 atomic_assertion_ledger 非法。")
    rules = payload["required_rules"]
    groups = payload["scenario_only_groups"]
    if not isinstance(rules, list) or not rules or not isinstance(groups, list):
        raise ConfigurationError(f"{label} required_rules 不能为空，scenario_only_groups 必须是数组。")
    owners: dict[str, tuple[str, str]] = {}
    rule_ids: list[str] = []
    rule_docs: dict[str, dict[str, Any]] = {}

    def claim(assertion_ids: Any, kind: str, owner: str) -> None:
        if not isinstance(assertion_ids, list) or not assertion_ids:
            raise ConfigurationError(f"{label} {owner} 的 atomic_assertion_ids 必须是非空数组。")
        for assertion_id in assertion_ids:
            if not isinstance(assertion_id, str) or not SAFE_ID_RE.fullmatch(assertion_id):
                raise ConfigurationError(f"{label} {owner} 含非法原子断言 ID：{assertion_id!r}")
            if assertion_id in owners:
                raise ConfigurationError(f"{label}原子断言 {assertion_id} 重复或被多个 owner 归属。")
            owners[assertion_id] = (kind, owner)

    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != REQUIRED_RULE_FIELDS:
            raise ConfigurationError(f"{label}的规则字段不闭合：{rule.get('spec_id') if isinstance(rule, dict) else rule!r}")
        spec_id = rule["spec_id"]
        if not isinstance(spec_id, str) or not SPEC_ID_RE.fullmatch(spec_id) or spec_id in rule_docs:
            raise ConfigurationError(f"{label}规则编号非法或重复：{spec_id!r}")
        if any(not _text(rule[field]) for field in REQUIRED_RULE_TEXT_FIELDS):
            raise ConfigurationError(f"{label} {spec_id} 的范围、规则、源码、实测、实现、状态等文字字段不能为空。")
        claim(rule["atomic_assertion_ids"], "required_rule", spec_id)
        rule_ids.append(spec_id)
        rule_docs[spec_id] = rule
    group_ids: list[str] = []
    for group in groups:
        if not isinstance(group, dict) or set(group) != SCENARIO_GROUP_FIELDS:
            raise ConfigurationError(f"{label}的 scenario-only 组字段不闭合。")
        group_id = group["group_id"]
        if not isinstance(group_id, str) or not SAFE_ID_RE.fullmatch(group_id) or group_id in group_ids or group_id in rule_docs:
            raise ConfigurationError(f"{label} scenario-only 组 ID 非法或重复：{group_id!r}")
        if any(not _text(group[field]) for field in ("title", "responsibility", "disposition")):
            raise ConfigurationError(f"{label} scenario-only 组 {group_id} 的文字字段不能为空。")
        claim(group["atomic_assertion_ids"], "scenario_only", group_id)
        group_ids.append(group_id)
    profile_count = sum(1 for kind, _owner in owners.values() if kind == "required_rule")
    expected_counts = {
        "required_rule_count": len(rule_ids),
        "atomic_assertion_count": len(owners),
        "profile_atomic_assertion_count": profile_count,
        "scenario_only_assertion_count": len(owners) - profile_count,
    }
    drift = {key: (payload[key], value) for key, value in expected_counts.items() if payload[key] != value}
    if drift:
        raise ConfigurationError(f"{label}的计数字段与内容不一致（声明, 实际）：{drift}")
    return {
        "rules": rule_ids,
        "rule_docs": rule_docs,
        "groups": group_ids,
        "owners": owners,
        "profile_count": profile_count,
        "scenario_count": len(owners) - profile_count,
    }


# ---------------------------------------------------------------------------
# 输入：VC-1 封存对象、基线、语义候选分母、只记录事实
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationInputs:
    """一次分类调用读到并核过的全部输入；``bindings`` 是进 ``inputs_sha256`` 的可序列化绑定。"""

    bindings: dict[str, Any]
    sha256: str
    campaign_id: str
    baseline_version: str
    target_version: str
    purpose: str
    baseline: dict[str, Any]
    baseline_payload: dict[str, Any]
    discoveries: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    observed: list[dict[str, Any]]
    rule_evidence: dict[str, list[str]]
    evidence_items: dict[str, dict[str, Any]]
    scenarios: dict[str, Any]
    persona: dict[str, Any]
    evidence_fact_ref: dict[str, Any]
    evidence_package_ref: dict[str, Any]
    store_root: Path
    store_campaign_id: str
    tool_files_sha256: str
    reference_problems: list[str]
    classification_fact: dict[str, Any] | None


def _baseline_manifest_path(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[Path, str]:
    """基线 RequiredRules 清单：总计划冻结了 ``inputs.baseline_rules`` 就用冻结副本，否则用受管工具根下的版本化清单。"""

    if (manifest.get("inputs") or {}).get("baseline_rules") is not None:
        path = claude_upgrade_seal._frozen_input(campaign_dir, manifest, "baseline_rules")
        return path, path.relative_to(campaign_dir).as_posix()
    path = claude_upgrade_plan.baseline_rule_manifest_path(str(manifest["baseline_version"]))
    shown = _relative_to(path, claude_upgrade_plan.REPOSITORY_ROOT)
    return path, shown if shown is not None else str(path)


def _read_bound_json(data_root: Path, binding: Any, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """按宿主数据根解析外部证据绑定，核摘要与字节数后读 JSON；返回 (内容, 绑定副本)。"""

    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256", "bytes"}:
        raise ConfigurationError(f"{label}的绑定字段不闭合。")
    relative = PurePosixPath(str(binding["path"]))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ConfigurationError(f"{label}的绑定路径不是数据根内相对路径：{binding['path']}")
    path = data_root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label}缺失或不是可信普通文件：{binding['path']}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(data_root.resolve(strict=True)):
        raise ConfigurationError(f"{label}越过宿主数据根：{binding['path']}")
    data = resolved.read_bytes()
    if _sha256(data) != binding["sha256"] or len(data) != binding["bytes"]:
        raise ConfigurationError(f"{label}的摘要或字节数与封存绑定不一致（VC-1 封存后漂移）：{binding['path']}")
    return _parse_json_object(data, label), {"path": str(binding["path"]), "sha256": str(binding["sha256"]), "bytes": int(binding["bytes"])}


def _official_stage(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """VC-1 checkpoint 重放（逐级到 VC-0）与它绑定的官方阶段结果；返回 (阶段结果, checkpoint 绑定, 阶段结果绑定)。"""

    plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
    checkpoint_path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, "VC-1")
    stage_path = campaign_dir / claude_upgrade_seal.STAGE_RESULT_RELATIVE
    receipt = Path(str(checkpoint["stage_receipt"]["path"]))
    receipt = receipt if receipt.is_absolute() else campaign_dir / receipt
    if receipt.resolve(strict=True) != stage_path.resolve(strict=True):
        raise ConfigurationError("VC-1 checkpoint 绑定的阶段结果不是 official/result.json。")
    stage = _read_json(stage_path, "官方阶段结果")
    if (
        stage.get("schema_version") != claude_upgrade_seal.STAGE_RESULT_SCHEMA
        or stage.get("client") != "claude"
        or stage.get("campaign_id") != manifest["campaign_id"]
        or stage.get("stage") != "capture-official"
        or stage.get("status") != "complete"
    ):
        raise ConfigurationError("官方阶段结果的合同、客户端、Campaign 或状态不符：VC-1 尚未完成封存。")
    store = stage.get("store")
    if (
        not isinstance(store, Mapping)
        or store.get("checkpoint") != STORE_CHECKPOINT
        or not isinstance(store.get("evidence_fact_ref"), Mapping)
        or not isinstance(store.get("evidence_package_ref"), Mapping)
    ):
        raise ConfigurationError("官方阶段结果缺少 evidence_recorded 的 Store 引用。")
    return (
        stage,
        {
            "path": checkpoint_path.relative_to(campaign_dir).as_posix(),
            "sha256": file_sha256(checkpoint_path),
            "checkpoint_sha256": str(checkpoint["checkpoint_sha256"]),
        },
        {"path": stage_path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(stage_path)},
    )


def _discovery_rows(inventory: Mapping[str, Any], target_version: str) -> list[dict[str, Any]]:
    """VC-1 DiscoveryInventory → 发现项分区的只读字段（按 ID 排序）；VC-1 未闭合（仍有 unclassified）即拒绝。"""

    if inventory.get("schema_version") != claude_upgrade_evidence.DISCOVERY_INVENTORY_SCHEMA or inventory.get("target_version") != target_version:
        raise ConfigurationError("VC-1 DiscoveryInventory 的合同名或目标版本不符。")
    items = inventory.get("items")
    if not isinstance(items, list) or inventory.get("item_count") != len(items):
        raise ConfigurationError("VC-1 DiscoveryInventory 的 items 与 item_count 不一致。")
    rows: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping) or set(item) != set(claude_upgrade_evidence.DISCOVERY_ITEM_FIELDS):
            raise ConfigurationError("VC-1 DiscoveryInventory 条目字段不闭合。")
        discovery_id = item["discovery_id"]
        if not isinstance(discovery_id, str) or not SAFE_ID_RE.fullmatch(discovery_id) or discovery_id in rows:
            raise ConfigurationError(f"VC-1 发现项 ID 非法或重复：{discovery_id!r}")
        for field in ("semantic_candidate_ids", "spec_ids"):
            if not isinstance(item[field], list) or any(not isinstance(value, str) for value in item[field]):
                raise ConfigurationError(f"VC-1 发现项 {discovery_id} 的 {field} 非法。")
        if item["disposition"] == claude_upgrade_evidence.UNCLASSIFIED:
            raise ConfigurationError(f"VC-1 DiscoveryInventory 仍有 unclassified 发现项（{discovery_id}）：VC-1 闭合门禁未通过，不能开始分类。")
        rows[discovery_id] = {
            "discovery_id": discovery_id,
            "source_kind": item["source_kind"],
            "proposition": item["proposition"],
            "vc1_disposition": item["disposition"],
            "semantic_candidate_ids": list(item["semantic_candidate_ids"]),
            "spec_ids": list(item["spec_ids"]),
        }
    return [rows[key] for key in sorted(rows)]


def _candidate_rows(path: Path, target_version: str) -> list[dict[str, Any]]:
    """语义候选分母 → 候选分区的只读字段（按 ID 排序）；各组计数与总数必须与实际候选一致。"""

    payload = _read_json(path, "语义候选分母")
    if payload.get("schema_version") != CANDIDATE_DENOMINATOR_SCHEMA or payload.get("target_version") != target_version:
        raise ConfigurationError(f"语义候选分母的合同名或目标版本不符（需要 {CANDIDATE_DENOMINATOR_SCHEMA}、{target_version}）。")
    groups = payload.get("candidate_groups")
    if not isinstance(groups, Mapping) or not groups:
        raise ConfigurationError("语义候选分母 candidate_groups 不能为空。")
    rows: dict[str, dict[str, Any]] = {}
    actual_counts: dict[str, int] = {}
    for group in sorted(groups):
        entries = groups[group]
        if not isinstance(group, str) or not SAFE_ID_RE.fullmatch(group) or not isinstance(entries, list):
            raise ConfigurationError(f"语义候选分母的组非法：{group!r}")
        actual_counts[group] = len(entries)
        for entry in entries:
            candidate_id = entry.get("candidate_id") if isinstance(entry, Mapping) else None
            if not isinstance(candidate_id, str) or not SAFE_ID_RE.fullmatch(candidate_id) or candidate_id in rows:
                raise ConfigurationError(f"语义候选 ID 非法或跨组重复：{candidate_id!r}")
            proposition = entry.get("proposition") if "proposition" in entry else entry.get("path")
            sources = entry.get("source_ids", [])
            if not isinstance(sources, list) or any(not isinstance(value, str) for value in sources):
                raise ConfigurationError(f"语义候选 {candidate_id} 的 source_ids 非法。")
            rows[candidate_id] = {
                "candidate_id": candidate_id,
                "candidate_group": group,
                "proposition": proposition if isinstance(proposition, str) else None,
                "source_ids": sorted(set(sources)),
            }
    declared = payload.get("counts")
    if declared is not None and dict(declared) != actual_counts:
        raise ConfigurationError(f"语义候选分母各组声明计数与实际不一致：声明 {declared}，实际 {actual_counts}")
    total = payload.get("total_orthogonal_candidates")
    if total is not None and total != len(rows):
        raise ConfigurationError(f"语义候选分母 total_orthogonal_candidates={total} 与实际候选数 {len(rows)} 不一致。")
    return [rows[key] for key in sorted(rows)]


def _reference_problems(discoveries: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    """发现项与语义候选的来源引用必须双向闭合（指南 §4.1.3）：两侧登记的（发现, 候选）对完全相同、且都指向存在的项。"""

    discovery_ids = {row["discovery_id"] for row in discoveries}
    candidate_ids = {row["candidate_id"] for row in candidates}
    forward = {(row["discovery_id"], candidate) for row in discoveries for candidate in row["semantic_candidate_ids"]}
    backward = {(source, row["candidate_id"]) for row in candidates for source in row["source_ids"]}
    problems = [f"发现项 {d} 引用了不存在的候选 {c}" for d, c in sorted(forward) if c not in candidate_ids]
    problems += [f"候选 {c} 的来源 {s} 不是发现项" for s, c in sorted(backward) if s not in discovery_ids]
    problems += [f"发现项 {d} → 候选 {c} 没有反向登记" for d, c in sorted(forward - backward) if c in candidate_ids]
    problems += [f"候选 {c} ← 发现项 {s} 没有正向登记" for s, c in sorted(backward - forward) if s in discovery_ids]
    return problems


def _suggested_bindings(fact_id: str, observed: bool, related: Sequence[str]) -> list[dict[str, Any]]:
    """只记录事实的处置建议（草案不代为绑定）：未出现 → 只记录支撑事实；出现 → 绑到场景覆盖的规则或待定条件事实。"""

    if not observed:
        return [
            {
                "binding_type": "supporting_fact_bound",
                "fact_ids": [fact_id],
                "owner_kind": "record_only",
                "evidence_role": "observed_only_absent_record_only",
            }
        ]
    if related:
        return [{"binding_type": "rule_bound", "spec_ids": list(related), "evidence_role": "observed_only_present"}]
    return [
        {
            "binding_type": "supporting_fact_bound",
            "fact_ids": [fact_id],
            "owner_kind": "condition_fact",
            "evidence_role": "observed_only_present_pending_rule_decision",
        }
    ]


def _observed_only_facts(
    items: Mapping[str, Mapping[str, Any]],
    data_root: Path,
    scenarios: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """识别 v5 只记录事实：逐个证据项读中继清单的 ``dimension_evidence.observed_only``；返回 (事实, 中继清单绑定)。"""

    dimensions, schema = _observed_only_catalog()
    covers: dict[str, set[str]] = {}
    dimension_covers: dict[str, list[str]] = {}
    expected: set[tuple[str, str]] = set()
    for scenario in scenarios["evidence_scenarios"]:
        for probe in scenario["probe_ids"]:
            covers.setdefault(probe, set()).update(scenario["covers"])
        if scenario["scenario_id"] in dimensions:
            expected.update((scenario["scenario_id"], probe) for probe in scenario["probe_ids"])
            dimension_covers[scenario["scenario_id"]] = sorted(set(scenario["covers"]))
    facts: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    for evidence_id in sorted(items):
        item = items[evidence_id]
        probe = evidence_id[len(EVIDENCE_PREFIX):] if evidence_id.startswith(EVIDENCE_PREFIX) else ""
        if not probe:
            raise ConfigurationError(f"EvidencePackage 证据项 ID 不是 {EVIDENCE_PREFIX}<probe>：{evidence_id}")
        refs = [
            ref
            for ref in item.get("evidence_refs") or []
            if isinstance(ref, Mapping) and PurePosixPath(str(ref.get("path", ""))).name == claude_upgrade_evidence.RELAY_MANIFEST_FILENAME
        ]
        if len(refs) != 1:
            raise ConfigurationError(f"证据项 {evidence_id} 必须恰好绑定一份中继清单（{claude_upgrade_evidence.RELAY_MANIFEST_FILENAME}）。")
        relay, binding = _read_bound_json(data_root, refs[0], f"证据项 {evidence_id} 的中继清单")
        if relay.get("probe_id") != probe:
            raise ConfigurationError(f"证据项 {evidence_id} 的中继清单 probe_id 不符。")
        dimension_evidence = relay.get("dimension_evidence")
        if (
            not isinstance(dimension_evidence, Mapping)
            or dimension_evidence.get("schema_version") != schema
            or not isinstance(dimension_evidence.get("observed_only"), list)
        ):
            raise ConfigurationError(
                f"证据项 {evidence_id} 的中继清单缺少 FW-F v5 维度证据（{schema}）：无法确认只记录事实是否齐全，不能静默忽略。"
            )
        sources.append({"evidence_id": evidence_id, **binding})
        for entry in dimension_evidence["observed_only"]:
            if not isinstance(entry, Mapping) or set(entry) != OBSERVED_ENTRY_FIELDS:
                raise ConfigurationError(f"证据项 {evidence_id} 的只记录事实字段不闭合。")
            dimension = entry["dimension"]
            spec = dimensions.get(dimension)
            if spec is None or entry["kind"] != spec["kind"] or entry["target"] != spec["target"]:
                raise ConfigurationError(f"证据项 {evidence_id} 登记了未知或错配的只记录维度：{dimension!r}")
            if (
                not isinstance(entry["evaluable"], bool)
                or not isinstance(entry["observed"], bool)
                or not isinstance(entry["observed_values"], list)
                or any(not isinstance(value, str) for value in entry["observed_values"])
                or isinstance(entry["observed_request_count"], bool)
                or not isinstance(entry["observed_request_count"], int)
                or entry["observed_request_count"] < 0
            ):
                raise ConfigurationError(f"证据项 {evidence_id} 的只记录事实 {dimension} 取值非法。")
            fact_id = f"{OBSERVED_PREFIX}{probe}-{dimension}"
            if not SAFE_ID_RE.fullmatch(fact_id) or fact_id in facts:
                raise ConfigurationError(f"只记录事实 ID 非法或重复：{fact_id}")
            # 相关规则取以该维度命名的场景所覆盖的规则（它才是这条事实对应的命题）；清单里没有该场景时退回 probe 全部场景之并。
            related = dimension_covers.get(dimension, sorted(covers.get(probe, set())))
            facts[fact_id] = {
                "fact_id": fact_id,
                "dimension": dimension,
                "kind": entry["kind"],
                "target": entry["target"],
                "probe_id": probe,
                "evidence_id": evidence_id,
                "source": dict(binding),
                "evaluable": entry["evaluable"],
                "observed": entry["observed"],
                "observed_values": list(entry["observed_values"]),
                "observed_request_count": entry["observed_request_count"],
                "conditions": entry["conditions"],
                "related_spec_ids": related,
                "suggested_bindings": _suggested_bindings(fact_id, entry["observed"], related),
            }
    probes_with_items = {evidence_id[len(EVIDENCE_PREFIX):] for evidence_id in items}
    without_items = sorted({probe for _dimension, probe in expected} - probes_with_items)
    if without_items:
        raise ConfigurationError(f"只记录维度的 probe 没有封存证据项：{without_items}")
    missing = sorted(expected - {(fact["dimension"], fact["probe_id"]) for fact in facts.values()})
    if missing:
        raise ConfigurationError(f"v5 只记录事实缺失（目标场景清单要求这些 probe 记录对应维度，不能静默跳过）：{missing}")
    return [facts[key] for key in sorted(facts)], sources


def _candidate_binding(campaign_dir: Path, path: Path) -> dict[str, Any]:
    relative = _relative_to(path, campaign_dir)
    if relative is None or path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"语义候选分母必须是 Campaign 目录内的可信普通文件：{path}")
    return {"path": relative, "sha256": file_sha256(path), "bytes": path.stat().st_size}


def collect_inputs(campaign_dir: Path, manifest: Mapping[str, Any], candidates_path: Path) -> ClassificationInputs:
    """读取并核对 VC-2 的全部输入（指南 §4.2.1 第 1 步）：任一输入漂移或 VC-1 未闭合都不得开始分类。"""

    orchestrator = _orchestrator()
    campaign_id = str(manifest["campaign_id"])
    baseline_version = str(manifest["baseline_version"])
    target_version = str(manifest["target_version"])
    stage, checkpoint_binding, stage_binding = _official_stage(campaign_dir, manifest)
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    tool_files_sha256 = claude_upgrade_seal._require_store_tool_current(store, store_campaign)
    status = _store_call("读取 Store 状态", lambda: claude_upgrade_seal.store_cli(["status", "--store", store.root, "--campaign", campaign_id]))
    if status.get("checkpoint") != STORE_CHECKPOINT:
        raise ConfigurationError(f"Store 检查点不是 {STORE_CHECKPOINT}（{status.get('checkpoint')}）：VC-2 只能接在 VC-1 封存之后。")
    evidence_fact_ref = dict(stage["store"]["evidence_fact_ref"])
    evidence_package_ref = dict(stage["store"]["evidence_package_ref"])
    evidence_fact = _store_call("读取 EvidenceFact", lambda: store.load_fact(evidence_fact_ref))
    if evidence_fact.get("fact_kind") != "evidence_recorded" or evidence_fact["payload"].get("evidence_package_ref") != evidence_package_ref:
        raise ConfigurationError("官方阶段结果的 EvidenceFact 与 EvidencePackage 引用不一致。")
    package = _store_call("读取 EvidencePackage", lambda: store.load_object(evidence_package_ref))["payload"]
    if package.get("schema_version") != claude_upgrade_evidence.EVIDENCE_PACKAGE_V3_SCHEMA or package.get("version") != target_version:
        raise ConfigurationError("VC-2 只接受目标版本的 EvidencePackage v3。")
    items = {str(item["evidence_id"]): dict(item) for item in package["evidence_items"]}
    completeness_ref = dict(package["completeness_ref"])
    completeness = _store_call("读取完整性清单", lambda: store.load_object(completeness_ref))["payload"]
    by_purpose = {purpose: name for name, purpose in claude_upgrade_evidence.COMPLETENESS_PURPOSES.items()}
    found: dict[str, Any] = {}
    for entry in completeness.get("entries") or []:
        facts = entry.get("facts") if isinstance(entry, Mapping) else None
        name = by_purpose.get(facts.get("purpose")) if isinstance(facts, Mapping) else None
        if name is None:
            continue
        if name in found:
            raise ConfigurationError(f"VC-1 完整性清单重复登记了 {name}。")
        found[name] = facts.get("binding")
    missing = [name for name in VC1_OBJECTS if name not in found]
    if missing:
        raise ConfigurationError(f"VC-1 完整性清单缺少 VC-2 必需的封存对象：{missing}")
    data_root = _capture()._host_data_root(campaign_dir)
    objects: dict[str, dict[str, Any]] = {}
    object_bindings: dict[str, dict[str, Any]] = {}
    for name in VC1_OBJECTS:
        objects[name], object_bindings[name] = _read_bound_json(data_root, found[name], f"VC-1 封存对象 {name}")
    if objects["capture_index"].get("result") != "passed":
        raise ConfigurationError("VC-1 CaptureIndex 不是 passed：VC-1 尚未闭合。")
    matrix = objects["cross_source_matrix"]
    if (
        matrix.get("schema_version") != claude_upgrade_evidence.CROSS_SOURCE_MATRIX_SCHEMA
        or (matrix.get("closure") or {}).get("status") != "closed"
        or not isinstance(matrix.get("rules"), list)
    ):
        raise ConfigurationError("VC-1 跨来源矩阵的合同名不符或闭合门禁不是 closed。")
    if objects["target_inventory"].get("target_version") != target_version:
        raise ConfigurationError("VC-1 目标 SinkInventory 的版本与 Campaign 目标版本不一致。")
    frozen_scenarios = claude_upgrade_seal._frozen_input(campaign_dir, manifest, "target_scenarios")
    if file_sha256(frozen_scenarios) != object_bindings["target_scenarios"]["sha256"]:
        raise ConfigurationError("VC-1 证据所依据的目标场景清单与本 Campaign 冻结的不一致。")
    scenarios = _read_json(frozen_scenarios, "冻结的目标场景清单")
    claude_upgrade_plan.validate_scenario_manifest(scenarios)
    if scenarios["claude_version"] != target_version:
        raise ConfigurationError("冻结的目标场景清单版本与 Campaign 目标版本不一致。")
    rule_evidence: dict[str, list[str]] = {}
    for row in matrix["rules"]:
        spec_id = row.get("spec_id") if isinstance(row, Mapping) else None
        evidence_ids = row.get("evidence_ids") if isinstance(row, Mapping) else None
        if not isinstance(spec_id, str) or not isinstance(evidence_ids, list) or spec_id in rule_evidence:
            raise ConfigurationError("VC-1 跨来源矩阵的规则行非法或重复。")
        unknown = sorted(set(evidence_ids) - set(items))
        if unknown:
            raise ConfigurationError(f"VC-1 跨来源矩阵引用了 EvidencePackage 之外的证据项：{unknown}")
        rule_evidence[spec_id] = sorted(set(evidence_ids))
    baseline_path, baseline_shown = _baseline_manifest_path(campaign_dir, manifest)
    rules = claude_upgrade_plan.load_baseline_rules(baseline_path, baseline_version)
    if tuple(manifest["required_rules"]) != rules:
        raise ConfigurationError("总计划 required_rules 与基线 RequiredRules 清单不一致。")
    baseline_payload = _read_json(baseline_path, "基线 RequiredRules 清单")
    baseline = rule_manifest_view(baseline_payload, expected_version=baseline_version, label="基线 RequiredRules 清单")
    discoveries = _discovery_rows(objects["discovery_inventory"], target_version)
    candidates_binding = _candidate_binding(campaign_dir, Path(candidates_path))
    candidates = _candidate_rows(Path(candidates_path), target_version)
    observed, observed_sources = _observed_only_facts(items, data_root, scenarios)
    classification = [
        fact
        for fact in _store_call("读取 Store 事实", lambda: store.list_facts(campaign_id))
        if fact["fact_kind"] == "rule_classification_recorded"
    ]
    if len(classification) > 1:
        raise ConfigurationError("Store 里同一 Campaign 有多条规则分类事实，状态不可信。")
    bindings = {
        "campaign": {
            "campaign_id": campaign_id,
            "baseline_version": baseline_version,
            "target_version": target_version,
            "campaign_purpose": str(manifest["campaign_purpose"]),
            "required_rules": list(rules),
        },
        "vc1_checkpoint": checkpoint_binding,
        "official_stage": stage_binding,
        "store": {
            "campaign_id": str(store_campaign["campaign_id"]),
            "campaign_identity_sha256": str(store_campaign["identity_sha256"]),
            "evidence_fact_ref": evidence_fact_ref,
            "evidence_package_ref": evidence_package_ref,
            "completeness_ref": completeness_ref,
        },
        "vc1_objects": object_bindings,
        "baseline_rules": {"path": baseline_shown, "sha256": file_sha256(baseline_path)},
        "candidates": candidates_binding,
        "observed_only_sources": observed_sources,
    }
    return ClassificationInputs(
        bindings=bindings,
        sha256=orchestrator._fingerprint(bindings),
        campaign_id=campaign_id,
        baseline_version=baseline_version,
        target_version=target_version,
        purpose=str(manifest["campaign_purpose"]),
        baseline=baseline,
        baseline_payload=baseline_payload,
        discoveries=discoveries,
        candidates=candidates,
        observed=observed,
        rule_evidence=rule_evidence,
        evidence_items=items,
        scenarios=scenarios,
        persona=dict(package["persona"]),
        evidence_fact_ref=evidence_fact_ref,
        evidence_package_ref=evidence_package_ref,
        store_root=Path(store.root),
        store_campaign_id=str(store_campaign["campaign_id"]),
        tool_files_sha256=tool_files_sha256,
        reference_problems=_reference_problems(discoveries, candidates),
        classification_fact=classification[0] if classification else None,
    )


def denominators(inputs: ClassificationInputs) -> dict[str, Any]:
    """草案分母：全部由输入计算（2.1.226 夹具应等于历史清零收据的 7368／593／110／40）。"""

    return {
        "discovery_count": len(inputs.discoveries),
        "discovery_source_kind_counts": dict(sorted(Counter(str(row["source_kind"]) for row in inputs.discoveries).items())),
        "candidate_count": len(inputs.candidates),
        "candidate_group_counts": dict(sorted(Counter(str(row["candidate_group"]) for row in inputs.candidates).items())),
        "observed_only_fact_count": len(inputs.observed),
        "observed_only_present_count": sum(1 for fact in inputs.observed if fact["observed"]),
        "baseline_rule_count": len(inputs.baseline["rules"]),
        "baseline_atomic_assertion_count": len(inputs.baseline["owners"]),
        "baseline_profile_atomic_assertion_count": inputs.baseline["profile_count"],
        "baseline_scenario_only_assertion_count": inputs.baseline["scenario_count"],
        "evidence_item_count": len(inputs.evidence_items),
    }


# ---------------------------------------------------------------------------
# 机器草案
# ---------------------------------------------------------------------------


def _applicability_hint(inputs: ClassificationInputs, evidence_ids: Sequence[str]) -> list[str]:
    """线索：这些证据项共有的适用条件（证据项自身登记的 probe／平台／隐私等条件之交）。"""

    common: set[str] | None = None
    for evidence_id in evidence_ids:
        values = set(inputs.evidence_items[evidence_id].get("applicability") or [])
        common = values if common is None else common & values
    return sorted(common or set())


def _ledger_sources(inputs: ClassificationInputs) -> dict[str, Any]:
    """处置台账绑定的输入（只读）：VC-1 的 DiscoveryInventory、CaptureIndex、跨来源矩阵、SinkInventory、EvidencePackage 与候选分母。"""

    objects = inputs.bindings["vc1_objects"]
    return {
        "discovery_inventory": dict(objects["discovery_inventory"]),
        "capture_index": dict(objects["capture_index"]),
        "cross_source_matrix": dict(objects["cross_source_matrix"]),
        "target_inventory": dict(objects["target_inventory"]),
        "evidence_package_ref": dict(inputs.evidence_package_ref),
        "candidates": dict(inputs.bindings["candidates"]),
    }


def _scenario_rule_binding(target_bytes: bytes, rule_count: int) -> dict[str, Any]:
    """场景清单对同目录目标规则清单的绑定（文件字节摘要）。"""

    return {"path": DRAFT_FILES["target_rules"], "sha256": _sha256(target_bytes), "rule_count": rule_count}


def machine_draft_files(inputs: ClassificationInputs) -> dict[str, bytes]:
    """由输入确定性生成机器草案的六个文件：全部未决，只预填不构成结论的线索。"""

    baseline = inputs.baseline_payload
    hints = {spec_id: list(inputs.rule_evidence.get(spec_id, [])) for spec_id in inputs.baseline["rules"]}
    target_rules = {
        "schema_version": TARGET_RULES_SCHEMA,
        "target_version": inputs.target_version,
        "rule_standard": baseline["rule_standard"],
        "atomic_assertion_ledger": DRAFT_FILES["atomic_assertions"],
        "atomic_assertion_count": baseline["atomic_assertion_count"],
        "profile_atomic_assertion_count": baseline["profile_atomic_assertion_count"],
        "scenario_only_assertion_count": baseline["scenario_only_assertion_count"],
        "required_rule_count": baseline["required_rule_count"],
        "required_rules": json.loads(json.dumps(baseline["required_rules"], ensure_ascii=False)),
        "scenario_only_groups": json.loads(json.dumps(baseline["scenario_only_groups"], ensure_ascii=False)),
    }
    spec_ids = sorted(inputs.baseline["rules"])
    migration = {
        "schema_version": RULE_MIGRATION_DRAFT_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "baseline_version": inputs.baseline_version,
        "target_version": inputs.target_version,
        "baseline_spec_ids": spec_ids,
        "target_spec_ids": spec_ids,
        "rules": [
            {
                "spec_id": spec_id,
                "migration_decision": BLOCKED,
                "evidence_level": BLOCKED,
                "rule_lifecycle": "candidate",
                "compatibility_class": "request_egress",
                "evidence_item_ids": hints[spec_id],
                "applicability": _applicability_hint(inputs, hints[spec_id]),
                "rationale": RULE_PLACEHOLDER_RATIONALE,
            }
            for spec_id in spec_ids
        ],
        "affected_rules": [],
        "inherited_rules": [],
    }
    assertions = []
    for assertion_id in sorted(inputs.baseline["owners"]):
        kind, owner = inputs.baseline["owners"][assertion_id]
        evidence = hints.get(owner, []) if kind == "required_rule" else []
        assertions.append(
            {
                "assertion_id": assertion_id,
                "owner_kind": kind,
                "owner_id": owner,
                "evidence_item_ids": list(evidence),
                "conditions": _applicability_hint(inputs, evidence),
                "channels": [],
                "result": BLOCKED,
                "rationale": ITEM_PLACEHOLDER_RATIONALE,
            }
        )
    atomic = {
        "schema_version": ATOMIC_ASSERTION_DRAFT_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "target_version": inputs.target_version,
        "scenario_only_owner_ids": sorted(inputs.baseline["groups"]),
        "assertions": assertions,
    }
    pending = {"bindings": [], "rationale": ITEM_PLACEHOLDER_RATIONALE}
    ledger = {
        "schema_version": DISCOVERY_LEDGER_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "target_version": inputs.target_version,
        "sources": _ledger_sources(inputs),
        "discoveries": [{**row, **pending} for row in inputs.discoveries],
        "candidates": [{**row, **pending} for row in inputs.candidates],
        "observed_only_facts": [{**row, **pending} for row in inputs.observed],
    }
    patches = {
        "schema_version": PROFILE_PATCHES_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "baseline_version": inputs.baseline_version,
        "target_version": inputs.target_version,
        "rule_patches": [],
    }
    files = {"target_rules": _serialize(target_rules)}
    scenarios = json.loads(json.dumps(inputs.scenarios, ensure_ascii=False))
    scenarios["rule_manifest"] = _scenario_rule_binding(files["target_rules"], len(target_rules["required_rules"]))
    files.update(
        {
            "rule_migration": _serialize(migration),
            "atomic_assertions": _serialize(atomic),
            "discovery_dispositions": _serialize(ledger),
            "scenarios": _serialize(scenarios),
            "profile_patches": _serialize(patches),
        }
    )
    return {name: files[name] for name in DRAFT_FILES}


# ---------------------------------------------------------------------------
# 清零门禁
# ---------------------------------------------------------------------------


class _Problems:
    """门禁问题：原因码 → 明细（汇报时每个原因给出计数与前 10 条样例）。"""

    def __init__(self) -> None:
        self._items: dict[str, list[str]] = {}

    def add(self, code: str, detail: str) -> None:
        self._items.setdefault(code, []).append(detail)

    def extend(self, code: str, details: Iterable[str]) -> None:
        for detail in details:
            self.add(code, detail)

    def __bool__(self) -> bool:
        return bool(self._items)

    def reasons(self) -> list[dict[str, Any]]:
        return [
            {"code": code, "count": len(details), "sample": details[:10]}
            for code, details in sorted(self._items.items())
        ]


def _check_target_rules(inputs: ClassificationInputs, document: Mapping[str, Any], problems: _Problems) -> dict[str, Any] | None:
    try:
        view = rule_manifest_view(document, expected_version=inputs.target_version, label="目标 RequiredRules 清单")
    except ConfigurationError as error:
        problems.add("target_rules_invalid", str(error))
        return None
    if document["atomic_assertion_ledger"] != DRAFT_FILES["atomic_assertions"]:
        problems.add("target_rules_invalid", f"atomic_assertion_ledger 必须指向同目录的 {DRAFT_FILES['atomic_assertions']}")
    for spec_id, rule in view["rule_docs"].items():
        if rule["responsibility"] == CLIENT_LOCAL_RESPONSIBILITY:
            problems.add("responsibility_boundary", f"{spec_id}：客户端本地职责只能进 scenario-only 组，不能成为 RequiredRule")
    return view


def _check_migration(
    inputs: ClassificationInputs,
    document: Mapping[str, Any],
    target: Mapping[str, Any] | None,
    problems: _Problems,
) -> dict[str, Any] | None:
    if set(document) != MIGRATION_DOC_FIELDS or document.get("schema_version") != RULE_MIGRATION_DRAFT_SCHEMA:
        problems.add("migration_invalid", "rule-migration.json 字段不闭合或合同名不符")
        return None
    if (document["campaign_id"], document["baseline_version"], document["target_version"]) != (
        inputs.campaign_id,
        inputs.baseline_version,
        inputs.target_version,
    ):
        problems.add("migration_invalid", "rule-migration.json 的 Campaign 或版本不符")
    baseline_ids = sorted(inputs.baseline["rules"])
    if document["baseline_spec_ids"] != baseline_ids:
        problems.add("migration_invalid", "baseline_spec_ids 必须等于基线 RequiredRules 全集（只读）")
    target_ids = document["target_spec_ids"]
    rows = document["rules"]
    if not _sorted_unique_strings(target_ids, pattern=SPEC_ID_RE) or not isinstance(rows, list):
        problems.add("migration_invalid", "target_spec_ids 必须是排序去重的 SPEC 编号，rules 必须是数组")
        return None
    if any(not isinstance(row, dict) or set(row) != RULE_ROW_FIELDS for row in rows):
        problems.add("migration_invalid", "rules 条目字段不闭合")
        return None
    spec_ids = [row["spec_id"] for row in rows]
    universe = set(baseline_ids) | set(target_ids)
    if (
        any(not isinstance(spec_id, str) for spec_id in spec_ids)
        or spec_ids != sorted(set(spec_ids))
        or set(spec_ids) != universe
    ):
        problems.add("migration_invalid", "rules 必须按 spec_id 排序、不重复，且恰为基线／目标规则的并集")
        return None
    baseline_set, target_set = set(baseline_ids), set(target_ids)
    decided: dict[str, dict[str, Any]] = {}
    decisions: dict[str, str] = {}
    blocked: list[str] = []
    for row in rows:
        spec_id = row["spec_id"]
        decision, level = row["migration_decision"], row["evidence_level"]
        if decision == BLOCKED or level == BLOCKED:
            blocked.append(spec_id)
            continue
        if not isinstance(decision, str) or decision not in MIGRATION_DECISIONS or not isinstance(level, str) or level not in EVIDENCE_LEVELS:
            problems.add("migration_invalid", f"{spec_id}：迁移决定或证据等级不在词表内（{decision!r}／{level!r}）")
            continue
        decisions[spec_id] = decision
        valid = True
        lifecycle, compatibility = row["rule_lifecycle"], row["compatibility_class"]
        if (
            not isinstance(lifecycle, str)
            or lifecycle not in RULE_LIFECYCLES
            or not isinstance(compatibility, str)
            or compatibility not in COMPATIBILITY_CLASSES
        ):
            problems.add("migration_invalid", f"{spec_id}：生命周期或兼容类别不在词表内")
            valid = False
        in_baseline, in_target = spec_id in baseline_set, spec_id in target_set
        if decision == "add" and (in_baseline or not in_target):
            problems.add("migration_invalid", f"{spec_id}：add 只能用于目标新增规则")
            valid = False
        elif decision == "delete" and (not in_baseline or in_target):
            problems.add("migration_invalid", f"{spec_id}：delete 只能用于基线规则，且必须移出目标规则")
            valid = False
        elif decision in {"inherit", "change", "condition_change"} and not (in_baseline and in_target):
            problems.add("migration_invalid", f"{spec_id}：{decision} 要求规则同时在基线与目标里")
            valid = False
        if decision == "delete" and row["rule_lifecycle"] != "superseded":
            problems.add("migration_invalid", f"{spec_id}：delete 必须使用 superseded 生命周期")
            valid = False
        if not _sorted_unique_strings(row["evidence_item_ids"], pattern=SAFE_ID_RE):
            problems.add("migration_evidence_missing", spec_id)
            valid = False
        else:
            unknown = sorted(set(row["evidence_item_ids"]) - set(inputs.evidence_items))
            if unknown:
                problems.add("evidence_item_unknown", f"{spec_id}：{unknown}")
                valid = False
        if not _sorted_unique_strings(row["applicability"]):
            problems.add("migration_applicability_missing", spec_id)
            valid = False
        if decision != "delete" and compatibility != "request_egress":
            problems.add("responsibility_boundary", f"{spec_id}：RequiredRule 只承担 request_egress（{compatibility!r}）")
            valid = False
        if inputs.purpose == "production_replacement" and decision != "delete" and level in {"observed", "regressed_evidence"}:
            problems.add("evidence_level_insufficient", f"{spec_id}：production_replacement 不接受 {level} 承担目标规则")
            valid = False
        if not _text(row["rationale"]) or row["rationale"] == RULE_PLACEHOLDER_RATIONALE:
            problems.add("rationale_placeholder", spec_id)
            valid = False
        if valid:
            decided[spec_id] = row
    if blocked:
        problems.extend("migration_blocked", blocked)
    # 两个规则集合按已给出的迁移决定派生（其余字段的问题另行报告，不连带成集合不一致）。
    affected = sorted(spec_id for spec_id, decision in decisions.items() if decision != "inherit")
    inherited = sorted(spec_id for spec_id, decision in decisions.items() if decision == "inherit")
    if document["affected_rules"] != affected or document["inherited_rules"] != inherited:
        problems.add("migration_partition_mismatch", f"affected_rules 应为 {affected}，inherited_rules 应为 {inherited}")
    if target is not None:
        if set(target["rules"]) != target_set:
            problems.add(
                "target_rules_mismatch",
                f"目标规则清单与 target_spec_ids 不一致：多 {sorted(set(target['rules']) - target_set)}，少 {sorted(target_set - set(target['rules']))}",
            )
        baseline_owned = _owned_by(inputs.baseline["owners"])
        target_owned = _owned_by(target["owners"])
        for spec_id in inherited:
            if baseline_owned.get(spec_id) != target_owned.get(spec_id):
                problems.add("inherit_assertions_changed", spec_id)
    return {
        "target_ids": list(target_ids),
        "rule_ids": universe,
        "decided": decided,
        "decision_by_spec": decisions,
        "blocked": blocked,
        "affected": affected,
        "inherited": inherited,
        "decisions": dict(sorted(Counter(str(row["migration_decision"]) for row in rows).items())),
    }


def _owned_by(owners: Mapping[str, tuple[str, str]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for assertion_id, (_kind, owner) in owners.items():
        result.setdefault(owner, set()).add(assertion_id)
    return result


def _check_assertions(
    inputs: ClassificationInputs,
    document: Mapping[str, Any],
    target: Mapping[str, Any] | None,
    migration: Mapping[str, Any] | None,
    problems: _Problems,
) -> dict[str, Any] | None:
    if set(document) != ASSERTION_DOC_FIELDS or document.get("schema_version") != ATOMIC_ASSERTION_DRAFT_SCHEMA:
        problems.add("assertion_invalid", "atomic-assertion-ledger.json 字段不闭合或合同名不符")
        return None
    if (document["campaign_id"], document["target_version"]) != (inputs.campaign_id, inputs.target_version):
        problems.add("assertion_invalid", "atomic-assertion-ledger.json 的 Campaign 或目标版本不符")
    groups = document["scenario_only_owner_ids"]
    rows = document["assertions"]
    if not _sorted_unique_strings(groups, non_empty=False, pattern=SAFE_ID_RE) or not isinstance(rows, list) or not rows:
        problems.add("assertion_invalid", "scenario_only_owner_ids 必须排序去重，assertions 不能为空")
        return None
    if any(not isinstance(row, dict) or set(row) != ASSERTION_ROW_FIELDS for row in rows):
        problems.add("assertion_invalid", "assertions 条目字段不闭合")
        return None
    assertion_ids = [row["assertion_id"] for row in rows]
    if any(not isinstance(item, str) or not SAFE_ID_RE.fullmatch(item) for item in assertion_ids) or assertion_ids != sorted(set(assertion_ids)):
        problems.add("assertion_invalid", "assertions 必须按 assertion_id 排序、不重复且 ID 合法")
        return None
    target_ids = set(migration["target_ids"]) if migration is not None else set(target["rules"]) if target is not None else set()
    owners: dict[str, tuple[str, str]] = {}
    blocked: list[str] = []
    for row in rows:
        assertion_id, kind, owner = row["assertion_id"], row["owner_kind"], row["owner_id"]
        if not isinstance(kind, str) or not isinstance(owner, str):
            problems.add("assertion_invalid", f"{assertion_id}：owner_kind 与 owner_id 必须是文字")
            continue
        if kind == "required_rule":
            if owner not in target_ids:
                problems.add("assertion_owner_open", f"{assertion_id} → {owner}（不是目标规则）")
        elif kind == "scenario_only":
            if owner not in groups:
                problems.add("assertion_owner_open", f"{assertion_id} → {owner}（不在 scenario_only_owner_ids）")
        else:
            problems.add("assertion_invalid", f"{assertion_id}：owner_kind 只能是 required_rule 或 scenario_only")
        owners[assertion_id] = (kind, owner)
        result = row["result"]
        if result == BLOCKED:
            blocked.append(assertion_id)
            continue
        if not isinstance(result, str) or result not in REVIEW_OUTCOMES:
            problems.add("assertion_invalid", f"{assertion_id}：result 只能是 pass 或 observed")
        channels = row["channels"]
        if (
            not isinstance(channels, list)
            or not channels
            or any(channel not in CHANNEL_ORDER for channel in channels)
            or channels != sorted(set(channels), key=CHANNEL_ORDER.index)
            or "M" not in channels
            or not ({"R", "P"} & set(channels))
        ):
            problems.add("assertion_channel_invalid", f"{assertion_id}：通道须按 P／R／J／M 排序，且含 M 与 R 或 P")
        if not _sorted_unique_strings(row["conditions"]):
            problems.add("assertion_conditions_missing", assertion_id)
        if not _sorted_unique_strings(row["evidence_item_ids"], pattern=SAFE_ID_RE):
            problems.add("assertion_evidence_missing", assertion_id)
        else:
            unknown = sorted(set(row["evidence_item_ids"]) - set(inputs.evidence_items))
            if unknown:
                problems.add("evidence_item_unknown", f"{assertion_id}：{unknown}")
        if not isinstance(row["rationale"], str):
            problems.add("assertion_invalid", f"{assertion_id}：rationale 必须是文字")
    if blocked:
        problems.extend("assertion_blocked", blocked)
    owned = _owned_by(owners)
    without = sorted(spec_id for spec_id in target_ids if not owned.get(spec_id))
    if without:
        problems.extend("rule_without_assertion", without)
    empty_groups = sorted(group for group in groups if not owned.get(group))
    if empty_groups:
        problems.extend("assertion_owner_open", [f"scenario-only 组 {group} 没有断言" for group in empty_groups])
    if target is not None:
        if set(groups) != set(target["groups"]):
            problems.add("ownership_mismatch", "scenario_only_owner_ids 与目标规则清单的 scenario-only 组不一致")
        mismatched = sorted(
            assertion_id
            for assertion_id in set(owners) | set(target["owners"])
            if owners.get(assertion_id) != target["owners"].get(assertion_id)
        )
        if mismatched:
            problems.extend("ownership_mismatch", mismatched)
    profile = sum(1 for kind, _owner in owners.values() if kind == "required_rule")
    return {"count": len(owners), "profile_count": profile, "scenario_count": len(owners) - profile, "blocked": len(blocked)}


def _binding_problems(
    binding: Any,
    *,
    own_id: str,
    partition_ids: set[str],
    rule_ids: set[str],
    target_ids: set[str],
) -> list[tuple[str, str]]:
    """一条终态绑定的合同校验：返回 (原因码, 明细)；空表示合法。"""

    if not isinstance(binding, dict):
        return [("binding_invalid", "绑定必须是对象")]
    kind = binding.get("binding_type")
    spec = TERMINAL_BINDINGS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        return [("binding_invalid", f"未知终态 {kind!r}")]
    required, optional = spec
    keys = set(binding) - {"binding_type"}
    if not required <= keys or keys - required - optional:
        return [("binding_invalid", f"{kind} 字段不闭合")]
    problems: list[tuple[str, str]] = []
    if kind == "rule_bound":
        if not _sorted_unique_strings(binding["spec_ids"], pattern=SPEC_ID_RE):
            problems.append(("binding_invalid", "rule_bound.spec_ids 必须是排序去重的 SPEC 编号"))
        else:
            orphan = sorted(set(binding["spec_ids"]) - rule_ids)
            if orphan:
                problems.append(("orphan_rule_reference", f"rule_bound 指向迁移台账之外的规则 {orphan}"))
    elif kind == "supporting_fact_bound":
        if not _sorted_unique_strings(binding["fact_ids"], pattern=SAFE_ID_RE):
            problems.append(("binding_invalid", "supporting_fact_bound.fact_ids 必须是排序去重的稳定 ID"))
        owner_kind = binding["owner_kind"]
        if not isinstance(owner_kind, str) or owner_kind not in SUPPORTING_FACT_OWNERS:
            problems.append(("binding_invalid", f"supporting_fact_bound.owner_kind 不在词表内：{owner_kind!r}"))
        elif owner_kind == "required_rule":
            owner_id = binding.get("owner_id")
            if not isinstance(owner_id, str) or owner_id not in target_ids:
                problems.append(("orphan_rule_reference", f"支撑事实的所属规则不是目标规则：{owner_id!r}"))
        elif "owner_id" in binding and not (isinstance(binding["owner_id"], str) and SAFE_ID_RE.fullmatch(binding["owner_id"])):
            problems.append(("binding_invalid", "supporting_fact_bound.owner_id 非法"))
        if "managed_egress_ids" in binding and not _sorted_unique_strings(binding["managed_egress_ids"], pattern=SAFE_ID_RE):
            problems.append(("binding_invalid", "supporting_fact_bound.managed_egress_ids 非法"))
    elif kind == "managed_egress_bound":
        if not _sorted_unique_strings(binding["managed_egress_ids"], pattern=SAFE_ID_RE) or not _text(binding["disposition"]):
            problems.append(("binding_invalid", "managed_egress_bound 需要受管出站 ID 与处置"))
    elif kind in {"non_egress_proven", "target_absent_proven"}:
        texts = ("proof_kind", "rationale") if kind == "non_egress_proven" else ("rationale",)
        if any(not _text(binding[field]) for field in texts) or not _sorted_unique_strings(binding["evidence_paths"]):
            problems.append(("binding_invalid", f"{kind} 需要非空理由与排序去重的证据路径"))
    elif kind == "duplicate_bound":
        canonical = binding["canonical_id"]
        if not isinstance(canonical, str) or canonical == own_id or canonical not in partition_ids:
            problems.append(("duplicate_orphan", f"duplicate_bound 必须指向同分区另一项：{canonical!r}"))
    if "evidence_role" in required and not _text(binding["evidence_role"]):
        problems.append(("binding_invalid", f"{kind}.evidence_role 不能为空"))
    return problems


def _check_partition(
    label: str,
    rows: Any,
    key: str,
    expected_rows: Sequence[Mapping[str, Any]],
    fixed: Sequence[str],
    *,
    rule_ids: set[str],
    target_ids: set[str],
    problems: _Problems,
) -> tuple[dict[str, int], dict[str, int]]:
    """一个处置分区：ID 集合与只读字段、逐项终态绑定、duplicate 链；返回 (计数, 终态类型计数)。"""

    expected = {str(row[key]): row for row in expected_rows}
    counts = {"total": len(expected), "resolved": 0, "unresolved": len(expected)}
    if not isinstance(rows, list):
        problems.add(f"{label}_set_mismatch", f"{label} 分区必须是数组")
        return counts, {}
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    allowed = set(fixed) | set(EDITABLE_ITEM_FIELDS)
    for row in rows:
        identifier = row.get(key) if isinstance(row, dict) else None
        if not isinstance(row, dict) or set(row) != allowed or not isinstance(identifier, str):
            problems.add(f"{label}_fields_modified", f"条目字段不闭合：{identifier!r}")
            continue
        if identifier in seen:
            problems.add(f"{label}_set_mismatch", f"重复：{identifier}")
            continue
        seen[identifier] = row
        order.append(identifier)
        if identifier not in expected:
            problems.add(f"{label}_set_mismatch", f"输入之外的条目：{identifier}")
        elif {field: row[field] for field in fixed} != {field: expected[identifier][field] for field in fixed}:
            problems.add(f"{label}_fields_modified", identifier)
        if not isinstance(row["rationale"], str):
            problems.add(f"{label}_fields_modified", f"{identifier}：rationale 必须是文字")
    for identifier in sorted(set(expected) - set(seen)):
        problems.add(f"{label}_set_mismatch", f"缺失：{identifier}")
    if order != sorted(order):
        problems.add(f"{label}_set_mismatch", f"{label} 分区必须按 {key} 排序")
    types: Counter[str] = Counter()
    resolved: set[str] = set()
    duplicates: dict[str, str] = {}
    for identifier in sorted(expected):
        row = seen.get(identifier)
        if row is None:
            continue
        bindings = row["bindings"]
        if not isinstance(bindings, list):
            problems.add("binding_invalid", f"{identifier}：bindings 必须是数组")
            continue
        if not bindings:
            continue
        valid = True
        for binding in bindings:
            for code, detail in _binding_problems(
                binding, own_id=identifier, partition_ids=set(expected), rule_ids=rule_ids, target_ids=target_ids
            ):
                problems.add(code, f"{identifier}：{detail}")
                valid = False
        kinds = [str(binding.get("binding_type")) for binding in bindings if isinstance(binding, dict)]
        if "duplicate_bound" in kinds:
            if len(bindings) != 1:
                problems.add("binding_invalid", f"{identifier}：duplicate_bound 必须是唯一绑定")
                valid = False
            elif valid:
                duplicates[identifier] = str(bindings[0]["canonical_id"])
        if label == "observed_only":
            fact = expected[identifier]
            if not fact["evaluable"]:
                problems.add("observed_only_not_evaluable", identifier)
                valid = False
            elif not fact["observed"] and set(kinds) - OBSERVED_ABSENT_BINDINGS:
                problems.add("observed_only_binding_forbidden", f"{identifier}：合法零流量只能记支撑事实或目标缺失，不得生成规则")
                valid = False
        types.update(kinds)
        if valid:
            resolved.add(identifier)
    for identifier in sorted(duplicates):
        chain = [identifier]
        current = duplicates[identifier]
        while current in duplicates and current not in chain:
            chain.append(current)
            current = duplicates[current]
        if current in chain:
            problems.add("duplicate_cycle", " → ".join([*chain, current]))
            resolved.discard(identifier)
        elif current not in resolved:
            problems.add("duplicate_unresolved", f"{identifier} 的规范项 {current} 未解决")
            resolved.discard(identifier)
    unresolved = sorted(set(expected) - resolved)
    if unresolved:
        problems.extend(f"{label}_unresolved", unresolved)
    counts = {"total": len(expected), "resolved": len(resolved), "unresolved": len(unresolved)}
    return counts, dict(sorted(types.items()))


def _check_dispositions(
    inputs: ClassificationInputs,
    document: Mapping[str, Any],
    migration: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
    problems: _Problems,
) -> dict[str, Any]:
    empty = {"total": 0, "resolved": 0, "unresolved": 0}
    result: dict[str, Any] = {"discovery": dict(empty), "candidate": dict(empty), "observed_only": dict(empty), "binding_type_counts": {}}
    if set(document) != LEDGER_DOC_FIELDS or document.get("schema_version") != DISCOVERY_LEDGER_SCHEMA:
        problems.add("disposition_invalid", "discovery-disposition-ledger.json 字段不闭合或合同名不符")
        return result
    if (document["campaign_id"], document["target_version"]) != (inputs.campaign_id, inputs.target_version):
        problems.add("disposition_invalid", "discovery-disposition-ledger.json 的 Campaign 或目标版本不符")
    if document["sources"] != _ledger_sources(inputs):
        problems.add("disposition_sources_drift", "sources 必须逐字等于本次输入的 VC-1 绑定（只读）")
    if migration is not None:
        rule_ids, target_ids = set(migration["rule_ids"]), set(migration["target_ids"])
    else:
        rule_ids = set(inputs.baseline["rules"]) | (set(target["rules"]) if target is not None else set())
        target_ids = set(target["rules"]) if target is not None else set(inputs.baseline["rules"])
    for label, field, key, expected, fixed in (
        ("discovery", "discoveries", "discovery_id", inputs.discoveries, DISCOVERY_FIXED_FIELDS),
        ("candidate", "candidates", "candidate_id", inputs.candidates, CANDIDATE_FIXED_FIELDS),
        ("observed_only", "observed_only_facts", "fact_id", inputs.observed, OBSERVED_FIXED_FIELDS),
    ):
        counts, types = _check_partition(
            label, document[field], key, expected, fixed, rule_ids=rule_ids, target_ids=target_ids, problems=problems
        )
        result[label] = counts
        result["binding_type_counts"][label] = types
    if inputs.reference_problems:
        problems.extend("input_reference_open", inputs.reference_problems)
    return result


def _check_scenarios(
    inputs: ClassificationInputs,
    document: dict[str, Any],
    target_bytes: bytes,
    target: Mapping[str, Any] | None,
    migration: Mapping[str, Any] | None,
    problems: _Problems,
) -> None:
    try:
        claude_upgrade_plan.validate_scenario_manifest(document)
    except ConfigurationError as error:
        problems.add("scenarios_invalid", str(error))
        return
    if document["claude_version"] != inputs.target_version:
        problems.add("scenarios_invalid", "scenarios.json 的 claude_version 不是目标版本")
    if target is not None and document["rule_manifest"] != _scenario_rule_binding(target_bytes, len(target["rules"])):
        problems.add(
            "scenarios_rule_binding",
            f"rule_manifest 必须绑定同目录目标规则清单：{_scenario_rule_binding(target_bytes, len(target['rules']))}",
        )
    fingerprint = _orchestrator()._fingerprint
    if fingerprint(claude_upgrade_plan.scenario_job_execution_contract(document)) != fingerprint(
        claude_upgrade_plan.scenario_job_execution_contract(inputs.scenarios)
    ):
        problems.add("scenarios_execution_changed", "官方取证执行契约与 VC-1 冻结值不一致：命令或证据身份变化必须新建 Campaign")
    target_ids = set(migration["target_ids"]) if migration is not None else set(target["rules"]) if target is not None else set()
    covered = {rule for scenario in document["evidence_scenarios"] for rule in scenario["covers"]}
    unknown = sorted(covered - target_ids)
    if unknown:
        problems.extend("scenarios_unknown_rule", unknown)
    gaps = sorted(target_ids - covered)
    if gaps:
        problems.extend("scenarios_coverage_gap", gaps)


def _check_profile_patches(
    inputs: ClassificationInputs,
    document: Mapping[str, Any],
    migration: Mapping[str, Any] | None,
    problems: _Problems,
) -> None:
    if set(document) != PATCH_DOC_FIELDS or document.get("schema_version") != PROFILE_PATCHES_SCHEMA:
        problems.add("profile_patches_invalid", "profile-patches.json 字段不闭合或合同名不符")
        return
    if (document["campaign_id"], document["baseline_version"], document["target_version"]) != (
        inputs.campaign_id,
        inputs.baseline_version,
        inputs.target_version,
    ):
        problems.add("profile_patches_invalid", "profile-patches.json 的 Campaign 或版本不符")
    rows = document["rule_patches"]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or set(row) != PATCH_ROW_FIELDS or not isinstance(row["spec_id"], str) for row in rows
    ):
        problems.add("profile_patches_invalid", "rule_patches 条目字段不闭合")
        return
    spec_ids = [row["spec_id"] for row in rows]
    if spec_ids != sorted(set(spec_ids)):
        problems.add("profile_patches_invalid", "rule_patches 必须按 spec_id 排序且不重复")
    for row in rows:
        paths = row["profile_paths"]
        if not _sorted_unique_strings(paths, non_empty=False) or any(not path.startswith("/") for path in paths) or not isinstance(row["rationale"], str):
            problems.add("profile_patches_invalid", f"{row['spec_id']}：profile_paths 必须是排序去重的 JSON Pointer，rationale 必须是文字")
    if migration is None:
        return
    if sorted(set(spec_ids)) != migration["affected"]:
        problems.add("profile_patches_mismatch", f"补丁计划必须恰好覆盖 affected_rules：{migration['affected']}")
    for row in rows:
        decision = migration["decision_by_spec"].get(row["spec_id"])
        if decision is not None and decision != row["migration_decision"]:
            problems.add("profile_patches_mismatch", f"{row['spec_id']}：迁移决定与台账不一致")


def store_payloads(inputs: ClassificationInputs, documents: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """把草案投影成 Store 合同的 RuleMigrationLedger 与 AtomicAssertionLedger，并预先算出两者的内容寻址引用。"""

    migration_doc = documents["rule_migration"]
    migration = {
        "schema_version": RULE_MIGRATION_LEDGER_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "persona": dict(inputs.persona),
        "baseline_version": inputs.baseline_version,
        "target_version": inputs.target_version,
        "evidence_package_ref": dict(inputs.evidence_package_ref),
        "baseline_spec_ids": list(migration_doc["baseline_spec_ids"]),
        "target_spec_ids": list(migration_doc["target_spec_ids"]),
        "rules": [{field: row[field] for field in STORE_RULE_FIELDS} for row in migration_doc["rules"]],
        "affected_rules": list(migration_doc["affected_rules"]),
        "inherited_rules": list(migration_doc["inherited_rules"]),
        "identity_sha256": "",
    }
    migration["identity_sha256"] = rule_migration_ledger_identity_sha256(migration)
    migration_ref = claude_upgrade_evidence._object_reference("rule_migration_ledger", migration)
    atomic_doc = documents["atomic_assertions"]
    atomic = {
        "schema_version": ATOMIC_ASSERTION_LEDGER_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "persona": dict(inputs.persona),
        "target_version": inputs.target_version,
        "rule_migration_ledger_ref": migration_ref,
        "scenario_only_owner_ids": list(atomic_doc["scenario_only_owner_ids"]),
        "assertions": [{field: row[field] for field in STORE_ASSERTION_FIELDS} for row in atomic_doc["assertions"]],
        "identity_sha256": "",
    }
    atomic["identity_sha256"] = atomic_assertion_ledger_identity_sha256(atomic)
    atomic_ref = claude_upgrade_evidence._object_reference("atomic_assertion_ledger", atomic)
    return {"rule_migration": migration, "rule_migration_ref": migration_ref, "atomic": atomic, "atomic_ref": atomic_ref}


def evaluate_revision(inputs: ClassificationInputs, files: Mapping[str, bytes]) -> dict[str, Any]:
    """清零门禁（指南 §4.2.4）：返回 ``status``（closed／blocked）、按原因码汇总的 ``reasons`` 与 ``counts``。"""

    problems = _Problems()
    documents: dict[str, dict[str, Any]] = {}
    for name, filename in DRAFT_FILES.items():
        try:
            documents[name] = _parse_json_object(files[name], filename)
        except ConfigurationError as error:
            problems.add("document_invalid", str(error))
    if problems:
        return {"status": "blocked", "reasons": problems.reasons(), "counts": {}}

    def guarded(filename: str, check: Callable[[], Any], default: Any = None) -> Any:
        # 审核人交回的文件可能在意料之外的位置放了错误类型的值：一律记为该文件结构非法，不让门禁抛出异常。
        try:
            return check()
        except (TypeError, KeyError, AttributeError, ValueError) as error:
            problems.add("document_invalid", f"{filename} 结构非法：{error!r}")
            return default

    empty = {"total": 0, "resolved": 0, "unresolved": 0}
    target = guarded(DRAFT_FILES["target_rules"], lambda: _check_target_rules(inputs, documents["target_rules"], problems))
    migration = guarded(DRAFT_FILES["rule_migration"], lambda: _check_migration(inputs, documents["rule_migration"], target, problems))
    assertions = guarded(
        DRAFT_FILES["atomic_assertions"], lambda: _check_assertions(inputs, documents["atomic_assertions"], target, migration, problems)
    )
    dispositions = guarded(
        DRAFT_FILES["discovery_dispositions"],
        lambda: _check_dispositions(inputs, documents["discovery_dispositions"], migration, target, problems),
        {"discovery": dict(empty), "candidate": dict(empty), "observed_only": dict(empty), "binding_type_counts": {}},
    )
    guarded(DRAFT_FILES["scenarios"], lambda: _check_scenarios(inputs, documents["scenarios"], files["target_rules"], target, migration, problems))
    guarded(DRAFT_FILES["profile_patches"], lambda: _check_profile_patches(inputs, documents["profile_patches"], migration, problems))
    if not problems:
        try:
            store_payloads(inputs, documents)
        except (ConfigurationError, ControlError, KeyError, TypeError) as error:
            problems.add("store_contract_invalid", str(error))
    counts = {
        "discovery": dispositions["discovery"],
        "candidate": dispositions["candidate"],
        "observed_only": dispositions["observed_only"],
        "binding_type_counts": dispositions["binding_type_counts"],
        "target_rule_count": len(target["rules"]) if target is not None else None,
        "atomic_assertion_count": assertions["count"] if assertions is not None else None,
        "profile_atomic_assertion_count": assertions["profile_count"] if assertions is not None else None,
        "scenario_only_assertion_count": assertions["scenario_count"] if assertions is not None else None,
        "blocked_assertion_count": assertions["blocked"] if assertions is not None else None,
        "migration_decisions": migration["decisions"] if migration is not None else None,
        "affected_rules": migration["affected"] if migration is not None else None,
        "inherited_rules": migration["inherited"] if migration is not None else None,
    }
    return {"status": "blocked" if problems else "closed", "reasons": problems.reasons(), "counts": counts}


def review_sha256(inputs: ClassificationInputs, revision: str, receipt: Mapping[str, Any], gate: Mapping[str, Any]) -> str:
    """审核摘要：revision、输入、六个文件、门禁结论与 Store 有效工具身份；任一变化都要重新审核。"""

    return _orchestrator()._fingerprint(
        {
            "schema_version": REVIEW_SCHEMA,
            "campaign_id": inputs.campaign_id,
            "revision": revision,
            "inputs_sha256": receipt["inputs_sha256"],
            "files": {name: receipt["files"][name]["sha256"] for name in DRAFT_FILES},
            "gate": dict(gate),
            "tool_files_sha256": inputs.tool_files_sha256,
        }
    )


# ---------------------------------------------------------------------------
# revision：发布、列举、加载
# ---------------------------------------------------------------------------


def _file_digests(files: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    return {name: {"path": DRAFT_FILES[name], "sha256": _sha256(files[name]), "bytes": len(files[name])} for name in DRAFT_FILES}


def _validate_receipt(receipt: Mapping[str, Any], *, campaign_id: str, revision: str) -> None:
    if (
        set(receipt) != DRAFT_RECEIPT_FIELDS
        or receipt.get("schema_version") != DRAFT_RECEIPT_SCHEMA
        or receipt.get("campaign_id") != campaign_id
        or receipt.get("revision") != revision
        or receipt.get("origin") not in {MACHINE_DRAFT, REVIEW_INPUT}
        or not isinstance(receipt.get("files"), Mapping)
        or set(receipt["files"]) != set(DRAFT_FILES)
        or not SHA256_RE.fullmatch(str(receipt.get("inputs_sha256", "")))
    ):
        raise ConfigurationError(f"revision {revision} 的收据不闭合或不属于本 Campaign。")


def list_revisions(campaign_dir: Path, campaign_id: str) -> list[tuple[str, dict[str, Any]]]:
    """按编号列出已发布的 revision 与收据（只读收据，不逐文件重算摘要）；草案目录里有非法条目即失败关闭。"""

    root = campaign_dir / DRAFT_ROOT
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"分类草案目录不可信：{root}")
    revisions: list[tuple[int, str, dict[str, Any]]] = []
    for child in root.iterdir():
        if child.name.startswith(DRAFT_TEMPORARY_PREFIX):
            continue
        match = REVISION_RE.fullmatch(child.name)
        if match is None or child.is_symlink() or not child.is_dir():
            raise ConfigurationError(f"分类草案目录含非法条目：{child.name}")
        receipt = _read_json(child / DRAFT_RECEIPT_FILENAME, f"revision {child.name} 收据")
        _validate_receipt(receipt, campaign_id=campaign_id, revision=child.name)
        revisions.append((int(match.group(1)), child.name, receipt))
    return [(name, receipt) for _number, name, receipt in sorted(revisions)]


def load_revision(campaign_dir: Path, campaign_id: str, revision: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """加载一个 revision：目录内容闭合、收据合法、六个文件逐字等于收据登记的摘要（只写一次，不得原地改）。"""

    if not REVISION_RE.fullmatch(str(revision)):
        raise ConfigurationError(f"revision 编号非法：{revision!r}（应为 rev-NNNN）")
    directory = campaign_dir / DRAFT_ROOT / revision
    if directory.is_symlink() or not directory.is_dir():
        raise ConfigurationError(f"revision 不存在：{revision}")
    names = sorted(child.name for child in directory.iterdir())
    if names != sorted([*DRAFT_FILES.values(), DRAFT_RECEIPT_FILENAME]):
        raise ConfigurationError(f"revision {revision} 目录内容不闭合：{names}")
    receipt = _read_json(directory / DRAFT_RECEIPT_FILENAME, f"revision {revision} 收据")
    _validate_receipt(receipt, campaign_id=campaign_id, revision=revision)
    files: dict[str, bytes] = {}
    for name, filename in DRAFT_FILES.items():
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"revision {revision} 的 {filename} 不是可信普通文件。")
        data = path.read_bytes()
        if {"path": filename, "sha256": _sha256(data), "bytes": len(data)} != receipt["files"][name]:
            raise ConfigurationError(
                f"revision {revision} 的 {filename} 已被改动（revision 只写一次）：修订请复制出去处置后走 --from-revision 与 --revision-input。"
            )
        files[name] = data
    return receipt, files


def _publish_revision(
    campaign_dir: Path,
    inputs: ClassificationInputs,
    files: Mapping[str, bytes],
    *,
    origin: str,
    parent: str | None,
) -> tuple[str, dict[str, Any], bool]:
    """发布 revision：同输入、同内容的既有 revision 直接复用；否则取下一个编号，临时目录写完整体改名。"""

    root = campaign_dir / DRAFT_ROOT
    ensure_private_directory(root, campaign_dir)
    _clear_temporaries(root, DRAFT_TEMPORARY_PREFIX)
    digests = _file_digests(files)
    revisions = list_revisions(campaign_dir, inputs.campaign_id)
    for name, receipt in revisions:
        if receipt["inputs_sha256"] == inputs.sha256 and receipt["files"] == digests:
            load_revision(campaign_dir, inputs.campaign_id, name)
            return name, receipt, True
    number = int(REVISION_RE.fullmatch(revisions[-1][0]).group(1)) + 1 if revisions else 1  # type: ignore[union-attr]
    revision = f"rev-{number:04d}"
    receipt = {
        "schema_version": DRAFT_RECEIPT_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "revision": revision,
        "parent_revision": parent,
        "origin": origin,
        "inputs": inputs.bindings,
        "inputs_sha256": inputs.sha256,
        "files": digests,
        "denominators": denominators(inputs),
        "created_at_utc": _orchestrator()._utc_now(),
    }
    content = {DRAFT_FILES[name]: files[name] for name in DRAFT_FILES}
    content[DRAFT_RECEIPT_FILENAME] = _serialize(receipt)
    _publish_directory(root, revision, DRAFT_TEMPORARY_PREFIX, content)
    return revision, receipt, False


def _read_revision_input(directory: Path) -> dict[str, bytes]:
    """审核人交回的目录：恰好六个草案文件（可另含 draft.json，忽略），都是可解析的 JSON 对象。"""

    path = directory if directory.is_absolute() else Path.cwd() / directory
    if path.is_symlink() or not path.is_dir():
        raise ConfigurationError(f"--revision-input 不是可信目录：{directory}")
    names = {child.name for child in path.iterdir()}
    expected = set(DRAFT_FILES.values())
    if not expected <= names or names - expected - {DRAFT_RECEIPT_FILENAME}:
        raise ConfigurationError(f"--revision-input 必须恰好包含六个草案文件 {sorted(expected)}（可另含 {DRAFT_RECEIPT_FILENAME}）。")
    files: dict[str, bytes] = {}
    for name, filename in DRAFT_FILES.items():
        file = path / filename
        if file.is_symlink() or not file.is_file():
            raise ConfigurationError(f"--revision-input 的 {filename} 不是可信普通文件。")
        files[name] = file.read_bytes()
        _parse_json_object(files[name], filename)
    return files


def _bound_candidates_path(campaign_dir: Path, receipt: Mapping[str, Any]) -> Path:
    binding = (receipt.get("inputs") or {}).get("candidates")
    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
        raise ConfigurationError("revision 收据缺少语义候选分母绑定。")
    return campaign_dir / str(binding["path"])


def _refuse_after_classification(campaign_dir: Path, inputs: ClassificationInputs | None = None) -> None:
    """已批准（或 Store 已记录分类事实）后不再生成草案：封存后变化须建立后继 Campaign 并从 VC-2 继续。"""

    stage = campaign_dir / STAGE_RESULT_RELATIVE
    if stage.exists() or stage.is_symlink() or (campaign_dir / APPROVED_ROOT).exists():
        raise ConfigurationError(
            "分类已批准：封存后迁移决定、RequiredRules 或断言归属的变化须建立后继 Campaign，从 VC-2 继续（指南 §4.2.4）。"
        )
    if inputs is not None and inputs.classification_fact is not None:
        raise ConfigurationError("Store 已记录规则分类事实而批准未收尾：重跑原批准命令收敛，不再生成草案。")


def _draft_summary(
    campaign_dir: Path,
    inputs: ClassificationInputs,
    revision: str,
    receipt: Mapping[str, Any],
    files: Mapping[str, bytes],
    *,
    reused: bool,
    mode: str,
) -> dict[str, Any]:
    gate = evaluate_revision(inputs, files)
    review = review_sha256(inputs, revision, receipt, gate)
    blocked = gate["status"] != "closed"
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "classification_blocked" if blocked else "awaiting_classification_approval",
        "mode": mode,
        "campaign_id": inputs.campaign_id,
        "revision": revision,
        "parent_revision": receipt["parent_revision"],
        "origin": receipt["origin"],
        "reused": reused,
        "draft_dir": str(campaign_dir / DRAFT_ROOT / revision),
        "inputs_sha256": inputs.sha256,
        "denominators": dict(receipt["denominators"]),
        "gate": gate,
        "review_sha256": review,
        "live_request_count": 0,
        "next_command": (
            "清零门禁 blocked：把该 revision 的六个文件复制到一个目录逐项处置，再执行 classify --campaign-dir "
            f"{campaign_dir} --from-revision {revision} --revision-input <目录> 生成新 revision"
            if blocked
            else f"classify --campaign-dir {campaign_dir} --approve-revision {revision} --approve-review-sha256 {review}"
        ),
    }


def _machine_draft(campaign_dir: Path, manifest: Mapping[str, Any], candidates: Path) -> dict[str, Any]:
    _refuse_after_classification(campaign_dir)
    path = claude_upgrade_seal._campaign_input_file(campaign_dir, candidates, "语义候选分母（--candidates）")
    assert path is not None
    inputs = collect_inputs(campaign_dir, manifest, path)
    _refuse_after_classification(campaign_dir, inputs)
    files = machine_draft_files(inputs)
    revision, receipt, reused = _publish_revision(campaign_dir, inputs, files, origin=MACHINE_DRAFT, parent=None)
    return _draft_summary(campaign_dir, inputs, revision, receipt, files, reused=reused, mode=MACHINE_DRAFT)


def _revise(campaign_dir: Path, manifest: Mapping[str, Any], parent: str, directory: Path) -> dict[str, Any]:
    _refuse_after_classification(campaign_dir)
    parent_receipt, _parent_files = load_revision(campaign_dir, str(manifest["campaign_id"]), parent)
    inputs = collect_inputs(campaign_dir, manifest, _bound_candidates_path(campaign_dir, parent_receipt))
    _refuse_after_classification(campaign_dir, inputs)
    if inputs.sha256 != parent_receipt["inputs_sha256"]:
        raise ConfigurationError(f"父 revision {parent} 绑定的输入已漂移：不能在旧输入上修订，先重新生成机器草案（--candidates）。")
    files = _read_revision_input(directory)
    revision, receipt, reused = _publish_revision(campaign_dir, inputs, files, origin=REVIEW_INPUT, parent=parent)
    return _draft_summary(campaign_dir, inputs, revision, receipt, files, reused=reused, mode=REVIEW_INPUT)


# ---------------------------------------------------------------------------
# 批准
# ---------------------------------------------------------------------------


def _publish_approved(campaign_dir: Path, receipt: Mapping[str, Any], files: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    """``classification/approved/``：被批准 revision 六个文件的逐字副本；已存在时逐个核对，不一致即拒绝。"""

    bindings = {
        name: {
            "path": (APPROVED_ROOT / filename).as_posix(),
            "sha256": receipt["files"][name]["sha256"],
            "bytes": receipt["files"][name]["bytes"],
        }
        for name, filename in DRAFT_FILES.items()
    }
    approved = campaign_dir / APPROVED_ROOT
    parent = campaign_dir / CLASSIFICATION_ROOT
    _clear_temporaries(parent, APPROVED_TEMPORARY_PREFIX)
    if approved.exists() or approved.is_symlink():
        if approved.is_symlink() or not approved.is_dir() or sorted(child.name for child in approved.iterdir()) != sorted(DRAFT_FILES.values()):
            raise ConfigurationError("批准目录已存在但内容不闭合：拒绝覆盖。")
        for name, filename in DRAFT_FILES.items():
            path = approved / filename
            if path.is_symlink() or not path.is_file() or file_sha256(path) != bindings[name]["sha256"]:
                raise ConfigurationError(f"批准目录已存在，但 {filename} 与被批准 revision 不一致：拒绝覆盖。")
        return bindings
    ensure_private_directory(parent, campaign_dir)
    _publish_directory(parent, APPROVED_ROOT.name, APPROVED_TEMPORARY_PREFIX, {DRAFT_FILES[name]: files[name] for name in DRAFT_FILES})
    return bindings


def _approve(campaign_dir: Path, manifest: Mapping[str, Any], revision: str, approved_review: str) -> dict[str, Any]:
    orchestrator = _orchestrator()
    if not SHA256_RE.fullmatch(approved_review):
        raise ConfigurationError("--approve-review-sha256 必须是 64 位小写十六进制。")
    campaign_id = str(manifest["campaign_id"])
    stage_path = campaign_dir / STAGE_RESULT_RELATIVE
    if stage_path.exists() or stage_path.is_symlink():
        existing = _read_json(stage_path, "分类阶段结果")
        if existing.get("revision") != revision:
            raise ConfigurationError(
                f"分类已批准 revision {existing.get('revision')}：禁止以 {revision} 覆盖；封存后变化须建立后继 Campaign。"
            )
    receipt, files = load_revision(campaign_dir, campaign_id, revision)
    inputs = collect_inputs(campaign_dir, manifest, _bound_candidates_path(campaign_dir, receipt))
    if inputs.sha256 != receipt["inputs_sha256"]:
        raise ConfigurationError(f"revision {revision} 绑定的输入已漂移，禁止批准：重新生成机器草案并审核。")
    gate = evaluate_revision(inputs, files)
    current_review = review_sha256(inputs, revision, receipt, gate)
    if approved_review != current_review:
        raise ConfigurationError(
            "批准摘要与该 revision 当前的 review_sha256 不一致（revision、工具身份或门禁结论可能已变化）：先不带批准参数重跑草案调用复核。"
        )
    if gate["status"] != "closed":
        raise ConfigurationError("清零门禁 blocked，禁止批准：" + "、".join(reason["code"] for reason in gate["reasons"]))
    orchestrator._require_tool_evolution_registered(
        campaign_dir, manifest, current=orchestrator._tool_identity(include_git=False), action="classify 批准"
    )
    documents = {name: _parse_json_object(files[name], DRAFT_FILES[name]) for name in DRAFT_FILES}
    projected = store_payloads(inputs, documents)
    payload = {
        "evidence_fact_ref": dict(inputs.evidence_fact_ref),
        "evidence_package_ref": dict(inputs.evidence_package_ref),
        "rule_migration_ledger_ref": dict(projected["rule_migration_ref"]),
        "atomic_assertion_ledger_ref": dict(projected["atomic_ref"]),
    }
    if inputs.classification_fact is not None and inputs.classification_fact["payload"] != payload:
        raise ConfigurationError("Store 已记录另一份规则分类事实：同一 Campaign 不得覆盖，封存后变化须建立后继 Campaign。")
    session = claude_upgrade_seal._StoreSession(inputs.store_root, inputs.store_campaign_id, campaign_dir / STORE_SCRATCH_RELATIVE)
    if session.seal_object("rule_migration_ledger", projected["rule_migration"]) != projected["rule_migration_ref"]:
        raise ConfigurationError("Store 封存 RuleMigrationLedger 得到的引用与投影不一致。")
    if session.seal_object("atomic_assertion_ledger", projected["atomic"]) != projected["atomic_ref"]:
        raise ConfigurationError("Store 封存 AtomicAssertionLedger 得到的引用与投影不一致。")
    fact = session.record_fact(
        "rule-classification-record",
        "rule_classification_recorded",
        payload,
        issued_at=str(receipt["created_at_utc"]),
        logical_action=APPROVE_LOGICAL_ACTION,
    )
    status = session.status()
    if status.get("checkpoint") != STORE_CHECKPOINT:
        raise ConfigurationError(f"规则分类后 Store 检查点应保持 {STORE_CHECKPOINT}，实际 {status.get('checkpoint')}。")
    approved = _publish_approved(campaign_dir, receipt, files)
    target = rule_manifest_view(documents["target_rules"], expected_version=inputs.target_version, label="目标 RequiredRules 清单")
    inherited = list(documents["rule_migration"]["inherited_rules"])
    draft_receipt = campaign_dir / DRAFT_ROOT / revision / DRAFT_RECEIPT_FILENAME
    stage_payload = {
        "schema_version": STAGE_RESULT_SCHEMA,
        "client": "claude",
        "campaign_id": campaign_id,
        "stage": "classify",
        "status": "complete",
        "revision": revision,
        "review_sha256": current_review,
        "draft_receipt": {"path": draft_receipt.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(draft_receipt)},
        "approved": approved,
        "inputs_sha256": inputs.sha256,
        "store": {
            "root": claude_upgrade_store.CAMPAIGN_STORE_DIRNAME,
            "campaign_id": inputs.store_campaign_id,
            "checkpoint": status["checkpoint"],
            "evidence_fact_ref": dict(inputs.evidence_fact_ref),
            "evidence_package_ref": dict(inputs.evidence_package_ref),
            "rule_migration_ledger_ref": dict(projected["rule_migration_ref"]),
            "atomic_assertion_ledger_ref": dict(projected["atomic_ref"]),
            "classification_fact_ref": dict(fact["fact_ref"]),
        },
        "rule_migration_identity_sha256": projected["rule_migration"]["identity_sha256"],
        "atomic_assertion_identity_sha256": projected["atomic"]["identity_sha256"],
        "baseline_rules": sorted(inputs.baseline["rules"]),
        "target_rules": list(target["rules"]),
        "affected_rules": list(documents["rule_migration"]["affected_rules"]),
        "inherited_rules": inherited,
        "denominators": dict(receipt["denominators"]),
        "counts": gate["counts"],
        "execute_item_ids": [],
        "reuse_item_ids": inherited,
        "live_request_count": 0,
        "scanned_bytes": 0,
        "approved_at_utc": orchestrator._utc_now(),
    }
    stage = claude_upgrade_seal._write_once_or_reuse(stage_path, stage_payload, ignore=("approved_at_utc",))
    checkpoint = claude_upgrade_vc.complete_campaign_phase(
        campaign_dir,
        manifest,
        phase="VC-2",
        stage_receipt_path=stage_path,
        completed_at_utc=str(stage["approved_at_utc"]),
        execute_item_ids=[],
        reuse_item_ids=inherited,
        live_request_count=0,
        scanned_bytes=0,
    )
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "complete",
        "mode": "approve",
        "campaign_id": campaign_id,
        "revision": revision,
        "review_sha256": current_review,
        "store": {**stage["store"], "classification_fact_reused": fact["reused"]},
        "stage_result": str(stage_path),
        "vc2_checkpoint": str(claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-2")),
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "affected_rules": stage["affected_rules"],
        "inherited_rules": stage["inherited_rules"],
        "denominators": stage["denominators"],
        "live_request_count": 0,
    }


# ---------------------------------------------------------------------------
# 读侧（交给 B3-01／B3-03）
# ---------------------------------------------------------------------------


def load_stage_result(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """读取并核对分类阶段结果：合同、Campaign、状态，批准副本与 revision 收据的摘要。"""

    stage = _read_json(campaign_dir / STAGE_RESULT_RELATIVE, "分类阶段结果")
    if (
        stage.get("schema_version") != STAGE_RESULT_SCHEMA
        or stage.get("client") != "claude"
        or stage.get("campaign_id") != manifest.get("campaign_id")
        or stage.get("stage") != "classify"
        or stage.get("status") != "complete"
    ):
        raise ConfigurationError("分类阶段结果的合同、Campaign 或状态不符。")
    approved = stage.get("approved")
    if not isinstance(approved, Mapping) or set(approved) != set(DRAFT_FILES):
        raise ConfigurationError("分类阶段结果缺少六个批准文件的绑定。")
    for name, binding in approved.items():
        path = campaign_dir / str(binding.get("path", ""))
        if path.is_symlink() or not path.is_file() or file_sha256(path) != binding.get("sha256") or path.stat().st_size != binding.get("bytes"):
            raise ConfigurationError(f"批准文件 {name} 缺失或摘要漂移。")
    receipt = stage.get("draft_receipt")
    path = campaign_dir / str((receipt or {}).get("path", ""))
    if path.is_symlink() or not path.is_file() or file_sha256(path) != (receipt or {}).get("sha256"):
        raise ConfigurationError("被批准 revision 的收据缺失或摘要漂移。")
    return stage


def approved_rules(campaign_dir: Path, manifest: Mapping[str, Any], *, require_approved: bool) -> tuple[str, ...]:
    """服务面 ``_approved_rules`` 的 Claude 实现：批准后取目标规则清单顺序，否则取基线规则（总计划 ``required_rules``）。"""

    if not require_approved:
        return tuple(str(rule) for rule in manifest["required_rules"])
    stage = load_stage_result(campaign_dir, manifest)
    payload = _read_json(campaign_dir / stage["approved"]["target_rules"]["path"], "批准的目标规则清单")
    view = rule_manifest_view(payload, expected_version=str(manifest["target_version"]), label="批准的目标规则清单")
    if list(view["rules"]) != list(stage["target_rules"]):
        raise ConfigurationError("批准的目标规则清单与分类阶段结果登记的目标规则不一致。")
    return tuple(view["rules"])
