"""冻结摘要类测试失败点的显式白名单：只有这里登记的失败点，快线才会把对应的 go test 失败记为“待台账闭合”。

背景：试验区还没生成台账，冻结测试本来就会红；快线要把这类失败和功能失败分开，但**绝不能**把真正的功能失败放过。
早先的规则（用例名带 Frozen／Transition／Drift 等字样，且失败信息带“摘要／漂移”，或测试源文件读 docs/egress）会把
`TestClaudeProductionRejectsSharedSelectorCoordinateDrift`（selector fail-close）、`TestCodexIdentityFactsRejectConditionDrift`
（身份条件）、`TestChangeset5CurrentFinalWireComparatorRejectsWireDrift`（真实 wire 比较）等功能测试误判成冻结类。
所以改成显式登记：**测试源文件 + 失败信息标记**，两者同时命中才算。原则是误判为冻结类必须不可能（wire、身份、selector、
catalog、executor 类测试一律 functional），冻结类被误判为功能失败可以接受（按功能阻断，人工看一眼就能放行）。

登记的依据（逐个读过源码）：条目对应的失败点（含标记的字符串字面量）前 30 行内，同时有“读仓库文件”“算 sha256”“`!=` 比较”，
即该处只是把台账登记的摘要与仓库文件的当前摘要比对（允许沿登记的后继边走到当前摘要）。这样的失败只会在“文件改了、台账还没
承接”时出现，生成台账 revision 后即闭合。每一节的说明里给出代表位置。`tests/test_fast_attribution.py` 的守卫用例会对
每个条目重新核对：文件存在、标记确在文件里、失败点前文仍满足上述三要素；源码变了、条目陈旧，用例就失败，提醒重新核对。

键是相对 ``backend`` 的测试源文件路径（两个包里有同名文件，必须带包目录）；值是该文件里被核对过的失败信息标记
（字符串字面量里第一个格式占位符之前的部分）。失败用例必须同时满足：失败信息首行 ``<文件>:<行>:`` 指出的文件在表里，
且失败信息含该文件登记的某个标记。没有被收录的情形一律按功能失败：

* “条目非法”“提交绑定不一致”：校验函数把条目结构和摘要比对并成一个条件（`err != nil || …`），无法确定是摘要还是结构问题；
* 比较的是收据自身摘要与 Go 常量、wire、selector、身份、catalog、executor 的测试；
* 失败信息读不出是哪种比对的（只有 ``invalid argument`` 之类的底层错误），除非下面“特例”里逐个查清。
"""

from __future__ import annotations

FROZEN_TEST_SOURCES: dict[str, tuple[str, ...]] = {
    # ===== 上游 source-transition／successor 通用台账（22 个文件）=====
    # 上游合并每一版登记的 source-transition、successor、框架 transition 台账。
    # 失败点的形态相同：读仓库里的文件、算 sha256，与台账登记的 ToSHA256／CurrentSHA256 比较，不相等且没有登记的后继边（…Supersedes／auditedSourceSuccessorReaches）能从登记摘要走到当前摘要时才报错。
    # 代表位置：upstream_v0179_source_transition_test.go 第 117～124 行、upstream_source_transition_generic_test.go 第 209～211 行、upstream_merge_egress_snapshot_transition_test.go 第 105～120 行。
    "internal/officialegress/composite_model_protocol_source_transition_test.go": (
        "Composite 模型协议 transition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_merge_egress_snapshot_transition_test.go": (
        "上游合并 egress snapshot transition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_merge_framework_transition_test.go": (
        "上游合并框架 transition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_merge_framework_v3_successor_test.go": (
        "上游合并框架 v3 successor 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_merge_framework_v4_successor_test.go": (
        "上游合并框架 v0.2.3 successor 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_merge_route_scope_transition_test.go": (
        "上游合并路由作用域 transition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_source_transition_generic_test.go": (
        "上游 source-transition 节点当前摘要不一致：",
    ),
    "internal/officialegress/upstream_v0177_source_transition_test.go": (
        "v0.1.177 上游源码摘要漂移：path=",
        "运行时可靠性修复源码摘要漂移：path=",
    ),
    "internal/officialegress/upstream_v0179_release_ci_repair_transition_test.go": (
        "上游 v0.1.179 发版 CI 修复 transition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_v0179_source_transition_test.go": (
        "上游 v0.1.179 source transition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_v0180_egress_prerequisite_transition_test.go": (
        "上游 v0.1.180 egress 前置 transition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_v0180_source_transition_test.go": (
        "上游 v0.1.180 source transition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_v023_scanner_successor_transition_test.go": (
        "上游 v0.2.3 scanner successor transition 当前摘要不一致：",
        "上游 v0.2.3 scanner successor addition 当前摘要不一致：",
    ),
    "internal/officialegress/upstream_v023_source_transition_test.go": (
        "上游 v0.2.3 源码 transition 当前摘要不一致：",
    ),
    "internal/service/composite_model_protocol_source_transition_test.go": (
        "Composite 模型协议 transition 当前摘要不一致：",
    ),
    "internal/service/upstream_merge_framework_v3_successor_test.go": (
        "上游合并框架 v3 successor 当前摘要不一致：",
    ),
    "internal/service/upstream_merge_framework_v4_successor_test.go": (
        "上游合并框架 v0.2.3 successor 当前摘要不一致：",
    ),
    "internal/service/upstream_source_transition_generic_test.go": (
        "上游 source-transition 节点当前摘要不一致：",
    ),
    "internal/service/upstream_v0179_release_ci_repair_transition_test.go": (
        "上游 v0.1.179 发版 CI 修复 transition 当前摘要不一致：",
    ),
    "internal/service/upstream_v0179_source_transition_test.go": (
        "上游 source transition 当前摘要不一致：",
    ),
    "internal/service/upstream_v023_scanner_successor_transition_test.go": (
        "上游 v0.2.3 scanner successor transition 当前摘要不一致：",
        "上游 v0.2.3 scanner successor addition 当前摘要不一致：",
    ),
    "internal/service/upstream_v023_source_transition_test.go": (
        "上游 v0.2.3 源码 transition 当前摘要不一致：",
    ),
    # ===== Codex 0.149.1 终态与 0.151 系列（31 个文件）=====
    # Codex 终态与 0.151 评估／工具／恢复系列的 transition 与 addition 台账。
    # 同样是读文件、算摘要、与登记摘要比较并允许登记的后继边；addition 条目比较的是新增文件的登记摘要。
    # 代表位置：codex_0151_c2pa_capture_tool_successor_test.go 第 120～128 行、codex_01491_terminal_state_test.go 第 175～207 行（present 状态）。
    "internal/officialegress/codex_01491_terminal_state_test.go": (
        "0.149.1 终态当前摘要不一致：",
        "OpenAI replay OOM 修复 transition 当前摘要不一致：",
        "OpenAI replay OOM 修复 addition 当前摘要不一致：",
        "OpenAI WS 兼容守卫修复 transition 当前摘要不一致：",
        "OpenAI WS 空终态输出修复 transition 当前摘要不一致：",
        "Codex CLI 0.151 工具就绪 transition 当前摘要不一致：",
        "Codex CLI 0.151 工具就绪 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_arm64_environment_producer_replay_tool_successor_test.go": (
        "Codex CLI 0.151 ARM64 环境 producer 重放工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 ARM64 环境 producer 重放工具后继 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_arm64_typescript_dependency_source_transition_test.go": (
        "Codex CLI 0.151 ARM64 TypeScript transition 当前摘要不一致：",
        "Codex CLI 0.151 ARM64 TypeScript addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_c2pa_capture_tool_successor_test.go": (
        "Codex CLI 0.151 C2PA 工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 C2PA 工具后继 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_container_path_recovery_tool_successor_test.go": (
        "Codex CLI 0.151 容器路径恢复工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 容器路径恢复工具后继 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_deterministic_package_fixture_source_transition_test.go": (
        "Codex CLI 0.151 确定性包夹具 transition 当前摘要不一致：",
        "Codex CLI 0.151 确定性包夹具 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_evaluation_recovery_source_transition_test.go": (
        "Codex CLI 0.151 评估恢复 transition 当前摘要不一致：",
        "Codex CLI 0.151 评估恢复 addition 当前摘要不一致：",
        "Codex CLI 0.151 框架澄清 transition 当前摘要不一致：",
        "Codex CLI 0.151 受管场景恢复 transition 当前摘要不一致：",
        "Codex CLI 0.151 manifest 排序恢复 transition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_evidence_manifest_replay_closure_source_transition_test.go": (
        "Codex CLI 0.151 manifest 重放闭合 transition 当前摘要不一致：",
        "Codex CLI 0.151 manifest 重放闭合 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_formal_recovery_source_transition_test.go": (
        "Codex CLI 0.151 Formal 恢复 transition 当前摘要不一致：",
        "Codex CLI 0.151 Formal 恢复 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_historical_rehearsal_successor_source_transition_test.go": (
        "Codex CLI 0.151 历史 rehearsal successor transition 当前摘要不一致：",
        "Codex CLI 0.151 历史 rehearsal successor addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_model_policy_tool_successor_test.go": (
        "Codex CLI 0.151 模型政策工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 模型政策工具后继 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_r9_candidate_readiness_source_transition_test.go": (
        "Codex CLI 0.151 r9 候选就绪 transition 当前摘要不一致：",
        "Codex CLI 0.151 r9 候选就绪 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_stopped_ledger_recovery_source_transition_test.go": (
        "Codex CLI 0.151 停线 Ledger 恢复 transition 当前摘要不一致：",
        "Codex CLI 0.151 停线 Ledger 恢复 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_system_proxy_capture_tool_successor_test.go": (
        "Codex CLI 0.151 系统代理取证工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 系统代理取证工具后继 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_test_trace_preflight_tool_successor_test.go": (
        "Codex CLI 0.151 test trace P0 后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 test trace P0 后继 addition 当前摘要不一致：",
    ),
    "internal/officialegress/codex_0151_timing_producer_replay_tool_successor_test.go": (
        "Codex CLI 0.151 计时生产者重放工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 计时生产者重放工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 受管执行根恢复工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 受管执行根恢复工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 bubblewrap 与 zstd 就绪工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 bubblewrap 与 zstd 就绪工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 C2PA Job 身份工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 C2PA Job 身份工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 完整 Job 就绪工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 完整 Job 就绪工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 证据标签前置工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 证据标签前置工具后继 addition 当前摘要不一致：",
    ),
    "internal/service/codex_01491_terminal_state_test.go": (
        "0.149.1 终态当前摘要不一致：",
        "OpenAI replay OOM 修复 transition 当前摘要不一致：",
        "OpenAI replay OOM 修复 addition 当前摘要不一致：",
        "OpenAI WS 兼容守卫修复 transition 当前摘要不一致：",
        "OpenAI WS 空终态输出修复 transition 当前摘要不一致：",
        "Codex CLI 0.151 工具就绪 transition 当前摘要不一致：",
        "Codex CLI 0.151 工具就绪 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_arm64_environment_producer_replay_tool_successor_test.go": (
        "Codex CLI 0.151 ARM64 环境 producer 重放工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 ARM64 环境 producer 重放工具后继 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_arm64_typescript_dependency_source_transition_test.go": (
        "Codex CLI 0.151 ARM64 TypeScript transition 当前摘要不一致：",
        "Codex CLI 0.151 ARM64 TypeScript addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_c2pa_capture_tool_successor_test.go": (
        "Codex CLI 0.151 C2PA 工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 C2PA 工具后继 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_container_path_recovery_tool_successor_test.go": (
        "Codex CLI 0.151 容器路径恢复工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 容器路径恢复工具后继 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_deterministic_package_fixture_source_transition_test.go": (
        "Codex CLI 0.151 确定性包夹具 transition 当前摘要不一致：",
        "Codex CLI 0.151 确定性包夹具 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_evaluation_recovery_source_transition_test.go": (
        "Codex CLI 0.151 评估恢复 transition 当前摘要不一致：",
        "Codex CLI 0.151 评估恢复 addition 当前摘要不一致：",
        "Codex CLI 0.151 框架澄清 transition 当前摘要不一致：",
        "Codex CLI 0.151 受管场景恢复 transition 当前摘要不一致：",
        "Codex CLI 0.151 manifest 排序恢复 transition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_evidence_manifest_replay_closure_source_transition_test.go": (
        "Codex CLI 0.151 manifest 重放闭合 transition 当前摘要不一致：",
        "Codex CLI 0.151 manifest 重放闭合 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_formal_recovery_source_transition_test.go": (
        "Codex CLI 0.151 Formal 恢复 transition 当前摘要不一致：",
        "Codex CLI 0.151 Formal 恢复 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_historical_rehearsal_successor_source_transition_test.go": (
        "Codex CLI 0.151 历史 rehearsal successor transition 当前摘要不一致：",
        "Codex CLI 0.151 历史 rehearsal successor addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_model_policy_tool_successor_test.go": (
        "Codex CLI 0.151 模型政策工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 模型政策工具后继 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_r9_candidate_readiness_source_transition_test.go": (
        "Codex CLI 0.151 r9 候选就绪 transition 当前摘要不一致：",
        "Codex CLI 0.151 r9 候选就绪 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_stopped_ledger_recovery_source_transition_test.go": (
        "Codex CLI 0.151 停线 Ledger 恢复 transition 当前摘要不一致：",
        "Codex CLI 0.151 停线 Ledger 恢复 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_system_proxy_capture_tool_successor_test.go": (
        "Codex CLI 0.151 系统代理取证工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 系统代理取证工具后继 addition 当前摘要不一致：",
    ),
    "internal/service/codex_0151_timing_producer_replay_tool_successor_test.go": (
        "Codex CLI 0.151 计时生产者重放工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 计时生产者重放工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 受管执行根恢复工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 受管执行根恢复工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 bubblewrap 与 zstd 就绪工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 bubblewrap 与 zstd 就绪工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 C2PA Job 身份工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 C2PA Job 身份工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 完整 Job 就绪工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 完整 Job 就绪工具后继 addition 当前摘要不一致：",
        "Codex CLI 0.151 证据标签前置工具后继 transition 当前摘要不一致：",
        "Codex CLI 0.151 证据标签前置工具后继 addition 当前摘要不一致：",
    ),
    # ===== Claude FW-G／FW-H 与 FW-E 系列（24 个文件）=====
    # Claude 与 FW-E 各批次源码 transition。
    # 失败点在测试函数体或校验函数里：读文件、算摘要，与 transition.ToSHA256 比较并允许后继边；信息里同时带 got 与 want 两个摘要的，want 即台账登记摘要。
    # 代表位置：claude_fw_g_count_tokens_transition_test.go 第 181～199 行、claude_fw_h_source_transition_test.go 第 221～238 行、fw_e_observation_source_transition_test.go 第 135～148 行。
    "internal/officialegress/claude_fw_g_alias_route_transition_test.go": (
        "Claude FW-G 裸路径别名 transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_count_tokens_transition_test.go": (
        "Claude FW-G count_tokens transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_desktop_compat_transition_test.go": (
        "Claude FW-G Desktop compatibility transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_desktop_ingress_transition_test.go": (
        "Claude FW-G Desktop 入站 transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_desktop_title_transition_test.go": (
        "Claude FW-G Desktop title transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_fable_desktop_title_transition_test.go": (
        "Claude FW-G Fable Desktop 标题 transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_ingress_authority_transition_test.go": (
        "Claude FW-G ingress authority transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_model_capability_transition_test.go": (
        "Claude 模型能力源码 transition 当前摘要不一致",
    ),
    "internal/officialegress/claude_fw_g_query_fail_close_transition_test.go": (
        "Claude FW-G query fail-close transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_source_transition_test.go": (
        "Claude FW-G source transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_support_envelope_transition_test.go": (
        "Claude FW-G SupportEnvelope transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_test_transition_test.go": (
        "Claude FW-G test transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_thinking_display_transition_test.go": (
        "Claude FW-G thinking.display transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_g_three_model_acceptance_attempt_test.go": (
        "Claude 三模型验收尝试文档 transition 当前摘要不一致",
    ),
    "internal/officialegress/claude_fw_g_three_model_acceptance_test.go": (
        "Claude 三模型最终验收 transition 当前摘要不一致",
    ),
    "internal/officialegress/claude_fw_h_fable_declared_fallback_transition_test.go": (
        "Claude FW-H Fable fallback 摘要漂移：",
        "Claude FW-H 最终文档摘要漂移：",
    ),
    "internal/officialegress/claude_fw_h_legacy_retirement_transition_test.go": (
        "Claude FW-H 遗留退休摘要漂移：",
    ),
    "internal/officialegress/claude_fw_h_response_request_id_transition_test.go": (
        "Claude request-id transition 当前摘要不一致",
    ),
    "internal/officialegress/claude_fw_h_source_transition_test.go": (
        "Claude FW-H source transition 漂移：path=",
    ),
    "internal/officialegress/claude_fw_h_state_durability_transition_test.go": (
        "Claude 状态耐久性 transition 当前摘要不一致：",
    ),
    "internal/officialegress/claude_fw_h_third_party_strict_transition_test.go": (
        "Claude FW-H 第三方 strict transition 当前摘要不一致",
    ),
    "internal/officialegress/fw_e_observation_source_transition_test.go": (
        "FW-E observation source transition 漂移：path=",
    ),
    "internal/service/claude_fw_g_source_transition_test.go": (
        "Claude FW-G source transition 漂移：path=",
    ),
    "internal/service/claude_fw_g_test_transition_test.go": (
        "Claude FW-G test transition 漂移：path=",
    ),
    # ===== 退休、闭集与运行稳定性台账（8 个文件）=====
    # 退休收据、兼容代码闭集、版本泄漏债务闭集、运行稳定性等台账里的“源码摘要”条目。
    # 失败点同样是读文件、算摘要、与登记摘要比较并允许后继边；只收源码摘要这一类信息，收据自身摘要与常量比较的信息（形如“收据漂移：got= want=”）不收。
    # 代表位置：version_leak_debt_closure_test.go 第 94～104 行、compatibility_code_retirement_closure_test.go 第 200～223 行、codex_models_unwired_transport_retirement_test.go 第 116～135 行。
    "internal/officialegress/catalog_generic_bind_retirement_test.go": (
        "Catalog 通用 binding 源码摘要漂移：path=",
    ),
    "internal/officialegress/executor_one_shot_retirement_test.go": (
        "Executor 一次性兼容源码摘要漂移：path=",
    ),
    "internal/officialegress/websocket_tokenless_retirement_test.go": (
        "WS 无 token 兼容源码摘要漂移：path=",
    ),
    "internal/service/codex_models_unwired_transport_retirement_test.go": (
        "Codex Models unwired transport 源码摘要没有形成追加式 transition：path=",
    ),
    "internal/service/compatibility_code_retirement_closure_test.go": (
        "兼容代码闭集文件摘要漂移：path=",
    ),
    "internal/service/sub2api_runtime_stability_source_transition_test.go": (
        "Sub2API 运行稳定性源码摘要漂移：path=",
    ),
    "internal/service/version_leak_debt_closure_test.go": (
        "版本泄漏源码摘要漂移：path=",
    ),
    "internal/service/websocket_finalizer_compat_retirement_test.go": (
        "WebSocket finalizer 源码摘要漂移：path=",
    ),
}

# 组合条件特例：失败信息同时出自“摘要比对”和“台账条目静态字段核对”的 ``||`` 条件，信息本身分不出是哪一项。
#
# 这些校验函数把两类检查并成一个条件，只返回一句话（“条目非法”“提交绑定不一致”）：
# * 台账条目的静态字段核对——路径非空、摘要是 64 位十六进制、前后摘要不同、reason 非空、提交号等于另一份台账里的提交号；
# * 对仓库文件当前摘要的比对（``…TargetMatches``：读文件、算 sha256、与登记摘要比较并允许后继边）。
# 静态字段属于不可变的冻结台账本身，产品代码的任何功能缺陷都不会让它们变化（除非有人改动受保护的 docs/egress 台账并重新签名，
# 那是证据链被动过，不是功能失败，U-4 前定向冻结检查和正式 U-4 同样会拦住）；会随合并变化的只有仓库文件的当前摘要。
# 所以这几处失败只可能来自文件摘要比对，登记为冻结摘要类。每项给出 (失败信息标记, 做摘要比对的函数)；
# COMPOUND_HELPER_EVIDENCE 写明该函数（或它调用的校验函数）所在的源文件里必须出现的片段，守卫用例逐项核对。
FROZEN_COMPOUND_SOURCES: dict[str, tuple[tuple[str, str], ...]] = {
    "internal/officialegress/claude_official_client_only_transition_test.go": (
        ("Claude official-client-only transition 条目非法", "claudeOfficialClientOnlyTargetMatches"),
        ("Claude official-client-only addition 条目非法", "claudeOfficialClientOnlyTargetMatches"),
    ),
    "internal/officialegress/claude_offline_ci_stabilization_transition_test.go": (
        ("Claude 离线 CI 稳定化 transition 条目非法", "claudeOfflineCIStabilizationTargetMatches"),
    ),
    "internal/officialegress/claude_offline_release_repair_transition_test.go": (
        ("Claude 离线发版修复 transition 条目非法", "claudeOfflineReleaseRepairTargetMatches"),
    ),
    # v4 successor：``err != nil || 提交号不等`` 里的 err 来自 readUpstreamV023SourceTransition，
    # 它调用 validateUpstreamV023SourceTransition，其中逐条核对仓库文件的当前摘要（auditedSourceCurrentStateAccepted）；
    # 同一轮试验区里 v0.2.3 台账自己的测试同时以“当前摘要不一致”失败，互相印证。
    "internal/officialegress/upstream_merge_framework_v4_successor_test.go": (
        ("上游合并框架 v0.2.3 successor 通用 transition 提交绑定不一致", "readUpstreamV023SourceTransition"),
    ),
    "internal/service/upstream_merge_framework_v4_successor_test.go": (
        ("上游合并框架 v0.2.3 successor 通用 transition 提交绑定不一致", "readUpstreamV023SourceTransitionService"),
    ),
}
COMPOUND_HELPER_EVIDENCE: dict[str, tuple[str, ...]] = {
    "claudeOfficialClientOnlyTargetMatches": ("os.ReadFile(", "sha256.Sum256(", "codex0151CurrentSourceDigestAccepted("),
    "claudeOfflineCIStabilizationTargetMatches": ("os.ReadFile(", "sha256.Sum256(", "upstreamMergeFrameworkTransitionSupersedes("),
    "claudeOfflineReleaseRepairTargetMatches": ("os.ReadFile(", "sha256.Sum256(", "codex0151CurrentSourceDigestAccepted("),
    "readUpstreamV023SourceTransition": ("validateUpstreamV023SourceTransition(", "auditedSourceCurrentStateAccepted(", "os.ReadFile("),
    "readUpstreamV023SourceTransitionService": (
        "validateUpstreamV023SourceTransitionService(",
        "auditedSourceCurrentStateAcceptedService(",
        "os.ReadFile(",
    ),
}

# 特例：失败信息只有底层错误 ``invalid argument``，已查清只可能来自摘要比对的失败点。
#
# internal/service/upstream_v0180_source_transition_test.go：loadUpstreamV0180SourceTransitionServiceReceipt 返回
# ``os.ErrInvalid`` 的只有两处——第 62 行的回执字段常量核对，与第 78 行对回执里每个路径的当前摘要核对
# （``auditedSourceCurrentStateAcceptedService`` 与 ``codex01491TerminalStateSupersedesService`` 都不接受当前摘要时返回）。
# 第 62 行只有在冻结回执本身被改动并重新签名后才可达：回执的自摘要由其前一步 ``validateUpstreamSourceTransitionIdentity`` 先校验，
# 内容被改会先报“自摘要不一致”。所以实际可达的是第 78 行，即摘要比对。守卫用例断言该文件里恰有这两处 ``os.ErrInvalid``，
# 数量变化即说明来源变了，需要重新核对。
FROZEN_SPECIAL_SOURCES: dict[str, tuple[str, ...]] = {
    "internal/service/upstream_v0180_source_transition_test.go": ("invalid argument",),
}


def registered_markers(source: str) -> tuple[str, ...]:
    """某个测试源文件（形如 ``internal/service/xxx_test.go``）登记的失败信息标记；未登记返回空元组。"""

    compound = tuple(marker for marker, _helper in FROZEN_COMPOUND_SOURCES.get(source, ()))
    return FROZEN_TEST_SOURCES.get(source, ()) + compound + FROZEN_SPECIAL_SOURCES.get(source, ())
