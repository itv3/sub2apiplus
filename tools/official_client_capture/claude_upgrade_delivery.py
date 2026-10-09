"""Claude 候选交付（VC-6）编排：四阶段交付、交付恢复段 dr<k>、经批准的阶段失效与交付终态
（Claude 升级控制面设计 B4-07 编排器部分；Store 侧合同见 ``official_client_control`` 的 B4-07 门禁 ①～⑧、
``delivery-chain`` 与候选交付收据 v3）。

一、边界
========
* 输入只认 VC-5 已在 Store 形成的终态：当前候选 revision、当前评估基线下最后一条 ``validation_completed``，再沿它取
  AcceptanceFact、CandidateEvidencePackage、``candidate_frozen`` 与 CandidateBuildReceipt。编排器不重算 VC-5，也不调用
  accept 门禁（B4-06）的代码；B4-06 如另定「VC-6 交接清单」，只需改 ``_vc5_handoff`` 一处。
* 编排器侧再重放 VC-5 的封存链（第八节）：交付只在 VC-5 checkpoint、accept 阶段结果与 Store 交接三者一致时进行。
* 计时账本必须是当前 revision 的 VC-5 已完成、VC-6 进行中（VC-6 由批次层开始）；交付写入一律在这个前提下进行。
* 交付写入只经父批次（第九节）：Formal Campaign 里 ``deliver-candidate run`` 与带批准的 ``finalize`` 只能由
  ``compile-and-run-vc-batch`` 派发；不带批准的 ``finalize`` 只看审核内容（零写入），``delivery-stage-invalidate`` 是批次
  之间的控制面命令，两者可以直接执行。ARM64 隔离候选环境的真实执行器是 B4-07 遗留项，另行排期（第二节）；生产激活属
  B4-08 生产分支（另批）。

二、交付环境执行器
==================
四阶段的环境动作（切到候选镜像、实际回退、恢复候选、稳定观察；任一阶段失败立即恢复回退镜像）由交付环境执行器完成，
编排器只做冻结、核对与登记。执行器按名字登记在 ``ENVIRONMENT_EXECUTORS``（名字 → 无参工厂），``--environment-executor``
选择；生产环境目前不登记任何执行器，选了即报「待落实」。执行器对象提供两个方法：

* ``freeze(request)``：只读冻结当前环境，返回环境身份 6 项（``host_id``、``architecture``、``compose_sha256``、
  ``environment_sha256``、``configuration_sha256``、``network_sha256``）、真实回退点（``rollback.image_digest``、
  ``rollback.runtime_profile_sha256``）、交付门禁结果（``gates``，id 闭集是 ``DELIVERY_GATES``，设计 §4.6.2：镜像身份、
  Compose 差异、启动与 health、依赖连通、入口路由、秘密扫描、证据 inventory、回退可用性、观察窗条件）、回退材料
  （``rollback_material``）与候选部署配置（``deployment_configuration``）。
* ``run_stage(request)``：执行一个阶段并返回观测：``result``（pass／failed）、实际环境身份、实际镜像与运行 Profile、
  逐项检查（id 闭集是冻结计划的 ``stage_checks[stage]``）、``observed_seconds``、失败时可给 ``root_cause_id``，以及
  可选的私有文件（``private_files``：``[{name, content}]``）。``live_request_count`` 必须为 0：交付 smoke 的请求入账随真实
  执行器一起设计，在那之前执行器不得发模型请求。

执行器说 pass 但编排器核对不符（环境身份漂移、镜像或 Profile 不是计划值、检查未全过、观察窗不足）一律按失败登记，
根因分别为 ``delivery-environment-drift`` 与 ``delivery-<阶段>-verification``；执行器自己判失败时根因取它给出的值，缺省
``delivery-<阶段>``。

三、``deliver-candidate run``
=============================
一次调用把当前 delivery 推进到下一个停点，重跑同一条命令即接着跑：

1. 只读解析与补账（第六节），当前 delivery 已签收据即返回 ``finalized``；
2. 执行器 ``freeze``：交付门禁有未通过项即停在 ``delivery_gates_failed``（零写入）；
3. 选 delivery：命名 ``<候选>-d<n>``。还没有 delivery，或环境身份 6 项、回退镜像、回退 Profile 任一与当前计划不同，就开
   新 delivery（门禁 ⑤：环境变化不能在同一 delivery 内恢复）；旧 delivery 的最新段还在推进时，先把它的下一阶段以
   ``delivery-environment-drift`` 记为失败、收口（只写失败事实，不调用执行器）。新 delivery 先封存环境冻结证据、回退
   材料与交付计划（``candidate_delivery_plan``，同内容复用）；
4. 按 ``delivery-chain`` 决定下一步：最新段已收口且是失败，或有待消费的已批准失效，就 ``delivery-recovery-reserve``
   预约 dr<k>（failure 锚点从失败阶段起跑，approved_invalidation 锚点从被失效的阶段起跑，``previous_valid_stage_ref``
   取有效链上起点前一阶段的 pass，``environment_before`` 取本次冻结）；否则沿最新段继续；
5. 逐阶段 ``run_stage`` → 写私有文件 → 封阶段证据清单 → ``candidate-delivery-record``（事实 v2，带幂等键）；
6. 停点：四阶段有效链全部通过且没有待消费失效 → ``awaiting_finalize``（退出 2，附交付包审核摘要）；某阶段失败 →
   ``stage_failed``（退出 3）。同根因失败达到计时账本冻结上限时，在任何 Store 写入之前拒绝。

``--rerun-from`` 只用于核对：必须等于第 4 步推导出的起点，否则拒绝（已通过的阶段要重跑，必须先 delivery-stage-invalidate）。

四、``deliver-candidate finalize``
==================================
不带 ``--approve-sha256`` 时只输出交付包审核内容（零写入，退出 2；已签收据时附交付阶段结果）；带上且与审核摘要相同才
封存私有归档清单、部署配置与交付包（同内容复用），再 ``candidate-delivery-finalize --allow-existing`` 签发
``ready_for_operator_release`` 收据，补账后写交付阶段结果与（validation_only）VC-6 checkpoint（第八节）。交付包内容由
Store 交付链与证据确定性推出，所以重复 finalize 得到同一交付包，Store 返回原收据（``reused``），阶段结果与 checkpoint
逐字复用。交付包绑定有效链四阶段的最终 pass；公开证据索引取这四个阶段的证据清单；私有归档清单收录本 delivery 全部阶段
（含失败阶段）的私有文件。

五、``delivery-stage-invalidate preview|apply``
===============================================
只在 delivery 未 finalize、最新段已收口、没有待消费失效、被失效阶段在当前有效链上时允许。preview 零写入：证据清单的
对象引用按内容现算（不封存），审核摘要由 Store 同名命令 ``--preview`` 给出；apply 核对批准摘要后封存证据清单，转调 Store
``delivery-stage-invalidate --approve-sha256 --allow-existing``，再记账本。重跑 apply 命中同一份待消费失效即复用。批次控制面
命令（与 invalidate-candidate、evaluation-recover 同类：批准一份恢复决定，不动环境、零请求，真正的重跑在下一个 run
批次里）：父监督器（campaign-run）上下文里由 ``claude_upgrade_batch.CONTROL_PLANE_COMMANDS`` 统一拒绝。

六、计时账本：先 Store 后账本
=============================
四个交付事件（``codex_upgrade_timing_ledger.DELIVERY_EVENT_TYPES``）全部由 Store 交付链确定性推出：每个已消费或待消费的
阶段失效一条 ``delivery_stage_invalidated``，每个 dr<k> 一条 ``delivery_recovery_started``，段收口时一条 completed／failed。
每次 Store 写入之后、以及每条命令开头都按 Store 补齐账本缺的事件（``_catch_up_ledger``，事件 ID 确定），所以两者之间被
强杀后重跑同一条命令即收敛；账本里已有的交付事件必须与推导结果的前缀逐项一致，否则拒绝。首轮交付（dr0）的失败不单独
记账，锚定它的 dr1 在 started 里补计一次同根因失败。

七、并发
========
一条命令全程持交付级排他锁（``control/delivery/.delivery.lock``，非阻塞，另一个交付命令在跑即拒绝）；每一步 Store 写入与
账本补账持 Campaign 锁。

八、VC-6 入口重放、交付阶段结果与 VC-6 checkpoint（B4-07 收尾）
================================================================
* 入口重放（每条交付命令在任何写入之前，设计 §4.6.1「进入前重放 VC-5 完成事件与 AcceptanceFact 并核对 VC-4／VC-5 同一
  条链」）：重放当前 revision 的 VC-5 checkpoint（逐级回溯，前序就是同 revision 的 VC-4 checkpoint）；读它绑定的 accept
  阶段结果（``claude_upgrade_acceptance.load_stage_result`` 读侧核对），revision、用途与 ``production_state`` 一致，结果登记
  的 ``validation_completed_ref``、``acceptance_ref``、``candidate_evidence_package_ref`` 必须就是 Store 交接取到的那几条；
  accept 证据封印登记的构建收据必须就是 VC-4 checkpoint 绑定的那份，构建收据的候选与镜像 digest（``image.manifest_digest``）
  与 Store 交接一致。任一不符零写入拒绝。
* 交付阶段结果 ``delivery/<候选>/result.json``（``claude-upgrade-delivery/v1``）：finalize 签发收据、补账之后写，两种用途都写
  （设计 §4.6.1 的「同一候选交付终点」）；绑定交付编号、交付计划、交付包、Store 交付收据、VC-5 checkpoint、accept 结果与
  VC-4 构建收据，``release_state=ready_for_operator_release``、``production_state=not_activated``。只写一次，重跑逐字复用
  （完成时刻除外）。
* validation_only：紧接着以交付阶段结果为阶段收据写 VC-6 checkpoint（``claude_upgrade_vc.complete_candidate_phase``，执行项与
  复用项为空、请求与扫描为 0，与 Codex VC-6 同一口径），``vc6_status=complete``。计时账本的 ``stage_completed`` 不在这里写：
  finalize 批次父 run 成功、checkpoint 已存在时由批次层（``upgrade_control_plane._complete_timing_ledger_phase_after_batch``）补写。
* production_replacement：只写交付阶段结果，不写 VC-6 checkpoint，``vc6_status=production_activation_pending``；VC-6 的生产激活
  （Catalog 转换、构建发布、部署与验收、DeploymentFact、ProductionAcceptance）属 B4-08 生产分支（另批），落地时以生产完成收据
  绑定这份交付结果再写 VC-6 checkpoint。账本 VC-6 保持进行中。

九、VC-6 批次接入（B4-07 收尾）
=============================
* 动作计划（``run_action``／``finalize_action``／``vc6_action_plan``）：两批分开派发（批准摘要要人审）。run 批次的动作
  ``vc6-01-deliver-run`` 命令为 ``deliver-candidate run --campaign-dir --candidate-id --environment-executor
  --observation-window-seconds``（不带只作核对的 ``--rerun-from``：逐字重派到段中途时它必然被拒），动作超时取观察窗加
  ``RUN_TIMEOUT_MARGIN_SECONDS``；finalize 批次的动作 ``vc6-02-deliver-finalize`` 带上一批输出的 ``--approve-sha256``。
  复用项为空。
* 父批次退出码（``claude_upgrade_batch.CAMPAIGN_RUN_PAUSE_STATUSES``，只在父监督器上下文里映射）：``awaiting_finalize`` 与
  ``stage_failed`` 是合法停点、退出 0——阶段失败已写 Store 并由交付事件入账、同根因由交付侧计数，父批次再判失败会让同一次
  失败在交付事件与监督器根因里各记一次；``delivery_gates_failed`` 零写入、没有交付记账，保持退出 3，交父批次失败收口。
* 父批次失败的续跑（异常、强杀、超时、交付门禁未过）：两个动作经适配器 ``candidate_post_run_extra_action``（形态判定
  ``batch_action_kind``）认作候选级零请求后处理动作，监督器收口为 ``recovery_required``（阶段保持进行中），
  ``reconcile-supervisor-run`` 入账后以 ``compile-and-run-vc-batch`` 逐字重派同一批次；同根因达上限照常停线。与 Codex VC-6
  的 canonical 生产步骤、Claude VC-5 的 compare／accept 同一条恢复路径，不另设阶段幂等重派合同。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_batch
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control import delivery_execution
from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    expect_image_digest,
    sha256_bytes,
    write_once,
)
from tools.official_client_control.contracts import (
    CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS,
    CANDIDATE_DELIVERY_FACT_V2_SCHEMA,
    CANDIDATE_DELIVERY_PACKAGE_SCHEMA,
    CANDIDATE_DELIVERY_PLAN_SCHEMA,
    CANDIDATE_DELIVERY_STAGES,
    OBJECT_SCHEMA,
    PRIVATE_ARCHIVE_MANIFEST_SCHEMA,
    fact_idempotency_key,
    invalidation_review_sha256,
)
from tools.official_client_control.errors import ControlError

SUBCOMMANDS = ("deliver-candidate", "delivery-stage-invalidate")
DELIVERY_ACTIONS = ("run", "finalize")
INVALIDATION_ACTIONS = ("preview", "apply")
STAGES = CANDIDATE_DELIVERY_STAGES
DELIVERY_PHASE = "VC-6"
# 四阶段冻结的检查闭集（设计 §4.6.3）：候选启动并通过交付 smoke；只替换应用容器切回真实回退镜像，验证 health、鉴权、数据
# 与依赖；恢复同一候选 digest，复核 Profile、入口与运行事实；冻结观察窗内无身份、安全、数据、状态或跨 Release 混用异常。
STAGE_CHECKS: Mapping[str, tuple[str, ...]] = {
    "candidate_active": ("entry-routing", "health", "image-identity", "smoke"),
    "rollback_verified": ("auth", "data", "dependencies", "health", "image-identity"),
    "candidate_restored": ("entry-routing", "health", "image-identity", "profile"),
    "stable_observed": ("data", "identity", "release-isolation", "safety", "state"),
}
# 交付门禁闭集（设计 §4.6.2）：任何写操作之前由执行器 freeze 给出，全部通过才封存交付计划或预约恢复段。
DELIVERY_GATES = (
    "compose-diff",
    "dependency-connectivity",
    "entry-routing",
    "evidence-inventory",
    "health",
    "image-identity",
    "observation-window",
    "rollback-availability",
    "secret-scan",
)
DEFAULT_OBSERVATION_WINDOW_SECONDS = 900
# 用户管理的生产环境状态（设计附录 G 例外④：字段名沿用 vircs_state）：候选交付不连接、不修改、不验证生产环境。
PRODUCTION_STATE = {"ownership": "operator_managed", "verification": "unverified", "action": "not_touched"}
OPERATIONAL_EVIDENCE_SCHEMA = "official-client-operational-evidence/v1"
DELIVERY_RELATIVE = Path("control") / "delivery"
STORE_SCRATCH_RELATIVE = Path("control") / "store-inputs"
LOCK_FILENAME = ".delivery.lock"
ROOT_CAUSE_ENVIRONMENT_DRIFT = "delivery-environment-drift"
LOGICAL_ACTION_PREFIX = "claude-upgrade:deliver-candidate"
EXECUTOR_PENDING_WORK_ITEM = "B4-07 遗留项：ARM64 隔离候选环境真实执行器（另行排期）"
# 交付环境执行器登记表：名字 → 无参工厂。生产环境目前不登记任何执行器（第二节）。
ENVIRONMENT_EXECUTORS: dict[str, Callable[..., Any]] = {}
EXECUTOR_V1 = "claude-upgrade-arm64-delivery-executor/v1"
EXECUTOR_CONFIGURATION_FLAGS = frozenset({"--executor-configuration", "--executor-configuration-sha256"})
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RESULTS = ("pass", "failed")
# 八、交付阶段结果与 VC-6 完成（B4-07 收尾）。
STAGE_RESULT_SCHEMA = "claude-upgrade-delivery/v1"
STAGE_RESULT_V2_SCHEMA = "claude-upgrade-delivery/v2"
STAGE_NAME = "deliver-candidate"
STAGE_RESULT_DIRECTORY = Path("delivery")
STAGE_RESULT_FILENAME = "result.json"
STAGE_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "client",
        "campaign_id",
        "stage",
        "candidate_id",
        "candidate_revision",
        "campaign_purpose",
        "status",
        "delivery_id",
        "delivery_plan_ref",
        "package_ref",
        "receipt_ref",
        "release_state",
        "production_state",
        "vc5_checkpoint",
        "accept_result",
        "vc4_build_receipt",
        "store",
        "vc6_status",
        "completed_at_utc",
    }
)
RELEASE_STATE = "ready_for_operator_release"
CANDIDATE_PRODUCTION_STATE = "not_activated"
VC6_STATUS_COMPLETE = "complete"
VC6_STATUS_PRODUCTION_PENDING = "production_activation_pending"
PRODUCTION_WORK_ITEM = "B4-08（生产分支，另批）"
# 九、VC-6 批次动作（B4-07 收尾）：动作 ID 与唯一项名相同；run 的超时为观察窗加余量（默认 900 秒的动作超时装不下默认
# 900 秒的观察窗与四个阶段）。
RUN_ACTION_ID = "vc6-01-deliver-run"
FINALIZE_ACTION_ID = "vc6-02-deliver-finalize"
BATCH_ACTION_KINDS: Mapping[str, str] = {RUN_ACTION_ID: "run", FINALIZE_ACTION_ID: "finalize"}
BATCH_ACTION_FLAGS: Mapping[str, frozenset[str]] = {
    "run": frozenset({"--campaign-dir", "--candidate-id", "--environment-executor", "--observation-window-seconds"}),
    "finalize": frozenset({"--campaign-dir", "--candidate-id", "--approve-sha256"}),
}
RUN_TIMEOUT_MARGIN_SECONDS = 1800
FINALIZE_TIMEOUT_SECONDS = 900


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


# ---------------------------------------------------------------------------
# 一、上下文：Formal Campaign、当前候选 revision、Store、计时账本与 VC-5 交接
# ---------------------------------------------------------------------------


class DeliveryContext:
    """一次交付命令的只读上下文（解析时已核对候选属于当前 revision、Store 工具身份当前有效、VC-5 已在 Store 完成）。"""

    def __init__(
        self,
        *,
        campaign_dir: Path,
        manifest: Mapping[str, Any],
        candidate_id: str,
        revision: int,
        store: Any,
        campaign: Mapping[str, Any],
        ledger_dir: Path,
        data_root: Path,
        handoff: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> None:
        self.campaign_dir = campaign_dir
        self.manifest = manifest
        self.candidate_id = candidate_id
        self.revision = revision
        self.store = store
        self.campaign = dict(campaign)
        self.campaign_id = str(campaign["campaign_id"])
        self.persona = dict(campaign["persona"])
        self.ledger_dir = ledger_dir
        self.data_root = data_root
        self.handoff = dict(handoff)
        # 第八节入口重放的结论：VC-5 checkpoint、accept 结果与 VC-4 构建收据的绑定（交付阶段结果原样登记）。
        self.entry = dict(entry)
        self.session = claude_upgrade_seal._StoreSession(
            Path(store.root), self.campaign_id, campaign_dir / STORE_SCRATCH_RELATIVE
        )

    @property
    def delivery_root(self) -> Path:
        return self.campaign_dir / DELIVERY_RELATIVE


def _resolve(campaign_dir: Path, candidate_id: str, *, action: str) -> DeliveryContext:
    orchestrator = _orchestrator()
    if not SAFE_ID_RE.fullmatch(candidate_id or ""):
        raise ConfigurationError("--candidate-id 格式非法。")
    campaign_dir = Path(campaign_dir)
    manifest = orchestrator._require_formal_campaign(campaign_dir)
    revision, record = orchestrator._require_candidate_revision(campaign_dir, manifest, action=action)
    if record is None or record.get("candidate_id") != candidate_id:
        current = record.get("candidate_id") if isinstance(record, Mapping) else None
        raise ConfigurationError(f"{action}：候选 {candidate_id} 不是当前候选 revision r{revision} 的候选（当前是 {current}）。")
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    campaign = dict(store_campaign)
    claude_upgrade_seal._require_store_tool_current(store, campaign)
    try:
        if store_receipts.revision_invalidation(store, campaign, int(revision)) is not None:
            raise ConfigurationError(f"候选 revision r{revision} 已作废（revision_required）：不能交付作废候选。")
        handoff = _vc5_handoff(store, campaign, candidate_id)
    except ControlError as error:
        raise ConfigurationError(f"Store 无法给出交付输入：{error}") from error
    ledger_dir = orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    from tools.official_client_capture import claude_upgrade_capture

    data_root = claude_upgrade_capture._host_data_root(campaign_dir)
    entry = _vc5_entry_replay(campaign_dir.resolve(strict=True), manifest, candidate_id, int(revision), handoff)
    return DeliveryContext(
        campaign_dir=campaign_dir.resolve(strict=True),
        manifest=manifest,
        candidate_id=candidate_id,
        revision=int(revision),
        store=store,
        campaign=campaign,
        ledger_dir=ledger_dir,
        data_root=data_root,
        handoff=handoff,
        entry=entry,
    )


def _vc5_handoff(store: Any, campaign: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    """VC-5 → VC-6 的交接：当前候选 revision、当前评估基线下该候选最后一条 ``validation_completed`` 及其引用链。"""

    formed = store_receipts.current_formed_under(store, dict(campaign))
    coordinates = (
        (formed["candidate_revision"], formed["evaluation_baseline"]) if isinstance(formed, Mapping) else None
    )
    completed = [
        fact
        for fact in store.list_facts(str(campaign["campaign_id"]), "validation")
        if fact["fact_kind"] == "validation_completed"
        and fact["payload"].get("candidate_id") == candidate_id
        and (
            coordinates is None
            or (fact["formed_under"]["candidate_revision"], fact["formed_under"]["evaluation_baseline"]) == coordinates
        )
    ]
    if not completed:
        raise ConfigurationError(
            f"候选 {candidate_id} 在当前候选 revision 与评估基线下没有 VC-5 完成事实（validation_completed）："
            "交付只能在 accept 门禁通过、validation-finalize 之后进行。"
        )
    fact = completed[-1]
    payload = fact["payload"]
    candidate = store.load_fact(payload["candidate_ref"])["payload"]
    build = store.load_receipt(payload["candidate_build_receipt_ref"])
    return {
        "validation_completed_ref": dict(store.fact_ref(fact)),
        "acceptance_ref": dict(payload["acceptance_ref"]),
        "candidate_ref": dict(payload["candidate_ref"]),
        "candidate_build_receipt_ref": dict(payload["candidate_build_receipt_ref"]),
        "candidate_evidence_package_ref": dict(payload["candidate_evidence_package_ref"]),
        "candidate_image_digest": str(candidate["image_digest"]),
        "architecture": str(candidate["target_architecture"]),
        "runtime_profile_sha256": str(build["profile_identity"]["profile_digest"]),
    }


def _campaign_path(campaign_dir: Path, raw: Any, label: str) -> Path:
    """checkpoint／绑定里的路径：Campaign 相对路径落到 Campaign 目录下（不得逃出），绝对路径原样。"""

    path = Path(str(raw))
    if not path.is_absolute():
        path = campaign_dir / path
        if not path.resolve().is_relative_to(campaign_dir.resolve()):
            raise ConfigurationError(f"{label} 的路径逃出了 Campaign 目录。")
    return path


def _vc5_entry_replay(
    campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, revision: int, handoff: Mapping[str, Any]
) -> dict[str, Any]:
    """第八节入口重放：VC-5 checkpoint → accept 阶段结果 → Store 交接，及 accept 证据封印 → VC-4 构建收据（只读）。

    返回交付阶段结果要登记的三项绑定：``vc5_checkpoint``（文件绑定加内部身份）、``accept_result``、``vc4_build_receipt``。
    """

    from tools.official_client_capture import claude_upgrade_acceptance

    plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
    vc5_path, vc5 = claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, "VC-5", revision=revision)
    accept_path = _campaign_path(campaign_dir, vc5["stage_receipt"]["path"], "VC-5 checkpoint 的阶段收据")
    accept = claude_upgrade_acceptance.load_stage_result(campaign_dir, accept_path, candidate_id, manifest=manifest)
    if (
        accept.get("candidate_revision") != revision
        or accept.get("campaign_purpose") != manifest.get("campaign_purpose")
        or accept.get("production_state") != CANDIDATE_PRODUCTION_STATE
    ):
        raise ConfigurationError(
            f"VC-5 checkpoint 绑定的 accept 阶段结果不属于当前候选 revision r{revision}、本 Campaign 用途或未激活的候选："
            "交付只承接当前 revision 的 VC-5 验收。"
        )
    store = accept.get("store") if isinstance(accept.get("store"), Mapping) else {}
    for key in ("validation_completed_ref", "acceptance_ref", "candidate_evidence_package_ref"):
        if store.get(key) != handoff[key]:
            raise ConfigurationError(
                f"accept 阶段结果登记的 {key} 与 Store 交接不一致：VC-5 的编排器侧封存与 Store 验收链不是同一条。"
            )
    seal_path = _campaign_path(campaign_dir, accept["evidence_seal"]["path"], "accept 验收证据封印")
    seal = claude_upgrade_vc._read_json(seal_path, "accept 验收证据封印")
    vc4_path, vc4 = claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, "VC-4", revision=revision)
    build_binding = {"path": str(vc4["stage_receipt"]["path"]), "sha256": str(vc4["stage_receipt"]["sha256"])}
    sealed_build = seal.get("build_receipt") if isinstance(seal.get("build_receipt"), Mapping) else {}
    if {"path": sealed_build.get("path"), "sha256": sealed_build.get("sha256")} != build_binding:
        raise ConfigurationError("accept 证据封印登记的构建收据不是 VC-4 checkpoint 绑定的那份：VC-4 与 VC-5 不是同一条链。")
    build = claude_upgrade_vc._read_json(_campaign_path(campaign_dir, build_binding["path"], "VC-4 构建收据"), "VC-4 构建收据")
    # 构建收据（official-client-upgrade-candidate-build-receipt/v1）的镜像 digest 记在 ``image.manifest_digest``，Store
    # ``candidate_frozen`` 的 ``image_digest`` 取的就是它（``claude_upgrade_candidate`` 构建登记）。
    image = build.get("image") if isinstance(build.get("image"), Mapping) else {}
    if build.get("candidate_id") != candidate_id or image.get("manifest_digest") != handoff["candidate_image_digest"]:
        raise ConfigurationError("VC-4 构建收据的候选或镜像 digest 与 Store 交接（candidate_frozen）不一致。")
    del vc4_path
    return {
        "vc5_checkpoint": claude_upgrade_vc.vc_checkpoint_reference(campaign_dir, vc5_path, vc5),
        "accept_result": claude_upgrade_vc.vc_control_binding(campaign_dir, accept_path),
        "vc4_build_receipt": build_binding,
    }


def _ledger_state(context: DeliveryContext) -> dict[str, Any]:
    try:
        return timing_ledger.phase_ledger_state(context.ledger_dir)
    except timing_ledger.TimingLedgerError as error:
        raise ConfigurationError(f"计时账本不可读：{error}") from error


def _require_vc6_open(context: DeliveryContext, state: Mapping[str, Any]) -> None:
    """交付写入的账本前提：当前 revision 的 VC-5 已完成、VC-6 进行中、账本处于 active。"""

    phases = state.get("revision_phase_state", {}).get(str(context.revision), {})
    if state.get("current_revision") != context.revision or phases.get("VC-5") != "completed":
        raise ConfigurationError(
            f"计时账本里候选 revision r{context.revision} 的 VC-5 尚未完成：交付只能在 VC-5 完成之后进行。"
        )
    if state.get("status") != "active" or state.get("active_phase") != DELIVERY_PHASE:
        raise ConfigurationError(
            f"计时账本当前状态 {state.get('status')}、进行中阶段 {state.get('active_phase')}：交付写入要求 VC-6 进行中"
            "（VC-6 由批次层开始；预算暂停须先批准延期，停线或同根因达上限须先 campaign-resume）。"
        )


# ---------------------------------------------------------------------------
# 二、交付环境执行器
# ---------------------------------------------------------------------------


def _environment_executor(name: Any, *, configuration: Mapping[str, Any] | None = None, execution_context: Mapping[str, Any] | None = None) -> Any:
    if not isinstance(name, str) or not SAFE_NAME_RE.fullmatch(name):
        raise ConfigurationError("--environment-executor 格式非法。")
    factory = ENVIRONMENT_EXECUTORS.get(name)
    if factory is None:
        raise _orchestrator()._pending(f"交付环境执行器 {name}", EXECUTOR_PENDING_WORK_ITEM)
    if configuration is None:
        return factory()
    if execution_context is None:
        raise ConfigurationError("真实交付缺少冻结执行上下文。")
    executor = factory(name, dict(configuration), dict(execution_context))
    if getattr(executor, "contract_version", None) != EXECUTOR_V1:
        raise ConfigurationError("已登记工厂未提供受管真实交付执行合同。")
    return executor


def _configuration_input(context: DeliveryContext, arguments: argparse.Namespace) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """父动作里的两项配置旗标必须一起给出，文件限 Campaign 内并按原始字节核 SHA。"""

    raw_path = getattr(arguments, "executor_configuration", None)
    digest = getattr(arguments, "executor_configuration_sha256", None)
    if (raw_path is None) != (digest is None):
        raise ConfigurationError("执行配置路径与 SHA 必须成对给出。")
    if raw_path is None:
        if delivery_execution.requires_current_contract(context.store, context.campaign):
            raise ConfigurationError("当前 Claude 真实交付必须显式冻结执行配置，缺配置零变更拒绝。")
        return None, None
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not path.resolve(strict=True).is_relative_to(context.campaign_dir):
        raise ConfigurationError("执行配置必须是 Campaign 内的可信绝对路径普通文件。")
    content = path.read_bytes()
    if not SHA256_RE.fullmatch(str(digest)) or sha256_bytes(content) != digest:
        raise ConfigurationError("执行配置原始字节与父动作冻结 SHA 不一致。")
    configuration = claude_upgrade_vc._read_json(path, "真实交付执行配置")
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import validate_configuration
    validate_configuration(configuration)
    binding = {"path": path.relative_to(context.data_root).as_posix(), "sha256": digest, "bytes": len(content)}
    return dict(configuration), binding


def _executor_context(context: DeliveryContext, delivery_id: str, binding: Mapping[str, Any]) -> dict[str, Any]:
    return {"campaign_id": context.campaign_id, "candidate_id": context.candidate_id, "delivery_id": delivery_id, "segment": "dr0", "run_nonce": f"{delivery_id}-dr0", "data_root": str(context.data_root), "configuration_ref": dict(binding)}


def _typed_manifest(context: DeliveryContext, manifest_id: str, kind: str, core: Mapping[str, Any]) -> dict[str, Any]:
    if len(manifest_id) > 128:
        manifest_id = f"delivery-{kind.replace('_', '-')}-{canonical_sha256(manifest_id)[:40]}"
    payload = {"schema_version": delivery_execution.OPERATIONAL_V2, "persona": dict(context.persona), "manifest_id": manifest_id, "evidence_type": kind, "entries": [{"id": kind, "facts": dict(core)}]}
    delivery_execution.validate_operational_evidence(payload)
    return payload


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigurationError(f"交付环境执行器返回非法：{message}")


def _checked_items(value: Any, expected_ids: Sequence[str] | None, label: str) -> list[dict[str, Any]]:
    """``[{id, result, facts}]``：id 安全且唯一；给出 ``expected_ids`` 时必须恰好是这个闭集。"""

    _expect(isinstance(value, list), f"{label} 必须是数组")
    items: list[dict[str, Any]] = []
    for item in value:
        _expect(isinstance(item, Mapping) and set(item) <= {"id", "result", "facts"} and "id" in item, f"{label} 条目字段非法")
        item_id = item["id"]
        _expect(isinstance(item_id, str) and SAFE_NAME_RE.fullmatch(item_id) is not None, f"{label} 条目 id 非法")
        result = item.get("result", "pass")
        _expect(result in RESULTS, f"{label}.{item_id} 的 result 非法")
        facts = item.get("facts", {})
        _expect(isinstance(facts, Mapping), f"{label}.{item_id} 的 facts 必须是对象")
        items.append({"id": item_id, "result": result, "facts": json.loads(json.dumps(facts))})
    ids = [item["id"] for item in items]
    _expect(len(ids) == len(set(ids)), f"{label} 条目 id 重复")
    if expected_ids is not None:
        _expect(sorted(ids) == sorted(expected_ids), f"{label} 必须恰好覆盖 {sorted(expected_ids)}")
    return sorted(items, key=lambda item: item["id"])


def _environment(value: Any, label: str) -> dict[str, str]:
    _expect(isinstance(value, Mapping) and set(value) == set(CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS), f"{label} 字段不闭合")
    _expect(isinstance(value["host_id"], str) and SAFE_ID_RE.fullmatch(value["host_id"]) is not None, f"{label}.host_id 非法")
    _expect(isinstance(value["architecture"], str) and bool(value["architecture"]), f"{label}.architecture 非法")
    for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS[2:]:
        _expect(isinstance(value[key], str) and SHA256_RE.fullmatch(value[key]) is not None, f"{label}.{key} 非法")
    return {key: str(value[key]) for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS}


def _image_digest(value: Any, label: str) -> str:
    try:
        return expect_image_digest(value, label)
    except ControlError as error:
        raise ConfigurationError(f"交付环境执行器返回非法：{error}") from error


def _freeze(context: DeliveryContext, executor: Any, name: str, delivery_id: str, window: int) -> dict[str, Any]:
    """执行器只读冻结当前环境，并按第二节校验返回值。"""

    request = {
        "campaign_id": context.campaign_id,
        "candidate_id": context.candidate_id,
        "delivery_id": delivery_id,
        "candidate_image_digest": context.handoff["candidate_image_digest"],
        "architecture": context.handoff["architecture"],
        "runtime_profile_sha256": context.handoff["runtime_profile_sha256"],
        "observation_window_seconds": window,
        "delivery_gates": list(DELIVERY_GATES),
    }
    raw = executor.freeze(request)
    _expect(isinstance(raw, Mapping), "freeze 必须返回对象")
    _expect(
        set(raw) == ({"environment", "rollback", "gates", "cores"} if getattr(executor, "contract_version", None) == EXECUTOR_V1 else {"environment", "rollback", "gates", "rollback_material", "deployment_configuration"}),
        "freeze 字段不闭合",
    )
    if getattr(executor, "contract_version", None) == EXECUTOR_V1:
        _expect(set(raw) == {"environment", "rollback", "gates", "cores"}, "真实 freeze 字段不闭合")
        cores = raw["cores"]
        kinds = {"configuration", "stable_projection", "freeze", "rollback_state", "zero_model_probes", "deployment_configuration"}
        _expect(isinstance(cores, Mapping) and set(cores) == kinds, "真实 freeze 的六种输入证据不闭合")
        for kind in kinds:
            _typed_manifest(context, f"{delivery_id}-{kind.replace('_', '-')}", kind, cores[kind])
        return {"executor": name, "environment": _environment(raw["environment"], "environment"), "rollback": dict(raw["rollback"]), "gates": _checked_items(raw["gates"], DELIVERY_GATES, "gates"), "cores": dict(cores)}
    rollback = raw["rollback"]
    _expect(isinstance(rollback, Mapping) and set(rollback) == {"image_digest", "runtime_profile_sha256"}, "rollback 字段不闭合")
    _expect(
        isinstance(rollback["runtime_profile_sha256"], str) and SHA256_RE.fullmatch(rollback["runtime_profile_sha256"]) is not None,
        "rollback.runtime_profile_sha256 非法",
    )
    snapshot = {
        "executor": name,
        "environment": _environment(raw["environment"], "environment"),
        "rollback": {
            "image_digest": _image_digest(rollback["image_digest"], "rollback.image_digest"),
            "runtime_profile_sha256": str(rollback["runtime_profile_sha256"]),
        },
        "gates": _checked_items(raw["gates"], DELIVERY_GATES, "gates"),
        "rollback_material": _checked_items(raw["rollback_material"], None, "rollback_material"),
        "deployment_configuration": _checked_items(raw["deployment_configuration"], None, "deployment_configuration"),
    }
    _expect(bool(snapshot["rollback_material"]), "rollback_material 不能为空")
    _expect(bool(snapshot["deployment_configuration"]), "deployment_configuration 不能为空")
    for key in ("rollback_material", "deployment_configuration"):
        _expect(all(item["result"] == "pass" for item in snapshot[key]), f"{key} 必须全部通过")
    return snapshot


def _observation(raw: Any, stage: str) -> dict[str, Any]:
    _expect(isinstance(raw, Mapping), "run_stage 必须返回对象")
    allowed = {
        "stage", "result", "environment", "image_digest", "runtime_profile_sha256", "checks", "observed_seconds",
        "root_cause_id", "live_request_count", "private_files",
    }
    _expect(set(raw) <= allowed and set(raw) >= allowed - {"root_cause_id", "live_request_count", "private_files"}, "run_stage 字段不闭合")
    _expect(raw["stage"] == stage, f"run_stage 返回的阶段不是 {stage}")
    _expect(raw["result"] in RESULTS, "run_stage.result 非法")
    count = raw.get("live_request_count", 0)
    _expect(count == 0 and not isinstance(count, bool), "live_request_count 必须为 0（交付 smoke 请求入账随真实执行器一起设计）")
    seconds = raw["observed_seconds"]
    _expect(isinstance(seconds, int) and not isinstance(seconds, bool) and seconds >= 0, "observed_seconds 非法")
    cause = raw.get("root_cause_id")
    _expect(cause is None or (isinstance(cause, str) and SAFE_ID_RE.fullmatch(cause) is not None), "root_cause_id 非法")
    _expect(
        isinstance(raw["runtime_profile_sha256"], str) and SHA256_RE.fullmatch(raw["runtime_profile_sha256"]) is not None,
        "runtime_profile_sha256 非法",
    )
    files = raw.get("private_files", [])
    _expect(isinstance(files, list), "private_files 必须是数组")
    private: list[dict[str, str]] = []
    for item in files:
        _expect(isinstance(item, Mapping) and set(item) == {"name", "content"}, "private_files 条目字段不闭合")
        _expect(isinstance(item["name"], str) and SAFE_NAME_RE.fullmatch(item["name"]) is not None, "private_files.name 非法")
        _expect(item["name"] != "observation.json", "private_files 不得使用保留名 observation.json")
        _expect(isinstance(item["content"], str), "private_files.content 必须是文本")
        private.append({"name": item["name"], "content": item["content"]})
    _expect(len({item["name"] for item in private}) == len(private), "private_files 名字重复")
    return {
        "stage": stage,
        "result": str(raw["result"]),
        "environment": _environment(raw["environment"], "run_stage.environment"),
        "image_digest": _image_digest(raw["image_digest"], "run_stage.image_digest"),
        "runtime_profile_sha256": str(raw["runtime_profile_sha256"]),
        "checks": _checked_items(raw["checks"], None, "checks"),
        "observed_seconds": int(seconds),
        "root_cause_id": cause,
        "private_files": sorted(private, key=lambda item: item["name"]),
    }


# ---------------------------------------------------------------------------
# 三、Store 读写的小工具
# ---------------------------------------------------------------------------


def _store(argv: Sequence[Any]) -> Any:
    return claude_upgrade_seal.store_cli([str(item) for item in argv])


def _object_ref(kind: str, payload: Mapping[str, Any]) -> dict[str, str]:
    """对象内容寻址引用（与 ``ControlStore.seal_object_outcome`` 同一算法），不封存。"""

    document = {"schema_version": OBJECT_SCHEMA, "object_kind": kind, "payload": dict(payload)}
    return {"object_kind": kind, "sha256": sha256_bytes(canonical_json_bytes(document))}


def _with_identity(payload: dict[str, Any]) -> dict[str, Any]:
    payload["identity_sha256"] = ""
    payload["identity_sha256"] = canonical_sha256({key: value for key, value in payload.items() if key != "identity_sha256"})
    return payload


def _manifest(context: DeliveryContext, manifest_id: str, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not SAFE_ID_RE.fullmatch(manifest_id):
        raise ConfigurationError(f"证据清单编号过长或非法：{manifest_id}")
    return {
        "schema_version": OPERATIONAL_EVIDENCE_SCHEMA,
        "persona": dict(context.persona),
        "manifest_id": manifest_id,
        "entries": sorted((dict(entry) for entry in entries), key=lambda entry: str(entry["id"])),
    }


def _sorted_refs(references: Any) -> list[dict[str, Any]]:
    """Store 引用数组的规范形态：按 ``canonical_sha256`` 严格排序、去重（``contracts._validate_ref_list``）。"""

    unique = {canonical_sha256(dict(reference)): dict(reference) for reference in references}
    return [unique[key] for key in sorted(unique)]


def _entry(entry_id: str, result: str, **facts: Any) -> dict[str, Any]:
    return {"id": entry_id, "facts": {"result": result, **facts}}


def _chain(context: DeliveryContext, delivery_id: str) -> dict[str, Any]:
    return dict(_store(["delivery-chain", "--store", context.store.root, "--campaign", context.campaign_id, "--delivery-id", delivery_id]))


def _deliveries(context: DeliveryContext) -> list[dict[str, Any]]:
    """本候选的交付（按编号）：``[{delivery_id, index, plan_ref, plan}]``；编号取自 ``<候选>-d<n>``。"""

    pattern = re.compile(rf"^{re.escape(context.candidate_id)}-d([1-9][0-9]*)$")
    found: dict[str, dict[str, Any]] = {}
    for reference in context.store.list_object_refs("candidate_delivery_plan"):
        plan = context.store.load_object(reference)["payload"]
        if plan["campaign_id"] != context.campaign_id or plan["candidate_id"] != context.candidate_id:
            continue
        match = pattern.fullmatch(str(plan["delivery_id"]))
        if match is None:
            raise ConfigurationError(f"Store 里有不按 <候选>-d<n> 命名的交付计划：{plan['delivery_id']}，不是本编排器写的。")
        if plan["delivery_id"] in found:
            raise ConfigurationError(f"交付 {plan['delivery_id']} 在 Store 里有多份交付计划，Store 已不一致。")
        found[plan["delivery_id"]] = {
            "delivery_id": str(plan["delivery_id"]),
            "index": int(match.group(1)),
            "plan_ref": dict(reference),
            "plan": dict(plan),
        }
    deliveries = sorted(found.values(), key=lambda item: item["index"])
    if [item["index"] for item in deliveries] != list(range(1, len(deliveries) + 1)):
        raise ConfigurationError("交付编号必须从 d1 起连续。")
    return deliveries


def _select_delivery(context: DeliveryContext, delivery_id: str | None) -> dict[str, Any]:
    deliveries = _deliveries(context)
    if not deliveries:
        raise ConfigurationError(f"候选 {context.candidate_id} 还没有交付：先执行 deliver-candidate run。")
    current = deliveries[-1]
    if delivery_id is not None and delivery_id != current["delivery_id"]:
        raise ConfigurationError(
            f"--delivery-id {delivery_id} 不是当前交付 {current['delivery_id']}：环境变化后旧交付只读保留，不再推进。"
        )
    return current


@contextlib.contextmanager
def _delivery_lock(context: DeliveryContext) -> Iterator[None]:
    root = ensure_private_directory(context.delivery_root, context.campaign_dir)
    path = root / LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ConfigurationError("交付锁必须是当前用户拥有的普通文件。")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ConfigurationError("另一个交付命令正在运行（交付锁被占用）：等它结束后再重跑。") from error
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _write_scope(context: DeliveryContext) -> Iterator[None]:
    with _orchestrator()._campaign_lock(context.campaign_dir):
        yield


# ---------------------------------------------------------------------------
# 四、计时账本：由 Store 交付链推出交付事件并补齐（第六节）
# ---------------------------------------------------------------------------


def _failure_root_cause(context: DeliveryContext, payload: Mapping[str, Any]) -> str:
    """失败阶段事实的根因：证据清单 ``stage-failure`` 项登记的 ``root_cause_id``，缺省 ``delivery-<阶段>``。"""

    for reference in payload.get("evidence_refs", []):
        manifest = context.store.load_object(reference)["payload"]
        for entry in manifest.get("entries", []):
            facts = entry.get("facts", {})
            if entry.get("id") == "stage-failure" and isinstance(facts.get("root_cause_id"), str):
                return str(facts["root_cause_id"])
    return f"delivery-{payload['stage']}"


def _event(context: DeliveryContext, event_id: str, event_type: str, *, delivery_id: str, stage: str,
           recovery: str | None, cause: str | None, next_action: str) -> dict[str, Any]:
    if not SAFE_ID_RE.fullmatch(event_id):
        raise ConfigurationError(f"计时账本事件 ID 过长或非法：{event_id}")
    return {
        "event_id": event_id,
        "phase": DELIVERY_PHASE,
        "event_type": event_type,
        "root_cause_id": cause,
        "next_action": next_action,
        "revision": context.revision,
        "candidate_id": context.candidate_id,
        "delivery_id": delivery_id,
        "delivery_recovery_revision": recovery,
        "delivery_stage": stage,
    }


def _expected_ledger_events(context: DeliveryContext) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for delivery in _deliveries(context):
        delivery_id = delivery["delivery_id"]
        try:
            state = store_receipts.delivery_chain_state(context.store, context.campaign_id, delivery_id)
        except ControlError as error:
            raise ConfigurationError(f"交付 {delivery_id} 的 Store 交付链不一致：{error}") from error

        def invalidation(reference: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
            return _event(
                context,
                f"delivery-{delivery_id}-invalidation-{str(reference['sha256'])[:16]}",
                "delivery_stage_invalidated",
                delivery_id=delivery_id,
                stage=str(payload["invalidated_stage"]),
                recovery=None,
                cause=None,
                next_action=f"deliver-candidate run：以 approved_invalidation 恢复段从 {payload['invalidated_stage']} 起重跑",
            )

        for number in sorted(state.recoveries):
            _reference, reservation = state.recoveries[number]
            recovery = f"dr{number}"
            stage = str(reservation["rerun_from_stage"])
            if reservation["anchor_kind"] == "approved_invalidation":
                anchor = reservation["anchor_ref"]
                events.append(invalidation(anchor, context.store.load_object(anchor)["payload"]))
                cause = None
            else:
                cause = _failure_root_cause(context, context.store.load_fact(reservation["anchor_ref"])["payload"])
            events.append(
                _event(
                    context, f"delivery-{delivery_id}-{recovery}-started", "delivery_recovery_started",
                    delivery_id=delivery_id, stage=stage, recovery=recovery, cause=cause,
                    next_action=f"{recovery}：从 {stage} 起重跑到 stable_observed",
                )
            )
            group = state.groups.get(number, [])
            last = group[-1]["payload"] if group else None
            if last is not None and last["result"] == "failed":
                failed_cause = _failure_root_cause(context, last)
                events.append(
                    _event(
                        context, f"delivery-{delivery_id}-{recovery}-failed", "delivery_recovery_failed",
                        delivery_id=delivery_id, stage=str(last["stage"]), recovery=recovery, cause=failed_cause,
                        next_action=f"deliver-candidate run：预约下一恢复段从 {last['stage']} 续跑",
                    )
                )
            elif last is not None and last["stage"] == STAGES[-1]:
                events.append(
                    _event(
                        context, f"delivery-{delivery_id}-{recovery}-completed", "delivery_recovery_completed",
                        delivery_id=delivery_id, stage=STAGES[-1], recovery=recovery, cause=None,
                        next_action="deliver-candidate finalize：审核交付包后签发候选交付收据",
                    )
                )
        if state.pending is not None:
            events.append(invalidation(*state.pending))
    return events


_EVENT_COMPARED_FIELDS = (
    "event_type", "phase", "root_cause_id", "revision", "candidate_id", "delivery_id", "delivery_recovery_revision",
    "delivery_stage",
)


def _catch_up_ledger(context: DeliveryContext) -> list[str]:
    """按 Store 补齐账本里缺的交付事件；账本已有的交付事件必须是推导结果的前缀（逐项一致）。返回本次追加的事件 ID。"""

    expected = _expected_ledger_events(context)
    appended: list[str] = []
    try:
        with codex_upgrade_supervisor._timing_closeout_lock(context.ledger_dir):
            existing = [
                event
                for event, _raw in timing_ledger._load_events(context.ledger_dir)
                if event.get("event_type") in timing_ledger.DELIVERY_EVENT_TYPES
                and event.get("revision") == context.revision
            ]
            if len(existing) > len(expected):
                raise ConfigurationError("计时账本的交付事件多于 Store 交付链能推出的事件：账本与 Store 不一致。")
            for actual, wanted in zip(existing, expected):
                if actual.get("event_id") != wanted["event_id"] or any(
                    actual.get(field) != wanted.get(field) for field in _EVENT_COMPARED_FIELDS
                ):
                    raise ConfigurationError(
                        f"计时账本的交付事件 {actual.get('event_id')} 与 Store 交付链推出的 {wanted['event_id']} 不一致。"
                    )
            for wanted in expected[len(existing):]:
                timing_ledger.append_event(context.ledger_dir, live_request_count=0, receipts=[], **wanted)
                appended.append(wanted["event_id"])
    except (timing_ledger.TimingLedgerError, codex_upgrade_supervisor.SupervisorError) as error:
        raise ConfigurationError(
            f"计时账本拒绝补记交付事件（Store 已写、账本待补；处理账本状态后重跑同一条命令即补齐）：{error}"
        ) from error
    return appended


# ---------------------------------------------------------------------------
# 五、交付计划、恢复段预约与阶段登记
# ---------------------------------------------------------------------------


def _delivery_id(context: DeliveryContext, index: int) -> str:
    delivery_id = f"{context.candidate_id}-d{index}"
    if not SAFE_ID_RE.fullmatch(delivery_id):
        raise ConfigurationError(f"候选 ID 过长，无法派生交付编号：{delivery_id}")
    return delivery_id


def _environment_entries(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        _entry("environment", "pass", **snapshot["environment"]),
        _entry("executor", "pass", name=snapshot["executor"]),
        _entry("rollback-point", "pass", **snapshot["rollback"]),
    ]


def _freeze_manifest(context: DeliveryContext, manifest_id: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """环境冻结证据：交付门禁、环境身份、回退点、执行器与候选部署配置。"""

    entries = _environment_entries(snapshot)
    entries += [_entry(f"gate-{item['id']}", item["result"], detail=item["facts"]) for item in snapshot["gates"]]
    entries += [_entry(f"deployment-{item['id']}", "pass", detail=item["facts"]) for item in snapshot["deployment_configuration"]]
    return _manifest(context, manifest_id, entries)


def _plan_drift(plan: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """冻结计划与本次冻结的差异：环境身份 6 项、回退镜像与回退 Profile（任一不同即须开新 delivery）。"""

    drift: dict[str, dict[str, Any]] = {}
    if (plan.get("schema_version") == delivery_execution.PLAN_V2) != ("cores" in snapshot):
        drift["execution_contract"] = {"planned": plan.get("schema_version"), "observed": delivery_execution.PLAN_V2 if "cores" in snapshot else CANDIDATE_DELIVERY_PLAN_SCHEMA}
    for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS:
        if plan[key] != snapshot["environment"][key]:
            drift[key] = {"planned": plan[key], "observed": snapshot["environment"][key]}
    if plan["rollback_image_digest"] != snapshot["rollback"]["image_digest"]:
        drift["rollback_image_digest"] = {"planned": plan["rollback_image_digest"], "observed": snapshot["rollback"]["image_digest"]}
    if plan["rollback_runtime_profile_sha256"] != snapshot["rollback"]["runtime_profile_sha256"]:
        drift["rollback_runtime_profile_sha256"] = {
            "planned": plan["rollback_runtime_profile_sha256"],
            "observed": snapshot["rollback"]["runtime_profile_sha256"],
        }
    return drift


def _open_delivery(context: DeliveryContext, index: int, snapshot: Mapping[str, Any], window: int) -> dict[str, Any]:
    """封存环境冻结证据、回退材料与交付计划（同内容复用），返回新 delivery。"""

    delivery_id = _delivery_id(context, index)
    if snapshot["environment"]["architecture"] != context.handoff["architecture"]:
        raise ConfigurationError(
            f"交付环境架构 {snapshot['environment']['architecture']} 与候选 {context.handoff['architecture']} 不一致。"
        )
    if "cores" in snapshot:
        return _open_typed_delivery(context, index, snapshot, window)
    freeze = _freeze_manifest(context, f"{delivery_id}-environment-freeze", snapshot)
    rollback = _manifest(
        context,
        f"{delivery_id}-rollback-material",
        [_entry(f"rollback-{item['id']}", "pass", detail=item["facts"]) for item in snapshot["rollback_material"]],
    )
    with _write_scope(context):
        freeze_ref = context.session.seal_object("operational_evidence", freeze)
        rollback_ref = context.session.seal_object("operational_evidence", rollback)
        plan = _with_identity(
            {
                "schema_version": CANDIDATE_DELIVERY_PLAN_SCHEMA,
                "campaign_id": context.campaign_id,
                "persona": dict(context.persona),
                "plan_id": f"{delivery_id}-plan",
                "delivery_id": delivery_id,
                "candidate_id": context.candidate_id,
                "candidate_ref": context.handoff["candidate_ref"],
                "candidate_build_receipt_ref": context.handoff["candidate_build_receipt_ref"],
                "validation_completed_ref": context.handoff["validation_completed_ref"],
                "acceptance_ref": context.handoff["acceptance_ref"],
                "candidate_evidence_package_ref": context.handoff["candidate_evidence_package_ref"],
                **snapshot["environment"],
                "runtime_profile_sha256": context.handoff["runtime_profile_sha256"],
                "rollback_runtime_profile_sha256": snapshot["rollback"]["runtime_profile_sha256"],
                "candidate_image_digest": context.handoff["candidate_image_digest"],
                "rollback_image_digest": snapshot["rollback"]["image_digest"],
                "observation_window_seconds": window,
                "stage_checks": {stage: list(STAGE_CHECKS[stage]) for stage in STAGES},
                "environment_freeze_ref": freeze_ref,
                "rollback_material_refs": [rollback_ref],
                "vircs_state": dict(PRODUCTION_STATE),
            }
        )
        plan_ref = context.session.seal_object("candidate_delivery_plan", plan)
    return {"delivery_id": delivery_id, "index": index, "plan_ref": plan_ref, "plan": plan}


def _open_typed_delivery(context: DeliveryContext, index: int, snapshot: Mapping[str, Any], window: int) -> dict[str, Any]:
    """先封存不反向引用 Plan 的六种真实输入，再形成 Plan v2。"""

    delivery_id = _delivery_id(context, index)
    cores = snapshot["cores"]
    for core in cores.values():
        if core["campaign_id"] != context.campaign_id or core["delivery_id"] != delivery_id:
            raise ConfigurationError("冻结执行输入不是即将建立的同一交付；须按新 delivery 重做只读 freeze。")
    with _write_scope(context):
        refs = {}
        for kind in ("configuration", "stable_projection", "freeze", "rollback_state", "zero_model_probes", "deployment_configuration"):
            refs[kind] = context.session.seal_object("operational_evidence", _typed_manifest(context, f"{delivery_id}-{kind.replace('_', '-')}", kind, cores[kind]))
        plan = _with_identity({"schema_version": delivery_execution.PLAN_V2, "campaign_id": context.campaign_id, "persona": dict(context.persona), "plan_id": f"{delivery_id}-plan", "delivery_id": delivery_id, "candidate_id": context.candidate_id, **{key: context.handoff[key] for key in ("candidate_ref", "candidate_build_receipt_ref", "validation_completed_ref", "acceptance_ref", "candidate_evidence_package_ref")}, **snapshot["environment"], "runtime_profile_sha256": context.handoff["runtime_profile_sha256"], "rollback_runtime_profile_sha256": snapshot["rollback"]["runtime_profile_sha256"], "candidate_image_digest": context.handoff["candidate_image_digest"], "rollback_image_digest": snapshot["rollback"]["image_digest"], "observation_window_seconds": window, "stage_checks": {stage: list(STAGE_CHECKS[stage]) for stage in STAGES}, "environment_freeze_ref": refs["freeze"], "rollback_material_refs": [refs["rollback_state"]], "vircs_state": dict(PRODUCTION_STATE), "executor_configuration_ref": refs["configuration"], "stable_environment_projection_ref": refs["stable_projection"], "probe_recipe_ref": refs["zero_model_probes"], "rollback_state_ref": refs["rollback_state"], "environment_before_ref": refs["freeze"], "deployment_configuration_ref": refs["deployment_configuration"]})
        plan_ref = context.session.seal_object("candidate_delivery_plan", plan)
    return {"delivery_id": delivery_id, "index": index, "plan_ref": plan_ref, "plan": plan}


def _require_plan_current(context: DeliveryContext, delivery: Mapping[str, Any]) -> None:
    plan = delivery["plan"]
    for key in ("validation_completed_ref", "acceptance_ref", "candidate_ref", "candidate_build_receipt_ref"):
        if plan[key] != context.handoff[key]:
            raise ConfigurationError(
                f"交付 {delivery['delivery_id']} 的计划绑定的 {key} 不是当前 VC-5 交接：VC-5 输入在交付开始后发生了变化。"
            )


def _segment(context: DeliveryContext, chain: Mapping[str, Any]) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    """最新段：``(编号, 段预约引用, 段预约)``；首轮交付（dr0）后两者为 None。"""

    latest = chain.get("latest_recovery_revision")
    if latest is None:
        return 0, None, None
    number = int(str(latest)[2:])
    reference = dict(chain["recovery_refs"][number - 1])
    return number, reference, dict(context.store.load_object(reference)["payload"])


def _next_stage(context: DeliveryContext, chain: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None] | None:
    """最新段未收口时它的下一阶段与前序事实引用；已收口返回 None（要么签收据，要么先预约新恢复段）。"""

    if chain["latest_closed"]:
        return None
    number, _reference, reservation = _segment(context, chain)
    terminal = chain.get("terminal_ref")
    if terminal is None:
        if number == 0:
            return STAGES[0], None
        assert reservation is not None
        return str(reservation["rerun_from_stage"]), reservation["previous_valid_stage_ref"]
    payload = context.store.load_fact(terminal)["payload"]
    return STAGES[STAGES.index(payload["stage"]) + 1], dict(terminal)


def _anchor(context: DeliveryContext, chain: Mapping[str, Any]) -> tuple[str, dict[str, Any], str, str | None]:
    """新恢复段的锚点：``(anchor_kind, anchor_ref, rerun_from_stage, 根因)``；待消费失效优先（Store 门禁 ④）。"""

    pending = chain.get("pending_invalidation_ref")
    if pending is not None:
        payload = context.store.load_object(pending)["payload"]
        return "approved_invalidation", dict(pending), str(payload["invalidated_stage"]), None
    terminal = chain.get("terminal_ref")
    payload = context.store.load_fact(terminal)["payload"] if terminal is not None else None
    if payload is None or payload["result"] != "failed":
        raise ConfigurationError("最新交付段没有以失败收口，也没有待消费的阶段失效：没有可续跑的恢复段。")
    return "failure", dict(terminal), str(payload["stage"]), _failure_root_cause(context, payload)


def _reserve(
    context: DeliveryContext,
    delivery: Mapping[str, Any],
    chain: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    rerun_from: str | None,
) -> dict[str, Any]:
    """预约 dr<k>：先做账本预检（零写入），再封 environment_before 证据与段预约，最后补账（delivery_recovery_started）。"""

    anchor_kind, anchor_ref, stage, cause = _anchor(context, chain)
    if rerun_from is not None and rerun_from != stage:
        raise ConfigurationError(
            f"推导出的续跑起点是 {stage}，与 --rerun-from {rerun_from} 不同：已通过的阶段要重跑，须先 delivery-stage-invalidate。"
        )
    number = int(str(chain["next_recovery_revision"])[2:])
    index = STAGES.index(stage)
    previous = chain["valid_stage_refs"].get(STAGES[index - 1]) if index else None
    if index and previous is None:
        raise ConfigurationError(f"续跑起点 {stage} 之前的阶段在有效链上没有 pass：Store 交付链不一致。")
    state = _ledger_state(context)
    _require_vc6_open(context, state)
    if cause is not None:
        try:
            plan, _raw = timing_ledger._load_plan(context.ledger_dir)
        except timing_ledger.TimingLedgerError as error:
            raise ConfigurationError(f"计时账本计划不可读：{error}") from error
        limit = int(plan["same_root_cause_retry_limit"])
        count = int(state.get("same_root_cause_failures", {}).get(cause, 0)) + (1 if number == 1 else 0)
        if count >= limit:
            raise ConfigurationError(
                f"同一根因 {cause} 的交付失败已达计时账本冻结上限（{count}／{limit}）：零写入停在这里，修复并经 "
                "campaign-resume 确认后再续跑。"
            )
    delivery_id = str(delivery["delivery_id"])
    recovery = f"dr{number}"
    environment = _manifest(context, f"{delivery_id}-{recovery}-environment-before", _environment_entries(snapshot)
                            + [_entry(f"gate-{item['id']}", item["result"], detail=item["facts"]) for item in snapshot["gates"]])
    if delivery["plan"].get("schema_version") == delivery_execution.PLAN_V2:
        environment = _typed_manifest(context, f"{delivery_id}-{recovery}-environment-before", "freeze", snapshot["cores"]["freeze"])
    with _write_scope(context):
        evidence_ref = context.session.seal_object("operational_evidence", environment)
        payload = {
            "campaign_id": context.campaign_id,
            "delivery_id": delivery_id,
            "delivery_plan_ref": dict(delivery["plan_ref"]),
            "recovery_revision": recovery,
            "anchor_kind": anchor_kind,
            "anchor_ref": anchor_ref,
            "rerun_from_stage": stage,
            "previous_valid_stage_ref": dict(previous) if previous is not None else None,
            "stages_to_rerun": list(STAGES[index:]),
            "run_nonce": f"{delivery_id}-{recovery}",
            "environment_before": {**snapshot["environment"], "evidence_ref": evidence_ref},
        }
        outcome = context.session._with_input(
            payload,
            lambda path: _store(
                ["delivery-recovery-reserve", "--store", context.store.root, "--campaign", context.campaign_id,
                 "--input", path, "--allow-existing"]
            ),
        )
        appended = _catch_up_ledger(context)
    return {
        "recovery_revision": recovery,
        "recovery_ref": dict(outcome["object_ref"]),
        "anchor_kind": anchor_kind,
        "rerun_from_stage": stage,
        "root_cause_id": cause,
        "ledger_events": appended,
    }


def _expected_stage_identity(plan: Mapping[str, Any], stage: str) -> tuple[str, str]:
    if stage == "rollback_verified":
        return str(plan["rollback_image_digest"]), str(plan["rollback_runtime_profile_sha256"])
    return str(plan["candidate_image_digest"]), str(plan["runtime_profile_sha256"])


def _verdict(plan: Mapping[str, Any], stage: str, observation: Mapping[str, Any]) -> tuple[str, str | None, list[str]]:
    """编排器对执行器观测的核对（第二节）：``(结果, 根因, 不符项)``。"""

    problems: list[str] = []
    drift = [key for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS if observation["environment"][key] != plan[key]]
    if drift:
        problems.append(f"环境身份漂移：{'、'.join(drift)}")
    image, profile = _expected_stage_identity(plan, stage)
    if observation["image_digest"] != image:
        problems.append("镜像不是冻结计划值")
    if observation["runtime_profile_sha256"] != profile:
        problems.append("运行 Profile 不是冻结计划值")
    if [item["id"] for item in observation["checks"]] != sorted(plan["stage_checks"][stage]):
        problems.append("检查项不是冻结闭集")
    elif any(item["result"] != "pass" for item in observation["checks"]):
        problems.append("检查未全部通过")
    if stage == STAGES[-1] and observation["observed_seconds"] < int(plan["observation_window_seconds"]):
        problems.append("观察窗不足")
    if observation["result"] == "failed":
        return "failed", observation["root_cause_id"] or f"delivery-{stage}", problems
    if not problems:
        return "pass", None, []
    if drift:
        return "failed", ROOT_CAUSE_ENVIRONMENT_DRIFT, problems
    return "failed", f"delivery-{stage}-verification", problems


def _write_private(context: DeliveryContext, delivery_id: str, label: str, files: Sequence[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """把一次阶段运行的私有文件写进 ``control/delivery/<delivery_id>/<label>-*/``（0600，只写一次），返回数据根相对绑定。"""

    root = ensure_private_directory(context.delivery_root / delivery_id, context.campaign_dir)
    directory = Path(tempfile.mkdtemp(prefix=f"{label}-", dir=root))
    bindings: list[dict[str, Any]] = []
    for name, content in files:
        path = directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        bindings.append(
            {
                "path": path.resolve(strict=True).relative_to(context.data_root.resolve(strict=True)).as_posix(),
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    return sorted(bindings, key=lambda item: item["path"])


def _stage_payload(
    context: DeliveryContext,
    delivery: Mapping[str, Any],
    stage: str,
    *,
    previous: Mapping[str, Any] | None,
    recovery: tuple[str, Mapping[str, Any]] | None,
    evidence_ref: Mapping[str, Any],
    observed_seconds: int,
    result: str,
) -> dict[str, Any]:
    plan = delivery["plan"]
    image, profile = _expected_stage_identity(plan, stage)
    return _with_identity(
        {
            "schema_version": CANDIDATE_DELIVERY_FACT_V2_SCHEMA,
            "delivery_id": plan["delivery_id"],
            "stage": stage,
            "candidate_id": plan["candidate_id"],
            "candidate_ref": plan["candidate_ref"],
            "candidate_build_receipt_ref": plan["candidate_build_receipt_ref"],
            "validation_completed_ref": plan["validation_completed_ref"],
            "acceptance_ref": plan["acceptance_ref"],
            "delivery_plan_ref": dict(delivery["plan_ref"]),
            "previous_stage_ref": dict(previous) if previous is not None else None,
            **{key: plan[key] for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS},
            "runtime_profile_sha256": profile,
            "image_digest": image,
            "check_ids": list(plan["stage_checks"][stage]),
            "evidence_refs": [dict(evidence_ref)],
            # Store 合同：只有 stable_observed 记观察秒数，其余阶段恒为 0（执行器报的耗时留在阶段证据的 run 项）。
            "observed_seconds": int(observed_seconds) if stage == STAGES[-1] else 0,
            "result": result,
            "recovery_revision": recovery[0] if recovery is not None else None,
            "recovery_ref": dict(recovery[1]) if recovery is not None else None,
        }
    )


def _record_stage(context: DeliveryContext, stage: str, payload: Mapping[str, Any], *, publication: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``candidate-delivery-record``（事实 v2）：带幂等键，父监督器里另带 producer run；返回 ``{fact_ref, reused}``。"""

    key = fact_idempotency_key(
        f"{LOGICAL_ACTION_PREFIX}:{payload['delivery_id']}:{payload['recovery_revision'] or 'dr0'}:{stage}",
        canonical_sha256(dict(payload)),
        "candidate_delivery_recorded",
    )
    producer = claude_upgrade_seal._producer_run()
    issued_at = _orchestrator()._utc_now()
    if publication is not None:
        if publication["payload"] != payload or publication["idempotency_key"] != key:
            raise ConfigurationError("不可变发布意图与待补发事实不一致。")
        producer = publication["producer_run"]
        issued_at = publication["issued_at_utc"]
        # 已有同键事实只读复用；尚未写入时禁止用新形成期坐标替换旧物理意图。
        existing = context.store.find_fact_by_idempotency_key(context.campaign_id, key)
        if existing is None and store_receipts.current_formed_under(context.store, dict(context.campaign)) != publication["formed_under"]:
            raise ConfigurationError("物理操作已完成，但形成期坐标已变化；保留原意图并停止补发，不能重做物理阶段。")

    def call(path: Path) -> Any:
        argv: list[Any] = [
            "candidate-delivery-record", "--store", context.store.root, "--campaign", context.campaign_id,
            "--stage", stage, "--input", path, "--issued-at", issued_at, "--idempotency-key", key,
        ]
        if producer is None:
            return _store(argv)
        return context.session._with_input(producer, lambda producer_path: _store([*argv, "--producer-run", producer_path]))

    outcome = context.session._with_input(dict(payload), call)
    if publication is not None:
        fact = context.store.load_fact(outcome["fact_ref"])
        if any(fact[key] != publication[key] for key in ("payload", "issued_at_utc", "producer_run", "formed_under", "idempotency_key")):
            raise ConfigurationError("Store 幂等事实不属于原始发布意图。")
    return {"fact_ref": dict(outcome["fact_ref"]), "reused": bool(outcome["reused"])}


def _publication_root(context: DeliveryContext, delivery_id: str) -> Path:
    return ensure_private_directory(context.delivery_root / delivery_id / "publication", context.campaign_dir)


def _bound_local(context: DeliveryContext, path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or not path.resolve(strict=True).is_relative_to(context.data_root):
        raise ConfigurationError("交付后处理原件路径不可信。")
    return {"path": path.relative_to(context.data_root).as_posix(), "sha256": file_sha256(path), "bytes": path.stat().st_size}


def _immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    """同内容重入，异内容拒绝；文件及目录落盘后才允许下一步。"""

    content = canonical_json_bytes(dict(payload))
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise ConfigurationError("交付不可变意图已存在且字节不一致。")
        return
    write_once(path, content)


def _stage_request(delivery: Mapping[str, Any], stage: str, run_nonce: str) -> dict[str, Any]:
    plan = delivery["plan"]
    return {"stage": stage, "run_nonce": run_nonce, "plan_ref": dict(delivery["plan_ref"]), "configuration_ref": plan["executor_configuration_ref"], "stable_projection_ref": plan["stable_environment_projection_ref"], "probe_recipe_ref": plan["probe_recipe_ref"], "environment": {key: plan[key] for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS}}


def _publication_seed(context: DeliveryContext, delivery: Mapping[str, Any], request: Mapping[str, Any], previous: Mapping[str, Any] | None, recovery: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _publication_root(context, str(delivery["delivery_id"]))
    path = root / "seeds" / f"{canonical_sha256(dict(request))}.json"
    if path.exists():
        value = claude_upgrade_vc._read_json(path, "交付发布预登记")
        if value.get("request") != request or value.get("delivery_id") != delivery["delivery_id"]:
            raise ConfigurationError("重入请求与原发布预登记不一致。")
    else:
        value = {"schema_version": "claude-upgrade-delivery-publication-seed/v1", "campaign_id": context.campaign_id, "candidate_id": context.candidate_id, "delivery_id": delivery["delivery_id"], "request": dict(request), "previous_stage_ref": dict(previous) if previous is not None else None, "recovery_revision": recovery[0] if recovery is not None else None, "recovery_ref": dict(recovery[1]) if recovery is not None else None, "issued_at_utc": _orchestrator()._utc_now(), "producer_run": claude_upgrade_seal._producer_run(), "formed_under": store_receipts.current_formed_under(context.store, dict(context.campaign))}
        _immutable_json(path, value)
    return value, _bound_local(context, path)


def _typed_stage_payload(context: DeliveryContext, delivery: Mapping[str, Any], core: Mapping[str, Any], reference: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    payload = _stage_payload(context, delivery, str(core["stage"]), previous=seed["previous_stage_ref"], recovery=(seed["recovery_revision"], seed["recovery_ref"]) if seed["recovery_revision"] is not None else None, evidence_ref=reference, observed_seconds=int(core["observed_seconds"]), result=str(core["result"]))
    image = payload.pop("image_digest")
    profile = payload.pop("runtime_profile_sha256")
    payload.update(schema_version=delivery_execution.FACT_V3, target_image_digest=image, target_runtime_profile_sha256=profile, execution_evidence_ref=dict(reference))
    return _with_identity(payload)


def _publish_execution(context: DeliveryContext, delivery: Mapping[str, Any], core: Mapping[str, Any]) -> dict[str, Any]:
    """从 guard 已封的同字节执行证明补 Store／账本；此函数没有物理动作。"""

    request = _stage_request(delivery, str(core["stage"]), str(core["run_nonce"]))
    root = _publication_root(context, str(delivery["delivery_id"]))
    seed_path = root / "seeds" / f"{canonical_sha256(request)}.json"
    if not seed_path.is_file() or seed_path.is_symlink():
        raise ConfigurationError("已完成物理操作缺少修改前发布预登记，不能生成新的 producer 或时间坐标。")
    seed = claude_upgrade_vc._read_json(seed_path, "原发布预登记")
    seed_binding = _bound_local(context, seed_path)
    if seed.get("request") != request or seed.get("campaign_id") != context.campaign_id:
        raise ConfigurationError("原物理执行与发布预登记不一致。")
    manifest = _typed_manifest(context, f"{delivery['delivery_id']}-{core['operation_id']}-execution", "stage_execution", core)
    reference = _object_ref("operational_evidence", manifest)
    payload = _typed_stage_payload(context, delivery, core, reference, seed)
    key = fact_idempotency_key(f"{LOGICAL_ACTION_PREFIX}:{payload['delivery_id']}:{payload['recovery_revision'] or 'dr0'}:{payload['stage']}", canonical_sha256(payload), "candidate_delivery_recorded")
    publication = {"schema_version": "claude-upgrade-delivery-publication-intent/v1", "campaign_id": context.campaign_id, "delivery_id": delivery["delivery_id"], "operation_id": core["operation_id"], "seed_binding": seed_binding, "execution_evidence_ref": reference, "payload": payload, "issued_at_utc": seed["issued_at_utc"], "producer_run": seed["producer_run"], "formed_under": seed["formed_under"], "idempotency_key": key, "ledger_action": "catch_up_delivery_events"}
    path = root / f"{core['operation_id']}.json"
    with _write_scope(context):
        # 来源语义先于发布意图；已提交意图后的异常不再调用物理恢复或重做阶段。
        delivery_execution.validate_evidence_graph(context.store, manifest)
        sealed = context.session.seal_object("operational_evidence", manifest)
        if sealed != reference:
            raise ConfigurationError("交付执行对象封存引用漂移。")
        delivery_execution.validate_stage_execution(context.store, delivery["plan"], payload)
        _immutable_json(path, publication)
        recorded = _record_stage(context, str(core["stage"]), payload, publication=publication)
        appended = _catch_up_ledger(context)
    return {"stage": core["stage"], "segment": seed["recovery_revision"] or "dr0", "result": core["result"], "root_cause_id": None if core["result"] == "pass" else f"delivery-{core['stage']}", "problems": [] if core["result"] == "pass" else [core["restoration"]["reason"]], "fact_ref": recorded["fact_ref"], "evidence_ref": reference, "publication_intent_binding": _bound_local(context, path), "ledger_events": appended}


def _verify_typed_observation(context: DeliveryContext, delivery: Mapping[str, Any], ticket: Mapping[str, Any], raw: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """提交物理意图前核真实观测、原件、当前下层图和阶段顺序。"""

    from tools.official_client_capture.claude_upgrade_delivery_recovery_guard import check_binding, read_json, validate_observation
    prepared = read_json(check_binding(context.data_root, ticket["prepared_binding"]))
    if prepared.get("request") != request:
        raise ConfigurationError("guard prepared 不属于当前冻结阶段。")
    observation = validate_observation(raw, prepared, context.data_root)
    plan = delivery["plan"]
    delivery_execution.validate_plan(context.store, plan)
    _require_plan_current(context, delivery)
    if store_receipts.current_formed_under(context.store, dict(context.campaign)) != claude_upgrade_vc._read_json(_publication_root(context, delivery["delivery_id"]) / "seeds" / f"{canonical_sha256(dict(request))}.json", "原发布预登记")["formed_under"]:
        raise ConfigurationError("物理操作期间形成期坐标发生变化。")
    step = _next_stage(context, _chain(context, delivery["delivery_id"]))
    if step is None or step[0] != request["stage"]:
        raise ConfigurationError("物理提交前 Store 阶段顺序发生变化。")
    delivery_execution._requests(observation["request_records"])
    recipe = delivery_execution.evidence_facts(context.store, plan["probe_recipe_ref"], "zero_model_probes")["recipe"]
    allowed = delivery_execution.allowed_probe_requests(recipe)
    if any((row["method"], row["path"]) not in allowed for row in observation["request_records"]):
        raise ConfigurationError("实际请求超出冻结探针 recipe。")
    if observation["result"] != "pass":
        raise ConfigurationError("真实观测未通过。")
    return observation


def _execute_typed_stage(context: DeliveryContext, executor: Any, delivery: Mapping[str, Any], stage: str, previous: Any, recovery: Any) -> dict[str, Any]:
    run_nonce = str(recovery[2]["run_nonce"]) if recovery is not None else f"{delivery['delivery_id']}-dr0"
    request = _stage_request(delivery, stage, run_nonce)
    with _write_scope(context):
        _publication_seed(context, delivery, request, previous, recovery)
    ticket = executor.prepare_stage(request)
    raw: Any = None
    reason = "观测及提交前语义通过"
    try:
        raw = executor.run_stage(ticket)
        _verify_typed_observation(context, delivery, ticket, raw, request)
        verdict = "pass"
    except Exception as error:
        # 这里只捕获提交前异常，finish 后的 Store 失败属于后处理恢复。
        verdict = "failed"
        reason = f"提交前复核失败：{type(error).__name__}"
    try:
        document = {"schema_version": "claude-upgrade-delivery-verifier/v1", "operation_id": ticket["operation_id"], "request_sha256": canonical_sha256(request), "observation_sha256": canonical_sha256(raw), "result": verdict, "reason": reason}
        path = _publication_root(context, delivery["delivery_id"]) / "verifiers" / f"{ticket['operation_id']}.json"
        _immutable_json(path, document)
        decision = {"result": verdict, "observation_sha256": document["observation_sha256"], "verifier_binding": _bound_local(context, path), "reason": reason}
    except Exception as error:
        # 编排复核原件写不下也立即 abort；现有 prepared 是可信只读绑定，不能放任 lease 悬挂。
        decision = {"result": "failed", "observation_sha256": canonical_sha256(None), "verifier_binding": ticket["prepared_binding"], "reason": f"复核原件预写失败：{type(error).__name__}"}
    core = executor.finish_stage(ticket, decision)
    return _publish_execution(context, delivery, core)


def _execute_stage(
    context: DeliveryContext,
    executor: Any,
    executor_name: str,
    delivery: Mapping[str, Any],
    stage: str,
    previous: Mapping[str, Any] | None,
    recovery: tuple[str, Mapping[str, Any], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """执行器跑一个阶段 → 核对 → 写私有文件 → 封阶段证据 → 登记阶段事实 → 补账。"""

    if delivery["plan"].get("schema_version") == delivery_execution.PLAN_V2:
        return _execute_typed_stage(context, executor, delivery, stage, previous, recovery)

    plan = delivery["plan"]
    delivery_id = str(delivery["delivery_id"])
    segment = recovery[0] if recovery is not None else "dr0"
    run_nonce = str(recovery[2]["run_nonce"]) if recovery is not None else f"{delivery_id}-dr0"
    request = {
        "campaign_id": context.campaign_id,
        "candidate_id": context.candidate_id,
        "delivery_id": delivery_id,
        "segment": segment,
        "run_nonce": run_nonce,
        "stage": stage,
        "plan": {
            **{key: plan[key] for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS},
            "candidate_image_digest": plan["candidate_image_digest"],
            "rollback_image_digest": plan["rollback_image_digest"],
            "runtime_profile_sha256": plan["runtime_profile_sha256"],
            "rollback_runtime_profile_sha256": plan["rollback_runtime_profile_sha256"],
            "observation_window_seconds": plan["observation_window_seconds"],
            "stage_checks": list(plan["stage_checks"][stage]),
        },
    }
    observation = _observation(executor.run_stage(request), stage)
    result, cause, problems = _verdict(plan, stage, observation)
    files = [("observation.json", canonical_json_bytes({**observation, "private_files": [item["name"] for item in observation["private_files"]]}))]
    files += [(item["name"], item["content"].encode("utf-8")) for item in observation["private_files"]]
    bindings = _write_private(context, delivery_id, f"{segment}-{stage}", files)
    entries = [_entry(f"check-{item['id']}", item["result"], detail=item["facts"]) for item in observation["checks"]]
    entries += [
        _entry("environment", "failed" if any(problem.startswith("环境身份漂移") for problem in problems) else "pass",
               **observation["environment"]),
        _entry("image", "pass" if not any(problem.startswith(("镜像", "运行 Profile")) for problem in problems) else "failed",
               image_digest=observation["image_digest"], runtime_profile_sha256=observation["runtime_profile_sha256"]),
        _entry("run", "pass", segment=segment, run_nonce=run_nonce, executor=executor_name,
               observed_seconds=observation["observed_seconds"]),
    ]
    entries += [_entry(f"private-{index:03d}", "pass", **binding) for index, binding in enumerate(bindings, start=1)]
    if result == "failed":
        entries.append(_entry("stage-failure", "failed", root_cause_id=cause, problems=problems))
    manifest = _manifest(context, f"{delivery_id}-{segment}-{stage}", entries)
    with _write_scope(context):
        evidence_ref = context.session.seal_object("operational_evidence", manifest)
        payload = _stage_payload(
            context,
            delivery,
            stage,
            previous=previous,
            recovery=(recovery[0], recovery[1]) if recovery is not None else None,
            evidence_ref=evidence_ref,
            observed_seconds=observation["observed_seconds"],
            result=result,
        )
        recorded = _record_stage(context, stage, payload)
        appended = _catch_up_ledger(context)
    return {
        "stage": stage,
        "segment": segment,
        "result": result,
        "root_cause_id": cause,
        "problems": problems,
        "fact_ref": recorded["fact_ref"],
        "evidence_ref": evidence_ref,
        "ledger_events": appended,
    }


def _close_for_drift(
    context: DeliveryContext,
    delivery: Mapping[str, Any],
    chain: Mapping[str, Any],
    drift: Mapping[str, Any],
) -> dict[str, Any] | None:
    """环境变化时收口旧 delivery 仍在推进的最新段：把它的下一阶段以环境漂移记为失败（不调用执行器）。"""

    if chain["latest_closed"]:
        return None
    if delivery["plan"].get("schema_version") == delivery_execution.PLAN_V2:
        # 没发生新的物理操作，就没有可以签发的真实 StageFact。
        # 旧链和全部 guard 操作原样保留；新环境只通过新 delivery 的门禁与计划继续。
        return {"status": "retained_as_previous_environment", "delivery_id": delivery["delivery_id"], "drift": dict(drift), "fact_ref": None, "ledger_events": []}
    number, reference, _reservation = _segment(context, chain)
    if number == 0 and chain.get("terminal_ref") is None:
        return None  # 首轮交付还没有任何事实：旧 delivery 原样保留，不需要收口。
    step = _next_stage(context, chain)
    assert step is not None
    stage, previous = step
    delivery_id = str(delivery["delivery_id"])
    segment = f"dr{number}" if number else "dr0"
    entries = [
        _entry("stage-failure", "failed", root_cause_id=ROOT_CAUSE_ENVIRONMENT_DRIFT, drift=dict(drift)),
        _entry("run", "pass", segment=segment, executor="not-invoked"),
    ]
    manifest = _manifest(context, f"{delivery_id}-{segment}-{stage}-environment-drift", entries)
    with _write_scope(context):
        evidence_ref = context.session.seal_object("operational_evidence", manifest)
        payload = _stage_payload(
            context,
            delivery,
            stage,
            previous=previous,
            recovery=(segment, reference) if number and reference is not None else None,
            evidence_ref=evidence_ref,
            observed_seconds=0,
            result="failed",
        )
        recorded = _record_stage(context, stage, payload)
        appended = _catch_up_ledger(context)
    return {"stage": stage, "segment": segment, "fact_ref": recorded["fact_ref"], "ledger_events": appended}


# ---------------------------------------------------------------------------
# 六、交付包与终态收据
# ---------------------------------------------------------------------------


def _private_bindings(context: DeliveryContext, delivery_id: str) -> list[dict[str, Any]]:
    """本 delivery 全部阶段事实（含失败阶段）证据清单登记的私有文件，逐个核对仍在数据根内且摘要不变。"""

    bindings: dict[str, dict[str, Any]] = {}
    for fact in store_receipts.delivery_facts(context.store, context.campaign_id, delivery_id):
        for reference in fact["payload"]["evidence_refs"]:
            manifest = context.store.load_object(reference)["payload"]
            for entry in manifest["entries"]:
                if not str(entry["id"]).startswith("private-"):
                    continue
                facts = entry["facts"]
                binding = {"path": str(facts["path"]), "sha256": str(facts["sha256"]), "bytes": int(facts["bytes"])}
                path = context.data_root / binding["path"]
                if path.is_symlink() or not path.is_file() or file_sha256(path) != binding["sha256"]:
                    raise ConfigurationError(f"交付私有文件缺失或摘要漂移：{binding['path']}")
                bindings[binding["path"]] = binding
    if not bindings:
        raise ConfigurationError(f"交付 {delivery_id} 没有可归档的私有文件。")
    return [bindings[path] for path in sorted(bindings)]


def _package_review(context: DeliveryContext, delivery: Mapping[str, Any], chain: Mapping[str, Any]) -> dict[str, Any]:
    """确定性推出私有归档清单、部署配置与交付包（不封存）；审核摘要即交付包的 ``identity_sha256``。"""

    if delivery["plan"].get("schema_version") == delivery_execution.PLAN_V2:
        return _typed_package_review(context, delivery, chain)

    delivery_id = str(delivery["delivery_id"])
    plan = delivery["plan"]
    entries = _private_bindings(context, delivery_id)
    archive = _with_identity(
        {
            "schema_version": PRIVATE_ARCHIVE_MANIFEST_SCHEMA,
            "campaign_id": context.campaign_id,
            "persona": dict(context.persona),
            "manifest_id": f"{delivery_id}-private-archive",
            "redacted": True,
            "entries": entries,
            "entry_count": len(entries),
            "total_bytes": sum(item["bytes"] for item in entries),
            "manifest_sha256": canonical_sha256(entries),
        }
    )
    freeze = context.store.load_object(plan["environment_freeze_ref"])["payload"]
    deployment = _manifest(
        context,
        f"{delivery_id}-deployment-configuration",
        [entry for entry in freeze["entries"] if str(entry["id"]).startswith("deployment-")],
    )
    valid = [dict(chain["valid_stage_refs"][stage]) for stage in STAGES]
    public = _sorted_refs(
        reference for fact_ref in valid for reference in context.store.load_fact(fact_ref)["payload"]["evidence_refs"]
    )
    rollback = [dict(reference) for reference in plan["rollback_material_refs"]]
    package = _with_identity(
        {
            "schema_version": CANDIDATE_DELIVERY_PACKAGE_SCHEMA,
            "campaign_id": context.campaign_id,
            "persona": dict(context.persona),
            "package_id": f"{delivery_id}-package",
            "delivery_id": delivery_id,
            "candidate_id": plan["candidate_id"],
            "candidate_ref": plan["candidate_ref"],
            "candidate_build_receipt_ref": plan["candidate_build_receipt_ref"],
            "validation_completed_ref": plan["validation_completed_ref"],
            "acceptance_ref": plan["acceptance_ref"],
            "candidate_evidence_package_ref": plan["candidate_evidence_package_ref"],
            "delivery_plan_ref": dict(delivery["plan_ref"]),
            "delivery_fact_refs": valid,
            "deployment_configuration_ref": _object_ref("operational_evidence", deployment),
            "public_evidence_index_refs": public,
            "rollback_material_refs": rollback,
            "private_archive_manifest_ref": _object_ref("private_archive_manifest", archive),
            "candidate_image_digest": plan["candidate_image_digest"],
            "artifact_count": 1 + len(public) + len(rollback),
        }
    )
    return {
        "review_sha256": package["identity_sha256"],
        "package": package,
        "private_archive_manifest": archive,
        "deployment_configuration": deployment,
    }


def _frozen_executor(context: DeliveryContext, delivery: Mapping[str, Any]) -> Any:
    core = delivery_execution.evidence_facts(context.store, delivery["plan"]["executor_configuration_ref"], "configuration")
    return _environment_executor("arm64-isolated", configuration=core["configuration"], execution_context=_executor_context(context, delivery["delivery_id"], core["binding"]))


def _typed_package_review(context: DeliveryContext, delivery: Mapping[str, Any], chain: Mapping[str, Any]) -> dict[str, Any]:
    """从全操作索引生成归档；未入 Store 的操作、恢复未决或发布原件缺失都阻断收口。"""

    inventory = _frozen_executor(context, delivery).inventory()
    _expect(isinstance(inventory, Mapping) and set(inventory) == {"executions", "restorations", "operations_index_binding", "origin"}, "完整交付 inventory 字段不闭合")
    if not isinstance(inventory["executions"], list) or not inventory["executions"] or not isinstance(inventory["restorations"], list):
        raise ConfigurationError("完整交付 inventory 没有合法执行与恢复集合。")
    plan = delivery["plan"]
    index_binding = inventory["operations_index_binding"]
    index_content = {"campaign_id": context.campaign_id, "delivery_id": delivery["delivery_id"], "operation_index_binding": index_binding}
    source = delivery_execution._verified_source(context.store, index_content, inventory["origin"])
    index = delivery_execution._source_json(source, index_binding)
    if any(operation.get("status") not in {"execution_committed", "restored"} for operation in index.get("operations", [])):
        raise ConfigurationError("交付全操作索引仍有恢复未决操作，不能 finalize。")
    manifests = []
    execution_refs, restoration_refs, publications = [], [], []
    for kind, cores in (("stage_execution", inventory["executions"]), ("restore", inventory["restorations"])):
        for core in cores:
            manifest = _typed_manifest(context, f"{delivery['delivery_id']}-{kind.replace('_', '-')}-{canonical_sha256(core)[:24]}", kind, core)
            # 执行对象必须沿原发布意图的确定性名字形成同一引用，不能另签复本。
            if kind == "stage_execution":
                manifest = _typed_manifest(context, f"{delivery['delivery_id']}-{core['operation_id']}-execution", kind, core)
                path = _publication_root(context, delivery["delivery_id"]) / f"{core['operation_id']}.json"
                publications.append(_bound_local(context, path))
            delivery_execution.validate_evidence_graph(context.store, manifest)
            reference = _object_ref("operational_evidence", manifest)
            (execution_refs if kind == "stage_execution" else restoration_refs).append(reference)
            manifests.append(manifest)
    publications.sort(key=lambda item: item["path"])
    input_material = [context.store.load_object(plan[key])["payload"] for key in delivery_execution.PLAN_EXTRA_FIELDS]
    input_material.extend(manifests)
    input_material.extend([index, index_binding, publications])
    entries = delivery_execution.complete_delivery_raw_bindings(context.store, plan, source, input_material)
    archive = _with_identity({"schema_version": PRIVATE_ARCHIVE_MANIFEST_SCHEMA, "campaign_id": context.campaign_id, "persona": dict(context.persona), "manifest_id": f"{delivery['delivery_id']}-private-archive", "redacted": True, "entries": entries, "entry_count": len(entries), "total_bytes": sum(row["bytes"] for row in entries), "manifest_sha256": canonical_sha256(entries)})
    valid = [dict(chain["valid_stage_refs"][stage]) for stage in STAGES]
    public = _sorted_refs(reference for fact_ref in valid for reference in context.store.load_fact(fact_ref)["payload"]["evidence_refs"])
    rollback = [dict(reference) for reference in plan["rollback_material_refs"]]
    package = _with_identity({"schema_version": delivery_execution.PACKAGE_V2, "campaign_id": context.campaign_id, "persona": dict(context.persona), "package_id": f"{delivery['delivery_id']}-package", "delivery_id": delivery["delivery_id"], "candidate_id": plan["candidate_id"], **{key: plan[key] for key in ("candidate_ref", "candidate_build_receipt_ref", "validation_completed_ref", "acceptance_ref", "candidate_evidence_package_ref")}, "delivery_plan_ref": dict(delivery["plan_ref"]), "delivery_fact_refs": valid, "deployment_configuration_ref": plan["deployment_configuration_ref"], "public_evidence_index_refs": public, "rollback_material_refs": rollback, "private_archive_manifest_ref": _object_ref("private_archive_manifest", archive), "candidate_image_digest": plan["candidate_image_digest"], "execution_evidence_refs": _sorted_refs(execution_refs), "restoration_evidence_refs": _sorted_refs(restoration_refs), "operation_index_binding": index_binding, "operation_index_origin": inventory["origin"], "publication_intent_bindings": publications, "artifact_count": 2 + len(public) + len(rollback) + len(execution_refs) + len(restoration_refs) + len(publications)})
    return {"review_sha256": package["identity_sha256"], "package": package, "private_archive_manifest": archive, "deployment_configuration": context.store.load_object(plan["deployment_configuration_ref"])["payload"], "operational_evidence": manifests}


# ---------------------------------------------------------------------------
# 六之二、交付阶段结果与 VC-6 完成（模块说明第八节）
# ---------------------------------------------------------------------------


def stage_result_path(campaign_dir: Path, candidate_id: str) -> Path:
    """交付阶段结果坐标 ``delivery/<候选>/result.json``（与 accept 的 ``acceptance/<候选>/result.json`` 同一做法）。"""

    if not SAFE_ID_RE.fullmatch(candidate_id or ""):
        raise ConfigurationError("交付阶段结果的候选 ID 非法。")
    return Path(campaign_dir) / STAGE_RESULT_DIRECTORY / candidate_id / STAGE_RESULT_FILENAME


def load_stage_result(campaign_dir: Path, candidate_id: str, *, manifest: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """读取并核对交付阶段结果：没有时返回 None；合同、字段闭集、Campaign、候选、状态与三项文件绑定任一不符即失败关闭。"""

    campaign_dir = Path(campaign_dir)
    path = stage_result_path(campaign_dir, candidate_id)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError("交付阶段结果路径不可信。")
    if not path.exists():
        return None
    result = claude_upgrade_vc._read_json(path, "交付阶段结果")
    campaign_id = (manifest or claude_upgrade_vc._read_json(campaign_dir / "campaign.json", "Campaign 核心清单")).get("campaign_id")
    if (
        set(result) != STAGE_RESULT_FIELDS
        or result.get("schema_version") not in {STAGE_RESULT_SCHEMA, STAGE_RESULT_V2_SCHEMA}
        or result.get("client") != "claude"
        or result.get("campaign_id") != campaign_id
        or result.get("stage") != STAGE_NAME
        or result.get("candidate_id") != candidate_id
        or result.get("status") != "complete"
        or result.get("release_state") != RELEASE_STATE
        or result.get("production_state") != CANDIDATE_PRODUCTION_STATE
        or result.get("vc6_status") not in {VC6_STATUS_COMPLETE, VC6_STATUS_PRODUCTION_PENDING}
        or not _orchestrator()._is_rfc3339_timestamp(result.get("completed_at_utc"))
    ):
        raise ConfigurationError("交付阶段结果合同、Campaign、候选或状态不一致。")
    checkpoint = result.get("vc5_checkpoint") if isinstance(result.get("vc5_checkpoint"), Mapping) else {}
    for label, binding in (
        ("VC-5 checkpoint", {"path": checkpoint.get("path"), "sha256": checkpoint.get("sha256")}),
        ("accept 阶段结果", result.get("accept_result")),
        ("VC-4 构建收据", result.get("vc4_build_receipt")),
    ):
        claude_upgrade_vc._bound_file(campaign_dir, binding, f"交付阶段结果登记的{label}")
    # 这是当前操作入口的消费；历史 v1 幂等引用不授予新版本的完成权。
    if result.get("schema_version") == STAGE_RESULT_V2_SCHEMA:
        validate_current_stage_result(campaign_dir, result)
    else:
        store, campaign = claude_upgrade_store.open_campaign_store(campaign_dir, claude_upgrade_vc._read_json(campaign_dir / "campaign.json", "Campaign 核心清单"))
        if delivery_execution.requires_current_contract(store, campaign):
            raise ConfigurationError("当前 VC-6 不接受历史交付结果 v1 作为真实交付完成证明。")
    return result


def validate_current_stage_result(campaign_dir: Path, result: Mapping[str, Any]) -> None:
    """只读重验下层 VC-5 与交付图，不反向调用自身 VC-6 checkpoint。"""

    try:
        context = _resolve(campaign_dir, str(result.get("candidate_id", "")), action="重验真实交付结果")
        if result.get("schema_version") != STAGE_RESULT_V2_SCHEMA or result.get("candidate_revision") != context.revision or result.get("campaign_purpose") != context.manifest.get("campaign_purpose"):
            raise ConfigurationError("真实交付结果的版本、当前 revision 或用途不一致。")
        receipt = store_receipts.replay_receipt(
            context.store, dict(result["receipt_ref"]), current_delivery=True
        )
        for key, receipt_key in (("delivery_plan_ref", "delivery_plan_ref"), ("package_ref", "candidate_delivery_package_ref")):
            if result[key] != receipt[receipt_key]:
                raise ConfigurationError("交付结果与 Store 的交付计划或交付包不一致。")
        if result["delivery_id"] != context.store.load_object(receipt["delivery_plan_ref"])["payload"]["delivery_id"]:
            raise ConfigurationError("交付结果与真实执行 delivery 坐标不一致。")
        for key in ("validation_completed_ref", "acceptance_ref", "candidate_evidence_package_ref"):
            if result["store"][key] != receipt[key] or receipt[key] != context.handoff[key]:
                raise ConfigurationError("交付结果、当前 VC-5 与 Store 收据不属于同一条验收链。")
        for key in ("vc5_checkpoint", "accept_result", "vc4_build_receipt"):
            if result[key] != context.entry[key]:
                raise ConfigurationError("交付结果的下层验收文件绑定不是当前冻结输入。")
        expected = VC6_STATUS_COMPLETE if result["campaign_purpose"] == "validation_only" else VC6_STATUS_PRODUCTION_PENDING
        if result["vc6_status"] != expected or result["production_state"] != CANDIDATE_PRODUCTION_STATE:
            raise ConfigurationError("候选交付终态不能冒充生产激活完成。")
    except ControlError as error:
        raise ConfigurationError(f"真实交付结果重验失败：{error}") from error


def _require_parent_batch(label: str) -> None:
    """第九节：Formal Campaign 的交付写入只经父批次派发。直接 CLI 写的 VC-6 checkpoint 不会有批次层补写的 stage_completed，
    而 checkpoint 已存在后共享入口又拒绝再编译 VC-6 批次——账本会永远停在 VC-6 进行中。"""

    if not claude_upgrade_batch.in_campaign_run():
        raise ConfigurationError(
            f"{label} 是 VC-6 批次动作：Formal Campaign 里只能由 compile-and-run-vc-batch 经父监督器派发"
            "（动作计划见 claude_upgrade_delivery.run_action／finalize_action）；不带 --approve-sha256 的 finalize 只看审核内容，"
            "可以直接执行。"
        )


def _complete_vc6(
    context: DeliveryContext, delivery: Mapping[str, Any], package_ref: Mapping[str, Any], receipt_ref: Mapping[str, Any]
) -> dict[str, Any]:
    """收据签发、补账之后：写交付阶段结果；validation_only 再写 VC-6 checkpoint（第八节）。只写一次，重跑逐字复用。"""

    purpose = str(context.manifest.get("campaign_purpose"))
    vc6_status = VC6_STATUS_COMPLETE if purpose == "validation_only" else VC6_STATUS_PRODUCTION_PENDING
    payload = {
        "schema_version": STAGE_RESULT_SCHEMA,
        "client": "claude",
        "campaign_id": context.campaign_id,
        "stage": STAGE_NAME,
        "candidate_id": context.candidate_id,
        "candidate_revision": context.revision,
        "campaign_purpose": purpose,
        "status": "complete",
        "delivery_id": str(delivery["delivery_id"]),
        "delivery_plan_ref": dict(delivery["plan_ref"]),
        "package_ref": dict(package_ref),
        "receipt_ref": dict(receipt_ref),
        "release_state": RELEASE_STATE,
        "production_state": CANDIDATE_PRODUCTION_STATE,
        "vc5_checkpoint": dict(context.entry["vc5_checkpoint"]),
        "accept_result": dict(context.entry["accept_result"]),
        "vc4_build_receipt": dict(context.entry["vc4_build_receipt"]),
        "store": {
            key: dict(context.handoff[key])
            for key in ("validation_completed_ref", "acceptance_ref", "candidate_evidence_package_ref")
        },
        "vc6_status": vc6_status,
        "completed_at_utc": _orchestrator()._utc_now(),
    }
    if delivery["plan"].get("schema_version") == delivery_execution.PLAN_V2:
        payload["schema_version"] = STAGE_RESULT_V2_SCHEMA
        validate_current_stage_result(context.campaign_dir, payload)
    elif delivery_execution.requires_current_contract(context.store, context.campaign):
        raise ConfigurationError("旧交付收据可只读复用，不能生成当前 v2 交付结果或 VC-6 完成证明。")
    path = stage_result_path(context.campaign_dir, context.candidate_id)
    ensure_private_directory(path.parent, context.campaign_dir)
    sealed = claude_upgrade_seal._write_once_or_reuse(path, payload, ignore=("completed_at_utc",))
    completion: dict[str, Any] = {
        "vc6_status": vc6_status,
        "stage_result": claude_upgrade_vc.vc_control_binding(context.campaign_dir, path),
        "vc6_checkpoint": None,
    }
    if vc6_status == VC6_STATUS_COMPLETE:
        checkpoint = claude_upgrade_vc.complete_candidate_phase(
            context.campaign_dir,
            context.manifest,
            phase=DELIVERY_PHASE,
            revision=context.revision,
            stage_receipt_path=path,
            completed_at_utc=str(sealed["completed_at_utc"]),
            execute_item_ids=[],
            reuse_item_ids=[],
            live_request_count=0,
            scanned_bytes=0,
        )
        checkpoint_path = claude_upgrade_vc.vc_checkpoint_path(context.campaign_dir, DELIVERY_PHASE, revision=context.revision)
        completion["vc6_checkpoint"] = claude_upgrade_vc.vc_checkpoint_reference(context.campaign_dir, checkpoint_path, checkpoint)
        completion["next_command"] = (
            "VC-6 已完成（validation_only 候选交付终点）：本批次父 run 成功后由批次层登记 stage_completed；"
            "候选交付收据与交付包交操作员发布"
        )
    else:
        completion["next_command"] = (
            f"候选交付终点已登记；production_replacement 的生产激活属 {PRODUCTION_WORK_ITEM}，尚未支持，VC-6 保持进行中"
        )
    return completion


# ---------------------------------------------------------------------------
# 六之三、VC-6 批次动作（模块说明第九节）
# ---------------------------------------------------------------------------


def run_action(
    campaign_dir: Path,
    candidate_id: str,
    *,
    environment_executor: str,
    observation_window_seconds: int = DEFAULT_OBSERVATION_WINDOW_SECONDS,
    executor_configuration: Path | None = None,
    executor_configuration_sha256: str | None = None,
) -> dict[str, Any]:
    """run 批次的唯一动作：受管 Python 直接调用 ``deliver-candidate run``，超时为观察窗加余量。"""

    window = int(observation_window_seconds)
    if window <= 0:
        raise ConfigurationError("--observation-window-seconds 必须大于 0。")
    config_flags: list[Any] = []
    if (executor_configuration is None) != (executor_configuration_sha256 is None):
        raise ConfigurationError("执行配置路径与 SHA 必须成对给出。")
    if executor_configuration is not None:
        actual = claude_upgrade_batch._campaign_input_file(campaign_dir, str(executor_configuration), "--executor-configuration")
        if actual != executor_configuration_sha256:
            raise ConfigurationError("run 动作执行配置字节与 SHA 不一致。")
        config_flags = ["--executor-configuration", executor_configuration, "--executor-configuration-sha256", actual]
    return claude_upgrade_batch.managed_action(
        RUN_ACTION_ID,
        f"{DELIVERY_PHASE}:{RUN_ACTION_ID}",
        [
            "deliver-candidate", "run", "--campaign-dir", campaign_dir, "--candidate-id", candidate_id,
            "--environment-executor", environment_executor, "--observation-window-seconds", window, *config_flags,
        ],
        item_ids=[RUN_ACTION_ID],
        timeout_seconds=window + RUN_TIMEOUT_MARGIN_SECONDS,
    )


def finalize_action(campaign_dir: Path, candidate_id: str, approve_sha256: str) -> dict[str, Any]:
    """finalize 批次的唯一动作：带上一批 run 输出的交付包审核摘要。"""

    if not SHA256_RE.fullmatch(str(approve_sha256)):
        raise ConfigurationError("--approve-sha256 必须是 64 位十六进制摘要。")
    return claude_upgrade_batch.managed_action(
        FINALIZE_ACTION_ID,
        f"{DELIVERY_PHASE}:{FINALIZE_ACTION_ID}",
        ["deliver-candidate", "finalize", "--campaign-dir", campaign_dir, "--candidate-id", candidate_id, "--approve-sha256", approve_sha256],
        item_ids=[FINALIZE_ACTION_ID],
        timeout_seconds=FINALIZE_TIMEOUT_SECONDS,
    )


def vc6_action_plan(action: Mapping[str, Any]) -> dict[str, Any]:
    """一个交付动作的 VC-6 批次动作计划（按共享合同校验，复用项为空）。"""

    return claude_upgrade_batch.action_plan([action], phase=DELIVERY_PHASE)


def batch_action_kind(action: Any, manifest: Mapping[str, Any]) -> str | None:
    """父批次清单里的动作是不是 VC-6 交付动作：是则返回 ``run``／``finalize``，否则 None（适配器
    ``candidate_post_run_extra_action`` 的判定，第九节）。按形态精确闭合：阶段 VC-6，动作 ID 与唯一项名相同且是两者之一，命令是
    受管 Python 对 ``claude_upgrade.py`` 的直接调用、子命令与动作对应，成对旗标恰好是该动作的闭集，``--campaign-dir`` 指向本
    Campaign（目录名即 Campaign ID），``--candidate-id`` 是清单的候选，窗口与批准摘要形态合法。判定失败一律返回 None。"""

    try:
        if not isinstance(action, Mapping) or not isinstance(manifest, Mapping) or manifest.get("phase") != DELIVERY_PHASE:
            return None
        action_id = action.get("action_id")
        kind = BATCH_ACTION_KINDS.get(action_id) if isinstance(action_id, str) else None
        if kind is None or action.get("item_ids") != [action_id]:
            return None
        command = action.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(value, str) for value in command):
            return None
        argv = claude_upgrade_batch._orchestrator_argv(command)
        if argv[:2] != ["deliver-candidate", kind]:
            return None
        flags = claude_upgrade_batch._paired_flags("deliver-candidate", argv[2:])
        required = BATCH_ACTION_FLAGS[kind]
        if set(flags) != required and not (kind == "run" and set(flags) == required | EXECUTOR_CONFIGURATION_FLAGS):
            return None
        if kind == "run" and set(flags) & EXECUTOR_CONFIGURATION_FLAGS:
            actual = claude_upgrade_batch._campaign_input_file(Path(flags["--campaign-dir"]), flags["--executor-configuration"], "--executor-configuration")
            if actual != flags["--executor-configuration-sha256"]:
                return None
        campaign_id = manifest.get("campaign_id")
        if not isinstance(campaign_id, str) or Path(flags["--campaign-dir"]).name != campaign_id:
            return None
        candidate = manifest.get("candidate_id")
        if not SAFE_ID_RE.fullmatch(flags["--candidate-id"]) or (isinstance(candidate, str) and flags["--candidate-id"] != candidate):
            return None
        if kind == "run":
            window = flags["--observation-window-seconds"]
            if not SAFE_NAME_RE.fullmatch(flags["--environment-executor"]) or not window.isdigit() or int(window) <= 0:
                return None
        elif not SHA256_RE.fullmatch(flags["--approve-sha256"]):
            return None
    except Exception:  # noqa: BLE001 —— 判定失败一律按「不是」处理
        return None
    return kind


# ---------------------------------------------------------------------------
# 六之四、status 用的只读交付状态
# ---------------------------------------------------------------------------


def delivery_status(campaign_dir: Path, candidate_id: str) -> dict[str, Any]:
    """VC-5 已完成之后的交付状态与下一命令（只读：入口重放、Store 交付链与交付阶段结果；``status`` 转调，状态名见
    ``claude_upgrade_recovery`` 模块说明）。返回 ``{status, next_command, delivery_id, ...}``。"""

    context = _resolve(Path(campaign_dir), candidate_id, action="status")
    run_hint = (
        f"compile-and-run-vc-batch --phase {DELIVERY_PHASE} 派交付 run 批次（动作取 claude_upgrade_delivery.run_action，"
        f"候选 {candidate_id}）"
    )
    result = load_stage_result(context.campaign_dir, candidate_id, manifest=context.manifest)
    if result is not None:
        base = {"delivery_id": result["delivery_id"], "stage_result": claude_upgrade_vc.vc_control_binding(
            context.campaign_dir, stage_result_path(context.campaign_dir, candidate_id))}
        if result["vc6_status"] == VC6_STATUS_PRODUCTION_PENDING:
            return {
                **base,
                "status": "production_activation_pending",
                "next_command": f"候选交付终点已登记；production_replacement 的生产激活属 {PRODUCTION_WORK_ITEM}，尚未支持",
            }
        checkpoint = claude_upgrade_vc.vc_checkpoint_path(context.campaign_dir, DELIVERY_PHASE, revision=context.revision)
        if checkpoint.is_file() and not checkpoint.is_symlink():
            return {**base, "status": "ready_for_operator_release", "next_command": "validation_only 候选已完成 VC-6 交付登记"}
        return {
            **base,
            "status": "delivery_completion_pending",
            "next_command": "交付阶段结果已写、VC-6 checkpoint 未写：逐字重派 finalize 批次补写",
        }
    deliveries = _deliveries(context)
    if not deliveries:
        return {"status": "ready", "delivery_id": None, "next_command": f"VC-6：{run_hint}"}
    current = deliveries[-1]
    chain = _chain(context, current["delivery_id"])
    base = {"delivery_id": current["delivery_id"]}
    if chain["receipt_refs"]:
        return {
            **base,
            "status": "delivery_completion_pending",
            "next_command": "候选交付收据已签、交付阶段结果未写：逐字重派 finalize 批次补写",
        }
    if chain["complete"] and chain["pending_invalidation_ref"] is None:
        review = _package_review(context, current, chain)["review_sha256"]
        return {
            **base,
            "status": "delivery_awaiting_finalize",
            "review_sha256": review,
            "next_command": (
                f"审核交付包（deliver-candidate finalize --candidate-id {candidate_id} 不带批准即输出审核内容）后，派 finalize 批次"
                f"（动作取 claude_upgrade_delivery.finalize_action，--approve-sha256 {review}）"
            ),
        }
    if chain["pending_invalidation_ref"] is not None:
        return {
            **base,
            "status": "delivery_invalidation_pending",
            "next_command": f"有已批准的阶段失效待消费：{run_hint}，以 approved_invalidation 恢复段从被失效的阶段起重跑",
        }
    if chain["latest_closed"]:
        return {
            **base,
            "status": "delivery_stage_failed",
            "next_command": f"修复后{run_hint}：自动预约下一恢复段，从失败阶段续跑；已通过的阶段要重跑先 delivery-stage-invalidate",
        }
    return {
        **base,
        "status": "delivery_in_progress",
        "next_command": f"交付段尚未收口：{run_hint} 接着跑（父批次失败时先 reconcile-supervisor-run 对账、逐字重派）",
    }


# ---------------------------------------------------------------------------
# 七、子命令
# ---------------------------------------------------------------------------


def add_arguments(name: str, parser: argparse.ArgumentParser) -> None:
    """``claude_upgrade.py`` 的解析器循环里转调（``--campaign-dir`` 由编排器统一加）。"""

    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--delivery-id", help="核对用：必须是当前交付（缺省取当前交付）")
    if name == "deliver-candidate":
        parser.add_argument("delivery_action", choices=DELIVERY_ACTIONS)
        parser.add_argument("--executor-configuration", type=Path, help="run：Campaign 内已冻结的 ARM64 执行配置原件")
        parser.add_argument("--executor-configuration-sha256", help="run：配置原始字节 SHA；与路径成对传入父动作")
        parser.add_argument("--environment-executor", help="run：交付环境执行器名字（第二节）")
        parser.add_argument(
            "--observation-window-seconds", type=int,
            help=f"run：新交付计划冻结的稳定观察窗（秒，缺省 {DEFAULT_OBSERVATION_WINDOW_SECONDS}）；已有计划时只用于核对",
        )
        parser.add_argument("--rerun-from", choices=STAGES, help="run：核对新恢复段的起点（不能用来重跑已通过的阶段）")
        parser.add_argument("--approve-sha256", help="finalize：交付包审核摘要；缺省只输出审核内容")
    else:
        parser.add_argument("invalidation_action", choices=INVALIDATION_ACTIONS)
        parser.add_argument("--stage", required=True, choices=STAGES)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--evidence", action="append", type=Path, default=[], help="支撑失效理由的 Campaign 内文件，可多次给出")
        parser.add_argument("--approve-sha256", help="apply：preview 输出的 review_sha256")
        parser.add_argument("--approved-by", help="apply：批准人")


def command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``claude_upgrade.main`` 转调入口：返回 ``(结果, 退出码)``。"""

    if arguments.command == "deliver-candidate":
        if arguments.delivery_action == "run":
            return _run(arguments)
        return _finalize(arguments)
    if arguments.command == "delivery-stage-invalidate":
        return _invalidate(arguments)
    raise ConfigurationError(f"不是交付子命令：{arguments.command}")


def _summary(context: DeliveryContext, delivery: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "campaign_id": context.campaign_id,
        "candidate_id": context.candidate_id,
        "revision": context.revision,
        "delivery_id": delivery["delivery_id"] if delivery is not None else None,
        "delivery_plan_ref": dict(delivery["plan_ref"]) if delivery is not None else None,
        "live_request_count": 0,
    }


def _run(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    _require_parent_batch("deliver-candidate run")
    context = _resolve(Path(arguments.campaign_dir), str(arguments.candidate_id), action="deliver-candidate run")
    configuration, configuration_binding = _configuration_input(context, arguments)
    executor = None
    rerun_from = arguments.rerun_from
    with _delivery_lock(context):
        with _write_scope(context):
            ledger_events = _catch_up_ledger(context)
        deliveries = _deliveries(context)
        current = deliveries[-1] if deliveries else None
        if arguments.delivery_id is not None and (current is None or arguments.delivery_id != current["delivery_id"]):
            raise ConfigurationError(f"--delivery-id {arguments.delivery_id} 不是当前交付。")
        if current is not None:
            _require_plan_current(context, current)
            chain = _chain(context, current["delivery_id"])
            if chain["receipt_refs"]:
                if delivery_execution.requires_current_contract(context.store, context.campaign):
                    for reference in chain["receipt_refs"]:
                        delivery_execution.validate_delivery_receipt(context.store, context.store.load_receipt(reference), current=True)

                return {
                    **_summary(context, current),
                    "status": "finalized",
                    "receipt_refs": chain["receipt_refs"],
                    "chain": chain,
                    "ledger_events": ledger_events,
                }, 0
        _require_vc6_open(context, _ledger_state(context))
        window = arguments.observation_window_seconds
        if current is not None and window is not None and window != current["plan"]["observation_window_seconds"]:
            raise ConfigurationError(
                f"--observation-window-seconds {window} 与当前交付计划冻结的观察窗 {current['plan']['observation_window_seconds']} 不同。"
            )
        if window is None:
            window = int(current["plan"]["observation_window_seconds"]) if current is not None else DEFAULT_OBSERVATION_WINDOW_SECONDS
        if window <= 0:
            raise ConfigurationError("--observation-window-seconds 必须大于 0。")
        proposed = current["delivery_id"] if current is not None else _delivery_id(context, 1)
        execution_context = _executor_context(context, proposed, configuration_binding) if configuration_binding is not None else None
        executor = _environment_executor(arguments.environment_executor, configuration=configuration, execution_context=execution_context)
        if execution_context is not None:
            recovery = executor.recover_pending({"reason": "entry"})
            if current is not None and current["plan"].get("schema_version") == delivery_execution.PLAN_V2:
                # 先补缺失发布，再重做便宜门禁；已完成物理操作不得因新 freeze 失败而被遗漏。
                for operation in recovery.get("operations", []):
                    core = operation.get("execution")
                    if core is not None:
                        published = _publish_execution(context, current, core)
                        ledger_events += published["ledger_events"]
            if recovery.get("blocked") or recovery.get("unresolved_operation_ids"):
                raise ConfigurationError("真实交付仍有恢复未决操作，修复同一恢复记录后才能继续。")
        snapshot = _freeze(context, executor, str(arguments.environment_executor), proposed, window)
        failed_gates = [item["id"] for item in snapshot["gates"] if item["result"] != "pass"]
        if failed_gates:
            return {
                **_summary(context, current),
                "status": "delivery_gates_failed",
                "failed_gates": failed_gates,
                "gates": snapshot["gates"],
                "ledger_events": ledger_events,
                "next_command": "修复交付环境后重跑 deliver-candidate run（零写入）",
            }, 3
        switched: dict[str, Any] | None = None
        if current is not None:
            drift = _plan_drift(current["plan"], snapshot)
            if drift:
                if rerun_from is not None:
                    raise ConfigurationError("交付环境已变化，要开新交付从首阶段交付：--rerun-from 不适用。")
                closed = _close_for_drift(context, current, chain, drift)
                switched = {"previous_delivery_id": current["delivery_id"], "drift": drift, "closed": closed}
                current = None
                if configuration_binding is not None:
                    proposed = _delivery_id(context, len(_deliveries(context)) + 1)
                    executor = _environment_executor(arguments.environment_executor, configuration=configuration, execution_context=_executor_context(context, proposed, configuration_binding))
                    recovered = executor.recover_pending({"reason": "entry"})
                    if recovered.get("blocked") or recovered.get("unresolved_operation_ids"):
                        raise ConfigurationError("新交付仍有未解决的恢复操作。")
                    snapshot = _freeze(context, executor, str(arguments.environment_executor), proposed, window)
                    if any(item["result"] != "pass" for item in snapshot["gates"]):
                        raise ConfigurationError("按新 delivery 重做的冻结门禁未通过，零现场变更拒绝。")

        if current is None:
            current = _open_delivery(context, len(_deliveries(context)) + 1, snapshot, window)
        stages: list[dict[str, Any]] = []
        reservation: dict[str, Any] | None = None
        while True:
            chain = _chain(context, current["delivery_id"])
            base = {**_summary(context, current), "environment_changed": switched, "recovery": reservation, "stages": stages}
            if chain["complete"] and chain["pending_invalidation_ref"] is None:
                if rerun_from is not None:
                    raise ConfigurationError(
                        "当前交付四阶段有效链已全部通过：已通过的阶段要重跑，须先 delivery-stage-invalidate 批准阶段失效。"
                    )
                review = _package_review(context, current, chain)
                return {
                    **base,
                    "status": "awaiting_finalize",
                    "chain": chain,
                    "package_review": review,
                    "ledger_events": ledger_events + [item for stage in stages for item in stage["ledger_events"]],
                    "next_command": (
                        f"审核交付包后执行 deliver-candidate finalize --candidate-id {context.candidate_id} "
                        f"--approve-sha256 {review['review_sha256']}"
                    ),
                }, 2
            step = _next_stage(context, chain)
            if step is None:
                reservation = _reserve(context, current, chain, snapshot, rerun_from)
                ledger_events += reservation["ledger_events"]
                rerun_from = None
                continue
            if rerun_from is not None:
                raise ConfigurationError(
                    "当前交付段仍在推进：--rerun-from 只在预约新恢复段时核对起点；重跑同一条命令即沿当前段接着跑。"
                )
            stage, previous = step
            number, recovery_ref, recovery_payload = _segment(context, chain)
            recovery = (f"dr{number}", recovery_ref, recovery_payload) if number else None
            outcome = _execute_stage(context, executor, str(arguments.environment_executor), current, stage, previous, recovery)
            stages.append(outcome)
            if outcome["result"] == "failed":
                return {
                    **base,
                    "status": "stage_failed",
                    "failed_stage": stage,
                    "root_cause_id": outcome["root_cause_id"],
                    "chain": _chain(context, current["delivery_id"]),
                    "ledger_events": ledger_events + [item for stage in stages for item in stage["ledger_events"]],
                    "next_command": "修复后重跑 deliver-candidate run：自动预约下一恢复段，从失败阶段续跑",
                }, 3


def _finalize(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    context = _resolve(Path(arguments.campaign_dir), str(arguments.candidate_id), action="deliver-candidate finalize")
    with _delivery_lock(context):
        delivery = _select_delivery(context, arguments.delivery_id)
        _require_plan_current(context, delivery)
        chain = _chain(context, delivery["delivery_id"])
        finalized = bool(chain["receipt_refs"])
        if not finalized and not (chain["complete"] and chain["pending_invalidation_ref"] is None):
            raise ConfigurationError(
                f"交付 {delivery['delivery_id']} 的有效链尚未四阶段全部通过或仍有待消费的阶段失效：先 deliver-candidate run。"
            )
        review = _package_review(context, delivery, chain)
        if arguments.approve_sha256 is None:
            return {
                **_summary(context, delivery),
                "status": "finalized" if finalized else "awaiting_finalize",
                "receipt_refs": chain["receipt_refs"],
                "package_review": review,
                "stage_result": load_stage_result(context.campaign_dir, context.candidate_id, manifest=context.manifest),
            }, 0 if finalized else 2
        if arguments.approve_sha256 != review["review_sha256"]:
            raise ConfigurationError("--approve-sha256 与交付包审核摘要不一致：批准的不是这份交付包，请重新查看审核内容。")
        _require_parent_batch("deliver-candidate finalize --approve-sha256")
        if not finalized:
            _require_vc6_open(context, _ledger_state(context))
        with _write_scope(context):
            for manifest in review.get("operational_evidence", []):
                context.session.seal_object("operational_evidence", manifest)
            deployment_ref = context.session.seal_object("operational_evidence", review["deployment_configuration"])
            archive_ref = context.session.seal_object("private_archive_manifest", review["private_archive_manifest"])
            package_ref = context.session.seal_object("candidate_delivery_package", review["package"])
            if deployment_ref != review["package"]["deployment_configuration_ref"] or archive_ref != review["package"]["private_archive_manifest_ref"]:
                raise ConfigurationError("封存后的部署配置或私有归档清单引用与交付包不一致。")
            outcome = context.session._with_input(
                package_ref,
                lambda path: _store(
                    ["candidate-delivery-finalize", "--store", context.store.root, "--campaign", context.campaign_id,
                     "--package-ref", path, "--allow-existing"]
                ),
            )
            ledger_events = _catch_up_ledger(context)
            completion = _complete_vc6(context, delivery, package_ref, dict(outcome["receipt_ref"]))
    return {
        **_summary(context, delivery),
        "status": "finalized",
        "receipt_ref": dict(outcome["receipt_ref"]),
        "reused": bool(outcome["reused"]),
        "package_ref": package_ref,
        "release_state": RELEASE_STATE,
        "ledger_events": ledger_events,
        **completion,
    }, 0


def _invalidation_evidence(context: DeliveryContext, stage: str, stage_ref: Mapping[str, Any], files: Sequence[Path]) -> dict[str, Any]:
    entries = [_entry("invalidated-stage", "pass", stage=stage, fact_ref=dict(stage_ref))]
    for index, value in enumerate(files, start=1):
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"--evidence 不是可信普通文件：{value}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(context.campaign_dir):
            raise ConfigurationError(f"--evidence 必须位于 Campaign 目录内：{value}")
        entries.append(
            _entry(
                f"evidence-{index:03d}", "pass",
                path=resolved.relative_to(context.data_root.resolve(strict=True)).as_posix(),
                sha256=file_sha256(resolved),
                bytes=resolved.stat().st_size,
            )
        )
    return _manifest(context, f"{stage_ref['sha256'][:16]}-stage-invalidation", entries)


def _invalidate(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    # 批次控制面命令：父监督器上下文里由 ``claude_upgrade.main`` 按 ``claude_upgrade_batch.CONTROL_PLANE_COMMANDS`` 先行拒绝。
    action = str(arguments.invalidation_action)
    context = _resolve(Path(arguments.campaign_dir), str(arguments.candidate_id), action=f"delivery-stage-invalidate {action}")
    reason = str(arguments.reason or "").strip()
    if not reason:
        raise ConfigurationError("--reason 不能为空。")
    with _delivery_lock(context):
        delivery = _select_delivery(context, arguments.delivery_id)
        delivery_id = str(delivery["delivery_id"])
        chain = _chain(context, delivery_id)
        if chain["receipt_refs"]:
            raise ConfigurationError(
                f"③ 交付 {delivery_id} 已 finalize（终态收据 {chain['receipt_refs'][0]['sha256']}）：已 finalize 的交付不得申请阶段失效。"
            )
        pending = chain.get("pending_invalidation_ref")
        if pending is not None:
            existing = context.store.load_object(pending)["payload"]
            if action == "apply" and arguments.approve_sha256 == existing["review_sha256"]:
                with _write_scope(context):
                    ledger_events = _catch_up_ledger(context)
                return {
                    **_summary(context, delivery),
                    "status": "applied",
                    "reused": True,
                    "invalidation_ref": dict(pending),
                    "review_sha256": existing["review_sha256"],
                    "ledger_events": ledger_events,
                }, 0
            raise ConfigurationError(
                f"交付 {delivery_id} 已有待消费的阶段失效（{existing['invalidated_stage']}）：先 deliver-candidate run 以恢复段消费它。"
            )
        if not chain["latest_closed"]:
            raise ConfigurationError("阶段失效只能在最新交付段收口（失败或到达 stable_observed）后申请：先让当前段跑完。")
        stage = str(arguments.stage)
        stage_ref = chain["valid_stage_refs"].get(stage)
        if stage_ref is None:
            raise ConfigurationError(f"{stage} 不在当前有效链上（失败阶段以 failure 锚点续跑，不需要失效）。")
        evidence = _invalidation_evidence(context, stage, stage_ref, arguments.evidence)
        review = {
            "campaign_id": context.campaign_id,
            "delivery_id": delivery_id,
            "invalidated_stage": stage,
            "invalidated_stage_ref": dict(stage_ref),
            "reason": reason,
            "evidence_refs": [_object_ref("operational_evidence", evidence)],
        }
        preview = context.session._with_input(
            review,
            lambda path: _store(
                ["delivery-stage-invalidate", "--store", context.store.root, "--campaign", context.campaign_id,
                 "--input", path, "--preview"]
            ),
        )
        review_sha256 = str(preview["review_sha256"])
        if review_sha256 != invalidation_review_sha256(review):
            raise ConfigurationError("Store 给出的审核摘要与编排器复算不一致。")
        if action == "preview":
            return {
                **_summary(context, delivery),
                "status": "preview",
                "review": preview["review"],
                "review_sha256": review_sha256,
                "evidence_manifest": evidence,
                "next_command": (
                    f"delivery-stage-invalidate apply --candidate-id {context.candidate_id} --stage {stage} "
                    f"--reason <同一理由> --approve-sha256 {review_sha256} --approved-by <批准人>"
                ),
            }, 2
        if arguments.approve_sha256 != review_sha256:
            raise ConfigurationError("--approve-sha256 与审核摘要不一致：批准的不是这份内容，请重新 preview。")
        approved_by = str(arguments.approved_by or "").strip()
        if not approved_by:
            raise ConfigurationError("apply 必须给出 --approved-by。")
        _require_vc6_open(context, _ledger_state(context))
        payload = {**review, "approved_by": approved_by, "approved_at_utc": _orchestrator()._utc_now(), "review_sha256": review_sha256}
        with _write_scope(context):
            evidence_ref = context.session.seal_object("operational_evidence", evidence)
            if evidence_ref != review["evidence_refs"][0]:
                raise ConfigurationError("封存后的失效证据清单引用与审核内容不一致。")
            outcome = context.session._with_input(
                payload,
                lambda path: _store(
                    ["delivery-stage-invalidate", "--store", context.store.root, "--campaign", context.campaign_id,
                     "--input", path, "--approve-sha256", review_sha256, "--allow-existing"]
                ),
            )
            ledger_events = _catch_up_ledger(context)
    return {
        **_summary(context, delivery),
        "status": "applied",
        "reused": bool(outcome.get("reused", False)),
        "invalidation_ref": dict(outcome["object_ref"]),
        "review_sha256": review_sha256,
        "ledger_events": ledger_events,
        "next_command": f"deliver-candidate run --candidate-id {context.candidate_id}：以 approved_invalidation 恢复段从 {stage} 起重跑",
    }, 0
