#!/usr/bin/env python3
"""维护并重放 Codex 官方客户端升级的只追加 UpgradeTimingLedger。"""

from __future__ import annotations

import argparse
import fcntl
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping


PLAN_SCHEMA = "codex-upgrade-timing-ledger-plan/v1"
EVENT_SCHEMA = "codex-upgrade-timing-ledger-event/v1"
RECEIPT_SCHEMA = "codex-upgrade-timing-ledger-receipt/v1"
PRODUCER_SCHEMA = "codex-upgrade-timing-ledger-producer/v1"
PRODUCER_VERSION = "1"
PHASE_ORDER = ("VC-0", "VC-1", "VC-2", "VC-3", "VC-4", "VC-5", "VC-6")
DEFAULT_STAGE_BUDGETS = {
    "VC-0": 45,
    "VC-1": 75,
    "VC-2": 75,
    "VC-3": 75,
    "VC-4": 90,
    "VC-5": 75,
    "VC-6": 75,
}
DEFAULT_TOTAL_BUDGET_MINUTES = 360
# 未绑定项目总账时沿用文档上限；绑定后由总账绝对截止裁剪（见 _budget_ceilings）。
UNBOUND_TOTAL_BUDGET_CEILING_MINUTES = 360
PROJECT_LEDGER_BINDING_FIELD = "project_ledger_binding"
PROJECT_LEDGER_BINDING_FIELDS = {"path", "plan_sha256", "absolute_deadline_utc"}
# R8：新账本在创建时冻结开始时刻的项目有效截止（含此前已批准的项目延期），读侧据此计算预算上限，
# 不再为校验计划而打开并重放总账；没有该字段的历史绑定按原绝对截止计算（与 main 相同）。
PROJECT_LEDGER_FROZEN_DEADLINE_FIELD = "effective_deadline_at_start_utc"
DEFAULT_RETRY_LIMIT = 2
PURPOSES = frozenset({"validation_only", "production_replacement"})
EVIDENCE_DECISIONS = frozenset({"reuse", "recapture"})
EVENT_TYPES = frozenset(
    {
        "stage_started",
        "stage_completed",
        "stage_abandoned",
        "attempt_started",
        "attempt_failed",
        "attempt_completed",
        "recovery_required",
        "recovery_authorized",
        "receipt_passed",
        "stop_the_line",
        "recovery_verified",
        "upgrade_completed",
        # 改造 2（候选级 revision）：候选动作失败后的只读等待、显式作废与新 revision 激活。
        "candidate_review_required",
        "stage_review_required",
        "candidate_invalidated",
        "stage_revision",
        # 改造 5（评估失败局部恢复）：评估基线 b<K> 激活与 attempt 恢复段 ar<k> 的三个事件；
        # 都在 VC-5 active 内发生，不新增账本状态。
        "evaluation_baseline",
        "attempt_recovery_started",
        "attempt_recovery_completed",
        "attempt_recovery_failed",
        "deadline_paused",
        "deadline_extended",
        "campaign_abandoned",
    }
)
# 候选级阶段：VC-4～VC-6 的阶段／attempt 事件按 revision 归属；VC-0～VC-3 是 Campaign 级。
CANDIDATE_PHASES = ("VC-4", "VC-5", "VC-6")
CANDIDATE_STAGE_EVENT_TYPES = frozenset(
    {
        "stage_started",
        "stage_completed",
        "stage_abandoned",
        "attempt_started",
        "attempt_failed",
        "attempt_completed",
        "candidate_review_required",
        "candidate_invalidated",
        "evaluation_baseline",
        "attempt_recovery_started",
        "attempt_recovery_completed",
        "attempt_recovery_failed",
    }
)
# 事件的可选 revision 字段：历史事件没有这些字段，回放时候选级事件缺失视为 r1。
# 改造 5 再加四个：evaluation_baseline／baseline_commit_sha256／baseline_kind 只属于
# evaluation_baseline 事件；recovery_revision 属于 evaluation_baseline（attempt-recovery 基线）
# 与三个 attempt_recovery_* 事件。
EVENT_REVISION_FIELDS = frozenset(
    {
        "revision",
        "candidate_id",
        "revision_commit_sha256",
        "supersedes_revision",
        "evaluation_baseline",
        "baseline_commit_sha256",
        "baseline_kind",
        "recovery_revision",
    }
)
EVALUATION_BASELINE_KINDS = ("evaluator-only", "attempt-recovery")
ATTEMPT_RECOVERY_EVENT_TYPES = frozenset(
    {"attempt_recovery_started", "attempt_recovery_completed", "attempt_recovery_failed"}
)
DEADLINE_CONTROL_EVENTS = frozenset({"deadline_paused", "deadline_extended", "campaign_abandoned"})


def _deadline_modules():
    """延迟导入控制模块，避免计时账本与项目总账初始化时互相依赖。"""
    if __package__ in {None, ""}:
        import codex_upgrade_vc_artifacts as artifacts
        import codex_upgrade_project_ledger as project
    else:
        from . import codex_upgrade_vc_artifacts as artifacts, codex_upgrade_project_ledger as project
    return artifacts, project


def _attempt_recovery_revision_admissible(
    attempt_id: str,
    recovery_revision: str,
    *,
    frozen_revision: object,
    attempt_recoveries: Mapping[str, Mapping[str, object]],
) -> bool:
    """改造 5 M2（崩溃矩阵 A1）：恢复段编号要么等于当前基线冻结的段（首段），要么是该 attempt 已登记段的
    下一个后继段——此时首段必须已登记且该 attempt 的全部已登记段都是 failed 终态（段中断／失败经
    ``reconcile-attempt --recovery-revision`` 入账后，同一基线下以新段全量补跑，不重复裁定根因）。"""

    if recovery_revision == frozen_revision:
        return True
    if not isinstance(frozen_revision, str):
        return False
    prefix = f"{attempt_id}:"
    segments = {
        key[len(prefix):]: item for key, item in attempt_recoveries.items() if key.startswith(prefix)
    }
    if frozen_revision not in segments:
        return False
    if any(item.get("status") != "failed" for item in segments.values()):
        return False
    numbers: list[int] = []
    for revision in segments:
        if not revision.startswith("ar") or not revision[2:].isdigit():
            return False
        numbers.append(int(revision[2:]))
    if not recovery_revision.startswith("ar") or not recovery_revision[2:].isdigit():
        return False
    return int(recovery_revision[2:]) == max(numbers) + 1


RECOVERY_REVISION_RE = re.compile(r"^ar[1-9][0-9]*$")
REVIEW_REQUIRED_ALLOWED_EVENTS = frozenset(
    {"attempt_failed", "receipt_passed", "candidate_invalidated", "stage_abandoned", "stop_the_line"}
)
STAGE_REVIEW_ALLOWED_EVENTS = frozenset(
    {"attempt_started", "attempt_failed", "receipt_passed", "stage_abandoned", "stop_the_line", "recovery_authorized"}
)
REVISION_REQUIRED_ALLOWED_EVENTS = frozenset(
    {"candidate_invalidated", "stage_revision", "stop_the_line"}
)
RECOVERY_ROLES = ("clean_p0", "offline_regression", "tool_fix")
RECOVERY_AUTHORIZATION_ROLES = ("recovery_approval", "recovery_preview")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
MAX_JSON_BYTES = 4 * 1024 * 1024
PRODUCER_TOOL_RELATIVE = "tools/official_client_capture/codex_upgrade_timing_ledger.py"
CURRENT_WORKTREE_SUCCESSOR_RELATIVE = (
    "docs/egress/maintenance/codex-cli-0151-worktree-successor.json"
)
PRODUCER_SUCCESSOR_TRANSITIONS = (
    {
        "path": "docs/egress/maintenance/codex-cli-0151-container-path-recovery-tool-successor-source-transition.json",
        "schema_version": "sub2apiplus-codex-cli-0151-container-path-recovery-tool-successor-source-transition/v1",
        "base_commit": "432a4dfb9dc612b0343ed217b8dace587698fc37",
        "scope": "codex-cli-0.151-container-path-recovery-tool-successor",
        "result": "passed_codex_cli_0151_container_path_recovery_tool_successor",
    },
    {
        "path": "docs/egress/maintenance/codex-cli-0151-timing-producer-replay-tool-successor-source-transition.json",
        "schema_version": "sub2apiplus-codex-cli-0151-timing-producer-replay-tool-successor-source-transition/v1",
        "base_commit": "990c26f955fde57817fe1e0e98862d01c3ec5f7d",
        "scope": "codex-cli-0.151-timing-producer-replay-tool-successor",
        "result": "passed_codex_cli_0151_timing_producer_replay_tool_successor",
    },
    {
        "path": "docs/egress/maintenance/codex-cli-0151-producer-coordinate-decoupling-source-transition.json",
        "schema_version": "sub2apiplus-codex-cli-0151-producer-coordinate-decoupling-source-transition/v1",
        "base_commit": "b4c3b58ea13a3aa85ed22e68586cfe368b0c9d88",
        "scope": "codex-cli-0.151-producer-coordinate-decoupling",
        "result": "passed_codex_cli_0151_producer_coordinate_decoupling",
    },
)
# 通用 freeze successor 也会改变计时工具摘要。这里逐份列出允许参与历史
# UpgradeTimingLedger 重放的收据；运行时只沿这些显式文件中的精确边前进，
# 不扫描 maintenance 目录，也不接受未登记摘要。
PRODUCER_FREEZE_SUCCESSORS = (
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc1-general-toolchain-closeout-20260915-freeze-successor.json",
        "base_commit": "92e82aade5f69ffd253045478e554c13c1fe2ab6",
        "scope": "upstream-codex-0154-vc1-general-toolchain-closeout-20260915-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc1-timing-producer-chain-closeout-20260915-freeze-successor.json",
        "base_commit": "a50cb75af108ac45ce954c43da61b2cdc32263fe",
        "scope": "upstream-codex-0154-vc1-timing-producer-chain-closeout-20260915-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a0a-tool-unlock-20260915-freeze-successor.json",
        "base_commit": "ff5f94cb46eb05ab209debee291836b97f083678",
        "scope": "upstream-codex-0154-a0a-tool-unlock-20260915-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a2-a3a-policy-reuse-20260916-freeze-successor.json",
        "base_commit": "3f34d0ac12658983d591a8ae3ed7c8dd4a99e528",
        "scope": "upstream-codex-0154-a2-a3a-policy-reuse-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-b0-reconciler-20260916-freeze-successor.json",
        "base_commit": "7fb309d025f29eafd036265149ec64f92c5deca2",
        "scope": "upstream-codex-0154-b0-reconciler-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a26-a25-policy-certification-20260916-freeze-successor.json",
        "base_commit": "ce8887fd23beb4ca5a81736e98bc13b61925828c",
        "scope": "upstream-codex-0154-a26-a25-policy-certification-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a25-py312-nesting-20260916-freeze-successor.json",
        "base_commit": "f92f978974e0a0c207475c4947652ff2eb09873d",
        "scope": "upstream-codex-0154-a25-py312-nesting-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-b1-deletion-proof-20260916-freeze-successor.json",
        "base_commit": "938a2f9e2b187f466844ae658cb837c88cff7678",
        "scope": "upstream-codex-0154-b1-deletion-proof-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-b-cleanup-20260916-freeze-successor.json",
        "base_commit": "ad2547a86d652d7978d726706311e1c311d76825",
        "scope": "upstream-codex-0154-b-cleanup-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-c-release-certification-20260916-freeze-successor.json",
        "base_commit": "c589f32de76026478a1f4bdc13095a072336bea7",
        "scope": "upstream-codex-0154-c-release-certification-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a1b-verdict-copy-20260916-freeze-successor.json",
        "base_commit": "d78cf9cfbf21cd5dfae2e626af9a33041920e200",
        "scope": "upstream-codex-0154-a1b-verdict-copy-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-c1-atomic-container-20260916-freeze-successor.json",
        "base_commit": "6526921ee27ed0ec05cee70bf25eb4ce51620524",
        "scope": "upstream-codex-0154-c1-atomic-container-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-c3-historical-p0-compat-20260916-freeze-successor.json",
        "base_commit": "ad0dd0d692fd20626bb56b305786978d2e37d9c9",
        "scope": "upstream-codex-0154-c3-historical-p0-compat-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a0b-provenance-campaign-id-20260916-freeze-successor.json",
        "base_commit": "183ae567735f44a19f4a970172a7286be7491fd9",
        "scope": "upstream-codex-0154-a0b-provenance-campaign-id-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a2-contract-v2-identity-20260916-freeze-successor.json",
        "base_commit": "13f0ea026ab460aca411a880c3efd173b21d2174",
        "scope": "upstream-codex-0154-a2-contract-v2-identity-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a3a-harden-tcpdump-owner-20260916-freeze-successor.json",
        "base_commit": "30d7c15b444be15a7da9493ce28dce0510f598f6",
        "scope": "upstream-codex-0154-a3a-harden-tcpdump-owner-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc1-evidence-boundary-v2-20260916-freeze-successor.json",
        "base_commit": "bf66ed875c59bb921e53bafaad67a50e3b07153a",
        "scope": "upstream-codex-0154-vc1-evidence-boundary-v2-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a0b-stop-phase-sealed-accounting-20260916-freeze-successor.json",
        "base_commit": "3b92135737d187dbe38536dd6f55c0eb63d07e2b",
        "scope": "upstream-codex-0154-a0b-stop-phase-sealed-accounting-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-a0b-stop-phase-import-sites-20260916-freeze-successor.json",
        "base_commit": "6d0eff0ebb1a3727a0229e558e60de810e9ef37e",
        "scope": "upstream-codex-0154-a0b-stop-phase-import-sites-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc2-vc6-governance-20260916-freeze-successor.json",
        "base_commit": "d2ae30b46bea56a1f28936fcde9578f61e411c77",
        "scope": "upstream-codex-0154-vc2-vc6-governance-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc2-classify-import-origin-20260916-freeze-successor.json",
        "base_commit": "138a912d4a169217a90eeb537931fa182fb2e2d6",
        "scope": "upstream-codex-0154-vc2-classify-import-origin-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc2-draft-legal-stop-20260916-freeze-successor.json",
        "base_commit": "a19e2e0d4518850a4c34cd85a360c29ebbb23b8d",
        "scope": "upstream-codex-0154-vc2-draft-legal-stop-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-client-checkpoint-legal-stop-20260916-freeze-successor.json",
        "base_commit": "63de165d4698872caf837abe941a48d4bbdd6455",
        "scope": "upstream-codex-0154-vc5-client-checkpoint-legal-stop-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-a15-models-cache-restart-20260916-freeze-successor.json",
        "base_commit": "2aed4f8b2ea742f4177d8de2e8299f7d627d8dcd",
        "scope": "upstream-codex-0154-vc5-a15-models-cache-restart-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-aux-empty-mapping-20260916-freeze-successor.json",
        "base_commit": "8c2455ae86312ed3c6000ef73613ff192465c731",
        "scope": "upstream-codex-0154-vc5-aux-empty-mapping-20260916-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-framework-closure-20260917-freeze-successor.json",
        "base_commit": "ea91d974529bf860c0d1785ce081f796bc4bdacf",
        "scope": "upstream-codex-0154-vc5-framework-closure-20260917-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-provenance-pcap-owner-20260917-freeze-successor.json",
        "base_commit": "949780405997698b944e396f813f65bdcf7a568e",
        "scope": "upstream-codex-0154-vc5-provenance-pcap-owner-20260917-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-reconciliation-producer-registration-20260917-freeze-successor.json",
        "base_commit": "f5aeaec617b6913f266555e3900d0cfc4919d2bb",
        "scope": "upstream-codex-0154-vc5-reconciliation-producer-registration-20260917-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-classification-successor-vc-control-20260917-freeze-successor.json",
        "base_commit": "4035ac6cbe6722d888d57a3eed992693caccfcdc",
        "scope": "upstream-codex-0154-classification-successor-vc-control-20260917-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-failed-job-recovery-20260917-freeze-successor.json",
        "base_commit": "3cfd8cbe31f24127840e4d6ce59b08805afd2dd9",
        "scope": "upstream-codex-0154-vc5-failed-job-recovery-20260917-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-legacy-build-replay-20260917-freeze-successor.json",
        "base_commit": "7bcb3f213194d551e2c64c18619134038f8f8189",
        "scope": "upstream-codex-0154-vc5-legacy-build-replay-20260917-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-runtime-rebase-component-20260917-freeze-successor.json",
        "base_commit": "e422e021850c4eeee0a68692d7a6d20e798105d1",
        "scope": "upstream-codex-0154-vc5-runtime-rebase-component-20260917-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        # 改造 2（候选级 revision）：账本新增三类事件、两个状态与 revision 摘要字段。
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-tooling-batch2-m2-20260919-freeze-successor.json",
        "base_commit": "8415fcd530926dd7ed2ef2945bdbf51fcd2bb0bc",
        "scope": "upstream-codex-0154-vc5-tooling-batch2-m2-20260919-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m1-20260920-freeze-successor.json",
        "base_commit": "3f9ffb12e8b59e627b425cdc001f2873689c15dc",
        "scope": "upstream-codex-0154-vc5-tooling-batch3-m1-20260920-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc5-tooling-batch3-m2-t518-20260921-freeze-successor.json",
        "base_commit": "932111bc7b6a69b24fc986180e9da3f6c93da3c1",
        "scope": "upstream-codex-0154-vc5-tooling-batch3-m2-t518-20260921-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-01561-r4-20260924-freeze-successor.json",
        "base_commit": "a15f5c74f9e591a4bc3cf7f479d44e5d04ec5259",
        "scope": "upstream-codex-01561-r4-20260924-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-01561-r8-20260924-freeze-successor.json",
        "base_commit": "f0cbc122d20236522e87a5f23c2564229f051058",
        "scope": "upstream-codex-01561-r8-20260924-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-01561-r8-final-20260924-freeze-successor.json",
        "base_commit": "3b4df92c191126944bad73e991076285608eee25",
        "scope": "upstream-codex-01561-r8-final-20260924-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-01561-r2-review-fix-20260924-freeze-successor.json",
        "base_commit": "4cad7d625573de351245d424d7efad18ae9529d2",
        "scope": "upstream-codex-01561-r2-review-fix-20260924-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-01561-r4-review-fix-20260924-freeze-successor.json",
        "base_commit": "69f37a1da95eea876fc8fc85dcb07f506968b137",
        "scope": "upstream-codex-01561-r4-review-fix-20260924-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-01561-r8-review-fix-20260924-freeze-successor.json",
        "base_commit": "fc071b4b263204d80d24d4152c6c743211e53c9a",
        "scope": "upstream-codex-01561-r8-review-fix-20260924-freeze-successor",
        "result": "manual_actions_required",
    },
)


class TimingLedgerError(ValueError):
    """计时台账不完整、超时、发生漂移或违反重试纪律。"""


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    # 批准事件携带微秒时间；截断到整秒会使紧随其后的读取早于刚写入事件。
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise TimingLedgerError(f"{label}不是带时区 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TimingLedgerError(f"{label}不是有效时间") from error
    if parsed.tzinfo is None:
        raise TimingLedgerError(f"{label}缺少时区")
    return parsed.astimezone(timezone.utc)


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise TimingLedgerError(f"{label}不是安全标识")
    return value


def _expect(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TimingLedgerError(f"{label}必须是对象")
    actual = set(value)
    if actual != fields:
        raise TimingLedgerError(
            f"{label}字段不闭合：缺失={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )
    return value


def _private_ledger(path: Path, *, must_exist: bool) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise TimingLedgerError("ledger dir 必须是非符号链接绝对路径")
    if must_exist:
        if not path.is_dir():
            raise TimingLedgerError("ledger dir 不存在")
        resolved = path.resolve(strict=True)
        if stat.S_IMODE(resolved.stat().st_mode) != 0o700:
            raise TimingLedgerError("ledger dir 权限必须是 0700")
        return resolved
    if path.exists():
        raise TimingLedgerError("ledger dir 已存在，禁止覆盖")
    parent = path.parent.resolve(strict=True)
    if path.parent.is_symlink() or not parent.is_dir():
        raise TimingLedgerError("ledger dir 父目录不可信")
    return path


def _relative(root: Path, value: str, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise TimingLedgerError(f"{label}必须是 ledger 内 POSIX 相对路径")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or str(parsed) != value
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise TimingLedgerError(f"{label}路径不规范")
    current = root
    for part in parsed.parts:
        current /= part
        if current.is_symlink():
            raise TimingLedgerError(f"{label}路径包含符号链接")
    try:
        current.resolve(strict=current.exists()).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise TimingLedgerError(f"{label}越过 ledger dir") from error
    return current


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file() or path.is_symlink():
        raise TimingLedgerError(f"{label}不是可信普通文件")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise TimingLedgerError(f"{label}权限必须是 0600")
    if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
        raise TimingLedgerError(f"{label}大小非法")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TimingLedgerError(f"{label}不是合法 UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise TimingLedgerError(f"{label}顶层必须是对象")
    return payload, raw


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise TimingLedgerError(f"输出已存在，禁止覆盖：{path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise TimingLedgerError("输出父目录不可信")
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise TimingLedgerError("输出父目录权限必须是 0700")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _producer() -> dict[str, str]:
    tool = Path(__file__).resolve()
    return {
        "schema_version": PRODUCER_SCHEMA,
        "tool": str(tool),
        "tool_sha256": _sha256_file(tool),
        "version": PRODUCER_VERSION,
    }


def _repository_file(root: Path, relative: Any, label: str) -> Path:
    """解析并约束 Git 工作树内的只读来源文件。"""

    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise TimingLedgerError(f"{label}不是规范仓库相对路径")
    parsed = PurePosixPath(relative)
    if (
        parsed.is_absolute()
        or str(parsed) != relative
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise TimingLedgerError(f"{label}不是规范仓库相对路径")
    current = root
    for part in parsed.parts:
        current /= part
        if current.is_symlink():
            raise TimingLedgerError(f"{label}包含符号链接")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise TimingLedgerError(f"{label}越过仓库根或不存在") from error
    metadata = resolved.stat()
    if not resolved.is_file() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise TimingLedgerError(f"{label}不是只读普通文件")
    if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
        raise TimingLedgerError(f"{label}大小非法")
    return resolved


def _load_producer_successor_edge(
    repository_root: Path,
    descriptor: dict[str, str],
) -> tuple[str, str]:
    """重放一个已登记来源 transition 中的计时工具精确摘要边。"""

    transition_path = _repository_file(
        repository_root,
        descriptor["path"],
        "producer successor transition",
    )
    raw = transition_path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TimingLedgerError("producer successor transition 不是合法 JSON") from error
    transition = _expect(
        payload,
        {
            "schema_version",
            "issued_at_utc",
            "base_commit",
            "scope",
            "predecessor",
            "transitions",
            "additions",
            "verification",
            "safety",
            "result",
            "identity_sha256",
        },
        "producer successor transition",
    )
    for field in ("schema_version", "base_commit", "scope", "result"):
        if transition.get(field) != descriptor[field]:
            raise TimingLedgerError(f"producer successor transition {field} 漂移")
    _timestamp(transition.get("issued_at_utc"), "producer successor issued_at_utc")
    identity = transition.get("identity_sha256")
    unsigned = dict(transition)
    unsigned.pop("identity_sha256")
    if not isinstance(identity, str) or not SHA256_RE.fullmatch(identity):
        raise TimingLedgerError("producer successor transition 自摘要非法")
    if _sha256_bytes(_canonical(unsigned)) != identity:
        raise TimingLedgerError("producer successor transition 自摘要不一致")
    predecessor = _expect(
        transition.get("predecessor"),
        {"kind", "path", "sha256"},
        "producer successor predecessor",
    )
    predecessor_path = _repository_file(
        repository_root,
        predecessor.get("path"),
        "producer successor predecessor.path",
    )
    predecessor_sha256 = predecessor.get("sha256")
    if (
        not isinstance(predecessor_sha256, str)
        or not SHA256_RE.fullmatch(predecessor_sha256)
        or _sha256_file(predecessor_path) != predecessor_sha256
    ):
        raise TimingLedgerError("producer successor predecessor 摘要不一致")
    safety = _expect(
        transition.get("safety"),
        {
            "live_account_used",
            "online_acceptance_performed",
            "production_config_changed",
            "official_egress_profile_changed",
        },
        "producer successor safety",
    )
    if any(safety.values()):
        raise TimingLedgerError("producer successor transition 超出离线工具修复边界")
    entries = transition.get("transitions")
    if not isinstance(entries, list):
        raise TimingLedgerError("producer successor transitions 不是数组")
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and item.get("path") == PRODUCER_TOOL_RELATIVE
    ]
    if len(matches) != 1:
        raise TimingLedgerError("producer successor transition 未唯一登记计时工具")
    edge = _expect(
        matches[0],
        {"path", "from_sha256", "to_sha256", "reason"},
        "producer successor tool edge",
    )
    before = edge.get("from_sha256")
    after = edge.get("to_sha256")
    if (
        not isinstance(before, str)
        or not SHA256_RE.fullmatch(before)
        or not isinstance(after, str)
        or not SHA256_RE.fullmatch(after)
        or before == after
        or not isinstance(edge.get("reason"), str)
        or not edge["reason"].strip()
    ):
        raise TimingLedgerError("producer successor tool edge 非法")
    return before, after


FREEZE_RESULT_PASSED_WITH_DELETIONS = "passed_with_deletions"
FREEZE_DELETION_PROOF_ALGORITHM = "deletion-proof/v1"
FREEZE_REFERENCE_SCAN_ALGORITHM = "reference-scan/v1"


def _validate_freeze_deletion_proof(
    repository_root: Path,
    receipt: dict[str, Any],
) -> None:
    """B1：删除冻结路径的 freeze successor 必须携带三项证明。

    与 ``codex_upgrade._validate_freeze_deletion_proof`` 语义一致，但计时账本
    保持自包含：非空删除原因、逐路径无引用扫描为空、历史读取器仍在仓库内且
    摘要一致。未删除冻结路径时不得携带 ``deletion_proof``。
    """

    deleted = receipt.get("deleted_frozen_paths")
    proof = receipt.get("deletion_proof")
    if not isinstance(deleted, list) or any(
        not isinstance(item, str) or not item for item in deleted
    ):
        raise TimingLedgerError("producer freeze successor deleted_frozen_paths 非法")
    if not deleted and proof is None:
        return
    if deleted and receipt.get("result") != FREEZE_RESULT_PASSED_WITH_DELETIONS:
        raise TimingLedgerError("producer freeze successor 删除了冻结路径但 result 不是 passed_with_deletions")
    # 未登记路径的删除也可以携带证明；此时证明里不得声称任何路径是冻结路径。
    proof = _expect(
        proof,
        {"algorithm", "reason", "deleted_paths", "historical_readers"},
        "producer freeze successor deletion_proof",
    )
    if (
        proof.get("algorithm") != FREEZE_DELETION_PROOF_ALGORITHM
        or not isinstance(proof.get("reason"), str)
        or not proof["reason"].strip()
        or not isinstance(proof.get("deleted_paths"), list)
        or not isinstance(proof.get("historical_readers"), list)
        or not proof["historical_readers"]
    ):
        raise TimingLedgerError("producer freeze successor deletion_proof 原因或历史读取器缺失")
    covered: dict[str, bool] = {}
    for item in proof["deleted_paths"]:
        entry = _expect(item, {"path", "frozen", "last_sha256", "reference_scan"}, "deletion_proof 删除路径")
        scan = _expect(
            entry.get("reference_scan"),
            {"algorithm", "patterns", "scopes", "references"},
            "deletion_proof 引用扫描",
        )
        if (
            not isinstance(entry.get("path"), str)
            or not entry["path"]
            or not isinstance(entry.get("frozen"), bool)
            or scan.get("algorithm") != FREEZE_REFERENCE_SCAN_ALGORITHM
            or not isinstance(scan.get("patterns"), list)
            or not scan["patterns"]
            or scan.get("references") != []
        ):
            raise TimingLedgerError("producer freeze successor 删除路径仍有引用或扫描证明非法")
        covered[str(entry["path"])] = bool(entry["frozen"])
    if any(covered.get(path) is not True for path in deleted):
        raise TimingLedgerError("producer freeze successor 存在没有无引用证明的冻结删除路径")
    if any(frozen and path not in deleted for path, frozen in covered.items()):
        raise TimingLedgerError("producer freeze successor deletion_proof 声称的冻结删除路径与 deleted_frozen_paths 不一致")
    for reader in proof["historical_readers"]:
        binding = _expect(reader, {"path", "sha256"}, "deletion_proof 历史读取器")
        relative = binding.get("path")
        if (
            not isinstance(relative, str)
            or not relative.endswith(".py")
            or relative in deleted
            or not isinstance(binding.get("sha256"), str)
            or not SHA256_RE.fullmatch(binding["sha256"])
        ):
            raise TimingLedgerError("producer freeze successor 历史读取器登记非法")
        # 记录的 sha256 只是签发时的快照；读取器后续按通用 successor 图演进，
        # 这里只要求它仍是仓库内存在的普通文件。
        _repository_file(repository_root, relative, "deletion_proof 历史读取器")


def _load_freeze_successor_edge(
    repository_root: Path,
    descriptor: dict[str, str],
) -> tuple[str, str]:
    """重放一份显式登记的通用 freeze successor 计时工具摘要边。"""

    receipt_path = _repository_file(
        repository_root,
        descriptor["path"],
        "producer freeze successor",
    )
    try:
        payload = json.loads(receipt_path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TimingLedgerError("producer freeze successor 不是合法 JSON") from error
    receipt_fields = {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "current_commit",
        "scope",
        "mode",
        "extra_worktree_paths",
        "frozen_path_count",
        "frozen_edge_count",
        "changed_path_count",
        "transitions",
        "unregistered_path_count",
        "unregistered_paths",
        "deleted_frozen_paths",
        "required_manual_actions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }
    if isinstance(payload, dict) and "deletion_proof" in payload:
        # B1：带证明删除冻结路径的收据额外携带 deletion_proof。
        receipt_fields = receipt_fields | {"deletion_proof"}
    receipt = _expect(payload, receipt_fields, "producer freeze successor")
    expected_fields = {
        "schema_version": "official-egress-upstream-freeze-successor/v1",
        "base_commit": descriptor["base_commit"],
        "scope": descriptor["scope"],
        "mode": "commit",
        "result": descriptor["result"],
    }
    for field, expected in expected_fields.items():
        if receipt.get(field) != expected:
            raise TimingLedgerError(f"producer freeze successor {field} 漂移")
    _timestamp(receipt.get("issued_at_utc"), "producer freeze successor issued_at_utc")
    current_commit = receipt.get("current_commit")
    if (
        not isinstance(current_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", current_commit)
        or current_commit == descriptor["base_commit"]
    ):
        raise TimingLedgerError("producer freeze successor current_commit 非法")
    identity = receipt.get("identity_sha256")
    unsigned = dict(receipt)
    unsigned.pop("identity_sha256")
    compact = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not isinstance(identity, str) or not SHA256_RE.fullmatch(identity):
        raise TimingLedgerError("producer freeze successor 自摘要非法")
    if _sha256_bytes(compact) != identity:
        raise TimingLedgerError("producer freeze successor 自摘要不一致")
    safety = _expect(
        receipt.get("safety"),
        {
            "deployment_performed",
            "live_account_used",
            "official_egress_profile_changed",
            "production_config_changed",
            "wire_or_persona_selection_changed",
        },
        "producer freeze successor safety",
    )
    if any(safety.values()):
        raise TimingLedgerError("producer freeze successor 超出离线工具修复边界")
    _validate_freeze_deletion_proof(repository_root, receipt)
    entries = receipt.get("transitions")
    if not isinstance(entries, list):
        raise TimingLedgerError("producer freeze successor transitions 不是数组")
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and item.get("path") == PRODUCER_TOOL_RELATIVE
    ]
    if len(matches) != 1:
        raise TimingLedgerError("producer freeze successor 未唯一登记计时工具")
    edge = _expect(
        matches[0],
        {
            "path",
            "old_path",
            "status",
            "predecessor_sha256s",
            "to_sha256",
            "source_receipts",
            "reason",
        },
        "producer freeze successor tool edge",
    )
    predecessors = edge.get("predecessor_sha256s")
    after = edge.get("to_sha256")
    if (
        edge.get("old_path") != ""
        or edge.get("status") != "M"
        or not isinstance(predecessors, list)
        or len(predecessors) != 1
        or not isinstance(predecessors[0], str)
        or not SHA256_RE.fullmatch(predecessors[0])
        or not isinstance(after, str)
        or not SHA256_RE.fullmatch(after)
        or predecessors[0] == after
        or not isinstance(edge.get("source_receipts"), list)
        or not edge["source_receipts"]
        or not all(isinstance(item, str) and item for item in edge["source_receipts"])
        or not isinstance(edge.get("reason"), str)
        or not edge["reason"].strip()
    ):
        raise TimingLedgerError("producer freeze successor tool edge 非法")
    return predecessors[0], after


def _producer_tool_coordinate(value: Any) -> tuple[str, ...] | None:
    """提取 producer 的规范相对坐标，忽略工作树根目录。"""

    if not isinstance(value, str) or not value or not value.startswith("/"):
        return None
    try:
        parsed = PurePosixPath(value)
        parts = parsed.parts
    except (TypeError, ValueError):
        return None
    relative = tuple(PurePosixPath(PRODUCER_TOOL_RELATIVE).parts)
    if (
        str(parsed) != value
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) < len(relative)
        or parts[-len(relative) :] != relative
    ):
        return None
    return relative


def _load_current_worktree_successor_edge(
    repository_root: Path,
) -> tuple[str, str] | None:
    """读取 0.151 工作区快照对计时工具追加的初始摘要边。"""

    path = _repository_file(
        repository_root,
        CURRENT_WORKTREE_SUCCESSOR_RELATIVE,
        "current worktree successor",
    )
    try:
        payload = json.loads(path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TimingLedgerError("current worktree successor 不是合法 JSON") from error
    if payload.get("schema_version") != "sub2apiplus-codex-cli-0151-worktree-successor/v1":
        raise TimingLedgerError("current worktree successor schema 漂移")
    identity = payload.get("identity_sha256")
    unsigned = dict(payload)
    unsigned.pop("identity_sha256", None)
    pretty = (json.dumps(unsigned, ensure_ascii=False, indent=2) + "\n").encode()
    if not isinstance(identity, str) or not SHA256_RE.fullmatch(identity):
        raise TimingLedgerError("current worktree successor 自摘要非法")
    if _sha256_bytes(pretty) != identity:
        raise TimingLedgerError("current worktree successor 自摘要不一致")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise TimingLedgerError("current worktree successor entries 非法")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("path") == PRODUCER_TOOL_RELATIVE
    ]
    if len(matches) != 1:
        raise TimingLedgerError("current worktree successor 未唯一登记计时工具")
    entry = matches[0]
    before = (entry.get("before") or {}).get("sha256")
    after = (entry.get("after") or {}).get("sha256")
    if (
        not isinstance(before, str)
        or not SHA256_RE.fullmatch(before)
        or not isinstance(after, str)
        or not SHA256_RE.fullmatch(after)
        or before == after
    ):
        raise TimingLedgerError("current worktree successor 计时工具摘要边非法")
    return before, after


def _producer_identity_matches(frozen: Any, current: dict[str, str]) -> bool:
    """按规范坐标和内容摘要承接历史 producer，不绑定工作树绝对根。"""

    if frozen == current:
        return True
    if not isinstance(frozen, dict) or set(frozen) != set(current):
        return False
    for field in ("schema_version", "version"):
        if frozen.get(field) != current[field]:
            return False
    # 旧台账可能来自已删除的 worktree；绝对根是运行坐标，不是 producer
    # 身份。仍要求两端都落在同一个受管相对路径，避免任意文件被冒充。
    if _producer_tool_coordinate(frozen.get("tool")) is None or _producer_tool_coordinate(
        current.get("tool")
    ) is None:
        return False
    frozen_sha256 = frozen.get("tool_sha256")
    if not isinstance(frozen_sha256, str) or not SHA256_RE.fullmatch(frozen_sha256):
        return False
    if frozen_sha256 == current["tool_sha256"]:
        # 内容相同即可证明同一 producer；工作树根目录仍只按下面的
        # 规范相对坐标校验，不把路径变化误判成工具漂移。
        return True
    tool = Path(current["tool"]).resolve()
    repository_root = tool.parents[2]
    if tool != repository_root / PRODUCER_TOOL_RELATIVE:
        return False
    edges = [
        _load_producer_successor_edge(repository_root, descriptor)
        for descriptor in PRODUCER_SUCCESSOR_TRANSITIONS
    ]
    current_edge = _load_current_worktree_successor_edge(repository_root)
    if current_edge is not None:
        edges.append(current_edge)
    edges.extend(
        _load_freeze_successor_edge(repository_root, descriptor)
        for descriptor in PRODUCER_FREEZE_SUCCESSORS
    )
    if not edges:
        return False
    # 每份旧台账都必须沿已登记的摘要边走到当前工具。历史边可以分叉，
    # 但从一个具体前序摘要出发不得出现两条不同后继。
    successors: dict[str, str] = {}
    for before, after in edges:
        if after == before or (before in successors and successors[before] != after):
            return False
        successors[before] = after
    visited: set[str] = set()
    node = frozen_sha256
    while node != current["tool_sha256"]:
        if node in visited or node not in successors:
            return False
        visited.add(node)
        node = successors[node]
    return True


def _budget_ceilings(plan: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """按是否绑定项目总账给出总预算与阶段预算上限。

    未绑定时沿用文档口径（总 360 分钟、阶段各自默认上限）。绑定后 Campaign 账本
    的总预算由总账绝对截止裁剪：上限是从账本开始到绝对截止的整分钟数，阶段预算
    再由 Campaign 计划在总预算内自行规定——VC-2 起的人工核对发生在批次之间，
    不能再按 75 分钟墙钟硬切。框架 §5.3.5 要求连续计时与到期暂停，数值由
    客户端指南或已批准的 Campaign 计划规定。
    """

    binding = plan.get(PROJECT_LEDGER_BINDING_FIELD)
    if binding is None:
        return UNBOUND_TOTAL_BUDGET_CEILING_MINUTES, dict(DEFAULT_STAGE_BUDGETS)
    fields = set(PROJECT_LEDGER_BINDING_FIELDS)
    if isinstance(binding, dict) and PROJECT_LEDGER_FROZEN_DEADLINE_FIELD in binding:
        fields.add(PROJECT_LEDGER_FROZEN_DEADLINE_FIELD)
    _expect(binding, fields, "project_ledger_binding")
    path = binding.get("path")
    if not isinstance(path, str) or not path or not PurePosixPath(path).is_absolute():
        raise TimingLedgerError("project_ledger_binding.path 必须是绝对路径")
    plan_sha256 = binding.get("plan_sha256")
    if not isinstance(plan_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
        raise TimingLedgerError("project_ledger_binding.plan_sha256 非法")
    deadline = _timestamp(binding.get("absolute_deadline_utc"), "project_ledger_binding.absolute_deadline_utc")
    started = _timestamp(plan.get("started_at_utc"), "started_at_utc")
    # R8 审核修正：读侧只用绑定字段，不打开总账（总账迁移、归档或权限变化不影响计划校验）。
    # 创建时已经批准的项目延期由 _project_ledger_binding 冻结进绑定；后续延期不得反过来扩大初始预算。
    if PROJECT_LEDGER_FROZEN_DEADLINE_FIELD in binding:
        frozen = _timestamp(binding[PROJECT_LEDGER_FROZEN_DEADLINE_FIELD], "project_ledger_binding.effective_deadline_at_start_utc")
        if frozen < deadline:
            raise TimingLedgerError("创建时冻结的项目有效截止不得早于总账原绝对截止")
        deadline = frozen
    minutes = int((deadline - started).total_seconds() // 60)
    if minutes < 1:
        raise TimingLedgerError("项目总账绝对截止早于账本开始时间，无法冻结预算")
    return minutes, {phase: minutes for phase in PHASE_ORDER}


def _validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "upgrade_id",
        "created_at_utc",
        "started_at_utc",
        "baseline_version",
        "target_version",
        "campaign_purpose",
        "evidence_decision",
        "total_budget_minutes",
        "stage_budgets_minutes",
        "same_root_cause_retry_limit",
        "producer",
    }
    if isinstance(plan, dict) and PROJECT_LEDGER_BINDING_FIELD in plan:
        # 历史账本没有该字段；有字段时必须闭合校验，不允许悄悄漂移。
        required = required | {PROJECT_LEDGER_BINDING_FIELD}
    _expect(plan, required, "ledger plan")
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise TimingLedgerError("ledger plan schema_version 不匹配")
    _safe_id(plan.get("upgrade_id"), "upgrade_id")
    created = _timestamp(plan.get("created_at_utc"), "created_at_utc")
    started = _timestamp(plan.get("started_at_utc"), "started_at_utc")
    if created != started:
        raise TimingLedgerError("计时必须从台账创建时连续开始")
    for field in ("baseline_version", "target_version"):
        if not isinstance(plan.get(field), str) or not VERSION_RE.fullmatch(plan[field]):
            raise TimingLedgerError(f"{field} 不是三段式版本号")
    if plan["baseline_version"] == plan["target_version"]:
        raise TimingLedgerError("baseline 与 target 版本不得相同")
    if plan.get("campaign_purpose") not in PURPOSES:
        raise TimingLedgerError("campaign_purpose 非法")
    if plan.get("evidence_decision") not in EVIDENCE_DECISIONS:
        raise TimingLedgerError("P0 必须冻结唯一 reuse／recapture 决定")
    total_ceiling, stage_ceilings = _budget_ceilings(plan)
    total = plan.get("total_budget_minutes")
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0 or total > total_ceiling:
        raise TimingLedgerError(f"总墙钟预算必须为 1～{total_ceiling} 分钟")
    budgets = plan.get("stage_budgets_minutes")
    if not isinstance(budgets, dict) or list(budgets) != list(PHASE_ORDER):
        raise TimingLedgerError("阶段预算必须按 VC-0～VC-6 完整排序")
    for phase in PHASE_ORDER:
        value = budgets.get(phase)
        ceiling = min(stage_ceilings[phase], total)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > ceiling:
            raise TimingLedgerError(f"{phase} 预算必须为正数且不得宽于上限 {ceiling}")
    if plan.get("same_root_cause_retry_limit") != DEFAULT_RETRY_LIMIT:
        raise TimingLedgerError("同根因重试上限必须固定为 2")
    if not _producer_identity_matches(plan.get("producer"), _producer()):
        raise TimingLedgerError("计时台账生成器身份漂移")
    return plan


def _load_plan(root: Path) -> tuple[dict[str, Any], bytes]:
    plan, raw = _load_json(root / "ledger.json", "ledger plan")
    return _validate_plan(plan), raw


def _validate_binding(root: Path, value: Any, label: str) -> dict[str, Any]:
    binding = _expect(value, {"role", "path", "sha256"}, label)
    role = _safe_id(binding.get("role"), f"{label}.role")
    path = _relative(root, binding.get("path"), f"{label}.path")
    expected = binding.get("sha256")
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        raise TimingLedgerError(f"{label}.sha256 非法")
    if not path.is_file() or path.is_symlink() or _sha256_file(path) != expected:
        raise TimingLedgerError(f"{label}引用缺失或摘要漂移")
    return {
        "role": role,
        "path": binding["path"],
        "sha256": expected,
        "bytes": path.stat().st_size,
    }


# 写入中的事件临时文件：_write_once 在事件目录内 mkstemp（".<序号>.json.<随机>.tmp"）后原子改名。
# 并发读取方（例如监督器看门狗每周期重算预算）可能在改名前列到它；它不是事件，读取时忽略，
# 写入进程中途被杀遗留的同名临时文件也不会让账本永久不可读。其他任何额外文件仍按篡改拒绝。
INFLIGHT_EVENT_TEMP_RE = re.compile(r"^\.[0-9]{6}\.json\.[A-Za-z0-9_]+\.tmp$")


def _load_events(root: Path, *, limit: int | None = None) -> list[tuple[dict[str, Any], bytes]]:
    events_root = root / "events"
    if not events_root.is_dir() or events_root.is_symlink():
        raise TimingLedgerError("events 目录缺失或不可信")
    paths = sorted(path for path in events_root.iterdir() if not INFLIGHT_EVENT_TEMP_RE.fullmatch(path.name))
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise TimingLedgerError("events 目录只能包含普通文件")
    expected_names = [f"{index:06d}.json" for index in range(1, len(paths) + 1)]
    if [path.name for path in paths] != expected_names:
        raise TimingLedgerError("event 序号不连续或存在额外文件")
    if limit is not None:
        if limit <= 0 or limit > len(paths):
            raise TimingLedgerError("receipt 绑定的 event head 越界")
        paths = paths[:limit]
    return [_load_json(path, f"event {path.name}") for path in paths]


def _validate_event_revision_fields(event: dict[str, Any], sequence: int) -> None:
    """校验改造 2 的四个可选字段；缺失即 None（历史事件），存在则类型与语义闭合。"""

    revision = event.get("revision")
    if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
        raise TimingLedgerError(f"event {sequence}.revision 必须是正整数或 null")
    candidate_id = event.get("candidate_id")
    if candidate_id is not None:
        _safe_id(candidate_id, f"event {sequence}.candidate_id")
    commit = event.get("revision_commit_sha256")
    if commit is not None and (not isinstance(commit, str) or not SHA256_RE.fullmatch(commit)):
        raise TimingLedgerError(f"event {sequence}.revision_commit_sha256 非法")
    supersedes = event.get("supersedes_revision")
    if supersedes is not None and (
        isinstance(supersedes, bool) or not isinstance(supersedes, int) or supersedes < 1
    ):
        raise TimingLedgerError(f"event {sequence}.supersedes_revision 必须是正整数或 null")
    event_type = event.get("event_type")
    phase = event.get("phase")
    if event_type == "stage_review_required" and phase not in {"VC-1", "VC-2", "VC-3"}:
        raise TimingLedgerError("stage_review_required 只用于 VC-1～VC-3")
    # 改造 5：四个评估基线／恢复段字段只属于对应事件；其他事件出现即拒绝。
    baseline = event.get("evaluation_baseline")
    if baseline is not None and (isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 1):
        raise TimingLedgerError(f"event {sequence}.evaluation_baseline 必须是正整数或 null")
    baseline_commit = event.get("baseline_commit_sha256")
    if baseline_commit is not None and (
        not isinstance(baseline_commit, str) or not SHA256_RE.fullmatch(baseline_commit)
    ):
        raise TimingLedgerError(f"event {sequence}.baseline_commit_sha256 非法")
    baseline_kind = event.get("baseline_kind")
    if baseline_kind is not None and baseline_kind not in EVALUATION_BASELINE_KINDS:
        raise TimingLedgerError(f"event {sequence}.baseline_kind 非法")
    recovery_revision = event.get("recovery_revision")
    if recovery_revision is not None and (
        not isinstance(recovery_revision, str) or not RECOVERY_REVISION_RE.fullmatch(recovery_revision)
    ):
        raise TimingLedgerError(f"event {sequence}.recovery_revision 必须是 ar<k> 或 null")
    if event_type == "evaluation_baseline":
        if (
            phase != "VC-5"
            or revision is None
            or candidate_id is None
            or baseline is None
            or baseline_commit is None
            or baseline_kind is None
        ):
            raise TimingLedgerError(
                f"event {sequence} evaluation_baseline 必须在 VC-5 且携带 revision、candidate_id、"
                "evaluation_baseline、baseline_commit_sha256 与 baseline_kind"
            )
        if (baseline_kind == "attempt-recovery") != (recovery_revision is not None):
            raise TimingLedgerError(
                f"event {sequence} evaluation_baseline 只有 attempt-recovery 基线携带 recovery_revision"
            )
        if commit is not None or supersedes is not None:
            raise TimingLedgerError(f"event {sequence} evaluation_baseline 不接受 revision 提交字段")
        return
    if event_type in ATTEMPT_RECOVERY_EVENT_TYPES:
        if phase != "VC-5" or revision is None or candidate_id is None or recovery_revision is None:
            raise TimingLedgerError(
                f"event {sequence} {event_type} 必须在 VC-5 且携带 revision、candidate_id 与 recovery_revision"
            )
        if baseline is not None or baseline_commit is not None or baseline_kind is not None:
            raise TimingLedgerError(f"event {sequence} {event_type} 不接受评估基线字段")
        if commit is not None or supersedes is not None:
            raise TimingLedgerError(f"event {sequence} {event_type} 不接受 revision 提交字段")
        return
    if baseline is not None or baseline_commit is not None or baseline_kind is not None or recovery_revision is not None:
        raise TimingLedgerError(f"event {sequence} {event_type} 不接受评估基线或恢复段字段")
    if event_type == "stage_revision":
        if phase != "VC-4" or revision is None or candidate_id is None or commit is None:
            raise TimingLedgerError(
                f"event {sequence} stage_revision 必须在 VC-4 且携带 revision、candidate_id 与 revision_commit_sha256"
            )
        if supersedes is not None and supersedes >= revision:
            raise TimingLedgerError(f"event {sequence} stage_revision 的 supersedes_revision 必须小于 revision")
    elif event_type in {"candidate_review_required", "candidate_invalidated"}:
        if phase not in CANDIDATE_PHASES or revision is None or candidate_id is None:
            raise TimingLedgerError(
                f"event {sequence} {event_type} 必须在候选级阶段且携带 revision 与 candidate_id"
            )
        if commit is not None or supersedes is not None:
            raise TimingLedgerError(f"event {sequence} {event_type} 不接受 revision 提交字段")
    else:
        if commit is not None or supersedes is not None:
            raise TimingLedgerError(f"event {sequence} {event_type} 不接受 revision 提交字段")
        if revision is not None and (phase not in CANDIDATE_PHASES or event_type not in CANDIDATE_STAGE_EVENT_TYPES):
            raise TimingLedgerError(f"event {sequence} 只有候选级阶段事件才能绑定 revision")
        if candidate_id is not None and event_type not in CANDIDATE_STAGE_EVENT_TYPES:
            raise TimingLedgerError(f"event {sequence} {event_type} 不接受 candidate_id")


def _validate_event_shape(root: Path, event: dict[str, Any], sequence: int) -> dict[str, Any]:
    required = {
        "schema_version",
        "sequence",
        "event_id",
        "recorded_at_utc",
        "phase",
        "event_type",
        "attempt_id",
        "root_cause_id",
        "live_request_count",
        "receipts",
        "next_action",
        "previous_event_sha256",
    }
    if not isinstance(event, dict):
        raise TimingLedgerError(f"event {sequence} 必须是对象")
    extra = set(event) - required - EVENT_REVISION_FIELDS - {"deadline_control"}
    if extra or not required.issubset(event):
        raise TimingLedgerError(f"event {sequence} 字段不闭合")
    _validate_event_revision_fields(event, sequence)
    control = event.get("deadline_control")
    if event.get("event_type") in DEADLINE_CONTROL_EVENTS:
        if not isinstance(control, dict):
            raise TimingLedgerError("预算控制事件缺少对应的批准或暂停事实")
        if event["event_type"] == "deadline_extended":
            artifacts, _project = _deadline_modules()
            try:
                artifacts.validate_deadline_extension(control)
            except artifacts.VCArtifactError as error:
                raise TimingLedgerError(str(error)) from error
        elif event["event_type"] == "deadline_paused":
            if set(control) != {"scopes", "paused_since_utc"} or not control["scopes"] or not set(control["scopes"]) <= {"project", "campaign", "stage"}:
                raise TimingLedgerError("预算暂停层级非法")
            _timestamp(control["paused_since_utc"], "暂停时间")
        elif set(control) != {"approved_by", "approved_at_utc", "reason"} or not all(isinstance(control[key], str) and control[key].strip() for key in control):
            raise TimingLedgerError("显式放弃必须有批准人、时间和理由")
        else:
            _timestamp(control["approved_at_utc"], "放弃批准时间")
        if event.get("live_request_count") != 0 or event.get("attempt_id") is not None or event.get("root_cause_id") is not None:
            raise TimingLedgerError("预算控制事件不得清零、增加请求或裁定根因")
    elif control is not None:
        raise TimingLedgerError("普通事件不能携带预算控制字段")
    if event.get("schema_version") != EVENT_SCHEMA or event.get("sequence") != sequence:
        raise TimingLedgerError(f"event {sequence} schema 或序号不一致")
    _safe_id(event.get("event_id"), f"event {sequence}.event_id")
    _timestamp(event.get("recorded_at_utc"), f"event {sequence}.recorded_at_utc")
    if event.get("phase") not in PHASE_ORDER or event.get("event_type") not in EVENT_TYPES:
        raise TimingLedgerError(f"event {sequence} phase 或 event_type 非法")
    for field in ("attempt_id", "root_cause_id"):
        value = event.get(field)
        if value is not None:
            _safe_id(value, f"event {sequence}.{field}")
    count = event.get("live_request_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise TimingLedgerError(f"event {sequence}.live_request_count 非法")
    receipts = event.get("receipts")
    if not isinstance(receipts, list):
        raise TimingLedgerError(f"event {sequence}.receipts 必须是数组")
    roles = [item.get("role") for item in receipts if isinstance(item, dict)]
    if roles != sorted(roles) or len(set(roles)) != len(roles):
        raise TimingLedgerError(f"event {sequence}.receipts 必须按 role 唯一排序")
    normalized = [_validate_binding(root, item, f"event {sequence}.receipts") for item in receipts]
    if event["event_type"] == "recovery_verified" and tuple(roles) != RECOVERY_ROLES:
        raise TimingLedgerError("recovery_verified 必须绑定工具修复、离线回归和干净 P0 三份收据")
    if (
        event["event_type"] == "recovery_authorized"
        and tuple(roles) != RECOVERY_AUTHORIZATION_ROLES
    ):
        raise TimingLedgerError(
            "recovery_authorized 必须绑定恢复预览与恢复批准两份收据"
        )
    if (
        event["event_type"] not in {"recovery_verified", "recovery_authorized"}
        and roles
        and event["event_type"]
        not in {"receipt_passed", "stop_the_line", "upgrade_completed", "candidate_invalidated"}
    ):
        raise TimingLedgerError(f"{event['event_type']} 不接受 receipts")
    next_action = event.get("next_action")
    if next_action is not None and (not isinstance(next_action, str) or not next_action.strip()):
        raise TimingLedgerError(f"event {sequence}.next_action 非法")
    previous = event.get("previous_event_sha256")
    if sequence == 1:
        if previous is not None:
            raise TimingLedgerError("首个 event 的 previous_event_sha256 必须为空")
    elif not isinstance(previous, str) or not SHA256_RE.fullmatch(previous):
        raise TimingLedgerError(f"event {sequence}.previous_event_sha256 非法")
    return {**event, "receipts": normalized}


def _summarize(
    root: Path,
    plan: dict[str, Any],
    raw_events: list[tuple[dict[str, Any], bytes]],
    *,
    as_of: datetime,
) -> dict[str, Any]:
    if not raw_events:
        raise TimingLedgerError("UpgradeTimingLedger 至少需要一个 event")
    event_ids: set[str] = set()
    attempts: dict[str, dict[str, Any]] = {}
    failure_counts: dict[str, int] = {}
    active_phase: str | None = None
    active_phase_started: datetime | None = None
    active_phase_revision: int | None = None
    stopped = False
    completed = False
    recovery_required = False
    recovery_phase: str | None = None
    recovery_root_cause_id: str | None = None
    # 改造 2：当前 revision 只由最后一条有效 stage_revision 决定；历史账本（无 revision
    # 字段）在第一条候选级事件出现时隐含为 r1。revision_phase_state[r][phase] 记录
    # 该 revision 各候选级阶段的最后状态；phase_elapsed 按 (revision, phase) 累计已关闭
    # 段的耗时，同一 phase 在其他 revision 的耗时计入当前段的阶段预算。
    current_revision: int | None = None
    revision_phase_state: dict[int, dict[str, str]] = {}
    phase_elapsed: dict[tuple[int | None, str], int] = {}
    review_required = False
    stage_review_required = False
    review_phase: str | None = None
    review_root_cause_id: str | None = None
    abandoned_started: datetime | None = None
    revision_required = False
    last_invalidated: tuple[int, str] | None = None
    revision_commits: dict[int, str] = {}
    campaign_completed_phases: list[str] = []
    # 改造 5：当前评估基线只由最后一条 evaluation_baseline 决定，随候选 revision 切换归零；
    # attempt 恢复段以 (attempt_id, ar<k>) 为键记录段状态，段 active 时阶段不得关闭。
    current_evaluation_baseline: dict[str, Any] | None = None
    attempt_recoveries: dict[str, dict[str, Any]] = {}
    total_live_requests = 0
    last_time: datetime | None = None
    previous_raw: bytes | None = None
    last_successful_receipt: dict[str, Any] | None = None
    last_event: dict[str, Any] | None = None
    # 最后一条非预算控制事件：预算暂停、延期与显式放弃只记录预算控制，业务步骤的先后按实质事件判断。
    last_substantive_event: dict[str, Any] | None = None
    deadline_extensions: list[dict[str, Any]] = []
    pause_facts: list[dict[str, Any]] = []
    campaign_extension: datetime | None = None
    stage_extensions: dict[str, float] = {}
    abandoned = False

    def close_active_phase(recorded_at: datetime) -> None:
        nonlocal active_phase, active_phase_started, active_phase_revision
        assert active_phase is not None and active_phase_started is not None
        key = (active_phase_revision, active_phase)
        phase_elapsed[key] = phase_elapsed.get(key, 0) + max(
            0, int((recorded_at - active_phase_started).total_seconds())
        )
        active_phase = None
        active_phase_started = None
        active_phase_revision = None

    for sequence, (event, raw) in enumerate(raw_events, 1):
        normalized = _validate_event_shape(root, event, sequence)
        recorded = _timestamp(normalized["recorded_at_utc"], "event time")
        if recorded < _timestamp(plan["started_at_utc"], "started_at_utc"):
            raise TimingLedgerError("event 早于台账开始时间")
        if last_time is not None and recorded < last_time:
            raise TimingLedgerError("event 时间发生倒退")
        if previous_raw is not None and normalized["previous_event_sha256"] != _sha256_bytes(previous_raw):
            raise TimingLedgerError("event 摘要链断裂")
        if normalized["event_id"] in event_ids:
            raise TimingLedgerError("event_id 重复")
        event_ids.add(normalized["event_id"])
        event_type = normalized["event_type"]
        phase = normalized["phase"]
        candidate_level = phase in CANDIDATE_PHASES and event_type in CANDIDATE_STAGE_EVENT_TYPES
        event_revision = normalized.get("revision")
        if candidate_level:
            if event_revision is None:
                # 历史账本：候选级事件没有 revision 字段，一律归 r1。
                if current_revision is None:
                    current_revision = 1
                event_revision = current_revision
            elif current_revision is None:
                raise TimingLedgerError(
                    f"event {sequence} 候选级阶段事件出现在任何 revision 激活之前"
                )
            elif event_revision != current_revision:
                raise TimingLedgerError(
                    f"event {sequence} 的 revision {event_revision} 不是当前 revision {current_revision}"
                )
        if completed or abandoned:
            raise TimingLedgerError("upgrade_completed 后禁止追加 event")
        if stopped and event_type != "recovery_verified":
            raise TimingLedgerError("stop_the_line 后只能记录 recovery_verified")
        if recovery_required and event_type not in DEADLINE_CONTROL_EVENTS | {
            "attempt_started",
            "attempt_failed",
            # 改造 5 M2：恢复段动作失败进入 recovery_required 后，段对账登记 attempt_recovery_failed。
            "attempt_recovery_failed",
            "receipt_passed",
            "recovery_authorized",
            "stage_abandoned",
            "stop_the_line",
        }:
            raise TimingLedgerError(
                "recovery_required 期间只允许对账或已批准的恢复动作"
            )
        if revision_required and event_type not in REVISION_REQUIRED_ALLOWED_EVENTS | DEADLINE_CONTROL_EVENTS:
            raise TimingLedgerError(
                "revision_required 期间只允许 candidate_invalidated、stage_revision 或 stop_the_line"
            )
        if review_required and event_type not in REVIEW_REQUIRED_ALLOWED_EVENTS | DEADLINE_CONTROL_EVENTS:
            raise TimingLedgerError(
                "candidate_review_required 期间只允许对账、候选作废、stage_abandoned 或 stop_the_line"
            )
        if stage_review_required and event_type not in STAGE_REVIEW_ALLOWED_EVENTS | DEADLINE_CONTROL_EVENTS:
            raise TimingLedgerError("stage_review_required 期间只允许对账、停线或已批准恢复")
        if event_type == "deadline_extended":
            extension = normalized["deadline_control"]
            _artifacts, project = _deadline_modules()
            binding = plan.get(PROJECT_LEDGER_BINDING_FIELD)
            # R8 审核修正：不按收据内嵌的绝对路径直接打开总账，迁移后按计划摘要重新定位。
            project_root = _locate_project_root(
                root,
                recorded_path=str(binding["path"] if isinstance(binding, Mapping) else extension["project_ledger_path"]),
                plan_sha256=binding["plan_sha256"] if isinstance(binding, Mapping) else None,
            )
            project.verify_committed_deadline_extension(extension, root=project_root)
            expected_head = {"sequence": sequence - 1, "sha256": normalized["previous_event_sha256"]}
            if extension["campaign_ledger_head"] != expected_head:
                raise TimingLedgerError("延期批准绑定的 Campaign 账本 head 已过期")
            if extension["approved_at_utc"] != normalized["recorded_at_utc"]:
                raise TimingLedgerError("延期事件批准时间不一致")
            old = _timestamp(extension["original_deadline_at_utc"], "延期原截止")
            new = _timestamp(extension["new_deadline_at_utc"], "延期新截止")
            if extension["scope"] == "campaign":
                previous = campaign_extension or (_timestamp(plan["started_at_utc"], "起点") + timedelta(minutes=plan["total_budget_minutes"]))
                if old != previous:
                    raise TimingLedgerError("Campaign 延期没有承接当前有效截止")
                campaign_extension = new
            elif extension["scope"] == "stage":
                extension_phase = extension["phase"]
                if extension_phase != (active_phase or review_phase):
                    raise TimingLedgerError("阶段延期只能作用于当前阶段")
                anchor = active_phase_started if active_phase else abandoned_started
                carried = sum(seconds for (revision, phase_name), seconds in phase_elapsed.items()
                              if phase_name == extension_phase and active_phase_revision is not None
                              and revision is not None and revision != active_phase_revision)
                if anchor is None or old != anchor + timedelta(minutes=plan["stage_budgets_minutes"][extension_phase],
                        seconds=stage_extensions.get(extension_phase, 0) - carried):
                    raise TimingLedgerError("阶段延期没有承接原起点与累计耗时")
                stage_extensions[extension_phase] = stage_extensions.get(extension_phase, 0) + (new - old).total_seconds()
            deadline_extensions.append(dict(extension))
        elif event_type == "campaign_abandoned":
            abandoned = True
        elif event_type == "deadline_paused":
            # 只追加暂停事实；active、review、recovery 及耗时起点均保留。
            pause_facts.append(normalized)
        elif event_type == "stage_started":
            if active_phase is not None or normalized["attempt_id"] is not None or normalized["root_cause_id"] is not None:
                raise TimingLedgerError("stage_started 身份或阶段状态非法")
            if candidate_level:
                assert event_revision is not None
                if revision_phase_state.get(event_revision, {}).get(phase) == "completed":
                    raise TimingLedgerError(
                        f"revision {event_revision} 的 {phase} 已完成，同一 revision 不得重开阶段"
                    )
                revision_phase_state.setdefault(event_revision, {})[phase] = "started"
            active_phase = phase
            active_phase_started = recorded
            active_phase_revision = event_revision if candidate_level else None
        elif event_type == "stage_completed":
            if (
                active_phase != phase
                or any(item["status"] == "active" for item in attempts.values())
                or any(item["status"] == "active" for item in attempt_recoveries.values())
            ):
                raise TimingLedgerError("stage_completed 与当前阶段或 attempt 状态不一致")
            if candidate_level:
                assert event_revision is not None
                revision_phase_state.setdefault(event_revision, {})[phase] = "completed"
            elif phase not in campaign_completed_phases:
                campaign_completed_phases.append(phase)
            close_active_phase(recorded)
        elif event_type == "stage_abandoned":
            if (
                active_phase != phase
                or any(item["status"] == "active" for item in attempts.values())
                or any(item["status"] == "active" for item in attempt_recoveries.values())
                or normalized["attempt_id"] is not None
                or normalized["root_cause_id"] is None
                or not normalized["next_action"]
            ):
                raise TimingLedgerError(
                    "stage_abandoned 必须关闭当前阶段、登记根因和唯一下一动作"
                )
            if candidate_level:
                assert event_revision is not None
                revision_phase_state.setdefault(event_revision, {})[phase] = "abandoned"
            abandoned_started = active_phase_started
            close_active_phase(recorded)
            recovery_required = False
            recovery_phase = None
            recovery_root_cause_id = None
        elif event_type == "stage_review_required":
            if (
                stage_review_required
                or active_phase is not None
                or last_substantive_event is None
                or last_substantive_event["event_type"] != "stage_abandoned"
                or last_substantive_event["phase"] != phase
                or last_substantive_event["root_cause_id"] != normalized["root_cause_id"]
                or normalized["attempt_id"] is not None
                or normalized["root_cause_id"] is None
                or not normalized["next_action"]
            ):
                raise TimingLedgerError("stage_review_required 必须紧接同阶段同根因的 stage_abandoned")
            stage_review_required = True
            review_phase = phase
            review_root_cause_id = normalized["root_cause_id"]
        elif event_type == "candidate_review_required":
            if (
                active_phase is not None
                or normalized["attempt_id"] is not None
                or normalized["root_cause_id"] is None
                or not normalized["next_action"]
                or any(item["status"] == "active" for item in attempts.values())
            ):
                raise TimingLedgerError(
                    "candidate_review_required 必须在阶段已关闭后登记根因与唯一下一动作"
                )
            review_required = True
        elif event_type == "candidate_invalidated":
            if (
                active_phase is not None
                or normalized["attempt_id"] is not None
                or normalized["root_cause_id"] is None
                or not normalized["next_action"]
                or any(item["status"] == "active" for item in attempts.values())
            ):
                raise TimingLedgerError(
                    "candidate_invalidated 必须在阶段已关闭后登记根因与唯一下一动作"
                )
            assert event_revision is not None
            key = (event_revision, str(normalized["candidate_id"]))
            if revision_required and last_invalidated != key:
                raise TimingLedgerError("revision_required 期间只允许对同一候选幂等重复 candidate_invalidated")
            last_invalidated = key
            revision_required = True
            review_required = False
        elif event_type == "stage_revision":
            revision = int(normalized["revision"])
            supersedes = normalized.get("supersedes_revision")
            # Campaign 级阶段（导入型后继 Campaign 常停在 active VC-0）进行中允许登记 r1；
            # 候选级阶段进行中不得切换 revision。
            if (
                active_phase in CANDIDATE_PHASES
                or normalized["attempt_id"] is not None
                or normalized["root_cause_id"] is not None
            ):
                raise TimingLedgerError("stage_revision 不得在候选级阶段进行中登记")
            if current_revision is None:
                if revision != 1 or supersedes is not None:
                    raise TimingLedgerError("首个 stage_revision 必须是 r1 且不取代任何 revision")
            else:
                if (
                    not revision_required
                    or revision != current_revision + 1
                    or supersedes != current_revision
                    or last_invalidated is None
                    or last_invalidated[0] != current_revision
                    or last_invalidated[1] == normalized["candidate_id"]
                ):
                    raise TimingLedgerError(
                        "stage_revision 必须在 revision_required 下以新候选顺序取代已作废的当前 revision"
                    )
            if revision in revision_commits:
                raise TimingLedgerError(f"revision {revision} 已经激活过")
            revision_commits[revision] = str(normalized["revision_commit_sha256"])
            current_revision = revision
            revision_required = False
            review_required = False
            # 候选 revision 切换后旧候选的全部评估基线只读；新候选从 b0 开始。
            current_evaluation_baseline = None
            attempt_recoveries = {}
        elif event_type == "evaluation_baseline":
            # 只在 VC-5 进行中、无 active attempt／恢复段时切换当前基线，不改变 active_phase。
            baseline = int(normalized["evaluation_baseline"])
            if (
                active_phase != "VC-5"
                or phase != "VC-5"
                or normalized["attempt_id"] is not None
                or normalized["root_cause_id"] is not None
                or any(item["status"] == "active" for item in attempts.values())
                or any(item["status"] == "active" for item in attempt_recoveries.values())
            ):
                raise TimingLedgerError("evaluation_baseline 只能在 VC-5 进行中且无 active attempt／恢复段时登记")
            previous_baseline = (
                int(current_evaluation_baseline["evaluation_baseline"])
                if current_evaluation_baseline is not None
                else 0
            )
            if baseline <= previous_baseline:
                raise TimingLedgerError(
                    f"evaluation_baseline 必须大于当前基线 b{previous_baseline}，收到 b{baseline}"
                )
            current_evaluation_baseline = {
                "evaluation_baseline": baseline,
                "baseline_commit_sha256": str(normalized["baseline_commit_sha256"]),
                "baseline_kind": str(normalized["baseline_kind"]),
                "recovery_revision": normalized.get("recovery_revision"),
                "candidate_id": str(normalized["candidate_id"]),
                "revision": int(event_revision) if event_revision is not None else None,
            }
        elif event_type in ATTEMPT_RECOVERY_EVENT_TYPES:
            attempt_id = normalized["attempt_id"]
            recovery_revision = str(normalized["recovery_revision"])
            if attempt_id is None or phase != "VC-5":
                raise TimingLedgerError(f"{event_type} 必须绑定原 attempt 且在 VC-5")
            key = f"{attempt_id}:{recovery_revision}"
            if event_type == "attempt_recovery_started":
                if (
                    active_phase != phase
                    or attempts.get(attempt_id, {}).get("status") != "completed"
                    or key in attempt_recoveries
                    or any(item["status"] == "active" for item in attempts.values())
                    or any(item["status"] == "active" for item in attempt_recoveries.values())
                    or current_evaluation_baseline is None
                    or current_evaluation_baseline["baseline_kind"] != "attempt-recovery"
                    or not _attempt_recovery_revision_admissible(
                        attempt_id,
                        recovery_revision,
                        frozen_revision=current_evaluation_baseline["recovery_revision"],
                        attempt_recoveries=attempt_recoveries,
                    )
                ):
                    raise TimingLedgerError(
                        "attempt_recovery_started 必须承接已完成的原 attempt、当前 attempt-recovery 基线的恢复段"
                        "（或其失败段的下一个后继段），且同段不得重开"
                    )
                cause = normalized["root_cause_id"]
                if cause is not None and failure_counts.get(cause, 0) >= plan["same_root_cause_retry_limit"]:
                    raise TimingLedgerError("同一根因已连续失败两次，禁止第三次恢复段")
                attempt_recoveries[key] = {
                    "attempt_id": attempt_id,
                    "recovery_revision": recovery_revision,
                    "status": "active",
                    "root_cause_id": cause,
                }
            else:
                if attempt_recoveries.get(key, {}).get("status") != "active":
                    raise TimingLedgerError(f"{event_type} 没有对应的 active 恢复段")
                if event_type == "attempt_recovery_failed":
                    cause = normalized["root_cause_id"]
                    if cause is None:
                        raise TimingLedgerError("attempt_recovery_failed 必须登记 root_cause_id")
                    attempt_recoveries[key]["status"] = "failed"
                    attempt_recoveries[key]["root_cause_id"] = cause
                    failure_counts[cause] = failure_counts.get(cause, 0) + 1
                else:
                    attempt_recoveries[key]["status"] = "completed"
        elif event_type == "attempt_started":
            attempt_id = normalized["attempt_id"]
            if (attempt_id is None or attempt_id in attempts
                    or (active_phase != phase and not (stage_review_required and phase == review_phase == "VC-1"))):
                raise TimingLedgerError("attempt_started 与当前阶段或 attempt 身份不一致")
            cause = normalized["root_cause_id"]
            if cause is not None and failure_counts.get(cause, 0) >= plan["same_root_cause_retry_limit"]:
                raise TimingLedgerError("同一根因已连续失败两次，禁止第三次 attempt")
            attempts[attempt_id] = {"status": "active", "root_cause_id": cause}
        elif event_type in {"attempt_failed", "attempt_completed"}:
            attempt_id = normalized["attempt_id"]
            if attempt_id is None or attempts.get(attempt_id, {}).get("status") != "active":
                raise TimingLedgerError(f"{event_type} 没有对应的 active attempt")
            if event_type == "attempt_failed":
                cause = normalized["root_cause_id"]
                if cause is None:
                    raise TimingLedgerError("attempt_failed 必须登记 root_cause_id")
                attempts[attempt_id] = {"status": "failed", "root_cause_id": cause}
                failure_counts[cause] = failure_counts.get(cause, 0) + 1
            else:
                attempts[attempt_id]["status"] = "completed"
        elif event_type == "recovery_required":
            cause = normalized["root_cause_id"]
            if (
                recovery_required
                or active_phase != phase
                or normalized["attempt_id"] is not None
                or cause is None
                or normalized["receipts"]
                or not normalized["next_action"]
            ):
                raise TimingLedgerError(
                    "recovery_required 必须暂停当前阶段、登记根因和唯一下一动作"
                )
            recovery_required = True
            recovery_phase = phase
            recovery_root_cause_id = cause
        elif event_type == "receipt_passed":
            if stage_review_required:
                roles = tuple(item["role"] for item in normalized["receipts"])
                if (
                    phase != review_phase
                    or normalized["attempt_id"] is not None
                    or normalized["root_cause_id"] is not None
                    or roles != ("provenance", "reconciliation", "stage_replay")
                    or normalized["next_action"] not in {"redispatch-same-sequence", "redispatch-same-batch"}
                    or abandoned_started is None
                ):
                    raise TimingLedgerError("阶段 review 恢复必须绑定同阶段的对账许可")
                reference = next(item for item in normalized["receipts"] if item["role"] == "reconciliation")
                reconciliation, _ = _load_json(root / reference["path"], "阶段恢复对账收据")
                proof = next(item for item in normalized["receipts"] if item["role"] == "stage_replay")
                replay, _ = _load_json(root / proof["path"], "阶段幂等重派证明")
                if (
                    replay.get("schema_version") != "codex-upgrade-stage-replay/v1"
                    or replay.get("decision") != "recoverable"
                    or replay.get("allowed") is not True
                    or replay.get("reconciliation_receipt_sha256") != _sha256_bytes(
                        (json.dumps(reconciliation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
                    )
                    or replay.get("phase") != phase
                    or replay.get("review_root_cause_id") != review_root_cause_id
                    or replay.get("next_action") != normalized["next_action"]
                    or reconciliation.get("reservation_exists") is not False
                ):
                    raise TimingLedgerError("阶段恢复对账未证明动作可幂等重派")
                stage_review_required = False
                review_phase = None
                review_root_cause_id = None
                active_phase = phase
                # review 与恢复都不重置阶段时钟；保留失败前的起点。
                active_phase_started = abandoned_started
            if recovery_required:
                roles = tuple(item["role"] for item in normalized["receipts"])
                if (
                    active_phase != phase
                    or any(item["status"] == "active" for item in attempts.values())
                    or normalized["attempt_id"] is not None
                    or normalized["root_cause_id"] is not None
                    or roles != ("provenance", "reconciliation")
                    or not normalized["next_action"]
                ):
                    raise TimingLedgerError(
                        "reservation 前恢复必须由对账收据恢复当前阶段"
                    )
                recovery_required = False
                recovery_phase = None
                recovery_root_cause_id = None
        elif event_type == "recovery_authorized":
            cause = normalized["root_cause_id"]
            if stage_review_required:
                if (phase != review_phase or phase != "VC-1" or cause != review_root_cause_id
                        or any(item["status"] == "active" for item in attempts.values())
                        or normalized["next_action"] != "resume-rerun-failed"):
                    raise TimingLedgerError("阶段 review 的预约恢复必须完成对账并绑定恢复批准")
                active_phase = phase
                active_phase_started = abandoned_started
                recovery_required = True
                recovery_root_cause_id = review_root_cause_id
                stage_review_required = False
                review_phase = None
                review_root_cause_id = None
            if (
                not recovery_required
                or active_phase != phase
                or any(item["status"] == "active" for item in attempts.values())
                or normalized["attempt_id"] is not None
                or cause != recovery_root_cause_id
                or not normalized["next_action"]
            ):
                raise TimingLedgerError(
                    "reservation 后恢复必须绑定当前暂停根因且 attempt 已完成对账"
                )
            recovery_required = False
            recovery_phase = None
            recovery_root_cause_id = None
        elif event_type == "stop_the_line":
            if not normalized["next_action"]:
                raise TimingLedgerError("stop_the_line 必须冻结唯一下一动作")
            stopped = True
            recovery_required = False
            recovery_phase = None
            recovery_root_cause_id = None
            stage_review_required = False
            review_phase = None
            review_root_cause_id = None
        elif event_type == "recovery_verified":
            cause = normalized["root_cause_id"]
            if not stopped or cause is None or failure_counts.get(cause, 0) < plan["same_root_cause_retry_limit"]:
                raise TimingLedgerError("recovery_verified 没有对应的两次同根因失败停线")
            failure_counts[cause] = 0
            stopped = False
        elif event_type == "upgrade_completed":
            if (
                active_phase != phase
                or any(item["status"] == "active" for item in attempts.values())
                or any(item["status"] == "active" for item in attempt_recoveries.values())
            ):
                raise TimingLedgerError("upgrade_completed 时仍有未关闭阶段或 attempt")
            completed = True
        if normalized["receipts"]:
            last_successful_receipt = normalized["receipts"][-1]
        total_live_requests += normalized["live_request_count"]
        last_time = recorded
        previous_raw = raw
        last_event = normalized
        if event_type not in DEADLINE_CONTROL_EVENTS:
            last_substantive_event = normalized
    assert last_event is not None and last_time is not None and previous_raw is not None
    if as_of < last_time:
        raise TimingLedgerError("检查时间早于最新 event")
    started = _timestamp(plan["started_at_utc"], "started_at_utc")
    total_elapsed = max(0, int((as_of - started).total_seconds()))
    original_total_deadline = started + timedelta(minutes=plan["total_budget_minutes"])
    total_deadline = campaign_extension or original_total_deadline
    stage_elapsed = None
    stage_deadline = None
    if active_phase is not None and active_phase_started is not None:
        # 同一 phase 在其他 revision 已消耗的耗时计入本段：阶段预算跨 revision 累计，
        # 总 deadline 不变。同 revision 内（历史账本）保持按本段起算。
        carried = sum(
            seconds
            for (revision, phase_name), seconds in phase_elapsed.items()
            if phase_name == active_phase
            and active_phase_revision is not None
            and revision is not None
            and revision != active_phase_revision
        )
        stage_elapsed = carried + max(0, int((as_of - active_phase_started).total_seconds()))
        stage_deadline = active_phase_started + timedelta(
            minutes=plan["stage_budgets_minutes"][active_phase]
        ) - timedelta(seconds=carried)
    elif stage_review_required and review_phase is not None and abandoned_started is not None:
        # 阶段 review 仍沿原起点计时；对账等待不能规避阶段预算。
        stage_elapsed = max(0, int((as_of - abandoned_started).total_seconds()))
        stage_deadline = abandoned_started + timedelta(minutes=plan["stage_budgets_minutes"][review_phase])
    if stage_deadline is not None:
        stage_deadline += timedelta(seconds=stage_extensions.get(active_phase or review_phase, 0))
    deadlines = {"campaign": total_deadline}
    if stage_deadline is not None:
        deadlines["stage"] = stage_deadline
    project_binding = plan.get(PROJECT_LEDGER_BINDING_FIELD)
    relevant_extensions = list(deadline_extensions)

    def owns_extension(extension: Mapping[str, Any]) -> bool:
        """R8 审核修正：按批准时绑定的本账本 head 判断延期是否属于本账本，不依赖 upgrade_id 等于 campaign_id。"""

        head = extension.get("campaign_ledger_head")
        sequence = head.get("sequence") if isinstance(head, Mapping) else None
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not 1 <= sequence <= len(raw_events):
            return False
        return _sha256_bytes(raw_events[sequence - 1][1]) == head.get("sha256")

    if isinstance(project_binding, Mapping):
        _artifacts, project = _deadline_modules()
        project_root = _locate_project_root(root, recorded_path=str(project_binding["path"]),
                                            plan_sha256=str(project_binding["plan_sha256"]))
        deadlines["project"] = _timestamp(project.effective_project_deadline(project_root, as_of=as_of), "项目有效截止")
        project_plan, _raw = project._load_plan(project_root)
        project_head = project._replay(project_root, project_plan, project._load_events(project_root), rebuild_cache=False)
        relevant_extensions.extend(row for row in project_head["deadline_extensions"]
                                   if row["scope"] == "project" and _timestamp(row["approved_at_utc"], "批准时间") <= as_of)
        applied = {row["receipt_sha256"] for row in deadline_extensions}
        for extension in project_head["deadline_extensions"]:
            if (owns_extension(extension) or extension["scope"] == "project") and _timestamp(extension["approved_at_utc"], "批准时间") <= as_of:
                if extension["receipt_sha256"] not in applied and not project.deadline_extension_applied(extension, project_head):
                    deadlines["extension_pending"] = _timestamp(extension["approved_at_utc"], "待补齐批准时间")
    expired = sorted(key for key, expiry in deadlines.items() if as_of >= expiry)
    retry_stop_required = any(
        count >= plan["same_root_cause_retry_limit"] for count in failure_counts.values()
    )
    status_before_pause = (
        "complete"
        if completed
        else "abandoned"
        if abandoned
        else "stopped"
        if stopped
        else "stop_required"
        if retry_stop_required
        else "recovery_required"
        if recovery_required
        else "revision_required"
        if revision_required
        else "candidate_review_required"
        if review_required
        else "stage_review_required"
        if stage_review_required
        else "active"
    )
    status = "deadline_paused" if expired and status_before_pause not in {"complete", "abandoned", "stopped", "stop_required"} else status_before_pause
    pause_starts = [deadlines[key] for key in expired]
    for fact in pause_facts:
        remaining = set(fact["deadline_control"]["scopes"])
        for extension in relevant_extensions:
            # 本 Campaign 用事件序号判断前后；另一 Campaign 的项目延期按批准时间
            # 生效。部分延期保留连续暂停起点，所有层解除后才开始新的暂停周期。
            follows = (extension["campaign_ledger_head"]["sequence"] >= fact["sequence"]
                       if owns_extension(extension)
                       else _timestamp(extension["approved_at_utc"], "批准时间") >= _timestamp(fact["recorded_at_utc"], "暂停事件时间"))
            if follows:
                remaining.discard(extension["scope"])
        if remaining.intersection(expired):
            pause_starts.append(_timestamp(fact["deadline_control"]["paused_since_utc"], "连续暂停起点"))
    paused_since = min(pause_starts) if status == "deadline_paused" else None
    paused_hours = max(0.0, (as_of - paused_since).total_seconds() / 3600) if paused_since else 0.0
    review_since = max([paused_since] + [_timestamp(row["approved_at_utc"], "批准时间")
                                      for row in relevant_extensions]) if paused_since else None
    return {
        "status": status,
        "status_before_pause": status_before_pause,
        "paused_scopes": expired if status == "deadline_paused" else [],
        "paused_since_utc": paused_since.isoformat() if paused_since else None,
        "paused_hours": paused_hours,
        "review_reminder": bool(review_since and (as_of - review_since).total_seconds() >= 72 * 3600),
        "original_total_deadline_at_utc": original_total_deadline.isoformat(),
        "deadline_extensions": deadline_extensions,
        "upgrade_id": plan["upgrade_id"],
        "baseline_version": plan["baseline_version"],
        "target_version": plan["target_version"],
        "campaign_purpose": plan["campaign_purpose"],
        "evidence_decision": plan["evidence_decision"],
        "active_phase": active_phase,
        "recovery_phase": recovery_phase,
        "recovery_root_cause_id": recovery_root_cause_id,
        "review_phase": review_phase,
        "review_root_cause_id": review_root_cause_id,
        "current_revision": current_revision,
        "revision_phase_state": {
            str(revision): dict(sorted(states.items()))
            for revision, states in sorted(revision_phase_state.items())
        },
        "campaign_completed_phases": list(campaign_completed_phases),
        "current_evaluation_baseline": (
            dict(current_evaluation_baseline) if current_evaluation_baseline is not None else None
        ),
        "attempt_recoveries": {key: dict(value) for key, value in sorted(attempt_recoveries.items())},
        "head_sequence": len(raw_events),
        "head_sha256": _sha256_bytes(previous_raw),
        "total_elapsed_seconds": total_elapsed,
        "stage_elapsed_seconds": stage_elapsed,
        "total_deadline_at_utc": total_deadline.isoformat(),
        "stage_deadline_at_utc": (
            stage_deadline.isoformat() if stage_deadline else None
        ),
        "total_live_request_count": total_live_requests,
        "same_root_cause_failures": dict(sorted(failure_counts.items())),
        "last_successful_receipt": last_successful_receipt,
        "last_event_id": last_event["event_id"],
        "next_action": last_event["next_action"],
    }


def inspect_ledger(root: Path, *, now: str | None = None, limit: int | None = None) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    plan, _ = _load_plan(root)
    events = _load_events(root, limit=limit)
    observed = _timestamp(now or _utc_now(), "检查时间")
    return _summarize(root, plan, events, as_of=observed)


def create_ledger(
    root: Path,
    *,
    upgrade_id: str,
    baseline_version: str,
    target_version: str,
    campaign_purpose: str,
    evidence_decision: str,
    started_at_utc: str | None = None,
    total_budget_minutes: int = DEFAULT_TOTAL_BUDGET_MINUTES,
    stage_budgets_minutes: dict[str, int] | None = None,
    project_ledger_dir: Path | None = None,
) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=False)
    started = started_at_utc or _utc_now()
    binding = (
        _project_ledger_binding(project_ledger_dir, started_at_utc=started)
        if project_ledger_dir is not None
        else None
    )
    plan = {
        "schema_version": PLAN_SCHEMA,
        "upgrade_id": upgrade_id,
        "created_at_utc": started,
        "started_at_utc": started,
        "baseline_version": baseline_version,
        "target_version": target_version,
        "campaign_purpose": campaign_purpose,
        "evidence_decision": evidence_decision,
        "total_budget_minutes": total_budget_minutes,
        "stage_budgets_minutes": stage_budgets_minutes or dict(DEFAULT_STAGE_BUDGETS),
        "same_root_cause_retry_limit": DEFAULT_RETRY_LIMIT,
        "producer": _producer(),
    }
    if binding is not None:
        plan[PROJECT_LEDGER_BINDING_FIELD] = binding
    _validate_plan(plan)
    root.mkdir(mode=0o700)
    (root / "events").mkdir(mode=0o700)
    (root / "receipts").mkdir(mode=0o700)
    _write_once(root / "ledger.json", plan)
    initial = {
        "schema_version": EVENT_SCHEMA,
        "sequence": 1,
        "event_id": "doc-pre-p0-started",
        "recorded_at_utc": started,
        "phase": "VC-0",
        "event_type": "stage_started",
        "attempt_id": None,
        "root_cause_id": None,
        "live_request_count": 0,
        "receipts": [],
        "next_action": "完成 DOC-PRE／P0 并清零工具阻断",
        "previous_event_sha256": None,
    }
    _validate_event_shape(root, initial, 1)
    _write_once(root / "events" / "000001.json", initial)
    return inspect_ledger(root, now=started)


def _project_ledger_binding(project_ledger_dir: Path, *, started_at_utc: str) -> dict[str, Any]:
    """绑定项目总账 plan：绝对路径、plan 摘要、绝对截止时间，以及账本开始时刻的项目有效截止。

    创建是写路径，这里按总账重放取开始时刻已批准的项目延期并冻结；此后读侧只用这些字段。
    """

    directory = Path(project_ledger_dir)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise TimingLedgerError("项目总账目录必须是存在的非符号链接绝对目录")
    plan_path = directory / "plan.json"
    if plan_path.is_symlink() or not plan_path.is_file():
        raise TimingLedgerError("项目总账缺少 plan.json")
    payload, _raw = _load_json(plan_path, "项目总账 plan")
    if payload.get("schema_version") != "upgrade-project-ledger-plan/v1":
        raise TimingLedgerError("项目总账 plan schema 非法")
    deadline = payload.get("absolute_deadline_utc")
    _timestamp(deadline, "项目总账 absolute_deadline_utc")
    _artifacts, project = _deadline_modules()
    effective = project.effective_project_deadline(
        directory.resolve(strict=True), as_of=_timestamp(started_at_utc, "started_at_utc"),
    )
    return {
        "path": str(directory.resolve(strict=True)),
        "plan_sha256": _sha256_file(plan_path),
        "absolute_deadline_utc": str(deadline),
        PROJECT_LEDGER_FROZEN_DEADLINE_FIELD: str(effective),
    }


def _locate_project_root(ledger_root: Path, *, recorded_path: str, plan_sha256: str | None) -> Path:
    """定位项目总账：先用记录的路径；数据根整体迁移后，从计时账本目录逐级向上查找同名总账目录。

    有计划摘要时以摘要确认是同一总账；收据与绑定里的绝对路径只作提示。找不到时失败关闭，不静默跳过预算层。
    """

    _artifacts, project = _deadline_modules()
    candidates = [Path(recorded_path)]
    candidates.extend(parent / project.LEDGER_DIR_NAME for parent in Path(ledger_root).resolve().parents)
    for candidate in candidates:
        plan_path = candidate / "plan.json"
        try:
            if candidate.is_symlink() or plan_path.is_symlink() or not plan_path.is_file():
                continue
            if plan_sha256 is None or _sha256_file(plan_path) == plan_sha256:
                return candidate
        except OSError:
            continue
    raise TimingLedgerError("项目总账不在记录的路径，且无法在数据根内按计划摘要重新定位（数据根迁移须整体移动）")


def phase_ledger_state(root: Path, *, now: str | None = None) -> dict[str, Any]:
    """在 inspect 摘要之上补充按事件顺序收集的已完成阶段列表。

    VC-2 起的批次派发入口用它决定要补写哪些 stage_completed／stage_started：
    摘要只暴露 active_phase，看不出哪些阶段已经登记完成。
    """

    root = _private_ledger(root, must_exist=True)
    summary = inspect_ledger(root, now=now)
    # 改造 2：已完成阶段 = Campaign 级完成 ∪ 当前 revision 完成的候选级阶段；被作废
    # revision 的完成不算数，新 revision 从 VC-4 重新开始。
    completed = list(summary.get("campaign_completed_phases", []))
    current = summary.get("current_revision")
    if current is not None:
        states = summary.get("revision_phase_state", {}).get(str(current), {})
        for phase in PHASE_ORDER:
            if states.get(phase) == "completed" and phase not in completed:
                completed.append(phase)
    completed.sort(key=PHASE_ORDER.index)
    return {**summary, "completed_phases": completed}


def last_substantive_event(root: Path) -> dict[str, Any] | None:
    """返回最后一条非预算控制事件（预算暂停、延期与显式放弃之外）；没有时返回 None。

    预算控制事件不改变父失败收口等业务步骤的进度，判断收口已写到哪一步时必须跳过它们。
    """

    for event, _raw in reversed(_load_events(Path(root))):
        if event.get("event_type") not in DEADLINE_CONTROL_EVENTS:
            return event
    return None


def append_event(
    root: Path,
    *,
    event_id: str,
    phase: str,
    event_type: str,
    attempt_id: str | None = None,
    root_cause_id: str | None = None,
    live_request_count: int = 0,
    receipts: list[dict[str, str]] | None = None,
    next_action: str | None = None,
    recorded_at_utc: str | None = None,
    revision: int | None = None,
    candidate_id: str | None = None,
    revision_commit_sha256: str | None = None,
    supersedes_revision: int | None = None,
    evaluation_baseline: int | None = None,
    baseline_commit_sha256: str | None = None,
    baseline_kind: str | None = None,
    recovery_revision: str | None = None,
    deadline_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    plan, _ = _load_plan(root)
    raw_events = _load_events(root)
    recorded_raw = recorded_at_utc or _utc_now()
    recorded = _timestamp(recorded_raw, "recorded_at_utc")
    current = _summarize(root, plan, raw_events, as_of=recorded)
    if current["status"] == "deadline_paused" and event_type not in DEADLINE_CONTROL_EVENTS | {
        # 到期后的元数据对账允许关闭死亡进程并记账；不得借此启动、复用或封存。
        "attempt_failed", "attempt_recovery_failed", "receipt_passed", "stage_abandoned", "stop_the_line",
        # R4×R8：stage_abandoned 之后补齐的阶段／候选审核同属元数据收口，不启动、复用或封存任何动作。
        "stage_review_required", "candidate_review_required",
    }:
        raise TimingLedgerError("预算已暂停；批准延期前禁止继续执行或封存")
    if current["status"] == "deadline_paused" and (
        (event_type == "receipt_passed" and not receipts)
        or (event_type in {"stage_abandoned", "stop_the_line"} and root_cause_id is None)
    ):
        raise TimingLedgerError("预算暂停不能冒充普通完成或无根因停线；需要批准延期或显式放弃")
    if (current["status"] == "deadline_paused" and event_type in {"attempt_failed", "attempt_recovery_failed"}
            and (receipts or live_request_count != 0)):
        raise TimingLedgerError("预算暂停只允许元数据失败登记；请求计量必须由后续对账收据承接")
    if phase in CANDIDATE_PHASES and event_type in CANDIDATE_STAGE_EVENT_TYPES:
        # 候选级阶段事件必须绑定 revision：未显式给出时取当前 revision；没有当前
        # revision 的新 Campaign 必须先 revision-open --initial。
        if revision is None:
            revision = current.get("current_revision")
        if revision is None:
            raise TimingLedgerError(
                f"{phase} 的 {event_type} 必须绑定候选 revision；请先执行 revision-open --initial"
            )
    allowed_while_stopping = {"stop_the_line", "recovery_verified"}
    # 根因重试上限已经要求停线时，仍必须先把 active 阶段显式废弃，随后才能
    # 写 stop_the_line；否则父编排器失败会永久留下 active/VC-x 假象。
    if current["status"] == "stop_required":
        allowed_while_stopping.add("stage_abandoned")
        # metadata-only attempt_failed：只登记失败与根因，不带收据、不计请求，
        # 不伴随任何 Job 执行。没有它，stop_required 下的 active attempt 永远关不掉，
        # stage_abandoned 也就永远写不进去。
        if event_type == "attempt_failed" and not receipts and live_request_count == 0:
            allowed_while_stopping.add("attempt_failed")
    if current["status"] in {"stop_required", "stopped"} and event_type not in allowed_while_stopping:
        raise TimingLedgerError("重试或既有停线门禁已关闭，禁止继续追加执行事件")
    if current["status"] == "recovery_required" and event_type not in DEADLINE_CONTROL_EVENTS | {
        "attempt_started",
        "attempt_failed",
        "attempt_recovery_failed",
        "receipt_passed",
        "recovery_authorized",
        "stage_abandoned",
        "stop_the_line",
    }:
        raise TimingLedgerError(
            "recovery_required 期间只允许对账或已批准的恢复动作"
        )
    if current["status"] == "revision_required" and event_type not in REVISION_REQUIRED_ALLOWED_EVENTS | DEADLINE_CONTROL_EVENTS:
        raise TimingLedgerError(
            "revision_required 期间只允许 candidate_invalidated、stage_revision 或 stop_the_line"
        )
    if current["status"] == "candidate_review_required" and event_type not in REVIEW_REQUIRED_ALLOWED_EVENTS | DEADLINE_CONTROL_EVENTS:
        raise TimingLedgerError(
            "candidate_review_required 期间只允许对账、候选作废、stage_abandoned 或 stop_the_line"
        )
    if current["status"] == "stage_review_required" and event_type not in STAGE_REVIEW_ALLOWED_EVENTS | DEADLINE_CONTROL_EVENTS:
        raise TimingLedgerError("stage_review_required 期间只允许对账、停线或已批准恢复")
    sequence = len(raw_events) + 1
    event = {
        "schema_version": EVENT_SCHEMA,
        "sequence": sequence,
        "event_id": event_id,
        "recorded_at_utc": recorded_raw,
        "phase": phase,
        "event_type": event_type,
        "attempt_id": attempt_id,
        "root_cause_id": root_cause_id,
        "live_request_count": live_request_count,
        "receipts": sorted(receipts or [], key=lambda item: item["role"]),
        "next_action": next_action,
        "previous_event_sha256": _sha256_bytes(raw_events[-1][1]),
        "revision": revision,
        "candidate_id": candidate_id,
        "revision_commit_sha256": revision_commit_sha256,
        "supersedes_revision": supersedes_revision,
        "evaluation_baseline": evaluation_baseline,
        "baseline_commit_sha256": baseline_commit_sha256,
        "baseline_kind": baseline_kind,
        "recovery_revision": recovery_revision,
    }
    if deadline_control is not None:
        event["deadline_control"] = dict(deadline_control)
    candidate_raw = _canonical(event)
    _summarize(root, plan, [*raw_events, (event, candidate_raw)], as_of=recorded)
    _write_once(root / "events" / f"{sequence:06d}.json", event)
    return inspect_ledger(root, now=recorded_raw)


def stopped_phase(root: Path, head_sequence: int) -> str | None:
    """返回账本 head 事件若为 stop_the_line 时所记录的阶段。

    A0a-8 的 close-campaign-ledger 先 stage_abandoned 再 stop_the_line，摘要里的
    ``active_phase`` 因而为空；停线事实发生在哪个阶段只能从 stop_the_line 事件自身读取。
    """

    events = _load_events(root)
    if not isinstance(head_sequence, int) or head_sequence < 1 or head_sequence > len(events):
        return None
    event, _raw = events[head_sequence - 1]
    if event.get("event_type") != "stop_the_line":
        return None
    phase = event.get("phase")
    return phase if phase in PHASE_ORDER else None


def build_checkpoint(root: Path, *, observed_at_utc: str | None = None) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    plan, plan_raw = _load_plan(root)
    events = _load_events(root)
    observed = observed_at_utc or _utc_now()
    summary = _summarize(
        root, plan, events, as_of=_timestamp(observed, "observed_at_utc")
    )
    return {
        "schema_version": RECEIPT_SCHEMA,
        "observed_at_utc": observed,
        "ledger_plan": {
            "path": "ledger.json",
            "sha256": _sha256_bytes(plan_raw),
            "bytes": len(plan_raw),
        },
        "event_head": {
            "sequence": summary["head_sequence"],
            "sha256": summary["head_sha256"],
        },
        "summary": summary,
        "producer": _producer(),
    }


def checkpoint(root: Path, output_relative: str) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    output = _relative(root, output_relative, "checkpoint output")
    receipt = build_checkpoint(root)
    _write_once(output, receipt)
    return receipt


def replay(root: Path, receipt_relative: str) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    path = _relative(root, receipt_relative, "checkpoint receipt")
    receipt, raw = _load_json(path, "checkpoint receipt")
    _expect(
        receipt,
        {"schema_version", "observed_at_utc", "ledger_plan", "event_head", "summary", "producer"},
        "checkpoint receipt",
    )
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise TimingLedgerError("checkpoint receipt schema_version 不匹配")
    plan_binding = _expect(receipt.get("ledger_plan"), {"path", "sha256", "bytes"}, "ledger_plan")
    plan_path = _relative(root, plan_binding.get("path"), "ledger_plan.path")
    if (
        plan_path != root / "ledger.json"
        or plan_binding.get("sha256") != _sha256_file(plan_path)
        or plan_binding.get("bytes") != plan_path.stat().st_size
    ):
        raise TimingLedgerError("checkpoint 绑定的 ledger plan 漂移")
    head = _expect(receipt.get("event_head"), {"sequence", "sha256"}, "event_head")
    sequence = head.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise TimingLedgerError("checkpoint event head 非法")
    plan, plan_raw = _load_plan(root)
    events = _load_events(root, limit=sequence)
    summary = _summarize(
        root,
        plan,
        events,
        as_of=_timestamp(receipt.get("observed_at_utc"), "observed_at_utc"),
    )
    expected_summary = dict(summary)
    frozen_summary = receipt.get("summary")
    deadline_fields = ("status_before_pause", "paused_scopes", "paused_since_utc", "paused_hours", "review_reminder",
                       "original_total_deadline_at_utc", "deadline_extensions")
    if (isinstance(frozen_summary, dict) and all(key not in frozen_summary for key in deadline_fields)
            and not any(event["event_type"] in DEADLINE_CONTROL_EVENTS for event, _raw in events)):
        # 历史 checkpoint 的到期状态仍按原 stop_required 回放；不会生成新的旧式停线事件。
        # 旧格式只判断 Campaign／阶段到期，且把展示坐标截到秒；原始计算仍保留微秒。
        legacy_expired = any(summary.get(field) is not None and
            _timestamp(receipt["observed_at_utc"], "历史检查时间") >= _timestamp(summary[field], "历史截止")
            for field in ("total_deadline_at_utc", "stage_deadline_at_utc"))
        expected_summary["status"] = ("stop_required" if legacy_expired and summary["status_before_pause"] not in {"complete", "stopped"}
                                      else summary["status_before_pause"])
        for field in ("total_deadline_at_utc", "stage_deadline_at_utc"):
            if expected_summary[field] is not None:
                expected_summary[field] = _timestamp(expected_summary[field], field).replace(microsecond=0).isoformat()
        for key in deadline_fields:
            expected_summary.pop(key)
    legacy_review_fields = ("review_phase", "review_root_cause_id")
    if (isinstance(frozen_summary, dict) and all(field not in frozen_summary for field in legacy_review_fields)
            and all(expected_summary.get(field) is None for field in legacy_review_fields)):
        for field in legacy_review_fields:
            expected_summary.pop(field)
    legacy_recovery_fields = ("recovery_phase", "recovery_root_cause_id")
    if (
        isinstance(frozen_summary, dict)
        and all(field not in frozen_summary for field in legacy_recovery_fields)
        and all(expected_summary.get(field) is None for field in legacy_recovery_fields)
    ):
        # 早期 v1 checkpoint 在恢复暂停字段加入前已经冻结。只兼容冻结时缺失且
        # 当前重算仍为空的字段；非空恢复状态与已写入字段的新格式继续严格校验。
        for field in legacy_recovery_fields:
            expected_summary.pop(field)
    legacy_revision_fields = ("current_revision", "revision_phase_state", "campaign_completed_phases")
    if (
        isinstance(frozen_summary, dict)
        and all(field not in frozen_summary for field in legacy_revision_fields)
        and expected_summary.get("current_revision") in {None, 1}
        and set(expected_summary.get("revision_phase_state", {})) <= {"1"}
    ):
        # 改造 2 之前冻结的 checkpoint 没有 revision 字段；只兼容重算结果仍是
        # "无 revision 或隐含 r1"的历史语义，出现 r2 及以上的账本必须带新字段。
        for field in legacy_revision_fields:
            expected_summary.pop(field)
    legacy_evaluation_fields = ("current_evaluation_baseline", "attempt_recoveries")
    if (
        isinstance(frozen_summary, dict)
        and all(field not in frozen_summary for field in legacy_evaluation_fields)
        and expected_summary.get("current_evaluation_baseline") is None
        and not expected_summary.get("attempt_recoveries")
    ):
        # 改造 5 之前冻结的 checkpoint 没有评估基线字段；只兼容重算结果仍是
        # "无基线、无恢复段"的历史语义，出现 b≥1 或恢复段的账本必须带新字段。
        for field in legacy_evaluation_fields:
            expected_summary.pop(field)
    expected = {
        "schema_version": RECEIPT_SCHEMA,
        "observed_at_utc": receipt["observed_at_utc"],
        "ledger_plan": {
            "path": "ledger.json",
            "sha256": _sha256_bytes(plan_raw),
            "bytes": len(plan_raw),
        },
        "event_head": {"sequence": sequence, "sha256": summary["head_sha256"]},
        "summary": expected_summary,
        "producer": receipt.get("producer"),
    }
    if (
        not _producer_identity_matches(receipt.get("producer"), _producer())
        or head.get("sha256") != summary["head_sha256"]
        or _canonical(expected) != raw
    ):
        raise TimingLedgerError("UpgradeTimingLedger checkpoint 重放结果不一致")
    return receipt


def assert_usable(
    root: Path,
    receipt_relative: str,
    *,
    baseline_version: str,
    target_version: str,
    campaign_purpose: str,
    required_phase: str | None = None,
) -> dict[str, Any]:
    """重放冻结 checkpoint，并以当前墙钟确认台账仍可继续。"""

    receipt = replay(root, receipt_relative)
    summary = inspect_ledger(root)
    expected = {
        "baseline_version": baseline_version,
        "target_version": target_version,
        "campaign_purpose": campaign_purpose,
    }
    if any(summary.get(key) != value for key, value in expected.items()):
        raise TimingLedgerError("UpgradeTimingLedger 与 Campaign 版本或用途不一致")
    if summary["status"] != "active":
        raise TimingLedgerError(f"UpgradeTimingLedger 当前状态为 {summary['status']}，必须停线")
    if required_phase is not None and summary["active_phase"] != required_phase:
        raise TimingLedgerError(
            f"UpgradeTimingLedger 当前阶段为 {summary['active_phase']}，要求 {required_phase}"
        )
    if receipt["summary"]["status"] != "active":
        raise TimingLedgerError("冻结 checkpoint 在生成时已非 active")
    return summary


def _stage_budget_arguments(values: list[str]) -> dict[str, int] | None:
    """解析 ``VC-N=分钟`` 覆盖项；未覆盖的阶段沿用默认预算。"""

    if not values:
        return None
    budgets = dict(DEFAULT_STAGE_BUDGETS)
    for item in values:
        phase, separator, minutes = str(item).partition("=")
        if not separator or phase not in PHASE_ORDER or not minutes.isdigit() or int(minutes) <= 0:
            raise TimingLedgerError(f"阶段预算参数非法：{item}（应为 VC-N=正整数分钟）")
        budgets[phase] = int(minutes)
    return budgets


def _receipt_arguments(values: list[str]) -> list[dict[str, str]]:
    receipts: list[dict[str, str]] = []
    for value in values:
        role, separator, path = value.partition("=")
        if not separator or not role or not path:
            raise TimingLedgerError("--receipt 必须为 ROLE=RELATIVE_PATH")
        receipts.append({"role": role, "path": path, "sha256": ""})
    return receipts


LEDGER_CLOSE_SCHEMA = "ledger-close/v1"
PROVENANCE_RECEIPT_SCHEMA = "live-request-provenance/v2"
CLOSE_LOCK_NAME = ".vc0-closeout.lock"
DEFAULT_CLOSE_NEXT_ACTION = (
    "账本已按统一计量口径关闭；后续只能由项目总账登记、复用导入或普通后继 Campaign 承接"
)


@contextlib.contextmanager
def _close_lock(root: Path) -> Iterator[None]:
    """与 VC-0 收口共用同一把账本目录锁，串行化关闭与收口。"""

    lock_path = root / CLOSE_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise TimingLedgerError("账本锁文件不可信或无法创建") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise TimingLedgerError("账本锁文件身份不可信")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _active_attempts(raw_events: list[tuple[dict[str, Any], bytes]]) -> list[tuple[str, str]]:
    """按事件顺序重放 attempt 状态，返回仍 active 的 (attempt_id, phase)。"""

    active: dict[str, str] = {}
    for event, _raw in raw_events:
        attempt_id = event.get("attempt_id")
        event_type = event.get("event_type")
        if event_type == "attempt_started" and isinstance(attempt_id, str):
            active[attempt_id] = str(event.get("phase"))
        elif event_type in {"attempt_failed", "attempt_completed"} and isinstance(attempt_id, str):
            active.pop(attempt_id, None)
    return sorted(active.items())


def _load_provenance_receipt(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise TimingLedgerError("provenance 收据必须是可信绝对普通文件")
    payload, raw = _load_json(path, "provenance 收据")
    if payload.get("schema_version") != PROVENANCE_RECEIPT_SCHEMA:
        raise TimingLedgerError("provenance 收据 schema 不是 live-request-provenance/v2")
    # live-request-provenance/v2 的生产者（collect-campaign）把 Formal Campaign ID 写在
    # ``campaign_id``；关闭收据统一记为 ``formal_campaign_id``。两者同时存在时必须一致。
    campaign_id = payload.get("campaign_id")
    formal_campaign_id = payload.get("formal_campaign_id")
    if isinstance(campaign_id, str) and campaign_id:
        if formal_campaign_id not in (None, campaign_id):
            raise TimingLedgerError("provenance 收据 campaign_id 与 formal_campaign_id 不一致")
        payload = {**payload, "formal_campaign_id": campaign_id}
    for field in (
        "formal_campaign_id",
        "status",
        "precise_total",
        "estimated_total",
        "estimation_policy",
        "counting_rule",
        "identity_keys_sha256",
    ):
        if field not in payload:
            raise TimingLedgerError(f"provenance 收据缺少 {field}")
    for field in ("precise_total", "estimated_total"):
        value = payload[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TimingLedgerError(f"provenance 收据 {field} 非法")
    if payload["status"] not in {"complete", "accounting_unresolved"}:
        raise TimingLedgerError("provenance 收据 status 非法")
    return payload, raw


def _publish_once(path: Path, payload: dict[str, Any], label: str) -> None:
    """同一内容重复发布视为幂等；内容不同则失败关闭。"""

    if path.exists():
        existing, _raw = _load_json(path, label)
        if _canonical(existing) != _canonical(payload):
            raise TimingLedgerError(f"{label}已存在且内容不同：{path.name}")
        return
    _write_once(path, payload)


def close_campaign_ledger(
    root: Path,
    *,
    root_cause_id: str,
    provenance_receipt: Path,
    next_action: str | None = None,
    recorded_at_utc: str | None = None,
) -> dict[str, Any]:
    """按统一计量口径关闭一个未停线的 Campaign 计时账本（A0a-8）。

    顺序固定且每步幂等：先给仍 active 的 attempt 追加 metadata-only ``attempt_failed``，
    再 ``stage_abandoned`` 关闭 active 阶段，最后 ``stop_the_line`` 绑定 provenance 收据
    与关闭收据。``stop_the_line`` 的 ``live_request_count`` 只记 ``unaccounted_delta``：
    统一口径重算的精确加估计总数减去账本此前累计；重算结果小于旧累计时记 0 并在
    关闭收据里如实标 ``delta_clamped``，历史混合口径数字不再被改写。
    """

    root = _private_ledger(root, must_exist=True)
    _safe_id(root_cause_id, "root_cause_id")
    action = next_action or DEFAULT_CLOSE_NEXT_ACTION
    provenance, provenance_raw = _load_provenance_receipt(Path(provenance_receipt))
    provenance_sha256 = _sha256_bytes(provenance_raw)
    with _close_lock(root):
        plan, _ = _load_plan(root)
        raw_events = _load_events(root)
        now = recorded_at_utc or _utc_now()
        summary = _summarize(root, plan, raw_events, as_of=_timestamp(now, "recorded_at_utc"))
        if summary["status"] in {"stopped", "complete"}:
            return {"status": "already-closed", "ledger_status": summary["status"], "summary": summary}
        previous_count = int(summary["total_live_request_count"])
        precise = int(provenance["precise_total"])
        estimated = int(provenance["estimated_total"])
        recomputed_total = precise + estimated
        delta = recomputed_total - previous_count
        recorded_delta = max(delta, 0)
        close_root = root / "receipts" / "ledger-close"
        if close_root.is_symlink():
            raise TimingLedgerError("关闭收据目录不可信")
        if not close_root.exists():
            close_root.mkdir(mode=0o700)
        elif stat.S_IMODE(close_root.stat().st_mode) != 0o700:
            raise TimingLedgerError("关闭收据目录权限必须是 0700")
        tag = provenance_sha256[:16]
        provenance_copy = close_root / f"provenance-{tag}.json"
        _publish_once(provenance_copy, provenance, "provenance 收据副本")
        close_receipt = {
            "schema_version": LEDGER_CLOSE_SCHEMA,
            "upgrade_id": plan["upgrade_id"],
            "formal_campaign_id": provenance["formal_campaign_id"],
            "root_cause_id": root_cause_id,
            "closed_at_utc": now,
            "counting_rule": provenance["counting_rule"],
            "estimation_policy": provenance["estimation_policy"],
            "provenance_status": provenance["status"],
            "provenance_receipt_sha256": provenance_sha256,
            "identity_keys_sha256": provenance["identity_keys_sha256"],
            "unresolved_job_ids": list(provenance.get("unresolved_job_ids", [])),
            "previous_count": previous_count,
            "precise_count": precise,
            "estimated_count": estimated,
            "recomputed_total": recomputed_total,
            "unaccounted_delta": delta,
            "delta_clamped": delta < 0,
            "live_request_count_recorded": recorded_delta,
            "resulting_total": previous_count + recorded_delta,
        }
        close_path = close_root / f"ledger-close-{tag}.json"
        if close_path.exists():
            existing, _raw = _load_json(close_path, "关闭收据")
            comparable = {k: v for k, v in existing.items() if k != "closed_at_utc"}
            if comparable != {k: v for k, v in close_receipt.items() if k != "closed_at_utc"}:
                raise TimingLedgerError("关闭收据已存在且账务不同，拒绝覆盖")
        else:
            _write_once(close_path, close_receipt)
        bindings = sorted(
            [
                {
                    "role": "ledger_close",
                    "path": close_path.relative_to(root).as_posix(),
                    "sha256": _sha256_file(close_path),
                },
                {
                    "role": "provenance",
                    "path": provenance_copy.relative_to(root).as_posix(),
                    "sha256": _sha256_file(provenance_copy),
                },
            ],
            key=lambda item: item["role"],
        )
        appended: list[str] = []
        existing_ids = {event["event_id"] for event, _raw in raw_events}
        for attempt_id, phase in _active_attempts(raw_events):
            event_id = f"close-attempt-failed-{attempt_id}"
            if event_id in existing_ids:
                continue
            append_event(
                root,
                event_id=event_id,
                phase=phase,
                event_type="attempt_failed",
                attempt_id=attempt_id,
                root_cause_id=root_cause_id,
                next_action=action,
            )
            appended.append(event_id)
        summary = _summarize(root, plan, _load_events(root), as_of=_timestamp(_utc_now(), "now"))
        active_phase = summary.get("active_phase")
        if active_phase is not None:
            event_id = f"close-stage-abandoned-{active_phase}"
            if event_id not in existing_ids:
                append_event(
                    root,
                    event_id=event_id,
                    phase=str(active_phase),
                    event_type="stage_abandoned",
                    root_cause_id=root_cause_id,
                    next_action=action,
                )
                appended.append(event_id)
        raw_events = _load_events(root)
        summary = _summarize(root, plan, raw_events, as_of=_timestamp(_utc_now(), "now"))
        if summary["status"] != "stopped":
            last_phase = str(raw_events[-1][0]["phase"])
            append_event(
                root,
                event_id="close-stop-the-line",
                phase=last_phase,
                event_type="stop_the_line",
                root_cause_id=root_cause_id,
                live_request_count=recorded_delta,
                receipts=bindings,
                next_action=action,
            )
            appended.append("close-stop-the-line")
        final = inspect_ledger(root)
        if final["status"] != "stopped":
            raise TimingLedgerError("关闭后账本状态不是 stopped")
        return {
            "status": "closed",
            "ledger_status": final["status"],
            "appended_event_ids": appended,
            "ledger_close_receipt": bindings[0],
            "provenance_receipt": bindings[1],
            "previous_count": previous_count,
            "unaccounted_delta": delta,
            "resulting_total": int(final["total_live_request_count"]),
            "summary": final,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create", help="创建只写一次的 UpgradeTimingLedger")
    create_parser.add_argument("--ledger-dir", type=Path, required=True)
    create_parser.add_argument("--upgrade-id", required=True)
    create_parser.add_argument("--baseline-version", required=True)
    create_parser.add_argument("--target-version", required=True)
    create_parser.add_argument("--campaign-purpose", choices=sorted(PURPOSES), required=True)
    create_parser.add_argument("--evidence-decision", choices=sorted(EVIDENCE_DECISIONS), required=True)
    create_parser.add_argument("--total-budget-minutes", type=int, default=DEFAULT_TOTAL_BUDGET_MINUTES)
    create_parser.add_argument(
        "--project-ledger-dir",
        type=Path,
        help="绑定项目总账后，总预算与阶段预算上限改由总账绝对截止裁剪（0.154 起 VC-2～VC-6 含人工核对时使用）",
    )
    create_parser.add_argument(
        "--stage-budget-minutes",
        action="append",
        default=[],
        metavar="PHASE=MINUTES",
        help="覆盖单个阶段预算，可重复；未给出的阶段沿用默认值",
    )
    append_parser = commands.add_parser("append", help="追加一个不可覆盖的计时事件")
    append_parser.add_argument("--ledger-dir", type=Path, required=True)
    append_parser.add_argument("--event-id", required=True)
    append_parser.add_argument("--phase", choices=PHASE_ORDER, required=True)
    append_parser.add_argument("--event-type", choices=sorted(EVENT_TYPES), required=True)
    append_parser.add_argument("--attempt-id")
    append_parser.add_argument("--root-cause-id")
    append_parser.add_argument("--live-request-count", type=int, default=0)
    append_parser.add_argument("--receipt", action="append", default=[])
    append_parser.add_argument("--next-action")
    checkpoint_parser = commands.add_parser("checkpoint", help="封存当前 event head 的可重放 checkpoint")
    checkpoint_parser.add_argument("--ledger-dir", type=Path, required=True)
    checkpoint_parser.add_argument("--output", required=True)
    replay_parser = commands.add_parser("replay", help="独立重放历史 checkpoint")
    replay_parser.add_argument("--ledger-dir", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    status_parser = commands.add_parser("status", help="按当前墙钟只读检查计时状态")
    status_parser.add_argument("--ledger-dir", type=Path, required=True)
    close_parser = commands.add_parser(
        "close-campaign-ledger", help="按统一计量口径关闭未停线的 Campaign 计时账本"
    )
    close_parser.add_argument("--ledger-dir", type=Path, required=True)
    close_parser.add_argument("--root-cause", required=True, help="A0a-3 结构化根因 ID")
    close_parser.add_argument("--provenance-receipt", type=Path, required=True)
    close_parser.add_argument("--next-action")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "create":
            result = create_ledger(
                arguments.ledger_dir,
                upgrade_id=arguments.upgrade_id,
                baseline_version=arguments.baseline_version,
                target_version=arguments.target_version,
                campaign_purpose=arguments.campaign_purpose,
                evidence_decision=arguments.evidence_decision,
                total_budget_minutes=arguments.total_budget_minutes,
                stage_budgets_minutes=_stage_budget_arguments(arguments.stage_budget_minutes),
                project_ledger_dir=arguments.project_ledger_dir,
            )
        elif arguments.command == "append":
            bindings = _receipt_arguments(arguments.receipt)
            root = _private_ledger(arguments.ledger_dir, must_exist=True)
            for binding in bindings:
                path = _relative(root, binding["path"], "receipt")
                if not path.is_file() or path.is_symlink():
                    raise TimingLedgerError(f"receipt 不存在：{binding['path']}")
                binding["sha256"] = _sha256_file(path)
            result = append_event(
                root,
                event_id=arguments.event_id,
                phase=arguments.phase,
                event_type=arguments.event_type,
                attempt_id=arguments.attempt_id,
                root_cause_id=arguments.root_cause_id,
                live_request_count=arguments.live_request_count,
                receipts=bindings,
                next_action=arguments.next_action,
            )
        elif arguments.command == "checkpoint":
            result = checkpoint(arguments.ledger_dir, arguments.output)["summary"]
        elif arguments.command == "replay":
            result = replay(arguments.ledger_dir, arguments.receipt)["summary"]
        elif arguments.command == "close-campaign-ledger":
            result = close_campaign_ledger(
                arguments.ledger_dir,
                root_cause_id=arguments.root_cause,
                provenance_receipt=arguments.provenance_receipt.resolve(),
                next_action=arguments.next_action,
            )
        else:
            result = inspect_ledger(arguments.ledger_dir)
    except (OSError, TimingLedgerError) as error:
        print(f"UpgradeTimingLedger 失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
