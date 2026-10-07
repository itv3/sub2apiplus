"""候选 r2 与评估器修复的私有场景入口；未登记认证，仅供隔离副本内的定向串行验收。"""

from tools.official_client_capture.tests import claude_revision_evaluator_chain_driver as driver


class _RevisionEvaluatorScenarios(driver.ClaudeRevisionEvaluatorChainDriver):
    """必须由隔离外壳启动；普通工作树执行在 setUp 的任何修改之前拒绝。"""

    def _scenario_r1_source_failure_r2_evaluator_repair(self) -> None:
        self.run_revision_evaluator_chain()
