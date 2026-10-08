"""Claude 客户端适配器：编排器门面与 Claude 专属的适配器成员（Claude 升级控制面设计 D4、B1-01）。

``ORCHESTRATOR`` 按 ``upgrade_client_adapter.ADAPTER_BINDINGS["claude"]`` 的服务面绑定，现取 ``claude_upgrade`` 及其
子模块里的同名同签名实现（B1-09 落实），只暴露服务面内的名字。本文件被 Codex 身份策略忽略（``claude_`` 前缀），
不影响 Codex 的任何身份摘要。

各成员标注了落实它的工作项；指向尚未建立的模块（``claude_upgrade``、``claude_tool_identity_policy``、
``claude_upgrade_live_request_provenance``）的绑定在对应工作项完成前访问会失败，而不是静默退回 Codex。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    import codex_upgrade_root_cause
    from upgrade_client_adapter import OrchestratorFacade
else:
    from . import codex_upgrade_root_cause
    from .upgrade_client_adapter import OrchestratorFacade

ORCHESTRATOR = OrchestratorFacade("claude")

# Claude 编排器的 CLI 名（B1-09）；监督器据此识别预声明动作是否调用编排器。
ORCHESTRATOR_BASENAMES = frozenset({"claude_upgrade.py", "claude-upgrade"})
# 预声明动作里拒绝的控制面与引导入口：Codex 的 15 项（Claude 没有的历史子命令一并拒绝，无害）加 Claude 的
# 官方制品冻结（B2-01，VC-0 之前的离线工具）、前序证据导入（B2-09，对应 Codex 的 reuse-official-evidence）与残留取证
# 进程清理（B2-08：在明确批准下杀进程，只能由操作员直接执行，不进动作队列）。
FORBIDDEN_SUBCOMMANDS = frozenset({
    "successor",
    "recover-candidate-failed-jobs",
    "control-epoch",
    "evaluation-transition",
    "terminal-transition-preflight",
    "plan",
    "reuse-official-evidence",
    "harden-evidence-permissions",
    "reconcile-supervisor-run",
    "reconcile-attempt",
    "compile-vc-batch",
    "compile-vc-interrupted-recovery-batch",
    "compile-vc-interrupted-recovery-continuation",
    "continue-vc1-interruption",
    "finalize-vc1-deadline-orphan",
    "freeze",
    "import-predecessor-evidence",
    "environment-cleanup",
})
# Claude 评估器的断言结果 builder（B4-03）。
EVALUATION_BUILDER_BASENAME = "claude_build_rule_assertion_results.py"
# Claude 断言包取证清单里记录目标版本的字段名（B4 落实 Claude 取证清单合同时核对）。
TARGET_VERSION_FIELD = "claude_version"
# Claude 受管部署收据（B5a-01，合同 official-client-managed-deploy/v1）。
DEPLOY_RECEIPT_GLOB = "claude-managed-deploy-*.json"
# Claude 独立根因码表（B1-06）：同 schema，每个码的稳定维度都含 client。
ROOT_CAUSE_CODES_PATH = Path(__file__).resolve().parent / "claude_root_cause_codes.json"
# Claude 项目总账目录（B1-04，设计 D3）：与 Codex 共用 ARM64 数据根，目录名不同，两本总账互不可见。
PROJECT_LEDGER_DIR_NAME = "upgrade-project-ledger-claude"
# Claude 候选就绪探针在项目总账里的请求计量类别（B1-04 先定名；B4 落实 Claude 候选就绪探针时核对）。
CANDIDATE_PROBE_ACCOUNTING_CATEGORY = "claude_candidate_readiness_probe/v1"
# VC-1 封存批次（``claude_upgrade_static.vc1_seal_actions``）的零请求后处理阶段项：静态分析、seal 预览、seal 批准。
# 监督器 post-run-tooling 判据与 seal 链续派协议经适配器认它们（修好接着跑：VC-1 续跑路径）；动作 ID 与静态分析模块的
# 常量逐字相同（测试核对），这里写字面量，免得适配器一导入就加载静态分析模块。
OFFICIAL_POST_RUN_TOOLING_ITEM_IDS = frozenset({"vc1-01-analyze-target", "vc1-02-seal-preview", "vc1-03-seal-approve"})
# VC-5 候选采集续跑（B4-05b-1）：候选续跑命令逐字携带的身份参数。Claude 的候选身份只来自 VC-4 构建收据，resume 重走候选
# 入口门、拒绝 Codex 用来重建候选身份的参数（claude_upgrade_recovery 第六节），所以只有 ``--candidate-id``。
CANDIDATE_RECOVERY_IDENTITY_FLAGS = ("--candidate-id",)
# Claude 合法新 revision 必须先完整重放作废、对账、总账与 COMMIT 证明，避免旧批次重派提前认领并拒绝新候选。
# 只把 candidate_revision 提前到 environment_redispatch 前，其余协议的相对顺序与共享监督器默认顺序一致。
SUCCESSOR_PROTOCOL_ORDER = (
    "evolution_recovery",
    "seal_chain",
    "stage_review",
    "parent_start",
    "parent_finalize",
    "evaluation_baseline",
    "candidate_revision",
    "environment_redispatch",
    "official_recovery_preview",
    "official_recovery_preview_retry",
    "official_recovery_run_retry",
    "candidate_recovery_preview",
    "candidate_recovery_preview_retry",
    "candidate_recovery_run_retry",
    "attempt_recovery_segment",
)
# Claude compare 与 builder 输出绑定当前评估基线；即使只修 checker，后继基线也必须重新 compare。
COMPARISON_IS_BASELINE_BOUND = True
# Claude Campaign ID（如 ``claude-code-2_1_226-fw-g-production-replacement-v2-20260821``）不匹配根因模块的 Codex
# Campaign ID 模式，必须在调用根因 API 前由适配器拒绝；版本号分隔符兼容点与下划线。
CLAUDE_CAMPAIGN_ID_RE = re.compile(r"claude-code-\d+[._]\d+[._]\d+")


def reject_volatile_dimensions(dimensions: Mapping[str, str]) -> None:
    """维度值含 Claude Campaign ID 即拒绝；其余易变形态由根因模块的六类模式覆盖。"""

    for key, value in dimensions.items():
        if isinstance(value, str) and CLAUDE_CAMPAIGN_ID_RE.search(value):
            raise codex_upgrade_root_cause.RootCauseError(
                f"维度 {key}含 Claude Campaign ID，不是稳定根因输入：{value!r}"
            )


def requires_project_ledger(campaign_mode: Any, target_version: Any) -> bool:
    """Claude 的 formal Campaign 一律在项目总账内创建与执行（Codex 的版本门槛对 Claude 版本号无意义）。"""

    del target_version
    return campaign_mode == "formal"


def attempt_environment_restored(attempt: Mapping[str, Any]) -> bool:
    """对账器判定 Claude attempt 环境已恢复（B2-08）：before／after 探针都在位，且前后探针没有任何漂移。

    Claude 的环境恢复在每步中继里完成（恢复 hosts、停止中继，中继清单记 ``cleanup``），attempt 级没有单独的恢复收据；
    恢复是否成立由 attempt 前后两次只读探针（两个采集容器）逐类比较给出：任一类漂移或残留取证进程都会让 run 判污染
    （``environment_contaminated``），对账器在调用本函数之前已按污染、恢复错误、冲突隔离与连续性漂移分流。
    """

    environment = attempt.get("environment") if isinstance(attempt.get("environment"), Mapping) else {}
    restored = (
        environment.get("before_probe") is not None
        and environment.get("after_probe") is not None
        and environment.get("drifted_kinds") == []
    )
    if attempt.get("phase") == "candidate":
        # 候选 attempt（B4-01 c1）另要求网关隔离恢复核验通过：每步恢复报告全部核对通过、网关前后快照逐字段相等。
        recovery = attempt.get("gateway_recovery") if isinstance(attempt.get("gateway_recovery"), Mapping) else {}
        restored = restored and recovery.get("status") == "passed"
    return restored


def deploy_receipt_identity(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Claude 受管部署收据（``official-client-managed-deploy/v1``，B5a-01）的工具五摘要在 ``identity`` 子键里（B2-08）。

    收据声明的 ``client`` 不是 claude、或 ``identity`` 不是对象时返回空映射：比对一律不相等，对账器照常跳过这份收据。
    """

    identity = payload.get("identity") if payload.get("client") == "claude" else None
    return identity if isinstance(identity, Mapping) else {}


def official_attempt_free_post_run_action(action: Any, campaign_dir: Any) -> bool:
    """VC-1 动作是否是不绑定 attempt 的零请求后处理动作：只有封存批次的静态分析（修好接着跑：VC-1 续跑路径）。

    静态分析（``vc1-01-analyze-target``）读 Campaign 冻结的官方二进制、只写 ``official/static-analysis/``，不指向任何官方
    attempt，监督器据此把它从「全部动作指向同一 attempt」的目标解析里排除。认定按形态精确闭合，与阶段幂等重派合同同一
    判定（``claude_upgrade_batch._vc1_seal_action_shape``）：action_id 与唯一 item_id 都是该动作 ID，命令是受管 Python
    对 ``claude_upgrade.py`` 的直接调用 ``analyze-target --campaign-dir <本 Campaign>``，没有别的参数——给别的命令套上
    这个项名换不来可恢复分类。任何结构非法或判定失败都返回假（监督器的判据只能因此不成立，不会因此抛错）。
    """

    try:
        if __package__ in {None, ""}:
            import claude_upgrade_batch
        else:
            from . import claude_upgrade_batch

        action_id = claude_upgrade_batch._static().ACTION_ID
        if not isinstance(action, Mapping) or action.get("action_id") != action_id or action.get("item_ids") != [action_id]:
            return False
        command = action.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(value, str) for value in command):
            return False
        kind, _flags = claude_upgrade_batch._vc1_seal_action_shape(claude_upgrade_batch._orchestrator_argv(command), Path(campaign_dir))
    except Exception:  # noqa: BLE001 —— 判定失败一律按「不是」处理，由监督器沿用原分类
        return False
    return kind == action_id


def candidate_post_run_extra_action(manifest: Any, action: Any) -> bool:
    """候选级共享闭集之外的零请求后处理动作：只有 VC-6 两个交付动作（B4-07 收尾）。

    ``deliver-candidate run``（``vc6-01-deliver-run``）与带批准的 ``deliver-candidate finalize``（``vc6-02-deliver-finalize``）
    不发模型请求、重跑同一条命令即续跑（Store 只写一次、账本按 Store 补账），与 Codex VC-6 的 canonical 生产步骤同类：失败后
    对账、修好接着逐字重派，不必作废已验收的候选。认定按形态精确闭合（``claude_upgrade_delivery.batch_action_kind``）：
    阶段是 VC-6，动作 ID 与唯一项名相同，命令是受管 Python 对 ``claude_upgrade.py`` 的直接调用、子命令与动作对应、旗标闭集
    精确、``--campaign-dir`` 指向本 Campaign——给别的命令套上这两个项名换不来可恢复分类。任何结构非法或判定失败都返回假。
    """

    try:
        if __package__ in {None, ""}:
            import claude_upgrade_delivery
        else:
            from . import claude_upgrade_delivery

        return claude_upgrade_delivery.batch_action_kind(action, manifest) is not None
    except Exception:  # noqa: BLE001 —— 判定失败一律按「不是」处理，由监督器沿用原分类
        return False
