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
    return (
        environment.get("before_probe") is not None
        and environment.get("after_probe") is not None
        and environment.get("drifted_kinds") == []
    )


def deploy_receipt_identity(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Claude 受管部署收据（``official-client-managed-deploy/v1``，B5a-01）的工具五摘要在 ``identity`` 子键里（B2-08）。

    收据声明的 ``client`` 不是 claude、或 ``identity`` 不是对象时返回空映射：比对一律不相等，对账器照常跳过这份收据。
    """

    identity = payload.get("identity") if payload.get("client") == "claude" else None
    return identity if isinstance(identity, Mapping) else {}
