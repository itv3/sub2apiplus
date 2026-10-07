"""Claude 评估基线 b<K>（Claude 升级控制面设计 B4-04 编排器侧）：服务面成员、评估器授权与 Store 分层。

评估基线的共享流程（evaluation-recover 的 apply／reevaluate／approval-revision：PREPARED → AUTHORIZATION → COMMIT → 账本
``evaluation_baseline`` 事件）只在 ``upgrade_control_plane`` 有一份；Codex 身份闭包内的读取、路径与校验函数不迁出，由两个
编排器各自实现、合同测试对齐（设计 D7 与附录 E 第 2 类）。本模块就是这些函数的 Claude 实现，``claude_upgrade`` 里的同名
服务面成员只转调。

一、评估基线读取（Codex evidence 闭包内同名函数的 Claude 实现，判据逐条与 Codex 相同）
=======================================================================================
* ``read_evaluation_baseline_commit``／``load_evaluation_baseline_recovery``：读 ``candidates/<cid>/revisions/b<K>/`` 下的
  COMMIT 与 recovery.json，按共享合同校验自摘要与候选、编号身份；
* ``campaign_ledger_summary_for_baseline``：Campaign 绑定的计时账本摘要（Claude 没有控制纪元，只读总计划的
  ``control_receipts.upgrade_timing``）；
* ``orchestrator_current_baseline``：编排器侧当前基线，只由账本最后一条 ``evaluation_baseline`` 事件决定，事件属于别的候选
  （已作废 revision）恒为 b0，有事件时 COMMIT 必须存在且自摘要等于事件绑定值；
* ``current_evaluation_baseline``（服务面 ``_current_evaluation_baseline``）：在编排器侧之上再核 Store 侧（见第三节）；
* ``evaluation_baseline_states``、``evaluation_baseline_has_outputs``、``assertions_root``，以及 b≥1 的阶段读源与写目标
  （``stage_read_source``／``stage_write_target``，按 COMMIT 冻结的 ``stage_sources`` 递归解析，reused 只能指向更小编号）。

二、评估器授权（Codex 评估读侧闭包内同名函数的 Claude 实现）
==========================================================
* ``plan_evaluator_entry_digests``：plan 冻结的工具身份里 checker／builder（Claude 策略 ``evaluator`` 登记的两个受管条目键）
  各有且仅有一个规范 entry 的摘要。plan 时 Claude 评估器尚未建立（B4-03 之前建的 Campaign）即明确拒绝；
* ``b0_evaluator_authorized_digests``：b0 的授权口径——按演进链从新到旧取第一次适用于该候选的迁移（候选登记时没有记录，
  或记录 ``b0_authorization_moved`` 为真），取那次演进 ``evaluator.to``；没有适用的迁移时为 plan 冻结值。Claude 演进收据的
  ``evaluator.to`` 带 ``status``：尚未建立（摘要为 null）的 to 不构成授权，最新适用的迁移就是它时拒绝；
* ``authorized_evaluator_digests``：b≥1 取该基线 recovery.json 冻结的四项，b0 取上一条的两项；
* ``verify_evaluator_digests_authorized``：核对值与授权口径逐项相等，不等即失败关闭。Codex 有一段「旧读侧口径切换」的豁免
  （修好接着跑第 8 项），那是 Codex 身份引擎的历史口径，Claude 只有 v3 引擎一种口径，不需要；
* ``b0_evaluator_authorized_digest_history``（服务面 ``_b0_evaluator_authorized_digest_history``，设计 B4-03 风险③）：b0 授权
  口径的历史，供监督器判定重派时 checker／builder 的变化是否落在授权历史里。共享实现
  （``upgrade_control_plane._b0_evaluator_authorized_digest_history``）把演进 ``evaluator.to`` 钉死为恰好两键，Claude 收据多一个
  ``status``、尚未建立时摘要为 null，交给共享实现必然被判非法——所以 Claude 不转调它，改与上面的 b0 授权共用一条授权链：
  plan 口径（已建立时）在前，再按演进链顺序加入每次适用迁移的 to 值（已建立的去掉 ``status``，尚未建立的不授权、跳过），
  去重保序。不改共享实现，也不改 Codex 读侧；演进链全部已建立时与共享实现逐字相同（合同测试锁定）。

三、Store 分层（设计 B4-04「编排器待接」）
========================================
评估基线有两份登记，必须一致：编排器侧（COMMIT＋账本 ``evaluation_baseline`` 事件）与 Store 侧（``evaluation_baseline_recorded``
事实：同一候选 revision 内最后一条决定当前基线，PAIR／Acceptance v3 与之后写入的全部事实都以它为形成期基线）。顺序是
「编排器先写、Store 后写」，与候选 revision 的两份登记同一做法：

* ``current_evaluation_baseline`` 核两侧一致：Store 当前 revision 属于本候选（或隐含 r1 不限定候选）时，Store 当前基线必须
  等于编排器侧的 ``(K, COMMIT 摘要)``（b0 时 Store 也必须是 b0）；不一致即拒绝，提示以同一 ``evaluation-recover`` 命令重跑
  收敛。Store 当前 revision 属于别的候选时，本候选在 Store 里没有当前基线口径，只看编排器侧；
* ``evaluation_recover``（``claude_upgrade`` 的 ``evaluation-recover`` 子命令走这里）：会写盘的动作（abandon，以及带
  ``--approve-sha256`` 的 apply／reevaluate／approval-revision）先补写收敛——编排器侧已 COMMIT、账本已登记而 Store 还没有
  对应事实时补写它（两步之间被强杀的情形）；再对 apply／reevaluate／approval-revision 做零写入预检：Store 工具身份是当前
  受管代码、Store 当前 revision 属于本候选、本候选在当前 revision 有冻结事实、已有 ValidationAttempt（Store 门禁要求开基线时
  候选已进入 VC-5）、没有交付事实（交付开始后不得再开基线）、两侧当前基线一致。预检不过即拒绝，编排器侧零写入，两份登记
  不会被预检挡出分叉。然后执行共享流程；共享流程返回 ``applied`` 时写 Store 事实：``{baseline, baseline_commit_sha256,
  candidate_id, candidate_ref, supersedes, kind}`` 由 COMMIT 映射（``candidate_ref`` 是本候选在当前 revision 的冻结事实，
  ``supersedes`` 是 Store 同一 revision 内的上一条基线事实），``issued_at`` 取 COMMIT 的 ``committed_at_utc``，幂等键是
  ``claude-upgrade:evaluation-baseline``＋payload：重跑同一命令时 Store 侧复用原事实。写完复核两份登记一致；
* ``result_baseline_fields``：给 VC-5 accept（B4-06）写 PAIR／Acceptance 用——b0 用 v2 合同、不带基线字段；b<K> 用 v3 合同，
  ``evaluation_baseline`` 与 ``baseline_commit_sha256`` 取当前基线（经上面的两侧一致核对）。

四、工具演进的评估器候选侧（``claude_upgrade_evolution`` 调用）
============================================================
``evolution_candidates`` 按 Codex 同一口径扫描 ``candidates/``：有作废记录或被取代标记的候选只读（inactive），其余为活跃候选；
``evaluator_candidate_records`` 给每个活跃候选记录当前评估基线、b0 已有评估产出与 ``b0_authorization_moved``（b0 且还没有
任何评估产出时，b0 授权随本次演进迁到新 checker／builder；已有产出则不迁，须以 evaluation-recover 开新基线重评）。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_tool_identity_policy
from tools.official_client_capture import claude_upgrade_evolution
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.contracts import (
    ACCEPTANCE_V2_SCHEMA,
    ACCEPTANCE_V3_SCHEMA,
    PAIR_V2_SCHEMA,
    PAIR_V3_SCHEMA,
)
from tools.official_client_control.errors import ControlError

CLIENT = "claude"
STORE_SCRATCH_RELATIVE = Path("control") / "store-inputs"
BASELINE_LOGICAL_ACTION = "claude-upgrade:evaluation-baseline"
STORE_BASELINE_COMMAND = "evaluation-baseline-record"
STORE_BASELINE_KIND = "evaluation_baseline_recorded"
EVALUATOR_ENTRY_FIELDS = ("checker_sha256", "builder_sha256")
EVALUATION_BASELINE_DIR_RE = re.compile(r"^b([1-9][0-9]*)$")
# 会开评估基线的动作：带 --approve-sha256 时落盘并 COMMIT；abandon 只作废未 COMMIT 的 PREPARED，不影响 Store。
COMMITTING_ACTIONS = frozenset({"apply", "reevaluate", "approval-revision"})
COMMITTED_STATUS = "applied"
CANDIDATE_ATTEMPTS_DIRNAME = "attempts"


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _claude_scope() -> Any:
    return upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for(CLIENT))


# ---------------------------------------------------------------------------
# 一、评估基线读取
# ---------------------------------------------------------------------------


def read_evaluation_baseline_commit(campaign_dir: Path, candidate_id: str, baseline: int) -> dict[str, Any]:
    """读取并校验 b<K>/COMMIT（自摘要、候选与编号身份）。"""

    orchestrator = _orchestrator()
    commit_path = orchestrator._evaluation_baseline_dir(Path(campaign_dir), candidate_id, baseline) / orchestrator.EVALUATION_BASELINE_COMMIT_FILENAME
    if commit_path.is_symlink() or not commit_path.is_file():
        raise ConfigurationError(f"评估基线 b{baseline} 没有 COMMIT。")
    try:
        commit = vc_artifacts.validate_evaluation_baseline_commit(orchestrator._read_json(commit_path, f"评估基线 b{baseline} COMMIT"))
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"评估基线 b{baseline} COMMIT 无法校验：{error}") from error
    if commit["candidate_id"] != candidate_id or commit["evaluation_baseline"] != baseline:
        raise ConfigurationError(f"评估基线 b{baseline} COMMIT 的候选或编号身份不一致。")
    return commit


def load_evaluation_baseline_recovery(campaign_dir: Path, candidate_id: str, baseline: int) -> dict[str, Any]:
    """读取并校验 b<K>/recovery.json（自摘要、候选与编号身份）。"""

    orchestrator = _orchestrator()
    recovery_path = orchestrator._evaluation_baseline_dir(Path(campaign_dir), candidate_id, baseline) / orchestrator.EVALUATION_BASELINE_RECOVERY_FILENAME
    if recovery_path.is_symlink() or not recovery_path.is_file():
        raise ConfigurationError(f"评估基线 b{baseline} 没有 recovery.json。")
    try:
        recovery = vc_artifacts.validate_evaluation_recovery(orchestrator._read_json(recovery_path, f"评估基线 b{baseline} recovery"))
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"评估基线 b{baseline} recovery 无法校验：{error}") from error
    if recovery["candidate_id"] != candidate_id or recovery["evaluation_baseline"] != baseline:
        raise ConfigurationError(f"评估基线 b{baseline} recovery 的候选或编号身份不一致。")
    return recovery


def campaign_ledger_summary_for_baseline(campaign_dir: Path) -> dict[str, Any] | None:
    """轻量读取 Campaign 绑定的计时账本摘要；没有总计划或没有账本绑定时为 None，账本损坏失败关闭。

    Claude 没有控制纪元（Codex 的 control-epoch 子命令 Claude 明确拒绝），账本绑定只读总计划 ``control_receipts.upgrade_timing``。
    """

    orchestrator = _orchestrator()
    manifest_path = Path(campaign_dir) / "campaign.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    manifest = orchestrator._read_json(manifest_path, "Campaign 核心清单")
    controls = manifest.get("control_receipts")
    timing = controls.get("upgrade_timing") if isinstance(controls, Mapping) else None
    if not isinstance(timing, Mapping) or not isinstance(timing.get("ledger_dir"), str):
        return None
    ledger_dir = orchestrator._campaign_timing_ledger_dir(Path(campaign_dir), manifest)
    try:
        return codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
    except (OSError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise ConfigurationError(f"Campaign 时间账本无法重放：{error}") from error


def orchestrator_current_baseline(campaign_dir: Path, candidate_id: str) -> tuple[int, dict[str, Any] | None]:
    """编排器侧当前评估基线（不核 Store）：只由账本最后一条 ``evaluation_baseline`` 事件决定（与 Codex 同一判据）。

    返回 ``(K, COMMIT | None)``：无事件或事件属于其他候选（已作废 revision）恒为 ``(0, None)``；有事件时 ``b<K>/COMMIT``
    必须存在且自摘要等于事件绑定的 ``baseline_commit_sha256``，否则失败关闭。PREPARED／ABANDON 目录不影响当前基线。
    """

    summary = campaign_ledger_summary_for_baseline(Path(campaign_dir))
    if summary is None:
        return 0, None
    current = summary.get("current_evaluation_baseline")
    if not isinstance(current, Mapping) or current.get("candidate_id") != candidate_id:
        return 0, None
    baseline = int(current["evaluation_baseline"])
    commit = read_evaluation_baseline_commit(Path(campaign_dir), candidate_id, baseline)
    if commit["commit_sha256"] != current.get("baseline_commit_sha256"):
        raise ConfigurationError(f"账本引用的评估基线 b{baseline} COMMIT 摘要与目录内 COMMIT 不一致。")
    return baseline, commit


def current_evaluation_baseline(campaign_dir: Path, candidate_id: str) -> tuple[int, dict[str, Any] | None]:
    """服务面 ``_current_evaluation_baseline``：编排器侧当前基线，并核 Store 侧与之一致（见模块说明第三节）。"""

    baseline, commit = orchestrator_current_baseline(Path(campaign_dir), candidate_id)
    manifest = _read_manifest(Path(campaign_dir))
    store, store_campaign = claude_upgrade_store.open_campaign_store(Path(campaign_dir), manifest)
    _require_store_consistent(store, store_campaign, candidate_id, baseline, commit)
    return baseline, commit


def evaluation_baseline_states(campaign_dir: Path, candidate_id: str) -> dict[int, dict[str, bool]]:
    """扫描 candidates/<cid>/revisions/b*/：各编号的 PREPARED／AUTHORIZATION／COMMIT／ABANDON 存在性（与 Codex 同一口径）。"""

    orchestrator = _orchestrator()
    revisions_root = Path(campaign_dir) / "candidates" / candidate_id / "revisions"
    if revisions_root.is_symlink():
        raise ConfigurationError("候选 revisions 目录不得是符号链接。")
    states: dict[int, dict[str, bool]] = {}
    if not revisions_root.is_dir():
        return states
    for child in sorted(revisions_root.iterdir()):
        match = EVALUATION_BASELINE_DIR_RE.fullmatch(child.name)
        if match is None:
            # r<N>（候选 revision）等其他目录不属于评估基线。
            continue
        if child.is_symlink() or not child.is_dir():
            raise ConfigurationError(f"评估基线目录不可信：{child}")
        states[int(match.group(1))] = {
            name: (child / filename).is_file()
            for name, filename in (
                ("prepared", orchestrator.EVALUATION_BASELINE_PREPARED_FILENAME),
                ("authorization", orchestrator.EVALUATION_BASELINE_AUTHORIZATION_FILENAME),
                ("commit", orchestrator.EVALUATION_BASELINE_COMMIT_FILENAME),
                ("abandon", orchestrator.EVALUATION_BASELINE_ABANDON_FILENAME),
            )
        }
    return states


def evaluation_baseline_has_outputs(campaign_dir: Path, candidate_id: str, baseline: int) -> list[str]:
    """当前评估基线已有的评估产出（Campaign 相对路径）：b0 看断言目录／compare／accept 原路径，b≥1 看本基线各阶段读源。"""

    if baseline == 0:
        with _claude_scope():
            return upgrade_control_plane._candidate_b0_evaluation_outputs(Path(campaign_dir), candidate_id)
    found: list[str] = []
    for stage in ("assertions", "compare", "accept"):
        source = stage_read_source(Path(campaign_dir), candidate_id, baseline, stage)
        if source["status"] == "complete":
            found.append(Path(source["path"]).relative_to(Path(campaign_dir)).as_posix())
    return found


def assertions_root(campaign_dir: Path, candidate_id: str) -> Path:
    """服务面 ``_assertions_root``：当前基线的断言目录（b0：assertions/<cid>/；b≥1：COMMIT 冻结的 local 目标）。"""

    baseline, _commit = current_evaluation_baseline(Path(campaign_dir), candidate_id)
    return _orchestrator()._stage_write_target(Path(campaign_dir), candidate_id, baseline, "assertions")


def stage_read_source(campaign_dir: Path, candidate_id: str, baseline: int, stage: str) -> dict[str, Any]:
    """b≥1 的阶段读来源：按 COMMIT 的 ``stage_sources`` 递归解析，直到 local 或 b0（与 Codex 同一判据）。

    返回 ``{baseline_of_record, path, sha256 | None, status: complete | pending}``；``reused`` 的合法性（更小编号、规范解析
    路径、摘要相等）每次读取都校验。b0 由 ``claude_upgrade_batch.stage_read_source`` 按原路径解析。
    """

    orchestrator = _orchestrator()
    if baseline == 0:
        return orchestrator._stage_read_source(Path(campaign_dir), candidate_id, 0, stage)
    canonical = orchestrator._STAGE_ALIASES.get(stage, stage)
    commit = read_evaluation_baseline_commit(Path(campaign_dir), candidate_id, baseline)
    source = commit["stage_sources"][canonical]
    if source["source"] == "local":
        target = Path(campaign_dir) / source["target"]
        if canonical == "assertions":
            target = target / "results.json"
        exists = target.is_file() and not target.is_symlink()
        return {
            "baseline_of_record": baseline,
            "path": target,
            "sha256": file_sha256(target) if exists else None,
            "status": "complete" if exists else "pending",
        }
    reused_baseline = int(source["baseline"])
    if reused_baseline >= baseline:
        raise ConfigurationError(f"评估基线 b{baseline} 的 {canonical} 只能引用更小编号的基线。")
    resolved = orchestrator._stage_read_source(Path(campaign_dir), candidate_id, reused_baseline, canonical)
    if resolved["path"] != Path(campaign_dir) / source["path"]:
        raise ConfigurationError(f"评估基线 b{baseline} 的 {canonical} reused 路径不是 b{reused_baseline} 的规范解析结果。")
    if resolved["status"] != "complete" or resolved["sha256"] != source["sha256"]:
        raise ConfigurationError(f"评估基线 b{baseline} 的 {canonical} reused 引用的文件缺失或摘要漂移。")
    return resolved


def stage_write_target(campaign_dir: Path, candidate_id: str, baseline: int, stage: str) -> Path:
    """b≥1 的阶段写目标：只有 local 阶段可写（COMMIT 冻结的 ``target``），reused 阶段拒绝写入。"""

    orchestrator = _orchestrator()
    if baseline == 0:
        return orchestrator._stage_write_target(Path(campaign_dir), candidate_id, 0, stage)
    canonical = orchestrator._STAGE_ALIASES.get(stage, stage)
    commit = read_evaluation_baseline_commit(Path(campaign_dir), candidate_id, baseline)
    source = commit["stage_sources"][canonical]
    if source["source"] != "local":
        raise ConfigurationError(f"评估基线 b{baseline} 的 {canonical} 阶段引用前序基线（reused），禁止写入。")
    return Path(campaign_dir) / source["target"]


# ---------------------------------------------------------------------------
# 二、评估器授权
# ---------------------------------------------------------------------------


def _evaluator_entry_keys() -> tuple[str, str]:
    try:
        policy = claude_tool_identity_policy.load_policy()
    except claude_tool_identity_policy.ToolIdentityPolicyError as error:
        raise ConfigurationError(f"Claude 工具身份策略无法加载：{error}") from error
    return str(policy["evaluator"]["checker"]), str(policy["evaluator"]["builder"])


def _plan_evaluator_entries(manifest: Mapping[str, Any]) -> tuple[dict[str, str] | None, dict[str, int]]:
    """plan 工具身份里 checker／builder 的规范 entry 摘要；两项都没有为 ``(None, …)``（plan 时评估器尚未建立）。"""

    identity = manifest.get("tool_identity")
    entries = identity.get("entries") if isinstance(identity, Mapping) else None
    if not isinstance(entries, list):
        raise ConfigurationError("plan 工具身份缺少 entries，无法冻结 b0 evaluator 摘要。")
    digests: dict[str, str] = {}
    counts: dict[str, int] = {}
    for field, key in zip(EVALUATOR_ENTRY_FIELDS, _evaluator_entry_keys()):
        matches = [entry for entry in entries if isinstance(entry, Mapping) and entry.get("path") == key]
        counts[key] = len(matches)
        if len(matches) == 1:
            value = matches[0].get("sha256")
            if not isinstance(value, str) or not _orchestrator().SHA256_RE.fullmatch(value):
                raise ConfigurationError(f"plan 工具身份中 {key} 的摘要非法。")
            digests[field] = value
    if not digests and all(count == 0 for count in counts.values()):
        return None, counts
    if set(digests) != set(EVALUATOR_ENTRY_FIELDS):
        raise ConfigurationError(
            "plan 工具身份中 checker／builder 必须各有且仅有一个规范 entry（实际 "
            + "、".join(f"{key} {count} 个" for key, count in counts.items())
            + "）。"
        )
    return digests, counts


def plan_evaluator_entry_digests(manifest: Mapping[str, Any]) -> dict[str, str]:
    """服务面 ``_plan_evaluator_entry_digests``：plan 冻结的 checker／builder 摘要（b0 评估批次的起点防伪口径）。"""

    digests, _counts = _plan_evaluator_entries(manifest)
    if digests is None:
        raise ConfigurationError(
            "plan 工具身份里没有 Claude 评估器（checker／builder 在 plan 时尚未建立）：b0 的授权口径只能来自登记过的、"
            "建立评估器的工具演进。"
        )
    return digests


def _evolution_evaluator_entry(evolution: Mapping[str, Any]) -> dict[str, str] | None:
    """一条 Claude 演进收据 ``evaluator.to`` 的授权值：已建立时为两项摘要，尚未建立时为 None；形态非法即失败关闭。"""

    label = f"evolution-{int(evolution['index']):02d}"
    evaluator = evolution.get("evaluator")
    target = evaluator.get("to") if isinstance(evaluator, Mapping) else None
    if not isinstance(target, Mapping) or set(target) != {"status", *EVALUATOR_ENTRY_FIELDS}:
        raise ConfigurationError(f"{label} 的评估器 to 记录形态非法。")
    status = target.get("status")
    values = [target.get(field) for field in EVALUATOR_ENTRY_FIELDS]
    if status == claude_upgrade_evolution.EVALUATOR_NOT_ESTABLISHED and all(value is None for value in values):
        return None
    if status == claude_upgrade_evolution.EVALUATOR_ESTABLISHED and all(
        isinstance(value, str) and _orchestrator().SHA256_RE.fullmatch(value) for value in values
    ):
        return {field: str(target[field]) for field in EVALUATOR_ENTRY_FIELDS}
    raise ConfigurationError(f"{label} 的评估器 to 摘要非法。")


def _applicable_evolutions(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> list[tuple[Mapping[str, Any], dict[str, str] | None]]:
    """按演进链顺序列出对该候选 b0 授权适用的迁移：``[(演进收据, to 授权值或 None)]``（适用条件与 Codex 相同）。"""

    orchestrator = _orchestrator()
    frozen = manifest.get("tool_identity")
    if not isinstance(frozen, Mapping) or not orchestrator._is_policy_v2_identity(frozen):
        return []
    applicable: list[tuple[Mapping[str, Any], dict[str, str] | None]] = []
    for evolution in orchestrator._campaign_tool_evolutions(Path(campaign_dir), manifest):
        evaluator = evolution.get("evaluator")
        if not isinstance(evaluator, Mapping) or not isinstance(evaluator.get("candidates"), Mapping):
            raise ConfigurationError(f"evolution-{int(evolution['index']):02d} 的评估器记录形态非法。")
        record = evaluator["candidates"].get(candidate_id)
        if record is None or (isinstance(record, Mapping) and record.get("b0_authorization_moved") is True):
            applicable.append((evolution, _evolution_evaluator_entry(evolution)))
    return applicable


def b0_evaluator_authorized_digests(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> dict[str, str]:
    """b0 的 checker／builder 授权口径：最新一次适用迁移的 to 值；没有适用迁移时为 plan 冻结值（与 Codex 同一判据）。"""

    applicable = _applicable_evolutions(Path(campaign_dir), manifest, candidate_id)
    if applicable:
        evolution, entry = applicable[-1]
        if entry is None:
            raise ConfigurationError(
                f"evolution-{int(evolution['index']):02d} 之后 Claude 评估器尚未建立，候选 {candidate_id} 的 b0 没有授权口径。"
            )
        return entry
    return plan_evaluator_entry_digests(manifest)


def b0_evaluator_authorized_digest_history(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> list[dict[str, str]]:
    """服务面 ``_b0_evaluator_authorized_digest_history``：b0 授权口径的历史（见模块说明第二节，设计 B4-03 风险③）。

    plan 冻结值（plan 时已建立）在前，再按演进链顺序加上每次适用迁移的已建立 to 值；尚未建立的 to 不授权。历史只增不减，
    历史链重放时早先放行的后继依然成立。
    """

    if not isinstance(manifest, Mapping):
        raise ConfigurationError("Campaign 总计划必须是对象。")
    if not isinstance(candidate_id, str) or not _orchestrator().SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("b0 授权历史必须提供合法 candidate-id。")
    plan, _counts = _plan_evaluator_entries(manifest)
    history: list[dict[str, str]] = [plan] if plan is not None else []
    for _evolution, entry in _applicable_evolutions(Path(campaign_dir), manifest, candidate_id):
        if entry is not None and entry not in history:
            history.append(entry)
    if not history:
        raise ConfigurationError(f"候选 {candidate_id} 的 b0 没有任何评估器授权（plan 与已登记演进都没有建立 Claude 评估器）。")
    return history


def authorized_evaluator_digests(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, baseline: int) -> dict[str, str]:
    """服务面 ``_authorized_evaluator_digests``：b≥1 为该基线 recovery.json 冻结的四项，b0 为 b0 授权的两项。"""

    if baseline == 0:
        return b0_evaluator_authorized_digests(Path(campaign_dir), manifest, candidate_id)
    recovery = load_evaluation_baseline_recovery(Path(campaign_dir), candidate_id, baseline)
    return {field: str(recovery["current_evaluator_digests"][field]) for field in vc_artifacts.EVALUATOR_DIGEST_FIELDS}


def verify_evaluator_digests_authorized(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    baseline: int,
    digests: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """服务面 ``_verify_evaluator_digests_authorized``：``digests`` 必须等于基线授权口径，任一项不等即失败关闭。"""

    authorized = authorized_evaluator_digests(Path(campaign_dir), manifest, candidate_id, baseline)
    drift = sorted(field for field, value in authorized.items() if str(digests.get(field)) != value)
    if drift:
        origin = "b0 授权口径（plan 工具身份或已登记演进）" if baseline == 0 else f"b{baseline} recovery.json 授权"
        raise ConfigurationError(
            f"{label}：evaluator 摘要与{origin}不一致（{'、'.join(drift)}）；"
            + ("当前树已偏离 b0 授权口径，b0 评估批次不能再编译或接受。" if baseline == 0 else "先以 evaluation-recover 开新基线或恢复授权时的工具树。")
        )


# ---------------------------------------------------------------------------
# 三、Store 分层
# ---------------------------------------------------------------------------


def _read_manifest(campaign_dir: Path) -> dict[str, Any]:
    manifest_path = Path(campaign_dir) / "campaign.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ConfigurationError("Campaign 缺少总计划 campaign.json。")
    return _orchestrator()._read_json(manifest_path, "Campaign 核心清单")


def _validation_facts(store: Any, campaign: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(store.list_facts(str(campaign["campaign_id"]), "validation"))


def _store_revision(store: Any, campaign: Mapping[str, Any]) -> tuple[int, str | None]:
    try:
        return store_receipts.current_candidate_revision(store, dict(campaign))
    except ControlError as error:
        raise ConfigurationError(f"Store 当前候选 revision 无法确定：{error}") from error


def _store_baseline_facts(store: Any, campaign: Mapping[str, Any], revision: int) -> list[dict[str, Any]]:
    return [
        fact
        for fact in store_receipts.evaluation_baseline_facts(store, dict(campaign))
        if fact["formed_under"]["candidate_revision"] == revision
    ]


def _store_candidate_fact(store: Any, campaign: Mapping[str, Any], candidate_id: str, revision: int) -> dict[str, Any] | None:
    matches = [
        fact
        for fact in _validation_facts(store, campaign)
        if fact["fact_kind"] == "candidate_frozen"
        and fact["payload"].get("candidate_id") == candidate_id
        and (fact.get("formed_under") or {}).get("candidate_revision") == revision
    ]
    return matches[-1] if matches else None


def _require_store_consistent(
    store: Any,
    campaign: Mapping[str, Any],
    candidate_id: str,
    baseline: int,
    commit: Mapping[str, Any] | None,
) -> None:
    """两份登记一致：Store 当前 revision 属于本候选（或隐含 r1）时，Store 当前基线必须等于编排器侧的当前基线。"""

    revision, revision_candidate = _store_revision(store, campaign)
    if revision_candidate is not None and revision_candidate != candidate_id:
        return
    facts = _store_baseline_facts(store, campaign, revision)
    current = facts[-1] if facts else None
    if current is not None and current["payload"]["candidate_id"] != candidate_id:
        # 隐含 r1 下 Store 的基线属于别的候选：本候选在 Store 里没有基线口径，编排器侧也只能是 b0。
        current = None
    store_value = None if current is None else (int(current["payload"]["baseline"]), str(current["payload"]["baseline_commit_sha256"]))
    expected = None if baseline == 0 else (int(baseline), str(commit["commit_sha256"]))  # type: ignore[index]
    if store_value == expected:
        return
    store_label = "b0" if store_value is None else f"b{store_value[0]}（COMMIT {store_value[1][:12]}）"
    expected_label = "b0" if expected is None else f"b{expected[0]}（COMMIT {expected[1][:12]}）"
    raise ConfigurationError(
        f"评估基线两份登记不一致：编排器侧当前是 {expected_label}，Store 当前是 {store_label}。"
        "编排器先写、Store 后写，两步之间被强杀时出现这种状态：以同一 evaluation-recover 命令重跑，先补写 Store 收敛两份登记。"
    )


def store_baseline_target(
    store: Any,
    campaign: Mapping[str, Any],
    candidate_id: str,
    baseline: int,
    commit: Mapping[str, Any],
) -> dict[str, Any]:
    """编排器侧 b<K>／COMMIT 在 Store 侧要写（或已写）的 ``evaluation_baseline_recorded``：``{payload, existing}``。"""

    revision, revision_candidate = _store_revision(store, campaign)
    if revision_candidate is not None and revision_candidate != candidate_id:
        raise ConfigurationError(f"Store 当前候选 revision r{revision} 的候选是 {revision_candidate}，不是 {candidate_id}，不能为它登记评估基线。")
    facts = _store_baseline_facts(store, campaign, revision)
    for index, fact in enumerate(facts):
        if fact["payload"]["baseline_commit_sha256"] != commit["commit_sha256"]:
            continue
        payload = fact["payload"]
        if (int(payload["baseline"]), payload["candidate_id"], payload["kind"]) != (int(baseline), candidate_id, commit["kind"]):
            raise ConfigurationError(f"Store 已有同一 COMMIT 摘要的评估基线事实，但编号、候选或种类与编排器侧 b{baseline} 不一致。")
        if index != len(facts) - 1:
            raise ConfigurationError(f"Store 里评估基线 b{baseline} 之后还有更晚的基线事实，与编排器侧当前基线不一致。")
        return {"payload": dict(payload), "existing": fact}
    candidate_fact = _store_candidate_fact(store, campaign, candidate_id, revision)
    if candidate_fact is None:
        raise ConfigurationError(f"Store 没有候选 {candidate_id} 在 r{revision} 的冻结事实（VC-4 未完成），不能登记评估基线。")
    previous = facts[-1] if facts else None
    if previous is not None and int(previous["payload"]["baseline"]) >= int(baseline):
        raise ConfigurationError(f"Store 当前基线 b{previous['payload']['baseline']} 不小于编排器侧新基线 b{baseline}，两份登记不一致。")
    payload = {
        "baseline": int(baseline),
        "baseline_commit_sha256": str(commit["commit_sha256"]),
        "candidate_id": candidate_id,
        "candidate_ref": dict(store.fact_ref(candidate_fact)),
        "supersedes": dict(store.fact_ref(previous)) if previous is not None else None,
        "kind": str(commit["kind"]),
    }
    return {"payload": payload, "existing": None}


def _store_session(campaign_dir: Path, store: Any, campaign: Mapping[str, Any]) -> Any:
    return claude_upgrade_seal._StoreSession(Path(store.root), str(campaign["campaign_id"]), Path(campaign_dir) / STORE_SCRATCH_RELATIVE)


def converge_store_baseline(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> dict[str, Any] | None:
    """编排器侧当前基线已 COMMIT 而 Store 还没有对应事实时补写（幂等）；返回写入或复用的记录，b0 返回 None。

    编排器侧是 b0 时不写，只核 Store 也是 b0（Store 领先说明登记顺序被破坏，失败关闭）。
    """

    baseline, commit = orchestrator_current_baseline(Path(campaign_dir), candidate_id)
    store, store_campaign = claude_upgrade_store.open_campaign_store(Path(campaign_dir), manifest)
    if baseline == 0:
        _require_store_consistent(store, store_campaign, candidate_id, 0, None)
        return None
    assert commit is not None
    target = store_baseline_target(store, store_campaign, candidate_id, baseline, commit)
    if target["existing"] is not None:
        record = {"fact_ref": dict(store.fact_ref(target["existing"])), "reused": True, "payload": target["payload"]}
    else:
        fact = _store_session(Path(campaign_dir), store, store_campaign).record_fact(
            STORE_BASELINE_COMMAND,
            STORE_BASELINE_KIND,
            target["payload"],
            issued_at=str(commit["committed_at_utc"]),
            logical_action=BASELINE_LOGICAL_ACTION,
        )
        record = {"fact_ref": dict(fact["fact_ref"]), "reused": bool(fact["reused"]), "payload": target["payload"]}
    store, store_campaign = claude_upgrade_store.open_campaign_store(Path(campaign_dir), manifest)
    _require_store_consistent(store, store_campaign, candidate_id, baseline, commit)
    return record


def precheck_store_for_new_baseline(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    """开新评估基线之前的 Store 预检（零写入，见模块说明第三节）；返回 Store 当前 revision 与基线概况。"""

    store, store_campaign = claude_upgrade_store.open_campaign_store(Path(campaign_dir), manifest)
    claude_upgrade_seal._require_store_tool_current(store, store_campaign)
    revision, revision_candidate = _store_revision(store, store_campaign)
    if revision_candidate is not None and revision_candidate != candidate_id:
        raise ConfigurationError(f"Store 当前候选 revision r{revision} 的候选是 {revision_candidate}，不是 {candidate_id}：不能为它开评估基线。")
    if _store_candidate_fact(store, store_campaign, candidate_id, revision) is None:
        raise ConfigurationError(f"Store 没有候选 {candidate_id} 在 r{revision} 的冻结事实（VC-4 未完成），不能开评估基线。")
    if not any(
        fact["fact_kind"] == "validation_attempt_created" and fact["payload"].get("candidate_id") == candidate_id
        for fact in _validation_facts(store, store_campaign)
    ):
        raise ConfigurationError(f"Store 里候选 {candidate_id} 尚未进入 VC-5（没有 ValidationAttempt），没有可重评的评估，不能开评估基线。")
    deliveries = [
        fact
        for fact in store.list_facts(str(store_campaign["campaign_id"]), "deployment")
        if fact["fact_kind"] == "candidate_delivery_recorded"
        and fact["payload"].get("candidate_id") == candidate_id
        and (fact.get("formed_under") or {}).get("candidate_revision") == revision
    ]
    if deliveries:
        raise ConfigurationError(f"候选 {candidate_id} 已开始 VC-6 交付：交付开始后不得再开评估基线。")
    baseline, commit = orchestrator_current_baseline(Path(campaign_dir), candidate_id)
    _require_store_consistent(store, store_campaign, candidate_id, baseline, commit)
    return {"candidate_revision": revision, "current_baseline": baseline}


def evaluation_recover(arguments: argparse.Namespace) -> dict[str, Any]:
    """``evaluation-recover`` 的 Claude 实现：补写收敛 → 零写入预检 → 共享流程 → COMMIT 后写 Store 事实（模块说明第三节）。

    由 ``claude_upgrade._run_shared_subcommand`` 在 Claude 适配器作用域里调用；退出码口径与共享流程相同。
    """

    orchestrator = _orchestrator()
    action = str(getattr(arguments, "recover_action", "") or "")
    candidate_id = str(getattr(arguments, "candidate_id", "") or "")
    approve = getattr(arguments, "approve_sha256", None)
    writes = action == "abandon" or (action in COMMITTING_ACTIONS and approve is not None)
    converged: dict[str, Any] | None = None
    campaign_dir = Path(arguments.campaign_dir)
    if writes and orchestrator.SAFE_ID_RE.fullmatch(candidate_id):
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        with orchestrator._campaign_lock(campaign_dir):
            converged = converge_store_baseline(campaign_dir, manifest, candidate_id)
            if action in COMMITTING_ACTIONS:
                precheck_store_for_new_baseline(campaign_dir, manifest, candidate_id)
    try:
        result = upgrade_control_plane.evaluation_recover(arguments)
    except ConfigurationError as error:
        if converged is not None and not converged["reused"]:
            raise ConfigurationError(
                f"已补写 Store 评估基线 b{converged['payload']['baseline']}（两份登记现已一致）；随后共享流程拒绝：{error}"
            ) from error
        raise
    if action in COMMITTING_ACTIONS and result.get("status") == COMMITTED_STATUS:
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        with orchestrator._campaign_lock(campaign_dir):
            recorded = converge_store_baseline(campaign_dir, manifest, candidate_id)
            if recorded is None:
                raise ConfigurationError("evaluation-recover 返回已提交，但编排器侧没有当前评估基线。")
            current, _commit = current_evaluation_baseline(campaign_dir, candidate_id)
            if current != int(recorded["payload"]["baseline"]):
                raise ConfigurationError(f"evaluation-recover 之后两份登记仍不一致（当前 b{current}）。")
        result["store"] = {
            "evaluation_baseline_fact_ref": recorded["fact_ref"],
            "reused": recorded["reused"],
            "payload": recorded["payload"],
        }
    elif converged is not None:
        result["store_converged"] = {
            "evaluation_baseline_fact_ref": converged["fact_ref"],
            "reused": converged["reused"],
            "payload": converged["payload"],
        }
    return result


def result_baseline_fields(campaign_dir: Path, candidate_id: str) -> dict[str, Any]:
    """VC-5 accept 写 PAIR／Acceptance 时的基线字段（两份登记一致才给）：b0 用 v2 合同、不带基线字段；b<K> 用 v3 合同。"""

    baseline, commit = current_evaluation_baseline(Path(campaign_dir), candidate_id)
    if baseline == 0:
        return {"evaluation_baseline": 0, "pair_schema": PAIR_V2_SCHEMA, "acceptance_schema": ACCEPTANCE_V2_SCHEMA, "payload_fields": {}}
    assert commit is not None
    return {
        "evaluation_baseline": baseline,
        "pair_schema": PAIR_V3_SCHEMA,
        "acceptance_schema": ACCEPTANCE_V3_SCHEMA,
        "payload_fields": {"evaluation_baseline": baseline, "baseline_commit_sha256": str(commit["commit_sha256"])},
    }


# ---------------------------------------------------------------------------
# 四、工具演进的评估器候选侧
# ---------------------------------------------------------------------------


def evolution_candidates(campaign_dir: Path) -> tuple[list[str], list[str]]:
    """``(活跃候选, 只读候选)``：有作废记录或被取代标记的候选只读（与 Codex 同一口径）。"""

    orchestrator = _orchestrator()
    from tools.official_client_capture import claude_upgrade_revision

    active: list[str] = []
    inactive: list[str] = []
    candidates_root = Path(campaign_dir) / "candidates"
    if not candidates_root.is_dir() or candidates_root.is_symlink():
        return active, inactive
    for candidate_root in sorted(candidates_root.iterdir()):
        if not candidate_root.is_dir() or candidate_root.is_symlink():
            continue
        candidate_id = candidate_root.name
        if not orchestrator.SAFE_ID_RE.fullmatch(candidate_id):
            raise ConfigurationError("候选目录包含非法 candidate-id。")
        if any(
            (candidate_root / marker).exists() or (candidate_root / marker).is_symlink()
            for marker in (orchestrator.CANDIDATE_INVALIDATION_FILENAME, claude_upgrade_revision.SUPERSEDED_MARKER_FILENAME)
        ):
            inactive.append(candidate_id)
        else:
            active.append(candidate_id)
    return active, inactive


def candidate_has_capture_attempts(campaign_dir: Path, candidate_id: str) -> bool:
    """候选是否已有取证 attempt（``candidates/<cid>/attempts/`` 下有任何条目）。"""

    root = Path(campaign_dir) / "candidates" / candidate_id / CANDIDATE_ATTEMPTS_DIRNAME
    if root.is_symlink():
        raise ConfigurationError(f"候选 attempt 目录不得是符号链接：{root}")
    return root.is_dir() and any(root.iterdir())


def evaluator_candidate_records(campaign_dir: Path, candidates: list[str]) -> dict[str, Any]:
    """活跃候选的评估器授权迁移记录：当前基线、b0 已有评估产出与 ``b0_authorization_moved``（与 Codex 同一口径）。"""

    records: dict[str, Any] = {}
    for candidate_id in candidates:
        baseline, _commit = current_evaluation_baseline(Path(campaign_dir), candidate_id)
        with _claude_scope():
            outputs = upgrade_control_plane._candidate_b0_evaluation_outputs(Path(campaign_dir), candidate_id)
        records[candidate_id] = {
            "current_evaluation_baseline": baseline,
            "b0_evaluation_outputs": outputs,
            "b0_authorization_moved": baseline == 0 and not outputs,
        }
    return records
