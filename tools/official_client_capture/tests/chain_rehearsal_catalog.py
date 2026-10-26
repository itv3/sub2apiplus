"""第 13 项：VC-0～VC-6 整链空跑的登记目录（测试夹具，不进受管身份）。

引擎在 ``chain_rehearsal``；这里只登记“跑什么”：

1. **历史缺陷探针**（``DEFECT_PROBES``）：Codex 0.163 升级里真实撞到的工具缺陷，每个一个独立探针。探针运行的是修复时
   一并补上的回归用例（独立夹具，彼此不依赖前序步骤成功）；``injections`` 里的反向补丁能把缺陷原样放回受管树副本，
   用 ``replay-history`` 证明“缺陷回来时，探针能在同一轮里各自把它报出来”。
2. **恢复分支故障注入探针**（``RECOVERY_PROBES``）：VC-1、VC-5 恢复链的真实链（录制证据回放、崩溃注入、预览失败注入、
   守护者连败……），ARM64 root 环境专用；缺条件时如实“未执行”。
3. **完整链**（``CHAIN_STEPS``）：VC-1 取证 → VC-2～VC-5 连续链 → VC-5 评估 → 构建修订 → 段复用 → VC-6 canonical，
   有序，遇首个失败即停（沿用 ``rehearse-candidate-seal`` 的语义），汇总之后另跑一遍确认整体能通。

新增缺陷时：把修复提交里非测试文件的 diff 存成 ``fixtures/chain_rehearsal/<id>.patch``，在 ``DEFECT_PROBES`` 加一个探针，
用 ``test_chain_rehearsal_catalog`` 的目录自检确认补丁能反向应用、用例名都存在。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Mapping, Sequence

from tools.official_client_capture.tests import chain_rehearsal as engine
from tools.official_client_capture.tests import chain_rehearsal_inject as inject_module
from tools.official_client_capture.tests.chain_rehearsal import (
    Checkpoint, CommandRunner, Probe, Registry, UnittestRunner,
)
from tools.official_client_capture.tests.chain_rehearsal_inject import ExactReplacement, ReversePatch

TESTS = "tools.official_client_capture.tests"
REAL_CHAINS = f"{TESTS}.real_chains"
T = "tools/official_client_capture"

# 历史重放时复制的仓库子树：受管工具树（含 tests）、驱动目录、tools/ci（部分测试按路径加载）。其余仓库内容不复制。
COPIED_ROOTS = ("tools/official_client_capture", "tools/arm64_capture_driver", "tools/ci")
COPIED_DOCS = ("CODEX_CLI_CLIENT_EMULATION_GUIDE.md", "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md")


def copy_repo_subset(destination: Path, extras: Sequence[str] = ()) -> Path:
    """把受管树、驱动目录与 tools/ci 复制到 ``destination``（仓库根副本），返回副本根。``extras`` 是额外带上的仓库相对路径。

    不用 ``managed_tree_copy.copy_managed_tree``：它只复制受管树，而驱动测试（VC-2 离线判据门、阶段解析）要按仓库相对
    路径读驱动目录。字节码、版本快照目录不复制（工作树里不能出现 __pycache__）。
    """

    source_root = engine.REPO_ROOT
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "versions")
    for relative in COPIED_ROOTS:
        source = source_root / relative
        if source.is_dir():
            shutil.copytree(source, destination / relative, ignore=ignore)
    docs = destination / "docs"
    docs.mkdir(exist_ok=True)
    for name in COPIED_DOCS:
        origin = source_root / "docs" / name
        if origin.is_file():
            shutil.copy2(origin, docs / name)
    for relative in extras:
        origin = source_root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, target, ignore=ignore)
        elif origin.is_file():
            shutil.copy2(origin, target)
        else:
            raise FileNotFoundError(f"额外复制的路径不存在：{relative}")
    return destination


# ---------------------------------------------------------------------------
# 一、0.163 历史缺陷探针（一轮查全，各自独立）
# ---------------------------------------------------------------------------

DEFECT_PROBES: tuple[Probe, ...] = (
    Probe(
        "compaction-first-model-lite-flag",
        "压缩场景首模型的 Lite 标志没按主线声明核对（目录构造器写死历史语义）",
        "VC-1", "官方取证：压缩场景模型目录（build_compaction_model_catalog）",
        UnittestRunner([f"{TESTS}.test_compaction_scenario_models.CompactionScenarioModelTest"], timeout_seconds=300),
        history=("709b71f18", "记忆 codex-0163-gpt55-offline-main-track-1020", "VC-1 首批：gpt-5.5 下线、主线改 Lite"),
        signature=("unexpected keyword argument 'first_use_responses_lite'",),
        injections=(ReversePatch("compaction-first-model-lite-flag.patch", "709b71f18"),),
    ),
    Probe(
        "official-directed-response-model",
        "官方受控应答的 response.model 不跟随请求模型（写死主线），受控应答带 openai-model 信号",
        "VC-1", "官方取证：受控应答中继（upstream_byte_relay）",
        UnittestRunner([
            f"{TESTS}.test_official_directed_relay_flows.SyntheticModelFollowsRequestFlowTest",
            f"{TESTS}.test_official_directed_samples.OfficialSyntheticEventsTest.test_response_model_follows_the_request_model_in_every_constructor",
            f"{TESTS}.test_official_directed_samples.OfficialSyntheticEventsTest.test_request_model_accepts_only_a_plain_slug",
            f"{TESTS}.test_official_directed_samples.OfficialSyntheticEventsTest.test_synthetic_answers_carry_no_openai_model_signal",
            f"{TESTS}.test_official_directed_samples.StreamFaultWebSocketTest.test_every_answer_follows_the_model_of_its_own_response_create",
        ], timeout_seconds=300),
        history=("6c02609c2", "记忆 codex-0163-gpt55-offline-main-track-1020", "VC-1 首批：主线改 Lite"),
        signature=("cannot import name '_request_model'",),
        injections=(ReversePatch("official-directed-response-model.patch", "6c02609c2"),),
    ),
    Probe(
        "main-track-lite-compact-limit",
        "主线改 Lite 后，靠阈值触发自动压缩的主线作业还在用非 Lite 的压缩阈值",
        "VC-1", "官方取证：场景清单主线作业的 COMPACT_TOKEN_LIMIT",
        UnittestRunner([f"{TESTS}.test_main_track_models.MainTrackModelTests.test_lite_main_compaction_jobs_use_the_lite_threshold"],
                       timeout_seconds=300),
        history=("5f8f4431e", "记忆 codex-0163-gpt55-offline-main-track-1020", "VC-1 首批：主线改 Lite"),
        signature=("4000 not less than or equal to 2000",),
        injections=(ReversePatch("main-track-lite-compact-limit.patch", "5f8f4431e"),),
    ),
    Probe(
        "official-core-models-expectation-lite",
        "official-core 内嵌的 /models 补采期望没跟主线改 Lite，清单校验也不核对命令里内嵌的期望",
        "VC-1", "官方取证：official-core 作业命令内嵌期望与清单校验",
        UnittestRunner([f"{TESTS}.test_main_track_models.MainTrackModelTests.test_embedded_lite_expectations_in_commands_follow_the_declaration"],
                       timeout_seconds=300),
        history=("c60b6d135", "记忆 codex-0163-gpt55-offline-main-track-1020", "VC-1 首批：主线改 Lite"),
        signature=("'false' != 'true'",),
        injections=(ReversePatch("official-core-models-expectation-lite.patch", "c60b6d135"),),
    ),
    Probe(
        "capture-host-container-id",
        "候选采集脚本把监督器钉住的 64 位容器 ID 当主机名",
        "VC-5", "候选采集 candidate-core-mitm 首检（getent hosts 恒返回 2）",
        UnittestRunner([f"{TESTS}.test_account_gate_restoration.CaptureHostResolutionTest"], timeout_seconds=180),
        history=("3709a4964", "记忆 capture-container-id-pin-hostname-defect-1022", "VC-5 批次 22、27"),
        signature=('getent hosts "$capture_container"', 'getent ahostsv4 "$capture_container"',
                   'source.index("resolve_capture_host() {")'),
        injections=(ReversePatch("capture-host-container-id.patch", "3709a4964"),),
    ),
    Probe(
        "core-capture-lite-final-assertions",
        "候选核心采集脚本收尾断言没跟着主线改 Lite（A04 写死非 Lite、A10 不解压就数）",
        "VC-5", "候选采集 candidate-frozen-core 收尾断言",
        UnittestRunner([f"{TESTS}.test_candidate_core_capture.CoreCaptureLiteMainTrackChecksTest"], timeout_seconds=180),
        history=("144b60340", "记忆 codex-0163-gpt55-offline-main-track-1020", "VC-5 批次 29"),
        signature=('source.index("def _decoded_request_body(data):")',),
        injections=(ReversePatch("core-capture-lite-final-assertions.patch", "144b60340"),),
    ),
    Probe(
        "kilo-http-usage-order",
        "Kilo 收据把 HTTP 用量早于响应完成 0.15 毫秒判成时间乱序",
        "VC-5", "seal 链：Kilo 收据生成（codex_upgrade_receipt_finalizer）",
        # 原回归用例把终结器的拒绝原因吞进 StringIO，缺陷回来时只剩 “2 != 0”；这里跑同一个用例，但把被吞掉的标准错误
        # 还原出来（chain_rehearsal_phenomena），签名才能是工具真实的拒绝原因。
        CommandRunner([sys.executable, "-B", "-m", f"{TESTS}.chain_rehearsal_phenomena", "kilo-http-usage-order"],
                      timeout_seconds=240),
        history=("044c71143", "记忆 kilo-receipt-http-usage-order-defect-1022", "VC-5 seal 链"),
        signature=("Kilo 五份事实的时间顺序不成立",),
        injections=(ReversePatch("kilo-http-usage-order.patch", "044c71143"),),
    ),
    Probe(
        "producer-replay-registration",
        "改了收据生成器却没把部署边界处的旧摘要登记为只读重放身份",
        "VC-5", "seal 链重放 restoration 收据／后台验证 test_producer_replay_registration_gate",
        UnittestRunner([
            f"{TESTS}.test_producer_replay_registration_gate.CurrentTreeRegistrationGateTest.test_every_deployment_boundary_is_registered_for_replay",
        ], timeout_seconds=600, extra_env={"GIT_DIR": "{origin_git_dir}"}),
        needs=("git_history",), copy_extra=("docs/egress/maintenance",),
        history=("199683aab", "记忆 kilo-receipt-http-usage-order-defect-1022 连带教训", "后台验证 #15"),
        signature=("未登记的旧摘要：02bc3d7e4d7b",),
        injections=(ReversePatch("producer-replay-registration.patch", "199683aab"),),
    ),
    # 下面两个探针的注入改的是受管文件（codex_upgrade.py、codex_upgrade_reconciler.py），用例又会建 Campaign；ARM64 上固定
    # 采集执行位置有生产受管副本，副本与它漂移就在到达缺陷之前被 ConfigurationError 挡住（10-26 实跑误判成漏报）。
    # 绑定执行位置后比较的是副本自己。
    Probe(
        "orphan-candidate-recovery-preview",
        "零请求恢复预览取不到孤儿候选 attempt 的失败源（没有 attempt.json）",
        "VC-5", "候选采集被截停后的 N+1 零请求恢复预览",
        UnittestRunner([
            f"{TESTS}.test_codex_upgrade.CodexUpgradeTest.test_b0_zero_request_preview_adopts_authorized_orphan_preview",
        ], timeout_seconds=600, bind_execution_tree=True),
        history=("ca25c9936", "记忆 vc5-orphan-candidate-preview-gap-1021", "VC-5 批次 23"),
        signature=("尚未被授权消费",),
        injections=(ReversePatch("orphan-candidate-recovery-preview.patch", "ca25c9936"),),
    ),
    Probe(
        "zero-result-isolation-source",
        "环境隔离作废且一个作业结果都没有的 attempt 不能作续跑来源",
        "VC-5", "恢复预览：零结果隔离来源",
        UnittestRunner([
            f"{TESTS}.test_codex_upgrade.CodexUpgradeTest.test_b0_isolated_attempt_without_any_job_result_is_rerun_source",
        ], timeout_seconds=600, bind_execution_tree=True),
        history=("ee61713ab", "记忆 vc5-orphan-candidate-preview-gap-1021 第三轮", "VC-5 批次 25"),
        signature=("失败 attempt 缺少可重放 Job checkpoint",),
        injections=(ReversePatch("zero-result-isolation-source.patch", "ee61713ab"),),
    ),
    Probe(
        "r17-preview-isolation-window",
        "恢复预览复用 0、resume 却复用污染窗口前的作业（R17 核对拒绝批准）",
        "VC-1", "恢复预览（带环境隔离窗口）",
        UnittestRunner([f"{TESTS}.test_reconciler_isolation_window_reuse.IsolationWindowReuseTests"], timeout_seconds=300),
        history=("8f0835bca", "记忆 vc1-windowed-isolation-recovery-preview-mismatch-1021"),
        signature=("has no attribute '_isolation_window_reusable'",),
        injections=(ReversePatch("r17-preview-isolation-window.patch", "8f0835bca"),),
    ),
    Probe(
        "r17-isolated-attempt-continuity",
        "续跑承接被隔离且带窗口的 attempt 时环境连续性拒绝（无 after 探针）",
        "VC-1", "续跑承接：环境连续性",
        UnittestRunner([f"{TESTS}.test_reconciler_isolation_window_reuse.IsolatedAttemptContinuityTests"], timeout_seconds=600),
        history=("2cf62a735", "记忆 vc1-windowed-isolation-recovery-preview-mismatch-1021"),
        signature=("被承接 attempt 缺少 after 环境探针，无法证明连续性",),
        injections=(ReversePatch("r17-isolated-attempt-continuity.patch", "2cf62a735"),),
    ),
    Probe(
        "r17-strict-pre-job-failure",
        "首个 Job 前失败的严格形态没要求 before 探针未取到",
        "VC-1", "续跑承接：失败 attempt 形态判定",
        UnittestRunner([f"{TESTS}.test_reconciler_isolation_window_reuse.StrictPreJobFailureShapeTests"], timeout_seconds=600),
        history=("6cef03d79", "记忆 vc1-windowed-isolation-recovery-preview-mismatch-1021"),
        signature=("has no attribute '_is_strict_pre_job_failure'",),
        injections=(ReversePatch("r17-strict-pre-job-failure.patch", "6cef03d79"),),
    ),
    Probe(
        "r17-evolution-reuse-context",
        "失败源是最新演进之后产出的 attempt 时，续跑复用不给演进上下文",
        "VC-1", "续跑承接：演进口径复用上下文",
        UnittestRunner([f"{TESTS}.test_codex_upgrade_tool_evolution.EvolutionReuseContextTests"], timeout_seconds=600),
        history=("524de4522", "记忆 vc1-windowed-isolation-recovery-preview-mismatch-1021 第 7 处"),
        signature=("ToolEvolutionRequired not raised",),
        injections=(ReversePatch("r17-evolution-reuse-context.patch", "524de4522"),),
    ),
    Probe(
        "compaction-reason-downshift",
        "压缩原因提取把降档之后目标模型上的 context_limit 后续压缩判为非预期",
        "VC-1", "官方取证：extract_compaction_reason 判据",
        UnittestRunner([f"{TESTS}.test_extract_compaction_reason.UnexpectedReasonTests"], timeout_seconds=180),
        history=("063571f0c", "VC-1 第 25 个作业 official-relay-model-downshift"),
        signature=("cannot import name 'unexpected_reason_matches'",),
        injections=(ReversePatch("compaction-reason-downshift.patch", "063571f0c"),),
    ),
    Probe(
        "vc2-offline-gate-dual-wire",
        "VC-2 离线判据门在官方证据上实跑 candidate_profile 规则（8 项必败）",
        "VC-2", "VC-2 断言预检（vc2_assertion_preflight）",
        UnittestRunner([
            f"{TESTS}.test_arm64_driver_vc2_preflight.PreflightTests.test_official_side_executes_dual_wire_rules_only_by_default",
            f"{TESTS}.test_arm64_driver_vc2_preflight.PreflightTests.test_default_modes_never_fail_candidate_profile_checks_on_official_evidence",
        ], timeout_seconds=300),
        history=("2bc56dd77", "记忆 vc2-offline-gate-dual-wire-only-1021"),
        needs=("driver_sources",),
        signature=("does not have the attribute 'OFFICIAL_EXECUTED_MODES'",),
        injections=(ReversePatch("vc2-offline-gate-dual-wire.patch", "2bc56dd77"),),
    ),
    Probe(
        "vc6-delivery-batch-phase-resolution",
        "VC-6 交付登记批次被阶段解析误拒（canonical_batch_binding 返回 None 被当未绑定）",
        "VC-6", "VC-6 交付登记批次派发前的阶段解析（phase_context）",
        UnittestRunner([
            f"{TESTS}.test_arm64_phase_context.PhaseContextTests.test_vc6_delivery_batches_bind_current_campaign_without_canonical_receipts",
            f"{TESTS}.test_arm64_phase_context.PhaseContextTests.test_vc6_delivery_batch_rejects_wrong_binding_and_foreign_actions",
        ], timeout_seconds=300),
        needs=("driver_sources",),
        history=("49492ad3f", "86cf6bf8c（补测试）", "VC-6 第 44 批"),
        signature=("VC-6 计划未绑定当前 Campaign、Candidate 和 attempt",),
        injections=(ReversePatch("vc6-delivery-batch-phase-resolution.patch", "49492ad3f"),),
    ),
)

# ---------------------------------------------------------------------------
# 二、恢复分支故障注入探针（ARM64 root 专用；缺条件如实“未执行”）
# ---------------------------------------------------------------------------

# R17d：二次承接未重绑的承接结果时，按 Campaign 冻结身份回退验真（cad6e7955／6edb82a76）。当时没有任何单元用例覆盖，
# 只有真实恢复链走到“二次承接”才会暴露，所以把它登记为 VC-1 恢复链的历史重放：注入器把回退分支的条件改成恒假。
R17_SECOND_REUSE_FALLBACK = ExactReplacement(
    f"{T}/codex_upgrade.py",
    "                if (\n"
    "                    evolution_context is not None\n"
    "                    and not cross_campaign_source\n"
    "                    and item.get(\"disposition\") == \"reused\"\n",
    "                if (\n"
    "                    False\n"
    "                    and evolution_context is not None\n"
    "                    and not cross_campaign_source\n"
    "                    and item.get(\"disposition\") == \"reused\"\n",
    "二次承接回退分支恒假：承接链里没重绑过的结果再次承接时按源 attempt 生产身份核对不命中，被判不可复用",
)

RECOVERY_PROBES: tuple[Probe, ...] = (
    Probe(
        "vc1-recovery-fault-injection-chain",
        "VC-1 恢复链故障注入：首批超时 → 预览批次失败 → 守护者三连败 → 控制面／证据层修复部署 → 二次承接 → 封存",
        "VC-1", "录制回放：对账 → 预览 → 补跑 → 演进登记 → epoch → seal → classify",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_vc1_recorded_chain.VC1RecordedRecoveryChainTests"], timeout_seconds=3600),
        # 用例里要从仓库根复制这个脚本进录制回放的临时树；历史重放的副本只有受管树、驱动目录与 tools/ci，不带它，
        # 未注入的基线副本就红（10-26 ARM64 实跑）。
        kind="recovery", needs=("linux_root_unshare", "recorded_vc1_campaign"), copy_extra=("tools/prepare_assertion_bundle.sh",),
        history=("cad6e7955", "6edb82a76", "记忆 vc1-recorded-replay-chain", "记忆 vc1-windowed-isolation-recovery-preview-mismatch-1021"),
        # 签名取“二次承接对账（reconcile-attempt-3）”这一步的断言源码行：二次承接被判不可复用时，失败就落在这一步对
        # third_reconciled 的断言上。**未经 ARM64 实跑验证**——失败现象的消息文本我没见过，首次实跑后应改成该步骤抛出的
        # 具体错误消息（例如 R17 核对的“预览复用…resume 复用…”）。
        signature=('third_reconciled["status"]', 'third_reconciled["reuse_job_ids"]'),
        injections=(R17_SECOND_REUSE_FALLBACK,),
    ),
    Probe(
        "vc5-attempt-recovery-fault-injection",
        "VC-5 attempt 恢复链故障注入：恢复状态机 E1～E5 崩溃点、段增量 seal、恢复 finalizer",
        "VC-5", "评估 attempt 恢复（recover apply 崩溃注入）",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_evaluation_attempt_recovery_real_chain"], timeout_seconds=7200),
        kind="recovery", needs=("linux_root_unshare", "arm64_docker_go"),
        history=("记忆 vc5-reconcile-first-then-deploy-fix-limit-trap-1022",),
    ),
    Probe(
        "vc5-stage-recovery",
        "VC-5 阶段恢复链：失败批次对账、批准、重派",
        "VC-5", "阶段恢复（stage recovery）",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_stage_recovery"], timeout_seconds=1800),
        kind="recovery",
        history=("记忆 vc5-reconcile-first-then-deploy-fix-limit-trap-1022",),
    ),
    Probe(
        "official-reuse-resume",
        "官方证据复用与续跑（reuse 后继 Campaign）",
        "VC-1", "官方复用续跑",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_official_reuse_resume"], timeout_seconds=1800),
        kind="recovery",
        history=("记忆 reuse-successor-campaign-checklist",),
    ),
    Probe(
        "late-stage-faults",
        "后段注入链：VC-5 之后各阶段的故障注入",
        "VC-5", "后段注入（late stage faults）",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_late_stage_faults"], timeout_seconds=1800),
        kind="recovery", needs=("linux_root_unshare", "arm64_docker_go"),
    ),
    Probe(
        "deadline-extension-recovery",
        "阶段预算到期后的延期与恢复（deadline_paused）",
        "VC-1", "阶段延期（deadline-extend）",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_deadline_extension"], timeout_seconds=1800),
        kind="recovery",
        history=("记忆 vc1-windowed-isolation-recovery-preview-mismatch-1021 第 7 处缺陷与阶段预算到期",),
    ),
    Probe(
        "fault-fixtures-six-kinds",
        "六类历史故障的独立复现夹具（进程与 Docker 隔离链、R15 内核夹具）",
        "VC-1", "R13-3 六类故障夹具",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_fault_fixtures"], timeout_seconds=3600),
        kind="recovery", needs=("linux_root_unshare",),
    ),
)

# ---------------------------------------------------------------------------
# 三、完整链（有序，遇首个失败即停；汇总之后另跑一遍确认整体能通）
# ---------------------------------------------------------------------------

CHAIN_STEPS: tuple[Probe, ...] = (
    Probe(
        "chain-vc1-capture", "VC-0 收口建 Formal Campaign → VC-1 首批取证 → 权限收口 → 断言包 → seal → classify 草案",
        "VC-1", "录制回放取证链",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_vc1_recorded_chain.VC1RecordedCaptureChainTests"], timeout_seconds=1800),
        kind="chain", needs=("linux_root_unshare", "recorded_vc1_campaign"),
    ),
    Probe(
        "chain-vc2-vc5-validation-only", "VC-2 导入 → 三批分类 → 候选制品身份 → compare → builder → checker → accept → deliver",
        "VC-5", "连续 validation_only 链",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_full_chain"], timeout_seconds=3600),
        kind="chain", needs=("linux_root_unshare", "arm64_docker_go"),
    ),
    Probe(
        "chain-vc5-evaluation", "VC-5 真实评估链：compare／builder／accept 与 VC-5 completion",
        "VC-5", "评估链",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_evaluation_real_chain"], timeout_seconds=3600),
        kind="chain", needs=("linux_root_unshare", "arm64_docker_go"),
    ),
    Probe(
        "chain-vc5-build-revision", "VC-4／VC-5 候选构建修订链",
        "VC-5", "构建修订",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_build_revision"], timeout_seconds=3600),
        kind="chain", needs=("arm64_docker_go",),
    ),
    Probe(
        "chain-vc5-segment-reuse", "VC-5 段复用",
        "VC-5", "段复用",
        UnittestRunner([f"{REAL_CHAINS}.test_codex_upgrade_segment_reuse"], timeout_seconds=1800),
        kind="chain",
    ),
    Probe(
        "chain-vc6-canonical", "VC-5 canonical 交接与 VC-6 canonical／交付批次派发",
        "VC-6", "canonical 与 VC-6 批次",
        UnittestRunner([f"{TESTS}.test_codex_upgrade_canonical", f"{TESTS}.test_codex_upgrade_vc6_canonical_dispatch"],
                       timeout_seconds=1800),
        kind="chain",
    ),
)

# 完整链的范围声明（写进汇总与 README）：它是 6 个真实链模块串联，不是整条 VC-0～VC-6 的端到端回放。
CHAIN_SCOPE_NOTE = (
    "本完整链是 6 个真实链模块的串联（VC-1 取证、VC-2～VC-5 连续链、评估、构建修订、段复用、VC-6 canonical），"
    "不含候选侧 seal、compare、accept 链；接入 rehearse-candidate-seal 需要上一版候选证据，放在 ARM64 阶段。"
)

CHECKPOINTS: Mapping[str, Checkpoint] = {}


def registry() -> Registry:
    """引擎用的登记表：独立探针＝历史缺陷＋恢复分支；完整链单列。"""

    return Registry(
        probes=DEFECT_PROBES + RECOVERY_PROBES,
        chain=CHAIN_STEPS,
        checkpoints=CHECKPOINTS,
        copy_tree=copy_repo_subset,
        inject=inject_module.inject,
        chain_note=CHAIN_SCOPE_NOTE,
    )


def all_unittest_ids(probes: Sequence[Probe] | None = None) -> list[str]:
    """目录里引用的全部单元用例名（目录自检用：每个名字都必须能被加载）。"""

    ids: list[str] = []
    for probe in (probes if probes is not None else registry().probes + registry().chain):
        runner = probe.runner
        if isinstance(runner, UnittestRunner):
            ids.extend(runner.test_ids)
    return ids
