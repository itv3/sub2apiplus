"""Claude VC-3 画像 producer（Claude 升级控制面设计 B3-03 及其补充项）：``prepare-profile`` 与 ``stage-profile`` 的控制流程。

VC-3 在 VC-1／VC-2 的同一个受管 Store 里继续（「承接原 Store」），不像历史聚合入口 ``claude_fw_f_profile.py`` 那样另建
Store。全程离线、零请求，不改 Active／Rollback、生产 selector 与仓库里的生产 Catalog（指南 §4.3：暂存 Release／Catalog
不入库，纳入候选源码树属于 VC-4，生产注册属于 VC-6）。

一、两个子命令、四种调用
========================
* ``prepare-profile``——目标画像派生与 EvidenceApprovalFact：

  - **机器草案**（``--active-release-catalog`` + ``--active-profile``）：在 ``profile/prepare/draft/rev-NNNN/`` 写只写一次的
    revision，含五个文件与收据 ``draft.json``：``profile-policy.json``（阶段专用 Profile policy，由输入派生、只读）、
    ``rule-patches.json``（受影响规则的画像补丁，审核人逐条填）、``model-capability-patch.json``（已批准的模型能力差异，
    可为空）、``joint-inputs.json``（联合制品里无法由输入推出的事实：生产入口与出站清单、目标处置、支持范围、边界断言、
    Persona 派生、兼容边界与部署计划；能推出的部分预填）、``wire-projection-policy.json``（Wire 投影策略，取 Active
    版本的那一份、只换版本号，见 ``claude_upgrade_release``）。补丁一律为空、联合输入只预填可推出的部分，门禁因此 blocked。
  - **修订**（``--from-revision`` + ``--revision-input``）：审核人把五个文件复制出去处置后整目录交回，按字节写成新 revision；
    同内容重跑复用，父 revision 的输入漂移即拒绝。
  - **批准**（``--approve-revision`` + ``--approve-review-sha256`` + ``--approved-by``）：核对 revision 未被改动、输入未漂移、
    门禁 closed、审核摘要一致后，确保原 Store 有 ``evidence_approved``（v3：同时引用原 ``evidence_recorded``、未改写的
    EvidencePackage v3 与同一 Campaign 的 ``rule_classification_recorded``）——第一次准备批准时签发，Store 检查点到
    ``official_sealed``；它批准的是 VC-1 证据与 VC-2 分类的组合，与画像 revision 无关，之后再批准新的准备 revision 时沿用。
    再写 ``profile/prepare/approved/<rev>/``（五个批准文件、Active 副本、目标画像与派生收据、模型能力目录、目标 Wire 与
    Release 条目）与 ``profile/prepare/results/<rev>.json``。
  - 指南 §4.3「批准前只新建 draft revision」：这里的「批准」指 ProfileApprovalFact。准备批准之后、``stage-profile`` 批准之前，
    审核人仍可继续起草、修订并批准新的准备 revision，``stage-profile`` 一律采用**最新**批准的一份；ProfileApprovalFact 之后
    起草案、批准新 revision 都拒绝（批准后变化须建立后继 Campaign，§4.3.3）。
* ``stage-profile``——联合制品与 ProfileApprovalFact：

  - **预览**（只给 ``--campaign-dir``）：从最新批准的准备包确定性构造联合制品——PersonaDescriptor、ProfileSchema、Snapshot、
    ReleaseBundle（document 绑定 Wire、模型能力目录与 Release 的摘要）、ReleaseArtifact、ModelCapabilityCatalog、入口与出站的
    Observation／Inventory、边界断言、SupportEnvelope、PersonaDerivation、CompatibilityBoundary、ScenarioPlan、
    DeploymentPlan 共 17 个对象——按 Store 的内容寻址算法预先算出引用，写
    ``profile/stage/<代号>/joint-manifest.json`` 与 ``stage-preview.json``，给出联合摘要 ``joint_sha256``（退出码 2）。
  - **批准**（``--approve-joint-sha256`` + ``--approved-by``）：经 ``official_client_control`` CLI 以 ``artifact-seal
    --allow-existing`` 依依赖顺序封存全部联合制品，再 ``profile-approve`` 追加 ``profile_approved``（绑定联合引用、目标
    规则全集与入口／出站目标处置，``approval_purpose`` 取 Campaign 用途），Store 检查点到 ``profile_approved``；随后写
    暂存 Release ``profile/release/``（布局镜像仓库 catalogdata，见 ``claude_upgrade_release``）、阶段结果
    ``profile/result.json``（绑定暂存 Release 收据）与 VC-3 checkpoint（执行项为空、复用项为 ``inherited_rules``、零请求、
    零扫描字节）。

二、输入
========
* VC-2：``claude_upgrade_classify.load_stage_result`` 读 ``classification/result.json`` 与六个批准副本，VC-2 checkpoint
  逐级重放；Store 里唯一的 ``rule_classification_recorded`` 必须与阶段结果登记的引用逐字相同。
* Store：Campaign v2、有效工具身份等于当前受管代码；准备阶段要求检查点是 ``evidence_recorded`` 或 ``official_sealed``
  （已批准 revision 的重放还可以是 ``profile_approved``），暂存阶段要求 ``official_sealed``（批准重入时可为 ``profile_approved``）。
* 当前 Active：VC-0 应冻结 Active／Rollback 身份；Claude 的 VC-0 收口（B2-11）还没落地，这里取总计划
  ``inputs.active_release_catalog``／``inputs.active_profile`` 的冻结副本，没有时取命令行给出的 Campaign 内文件——Release
  目录 ``official-egress-claude-release-catalog/v1`` 的 ``production_active`` 选择器指向的 Release，其画像摘要必须等于给出
  的画像文件。两者的绑定写进 Profile policy，后续每次调用逐字重核。
* Wire 投影（B3-03 补充项）：VC-1 封存的 capture manifest、CaptureIndex 与 EvidencePackage 外部绑定（``claude_upgrade_release.
  load_release_evidence``），受管树里 Active 版本的 Wire 投影策略 ``claude_upgrade_wire_projection_<版本>.json``（没有即拒绝；
  绑定写进 Profile policy），VC-2 批准的目标规则清单副本摘要与 Store 原子断言台账按 ``owner_kind`` 的计数。

三、目标画像派生与 ``profile_diff_paths`` 约束（指南 §4.3.1）
=============================================================
``target = active + 版本身份替换 + 模型能力目录派生的 identity + rule_patches[affected_rules] + 已批准模型能力补丁``：

* **版本身份替换**只做精确值替换：Active 的版本、平台与二进制摘要（取自规则适用条件里唯一的 ``binary_sha256=``），以及
  ``version=``／``platform=``／``binary_sha256=`` 三种条件串，替换成 Campaign 的目标版本、平台与官方二进制摘要；证据路径与
  说明文字不做子串替换。替换落点即 ``version_identity_paths``。
* **规则补丁**：每条受影响规则至少一个操作（``add``／``remove``／``replace``），路径必须落在 VC-2 ``profile-patches.json``
  为该规则登记的 ``profile_paths`` 之内；不得触碰继承规则的画像条目、不得与版本身份落点相同、不同 owner 的操作不得重叠。
* **模型能力目录派生**（B3-03 补充项）：按批准的 Wire 投影策略从 VC-1 证据投影出模型能力目录，画像 ``identity`` 的
  ``supported_models``、``model_alias_policy``、``unknown_model_policy``、``model_capability_catalog_sha256`` 随目录派生，
  落点即 ``model_identity_paths``；补丁不得再碰这四个落点。
* **模型能力补丁**只允许落在 ``MODEL_CAPABILITY_PATHS``（模型闭集、别名与未知模型策略、能力目录摘要、模型特有的
  ``fallbacks`` 字段）之内；投影成功时实际只剩 ``fallbacks`` 的两个落点可改。
* 路径语法：JSON Pointer（``~0``／``~1`` 转义）；对象数组若有唯一键字段（``KEY_FIELDS`` 按序取第一个），段写成
  ``<字段>=<值>``，例如 ``/rules/spec_id=SPEC-BODY-001/title``。
* 复算：``profile_diff_paths = diff(active, target)``，每条差异都必须落在版本身份落点、模型身份落点或某个补丁操作的路径之内（或是其
  结构上级，例如键控列表的增删），每个补丁操作都必须产生差异；目标画像的规则全集必须等于 VC-2 目标规则，严格端点与
  ``feature_matrix`` 不得再引用目标规则之外的 SPEC。``profile_diff_paths`` 写进派生收据，批准后随准备包冻结。

四、联合制品与门禁（指南 §4.3.2，对齐 Store ``profile_approved`` 写入门禁）
======================================================================
联合制品全部由目标画像、VC-2 结果、Campaign 身份与 ``joint-inputs.json`` 确定性投影，草案阶段即按 Store 合同逐个
校验并预算引用，同时复核 Store 写入门禁会查的跨对象一致性：入口别名与调用方闭合、目标处置覆盖完整 Inventory、
SupportEnvelope 只覆盖 ``migrated_strict`` 入口、DeploymentPlan 的 Active 范围等于 SupportEnvelope、ScenarioPlan 精确覆盖
目标规则全集与 ``migrated_strict`` 协议类别、目标画像的每个严格端点都以 ``persona_strict`` 绑定同一组 SPEC、
``production_replacement`` 用途下目标规则的证据等级必须是 ``verified``。

五、幂等与崩溃收敛
==================
运行在 Campaign 锁内并要求 Campaign 静止（与 seal、classify 同口径：active 租约拒绝、停线租约视为静止，未隔离的
环境污染拒绝；B3-01 对齐）。revision、准备包与暂存代号目录都先写同级临时目录再整体改名发布，准备结果与阶段结果
只写一次。Store 写入经 ``claude_upgrade_seal._StoreSession``：对象 ``--allow-existing``，事实带
``fact_idempotency_key``（逻辑动作、payload 摘要、事实种类），父监督器上下文里带 ``--producer-run``；两条事实的
时刻都取冻结值（第一次被批准的 revision 的创建时刻、联合清单的生成时刻），任一步被强杀后重跑同一命令逐步复用、
事实数不增。EvidenceApprovalFact 每个 Campaign 只有一份：已存在时只核对它绑定的证据与分类引用，
不再追加（批准人与审核引用以第一次为准）。准备包已发布而结果未写时被强杀，重跑同一批准命令逐文件核对后补写结果；
换一个 revision 批准则遗留的准备包不被采用（暂存只看 ``results/`` 里最新的一份）。ProfileApprovalFact 只批准一次：以不同
批准人或不同联合摘要重复批准一律拒绝。

六、边界与已知缺口
==================
* Wire 与模型能力目录（B3-03 补充项）：投影工具 ``tools/generate_claude_fw_g_profile.py`` 拆出了版本专用的 Wire 投影策略，
  VC-1 布局的证据来源与装配在 ``claude_upgrade_release``。VC-1 现行场景清单只用主模型取证，非主模型（``<模型>-<probe>``）
  没有证据：投影策略登记多个模型时投影失败关闭，审核人只能先把模型闭集收到主模型（画像 ``supported_models`` 随之派生），
  或等 VC-1 补模型能力取证作业。
* 生产入口清单、出站处置清单的现状没有 VC-1 生产者（B2-07 补充项），这里由审核人在 ``joint-inputs.json`` 提供，按 Store
  合同与上面的门禁核对。
* B3-01（已落实，见 ``claude_upgrade_batch``）：``_load_stage_result`` 的 ``stage-profile`` 分支、VC-3 阶段重派证明；计时账本
  事件由共享原子入口登记。批次动作即 ``claude_upgrade.py stage-profile --campaign-dir <C> --approve-joint-sha256 <sha>
  --approved-by <人>``（参数全是成对旗标）。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层，不在 wire 与
evidence 闭包里。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_classify as classify
from tools.official_client_capture import claude_upgrade_evidence
from tools.official_client_capture import claude_upgrade_release as release
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256
from tools.official_client_control.canonical import canonical_sha256, expect_rfc3339
from tools.official_client_control.contracts import (
    APPROVAL_PURPOSES,
    EGRESS_DISPOSITIONS,
    EGRESS_GUARD_STATES,
    INGRESS_DISPOSITIONS,
    capability_key,
    profile_approval_identity_sha256,
    validate_fact_payload,
)
from tools.official_client_control.errors import ControlError

# ---------------------------------------------------------------------------
# 坐标、合同名与词表
# ---------------------------------------------------------------------------

PROFILE_ROOT = Path("profile")
PREPARE_ROOT = PROFILE_ROOT / "prepare"
PREPARE_DRAFT_ROOT = PREPARE_ROOT / "draft"
# 准备批准的产物按 revision 分开：``approved/<rev>/`` 是准备包，``results/<rev>.json`` 是准备结果；暂存阶段取最新的一份。
PREPARED_ROOT = PREPARE_ROOT / "approved"
PREPARE_RESULTS_ROOT = PREPARE_ROOT / "results"
STAGE_ROOT = PROFILE_ROOT / "stage"
STAGE_RESULT_RELATIVE = PROFILE_ROOT / "result.json"
STORE_SCRATCH_RELATIVE = PROFILE_ROOT / "store-inputs"
DRAFT_RECEIPT_FILENAME = "draft.json"
JOINT_MANIFEST_FILENAME = "joint-manifest.json"
STAGE_PREVIEW_FILENAME = "stage-preview.json"
DRAFT_TEMPORARY_PREFIX = ".tmp-rev-"
PREPARED_TEMPORARY_PREFIX = ".tmp-prepared-"
STAGE_TEMPORARY_PREFIX = ".tmp-stage-"

# 准备阶段 revision 的五个文件：名字 → 文件名（第五个是 B3-03 补充项的 Wire 投影策略）。
PREPARE_FILES: Mapping[str, str] = {
    "policy": "profile-policy.json",
    "rule_patches": "rule-patches.json",
    "model_patch": "model-capability-patch.json",
    "joint_inputs": "joint-inputs.json",
    "projection": "wire-projection-policy.json",
}
# 准备包另含的派生与副本文件（模型能力目录、目标 Wire 与 Release 条目都由批准的 revision 确定性投影）。
PREPARED_EXTRA_FILES: Mapping[str, str] = {
    "active_release_catalog": "active-release-catalog.json",
    "active_profile": "active-profile.json",
    "target_profile": "target-profile.json",
    "derivation": "profile-derivation.json",
    "model_catalog": "model-capability-catalog.json",
    "target_wire": "target-wire.json",
    "release_entry": "release-entry.json",
}

COMMAND_RESULT_SCHEMA = "claude-upgrade-profile-result/v1"
PROFILE_POLICY_SCHEMA = "claude-upgrade-profile-policy/v1"
RULE_PATCHES_SCHEMA = "claude-upgrade-profile-rule-patches/v1"
MODEL_PATCH_SCHEMA = "claude-upgrade-model-capability-patch/v1"
JOINT_INPUTS_SCHEMA = "claude-upgrade-profile-joint-inputs/v1"
DERIVATION_SCHEMA = "claude-upgrade-profile-derivation/v1"
DRAFT_RECEIPT_SCHEMA = "claude-upgrade-profile-draft/v1"
REVIEW_SCHEMA = "claude-upgrade-profile-review/v1"
PREPARE_RESULT_SCHEMA = "claude-upgrade-profile-prepare-result/v1"
JOINT_MANIFEST_SCHEMA = "claude-upgrade-profile-joint-manifest/v1"
STAGE_PREVIEW_SCHEMA = "claude-upgrade-profile-stage-preview/v1"
STAGE_RESULT_SCHEMA = "claude-upgrade-profile-stage-result/v1"
RELEASE_CATALOG_SCHEMA = "official-egress-claude-release-catalog/v1"
EVIDENCE_APPROVE_ACTION = "claude-upgrade:prepare-profile:evidence-approve"
PROFILE_APPROVE_ACTION = "claude-upgrade:stage-profile:profile-approve"
MACHINE_DRAFT = "machine_draft"
REVIEW_INPUT = "review_input"
PLACEHOLDER_RATIONALE = "草案占位：按目标证据写明补丁理由。"

SHA256_RE = classify.SHA256_RE
SAFE_ID_RE = classify.SAFE_ID_RE
SPEC_ID_RE = classify.SPEC_ID_RE
REVISION_RE = classify.REVISION_RE
STORE_EVIDENCE = "evidence_recorded"
STORE_SEALED = "official_sealed"
STORE_APPROVED = "profile_approved"

# 对象数组的键字段（按序取第一个在全部元素上存在且唯一的字符串字段）。
KEY_FIELDS = ("spec_id", "binding_id", "egress_id", "endpoint_id", "alias_id", "logical_ingress_id", "name", "scenario", "path", "id")
PATCH_OPS = frozenset({"add", "remove", "replace"})
# 模型能力补丁允许的落点（指南 §4.2.3：模型名、精确别名、未知模型策略、能力目录摘要、模型特有的 fallbacks）。
MODEL_CAPABILITY_PATHS = (
    "/body/field_types/fallbacks",
    "/body/optional_fields",
    "/identity/model_alias_policy",
    "/identity/model_capability_catalog_sha256",
    "/identity/supported_models",
    "/identity/unknown_model_policy",
)
# 版本身份条件串的前缀（规则适用条件里的 ``<键>=<值>``）。
IDENTITY_CONDITION_KEYS = ("version", "platform", "binary_sha256")

JOINT_TOP_FIELDS = frozenset(
    {
        "schema_version", "campaign_id", "target_version", "persona_descriptor", "ingress", "egress", "support",
        "boundary_assertions", "persona_derivation", "compatibility_boundary", "deployment",
    }
)
JOINT_INGRESS_FIELDS = frozenset({"observed_at_utc", "aliases", "entries", "target_dispositions"})
JOINT_EGRESS_FIELDS = frozenset({"observed_at_utc", "egresses", "entries", "target_dispositions"})
JOINT_DEPLOYMENT_FIELDS = frozenset(
    {"rollback_operational_capabilities", "deployment_traffic_capabilities", "rollback_target_kind", "failure_policy"}
)
ALIAS_FIELDS = frozenset({"alias_id", "logical_ingress_id", "physical_route", "caller_ids"})
INGRESS_ENTRY_FIELDS = frozenset(
    {"logical_ingress_id", "physical_alias_ids", "caller_ids", "adapter_id", "route_id", "ingress_kind", "protocol_class", "current_disposition"}
)
EGRESS_FIELDS = frozenset({"egress_id", "route_id", "sink_id", "oauth_related", "kind"})
EGRESS_ENTRY_FIELDS = frozenset({"egress_id", "current_disposition", "current_guard_state", "spec_ids", "managed_policy"})
EGRESS_TARGET_FIELDS = frozenset({"egress_id", "target_disposition", "spec_ids", "managed_policy"})
POLICY_FIELDS = frozenset(
    {
        "schema_version", "campaign_id", "baseline_version", "target_version", "campaign_purpose", "target_identity", "active",
        "classification", "rules", "identity_substitutions", "model_capability_paths", "generator",
    }
)
DRAFT_RECEIPT_FIELDS = frozenset(
    {"schema_version", "campaign_id", "revision", "parent_revision", "origin", "inputs", "inputs_sha256", "files", "created_at_utc"}
)


class PatchError(ValueError):
    """画像补丁路径不存在、类型不符或操作不合法。"""


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _capture() -> Any:
    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture


_read_json = classify._read_json
_parse_json_object = classify._parse_json_object
_serialize = classify._serialize
_sha256 = classify._sha256
_text = classify._text
_sorted_unique_strings = classify._sorted_unique_strings
_Problems = classify._Problems


def profile_bytes(document: Mapping[str, Any]) -> bytes:
    """Claude 画像文件的内容寻址字节（与后端 Catalog 同一规范：键排序、紧凑分隔、末尾换行），摘要即文件名。"""

    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def add_prepare_arguments(target: argparse.ArgumentParser) -> None:
    """``prepare-profile`` 的参数（``--campaign-dir`` 由编排器统一添加）。"""

    target.add_argument(
        "--active-release-catalog",
        type=Path,
        help=f"机器草案：当前 Active 的 Release 目录（{RELEASE_CATALOG_SCHEMA}，Campaign 内文件；总计划已冻结时不给）。",
    )
    target.add_argument("--active-profile", type=Path, help="机器草案：production_active Release 的画像文件（Campaign 内文件）。")
    target.add_argument("--from-revision", help="修订：父 revision 编号（rev-NNNN）。")
    target.add_argument("--revision-input", type=Path, help="修订：审核人处置后的四个草案文件所在目录（可另含 draft.json，忽略）。")
    target.add_argument("--approve-revision", help="批准：要批准的 revision 编号。")
    target.add_argument("--approve-review-sha256", help="批准：草案调用为该 revision 输出的 review_sha256。")
    target.add_argument("--approved-by", help="批准：批准人（写进 EvidenceApprovalFact 的 reviewer）。")


def add_stage_arguments(target: argparse.ArgumentParser) -> None:
    """``stage-profile`` 的参数（``--campaign-dir`` 由编排器统一添加）。"""

    target.add_argument("--approve-joint-sha256", help="批准：预览输出的联合摘要 joint_sha256。")
    target.add_argument("--approved-by", help="批准：批准人（写进 ProfileApprovalFact 的 reviewer）。")


def _locked(campaign_dir_value: Any, action: Callable[[Path, dict[str, Any]], tuple[dict[str, Any], int]]) -> tuple[dict[str, Any], int]:
    """Campaign 锁内执行：要求 Formal、Campaign 静止（与 seal、classify 同一口径）、没有未隔离的环境污染。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir_value)
    orchestrator._validate_existing_campaign_path(campaign_dir)
    campaign_dir = campaign_dir.resolve(strict=True)
    orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        # B3-01 对齐 B2-08 的恢复协议：active 租约拒绝、停线租约视为静止；记录过环境污染时按对账同一判据核对，
        # 已被隔离收据覆盖的污染不再封锁。
        orchestrator._require_quiescent_campaign_lease(campaign_dir, action="VC-3 画像")
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        if (campaign_dir / _capture().CONTAMINATION_MARKER).exists():
            orchestrator._reject_contaminated_campaign(campaign_dir)
        return action(campaign_dir, manifest)


def prepare_profile_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``prepare-profile``：草案与修订退出码 2；批准完成为 0。"""

    names = ("active_release_catalog", "active_profile", "from_revision", "revision_input", "approve_revision", "approve_review_sha256", "approved_by")
    given = {name: getattr(arguments, name, None) for name in names}
    if given["approve_revision"] is not None or given["approve_review_sha256"] is not None:
        if given["approve_revision"] is None or given["approve_review_sha256"] is None or not given["approved_by"]:
            raise ConfigurationError("批准必须同时给出 --approve-revision、--approve-review-sha256 与 --approved-by。")
        if any(given[name] is not None for name in ("active_release_catalog", "active_profile", "from_revision", "revision_input")):
            raise ConfigurationError("批准不接受 Active 输入或修订参数：输入一律取被批准 revision 的绑定。")
        return _locked(
            arguments.campaign_dir,
            lambda campaign, manifest: (
                _approve_prepare(campaign, manifest, str(given["approve_revision"]), str(given["approve_review_sha256"]), str(given["approved_by"])),
                0,
            ),
        )
    if given["approved_by"] is not None:
        raise ConfigurationError("--approved-by 只用于批准。")
    if given["from_revision"] is not None or given["revision_input"] is not None:
        if given["from_revision"] is None or given["revision_input"] is None:
            raise ConfigurationError("修订必须同时给出 --from-revision 与 --revision-input。")
        if given["active_release_catalog"] is not None or given["active_profile"] is not None:
            raise ConfigurationError("修订沿用父 revision 的 Active 绑定，不接受 --active-release-catalog／--active-profile。")
        return _locked(
            arguments.campaign_dir,
            lambda campaign, manifest: (_revise(campaign, manifest, str(given["from_revision"]), Path(given["revision_input"])), 2),
        )
    return _locked(
        arguments.campaign_dir,
        lambda campaign, manifest: (_machine_draft(campaign, manifest, given["active_release_catalog"], given["active_profile"]), 2),
    )


def stage_profile_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``stage-profile``：预览退出码 2；批准完成为 0。"""

    joint = getattr(arguments, "approve_joint_sha256", None)
    approved_by = getattr(arguments, "approved_by", None)
    if joint is None and approved_by is None:
        return _locked(arguments.campaign_dir, lambda campaign, manifest: (_stage_preview(campaign, manifest), 2))
    if joint is None or not approved_by:
        raise ConfigurationError("批准必须同时给出 --approve-joint-sha256 与 --approved-by。")
    return _locked(arguments.campaign_dir, lambda campaign, manifest: (_stage_approve(campaign, manifest, str(joint), str(approved_by)), 0))


# ---------------------------------------------------------------------------
# JSON Pointer（键控段）、差异与补丁
# ---------------------------------------------------------------------------


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _unescape(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def join_pointer(prefix: str, segment: str) -> str:
    return f"{prefix}/{_escape(segment)}"


def pointer_parts(pointer: Any) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise PatchError(f"不是 JSON Pointer：{pointer!r}")
    return [] if pointer == "" else [_unescape(part) for part in pointer[1:].split("/")]


def list_key_field(values: Any) -> str | None:
    """对象数组的键字段：``KEY_FIELDS`` 里第一个在全部元素上是唯一非空字符串的字段；否则 None（按下标寻址）。"""

    if not isinstance(values, list) or not values or not all(isinstance(item, dict) for item in values):
        return None
    for field in KEY_FIELDS:
        keys = [item.get(field) for item in values]
        if all(isinstance(key, str) and key for key in keys) and len(set(keys)) == len(keys):
            return field
    return None


def _keyed(segment: str) -> tuple[str, str] | None:
    field, separator, value = segment.partition("=")
    if separator and field in KEY_FIELDS and value:
        return field, value
    return None


def within(path: str, scope: str) -> bool:
    """``path`` 等于 ``scope`` 或在它之下。"""

    return path == scope or path.startswith(scope + "/") or scope == ""


def _child(container: Any, segment: str, pointer: str) -> Any:
    if isinstance(container, dict):
        if segment not in container:
            raise PatchError(f"路径不存在：{pointer}")
        return container[segment]
    if isinstance(container, list):
        field = list_key_field(container)
        keyed = _keyed(segment)
        if field is not None:
            if keyed is None or keyed[0] != field:
                raise PatchError(f"路径必须按键字段 {field} 寻址：{pointer}")
            matches = [item for item in container if item.get(field) == keyed[1]]
            if len(matches) != 1:
                raise PatchError(f"路径不存在：{pointer}")
            return matches[0]
        if not segment.isdigit() or int(segment) >= len(container):
            raise PatchError(f"路径不存在：{pointer}")
        return container[int(segment)]
    raise PatchError(f"路径不存在：{pointer}")


def apply_op(document: Any, op: Mapping[str, Any]) -> None:
    """原地应用一个补丁操作；值一律深拷贝。"""

    kind, pointer = op.get("op"), op.get("path")
    parts = pointer_parts(pointer)
    if not parts:
        raise PatchError("补丁不得替换文档根。")
    parent = document
    for part in parts[:-1]:
        parent = _child(parent, part, str(pointer))
    leaf = parts[-1]
    value = copy.deepcopy(op.get("value"))
    if isinstance(parent, dict):
        exists = leaf in parent
        if kind == "add" and exists or kind in {"remove", "replace"} and not exists:
            raise PatchError(f"{kind} 的目标{'已存在' if exists else '不存在'}：{pointer}")
        if kind == "remove":
            del parent[leaf]
        else:
            parent[leaf] = value
        return
    if not isinstance(parent, list):
        raise PatchError(f"补丁的父节点不是容器：{pointer}")
    field = list_key_field(parent)
    keyed = _keyed(leaf)
    if field is not None or (keyed is not None and not parent):
        if keyed is None or (field is not None and keyed[0] != field):
            raise PatchError(f"路径必须按键字段 {field} 寻址：{pointer}")
        key_field, key_value = keyed
        index = next((i for i, item in enumerate(parent) if isinstance(item, dict) and item.get(key_field) == key_value), None)
        if kind == "add":
            if index is not None:
                raise PatchError(f"add 的目标已存在：{pointer}")
            if not isinstance(value, dict) or value.get(key_field) != key_value:
                raise PatchError(f"add 的值必须是对象且 {key_field} 等于路径里的键：{pointer}")
            ordered = [item.get(key_field) for item in parent] == sorted(item.get(key_field) for item in parent)
            parent.append(value)
            if ordered:
                parent.sort(key=lambda item: item[key_field])
            return
        if index is None:
            raise PatchError(f"{kind} 的目标不存在：{pointer}")
        if kind == "remove":
            del parent[index]
            return
        if not isinstance(value, dict) or value.get(key_field) != key_value:
            raise PatchError(f"replace 的值必须是对象且 {key_field} 等于路径里的键：{pointer}")
        parent[index] = value
        return
    if kind == "add":
        if leaf == "-":
            parent.append(value)
        elif leaf.isdigit() and int(leaf) <= len(parent):
            parent.insert(int(leaf), value)
        else:
            raise PatchError(f"add 的数组下标非法：{pointer}")
        return
    if not leaf.isdigit() or int(leaf) >= len(parent):
        raise PatchError(f"{kind} 的数组下标不存在：{pointer}")
    if kind == "remove":
        del parent[int(leaf)]
    else:
        parent[int(leaf)] = value


def diff_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """两份 JSON 的差异落点（键控数组按键对齐，键序变化记在数组本身；无键数组长度不同记整数组）。"""

    if type(before) is not type(after):
        return [prefix]
    if isinstance(before, dict):
        out: list[str] = []
        for key in sorted(set(before) | set(after)):
            pointer = join_pointer(prefix, key)
            if key not in before or key not in after:
                out.append(pointer)
            else:
                out.extend(diff_paths(before[key], after[key], pointer))
        return out
    if isinstance(before, list):
        field = list_key_field(before)
        if field is not None and field == list_key_field(after):
            before_map = {item[field]: item for item in before}
            after_map = {item[field]: item for item in after}
            out = []
            for key in sorted(set(before_map) | set(after_map)):
                pointer = join_pointer(prefix, f"{field}={key}")
                if key not in before_map or key not in after_map:
                    out.append(pointer)
                else:
                    out.extend(diff_paths(before_map[key], after_map[key], pointer))
            kept_before = [item[field] for item in before if item[field] in after_map]
            kept_after = [item[field] for item in after if item[field] in before_map]
            if kept_before != kept_after:
                out.append(prefix)
            return out
        if len(before) == len(after):
            out = []
            for index, (left, right) in enumerate(zip(before, after)):
                out.extend(diff_paths(left, right, join_pointer(prefix, str(index))))
            return out
        return [prefix]
    return [] if before == after else [prefix]


def _substitute(value: Any, replacements: Mapping[str, str], prefix: str, changed: list[str]) -> Any:
    """精确值替换（只换整串相等的字符串叶子），记录落点。"""

    if isinstance(value, dict):
        return {key: _substitute(item, replacements, join_pointer(prefix, key), changed) for key, item in value.items()}
    if isinstance(value, list):
        field = list_key_field(value)
        result = []
        for index, item in enumerate(value):
            segment = f"{field}={item[field]}" if field is not None else str(index)
            result.append(_substitute(item, replacements, join_pointer(prefix, segment), changed))
        return result
    if isinstance(value, str) and value in replacements:
        changed.append(prefix)
        return replacements[value]
    return value


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileInputs:
    """一次 VC-3 调用读到并核过的全部输入；``bindings`` 进 ``inputs_sha256``，``policy`` 是阶段 Profile policy。"""

    bindings: dict[str, Any]
    sha256: str
    policy: dict[str, Any]
    campaign_id: str
    baseline_version: str
    target_version: str
    purpose: str
    platform: str
    privacy_mode: str
    persona: dict[str, Any]
    classification: dict[str, Any]
    documents: dict[str, dict[str, Any]]
    target_rules: list[str]
    affected_rules: list[str]
    inherited_rules: list[str]
    decisions: dict[str, str]
    evidence_levels: dict[str, str]
    planned_paths: dict[str, list[str]]
    owners: dict[str, list[str]]
    evidence_ids: frozenset[str]
    active_profile: dict[str, Any]
    active_profile_bytes: bytes
    active_catalog_bytes: bytes
    replacements: dict[str, str]
    store_root: Path
    store_campaign_id: str
    tool_files_sha256: str
    status: str
    evidence_fact_ref: dict[str, Any]
    evidence_package_ref: dict[str, Any]
    classification_fact_ref: dict[str, Any]
    evidence_approval: dict[str, Any] | None
    evidence_approval_ref: dict[str, Any] | None
    profile_approval: dict[str, Any] | None
    vc1_sealed_at_utc: str
    release_evidence: Any
    active_projection_policy: dict[str, Any]
    target_rules_sha256: str
    atomic_counts: dict[str, int]


def _campaign_file_binding(campaign_dir: Path, path: Path, label: str) -> dict[str, Any]:
    relative = classify._relative_to(path, campaign_dir)
    if relative is None or path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label}必须是 Campaign 目录内的可信普通文件：{path}")
    return {"path": relative, "sha256": file_sha256(path), "bytes": path.stat().st_size}


def _active_paths(campaign_dir: Path, manifest: Mapping[str, Any], catalog: Path | None, profile: Path | None) -> tuple[Path, Path]:
    """Active 输入：总计划冻结了就用冻结副本（命令行不得另给），否则用命令行给出的 Campaign 内文件。"""

    inputs = manifest.get("inputs") or {}
    frozen = [name for name in ("active_release_catalog", "active_profile") if inputs.get(name) is not None]
    if frozen:
        if len(frozen) != 2:
            raise ConfigurationError("总计划只冻结了 active_release_catalog 与 active_profile 之一。")
        if catalog is not None or profile is not None:
            raise ConfigurationError("总计划已冻结 Active 输入，不接受 --active-release-catalog／--active-profile。")
        return (
            claude_upgrade_seal._frozen_input(campaign_dir, manifest, "active_release_catalog"),
            claude_upgrade_seal._frozen_input(campaign_dir, manifest, "active_profile"),
        )
    if catalog is None or profile is None:
        raise ConfigurationError("机器草案必须提供 --active-release-catalog 与 --active-profile（总计划没有冻结 Active 输入）。")
    resolved = [claude_upgrade_seal._campaign_input_file(campaign_dir, Path(value), label) for value, label in ((catalog, "Active Release 目录"), (profile, "Active 画像"))]
    assert resolved[0] is not None and resolved[1] is not None
    return resolved[0], resolved[1]


def _active_release(catalog: Mapping[str, Any], profile_sha256: str) -> dict[str, Any]:
    if catalog.get("schema_version") != RELEASE_CATALOG_SCHEMA or not isinstance(catalog.get("releases"), list):
        raise ConfigurationError(f"Active Release 目录不是 {RELEASE_CATALOG_SCHEMA}。")
    selector = (catalog.get("selectors") or {}).get("production_active")
    if not isinstance(selector, Mapping) or selector.get("kind") != "release" or not SHA256_RE.fullmatch(str(selector.get("release_sha256", ""))):
        raise ConfigurationError("Active Release 目录的 production_active 选择器不是 Release。")
    matches = [item for item in catalog["releases"] if isinstance(item, Mapping) and item.get("release_sha256") == selector["release_sha256"]]
    if len(matches) != 1:
        raise ConfigurationError("production_active 选择器指向的 Release 不唯一或不存在。")
    release = matches[0]
    if (release.get("profile") or {}).get("sha256") != profile_sha256:
        raise ConfigurationError("给出的 Active 画像不是 production_active Release 的画像（摘要不符）。")
    return {
        "version": str(release.get("version", "")),
        "platform": str(release.get("platform", "")),
        "profile_sha256": profile_sha256,
        "wire_sha256": str((release.get("wire") or {}).get("sha256", "")),
        "release_sha256": str(release["release_sha256"]),
        "bundle_sha256": str(release.get("bundle_sha256", "")),
        "required_rules_manifest_sha256": str(release.get("required_rules_manifest_sha256", "")),
    }


def _active_binary(profile: Mapping[str, Any]) -> str | None:
    values = {
        condition.split("=", 1)[1]
        for rule in profile.get("rules") or []
        if isinstance(rule, Mapping)
        for condition in rule.get("applicability") or []
        if isinstance(condition, str) and condition.startswith("binary_sha256=")
    }
    if len(values) > 1:
        raise ConfigurationError(f"Active 画像的规则适用条件里有多个 binary_sha256：{sorted(values)}")
    return next(iter(values), None)


def _store_state(store: Any, campaign_id: str) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    status = classify._store_call(
        "读取 Store 状态", lambda: claude_upgrade_seal.store_cli(["status", "--store", store.root, "--campaign", campaign_id])
    )
    facts: dict[str, list[dict[str, Any]]] = {}
    for fact in classify._store_call("读取 Store 事实", lambda: store.list_facts(campaign_id)):
        facts.setdefault(str(fact["fact_kind"]), []).append(fact)
    return str(status.get("checkpoint")), facts


def collect_inputs(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    active_catalog_path: Path,
    active_profile_path: Path,
    *,
    allowed_status: frozenset[str],
) -> ProfileInputs:
    """读取并核对 VC-3 的全部输入（指南 §4.3.1 开头：先核分类事实对两份台账与 VC-1 证据的绑定、复算 Active 摘要）。"""

    orchestrator = _orchestrator()
    campaign_id = str(manifest["campaign_id"])
    baseline_version = str(manifest["baseline_version"])
    target_version = str(manifest["target_version"])
    configuration = manifest.get("configuration") or {}
    platform = str(configuration.get("platform", ""))
    privacy_mode = str(configuration.get("privacy_mode", ""))
    purpose = str(manifest["campaign_purpose"])
    if purpose not in APPROVAL_PURPOSES:
        raise ConfigurationError(f"Campaign 用途不是 Store 批准用途：{purpose}")
    plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
    vc2_path, vc2 = claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, "VC-2")
    stage = classify.load_stage_result(campaign_dir, manifest)
    documents = {
        name: _read_json(campaign_dir / str(binding["path"]), f"VC-2 批准文件 {name}") for name, binding in stage["approved"].items()
    }
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    tool_files_sha256 = claude_upgrade_seal._require_store_tool_current(store, store_campaign)
    status, facts = _store_state(store, campaign_id)
    if status not in allowed_status:
        raise ConfigurationError(f"Store 检查点 {status} 不允许这一步（需要 {sorted(allowed_status)}）。")
    classification_facts = facts.get("rule_classification_recorded", [])
    if len(classification_facts) != 1:
        raise ConfigurationError("Store 里没有唯一的规则分类事实：VC-2 尚未封存。")
    classification_fact = classification_facts[0]
    store_section = stage["store"]
    classification_fact_ref = dict(store_section["classification_fact_ref"])
    if store.fact_ref(classification_fact) != classification_fact_ref or classification_fact["payload"] != {
        key: store_section[key]
        for key in ("evidence_fact_ref", "evidence_package_ref", "rule_migration_ledger_ref", "atomic_assertion_ledger_ref")
    }:
        raise ConfigurationError("Store 的规则分类事实与 VC-2 阶段结果登记的引用不一致。")
    migration = classify._store_call("读取 RuleMigrationLedger", lambda: store.load_object(dict(store_section["rule_migration_ledger_ref"])))["payload"]
    atomic = classify._store_call("读取 AtomicAssertionLedger", lambda: store.load_object(dict(store_section["atomic_assertion_ledger_ref"])))["payload"]
    package = classify._store_call("读取 EvidencePackage", lambda: store.load_object(dict(store_section["evidence_package_ref"])))["payload"]
    target_rules = list(stage["target_rules"])
    if sorted(target_rules) != sorted(migration["target_spec_ids"]):
        raise ConfigurationError("VC-2 阶段结果的目标规则与 Store 的 RuleMigrationLedger 不一致。")
    decisions = {str(rule["spec_id"]): str(rule["migration_decision"]) for rule in migration["rules"]}
    evidence_levels = {str(rule["spec_id"]): str(rule["evidence_level"]) for rule in migration["rules"]}
    owners: dict[str, list[str]] = {}
    for assertion in atomic["assertions"]:
        owners.setdefault(str(assertion["owner_id"]), []).append(str(assertion["assertion_id"]))
    patches = documents["profile_patches"]
    planned_paths = {str(row["spec_id"]): list(row["profile_paths"]) for row in patches["rule_patches"]}
    catalog_path, profile_path = active_catalog_path, active_profile_path
    catalog_binding = _campaign_file_binding(campaign_dir, catalog_path, "Active Release 目录")
    profile_binding = _campaign_file_binding(campaign_dir, profile_path, "Active 画像")
    catalog_bytes = catalog_path.read_bytes()
    profile_raw = profile_path.read_bytes()
    active_profile = _parse_json_object(profile_raw, "Active 画像")
    if profile_bytes(active_profile) != profile_raw:
        raise ConfigurationError("Active 画像不是内容寻址的规范字节（键排序、紧凑分隔、末尾换行）。")
    active = _active_release(_parse_json_object(catalog_bytes, "Active Release 目录"), _sha256(profile_raw))
    identity = active_profile.get("identity")
    if not isinstance(identity, Mapping) or identity.get("version") != baseline_version or active["version"] != baseline_version:
        raise ConfigurationError("Active 画像与 Release 的版本必须等于 Campaign 的基线版本。")
    if not isinstance(active_profile.get("rules"), list) or list_key_field(active_profile["rules"]) != "spec_id":
        raise ConfigurationError("Active 画像的 rules 必须是按 spec_id 键控的规则数组。")
    active_rule_ids = sorted(str(rule["spec_id"]) for rule in active_profile["rules"])
    if active_rule_ids != sorted(manifest["required_rules"]):
        raise ConfigurationError("Active 画像的规则全集不等于 Campaign 的基线 RequiredRules。")
    active_platform = str(identity.get("platform", ""))
    if active_platform != active["platform"]:
        raise ConfigurationError("Active 画像与 Release 目录登记的平台不一致。")
    active_binary = _active_binary(active_profile)
    target_binary = str(manifest["target_sha256"])
    pairs = [(baseline_version, target_version), (active_platform, platform)]
    if active_binary is not None:
        pairs.append((active_binary, target_binary))
    replacements: dict[str, str] = {}
    for old, new in pairs:
        if old and old != new:
            replacements[old] = new
    for key, (old, new) in zip(IDENTITY_CONDITION_KEYS, pairs + [(None, None)] * (3 - len(pairs))):
        if old and new and old != new:
            replacements[f"{key}={old}"] = f"{key}={new}"
    store_refs = {
        key: dict(store_section[key])
        for key in ("evidence_fact_ref", "evidence_package_ref", "rule_migration_ledger_ref", "atomic_assertion_ledger_ref", "classification_fact_ref")
    }
    stage_path = campaign_dir / classify.STAGE_RESULT_RELATIVE
    # B3-03 补充项：Wire 投影的输入——VC-1 证据索引、Active 版本的投影策略、原子断言按 owner_kind 的计数。
    release_evidence = release.load_release_evidence(campaign_dir, store, package, stage)
    active_projection, projection_binding = release.active_projection_policy(baseline_version)
    atomic_counts = dict(Counter(str(assertion["owner_kind"]) for assertion in atomic["assertions"]))
    policy = {
        "schema_version": PROFILE_POLICY_SCHEMA,
        "campaign_id": campaign_id,
        "baseline_version": baseline_version,
        "target_version": target_version,
        "campaign_purpose": purpose,
        "target_identity": {
            "version": target_version,
            "platform": platform,
            "binary_sha256": target_binary,
            "entrypoint": str(configuration.get("entrypoint", "")),
            "privacy_mode": privacy_mode,
        },
        "active": {
            "release_catalog": catalog_binding,
            "profile": profile_binding,
            **active,
            "binary_sha256": active_binary,
            "profile_schema_version": str(active_profile.get("schema_version", "")),
            "projection_policy": projection_binding,
        },
        "classification": {
            "stage_result": {"path": stage_path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(stage_path)},
            "vc2_checkpoint": {"path": vc2_path.relative_to(campaign_dir).as_posix(), "checkpoint_sha256": str(vc2["checkpoint_sha256"])},
            "revision": str(stage["revision"]),
            "review_sha256": str(stage["review_sha256"]),
            **store_refs,
        },
        "rules": {
            "target_rules": target_rules,
            "affected_rules": list(stage["affected_rules"]),
            "inherited_rules": list(stage["inherited_rules"]),
            "atomic_assertion_count": len(atomic["assertions"]),
            "scenario_only_owner_ids": list(atomic["scenario_only_owner_ids"]),
            "atomic_owner_counts": {key: atomic_counts[key] for key in sorted(atomic_counts)},
        },
        "identity_substitutions": [{"from": old, "to": new} for old, new in sorted(replacements.items())],
        "model_capability_paths": list(MODEL_CAPABILITY_PATHS),
        # 生成工具：policy 只登记生成器与 policy 合同；生成时的有效工具身份冻结在准备结果里（批准时刻），审核摘要也绑定它。
        "generator": {
            "module": "claude_upgrade_profile",
            "policy_schema": PROFILE_POLICY_SCHEMA,
            # Wire、模型能力目录与 Release 的确定性投影工具（指南 §4.3.2）与其策略合同。
            "projection_module": "tools.generate_claude_fw_g_profile",
            "projection_policy_schema": release.generator.PROJECTION_POLICY_SCHEMA,
        },
    }
    bindings = {"policy": policy, "active_release_catalog": catalog_binding, "active_profile": profile_binding}
    official = _read_json(campaign_dir / claude_upgrade_seal.STAGE_RESULT_RELATIVE, "官方阶段结果")
    return ProfileInputs(
        bindings=bindings,
        sha256=orchestrator._fingerprint(bindings),
        policy=policy,
        campaign_id=campaign_id,
        baseline_version=baseline_version,
        target_version=target_version,
        purpose=purpose,
        platform=platform,
        privacy_mode=privacy_mode,
        persona=dict(store_campaign["persona"]),
        classification=stage,
        documents=documents,
        target_rules=target_rules,
        affected_rules=list(stage["affected_rules"]),
        inherited_rules=list(stage["inherited_rules"]),
        decisions=decisions,
        evidence_levels=evidence_levels,
        planned_paths=planned_paths,
        owners=owners,
        evidence_ids=frozenset(str(item["evidence_id"]) for item in package["evidence_items"]),
        active_profile=active_profile,
        active_profile_bytes=profile_raw,
        active_catalog_bytes=catalog_bytes,
        replacements=replacements,
        store_root=Path(store.root),
        store_campaign_id=str(store_campaign["campaign_id"]),
        tool_files_sha256=tool_files_sha256,
        status=status,
        evidence_fact_ref=store_refs["evidence_fact_ref"],
        evidence_package_ref=store_refs["evidence_package_ref"],
        classification_fact_ref=store_refs["classification_fact_ref"],
        evidence_approval=(facts.get("evidence_approved") or [None])[0],
        evidence_approval_ref=store.fact_ref(facts["evidence_approved"][0]) if facts.get("evidence_approved") else None,
        profile_approval=(facts.get("profile_approved") or [None])[0],
        vc1_sealed_at_utc=str(official.get("sealed_at_utc", "")),
        release_evidence=release_evidence,
        active_projection_policy=active_projection,
        target_rules_sha256=str(stage["approved"]["target_rules"]["sha256"]),
        atomic_counts=atomic_counts,
    )


# ---------------------------------------------------------------------------
# 机器草案
# ---------------------------------------------------------------------------


def _strict_endpoints(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    endpoints = profile.get("strict_endpoints")
    if not isinstance(endpoints, list):
        return []
    return [item for item in endpoints if isinstance(item, dict)]


def _egress_kind(endpoint_kind: Any) -> str:
    """预填用的观测类别（审核人可改）：推理端点为 inference，生命周期端点为 lifecycle，其余为 auxiliary。"""

    text = str(endpoint_kind or "")
    if text == "messages-inference":
        return "inference"
    if text.startswith("lifecycle"):
        return "lifecycle"
    return "auxiliary"


def machine_draft_files(inputs: ProfileInputs) -> dict[str, bytes]:
    """机器草案：policy 由输入派生；规则补丁与模型补丁为空；联合输入只预填能由 Active 画像推出的严格端点；Wire 投影
    策略取 Active 版本的那一份、只换版本号。"""

    rule_patches = {
        "schema_version": RULE_PATCHES_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "baseline_version": inputs.baseline_version,
        "target_version": inputs.target_version,
        "active_profile_sha256": inputs.policy["active"]["profile_sha256"],
        "rule_patches": [
            {
                "spec_id": spec_id,
                "migration_decision": inputs.decisions[spec_id],
                "planned_paths": list(inputs.planned_paths.get(spec_id, [])),
                "ops": [],
                "evidence_item_ids": [],
                "rationale": PLACEHOLDER_RATIONALE,
            }
            for spec_id in inputs.affected_rules
        ],
    }
    model_patch = {
        "schema_version": MODEL_PATCH_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "target_version": inputs.target_version,
        "ops": [],
        "evidence_item_ids": [],
        "rationale": "",
    }
    endpoints = _strict_endpoints(inputs.active_profile)
    joint = {
        "schema_version": JOINT_INPUTS_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "target_version": inputs.target_version,
        "persona_descriptor": {"explicit_exclusions": []},
        "ingress": {"observed_at_utc": inputs.vc1_sealed_at_utc, "aliases": [], "entries": [], "target_dispositions": []},
        "egress": {
            "observed_at_utc": inputs.vc1_sealed_at_utc,
            "egresses": sorted(
                (
                    {
                        "egress_id": str(item.get("egress_id")),
                        "route_id": str(item.get("route_id")),
                        "sink_id": str(item.get("sink_id")),
                        "oauth_related": True,
                        "kind": _egress_kind(item.get("endpoint_kind")),
                    }
                    for item in endpoints
                ),
                key=lambda item: item["egress_id"],
            ),
            "entries": sorted(
                (
                    {
                        "egress_id": str(item.get("egress_id")),
                        "current_disposition": "persona_strict",
                        "current_guard_state": "enforced",
                        "spec_ids": sorted(set(item.get("spec_ids") or [])),
                        "managed_policy": None,
                    }
                    for item in endpoints
                ),
                key=lambda item: item["egress_id"],
            ),
            "target_dispositions": sorted(
                (
                    {
                        "egress_id": str(item.get("egress_id")),
                        "target_disposition": "persona_strict",
                        "spec_ids": sorted(set(item.get("spec_ids") or []) & set(inputs.target_rules)),
                        "managed_policy": None,
                    }
                    for item in endpoints
                ),
                key=lambda item: item["egress_id"],
            ),
        },
        "support": {"capabilities": [], "unsupported_conditions": []},
        "boundary_assertions": [],
        "persona_derivation": [],
        "compatibility_boundary": [],
        "deployment": {
            "rollback_operational_capabilities": [],
            "deployment_traffic_capabilities": [],
            "rollback_target_kind": "",
            "failure_policy": "",
        },
    }
    files = {
        "policy": _serialize(inputs.policy),
        "rule_patches": _serialize(rule_patches),
        "model_patch": _serialize(model_patch),
        "joint_inputs": _serialize(joint),
        # Active 版本的 Wire 投影策略只换版本号；模型闭集、版本指纹 salt 等由审核人按目标证据修订。
        "projection": _serialize(release.machine_projection_policy(inputs.active_projection_policy, inputs.target_version)),
    }
    return {name: files[name] for name in PREPARE_FILES}


# ---------------------------------------------------------------------------
# 目标画像派生与 profile_diff_paths 约束
# ---------------------------------------------------------------------------


def _check_ops(ops: Any, label: str, problems: _Problems) -> list[dict[str, Any]]:
    if not isinstance(ops, list):
        problems.add("patch_invalid", f"{label}：ops 必须是数组")
        return []
    valid: list[dict[str, Any]] = []
    for index, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in PATCH_OPS:
            problems.add("patch_invalid", f"{label} 第 {index + 1} 个操作：op 只能是 add／remove／replace")
            continue
        expected = {"op", "path"} if op["op"] == "remove" else {"op", "path", "value"}
        if set(op) != expected:
            problems.add("patch_invalid", f"{label} 第 {index + 1} 个操作字段不闭合（remove 不带 value，add／replace 必带 value）")
            continue
        try:
            if not pointer_parts(op["path"]):
                raise PatchError("补丁不得替换文档根。")
        except PatchError as error:
            problems.add("patch_invalid", f"{label}：{error}")
            continue
        valid.append(op)
    return valid


def derive_target_profile(
    inputs: ProfileInputs,
    documents: Mapping[str, Mapping[str, Any]],
    problems: _Problems,
    model_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """按指南 §4.3.1 派生目标画像并复算 ``profile_diff_paths`` 约束；返回 ``{target, derivation}``，结构失败返回 None。

    ``model_identity`` 是投影出的模型能力目录派生的四个 identity 字段（B3-03 补充项）：在版本身份替换之后写入，落点记为
    ``model_identity_paths``，是 ``profile_diff_paths`` 的一类解释；规则补丁与模型补丁都不得再碰这些落点。投影失败时
    为 None（门禁已因投影失败 blocked），派生照常进行以便列出其余原因。
    """

    patches = documents["rule_patches"]
    model = documents["model_patch"]
    if (
        set(patches) != {"schema_version", "campaign_id", "baseline_version", "target_version", "active_profile_sha256", "rule_patches"}
        or patches.get("schema_version") != RULE_PATCHES_SCHEMA
        or (patches["campaign_id"], patches["baseline_version"], patches["target_version"]) != (inputs.campaign_id, inputs.baseline_version, inputs.target_version)
        or patches["active_profile_sha256"] != inputs.policy["active"]["profile_sha256"]
        or not isinstance(patches["rule_patches"], list)
    ):
        problems.add("patch_invalid", "rule-patches.json 字段、合同名、Campaign、版本或 Active 摘要不符")
        return None
    if (
        set(model) != {"schema_version", "campaign_id", "target_version", "ops", "evidence_item_ids", "rationale"}
        or model.get("schema_version") != MODEL_PATCH_SCHEMA
        or (model["campaign_id"], model["target_version"]) != (inputs.campaign_id, inputs.target_version)
    ):
        problems.add("patch_invalid", "model-capability-patch.json 字段、合同名、Campaign 或版本不符")
        return None
    rows = patches["rule_patches"]
    row_fields = {"spec_id", "migration_decision", "planned_paths", "ops", "evidence_item_ids", "rationale"}
    if any(not isinstance(row, dict) or set(row) != row_fields for row in rows):
        problems.add("patch_invalid", "rule_patches 条目字段不闭合")
        return None
    expected_rows = {spec_id: (inputs.decisions[spec_id], inputs.planned_paths.get(spec_id, [])) for spec_id in inputs.affected_rules}
    spec_ids = [row["spec_id"] for row in rows]
    if spec_ids != list(inputs.affected_rules):
        problems.add("patch_set_mismatch", f"rule_patches 必须按序恰为 VC-2 的 affected_rules：{inputs.affected_rules}")
        return None
    owners: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        spec_id = row["spec_id"]
        if (row["migration_decision"], row["planned_paths"]) != expected_rows[spec_id]:
            problems.add("patch_fields_modified", f"{spec_id}：migration_decision 与 planned_paths 是 VC-2 的只读字段")
        planned = list(row["planned_paths"])
        for scope in planned:
            try:
                pointer_parts(scope)
            except PatchError as error:
                problems.add("planned_path_invalid", f"{spec_id}：{error}")
        ops = _check_ops(row["ops"], spec_id, problems)
        if not ops:
            problems.add("rule_patch_missing", spec_id)
        if not _sorted_unique_strings(row["evidence_item_ids"], pattern=SAFE_ID_RE):
            problems.add("patch_evidence_missing", spec_id)
        elif set(row["evidence_item_ids"]) - inputs.evidence_ids:
            problems.add("evidence_item_unknown", f"{spec_id}：{sorted(set(row['evidence_item_ids']) - inputs.evidence_ids)}")
        if not _text(row["rationale"]) or row["rationale"] == PLACEHOLDER_RATIONALE:
            problems.add("rationale_placeholder", spec_id)
        for op in ops:
            if not any(within(op["path"], scope) for scope in planned):
                problems.add("rule_patch_out_of_scope", f"{spec_id}：{op['path']} 不在 VC-2 登记的 profile_paths 之内")
            owners.append((spec_id, op))
    model_ops = _check_ops(model["ops"], "模型能力补丁", problems)
    if model_ops:
        if not _sorted_unique_strings(model["evidence_item_ids"], pattern=SAFE_ID_RE):
            problems.add("patch_evidence_missing", "模型能力补丁")
        elif set(model["evidence_item_ids"]) - inputs.evidence_ids:
            problems.add("evidence_item_unknown", f"模型能力补丁：{sorted(set(model['evidence_item_ids']) - inputs.evidence_ids)}")
        if not _text(model["rationale"]):
            problems.add("rationale_placeholder", "模型能力补丁")
    for op in model_ops:
        if not any(within(op["path"], scope) for scope in MODEL_CAPABILITY_PATHS):
            problems.add("model_patch_out_of_scope", f"{op['path']} 不在模型能力落点 {list(MODEL_CAPABILITY_PATHS)} 之内")
        owners.append(("model_capability", op))
    # 版本身份替换（精确值），再写入模型能力目录派生的 identity 字段，得到补丁的基底。
    version_paths: list[str] = []
    base = _substitute(inputs.active_profile, inputs.replacements, "", version_paths)
    version_paths = sorted(set(version_paths))
    model_identity_paths: list[str] = []
    if model_identity is not None:
        identity = base.setdefault("identity", {})
        for key in release.MODEL_IDENTITY_FIELDS:
            if identity.get(key) != model_identity[key]:
                identity[key] = copy.deepcopy(model_identity[key])
                model_identity_paths.append(join_pointer("/identity", key))
    derived_identity_scopes = [join_pointer("/identity", key) for key in release.MODEL_IDENTITY_FIELDS] if model_identity is not None else []
    inherited_scopes = [join_pointer("/rules", f"spec_id={spec_id}") for spec_id in inputs.inherited_rules]
    for owner, op in owners:
        path = op["path"]
        if path in version_paths:
            problems.add("patch_owner_conflict", f"{owner}：{path} 是版本身份落点")
        if any(within(path, scope) or within(scope, path) for scope in derived_identity_scopes):
            problems.add("patch_owner_conflict", f"{owner}：{path} 是模型能力目录派生的 identity 落点（由 Wire 投影策略决定）")
        for scope in inherited_scopes:
            if within(path, scope) or within(scope, path):
                problems.add("inherited_rule_changed", f"{owner}：{path} 触碰继承规则 {scope}")
    for index, (owner, op) in enumerate(owners):
        for other_owner, other in owners[index + 1:]:
            if owner != other_owner and (within(op["path"], other["path"]) or within(other["path"], op["path"])):
                problems.add("patch_owner_conflict", f"{owner} 与 {other_owner} 的补丁路径重叠：{op['path']}／{other['path']}")
    target = copy.deepcopy(base)
    applied = True
    for owner, op in owners:
        try:
            apply_op(target, op)
        except PatchError as error:
            problems.add("patch_apply_failed", f"{owner}：{error}")
            applied = False
    if not applied:
        return None
    op_paths = [op["path"] for _owner, op in owners]
    patch_diff = diff_paths(base, target)
    for owner, op in owners:
        if not any(within(path, op["path"]) or within(op["path"], path) for path in patch_diff):
            problems.add("patch_noop", f"{owner}：{op['path']} 没有产生差异")
    profile_diff = sorted(set(diff_paths(inputs.active_profile, target)))
    unexplained = [
        path
        for path in profile_diff
        if path not in version_paths
        and path not in model_identity_paths
        and not any(within(path, scope) or within(scope, path) for scope in op_paths)
    ]
    if unexplained:
        problems.extend("profile_diff_unexplained", unexplained)
    for path in patch_diff:
        for scope in inherited_scopes:
            if within(path, scope):
                problems.add("inherited_rule_changed", path)
    rules = target.get("rules")
    target_rule_ids = sorted(str(rule.get("spec_id")) for rule in rules if isinstance(rule, dict)) if isinstance(rules, list) else []
    if target_rule_ids != sorted(inputs.target_rules):
        problems.add(
            "profile_rule_set_mismatch",
            f"多 {sorted(set(target_rule_ids) - set(inputs.target_rules))}，少 {sorted(set(inputs.target_rules) - set(target_rule_ids))}",
        )
    stale: list[str] = []

    def scan(value: Any, pointer: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                scan(item, join_pointer(pointer, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                scan(item, join_pointer(pointer, str(index)))
        elif isinstance(value, str) and SPEC_ID_RE.fullmatch(value) and value not in inputs.target_rules:
            stale.append(f"{pointer}={value}")

    for section in ("strict_endpoints", "feature_matrix"):
        if section in target:
            scan(target[section], join_pointer("", section))
    if stale:
        problems.extend("profile_rule_reference_stale", stale)
    target_raw = profile_bytes(target)
    rule_field_paths: dict[str, list[str]] = {}
    model_paths: list[str] = []
    for owner, op in owners:
        if owner == "model_capability":
            model_paths.append(op["path"])
        else:
            rule_field_paths.setdefault(owner, []).append(op["path"])
    derivation = {
        "schema_version": DERIVATION_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "baseline_version": inputs.baseline_version,
        "target_version": inputs.target_version,
        "active_profile_sha256": inputs.policy["active"]["profile_sha256"],
        "target_profile_sha256": _sha256(target_raw),
        "identity_substitutions": inputs.policy["identity_substitutions"],
        "version_identity_paths": version_paths,
        "model_identity_paths": sorted(model_identity_paths),
        "model_capability_catalog_sha256": model_identity["model_capability_catalog_sha256"] if model_identity is not None else None,
        "rule_field_paths": {spec_id: sorted(set(paths)) for spec_id, paths in sorted(rule_field_paths.items())},
        "model_capability_paths": sorted(set(model_paths)),
        "profile_diff_paths": profile_diff,
        "unexplained_paths": unexplained,
        "affected_rules": list(inputs.affected_rules),
        "inherited_rules": list(inputs.inherited_rules),
        "live_request_count": 0,
    }
    return {"target": target, "target_bytes": target_raw, "derivation": derivation}


# ---------------------------------------------------------------------------
# 联合制品
# ---------------------------------------------------------------------------


def _manifest_payload(kind: str, persona: Mapping[str, Any], manifest_id: str, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": f"official-client-{kind.replace('_', '-')}/v1",
        "persona": dict(persona),
        "manifest_id": manifest_id,
        "entries": sorted((dict(entry) for entry in entries), key=lambda entry: str(entry.get("id"))),
    }


def _capabilities(values: Any, label: str, problems: _Problems) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        problems.add("joint_invalid", f"{label} 必须是数组")
        return []
    result = [dict(item) for item in values if isinstance(item, dict)]
    if len(result) != len(values):
        problems.add("joint_invalid", f"{label} 的条目必须是对象")
    try:
        keys = [capability_key(item) for item in result]
    except ControlError as error:
        problems.add("joint_invalid", f"{label}：{error}")
        return []
    if len(keys) != len(set(keys)):
        problems.add("joint_invalid", f"{label} 有重复能力")
    return [item for _key, item in sorted(zip(keys, result), key=lambda pair: pair[0])]


def _object(name: str, kind: str, payload: dict[str, Any], objects: list[dict[str, Any]], problems: _Problems) -> dict[str, str] | None:
    try:
        reference = claude_upgrade_evidence._object_reference(kind, payload)
    except ConfigurationError as error:
        problems.add("joint_contract_invalid", f"{name}：{error}")
        return None
    objects.append({"name": name, "kind": kind, "ref": reference, "payload": payload})
    return reference


def build_joint(
    inputs: ProfileInputs,
    target: Mapping[str, Any],
    target_raw: bytes,
    derivation: Mapping[str, Any],
    joint: Mapping[str, Any],
    files_sha256: Mapping[str, str],
    problems: _Problems,
    *,
    release_info: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """由目标画像、VC-2 结果与联合输入确定性投影联合制品（按封存依赖顺序），并复核 Store 画像批准门禁会查的一致性。

    ``release_info``（B3-03 补充项）带投影出的模型能力目录与 Release 身份摘要：ReleaseBundle 的 document 绑定 Wire、
    模型能力目录与 Release 的摘要，另封存一个 ``model_capability_catalog`` 对象，于是 Profile、Wire、模型目录与 Release
    进同一份联合摘要（指南 §4.3.3）。投影失败时为 None（门禁已 blocked）。
    """

    if set(joint) != JOINT_TOP_FIELDS or joint.get("schema_version") != JOINT_INPUTS_SCHEMA:
        problems.add("joint_invalid", "joint-inputs.json 字段不闭合或合同名不符")
        return None
    if (joint["campaign_id"], joint["target_version"]) != (inputs.campaign_id, inputs.target_version):
        problems.add("joint_invalid", "joint-inputs.json 的 Campaign 或目标版本不符")
    ingress, egress, deployment = joint["ingress"], joint["egress"], joint["deployment"]
    if not isinstance(ingress, dict) or set(ingress) != JOINT_INGRESS_FIELDS:
        problems.add("joint_invalid", "ingress 字段不闭合")
        return None
    if not isinstance(egress, dict) or set(egress) != JOINT_EGRESS_FIELDS:
        problems.add("joint_invalid", "egress 字段不闭合")
        return None
    if not isinstance(deployment, dict) or set(deployment) != JOINT_DEPLOYMENT_FIELDS:
        problems.add("joint_invalid", "deployment 字段不闭合")
        return None
    descriptor_input = joint["persona_descriptor"]
    if not isinstance(descriptor_input, dict) or set(descriptor_input) != {"explicit_exclusions"}:
        problems.add("joint_invalid", "persona_descriptor 只能有 explicit_exclusions")
        return None
    for label, value in (("ingress.observed_at_utc", ingress["observed_at_utc"]), ("egress.observed_at_utc", egress["observed_at_utc"])):
        try:
            expect_rfc3339(value, label)
        except ControlError as error:
            problems.add("joint_invalid", str(error))
    persona = inputs.persona
    objects: list[dict[str, Any]] = []
    endpoints = _strict_endpoints(target)
    target_digest = _sha256(target_raw)
    # 来源：本次联合制品依据的冻结输入，作为入口／出站条目与目标处置的证据引用。
    source = _object(
        "joint_source",
        "operational_evidence",
        _manifest_payload(
            "operational_evidence",
            persona,
            "claude-upgrade-vc3-joint-source",
            [
                {"id": "active-release", "facts": {key: inputs.policy["active"][key] for key in ("release_sha256", "profile_sha256", "bundle_sha256", "version", "platform")}},
                {"id": "classification", "facts": dict(inputs.policy["classification"])},
                {"id": "prepare-files", "facts": dict(files_sha256)},
                {"id": "target-profile", "facts": {"profile_sha256": target_digest, "derivation_sha256": canonical_sha256(dict(derivation))}},
            ],
        ),
        objects,
        problems,
    )
    if source is None:
        return None
    evidence = [source]
    exclusions = descriptor_input["explicit_exclusions"]
    if not _sorted_unique_strings(exclusions, non_empty=False):
        problems.add("joint_invalid", "persona_descriptor.explicit_exclusions 必须是排序去重的文字数组")
    descriptor = _object(
        "persona_descriptor",
        "persona_descriptor",
        {
            "schema_version": "official-client-persona-descriptor/v1",
            "persona": dict(persona),
            "routes": sorted({str(item.get("route_id")) for item in endpoints}),
            "sinks": sorted({str(item.get("sink_id")) for item in endpoints}),
            "explicit_exclusions": list(exclusions) if isinstance(exclusions, list) else [],
        },
        objects,
        problems,
    )
    schema_ref = _object(
        "profile_schema",
        "profile_schema",
        {
            "schema_version": "official-client-profile-schema/v1",
            "persona": dict(persona),
            "version": inputs.target_version,
            "schema_id": re.sub(r"[^A-Za-z0-9._-]", "-", str(target.get("schema_version", "claude-profile"))),
            "document": {
                "profile_schema_version": target.get("schema_version"),
                "top_level_fields": sorted(target),
                "rule_key_field": "spec_id",
                "content_addressing": "sha256(canonical-json, sort_keys, compact, trailing-newline)",
            },
        },
        objects,
        problems,
    )
    if schema_ref is None:
        return None
    snapshot_ref = _object(
        "snapshot",
        "snapshot",
        {
            "schema_version": "official-client-snapshot/v1",
            "persona": dict(persona),
            "version": inputs.target_version,
            "profile_digest": target_digest,
            "profile_schema_ref": schema_ref,
            # 派生证明：同一 Schema 由 Active 经版本身份替换与批准补丁派生（指南 §4.3.1），不经编译器重编。
            "compiler_attestation_sha256": canonical_sha256(
                {
                    "attestation": "claude-upgrade-vc3-active-derivation/v1",
                    "active_release_sha256": inputs.policy["active"]["release_sha256"],
                    "active_profile_sha256": inputs.policy["active"]["profile_sha256"],
                    "profile_schema_version": inputs.policy["active"]["profile_schema_version"],
                    "derivation_sha256": canonical_sha256(dict(derivation)),
                }
            ),
            "document": dict(target),
        },
        objects,
        problems,
    )
    if snapshot_ref is None:
        return None
    bundle_ref = _object(
        "release_bundle",
        "release_bundle",
        {
            "schema_version": "official-client-release-bundle/v1",
            "persona": dict(persona),
            "version": inputs.target_version,
            "profile_digest": target_digest,
            "snapshot_ref": snapshot_ref,
            "endpoint_digest": canonical_sha256({"endpoint": target.get("endpoint"), "strict_endpoints": [item.get("endpoint") for item in endpoints]}),
            "transport_digest": canonical_sha256({"transport": target.get("transport")}),
            "state_digest": canonical_sha256({"state": target.get("state")}),
            "policy_digest": canonical_sha256(
                {key: target.get(key) for key in ("headers", "body", "privacy", "strict_endpoints", "feature_matrix", "rules")}
            ),
            "document": {
                "generation_role": target.get("generation_role"),
                "profile_document_sha256": target_digest,
                "rule_count": len(target.get("rules") or []),
                "release": dict(release_info["summary"]) if release_info is not None else None,
            },
        },
        objects,
        problems,
    )
    if bundle_ref is None:
        return None
    release_ref = _object(
        "release_artifact",
        "release_artifact",
        {
            "schema_version": "official-client-release-artifact/v1",
            "persona": dict(persona),
            "version": inputs.target_version,
            "profile_digest": target_digest,
            "snapshot_ref": snapshot_ref,
            "release_bundle_ref": bundle_ref,
        },
        objects,
        problems,
    )
    if release_info is not None:
        projection = release_info["projection"]
        _object(
            "model_capability_catalog",
            release.CATALOG_OBJECT_KIND,
            release.catalog_object_payload(persona, projection.catalog, projection.catalog_sha256),
            objects,
            problems,
        )
    # 入口：别名与清单闭合、目标处置覆盖完整清单。
    aliases = [dict(item) for item in ingress["aliases"]] if isinstance(ingress["aliases"], list) else []
    entries = [dict(item) for item in ingress["entries"]] if isinstance(ingress["entries"], list) else []
    if any(set(item) != ALIAS_FIELDS for item in aliases) or any(set(item) != INGRESS_ENTRY_FIELDS for item in entries):
        problems.add("joint_invalid", "ingress.aliases 或 ingress.entries 的条目字段不闭合")
        return None
    if not aliases or not entries:
        problems.add("joint_ingress_missing", "生产入口的别名与清单不能为空（VC-1 尚无入口清单生产者，需审核人提供）")
        return None
    alias_index = {str(item["alias_id"]): item for item in aliases}
    claimed: dict[str, str] = {}
    for entry in entries:
        callers: set[str] = set()
        for alias_id in entry["physical_alias_ids"] if isinstance(entry["physical_alias_ids"], list) else []:
            alias = alias_index.get(str(alias_id))
            if alias is None or alias_id in claimed or alias["logical_ingress_id"] != entry["logical_ingress_id"]:
                problems.add("joint_ingress_open", f"{entry['logical_ingress_id']}：别名 {alias_id} 不存在、重复处置或归属不符")
                continue
            claimed[str(alias_id)] = str(entry["logical_ingress_id"])
            callers.update(alias["caller_ids"] if isinstance(alias["caller_ids"], list) else [])
        if entry["caller_ids"] != sorted(callers):
            problems.add("joint_ingress_open", f"{entry['logical_ingress_id']}：caller_ids 必须等于其别名调用方之并（排序）")
    if sorted(set(alias_index) - set(claimed)):
        problems.add("joint_ingress_open", f"别名未被任何入口处置：{sorted(set(alias_index) - set(claimed))}")
    ingress_observation = _object(
        "ingress_observation",
        "ingress_observation",
        {
            "schema_version": "official-client-ingress-observation/v1",
            "persona": dict(persona),
            "observed_at_utc": ingress["observed_at_utc"],
            "source_refs": evidence,
            "aliases": sorted(aliases, key=lambda item: str(item["alias_id"])),
        },
        objects,
        problems,
    )
    ingress_ref = None
    if ingress_observation is not None:
        ingress_ref = _object(
            "production_ingress_inventory",
            "production_ingress_inventory",
            {
                "schema_version": "official-client-production-ingress-inventory/v1",
                "persona": dict(persona),
                "observation_ref": ingress_observation,
                "entries": sorted(({**item, "evidence_refs": evidence} for item in entries), key=lambda item: str(item["logical_ingress_id"])),
            },
            objects,
            problems,
        )
    ingress_targets_raw = ingress["target_dispositions"] if isinstance(ingress["target_dispositions"], list) else []
    if any(not isinstance(item, dict) or set(item) != {"logical_ingress_id", "target_disposition"} for item in ingress_targets_raw):
        problems.add("joint_invalid", "ingress.target_dispositions 条目字段不闭合")
        return None
    ingress_targets = {str(item["logical_ingress_id"]): str(item["target_disposition"]) for item in ingress_targets_raw}
    entry_ids = {str(item["logical_ingress_id"]) for item in entries}
    if set(ingress_targets) != entry_ids or len(ingress_targets) != len(ingress_targets_raw):
        problems.add("joint_ingress_open", "入口目标处置必须恰好覆盖完整入口清单")
    if any(value not in INGRESS_DISPOSITIONS for value in ingress_targets.values()):
        problems.add("joint_invalid", f"入口目标处置只能是 {sorted(INGRESS_DISPOSITIONS)}")
    migrated = sorted(key for key, value in ingress_targets.items() if value == "migrated_strict")
    migrated_protocols = sorted({str(item["protocol_class"]) for item in entries if item["logical_ingress_id"] in migrated})
    # 出站：观测与处置清单闭合、目标处置覆盖完整清单，目标画像的严格端点逐个以 persona_strict 绑定同一组 SPEC。
    egresses = [dict(item) for item in egress["egresses"]] if isinstance(egress["egresses"], list) else []
    egress_entries = [dict(item) for item in egress["entries"]] if isinstance(egress["entries"], list) else []
    egress_targets_raw = [dict(item) for item in egress["target_dispositions"]] if isinstance(egress["target_dispositions"], list) else []
    if (
        any(set(item) != EGRESS_FIELDS for item in egresses)
        or any(set(item) != EGRESS_ENTRY_FIELDS for item in egress_entries)
        or any(set(item) != EGRESS_TARGET_FIELDS for item in egress_targets_raw)
    ):
        problems.add("joint_invalid", "egress 的观测、清单或目标处置条目字段不闭合")
        return None
    observed_ids = {str(item["egress_id"]) for item in egresses}
    if {str(item["egress_id"]) for item in egress_entries} != observed_ids or len(egress_entries) != len(egresses):
        problems.add("joint_egress_open", "出站处置清单必须恰好覆盖出站观测")
    egress_targets = {str(item["egress_id"]): item for item in egress_targets_raw}
    if set(egress_targets) != observed_ids or len(egress_targets) != len(egress_targets_raw):
        problems.add("joint_egress_open", "出站目标处置必须恰好覆盖出站观测")
    for item in egress_targets.values():
        if item["target_disposition"] not in EGRESS_DISPOSITIONS:
            problems.add("joint_invalid", f"{item['egress_id']}：出站目标处置只能是 {sorted(EGRESS_DISPOSITIONS)}")
        elif item["target_disposition"] == "persona_strict" and set(item["spec_ids"] or []) - set(inputs.target_rules):
            problems.add("joint_egress_open", f"{item['egress_id']}：persona_strict 绑定了目标规则之外的 SPEC")
    for endpoint in endpoints:
        egress_id = str(endpoint.get("egress_id"))
        expected_specs = sorted(set(endpoint.get("spec_ids") or []))
        target_item = egress_targets.get(egress_id)
        if (
            target_item is None
            or target_item["target_disposition"] != "persona_strict"
            or sorted(set(target_item["spec_ids"] or [])) != expected_specs
            or list(target_item["spec_ids"] or []) != expected_specs
        ):
            problems.add("joint_strict_endpoint_mismatch", f"{egress_id}：目标画像的严格端点必须以 persona_strict 绑定同一组 SPEC {expected_specs}")
    for item in egress_entries:
        if item["current_disposition"] not in EGRESS_DISPOSITIONS or item["current_guard_state"] not in EGRESS_GUARD_STATES:
            problems.add("joint_invalid", f"{item['egress_id']}：出站现状处置或守卫状态不在词表内")
    egress_observation = _object(
        "egress_observation",
        "egress_observation",
        {
            "schema_version": "official-client-egress-observation/v1",
            "persona": dict(persona),
            "observed_at_utc": egress["observed_at_utc"],
            "source_refs": evidence,
            "egresses": sorted(egresses, key=lambda item: str(item["egress_id"])),
        },
        objects,
        problems,
    )
    egress_ref = None
    if egress_observation is not None:
        egress_ref = _object(
            "egress_disposition_inventory",
            "egress_disposition_inventory",
            {
                "schema_version": "official-client-egress-disposition-inventory/v1",
                "persona": dict(persona),
                "observation_ref": egress_observation,
                "entries": sorted(({**item, "runtime_assertion_refs": evidence} for item in egress_entries), key=lambda item: str(item["egress_id"])),
            },
            objects,
            problems,
        )
    # 支持范围与边界断言。
    boundary_entries = joint["boundary_assertions"]
    if not isinstance(boundary_entries, list) or not boundary_entries:
        problems.add("joint_boundary_missing", "boundary_assertions 不能为空（SupportEnvelope 必须绑定边界断言）")
        return None
    if any(not isinstance(item, dict) or set(item) != {"id", "facts"} or not isinstance(item["facts"], dict) or item["facts"].get("result") != "pass" for item in boundary_entries):
        problems.add("joint_invalid", "boundary_assertions 条目必须是 {id, facts}，且 facts.result 为 pass")
    boundary_ref = _object(
        "boundary_assertions",
        "operational_evidence",
        _manifest_payload("operational_evidence", persona, "claude-upgrade-vc3-boundary", boundary_entries),
        objects,
        problems,
    )
    support = joint["support"]
    if not isinstance(support, dict) or set(support) != {"capabilities", "unsupported_conditions"}:
        problems.add("joint_invalid", "support 只能有 capabilities 与 unsupported_conditions")
        return None
    capabilities = _capabilities(support["capabilities"], "support.capabilities", problems)
    if not capabilities:
        problems.add("joint_support_missing", "SupportEnvelope 的能力不能为空")
    if {str(item.get("logical_ingress_id")) for item in capabilities} != set(migrated):
        problems.add("joint_support_mismatch", f"SupportEnvelope 只能且必须覆盖 migrated_strict 入口：{migrated}")
    models = set((target.get("identity") or {}).get("supported_models") or [])
    for item in capabilities:
        if item.get("platform") != inputs.platform or item.get("privacy_mode") != inputs.privacy_mode or (models and item.get("model") not in models):
            problems.add("joint_support_mismatch", f"能力 {capability_key(item)!r} 的平台、隐私模式或模型不在目标身份内")
    if not _sorted_unique_strings(support["unsupported_conditions"], non_empty=False):
        problems.add("joint_invalid", "support.unsupported_conditions 必须是排序去重的文字数组")
    support_ref = None
    if ingress_ref is not None and boundary_ref is not None:
        support_ref = _object(
            "support_envelope",
            "support_envelope",
            {
                "schema_version": "official-client-support-envelope/v1",
                "persona": dict(persona),
                "capabilities": capabilities,
                "unsupported_conditions": list(support["unsupported_conditions"]) if isinstance(support["unsupported_conditions"], list) else [],
                "target_spec_ids": sorted(inputs.target_rules),
                "production_ingress_inventory_ref": ingress_ref,
                "boundary_assertion_refs": [boundary_ref],
            },
            objects,
            problems,
        )
    references: dict[str, dict[str, str] | None] = {}
    for name, kind, manifest_id in (
        ("persona_derivation", "persona_derivation", "claude-upgrade-vc3-persona-derivation"),
        ("compatibility_boundary", "compatibility_boundary", "claude-upgrade-vc3-compatibility-boundary"),
    ):
        values = joint[name]
        if not isinstance(values, list) or not values:
            problems.add("joint_manifest_missing", f"{name} 不能为空")
            references[name] = None
            continue
        if any(not isinstance(item, dict) or set(item) != {"id", "facts"} for item in values):
            problems.add("joint_invalid", f"{name} 条目必须是 {{id, facts}}")
        references[name] = _object(name, kind, _manifest_payload(kind, persona, manifest_id, values), objects, problems)
    # 场景计划：VC-2 批准场景按目标规则投影，断言取每条规则的原子断言，协议类别取 migrated_strict 入口。
    plan_scenarios = []
    for scenario in inputs.documents["scenarios"]["evidence_scenarios"]:
        specs = sorted(set(scenario["covers"]) & set(inputs.target_rules))
        if not specs:
            continue
        plan_scenarios.append(
            {
                "id": scenario["scenario_id"],
                "spec_ids": specs,
                "ingress_protocol_classes": migrated_protocols,
                "conditions": sorted({f"approval={inputs.purpose}", f"platform={inputs.platform}", f"privacy={inputs.privacy_mode}"}),
                "assertion_ids": sorted({assertion for spec in specs for assertion in inputs.owners.get(spec, [])}),
            }
        )
    plan_scenarios.sort(key=lambda item: item["id"])
    if {spec for item in plan_scenarios for spec in item["spec_ids"]} != set(inputs.target_rules):
        problems.add("joint_scenario_gap", "VC-2 批准场景没有精确覆盖目标规则全集")
    if not migrated_protocols:
        problems.add("joint_support_mismatch", "没有 migrated_strict 入口，场景计划缺少协议类别")
    scenario_ref = _object(
        "scenario_plan",
        "scenario_plan",
        {"schema_version": "official-client-scenario-plan/v1", "persona": dict(persona), "manifest_id": "claude-upgrade-vc3-scenario-plan", "scenarios": plan_scenarios},
        objects,
        problems,
    )
    rollback = _capabilities(deployment["rollback_operational_capabilities"], "deployment.rollback_operational_capabilities", problems)
    traffic = _capabilities(deployment["deployment_traffic_capabilities"], "deployment.deployment_traffic_capabilities", problems)
    deployment_ref = _object(
        "deployment_plan",
        "deployment_plan",
        {
            "schema_version": "official-client-deployment-plan/v1",
            "persona": dict(persona),
            "manifest_id": "claude-upgrade-vc3-deployment-plan",
            "active_support_capabilities": capabilities,
            "rollback_operational_capabilities": rollback,
            "deployment_traffic_capabilities": traffic,
            "rollback_target_kind": deployment["rollback_target_kind"],
            "failure_policy": deployment["failure_policy"],
        },
        objects,
        problems,
    )
    if inputs.purpose == "production_replacement":
        weak = sorted(spec for spec in inputs.target_rules if inputs.evidence_levels.get(spec) != "verified")
        if weak:
            problems.extend("evidence_level_insufficient", weak)
    refs = {
        "persona_descriptor_ref": descriptor,
        "profile_schema_ref": schema_ref,
        "snapshot_ref": snapshot_ref,
        "release_artifact_ref": release_ref,
        "support_envelope_ref": support_ref,
        "production_ingress_inventory_ref": ingress_ref,
        "egress_disposition_inventory_ref": egress_ref,
        "persona_derivation_ref": references.get("persona_derivation"),
        "compatibility_boundary_ref": references.get("compatibility_boundary"),
        "scenario_plan_ref": scenario_ref,
        "deployment_plan_ref": deployment_ref,
    }
    if any(value is None for value in refs.values()):
        return None
    approval_core = {
        "approval_purpose": inputs.purpose,
        **refs,
        "target_spec_ids": sorted(inputs.target_rules),
        "ingress_target_dispositions": [
            {"logical_ingress_id": key, "target_disposition": ingress_targets[key], "evidence_refs": evidence} for key in sorted(ingress_targets)
        ],
        "egress_target_dispositions": [
            {
                "egress_id": key,
                "target_disposition": egress_targets[key]["target_disposition"],
                "spec_ids": list(egress_targets[key]["spec_ids"] or []),
                "managed_policy": egress_targets[key]["managed_policy"],
                "evidence_refs": evidence,
            }
            for key in sorted(egress_targets)
        ],
    }
    # 用一个占位批准人预检 ProfileApprovalFact 的 payload 合同（批准人与 review_ref 在批准时给出）。
    try:
        trial = {
            "evidence_approval_ref": {"campaign_id": inputs.campaign_id, "dimension": "approval", "sequence": 1, "sha256": "0" * 64},
            **approval_core,
            "reviewer": "precheck",
            "review_ref": "precheck",
            "identity_sha256": "",
        }
        trial["identity_sha256"] = profile_approval_identity_sha256(trial)
        validate_fact_payload("profile_approved", trial)
    except ControlError as error:
        problems.add("joint_contract_invalid", f"profile_approved：{error}")
    return {"objects": objects, "approval_core": approval_core, "target_profile_sha256": target_digest}


# ---------------------------------------------------------------------------
# 门禁与审核摘要
# ---------------------------------------------------------------------------


def evaluate_prepare(inputs: ProfileInputs, files: Mapping[str, bytes]) -> dict[str, Any]:
    """准备阶段门禁：policy 只读、补丁派生与 ``profile_diff_paths`` 约束、联合制品合同与一致性。"""

    problems = _Problems()
    documents: dict[str, dict[str, Any]] = {}
    for name, filename in PREPARE_FILES.items():
        try:
            documents[name] = _parse_json_object(files[name], filename)
        except ConfigurationError as error:
            problems.add("document_invalid", str(error))
    if problems:
        return {"status": "blocked", "reasons": problems.reasons(), "counts": {}, "derived": None}
    if documents["policy"] != inputs.policy or set(documents["policy"]) != POLICY_FIELDS:
        problems.add("policy_drift", "profile-policy.json 必须逐字等于本次输入派生的阶段 Profile policy（只读）")
    derived: dict[str, Any] | None = None
    joint: dict[str, Any] | None = None
    projection: release.CatalogProjection | None = None
    staged: dict[str, Any] | None = None
    # B3-03 补充项：先按投影策略投影实现策略与模型能力目录（目录摘要进画像 identity），画像派生之后再投影 Wire 与 Release。
    try:
        projection = release.project_catalog(
            inputs.release_evidence,
            documents["projection"],
            target_version=inputs.target_version,
            required_rule_count=len(inputs.target_rules),
        )
    except release.ProjectionError as error:
        problems.add("wire_projection_failed", str(error))
    try:
        derived = derive_target_profile(inputs, documents, problems, release.model_identity(projection) if projection is not None else None)
        if derived is not None and projection is not None:
            try:
                staged = release.finish_release(
                    projection,
                    target_profile=derived["target"],
                    target_profile_bytes=derived["target_bytes"],
                    platform=inputs.platform,
                    required_rules_manifest_sha256=inputs.target_rules_sha256,
                    atomic_counts=inputs.atomic_counts,
                )
            except release.ProjectionError as error:
                problems.add("wire_projection_failed", str(error))
        if derived is not None:
            joint = build_joint(
                inputs,
                derived["target"],
                derived["target_bytes"],
                derived["derivation"],
                documents["joint_inputs"],
                {name: _sha256(files[name]) for name in PREPARE_FILES},
                problems,
                release_info=(
                    {"projection": projection, "summary": release.release_summary(projection, staged)}
                    if projection is not None and staged is not None
                    else None
                ),
            )
    except (TypeError, KeyError, AttributeError, ValueError) as error:
        problems.add("document_invalid", f"草案结构非法：{error!r}")
    counts = {
        "affected_rule_count": len(inputs.affected_rules),
        "inherited_rule_count": len(inputs.inherited_rules),
        "target_rule_count": len(inputs.target_rules),
        "version_identity_path_count": len(derived["derivation"]["version_identity_paths"]) if derived else None,
        "profile_diff_path_count": len(derived["derivation"]["profile_diff_paths"]) if derived else None,
        "unexplained_path_count": len(derived["derivation"]["unexplained_paths"]) if derived else None,
        "joint_object_count": len(joint["objects"]) if joint else None,
        "target_profile_sha256": derived["derivation"]["target_profile_sha256"] if derived else None,
        "model_identity_path_count": len(derived["derivation"]["model_identity_paths"]) if derived else None,
        "release": release.release_summary(projection, staged) if projection is not None and staged is not None else None,
    }
    return {
        "status": "blocked" if problems else "closed",
        "reasons": problems.reasons(),
        "counts": counts,
        "derived": (
            {"profile": derived, "joint": joint, "projection": projection, "release": staged}
            if derived is not None and projection is not None and staged is not None and not problems
            else None
        ),
    }


def review_sha256(inputs: ProfileInputs, revision: str, receipt: Mapping[str, Any], gate: Mapping[str, Any]) -> str:
    return _orchestrator()._fingerprint(
        {
            "schema_version": REVIEW_SCHEMA,
            "campaign_id": inputs.campaign_id,
            "revision": revision,
            "inputs_sha256": receipt["inputs_sha256"],
            "files": {name: receipt["files"][name]["sha256"] for name in PREPARE_FILES},
            "gate": {key: gate[key] for key in ("status", "reasons", "counts")},
            "tool_files_sha256": inputs.tool_files_sha256,
        }
    )


# ---------------------------------------------------------------------------
# 准备阶段 revision
# ---------------------------------------------------------------------------


def _validate_receipt(receipt: Mapping[str, Any], *, campaign_id: str, revision: str) -> None:
    if (
        set(receipt) != DRAFT_RECEIPT_FIELDS
        or receipt.get("schema_version") != DRAFT_RECEIPT_SCHEMA
        or receipt.get("campaign_id") != campaign_id
        or receipt.get("revision") != revision
        or receipt.get("origin") not in {MACHINE_DRAFT, REVIEW_INPUT}
        or not isinstance(receipt.get("files"), Mapping)
        or set(receipt["files"]) != set(PREPARE_FILES)
        or not SHA256_RE.fullmatch(str(receipt.get("inputs_sha256", "")))
    ):
        raise ConfigurationError(f"画像草案 revision {revision} 的收据不闭合或不属于本 Campaign。")


def list_revisions(campaign_dir: Path, campaign_id: str) -> list[tuple[str, dict[str, Any]]]:
    root = campaign_dir / PREPARE_DRAFT_ROOT
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"画像草案目录不可信：{root}")
    revisions: list[tuple[int, str, dict[str, Any]]] = []
    for child in root.iterdir():
        if child.name.startswith(DRAFT_TEMPORARY_PREFIX):
            continue
        match = REVISION_RE.fullmatch(child.name)
        if match is None or child.is_symlink() or not child.is_dir():
            raise ConfigurationError(f"画像草案目录含非法条目：{child.name}")
        receipt = _read_json(child / DRAFT_RECEIPT_FILENAME, f"画像草案 {child.name} 收据")
        _validate_receipt(receipt, campaign_id=campaign_id, revision=child.name)
        revisions.append((int(match.group(1)), child.name, receipt))
    return [(name, receipt) for _number, name, receipt in sorted(revisions)]


def load_revision(campaign_dir: Path, campaign_id: str, revision: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not REVISION_RE.fullmatch(str(revision)):
        raise ConfigurationError(f"revision 编号非法：{revision!r}（应为 rev-NNNN）")
    directory = campaign_dir / PREPARE_DRAFT_ROOT / revision
    if directory.is_symlink() or not directory.is_dir():
        raise ConfigurationError(f"画像草案 revision 不存在：{revision}")
    names = sorted(child.name for child in directory.iterdir())
    if names != sorted([*PREPARE_FILES.values(), DRAFT_RECEIPT_FILENAME]):
        raise ConfigurationError(f"画像草案 revision {revision} 目录内容不闭合：{names}")
    receipt = _read_json(directory / DRAFT_RECEIPT_FILENAME, f"画像草案 {revision} 收据")
    _validate_receipt(receipt, campaign_id=campaign_id, revision=revision)
    files: dict[str, bytes] = {}
    for name, filename in PREPARE_FILES.items():
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"画像草案 {revision} 的 {filename} 不是可信普通文件。")
        data = path.read_bytes()
        if {"path": filename, "sha256": _sha256(data), "bytes": len(data)} != receipt["files"][name]:
            raise ConfigurationError(f"画像草案 {revision} 的 {filename} 已被改动（revision 只写一次）：修订请走 --from-revision 与 --revision-input。")
        files[name] = data
    return receipt, files


def _publish_revision(
    campaign_dir: Path, inputs: ProfileInputs, files: Mapping[str, bytes], *, origin: str, parent: str | None
) -> tuple[str, dict[str, Any], bool]:
    root = campaign_dir / PREPARE_DRAFT_ROOT
    ensure_private_directory(root, campaign_dir)
    classify._clear_temporaries(root, DRAFT_TEMPORARY_PREFIX)
    digests = {name: {"path": PREPARE_FILES[name], "sha256": _sha256(files[name]), "bytes": len(files[name])} for name in PREPARE_FILES}
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
        "created_at_utc": _orchestrator()._utc_now(),
    }
    content = {PREPARE_FILES[name]: files[name] for name in PREPARE_FILES}
    content[DRAFT_RECEIPT_FILENAME] = _serialize(receipt)
    classify._publish_directory(root, revision, DRAFT_TEMPORARY_PREFIX, content)
    return revision, receipt, False


def _read_revision_input(directory: Path) -> dict[str, bytes]:
    path = directory if directory.is_absolute() else Path.cwd() / directory
    if path.is_symlink() or not path.is_dir():
        raise ConfigurationError(f"--revision-input 不是可信目录：{directory}")
    names = {child.name for child in path.iterdir()}
    expected = set(PREPARE_FILES.values())
    if not expected <= names or names - expected - {DRAFT_RECEIPT_FILENAME}:
        raise ConfigurationError(f"--revision-input 必须恰好包含四个草案文件 {sorted(expected)}（可另含 {DRAFT_RECEIPT_FILENAME}）。")
    files: dict[str, bytes] = {}
    for name, filename in PREPARE_FILES.items():
        file = path / filename
        if file.is_symlink() or not file.is_file():
            raise ConfigurationError(f"--revision-input 的 {filename} 不是可信普通文件。")
        files[name] = file.read_bytes()
        _parse_json_object(files[name], filename)
    return files


def _bound_active_paths(campaign_dir: Path, receipt: Mapping[str, Any]) -> tuple[Path, Path]:
    inputs = receipt.get("inputs") or {}
    catalog = inputs.get("active_release_catalog")
    profile = inputs.get("active_profile")
    if not isinstance(catalog, Mapping) or not isinstance(profile, Mapping):
        raise ConfigurationError("画像草案收据缺少 Active 输入绑定。")
    return campaign_dir / str(catalog["path"]), campaign_dir / str(profile["path"])


def _refuse_after_profile_approval(campaign_dir: Path, inputs: ProfileInputs | None = None) -> None:
    """ProfileApprovalFact 之后不再接受新的画像草案与准备批准（指南 §4.3：批准前只新建 draft revision，批准后变化建立后继 Campaign）。"""

    if (campaign_dir / STAGE_RESULT_RELATIVE).exists() or (inputs is not None and inputs.profile_approval is not None):
        raise ConfigurationError("画像已批准（ProfileApprovalFact）：批准后画像或批准闭集的变化须建立后继 Campaign（指南 §4.3.3）。")


def _summary(campaign_dir: Path, inputs: ProfileInputs, revision: str, receipt: Mapping[str, Any], files: Mapping[str, bytes], *, reused: bool, mode: str) -> dict[str, Any]:
    gate = evaluate_prepare(inputs, files)
    review = review_sha256(inputs, revision, receipt, gate)
    blocked = gate["status"] != "closed"
    derivation = (gate["derived"] or {}).get("profile", {}).get("derivation") if gate["derived"] else None
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "profile_blocked" if blocked else "awaiting_profile_preparation_approval",
        "mode": mode,
        "campaign_id": inputs.campaign_id,
        "revision": revision,
        "parent_revision": receipt["parent_revision"],
        "origin": receipt["origin"],
        "reused": reused,
        "draft_dir": str(campaign_dir / PREPARE_DRAFT_ROOT / revision),
        "inputs_sha256": inputs.sha256,
        "gate": {key: gate[key] for key in ("status", "reasons", "counts")},
        "profile_diff_paths": derivation["profile_diff_paths"] if derivation else None,
        "review_sha256": review,
        "live_request_count": 0,
        "next_command": (
            "门禁 blocked：把该 revision 的四个文件复制到一个目录逐项处置，再执行 prepare-profile --campaign-dir "
            f"{campaign_dir} --from-revision {revision} --revision-input <目录>"
            if blocked
            else f"prepare-profile --campaign-dir {campaign_dir} --approve-revision {revision} --approve-review-sha256 {review} --approved-by <批准人>"
        ),
    }


# 草案与修订：EvidenceApprovalFact 之前（evidence_recorded）或之后（official_sealed）都可以；ProfileApprovalFact 之后拒绝。
_PREPARE_STATUSES = frozenset({STORE_EVIDENCE, STORE_SEALED, STORE_APPROVED})


def _machine_draft(campaign_dir: Path, manifest: Mapping[str, Any], catalog: Any, profile: Any) -> dict[str, Any]:
    _refuse_after_profile_approval(campaign_dir)
    catalog_path, profile_path = _active_paths(campaign_dir, manifest, catalog and Path(catalog), profile and Path(profile))
    inputs = collect_inputs(campaign_dir, manifest, catalog_path, profile_path, allowed_status=_PREPARE_STATUSES)
    _refuse_after_profile_approval(campaign_dir, inputs)
    files = machine_draft_files(inputs)
    revision, receipt, reused = _publish_revision(campaign_dir, inputs, files, origin=MACHINE_DRAFT, parent=None)
    return _summary(campaign_dir, inputs, revision, receipt, files, reused=reused, mode=MACHINE_DRAFT)


def _revise(campaign_dir: Path, manifest: Mapping[str, Any], parent: str, directory: Path) -> dict[str, Any]:
    _refuse_after_profile_approval(campaign_dir)
    parent_receipt, _files = load_revision(campaign_dir, str(manifest["campaign_id"]), parent)
    catalog_path, profile_path = _bound_active_paths(campaign_dir, parent_receipt)
    inputs = collect_inputs(campaign_dir, manifest, catalog_path, profile_path, allowed_status=_PREPARE_STATUSES)
    _refuse_after_profile_approval(campaign_dir, inputs)
    if inputs.sha256 != parent_receipt["inputs_sha256"]:
        raise ConfigurationError(f"父 revision {parent} 的输入已漂移：先重新生成机器草案。")
    files = _read_revision_input(directory)
    revision, receipt, reused = _publish_revision(campaign_dir, inputs, files, origin=REVIEW_INPUT, parent=parent)
    return _summary(campaign_dir, inputs, revision, receipt, files, reused=reused, mode=REVIEW_INPUT)


# ---------------------------------------------------------------------------
# 准备阶段批准：EvidenceApprovalFact 与准备包
# ---------------------------------------------------------------------------


def _publish_files(campaign_dir: Path, root_relative: Path, prefix: str, files: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    """把一组文件整体发布到 ``root_relative``；已存在时逐个核对，不一致即拒绝。"""

    target = campaign_dir / root_relative
    parent = target.parent
    if parent.exists():
        classify._clear_temporaries(parent, prefix)
    bindings = {
        filename: {"path": (root_relative / filename).as_posix(), "sha256": _sha256(data), "bytes": len(data)} for filename, data in files.items()
    }
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir() or sorted(child.name for child in target.iterdir()) != sorted(files):
            raise ConfigurationError(f"{target} 已存在但内容不闭合：拒绝覆盖。")
        for filename, data in files.items():
            if (target / filename).read_bytes() != data:
                raise ConfigurationError(f"{target / filename} 已存在且内容不同：拒绝覆盖。")
        return bindings
    ensure_private_directory(parent, campaign_dir)
    classify._publish_directory(parent, target.name, prefix, files)
    return bindings


def _approve_prepare(campaign_dir: Path, manifest: Mapping[str, Any], revision: str, approved_review: str, approved_by: str) -> dict[str, Any]:
    orchestrator = _orchestrator()
    if not SHA256_RE.fullmatch(approved_review):
        raise ConfigurationError("--approve-review-sha256 必须是 64 位小写十六进制。")
    result_path = prepare_result_path(campaign_dir, revision)
    if result_path.exists() or result_path.is_symlink():
        previous = _read_json(result_path, "画像准备结果").get("approved_by")
        if previous != approved_by:
            raise ConfigurationError(f"准备 revision {revision} 已由 {previous} 批准：不得以不同批准人重复批准。")
    receipt, files = load_revision(campaign_dir, str(manifest["campaign_id"]), revision)
    catalog_path, profile_path = _bound_active_paths(campaign_dir, receipt)
    inputs = collect_inputs(campaign_dir, manifest, catalog_path, profile_path, allowed_status=_PREPARE_STATUSES)
    if not result_path.exists():
        # 已批准的 revision 可以逐字重放；ProfileApprovalFact 之后不再批准新的准备 revision。
        _refuse_after_profile_approval(campaign_dir, inputs)
    if inputs.sha256 != receipt["inputs_sha256"]:
        raise ConfigurationError(f"revision {revision} 绑定的输入已漂移，禁止批准：重新生成机器草案并审核。")
    gate = evaluate_prepare(inputs, files)
    current = review_sha256(inputs, revision, receipt, gate)
    if approved_review != current:
        raise ConfigurationError("批准摘要与该 revision 当前的 review_sha256 不一致：先不带批准参数重跑草案调用复核。")
    if gate["status"] != "closed" or gate["derived"] is None:
        raise ConfigurationError("画像准备门禁 blocked，禁止批准：" + "、".join(reason["code"] for reason in gate["reasons"]))
    orchestrator._require_tool_evolution_registered(campaign_dir, manifest, current=orchestrator._tool_identity(include_git=False), action="prepare-profile 批准")
    session = claude_upgrade_seal._StoreSession(inputs.store_root, inputs.store_campaign_id, campaign_dir / STORE_SCRATCH_RELATIVE)
    bound = {
        "evidence_fact_ref": dict(inputs.evidence_fact_ref),
        "evidence_package_ref": dict(inputs.evidence_package_ref),
        "classification_fact_ref": dict(inputs.classification_fact_ref),
    }
    if inputs.evidence_approval is not None:
        # EvidenceApprovalFact 每个 Campaign 只有一份，批准的是 VC-1 证据与 VC-2 分类的组合，与画像 revision 无关：
        # 第一次准备批准时签发，之后的准备批准（新的草案 revision）沿用它。
        if any(inputs.evidence_approval["payload"].get(key) != value for key, value in bound.items()):
            raise ConfigurationError("Store 已有的 EvidenceApprovalFact 没有绑定同一份 VC-1 证据与 VC-2 分类事实。")
        evidence_approval_ref, evidence_reused = dict(inputs.evidence_approval_ref or {}), True
    else:
        payload = {
            **bound,
            "reviewer": approved_by,
            "review_ref": f"claude-upgrade-vc3-evidence:{inputs.classification['review_sha256']}",
        }
        fact = session.record_fact(
            "evidence-approve", "evidence_approved", payload, issued_at=str(receipt["created_at_utc"]), logical_action=EVIDENCE_APPROVE_ACTION
        )
        evidence_approval_ref, evidence_reused = dict(fact["fact_ref"]), bool(fact["reused"])
    status = session.status()
    if status.get("checkpoint") not in {STORE_SEALED, STORE_APPROVED}:
        raise ConfigurationError(f"EvidenceApprovalFact 之后 Store 检查点应为 {STORE_SEALED}，实际 {status.get('checkpoint')}。")
    derived = gate["derived"]["profile"]
    prepared = {PREPARE_FILES[name]: files[name] for name in PREPARE_FILES}
    prepared.update(_prepared_extras(inputs, gate["derived"]))
    bindings = _publish_files(campaign_dir, PREPARED_ROOT / revision, PREPARED_TEMPORARY_PREFIX, prepared)
    draft_receipt = campaign_dir / PREPARE_DRAFT_ROOT / revision / DRAFT_RECEIPT_FILENAME
    result_payload = {
        "schema_version": PREPARE_RESULT_SCHEMA,
        "client": "claude",
        "campaign_id": inputs.campaign_id,
        "stage": "prepare-profile",
        "status": "complete",
        "revision": revision,
        "review_sha256": current,
        "approved_by": approved_by,
        "draft_receipt": {"path": draft_receipt.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(draft_receipt)},
        "inputs_sha256": inputs.sha256,
        "prepared": bindings,
        "target_profile_sha256": derived["derivation"]["target_profile_sha256"],
        "profile_diff_paths": derived["derivation"]["profile_diff_paths"],
        "release": release.release_summary(gate["derived"]["projection"], gate["derived"]["release"]),
        # 投影工具在受管树之外：批准时的文件摘要留档（审核与重放核对用，不进审核摘要）。
        "projection_tool_sha256": file_sha256(Path(release.generator.__file__)),
        "store": {
            "root": claude_upgrade_store.CAMPAIGN_STORE_DIRNAME,
            "campaign_id": inputs.store_campaign_id,
            "checkpoint": STORE_SEALED,
            "evidence_fact_ref": dict(inputs.evidence_fact_ref),
            "evidence_package_ref": dict(inputs.evidence_package_ref),
            "classification_fact_ref": dict(inputs.classification_fact_ref),
            "evidence_approval_fact_ref": evidence_approval_ref,
        },
        "tool_files_sha256": inputs.tool_files_sha256,
        "live_request_count": 0,
        "approved_at_utc": orchestrator._utc_now(),
    }
    ensure_private_directory(result_path.parent, campaign_dir)
    stage = claude_upgrade_seal._write_once_or_reuse(result_path, result_payload, ignore=("approved_at_utc",))
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "profile_prepared",
        "mode": "approve",
        "campaign_id": inputs.campaign_id,
        "revision": revision,
        "review_sha256": current,
        "store": {**stage["store"], "evidence_approval_reused": evidence_reused},
        "prepare_result": str(result_path),
        "target_profile_sha256": stage["target_profile_sha256"],
        "profile_diff_path_count": len(stage["profile_diff_paths"]),
        "live_request_count": 0,
        "next_command": f"stage-profile --campaign-dir {campaign_dir}",
    }


def _prepared_extras(inputs: ProfileInputs, derived: Mapping[str, Any]) -> dict[str, bytes]:
    """准备包里由批准 revision 确定性派生或复制的文件（Active 副本、目标画像与派生收据、模型能力目录、Wire、Release 条目）。"""

    profile = derived["profile"]
    staged = derived["release"]
    return {
        PREPARED_EXTRA_FILES["active_release_catalog"]: inputs.active_catalog_bytes,
        PREPARED_EXTRA_FILES["active_profile"]: inputs.active_profile_bytes,
        PREPARED_EXTRA_FILES["target_profile"]: profile["target_bytes"],
        PREPARED_EXTRA_FILES["derivation"]: _serialize(profile["derivation"]),
        PREPARED_EXTRA_FILES["model_catalog"]: derived["projection"].catalog_bytes,
        PREPARED_EXTRA_FILES["target_wire"]: staged["wire_bytes"],
        PREPARED_EXTRA_FILES["release_entry"]: staged["entry_bytes"],
    }


def prepare_result_path(campaign_dir: Path, revision: str) -> Path:
    return campaign_dir / PREPARE_RESULTS_ROOT / f"{revision}.json"


def approved_prepare_revisions(campaign_dir: Path) -> list[str]:
    """已批准的准备 revision，按编号排序（写入中的临时文件以点开头，跳过）。"""

    root = campaign_dir / PREPARE_RESULTS_ROOT
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"画像准备结果目录不可信：{root}")
    revisions: list[tuple[int, str]] = []
    for child in root.iterdir():
        if child.name.startswith("."):
            continue
        stem = child.name[: -len(".json")] if child.name.endswith(".json") else ""
        match = REVISION_RE.fullmatch(stem)
        if match is None or child.is_symlink() or not child.is_file():
            raise ConfigurationError(f"画像准备结果目录含非法条目：{child.name}")
        revisions.append((int(match.group(1)), stem))
    return [name for _number, name in sorted(revisions)]


def load_prepare_result(campaign_dir: Path, manifest: Mapping[str, Any], revision: str | None = None) -> dict[str, Any]:
    """读取并核对画像准备结果（缺省取最新批准的 revision）：合同、Campaign、状态与准备包全部文件的摘要。"""

    revisions = approved_prepare_revisions(campaign_dir)
    if not revisions:
        raise ConfigurationError("没有已批准的画像准备结果：先完成 prepare-profile 批准。")
    chosen = revision if revision is not None else revisions[-1]
    if chosen not in revisions:
        raise ConfigurationError(f"画像准备 revision {chosen} 没有批准结果。")
    result = _read_json(prepare_result_path(campaign_dir, chosen), "画像准备结果")
    if result.get("revision") != chosen:
        raise ConfigurationError("画像准备结果的 revision 与文件名不一致。")
    if (
        result.get("schema_version") != PREPARE_RESULT_SCHEMA
        or result.get("campaign_id") != manifest.get("campaign_id")
        or result.get("status") != "complete"
        or not isinstance(result.get("prepared"), Mapping)
        or set(result["prepared"]) != {*PREPARE_FILES.values(), *PREPARED_EXTRA_FILES.values()}
    ):
        raise ConfigurationError("画像准备结果的合同、Campaign、状态或准备包清单不符。")
    for filename, binding in result["prepared"].items():
        path = campaign_dir / str(binding.get("path", ""))
        if path.is_symlink() or not path.is_file() or file_sha256(path) != binding.get("sha256"):
            raise ConfigurationError(f"准备包文件 {filename} 缺失或摘要漂移。")
    return result


# ---------------------------------------------------------------------------
# 暂存阶段：联合制品预览与 ProfileApprovalFact
# ---------------------------------------------------------------------------


def _prepared_files(campaign_dir: Path, result: Mapping[str, Any]) -> dict[str, bytes]:
    return {filename: (campaign_dir / str(binding["path"])).read_bytes() for filename, binding in result["prepared"].items()}


def _stage_context(
    campaign_dir: Path, manifest: Mapping[str, Any], allowed: frozenset[str]
) -> tuple[ProfileInputs, dict[str, Any], dict[str, Any], str, dict[str, Any]]:
    """暂存阶段的输入：重核准备包与全部 VC-2／Store 输入，从准备包复算联合制品（必须仍然闭合）；末项是复算出的派生结果。"""

    result = load_prepare_result(campaign_dir, manifest)
    prepared = _prepared_files(campaign_dir, result)
    catalog_path = campaign_dir / str(result["prepared"][PREPARED_EXTRA_FILES["active_release_catalog"]]["path"])
    profile_path = campaign_dir / str(result["prepared"][PREPARED_EXTRA_FILES["active_profile"]]["path"])
    receipt = _read_json(campaign_dir / str(result["draft_receipt"]["path"]), "被批准画像草案收据")
    if file_sha256(campaign_dir / str(result["draft_receipt"]["path"])) != result["draft_receipt"]["sha256"]:
        raise ConfigurationError("被批准画像草案收据摘要漂移。")
    original_catalog, original_profile = _bound_active_paths(campaign_dir, receipt)
    for original, copy_path in ((original_catalog, catalog_path), (original_profile, profile_path)):
        if file_sha256(original) != file_sha256(copy_path):
            raise ConfigurationError(f"Active 输入在准备批准后漂移：{original}")
    inputs = collect_inputs(campaign_dir, manifest, original_catalog, original_profile, allowed_status=allowed)
    if inputs.sha256 != result["inputs_sha256"]:
        raise ConfigurationError("VC-3 输入在准备批准后漂移：批准后变化须建立后继 Campaign。")
    if inputs.evidence_approval is None:
        raise ConfigurationError("Store 没有 EvidenceApprovalFact：先完成 prepare-profile 批准。")
    files = {name: prepared[PREPARE_FILES[name]] for name in PREPARE_FILES}
    gate = evaluate_prepare(inputs, files)
    if gate["status"] != "closed" or gate["derived"] is None:
        raise ConfigurationError("准备包按当前工具复算不再闭合：" + "、".join(reason["code"] for reason in gate["reasons"]))
    derived = gate["derived"]
    if derived["profile"]["target_bytes"] != prepared[PREPARED_EXTRA_FILES["target_profile"]]:
        raise ConfigurationError("准备包的目标画像无法由 Active 与批准补丁复算。")
    if _serialize(derived["profile"]["derivation"]) != prepared[PREPARED_EXTRA_FILES["derivation"]]:
        raise ConfigurationError("准备包的派生收据（profile_diff_paths）无法复算。")
    for name, data in _prepared_extras(inputs, derived).items():
        if prepared.get(name) != data:
            raise ConfigurationError(f"准备包的 {name} 无法由 VC-1 证据与批准的投影策略复算。")
    # 暂存代号绑定被采用的准备结果（revision 与摘要）和工具身份：换了准备 revision 或工具就是新的一代预览。
    generation = "g-" + _orchestrator()._fingerprint(
        {"prepare_result": _prepare_binding(campaign_dir, result), "tool_files_sha256": inputs.tool_files_sha256}
    )[:16]
    return inputs, result, derived["joint"], generation, derived


def _prepare_binding(campaign_dir: Path, result: Mapping[str, Any]) -> dict[str, str]:
    path = prepare_result_path(campaign_dir, str(result["revision"]))
    return {"path": path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(path)}


def _joint_sha256(objects: Sequence[Mapping[str, Any]], approval_core: Mapping[str, Any]) -> str:
    return _orchestrator()._fingerprint(
        {"objects": [{"name": item["name"], "kind": item["kind"], "ref": item["ref"]} for item in objects], "approval_core": dict(approval_core)}
    )


def _stage_preview(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    if (campaign_dir / STAGE_RESULT_RELATIVE).exists():
        raise ConfigurationError("画像已批准（profile/result.json 已存在）：批准后变化须建立后继 Campaign。")
    inputs, result, joint, generation, _derived = _stage_context(campaign_dir, manifest, frozenset({STORE_SEALED, STORE_APPROVED}))
    if inputs.profile_approval is not None:
        raise ConfigurationError("Store 已记录 ProfileApprovalFact 而阶段结果未写：重跑原批准命令（带 --approve-joint-sha256）收敛，不再生成预览。")
    stage_dir = campaign_dir / STAGE_ROOT / generation
    joint_sha = _joint_sha256(joint["objects"], joint["approval_core"])
    manifest_payload = {
        "schema_version": JOINT_MANIFEST_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "generation": generation,
        "prepare_result": _prepare_binding(campaign_dir, result),
        "objects": joint["objects"],
        "approval_core": joint["approval_core"],
        "target_profile_sha256": joint["target_profile_sha256"],
        "joint_sha256": joint_sha,
        "created_at_utc": _orchestrator()._utc_now(),
    }
    manifest_bytes = _serialize(manifest_payload)
    preview = {
        "schema_version": STAGE_PREVIEW_SCHEMA,
        "campaign_id": inputs.campaign_id,
        "generation": generation,
        "joint_manifest": {"path": (STAGE_ROOT / generation / JOINT_MANIFEST_FILENAME).as_posix(), "sha256": _sha256(manifest_bytes)},
        "joint_sha256": joint_sha,
        "object_count": len(joint["objects"]),
    }
    if stage_dir.exists():
        existing = _read_json(stage_dir / JOINT_MANIFEST_FILENAME, "联合清单")
        if {key: value for key, value in existing.items() if key != "created_at_utc"} != {
            key: value for key, value in manifest_payload.items() if key != "created_at_utc"
        }:
            raise ConfigurationError("同一代号的联合清单内容不同（准备包或工具在预览后被改动）。")
        preview = _read_json(stage_dir / STAGE_PREVIEW_FILENAME, "暂存预览")
    else:
        ensure_private_directory(campaign_dir / STAGE_ROOT, campaign_dir)
        classify._clear_temporaries(campaign_dir / STAGE_ROOT, STAGE_TEMPORARY_PREFIX)
        classify._publish_directory(
            campaign_dir / STAGE_ROOT,
            generation,
            STAGE_TEMPORARY_PREFIX,
            {JOINT_MANIFEST_FILENAME: manifest_bytes, STAGE_PREVIEW_FILENAME: _serialize(preview)},
        )
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "awaiting_profile_approval",
        "mode": "stage_preview",
        "campaign_id": inputs.campaign_id,
        "generation": generation,
        "joint_sha256": preview["joint_sha256"],
        "object_count": preview["object_count"],
        "target_profile_sha256": joint["target_profile_sha256"],
        "stage_dir": str(stage_dir),
        "live_request_count": 0,
        "next_command": f"stage-profile --campaign-dir {campaign_dir} --approve-joint-sha256 {preview['joint_sha256']} --approved-by <批准人>",
    }


def _stage_approve(campaign_dir: Path, manifest: Mapping[str, Any], approved_joint: str, approved_by: str) -> dict[str, Any]:
    orchestrator = _orchestrator()
    if not SHA256_RE.fullmatch(approved_joint):
        raise ConfigurationError("--approve-joint-sha256 必须是 64 位小写十六进制。")
    inputs, result, joint, generation, derived = _stage_context(campaign_dir, manifest, frozenset({STORE_SEALED, STORE_APPROVED}))
    stage_dir = campaign_dir / STAGE_ROOT / generation
    if not (stage_dir / STAGE_PREVIEW_FILENAME).is_file():
        raise ConfigurationError("当前准备包与工具身份没有冻结的暂存预览：先不带批准参数运行 stage-profile。")
    preview = _read_json(stage_dir / STAGE_PREVIEW_FILENAME, "暂存预览")
    manifest_path = stage_dir / JOINT_MANIFEST_FILENAME
    if file_sha256(manifest_path) != preview["joint_manifest"]["sha256"]:
        raise ConfigurationError("联合清单在预览后被改动。")
    frozen = _read_json(manifest_path, "联合清单")
    if frozen["joint_sha256"] != _joint_sha256(joint["objects"], joint["approval_core"]) or frozen["objects"] != joint["objects"]:
        raise ConfigurationError("联合清单与准备包复算的联合制品不一致。")
    if approved_joint != frozen["joint_sha256"]:
        raise ConfigurationError("批准摘要与暂存预览的 joint_sha256 不一致。")
    orchestrator._require_tool_evolution_registered(campaign_dir, manifest, current=orchestrator._tool_identity(include_git=False), action="stage-profile 批准")
    approval = {
        "evidence_approval_ref": dict(result["store"]["evidence_approval_fact_ref"]),
        **frozen["approval_core"],
        "reviewer": approved_by,
        "review_ref": f"claude-upgrade-vc3-joint:{generation}:{frozen['joint_sha256']}",
        "identity_sha256": "",
    }
    approval["identity_sha256"] = profile_approval_identity_sha256(approval)
    if inputs.profile_approval is not None and inputs.profile_approval["payload"] != approval:
        raise ConfigurationError("Store 已记录另一份 ProfileApprovalFact：同一 Campaign 不得覆盖，批准后变化须建立后继 Campaign。")
    session = claude_upgrade_seal._StoreSession(inputs.store_root, inputs.store_campaign_id, campaign_dir / STORE_SCRATCH_RELATIVE)
    for item in frozen["objects"]:
        if session.seal_object(item["kind"], item["payload"]) != item["ref"]:
            raise ConfigurationError(f"Store 封存 {item['name']} 得到的引用与联合清单不一致。")
    fact = session.record_fact(
        "profile-approve", "profile_approved", approval, issued_at=str(frozen["created_at_utc"]), logical_action=PROFILE_APPROVE_ACTION
    )
    status = session.status()
    if status.get("checkpoint") != STORE_APPROVED:
        raise ConfigurationError(f"ProfileApprovalFact 之后 Store 检查点应为 {STORE_APPROVED}，实际 {status.get('checkpoint')}。")
    core = frozen["approval_core"]
    # B3-03 补充项：暂存 Release（未入库的 Profile／Wire／Release 条目／投影策略，布局镜像仓库 catalogdata），VC-4 纳入候选源码树。
    staged = derived["release"]
    catalog_refs = [item["ref"] for item in frozen["objects"] if item["kind"] == release.CATALOG_OBJECT_KIND]
    if len(catalog_refs) != 1:
        raise ConfigurationError("联合清单没有唯一的模型能力目录对象。")
    release_files = release.staged_release_files(
        entry=staged["entry"],
        profile_bytes=derived["profile"]["target_bytes"],
        wire_bytes=staged["wire_bytes"],
        entry_bytes=staged["entry_bytes"],
        policy_bytes=_prepared_files(campaign_dir, result)[PREPARE_FILES["projection"]],
    )
    release_receipt = release.publish_staged_release(
        campaign_dir,
        release_files,
        release.staged_release_receipt(
            campaign_id=inputs.campaign_id,
            target_version=inputs.target_version,
            platform=inputs.platform,
            generation=generation,
            joint_sha256=frozen["joint_sha256"],
            entry=staged["entry"],
            files=release_files,
            store_refs={
                "profile_approval_fact_ref": fact["fact_ref"],
                "release_artifact_ref": core["release_artifact_ref"],
                "support_envelope_ref": core["support_envelope_ref"],
                "model_capability_catalog_ref": catalog_refs[0],
            },
        ),
    )
    release_receipt_path = campaign_dir / release.STAGED_RELEASE_ROOT / release.STAGED_RECEIPT_FILENAME
    stage_path = campaign_dir / STAGE_RESULT_RELATIVE
    stage_payload = {
        "schema_version": STAGE_RESULT_SCHEMA,
        "client": "claude",
        "campaign_id": inputs.campaign_id,
        "stage": "stage-profile",
        "status": "complete",
        "generation": generation,
        "joint_sha256": frozen["joint_sha256"],
        "prepare_result": _prepare_binding(campaign_dir, result),
        "prepared_revision": str(result["revision"]),
        "joint_manifest": {"path": manifest_path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(manifest_path)},
        "target_profile_sha256": frozen["target_profile_sha256"],
        "staged_release": {
            "path": release_receipt_path.relative_to(campaign_dir).as_posix(),
            "sha256": file_sha256(release_receipt_path),
            "release_sha256": release_receipt["release_entry"]["release_sha256"],
        },
        "store": {
            "root": claude_upgrade_store.CAMPAIGN_STORE_DIRNAME,
            "campaign_id": inputs.store_campaign_id,
            "checkpoint": STORE_APPROVED,
            "evidence_approval_fact_ref": dict(result["store"]["evidence_approval_fact_ref"]),
            "profile_approval_fact_ref": dict(fact["fact_ref"]),
            "model_capability_catalog_ref": dict(catalog_refs[0]),
            **{key: dict(core[key]) for key in core if key.endswith("_ref")},
        },
        "approval_purpose": core["approval_purpose"],
        "target_rules": list(core["target_spec_ids"]),
        "affected_rules": list(inputs.affected_rules),
        "inherited_rules": list(inputs.inherited_rules),
        "execute_item_ids": [],
        "reuse_item_ids": list(inputs.inherited_rules),
        "live_request_count": 0,
        "scanned_bytes": 0,
        "approved_at_utc": orchestrator._utc_now(),
    }
    stage = claude_upgrade_seal._write_once_or_reuse(stage_path, stage_payload, ignore=("approved_at_utc",))
    checkpoint = claude_upgrade_vc.complete_campaign_phase(
        campaign_dir,
        manifest,
        phase="VC-3",
        stage_receipt_path=stage_path,
        completed_at_utc=str(stage["approved_at_utc"]),
        execute_item_ids=[],
        reuse_item_ids=list(inputs.inherited_rules),
        live_request_count=0,
        scanned_bytes=0,
    )
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "complete",
        "mode": "stage_approve",
        "campaign_id": inputs.campaign_id,
        "generation": generation,
        "joint_sha256": frozen["joint_sha256"],
        "store": {**stage["store"], "profile_approval_reused": fact["reused"]},
        "stage_result": str(stage_path),
        "vc3_checkpoint": str(claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-3")),
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "target_profile_sha256": stage["target_profile_sha256"],
        "staged_release": dict(stage["staged_release"]),
        "live_request_count": 0,
    }


def load_stage_result(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """读取并核对 VC-3 阶段结果（交给 B3-01 的 ``_load_stage_result`` 与 B3-04 的候选构建）。"""

    stage = _read_json(campaign_dir / STAGE_RESULT_RELATIVE, "画像阶段结果")
    if (
        stage.get("schema_version") != STAGE_RESULT_SCHEMA
        or stage.get("campaign_id") != manifest.get("campaign_id")
        or stage.get("status") != "complete"
    ):
        raise ConfigurationError("画像阶段结果的合同、Campaign 或状态不符。")
    revision = stage.get("prepared_revision")
    if not isinstance(revision, str) or REVISION_RE.fullmatch(revision) is None:
        raise ConfigurationError("画像阶段结果没有合法的 prepared_revision。")
    # ProfileApprovalFact 之后不再批准新的准备 revision，所以被暂存的必须就是最新批准的那一份。
    if approved_prepare_revisions(campaign_dir)[-1:] != [revision]:
        raise ConfigurationError(f"画像阶段结果采用的准备 revision {revision} 不是最新批准的一份：准备结果目录被改动。")
    expected_prepare = prepare_result_path(campaign_dir, revision).relative_to(campaign_dir).as_posix()
    for key in ("prepare_result", "joint_manifest"):
        binding = stage[key]
        path = campaign_dir / str(binding["path"])
        if path.is_symlink() or not path.is_file() or file_sha256(path) != binding["sha256"]:
            raise ConfigurationError(f"画像阶段结果绑定的 {key} 缺失或摘要漂移。")
    if stage["prepare_result"]["path"] != expected_prepare:
        raise ConfigurationError("画像阶段结果绑定的准备结果路径与 prepared_revision 不一致。")
    load_prepare_result(campaign_dir, manifest, revision)
    staged = release.load_staged_release(campaign_dir, stage.get("staged_release") or {})
    if staged["release_entry"]["release_sha256"] != stage["staged_release"]["release_sha256"] or staged["joint_sha256"] != stage["joint_sha256"]:
        raise ConfigurationError("暂存 Release 收据与画像阶段结果登记的 Release 或联合摘要不一致。")
    return stage
