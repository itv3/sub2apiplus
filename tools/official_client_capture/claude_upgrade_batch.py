"""Claude 原子批次入口（Claude 升级控制面设计 B3-01）：VC-2～VC-6 经 ``compile-and-run-vc-batch`` 派发时 Claude 侧的成员。

共享流程只在 ``upgrade_control_plane.compile_and_run_vc_batch`` 有一份：三层治理（工具演进登记、项目总账准入、计时账本
只读预检）→ 孤儿对账 → staging 编译（``control/vc/staging/NNNN-vc-x/attempt-K``）→ prepared 父 run → COMMIT 四步
（nonce、evaluator 摘要、账本阶段事件、发布、COMMIT、激活）→ 动作执行 → 阶段完成登记。批次身份 12 字段与动作三元组
（``action_id``／``operation``／``item_ids``）、N+1 重派协议、阶段审核与对账全部沿用共享实现；Claude 只经编排器服务面
提供下面这些语义属于自己的成员（``claude_upgrade`` 注册并转调）。VC 控制制品（总计划、checkpoint、批次 → 队列清单、
VC-0 交接链）在 ``claude_upgrade_vc``。

一、阶段结果（服务面 ``_load_stage_result``）
============================================
* ``capture-official``：B2-07 的 ``official/result.json``（``claude_upgrade_seal.load_stage_result``，B2-08 起用）。
* ``classify``：B3-02 的 ``classification/result.json``（``claude_upgrade_classify.load_stage_result``），VC-2 checkpoint
  必须已经绑定这份结果；阶段结果写出、checkpoint 尚未写出的半成品按「尚未封存」处理（重跑原批准命令收敛）。
* ``stage-profile``（别名 ``profile``）：B3-03 的 ``profile/result.json``（``claude_upgrade_profile.load_stage_result``），
  同样要求 VC-3 checkpoint 已绑定。Codex 的 VC-3 没有阶段结果，``_stage_path`` 照搬 Codex 不认识这个阶段，所以在这里
  先于 ``_stage_path`` 分流；``prepare-profile`` 的准备结果不是阶段结果，读侧是 ``claude_upgrade_profile.load_prepare_result``。
* 候选级三阶段报待实现，写明工作项（B4-02／B4-03／B4-06）。

二、评估阶段路径（服务面 ``_stage_read_source``／``_stage_write_target``）
=====================================================================
b0（没有评估基线）按原路径解析，与 Codex 同一坐标；b≥1 的 ``stage_sources`` 递归解析依赖评估基线 COMMIT，随 B4-04 落实。

三、阶段幂等重派证明（服务面 ``_campaign_stage_replay_facts``）
==========================================================
阶段动作失败、账本进入 ``stage_review_required`` 后，``reconcile-supervisor-run`` 调这里判定能否按原批次 N+1 重派；
许可写进 ``codex-upgrade-stage-replay/v1`` 证明，监督器派发直接后继前再算一次、与证明逐字相同才放行。只读、不执行命令、
不补写产物。合同：

* 只认受管 Python 对 ``claude_upgrade.py`` 的直接调用（脚本路径或 ``-m tools.official_client_capture.claude_upgrade``），
  参数全是成对的旗标与值、按模式精确闭合，``--campaign-dir`` 必须是本 Campaign。
* VC-2：``classify`` 的草案（``--candidates``）、修订（``--from-revision``＋``--revision-input``）、批准
  （``--approve-revision``＋``--approve-review-sha256``）。VC-3：``prepare-profile`` 的草案（可带成对的 Active 输入）、修订、
  批准（另带 ``--approved-by``），``stage-profile`` 的预览与批准（``--approve-joint-sha256``＋``--approved-by``）。
* 这些 producer 的落盘全部是只写一次或逐字复用、Store 事实带幂等键（动作结果 WAL 第 ① 步），任一步被强杀后重跑同一命令
  收敛；证明只排除「重派必然被拒」的情形，失败关闭、留在审核：阶段已批准后的草案／修订／预览，批准了另一 revision、另一
  批准人或另一联合摘要，批准目录内容与被批准 revision 不一致，被修订或被批准的 revision 已被改动，没有冻结该联合摘要的
  暂存预览。Store 里已有同类事实而阶段结果未写时，是否同一次逻辑写入由命令自身按幂等键核对（不同即拒绝覆盖），证明不重复
  它的投影计算。
* ``inputs`` 绑定文件旗标的摘要、修订输入目录的树摘要、被批准 revision 的草案收据与被批准联合清单；``outputs`` 绑定阶段
  产物目录（``classification/``、``profile/``）每个普通文件的摘要、本阶段 Store 事实的摘要与已封存 checkpoint（键
  ``checkpoint``，``_require_stage_replay_proof_for_checkpoint`` 按它核对）。
* VC-4（B3-04）：``plan-candidate-gates`` 的生成计划形态（``--campaign-dir``、``--candidate-id``、``--candidate-source``、
  ``--mapping``、``--output``）与 ``record-candidate-build`` 的十九个旗标（与 Codex 同名同义，见 ``claude_upgrade_candidate``
  第四节），旗标闭集取共享 ``CANDIDATE_STAGE_REPLAY_FLAGS`` 的必需部分（Claude 这两个子命令不接受 watchdog 参数，没有
  可选旗标；需求预览与 ``--preview`` 都不是批次动作，没有合同）。判定与绑定交给共享 ``_candidate_stage_replay_action_facts``
  （候选属于当前 revision、未作废未被取代、没有候选 attempt；计划输出已存在即拒绝；构建输入文件与目录的摘要、已写的构建
  收据、build-evidence 与 revision seal 的摘要，已写构建收据的候选与构建标识核对；VC-4 checkpoint 重放后绑定），Claude 在
  其上补 Store 侧：Store 已冻结本候选的另一次构建（``build_id`` 或镜像摘要不同）即拒绝（逐字重派必然撞上 Store 的冻结），
  本候选的 ``candidate_frozen`` 事实与 Store CandidateBuildReceipt 引用进 ``outputs``。
* VC-1：Claude 没有阶段幂等重派合同（采集与已发布预约只走 ``reconcile-attempt``；seal 链零请求失败按 post-run-tooling
  对账后由 N+1 重派协议承接）；VC-5、VC-6 随 B4。

四、父监督器上下文的退出码（``campaign_run_aware_exit_code``）
=========================================================
直接 CLI 里草案、修订与预览返回诊断码 2（等审核）；在父批次里它们是该批次的预期终点，退出 0，否则父监督器会把动作记成
失败、把账本推进阶段审核。合法暂停点：``awaiting_seal_approval``／``seal_blocked``（seal 预览）、
``awaiting_classification_approval``／``classification_blocked``（分类草案与修订）、``awaiting_profile_preparation_approval``／
``profile_blocked``（画像准备草案与修订）、``awaiting_profile_approval``（暂存预览）。机器草案按设计全部未决、门禁必然
blocked，所以 blocked 也是暂停点；批准与封存只有 ``complete`` 才算完成（它们本来就退出 0）。与 Codex
``_campaign_run_aware_exit_code`` 同一口径（Codex 的暂停状态名不同）。采集 ``awaiting_receipts`` 由 B2-06 的 run 自己映射。

五、批次控制面命令不进动作队列（``reject_control_plane_in_campaign_run``）
=====================================================================
``compile-and-run-vc-batch``、``revision-open``、``invalidate-candidate``、评估恢复、延期、放弃、隔离、工具演进等是两个
批次之间的控制面命令，在父监督器上下文里执行即拒绝（与 Codex ``_reject_unparented_formal_write`` 同一口径）；对账、
导入、冻结、环境清理等已由 Claude 适配器的禁止子命令表在预声明时拒绝。

六、受管动作与动作计划（``managed_action``／``action_plan``）
=========================================================
驱动与测试生成批次动作的统一写法：命令是「受管 Python + ``claude_upgrade.py`` 绝对路径 + 子命令参数」，正是阶段幂等重派
证明认可的形态；动作计划按 ``codex-upgrade-vc-action-plan/v1`` 校验，动作按 ``action_id`` 排序（批次按这个顺序执行）。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_control.store import ControlStore

ORCHESTRATOR_SCRIPT = Path(__file__).resolve().with_name("claude_upgrade.py")
ORCHESTRATOR_MODULE = "tools.official_client_capture.claude_upgrade"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_ACTION_TIMEOUT_SECONDS = 900

# 一、阶段结果
PROFILE_STAGE_NAMES = frozenset({"stage-profile", "profile"})
CANDIDATE_STAGE_WORK_ITEMS: Mapping[str, str] = {
    "capture-candidate": "B4-02（候选证据四阶段封存）",
    "compare": "B4-03（Claude 评估器）",
    "accept": "B4-06（accept 门禁 Claude 版）",
}
STAGE_PHASES: Mapping[str, str] = {"capture-official": "VC-1", "classify": "VC-2", "stage-profile": "VC-3"}

# 三、阶段幂等重派证明：阶段 → 子命令 → 模式 → (必备旗标, 可选旗标)。可选旗标成对出现（见 ``_PAIRED_OPTIONAL``）。
STAGE_REPLAY_CONTRACTS: Mapping[str, Mapping[str, Mapping[str, tuple[frozenset[str], frozenset[str]]]]] = {
    "VC-2": {
        "classify": {
            "draft": (frozenset({"--campaign-dir", "--candidates"}), frozenset()),
            "revise": (frozenset({"--campaign-dir", "--from-revision", "--revision-input"}), frozenset()),
            "approve": (frozenset({"--campaign-dir", "--approve-revision", "--approve-review-sha256"}), frozenset()),
        },
    },
    "VC-3": {
        "prepare-profile": {
            "draft": (frozenset({"--campaign-dir"}), frozenset({"--active-release-catalog", "--active-profile"})),
            "revise": (frozenset({"--campaign-dir", "--from-revision", "--revision-input"}), frozenset()),
            "approve": (
                frozenset({"--campaign-dir", "--approve-revision", "--approve-review-sha256", "--approved-by"}),
                frozenset(),
            ),
        },
        "stage-profile": {
            "preview": (frozenset({"--campaign-dir"}), frozenset()),
            "approve": (frozenset({"--campaign-dir", "--approve-joint-sha256", "--approved-by"}), frozenset()),
        },
    },
    # VC-4：两个子命令各只有一种形态；必需旗标与共享闭集相同，Claude 不接受 watchdog 参数（见模块说明第三节）。
    "VC-4": {
        "plan-candidate-gates": {
            "plan": (upgrade_control_plane.CANDIDATE_STAGE_REPLAY_FLAGS["plan-candidate-gates"][0], frozenset()),
        },
        "record-candidate-build": {
            "record": (upgrade_control_plane.CANDIDATE_STAGE_REPLAY_FLAGS["record-candidate-build"][0], frozenset()),
        },
    },
}
_PAIRED_OPTIONAL = frozenset({"--active-release-catalog", "--active-profile"})
# 阶段产物目录与本阶段的 Store 事实种类（进 ``outputs``）。
STAGE_REPLAY_OUTPUTS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "VC-2": ("classification", ("rule_classification_recorded",)),
    "VC-3": ("profile", ("evidence_approved", "profile_approved")),
}
VC1_REPLAY_REASON = (
    "Claude VC-1 没有阶段幂等重派合同：采集与已发布预约只走 reconcile-attempt；"
    "seal 链零请求失败按 post-run-tooling 对账后由 N+1 重派协议承接"
)

# 四、父监督器上下文里的合法暂停点。
CAMPAIGN_RUN_PAUSE_STATUSES = frozenset(
    {
        "awaiting_seal_approval",
        "seal_blocked",
        "awaiting_classification_approval",
        "classification_blocked",
        "awaiting_profile_preparation_approval",
        "profile_blocked",
        "awaiting_profile_approval",
    }
)

# 五、批次控制面命令（共享流程子命令与工具演进登记）。
CONTROL_PLANE_COMMANDS = frozenset(
    {
        "compile-and-run-vc-batch",
        "revision-open",
        "invalidate-candidate",
        "evaluation-recover",
        "evaluation-epoch",
        "tool-evolution-status",
        "deadline-extend",
        "request-budget-extend",
        "campaign-resume",
        "accounting-resolve",
        "environment-isolate",
        "evidence-conflict-quarantine",
        "campaign-abandon",
        "tool-evolution",
    }
)


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _classify() -> Any:
    from tools.official_client_capture import claude_upgrade_classify

    return claude_upgrade_classify


def _profile() -> Any:
    from tools.official_client_capture import claude_upgrade_profile

    return claude_upgrade_profile


def _read_json(path: Path, label: str) -> dict[str, Any]:
    return _orchestrator()._read_json(path, label)


# ---------------------------------------------------------------------------
# 一、阶段结果
# ---------------------------------------------------------------------------


def load_stage_result(
    campaign_dir: Path,
    stage: str,
    candidate_id: str | None = None,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """服务面 ``_load_stage_result`` 的 Claude 实现：按阶段转给各自的 producer 读侧（见模块说明第一节）。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    canonical = "stage-profile" if stage in PROFILE_STAGE_NAMES else orchestrator._STAGE_ALIASES.get(stage, stage)
    if canonical in CANDIDATE_STAGE_WORK_ITEMS:
        raise orchestrator._pending(f"_load_stage_result（{canonical}）", CANDIDATE_STAGE_WORK_ITEMS[canonical])
    if canonical == "stage-profile":
        path = campaign_dir / _profile().STAGE_RESULT_RELATIVE
    else:
        canonical, path = orchestrator._stage_path(campaign_dir, canonical, candidate_id)
    if canonical == "capture-official":
        from tools.official_client_capture import claude_upgrade_seal

        return claude_upgrade_seal.load_stage_result(campaign_dir, path, manifest=manifest)
    if candidate_id is not None:
        raise ConfigurationError(f"{canonical} 是 Campaign 级阶段，不接受 candidate-id。")
    current = orchestrator._require_formal_campaign(campaign_dir, dict(manifest) if manifest is not None else None)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError(f"{canonical} 阶段结果路径不可信。")
    if not path.exists():
        raise ConfigurationError(f"阶段尚未封存：{canonical}（{path.relative_to(campaign_dir).as_posix()} 不存在）")
    payload = (
        _classify().load_stage_result(campaign_dir, current)
        if canonical == "classify"
        else _profile().load_stage_result(campaign_dir, current)
    )
    phase = STAGE_PHASES[canonical]
    checkpoint_path = claude_upgrade_vc.vc_checkpoint_path(campaign_dir, phase)
    if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
        raise ConfigurationError(
            f"阶段尚未封存完整：{canonical} 阶段结果已写出、{phase} checkpoint 尚未写出（重跑原批准命令收敛）。"
        )
    receipt = _read_json(checkpoint_path, f"{phase} checkpoint").get("stage_receipt")
    expected = {"path": path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(path)}
    if not isinstance(receipt, Mapping) or dict(receipt) != expected:
        raise ConfigurationError(f"{phase} checkpoint 没有绑定这份 {canonical} 阶段结果。")
    return payload


# ---------------------------------------------------------------------------
# 二、评估阶段路径（b0）
# ---------------------------------------------------------------------------


def _evaluation_stage(stage: str, candidate_id: str, baseline: int) -> str:
    canonical = _orchestrator()._STAGE_ALIASES.get(stage, stage)
    if canonical not in codex_upgrade_vc_artifacts.EVALUATION_STAGES:
        raise ConfigurationError(f"{stage} 不是评估阶段。")
    if not isinstance(candidate_id, str) or not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError(f"{canonical} 必须提供合法 candidate-id。")
    if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0:
        raise ConfigurationError("评估基线号必须是非负整数。")
    return canonical


def stage_read_source(campaign_dir: Path, candidate_id: str, baseline: int, stage: str) -> dict[str, Any]:
    """服务面 ``_stage_read_source``：b0 的读来源（原路径）；b≥1 随 B4-04 落实。"""

    orchestrator = _orchestrator()
    canonical = _evaluation_stage(stage, candidate_id, baseline)
    if baseline != 0:
        raise orchestrator._pending(f"_stage_read_source（评估基线 b{baseline}）", "B4-04（评估基线 b<K>）")
    if canonical == "assertions":
        path = Path(campaign_dir) / "assertions" / candidate_id / "results.json"
    else:
        path = orchestrator._legacy_stage_path(Path(campaign_dir), canonical, candidate_id)[1]
    exists = path.is_file() and not path.is_symlink()
    return {
        "baseline_of_record": 0,
        "path": path,
        "sha256": file_sha256(path) if exists else None,
        "status": "complete" if exists else "pending",
    }


def stage_write_target(campaign_dir: Path, candidate_id: str, baseline: int, stage: str) -> Path:
    """服务面 ``_stage_write_target``：b0 的写目标（原路径；断言阶段是候选的断言目录）；b≥1 随 B4-04 落实。"""

    orchestrator = _orchestrator()
    canonical = _evaluation_stage(stage, candidate_id, baseline)
    if baseline != 0:
        raise orchestrator._pending(f"_stage_write_target（评估基线 b{baseline}）", "B4-04（评估基线 b<K>）")
    if canonical == "assertions":
        return Path(campaign_dir) / "assertions" / candidate_id
    return orchestrator._legacy_stage_path(Path(campaign_dir), canonical, candidate_id)[1]


# ---------------------------------------------------------------------------
# 三、阶段幂等重派证明
# ---------------------------------------------------------------------------


def _orchestrator_argv(command: Sequence[str]) -> list[str]:
    """受管 Python 直接调用 Claude 编排器时的子命令参数；脚本、包装器、别的解释器都没有幂等合同。"""

    if len(command) > 3 and list(command[1:3]) == ["-m", ORCHESTRATOR_MODULE]:
        argv = list(command[3:])
    elif len(command) > 2 and Path(command[1]).resolve() == ORCHESTRATOR_SCRIPT:
        argv = list(command[2:])
    else:
        raise ConfigurationError("不是受管 claude_upgrade 的直接调用，不能证明幂等")
    if Path(command[0]).resolve(strict=True) != Path(sys.executable).resolve(strict=True):
        raise ConfigurationError("阶段动作解释器不是当前受管 Python")
    return argv


def _parse_contract(phase: str, argv: Sequence[str], campaign_dir: Path) -> tuple[str, str, dict[str, str]]:
    """子命令、模式与精确闭合的成对旗标；不在本阶段合同里的形态一律拒绝。"""

    contracts = STAGE_REPLAY_CONTRACTS[phase]
    subcommand = str(argv[0]) if argv else ""
    if subcommand not in contracts:
        raise ConfigurationError(f"{phase} 的动作 {subcommand or '（空）'} 没有阶段幂等合同")
    rest = [str(item) for item in argv[1:]]
    if len(rest) % 2:
        raise ConfigurationError(f"{subcommand} 的参数不是成对的旗标与值，没有幂等合同")
    keys = rest[0::2]
    flags = dict(zip(keys, rest[1::2]))
    if len(flags) != len(keys) or any(not key.startswith("--") for key in keys):
        raise ConfigurationError(f"{subcommand} 的旗标重复或形态非法")
    if len(contracts[subcommand]) == 1:
        (mode,) = contracts[subcommand]
    elif "--approve-revision" in flags or "--approve-joint-sha256" in flags:
        mode = "approve"
    elif "--from-revision" in flags or "--revision-input" in flags:
        mode = "revise"
    else:
        mode = "draft" if "draft" in contracts[subcommand] else "preview"
    required, optional = contracts[subcommand][mode]
    extra = set(flags) - required
    if not required <= set(flags) or extra - optional:
        raise ConfigurationError(f"{subcommand} {mode} 的参数没有精确闭合：{sorted(set(flags))}")
    if extra & _PAIRED_OPTIONAL and extra & _PAIRED_OPTIONAL != _PAIRED_OPTIONAL:
        raise ConfigurationError(f"{subcommand} 的 Active 输入必须成对给出")
    try:
        same_campaign = Path(flags["--campaign-dir"]).resolve(strict=True) == Path(campaign_dir).resolve(strict=True)
    except OSError:
        same_campaign = False
    if not same_campaign:
        raise ConfigurationError(f"{subcommand} 的 --campaign-dir 不是本 Campaign")
    return subcommand, mode, flags


def _campaign_input_file(campaign_dir: Path, value: str, flag: str) -> str:
    """文件旗标：Campaign 目录内的可信普通文件（producer 同样要求在 Campaign 内），返回摘要。"""

    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{flag} 不是可信的绝对路径普通文件")
    if not path.resolve(strict=True).is_relative_to(Path(campaign_dir).resolve(strict=True)):
        raise ConfigurationError(f"{flag} 不在 Campaign 目录内")
    return file_sha256(path)


def _tree_digests(root: Path, base: Path, label: str) -> dict[str, str]:
    """目录树里每个普通文件的摘要（键为相对 ``base`` 的路径）；符号链接与特殊文件一律拒绝，不跟随链接。"""

    if root.is_symlink():
        raise ConfigurationError(f"{label}不得是符号链接")
    digests: dict[str, str] = {}
    if not root.is_dir():
        return digests
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in sorted(subdirectories):
            if (current / name).is_symlink():
                raise ConfigurationError(f"{label}包含符号链接：{(current / name).relative_to(base).as_posix()}")
        for name in sorted(files):
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise ConfigurationError(f"{label}包含符号链接或特殊文件：{path.relative_to(base).as_posix()}")
            digests[path.relative_to(base).as_posix()] = file_sha256(path)
    return dict(sorted(digests.items()))


def _revision_input_digest(value: str) -> str:
    """修订输入目录：绝对路径、非符号链接，内容按文件名排序的树摘要。"""

    directory = Path(value)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ConfigurationError("--revision-input 不是可信的绝对路径目录")
    return canonical_sha256(_tree_digests(directory, directory, "修订输入目录"))


def _store_facts(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """只读打开 Campaign 的受管 Store，取本 Campaign 的全部事实（不加锁、不写）。"""

    root = claude_upgrade_store.campaign_store_root(campaign_dir)
    try:
        return ControlStore.open_read_only(root).list_facts(str(manifest["campaign_id"]))
    except ControlError as error:
        raise ConfigurationError(f"Campaign 受管 Store 不可读：{error}") from error


def _facts_of(facts: Sequence[Mapping[str, Any]], kind: str) -> list[Mapping[str, Any]]:
    return [fact for fact in facts if fact.get("fact_kind") == kind]


def _require_revision_label(value: str, flag: str) -> str:
    if not _classify().REVISION_RE.fullmatch(value):
        raise ConfigurationError(f"{flag} 不是合法的 revision（rev-NNNN）")
    return value


def _require_sha256(value: str, flag: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise ConfigurationError(f"{flag} 必须是 64 位小写十六进制")
    return value


def _classify_facts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    mode: str,
    flags: Mapping[str, str],
    facts: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """VC-2 ``classify`` 三种模式的前置判定与输入摘要（见模块说明第三节）。"""

    classify = _classify()
    campaign_id = str(manifest["campaign_id"])
    stage_path = campaign_dir / classify.STAGE_RESULT_RELATIVE
    approved = campaign_dir / classify.APPROVED_ROOT
    inputs: dict[str, str] = {}
    if mode in {"draft", "revise"}:
        if stage_path.exists() or stage_path.is_symlink() or approved.exists() or approved.is_symlink():
            raise ConfigurationError("分类已批准：草案与修订动作逐字重派必然被拒绝（封存后变化须建立后继 Campaign）")
        if _facts_of(facts, "rule_classification_recorded"):
            raise ConfigurationError("Store 已记录规则分类事实：只能重跑原批准命令收敛，草案与修订不能重派")
        if mode == "draft":
            inputs["--candidates"] = _campaign_input_file(campaign_dir, flags["--candidates"], "--candidates")
            return inputs
        parent = _require_revision_label(flags["--from-revision"], "--from-revision")
        classify.load_revision(campaign_dir, campaign_id, parent)
        inputs["--revision-input"] = _revision_input_digest(flags["--revision-input"])
        inputs[f"revision:{parent}"] = file_sha256(campaign_dir / classify.DRAFT_ROOT / parent / classify.DRAFT_RECEIPT_FILENAME)
        return inputs
    revision = _require_revision_label(flags["--approve-revision"], "--approve-revision")
    review = _require_sha256(flags["--approve-review-sha256"], "--approve-review-sha256")
    receipt, _files = classify.load_revision(campaign_dir, campaign_id, revision)
    inputs[f"revision:{revision}"] = file_sha256(campaign_dir / classify.DRAFT_ROOT / revision / classify.DRAFT_RECEIPT_FILENAME)
    if stage_path.exists() or stage_path.is_symlink():
        stage = _read_json(stage_path, "分类阶段结果")
        if stage.get("revision") != revision or stage.get("review_sha256") != review:
            raise ConfigurationError(
                f"分类已批准 revision {stage.get('revision')}（审核摘要 {str(stage.get('review_sha256'))[:12]}…）：重派的批准动作必然被拒绝"
            )
    if approved.exists() or approved.is_symlink():
        expected = {filename: receipt["files"][name]["sha256"] for name, filename in classify.DRAFT_FILES.items()}
        if approved.is_symlink() or not approved.is_dir() or sorted(child.name for child in approved.iterdir()) != sorted(expected):
            raise ConfigurationError("分类批准目录不闭合：重派的批准动作会被拒绝覆盖，先修复半成品再对账")
        for filename, digest in expected.items():
            path = approved / filename
            if path.is_symlink() or not path.is_file() or file_sha256(path) != digest:
                raise ConfigurationError(f"分类批准目录的 {filename} 与 revision {revision} 不一致：重派的批准动作会被拒绝覆盖")
    return inputs


def _prepare_profile_facts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    mode: str,
    flags: Mapping[str, str],
    facts: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """VC-3 ``prepare-profile`` 三种模式的前置判定与输入摘要。"""

    profile = _profile()
    campaign_id = str(manifest["campaign_id"])
    profile_approved = (campaign_dir / profile.STAGE_RESULT_RELATIVE).exists() or bool(_facts_of(facts, "profile_approved"))
    inputs: dict[str, str] = {}
    if mode in {"draft", "revise"}:
        if profile_approved:
            raise ConfigurationError("画像已批准（ProfileApprovalFact）：准备草案与修订动作逐字重派必然被拒绝")
        if mode == "draft":
            for flag in sorted(set(flags) & _PAIRED_OPTIONAL):
                inputs[flag] = _campaign_input_file(campaign_dir, flags[flag], flag)
            return inputs
        parent = _require_revision_label(flags["--from-revision"], "--from-revision")
        profile.load_revision(campaign_dir, campaign_id, parent)
        inputs["--revision-input"] = _revision_input_digest(flags["--revision-input"])
        inputs[f"revision:{parent}"] = file_sha256(campaign_dir / profile.PREPARE_DRAFT_ROOT / parent / profile.DRAFT_RECEIPT_FILENAME)
        return inputs
    revision = _require_revision_label(flags["--approve-revision"], "--approve-revision")
    review = _require_sha256(flags["--approve-review-sha256"], "--approve-review-sha256")
    profile.load_revision(campaign_dir, campaign_id, revision)
    inputs[f"revision:{revision}"] = file_sha256(campaign_dir / profile.PREPARE_DRAFT_ROOT / revision / profile.DRAFT_RECEIPT_FILENAME)
    result_path = profile.prepare_result_path(campaign_dir, revision)
    if result_path.exists() or result_path.is_symlink():
        result = _read_json(result_path, "画像准备结果")
        if result.get("approved_by") != flags["--approved-by"] or result.get("review_sha256") != review:
            raise ConfigurationError(f"准备 revision {revision} 已由 {result.get('approved_by')} 以另一审核摘要批准：重派的批准动作必然被拒绝")
    elif profile_approved:
        raise ConfigurationError("画像已批准（ProfileApprovalFact）：不再批准新的准备 revision，重派必然被拒绝")
    return inputs


def _stage_profile_facts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    mode: str,
    flags: Mapping[str, str],
    facts: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """VC-3 ``stage-profile`` 预览与批准的前置判定与输入摘要。"""

    del manifest
    profile = _profile()
    stage_path = campaign_dir / profile.STAGE_RESULT_RELATIVE
    approvals = _facts_of(facts, "profile_approved")
    inputs: dict[str, str] = {}
    if mode == "preview":
        if stage_path.exists() or stage_path.is_symlink():
            raise ConfigurationError("画像已批准（profile/result.json 已存在）：暂存预览动作逐字重派必然被拒绝")
        if approvals:
            raise ConfigurationError("Store 已记录 ProfileApprovalFact：只能重跑原批准命令收敛，暂存预览不能重派")
        revisions = profile.approved_prepare_revisions(campaign_dir)
        if not revisions:
            raise ConfigurationError("没有已批准的画像准备包：暂存预览动作重派必然被拒绝")
        latest = profile.prepare_result_path(campaign_dir, revisions[-1])
        inputs[latest.relative_to(campaign_dir).as_posix()] = file_sha256(latest)
        return inputs
    joint = _require_sha256(flags["--approve-joint-sha256"], "--approve-joint-sha256")
    approved_by = flags["--approved-by"]
    for fact in approvals:
        payload = fact.get("payload") if isinstance(fact.get("payload"), Mapping) else {}
        if payload.get("reviewer") != approved_by:
            raise ConfigurationError(f"Store 已记录批准人 {payload.get('reviewer')} 的 ProfileApprovalFact：重派的批准动作必然被拒绝")
    if stage_path.exists() or stage_path.is_symlink():
        stage = _read_json(stage_path, "画像阶段结果")
        if stage.get("joint_sha256") != joint:
            raise ConfigurationError("画像已按另一联合摘要批准：重派的批准动作必然被拒绝")
    stage_root = campaign_dir / profile.STAGE_ROOT
    frozen = []
    if stage_root.is_dir() and not stage_root.is_symlink():
        for manifest_path in sorted(stage_root.glob(f"*/{profile.JOINT_MANIFEST_FILENAME}")):
            if manifest_path.is_symlink() or not manifest_path.is_file():
                continue
            if _read_json(manifest_path, "联合清单").get("joint_sha256") == joint:
                frozen.append(manifest_path)
    if len(frozen) != 1:
        raise ConfigurationError("没有唯一冻结该联合摘要的暂存预览：重派的批准动作必然被拒绝")
    inputs[frozen[0].relative_to(campaign_dir).as_posix()] = file_sha256(frozen[0])
    return inputs


_MODE_FACTS = {
    "classify": _classify_facts,
    "prepare-profile": _prepare_profile_facts,
    "stage-profile": _stage_profile_facts,
}


def _phase_outputs(
    campaign_dir: Path,
    plan: Mapping[str, Any],
    phase: str,
    facts: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """阶段产物目录的文件摘要、本阶段 Store 事实摘要与已封存 checkpoint（重放后绑定）。"""

    root_name, kinds = STAGE_REPLAY_OUTPUTS[phase]
    outputs = _tree_digests(campaign_dir / root_name, campaign_dir, f"{phase} 阶段产物目录")
    for kind in kinds:
        matching = _facts_of(facts, kind)
        if matching:
            outputs[f"control-store/{kind}"] = canonical_sha256([dict(fact) for fact in matching])
    checkpoint = claude_upgrade_vc.vc_checkpoint_path(campaign_dir, phase)
    if checkpoint.exists() or checkpoint.is_symlink():
        claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, phase)
        outputs["checkpoint"] = file_sha256(checkpoint)
    return outputs


def _store_candidate_build_refs(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> list[dict[str, Any]]:
    """只读打开受管 Store，取属于本候选的 CandidateBuildReceipt 引用（按摘要排序）。"""

    root = claude_upgrade_store.campaign_store_root(campaign_dir)
    try:
        store = ControlStore.open_read_only(root)
        references = [dict(reference) for reference in store.list_receipt_refs("candidate_build")]
        owned = [reference for reference in references if store.load_receipt(reference).get("candidate_id") == candidate_id]
    except ControlError as error:
        raise ConfigurationError(f"Campaign 受管 Store 不可读：{error}") from error
    return sorted(owned, key=lambda reference: str(reference.get("sha256")))


def _candidate_stage_facts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    run_manifest: Mapping[str, Any],
    argv: Sequence[str],
    flags: Mapping[str, str],
    facts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """VC-4 两个零请求动作的前置判定与绑定：共享 ``_candidate_stage_replay_action_facts`` ＋ Claude 的 Store 侧（见模块说明第三节）。

    ``_parse_contract`` 已确认 ``--campaign-dir`` 与本 Campaign 是同一目录；共享判定按字面比较，所以把命令里的字面值传给它。
    """

    adapter = upgrade_client_adapter.adapter_for("claude")
    with upgrade_control_plane.adapter_scope(adapter):
        inputs, outputs = upgrade_control_plane._candidate_stage_replay_action_facts(
            Path(flags["--campaign-dir"]), run_manifest, [str(item) for item in argv]
        )
    if argv[0] != "record-candidate-build":
        return dict(inputs), dict(outputs)
    candidate_id = flags["--candidate-id"]
    image_digest = "sha256:" + str(flags["--runtime-image"]).rsplit("sha256:", 1)[-1]
    frozen = [
        fact
        for fact in _facts_of(facts, "candidate_frozen")
        if isinstance(fact.get("payload"), Mapping) and fact["payload"].get("candidate_id") == candidate_id
    ]
    for fact in frozen:
        payload = fact["payload"]
        if payload.get("build_id") != flags["--build-id"] or payload.get("image_digest") != image_digest:
            raise ConfigurationError(
                f"Store 已冻结候选 {candidate_id} 的另一次构建（build_id {payload.get('build_id')}）：重派的 record-candidate-build 必然被拒绝"
            )
    outputs = dict(outputs)
    if frozen:
        outputs["control-store/candidate_frozen"] = canonical_sha256([dict(fact) for fact in frozen])
    builds = _store_candidate_build_refs(campaign_dir, manifest, candidate_id)
    if builds:
        outputs["control-store/candidate_build"] = canonical_sha256(builds)
    return dict(inputs), outputs


def campaign_stage_replay_facts(campaign_dir: Path, run_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """服务面 ``_campaign_stage_replay_facts``：只读判定失败批次能否按原批次 N+1 重派（合同见模块说明第三节）。

    返回字段闭集 ``{phase, allowed, actions, reasons}``，每个动作 ``{action_id, command, inputs, outputs}``——对账器原样
    写进 ``codex-upgrade-stage-replay/v1`` 证明，监督器派发直接后继前再算一次逐字比较。
    """

    phase = str(run_manifest.get("phase"))
    result: dict[str, Any] = {"phase": phase, "allowed": False, "actions": [], "reasons": []}
    if phase == "VC-1":
        result["reasons"].append(VC1_REPLAY_REASON)
        return result
    if phase not in STAGE_REPLAY_CONTRACTS:
        result["reasons"].append(f"{phase} 没有阶段幂等重派合同")
        return result
    campaign_dir = Path(campaign_dir)
    candidate_phase = phase in codex_upgrade_timing_ledger.CANDIDATE_STAGE_REPLAY_PHASES
    try:
        orchestrator = _orchestrator()
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        facts = _store_facts(campaign_dir, manifest)
        outputs: dict[str, str] = {}
        if not candidate_phase:
            plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
            outputs = _phase_outputs(campaign_dir, plan, phase, facts)
        for action in run_manifest.get("actions", []):
            command = [str(item) for item in action["command"]]
            argv = _orchestrator_argv(command)
            subcommand, mode, flags = _parse_contract(phase, argv, campaign_dir)
            if candidate_phase:
                inputs, action_outputs = _candidate_stage_facts(campaign_dir, manifest, run_manifest, argv, flags, facts)
            else:
                inputs = _MODE_FACTS[subcommand](campaign_dir, manifest, mode, flags, facts)
                action_outputs = dict(outputs)
            result["actions"].append(
                {"action_id": str(action["action_id"]), "command": command, "inputs": inputs, "outputs": action_outputs}
            )
        if not result["actions"]:
            raise ConfigurationError("失败批次没有可核验动作")
        result["allowed"] = True
    except (OSError, ValueError, KeyError, TypeError, ConfigurationError) as error:
        result["actions"] = []
        result["reasons"].append(str(error))
    return result


# ---------------------------------------------------------------------------
# 四、父监督器上下文的退出码
# ---------------------------------------------------------------------------


def in_campaign_run() -> bool:
    return os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV) == "1"


def campaign_run_aware_exit_code(result: Mapping[str, Any], direct_exit_code: int) -> int:
    """父批次把合法暂停点视为动作成功（退出 0）；直接 CLI 保持原诊断码。"""

    if in_campaign_run() and result.get("status") in CAMPAIGN_RUN_PAUSE_STATUSES:
        return 0
    return direct_exit_code


# ---------------------------------------------------------------------------
# 五、批次控制面命令不进动作队列
# ---------------------------------------------------------------------------


def reject_control_plane_in_campaign_run(command: str) -> None:
    if in_campaign_run() and command in CONTROL_PLANE_COMMANDS:
        raise ConfigurationError(f"{command} 是 Campaign 引导／批次控制面命令，禁止由 campaign-run 动作派发。")


# ---------------------------------------------------------------------------
# 六、受管动作与动作计划
# ---------------------------------------------------------------------------


def managed_command(argv: Sequence[Any]) -> list[str]:
    """受管 Python 直接调用 ``claude_upgrade.py`` 的命令（阶段幂等重派证明认可的形态）。"""

    return [sys.executable, str(ORCHESTRATOR_SCRIPT), *[str(item) for item in argv]]


def managed_action(
    action_id: str,
    operation: str,
    argv: Sequence[Any],
    *,
    item_ids: Iterable[str],
    timeout_seconds: float = DEFAULT_ACTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "operation": operation,
        "timeout_seconds": timeout_seconds,
        "command": managed_command(argv),
        "item_ids": sorted(set(item_ids)),
    }


def action_plan(
    actions: Sequence[Mapping[str, Any]],
    *,
    phase: str,
    reuse_item_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """批次动作计划：执行项是全部动作的项，动作按 ``action_id`` 排序；按共享合同校验后返回。"""

    ordered = sorted((dict(action) for action in actions), key=lambda action: str(action["action_id"]))
    payload = {
        "schema_version": codex_upgrade_vc_artifacts.VC_ACTION_PLAN_SCHEMA,
        "execute_item_ids": sorted({item for action in ordered for item in action["item_ids"]}),
        "reuse_item_ids": sorted(set(reuse_item_ids)),
        "actions": ordered,
    }
    try:
        codex_upgrade_vc_artifacts.validate_action_plan(payload, phase=phase)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"动作计划不合法：{error}") from error
    return payload
