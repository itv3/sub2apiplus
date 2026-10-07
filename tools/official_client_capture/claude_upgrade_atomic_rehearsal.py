"""Claude 的原子双跑画像（Claude 升级控制面设计 B5a-09 续：atomic-double 收据合同 ``official-client-atomic-vc0-vc1-rehearsal/v1``）。

一、为什么要有
==============
发布认证（``certify_release``）除 pre-A3 与 Job 演练外还要绑定一份 atomic-double 收据：在采集容器 ``capture-cli`` 里、两个完全
独立的新根中各从零建一个合成 Formal Campaign，经编排器真实的原子入口 ``compile_and_run_vc_batch`` 完成一次 VC-0→VC-1
（staging 编译 → prepared 父 run → COMMIT → 零网络动作），再证明证据权限收口、失败父 run 的计时账本收口（阶段审核）与四个
负例（篡改批次、重复父 run、deadline 漂移、额外文件），全程零请求、零读取。

共享流程在 ``codex_upgrade_campaign_run_rehearsal_receipt``（按 ``--client claude`` 经客户端适配器取到本模块），对编排器的
引用经服务面——Claude 的原子入口、staging 写入与队列清单编译就是 Claude 编排器的服务面成员（实现在共享控制面
``upgrade_control_plane``，以 Claude 适配器转调）。本模块只给出 Claude 与 Codex 不同的部分。

二、与 Codex 的差别
===================
* 合同名：收据 ``official-client-atomic-vc0-vc1-rehearsal/v1``（Codex v2 的字段加 ``client``）、动作事实
  ``official-client-atomic-action-result/v1``、合成 VC-0 阶段事实 ``official-client-atomic-vc0-stage/v1``；
* 合成 Campaign 声明 ``client: claude``：监督器按它把 Claude 新合同清单判给 Claude、项目总账查找按它找 Claude 总账；版本取
  Claude 的基线与目标；计时账本是带 client 的 v2 计划，测试用项目总账是 ``upgrade-project-ledger-claude``（共享流程按适配器
  的 ``client`` 与 ``project_ledger_dir_name`` 建与核对）；
* 父 run 记录的队列清单是 ``official-client-campaign-run/v1``（Claude 编排器 ``_vc_run_manifest_from_batch`` 的产物）；
* 动作命令追加 ``--client claude``：worker 据此写 Claude 合同名的动作事实；
* 失败父 run（证明计时账本收口）：Codex 用 v1 相对预算清单，而 Claude Campaign 只接受 Claude 新合同（监督器拒绝给
  Claude Campaign 派发 Codex 清单），所以改用独立 state-dir 里序号 1 的 Claude 批次清单：前序是合成 VC-0 checkpoint，
  原始 deadline 与总计划摘要取合成总计划，候选级绑定两字段为 null（staging 模型清单必带），动作与 Codex 相同（零网络、
  退出码 7）；deadline 漂移负例的清单同样带 ``client`` 与候选级绑定；
* 工具身份另绑共享控制面实现 ``upgrade_control_plane.py``、Claude VC 制品 ``claude_upgrade_vc.py`` 与本模块（原子入口在
  Claude 一侧的真实实现，任一变化都要重做双跑）。

本模块属 Claude 身份策略的 control 层（``claude_upgrade_`` 前缀），被 Codex 身份策略忽略（``claude_`` 前缀）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_campaign_run_rehearsal_receipt as rehearsal
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts

CLIENT = "claude"
CLI_LABEL = "Claude"
ATOMIC_RECEIPT_SCHEMA = "official-client-atomic-vc0-vc1-rehearsal/v1"
ACTION_RESULT_SCHEMA = "official-client-atomic-action-result/v1"
ATOMIC_VC0_STAGE_SCHEMA = "official-client-atomic-vc0-stage/v1"
# 合成 Campaign 的版本：只是合成总计划、计时账本与项目总账里的标签（Claude 的基线与当前目标），不读任何官方制品。
ATOMIC_BASELINE_VERSION = "2.1.226"
ATOMIC_TARGET_VERSION = "2.1.285"
ATOMIC_RUN_SCHEMA = codex_upgrade_supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA
ATOMIC_CLIENT_FIELDS: Mapping[str, Any] = MappingProxyType({"client": CLIENT})
ATOMIC_CLIENT_ARGV: tuple[str, ...] = ("--client", CLIENT)
# Claude 的队列清单（含负例的 deadline 漂移清单）：新合同带 client，staging 模型清单带候选级绑定（Campaign 级阶段为 null）。
ATOMIC_BATCHED_MANIFEST_EXTRA: Mapping[str, Any] = MappingProxyType(
    {"client": CLIENT, "candidate_revision": None, "candidate_id": None}
)
# 失败父 run 的动作：与 Codex 相同的零网络失败（退出码 7，解释器在调用时取当前进程的），单动作超时 5 秒。
FAILING_ACTION_TIMEOUT_SECONDS = 5
FAILING_ACTION_SCRIPT = "raise SystemExit(7)"
VC0_CHECKPOINT = Path("control") / "vc" / "vc-0-checkpoint.json"
CAMPAIGN_PLAN = Path("control") / "vc" / "campaign-plan.json"


def atomic_extra_tool_files() -> tuple[Path, ...]:
    """原子入口在 Claude 一侧的真实实现：共享控制面、Claude VC 制品与本画像。"""

    from tools.official_client_capture import claude_upgrade_vc
    from tools.official_client_capture import upgrade_control_plane

    return (Path(upgrade_control_plane.__file__), Path(claude_upgrade_vc.__file__), Path(__file__))


def atomic_failing_parent_manifest(campaign_dir: Path, campaign_id: str, action_id: str) -> dict[str, Any]:
    """失败父 run 的 Claude 新合同清单：独立 state-dir 里序号 1 的单动作批次，从合成总计划与 VC-0 checkpoint 确定性导出。

    采集与重放用同一函数：重放时按 Campaign 里冻结的总计划与 checkpoint 重建，与父 run 实际执行的清单逐字比较。
    """

    campaign_dir = Path(campaign_dir)
    try:
        plan = codex_upgrade_vc_artifacts.validate_campaign_plan(rehearsal._load_json(campaign_dir / CAMPAIGN_PLAN, "原子演练总计划"))
        checkpoint_path = campaign_dir / VC0_CHECKPOINT
        checkpoint = codex_upgrade_vc_artifacts.validate_vc_checkpoint(
            rehearsal._load_json(checkpoint_path, "原子演练 VC-0 checkpoint"), plan
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise rehearsal.CampaignRunRehearsalError(f"原子演练总计划或 VC-0 checkpoint 非法：{error}") from error
    if plan["campaign_id"] != campaign_id:
        raise rehearsal.CampaignRunRehearsalError("失败父 run 的 Campaign 与合成总计划不一致")
    action = {
        "action_id": action_id,
        "operation": f"VC-1:{action_id}",
        "timeout_seconds": FAILING_ACTION_TIMEOUT_SECONDS,
        "command": [sys.executable, "-c", FAILING_ACTION_SCRIPT],
        "item_ids": [action_id],
    }
    try:
        return codex_upgrade_supervisor.build_batched_campaign_run_manifest(
            campaign_id=campaign_id,
            campaign_plan_sha256=str(plan["plan_sha256"]),
            batch_id=action_id,
            batch_sequence=1,
            batch_sha256=codex_upgrade_vc_artifacts.digest(action),
            phase="VC-1",
            predecessor_checkpoint={
                "path": VC0_CHECKPOINT.as_posix(),
                "sha256": rehearsal._sha256_file(checkpoint_path),
                "phase": "VC-0",
                "checkpoint_sha256": str(checkpoint["checkpoint_sha256"]),
            },
            original_deadline_at_utc=str(plan["original_deadline_at_utc"]),
            actions=[action],
            execute_items=[action_id],
            reuse_items=[],
            **ATOMIC_BATCHED_MANIFEST_EXTRA,
        )
    except codex_upgrade_supervisor.SupervisorError as error:
        raise rehearsal.CampaignRunRehearsalError(f"失败父 run 清单无法按 Claude 合同生成：{error}") from error
