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

from tools.official_client_capture import codex_upgrade_root_cause
from tools.official_client_capture.upgrade_client_adapter import OrchestratorFacade

ORCHESTRATOR = OrchestratorFacade("claude")

# Claude 编排器的 CLI 名（B1-09）；监督器据此识别预声明动作是否调用编排器。
ORCHESTRATOR_BASENAMES = frozenset({"claude_upgrade.py", "claude-upgrade"})
# 预声明动作里拒绝的控制面与引导入口：Codex 的 15 项（Claude 没有的历史子命令一并拒绝，无害）加 Claude 的
# 官方制品冻结（B2-01，VC-0 之前的离线工具）与前序证据导入（B2-09，对应 Codex 的 reuse-official-evidence）。
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
})
# Claude 评估器的断言结果 builder（B4-03）。
EVALUATION_BUILDER_BASENAME = "claude_build_rule_assertion_results.py"
# Claude 断言包取证清单里记录目标版本的字段名（B4 落实 Claude 取证清单合同时核对）。
TARGET_VERSION_FIELD = "claude_version"
# Claude 受管部署收据（B5a-01，合同 official-client-managed-deploy/v1）。
DEPLOY_RECEIPT_GLOB = "claude-managed-deploy-*.json"
# Claude 独立根因码表（B1-06）：同 schema，每个码的稳定维度都含 client。
ROOT_CAUSE_CODES_PATH = Path(__file__).resolve().parent / "claude_root_cause_codes.json"
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
