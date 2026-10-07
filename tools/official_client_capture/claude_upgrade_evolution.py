"""Claude 工具演进：演进链读写、有效身份、演进预览与 Store 侧登记（Claude 升级控制面设计 B2-08）。

一、为什么 Claude 自带演进链的读写
==================================
共享的 ``codex_upgrade_wire_transition`` 定义了演进收据合同 ``tool-evolution/v1`` 与链校验，但链校验
（``load_evolutions``／``_validate_evolution_payload``）在 Codex 评估器读侧闭包内，不能改；它把策略演进的变化路径钉死为
Codex 的策略文件名 ``tool_identity_policy_v2.json``，Claude 的策略演进（受管条目键
``official_client_capture/claude_tool_identity_policy_v2.json``）会被它拒绝。所以 Claude 的链读写在这里另写一份：合同、
文件位置（``control/tool-evolution/evolution-NN.json``）、自摘要与衔接规则全部与共享实现相同，只有两处不同——

* 策略演进的变化路径按 Claude 策略文件的受管条目键核对；
* 收据里的 ``to_identity`` 必须属于 Claude（``client == claude``）。

读侧只调用共享模块的纯函数（自摘要、身份摘要、策略演进形态校验、预约生产序号与累计影响），不改它们。共享代码经适配器
成员 ``effective_wire_identity`` 取 Claude 的有效 wire 身份（对账器 ``_identity_facts``）；编排器的
``_campaign_tool_evolutions``／``_campaign_effective_tool_identity`` 转调本模块（与 Codex 照搬版本登记分叉）。

二、演进预览（服务面成员 ``_tool_evolution_preview``）
=====================================================
口径与 Codex 相同：变化路径按 Campaign 有效身份与当前受管树逐文件比较、按 Claude 策略分层；wire 层变化与（被作业声明依赖
的）其它客户端文件映射到作业，编排器 wire 闭包变化或映射不到作业的 wire 层变化判全部作业受影响；evidence 层变化只记
``evaluation_paths``（不重采，按评估纪元重评）；control 层变化影响为空但同样要登记（当前受管树必须恰好是有效身份）。官方
已封存而作业受影响时拒绝登记。部署绑定取当前五摘要的 Claude 受管部署收据（对账器 ``_deployment_receipt`` 经适配器读
``identity`` 子键），且晚于上一次演进的部署。

评估器（4a，协调方 10-07 定）：Claude 的 checker／builder 由 B4-03 建立，之前不在受管树里。两者都不存在时评估器摘要显式记为
「尚未建立」（摘要为 null，``status`` 为 ``not_established``），不失败；两次都尚未建立算不变；从尚未建立到建立的那次演进记
一次「评估器建立」（两项都列为变化）；只建立了一个即失败关闭。候选侧（B4-04）：按 Codex 同一口径扫描 ``candidates/``，
有作废记录或被取代标记的候选记入 ``impact.inactive_candidates``；活跃候选在 ``evaluator.candidates`` 记当前评估基线、b0 已有
评估产出与 ``b0_authorization_moved``（b0 且没有产出时 b0 授权随本次演进迁移，见 ``claude_upgrade_baseline``）。候选已有
取证 attempt 时它的作业影响由 B4-01（候选采集）落实，此时报待实现。

策略演进（4b）：当前策略与有效策略不同时只允许「只改策略文件」的演进，绑定按 Claude v3 引擎出具的兼容收据与激活认证
（``codex_upgrade_policy_certification`` 的 ``client="claude"`` 口径），判据与 Codex 的 ``_policy_evolution_transition`` 相同。

三、Store 侧登记（协调方第 4 项）
==================================
seal 两层都核有效工具身份：编排器链（``_require_tool_evolution_registered``）与 Campaign Store 的
``tool_evolution_registration`` 链（``claude_upgrade_seal._require_store_tool_current``）。批准演进写完编排器收据后，
``ensure_store_tool_registration`` 在 Store 里登记一份 ``tool_evolution_registration``：``from_identity`` 是 Store 当前有效身份、
``to_identity`` 是 Store 现算的当前受管代码身份（Store 写入门禁本身要求它等于当前代码），并绑定这份编排器收据。两步之间
中断时，以同一批准重跑 ``tool-evolution`` 只补 Store 登记；seal 打开 Store 时也会补登这一份。Store 链因此可以比编排器链短
（只登记写入时的当前身份），Store 事实总在登记后的身份下形成。

Store Campaign 必须在工具变化之前建立：Store 建 Campaign v2 时要求冻结的工具身份等于现算值（B1-12），工具一旦变化就再也
建不出来（首次 seal 里的幂等建立也会被拒）。生产中它由 VC-0 收口建 Campaign 时一并建立（B2-11）；tool-evolution 在登记前
核对，未建立即零写入拒绝，免得登记之后再发真实请求、到 seal 才发现无法封存。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256
from tools.official_client_capture import claude_tool_identity_policy
from tools.official_client_capture import codex_upgrade_wire_transition as wire_transition
from tools.official_client_capture import upgrade_client_adapter

CLIENT = "claude"
# Claude 身份策略文件的受管条目键（两根清单的键带根目录名）：策略演进的变化路径只能是它。
POLICY_ENTRY = f"official_client_capture/{claude_tool_identity_policy.POLICY_FILENAME}"
EVALUATOR_ESTABLISHED = "established"
EVALUATOR_NOT_ESTABLISHED = "not_established"
EVALUATOR_ENTRY_FIELDS = ("checker_sha256", "builder_sha256")
STORE_REGISTRATION_KIND = "tool_evolution_registration"


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


# ---------------------------------------------------------------------------
# 演进链读写（合同同 codex_upgrade_wire_transition，策略文件按 Claude 条目键）
# ---------------------------------------------------------------------------


def _validate_evolution_payload(
    payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    manifest_sha256: str,
    previous_sha: str | None,
    previous_summary: Mapping[str, Any],
    label: str,
) -> None:
    """单条演进收据的衔接与形态（规则同共享 ``_validate_evolution_payload``，策略演进按 Claude 策略文件核对）。"""

    if payload.get("campaign_id") != manifest.get("campaign_id"):
        raise wire_transition.WireTransitionError(f"{label} 不属于本 Campaign")
    if payload.get("campaign_manifest_sha256") != manifest_sha256:
        raise wire_transition.WireTransitionError(f"{label} 绑定的 Campaign 清单摘要不一致")
    if payload.get("previous_evolution_sha256") != previous_sha:
        raise wire_transition.WireTransitionError(f"{label} 未衔接上一演进")
    if payload.get("from_summary") != dict(previous_summary):
        raise wire_transition.WireTransitionError(f"{label} 的起点不是上一有效工具身份")
    to_identity = payload.get("to_identity")
    if not isinstance(to_identity, Mapping) or not isinstance(to_identity.get("entries"), list):
        raise wire_transition.WireTransitionError(f"{label} 缺少完整 to 工具身份")
    if to_identity.get("client") != CLIENT:
        raise wire_transition.WireTransitionError(f"{label} 的 to 工具身份不属于 Claude")
    if payload.get("to_summary") != wire_transition.identity_summary(to_identity):
        raise wire_transition.WireTransitionError(f"{label} 的 to 摘要与完整身份不一致")
    if payload.get("to_summary") == dict(previous_summary):
        raise wire_transition.WireTransitionError(f"{label} 的 to 身份与起点相同，没有需要登记的演进")
    if wire_transition._fingerprint({"entries": list(to_identity["entries"])}) != to_identity.get("files_sha256"):
        raise wire_transition.WireTransitionError(f"{label} 的 to 文件清单与整树摘要不一致")
    impact = payload.get("impact")
    changes = payload.get("changes")
    if (
        not isinstance(impact, Mapping)
        or not isinstance(impact.get("official"), Mapping)
        or not isinstance(impact.get("candidates"), Mapping)
        or not isinstance(changes, Mapping)
        or not isinstance(changes.get("paths_by_layer"), Mapping)
    ):
        raise wire_transition.WireTransitionError(f"{label} 的影响或变化记录形态非法")
    policy_changed = to_identity.get("policy_sha256") != previous_summary.get("policy_sha256")
    transition = payload.get("policy_transition")
    if policy_changed:
        if transition is None:
            raise wire_transition.WireTransitionError(
                f"{label} 改变了工具身份策略但没有 policy_transition；策略变化只能以策略演进（绑定兼容收据与激活认证）承接"
            )
        wire_transition._validate_policy_transition(
            transition,
            previous_summary=previous_summary,
            to_summary=dict(payload["to_summary"]),
            changes=changes,
            policy_relative_path=POLICY_ENTRY,
            label=label,
        )
    elif transition is not None:
        raise wire_transition.WireTransitionError(f"{label} 策略未变化，不得携带 policy_transition")


def load_evolutions(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """读取并校验 Claude 工具演进链；没有目录返回空链（文件名、编号、自摘要与衔接规则同共享实现）。"""

    root = Path(campaign_dir) / wire_transition.EVOLUTION_DIR
    if not root.exists() and not root.is_symlink():
        return []
    if root.is_symlink() or not root.is_dir():
        raise wire_transition.WireTransitionError("tool-evolution 目录不可信")
    frozen = manifest.get("tool_identity")
    if not isinstance(frozen, Mapping) or wire_transition._v2_identity(manifest) is None:
        raise wire_transition.WireTransitionError("只有带五摘要身份的 Campaign 才能登记工具演进")
    found: dict[int, dict[str, Any]] = {}
    for child in sorted(root.iterdir()):
        match = wire_transition.EVOLUTION_RE.fullmatch(child.name)
        if not match:
            raise wire_transition.WireTransitionError(f"tool-evolution 含非法文件：{child.name}")
        payload = wire_transition._read_json(child, f"工具演进 {child.name}")
        index = int(match.group(1))
        if payload.get("schema_version") != wire_transition.EVOLUTION_SCHEMA or payload.get("index") != index:
            raise wire_transition.WireTransitionError(f"工具演进 {child.name} schema 或编号非法")
        wire_transition._self_check(payload, f"工具演进 {child.name}")
        found[index] = payload
    manifest_sha256 = wire_transition._campaign_manifest_file_sha256(Path(campaign_dir))
    chain: list[dict[str, Any]] = []
    previous_sha: str | None = None
    previous_summary = wire_transition.identity_summary(frozen)
    for index in range(1, max(found, default=0) + 1):
        payload = found.get(index)
        if payload is None:
            raise wire_transition.WireTransitionError(f"工具演进链缺少 evolution-{index:02d}")
        _validate_evolution_payload(
            payload,
            manifest,
            manifest_sha256=manifest_sha256,
            previous_sha=previous_sha,
            previous_summary=previous_summary,
            label=f"evolution-{index:02d}",
        )
        previous_sha = str(payload["receipt_sha256"])
        previous_summary = dict(payload["to_summary"])
        chain.append(payload)
    return chain


def _reject_wire_transitions(campaign_dir: Path) -> None:
    if wire_transition._load_transitions(Path(campaign_dir)):
        raise wire_transition.WireTransitionError(
            "Claude Campaign 不使用 wire transition 链：wire 层变化一律经工具演进（tool-evolution）登记"
        )


def effective_tool_identity(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """当前有效工具身份：最新演进的 to 完整身份；没有演进时是总计划冻结身份（返回形状同共享实现）。"""

    chain = load_evolutions(campaign_dir, manifest)
    _reject_wire_transitions(campaign_dir)
    if not chain:
        identity = dict(manifest["tool_identity"])
        return {"index": 0, "receipt_sha256": None, "identity": identity, "summary": wire_transition.identity_summary(identity)}
    last = chain[-1]
    return {
        "index": int(last["index"]),
        "receipt_sha256": str(last["receipt_sha256"]),
        "identity": dict(last["to_identity"]),
        "summary": dict(last["to_summary"]),
    }


def effective_wire_identity(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """适配器成员：Claude Campaign 的有效 wire 身份（返回形状同 ``wire_transition.effective_wire_identity``）。"""

    base = wire_transition._v2_identity(manifest)
    if base is None:
        return {"policy_version": 1, "wire_producer_sha256": None, "policy_sha256": None, "source": "v1", "pending_intent": None, "chain_length": 0}
    _reject_wire_transitions(campaign_dir)
    chain = load_evolutions(campaign_dir, manifest)
    if chain:
        last = chain[-1]
        return {
            "policy_version": int(last["to_summary"].get("policy_version") or base["policy_version"]),
            "wire_producer_sha256": str(last["to_summary"]["wire_producer_sha256"]),
            "policy_sha256": str(last["to_summary"]["policy_sha256"]),
            "evidence_semantics_sha256": str(last["to_summary"]["evidence_semantics_sha256"]),
            "source": f"evolution-{int(last['index']):02d}",
            "pending_intent": None,
            "chain_length": 0,
            "evolution_index": int(last["index"]),
        }
    return {
        "policy_version": base["policy_version"],
        "wire_producer_sha256": base["wire_producer_sha256"],
        "policy_sha256": base["policy_sha256"],
        "evidence_semantics_sha256": base["evidence_semantics_sha256"],
        "source": "manifest",
        "pending_intent": None,
        "chain_length": 0,
    }


def write_evolution(campaign_dir: Path, manifest: Mapping[str, Any], payload: Mapping[str, Any]) -> Path:
    """写入下一个演进收据（原子、只写一次）；编号、previous 与 from 摘要必须衔接现有链，写后整链重放。"""

    chain = load_evolutions(campaign_dir, manifest)
    _reject_wire_transitions(campaign_dir)
    index = len(chain) + 1
    if index > wire_transition.EVOLUTION_MAX_INDEX:
        raise wire_transition.WireTransitionError("工具演进次数已达上限")
    expected_previous = chain[-1]["receipt_sha256"] if chain else None
    expected_from = dict(chain[-1]["to_summary"]) if chain else wire_transition.identity_summary(manifest["tool_identity"])
    if payload.get("schema_version") != wire_transition.EVOLUTION_SCHEMA or payload.get("index") != index:
        raise wire_transition.WireTransitionError("工具演进收据的 schema 或编号与现有链不衔接；重新生成预览")
    wire_transition._self_check(payload, "工具演进收据")
    _validate_evolution_payload(
        payload,
        manifest,
        manifest_sha256=wire_transition._campaign_manifest_file_sha256(Path(campaign_dir)),
        previous_sha=expected_previous,
        previous_summary=expected_from,
        label=f"evolution-{index:02d}",
    )
    path = Path(campaign_dir) / wire_transition.EVOLUTION_DIR / f"evolution-{index:02d}.json"
    _orchestrator()._secure_write_json_once(path, dict(payload))
    load_evolutions(campaign_dir, manifest)
    return path


# ---------------------------------------------------------------------------
# 演进预览
# ---------------------------------------------------------------------------


def _entry_digests(identity: Mapping[str, Any]) -> dict[str, str]:
    entries = identity.get("entries")
    if not isinstance(entries, list):
        raise ConfigurationError("工具身份缺少逐文件清单。")
    return {str(entry["path"]): str(entry["sha256"]) for entry in entries if isinstance(entry, Mapping) and "path" in entry}


def _policy_only_change_paths(from_identity: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """两份身份之间除 Claude 策略文件之外的变化路径；策略演进要求它为空。"""

    before = _entry_digests(from_identity)
    after = _entry_digests(current)
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path) and path != POLICY_ENTRY)


def _policy_evolution_transition(
    from_identity: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    deployment: Mapping[str, Any],
    compatibility_receipt: Path | None,
    activation_certification: Path | None,
) -> dict[str, Any]:
    """Claude 策略演进的 ``policy_transition``：判据同 Codex（只改策略文件、兼容收据、激活认证），认证按 Claude 口径。"""

    from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification

    extra = _policy_only_change_paths(from_identity, current)
    if extra:
        raise ConfigurationError(
            f"工具身份策略已变化，但本次部署还改了策略文件之外的受管文件：{'、'.join(extra)}；策略演进只允许改 {POLICY_ENTRY}。"
            "请拆成两次部署：先只改策略并登记策略演进，再部署代码改动并登记常规演进。"
        )
    for label, value in (
        ("--policy-compatibility-receipt", compatibility_receipt),
        ("--policy-activation-certification", activation_certification),
    ):
        if value is None:
            raise ConfigurationError(f"工具身份策略已变化，登记策略演进必须提供 {label}（Claude 策略的兼容收据与激活认证）。")
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"{label} 必须是可信的绝对路径普通文件：{path}")
    compatibility_path = Path(compatibility_receipt).resolve(strict=True)  # type: ignore[arg-type]
    activation_path = Path(activation_certification).resolve(strict=True)  # type: ignore[arg-type]
    try:
        compatibility = policy_certification.load_compatibility_receipt(compatibility_path, client=CLIENT)
        activation = policy_certification.verify_activation_certification(
            activation_path,
            expected_identity={**current, "tool_files_sha256": current["files_sha256"]},
            client=CLIENT,
        )
    except policy_certification.PolicyCertificationError as error:
        raise ConfigurationError(f"策略演进的认证收据不可信：{error}") from error
    previous = compatibility.get("previous") or {}
    latest = compatibility.get("current") or {}
    if previous.get("policy_sha256") != from_identity.get("policy_sha256") or int(previous.get("policy_version", -1)) != int(
        from_identity.get("policy_version", -2)
    ):
        raise ConfigurationError("兼容收据的旧策略不是 Campaign 当前有效策略（先按有效身份出具兼容收据）。")
    if latest.get("policy_sha256") != current.get("policy_sha256") or int(latest.get("policy_version", -1)) != int(
        current.get("policy_version", -2)
    ):
        raise ConfigurationError("兼容收据的新策略不是当前受管工具的策略。")
    if compatibility.get("tool_files_sha256") != current.get("files_sha256"):
        raise ConfigurationError("兼容收据不是在当前受管树上出具的（tool_files_sha256 不一致）。")
    two = ("wire_producer_sha256", "evidence_semantics_sha256")
    three = (*two, "control_sha256")
    under_previous = compatibility.get("identity_under_previous_policy") or {}
    under_current = compatibility.get("identity_under_current_policy") or {}
    if {key: under_previous.get(key) for key in two} != {key: from_identity.get(key) for key in two}:
        raise ConfigurationError("兼容收据在旧策略下算出的 wire／evidence 摘要与 Campaign 有效身份不一致；不是同一棵树。")
    if {key: under_current.get(key) for key in three} != {key: current.get(key) for key in three}:
        raise ConfigurationError("兼容收据在新策略下算出的三层摘要与当前工具不一致。")
    compatibility_sha256 = file_sha256(compatibility_path)
    if (activation.get("compatibility_receipt") or {}).get("sha256") != compatibility_sha256:
        raise ConfigurationError("策略激活认证绑定的兼容收据不是本次提供的兼容收据。")
    if (activation.get("deployment_receipt") or {}).get("sha256") != deployment.get("sha256"):
        raise ConfigurationError("策略激活认证绑定的部署收据不是本次演进绑定的部署收据。")
    return {
        "from_policy_sha256": str(from_identity["policy_sha256"]),
        "from_policy_version": int(from_identity["policy_version"]),
        "to_policy_sha256": str(current["policy_sha256"]),
        "to_policy_version": int(current["policy_version"]),
        "compatibility_receipt": {"path": str(compatibility_path), "sha256": compatibility_sha256},
        "activation_certification": {"path": str(activation_path), "sha256": file_sha256(activation_path)},
    }


def evaluator_state(identity: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """身份里 checker／builder 的状态：两者都不在受管树为「尚未建立」（摘要 null），都在为已建立，只有一个即失败关闭。"""

    digests = _entry_digests(identity)
    values = {
        "checker_sha256": digests.get(str(policy["evaluator"]["checker"])),
        "builder_sha256": digests.get(str(policy["evaluator"]["builder"])),
    }
    present = [field for field, value in values.items() if value is not None]
    if not present:
        return {"status": EVALUATOR_NOT_ESTABLISHED, **values}
    if len(present) != len(values):
        raise ConfigurationError("Claude 评估器的 checker 与 builder 只建立了一个，评估器状态不一致；两者须随 B4-03 同时建立。")
    return {"status": EVALUATOR_ESTABLISHED, **values}


def _to_evaluator_digests(state: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """演进 to 侧的评估器四项摘要：尚未建立时四项都是 null（带 status），建立后用 v3 引擎现算。"""

    if state["status"] == EVALUATOR_NOT_ESTABLISHED:
        return {
            "status": EVALUATOR_NOT_ESTABLISHED,
            **{field: None for field in claude_tool_identity_policy.EVALUATOR_DIGEST_FIELDS},
        }
    try:
        digests = claude_tool_identity_policy.evaluator_dependency_digests(policy=policy)
    except claude_tool_identity_policy.ToolIdentityPolicyError as error:
        raise ConfigurationError(f"Claude 评估器摘要无法计算：{error}") from error
    return {"status": EVALUATOR_ESTABLISHED, **dict(digests)}


def tool_evolution_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    fix_commit: str,
    reason: str,
    control_root: Path | None,
    compatibility_receipt: Path | None = None,
    activation_certification: Path | None = None,
) -> dict[str, Any]:
    """计算 Claude 工具演进预览：变化路径、逐作业影响、评估器状态与绑定；只读，不写文件。"""

    from tools.official_client_capture import claude_upgrade_capture as capture
    from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    frozen = manifest.get("tool_identity")
    if not isinstance(frozen, Mapping) or not orchestrator._is_policy_v2_identity(frozen):
        raise ConfigurationError("只有带五摘要身份的 Claude Campaign 才能登记工具演进。")
    if not orchestrator.TOOL_EVOLUTION_FIX_COMMIT_RE.fullmatch(str(fix_commit)):
        raise ConfigurationError("--fix-commit 必须是 40 位小写十六进制提交号。")
    if not isinstance(reason, str) or not reason.strip():
        raise ConfigurationError("--reason 不能为空：写明修复了什么。")
    chain = orchestrator._campaign_tool_evolutions(campaign_dir, manifest)
    effective = orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)
    from_identity = effective["identity"]
    current = orchestrator._tool_identity(include_git=False)
    policy_changed = current.get("policy_sha256") != from_identity.get("policy_sha256")
    if policy_changed:
        extra = _policy_only_change_paths(from_identity, current)
        if extra:
            raise ConfigurationError(
                "工具身份策略已变化，但本次部署还改了策略文件之外的受管文件："
                + "、".join(extra)
                + "；策略演进只允许改策略文件，请拆成两次部署（先只改策略并登记策略演进，再登记常规演进）。"
            )
    from_summary = wire_transition.identity_summary(from_identity)
    to_summary = wire_transition.identity_summary(current)
    if from_summary != effective["summary"]:
        raise ConfigurationError("有效工具身份摘要自相矛盾，拒绝登记。")
    if to_summary == from_summary:
        raise ConfigurationError("当前受管工具与 Campaign 有效工具身份相同，无需登记演进。")
    quiet = orchestrator._tool_evolution_quiescence_problems(campaign_dir)
    if quiet:
        raise ConfigurationError("登记工具演进要求 Campaign 静默：" + "；".join(quiet))
    adapter = upgrade_client_adapter.adapter_for(CLIENT)
    with reconciler._client_scope(campaign_dir, adapter):
        try:
            deployment = reconciler._deployment_receipt(reconciler._control_root(campaign_dir, control_root), current, required=True)
        except (reconciler.ReconcilerError, adapter.host_layout.VC0CloseoutError) as error:
            raise ConfigurationError(f"工具演进缺少部署绑定：{error}") from error
    assert deployment is not None
    if chain:
        previous_deployment = chain[-1]["bindings"]["deployment_receipt"]
        if orchestrator._rfc3339_datetime(str(deployment["created_at_utc"]), "部署收据 created_at_utc") <= orchestrator._rfc3339_datetime(
            str(previous_deployment["created_at_utc"]), "上一演进部署收据 created_at_utc"
        ):
            raise ConfigurationError("当前部署收据不晚于上一次演进绑定的部署，拒绝登记。")
    policy_transition: dict[str, Any] | None = None
    if policy_changed:
        policy_transition = _policy_evolution_transition(
            from_identity,
            current,
            deployment=deployment,
            compatibility_receipt=compatibility_receipt,
            activation_certification=activation_certification,
        )
    elif compatibility_receipt is not None or activation_certification is not None:
        raise ConfigurationError("工具身份策略未变化，不得提供策略兼容收据或激活认证。")
    ledger_dir = orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    try:
        ledger = timing_ledger.inspect_ledger(ledger_dir)
    except timing_ledger.TimingLedgerError as error:
        raise ConfigurationError(f"UpgradeTimingLedger 无法重放：{error}") from error
    if ledger.get("status") in {"stopped", "complete", "abandoned"}:
        raise ConfigurationError(f"Campaign 账本已是 {ledger.get('status')}，不能再登记工具演进。")
    project_root = project_ledger.find_project_ledger(campaign_dir)
    if project_root is not None:
        try:
            head = project_ledger.replay_head(project_root)
        except project_ledger.ProjectLedgerError as error:
            raise ConfigurationError(f"项目总账无法重放：{error}") from error
        blocking = project_ledger.campaign_blocked(head, str(manifest["campaign_id"]))
        if blocking:
            raise ConfigurationError(f"项目总账 blocked（未决账务 {blocking}），先以 accounting-resolve 补清请求账务再登记工具演进。")

    # 变化路径：按当前 Claude 策略分层（策略未变时即 Campaign 有效策略）。
    try:
        policy = claude_tool_identity_policy.load_policy()
    except claude_tool_identity_policy.ToolIdentityPolicyError as error:
        raise ConfigurationError(f"Claude 工具身份策略无法加载：{error}") from error
    drift = claude_tool_identity_policy.codex_identity.layer_drift(policy, list(from_identity["entries"]), list(current["entries"]))
    wire_closure_changed = from_summary["wire_closure_sha256"] != to_summary["wire_closure_sha256"]
    impact_paths = sorted(set(drift["wire_producer"]))
    evaluation_paths = sorted(set(drift["evidence_semantics"]))

    # 候选侧（B4-04）：活跃候选的评估器授权迁移按 Codex 同一口径记录；候选已有取证 attempt 时它的作业影响由 B4-01 落实。
    from tools.official_client_capture import claude_upgrade_baseline

    active_candidates, inactive_candidates = claude_upgrade_baseline.evolution_candidates(campaign_dir)
    for candidate_id in active_candidates:
        if claude_upgrade_baseline.candidate_has_capture_attempts(campaign_dir, candidate_id):
            raise orchestrator._pending(f"候选 {candidate_id} 取证作业的工具演进影响", "B4-01（候选采集）")
    evaluator_candidates = claude_upgrade_baseline.evaluator_candidate_records(campaign_dir, active_candidates)
    official_jobs = orchestrator._campaign_jobs(campaign_dir, manifest, "official")
    path_map: dict[str, set[str]] = {}
    for job in official_jobs:
        for declared in getattr(job, "tool_dependencies", ()) or ():
            path_map.setdefault(capture._normalized_tool_dependency(str(declared)), set()).add(job.job_id)
    unmapped = sorted(path for path in impact_paths if path not in path_map)
    mapped_affected: set[str] = set()
    for path in [*impact_paths, *drift["ignored"]]:
        mapped_affected.update(path_map.get(path, set()))
    planned_ids = sorted(job.job_id for job in official_jobs)
    all_affected = wire_closure_changed or bool(unmapped)
    affected = list(planned_ids) if all_affected else sorted(set(planned_ids) & mapped_affected)
    sealed = orchestrator._stage_path(campaign_dir, "capture-official", None)[1].exists()
    official_part = {
        "planned_job_ids": planned_ids,
        "affected_job_ids": affected,
        "basis": (
            "编排器 wire 闭包变化或存在映射不到作业的 wire 层变化路径，全部受影响"
            if all_affected
            else "wire 层变化路径与被作业声明依赖的其它客户端文件 ∩ 作业声明依赖"
        ),
        "sealed": sealed,
    }
    if sealed and affected:
        raise ConfigurationError(
            "修改影响已封存的 official 作业，Campaign 内无法重采官方证据："
            + "、".join(affected)
            + (f"（映射不到作业的变化路径：{'、'.join(unmapped)}）" if unmapped else "")
            + ("（编排器 wire 闭包变化）" if wire_closure_changed else "")
            + "。请核对修改范围。"
        )
    from_evaluator = evaluator_state(from_identity, policy)
    to_evaluator = evaluator_state(current, policy)
    if from_evaluator["status"] == EVALUATOR_NOT_ESTABLISHED and to_evaluator["status"] == EVALUATOR_NOT_ESTABLISHED:
        changed_fields: list[str] = []
    elif from_evaluator["status"] != to_evaluator["status"]:
        changed_fields = list(EVALUATOR_ENTRY_FIELDS)
    else:
        changed_fields = sorted(field for field in EVALUATOR_ENTRY_FIELDS if from_evaluator[field] != to_evaluator[field])
    preview: dict[str, Any] = {
        "schema_version": wire_transition.EVOLUTION_SCHEMA,
        "index": len(chain) + 1,
        "campaign_id": str(manifest["campaign_id"]),
        "campaign_manifest_sha256": file_sha256(campaign_dir / "campaign.json"),
        "previous_evolution_sha256": chain[-1]["receipt_sha256"] if chain else None,
        "from_summary": from_summary,
        "to_summary": to_summary,
        "to_identity": current,
        "to_evaluator_digests": _to_evaluator_digests(to_evaluator, policy),
        "changes": {
            "paths_by_layer": {layer: list(paths) for layer, paths in sorted(drift.items())},
            "impact_paths": impact_paths,
            "evaluation_paths": evaluation_paths,
            "unmapped_paths": unmapped,
            "wire_closure_changed": wire_closure_changed,
            "evidence_closure_changed": from_summary["evidence_closure_sha256"] != to_summary["evidence_closure_sha256"],
        },
        "impact": {"official": official_part, "candidates": {}, "inactive_candidates": inactive_candidates},
        "evaluator": {
            "from": from_evaluator,
            "to": to_evaluator,
            "changed_fields": changed_fields,
            "established_in_this_evolution": (
                from_evaluator["status"] == EVALUATOR_NOT_ESTABLISHED and to_evaluator["status"] == EVALUATOR_ESTABLISHED
            ),
            "candidates": evaluator_candidates,
        },
        "bindings": {"fix_commit": str(fix_commit), "deployment_receipt": dict(deployment)},
        **({"policy_transition": policy_transition} if policy_transition is not None else {}),
        "campaign_ledger_head": {
            "sequence": ledger.get("head_sequence"),
            "sha256": ledger.get("head_sha256"),
            "status": ledger.get("status"),
        },
        "reason": reason.strip(),
        "live_request_count": 0,
    }
    preview["review_sha256"] = orchestrator._fingerprint(preview)
    return preview


# ---------------------------------------------------------------------------
# Store 侧登记
# ---------------------------------------------------------------------------


def _require_store_campaign_established(campaign_dir: Path, manifest: Mapping[str, Any]) -> None:
    """登记工具演进之前，Campaign 的 Store Campaign 必须已经建立（拒绝时零写入）。

    Store 建 Campaign v2 时要求冻结的工具身份等于当前受管代码现算值（B1-12）：工具一旦变化（这正是要登记演进的原因），
    冻结身份就再也不等于现算值，之后 Store Campaign 永远建不出来，seal 也就永远走不下去。生产中 Store Campaign 由 VC-0
    收口建 Campaign 时一并建立（B2-11）；这里在演进登记处失败关闭，免得登记之后再发真实请求、到 seal 才发现无法封存。
    """

    from tools.official_client_capture import claude_upgrade_store

    root = claude_upgrade_store.campaign_store_root(campaign_dir)
    if not (root / "store.json").is_file() or not (root / "campaigns" / f"{manifest['campaign_id']}.json").is_file():
        raise ConfigurationError(
            "tool-evolution 拒绝：Campaign 的 Store Campaign 尚未建立。Store 要求建 Campaign 时冻结的工具身份等于现算值，"
            "工具变化之后无法再建立；Store Campaign 须在工具变化之前建立（VC-0 收口建 Campaign 时一并建立，B2-11）。"
            "本次未写入任何文件。"
        )
    # 已建立时照常打开并核对与总计划同源（不同源、损坏等原样报错）。
    claude_upgrade_store.open_campaign_store(campaign_dir, manifest)


def ensure_store_tool_registration(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    evolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """让 Campaign Store 的有效工具身份等于当前受管代码：不等时登记一份 ``tool_evolution_registration``。

    只在编排器链已经承接当前代码（最新演进的 to 身份就是当前身份）时登记，绑定那份编排器演进收据。调用时机：tool-evolution
    批准写出编排器演进收据之后；seal 打开 Store 时再调一次，补登「编排器收据已写、Store 登记之前中断」的那一份。Store 尚未
    建立时返回 ``store_absent``（tool-evolution 已在登记前拒绝这种 Campaign，见 ``_require_store_campaign_established``）。
    幂等：Store 已是当前身份返回 ``current``。
    """

    from tools.official_client_capture import claude_upgrade_seal as seal
    from tools.official_client_capture import claude_upgrade_store
    from tools.official_client_control import receipts as store_receipts
    from tools.official_client_control.errors import ControlError

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    root = claude_upgrade_store.campaign_store_root(campaign_dir)
    if not (root / "store.json").is_file():
        return {"status": "store_absent", "reason": "Campaign Store 尚未建立：首次 seal 建立 Store Campaign 后补登"}
    store, campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    try:
        effective = store_receipts.effective_tool_identity(store, dict(campaign))
        chain = store_receipts.tool_evolutions(store, dict(campaign))
        if effective is not None and effective["tool_files_sha256"] == store_receipts.current_tool_manifest(CLIENT)["tool_files_sha256"]:
            # 与 seal 的 _require_store_tool_current 同一判据（整树摘要相同即当前身份），不必现算三层摘要。
            return {"status": "current", "index": len(chain)}
        current = store_receipts.current_tool_identity(CLIENT)
    except ControlError as error:
        raise ConfigurationError(f"Store 工具身份无法核对：{error}") from error
    if effective is None:
        raise ConfigurationError("Claude Campaign 的 Store Campaign 必须是 v2（带七项工具身份）。")
    evolutions = orchestrator._campaign_tool_evolutions(campaign_dir, manifest)
    latest = dict(evolution) if evolution is not None else (evolutions[-1] if evolutions else None)
    if latest is None or not evolutions or latest.get("receipt_sha256") != evolutions[-1].get("receipt_sha256"):
        raise ConfigurationError("当前受管代码不是 Store 有效工具身份，且编排器侧没有可绑定的最新工具演进：先登记 tool-evolution。")
    current_tool = orchestrator._tool_identity(include_git=False)
    orchestrator._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action="Store 工具演进登记")
    evolution_path = campaign_dir / wire_transition.EVOLUTION_DIR / f"evolution-{int(latest['index']):02d}.json"
    payload = {
        "client": CLIENT,
        "campaign_id": str(campaign["campaign_id"]),
        "index": len(chain) + 1,
        "from_identity": dict(effective),
        "to_identity": dict(current),
        "evolution_receipt": {
            "path": evolution_path.relative_to(campaign_dir).as_posix(),
            "sha256": file_sha256(evolution_path),
        },
        "approved_by": str(latest["approved_by"]),
        "approved_at_utc": str(latest["approved_at_utc"]),
        "previous_ref": dict(chain[-1][0]) if chain else None,
    }
    # 输入临时文件放在 Store 根之外（与 seal 的 _StoreSession 同口径：official/seal/<用途>/store-inputs），
    # 避免 Store 根下出现非 Store 管理的条目、干扰 Store 的重放与完整性核对。
    scratch = ensure_private_directory(campaign_dir / seal.SEAL_ROOT / "tool-evolution" / seal.STORE_SCRATCH_DIRNAME)
    descriptor, name = tempfile.mkstemp(prefix=".registration-", suffix=".json", dir=scratch)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        outcome = seal.store_cli(
            ["tool-evolution", "--store", root, "--campaign", str(campaign["campaign_id"]), "--input", path, "--allow-existing"]
        )
    finally:
        path.unlink(missing_ok=True)
    return {
        "status": "registered",
        "index": payload["index"],
        "registration_ref": dict(outcome["registration_ref"]),
        "reused": bool(outcome.get("reused")),
        "evolution_receipt": payload["evolution_receipt"],
    }


# ---------------------------------------------------------------------------
# tool-evolution 子命令（Claude 版：编排器链 + Store 登记）
# ---------------------------------------------------------------------------


def tool_evolution_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """``tool-evolution``：不带 ``--approve-sha256`` 只预览；批准后写编排器演进收据，再在 Campaign Store 登记同一身份。

    以同一批准重跑即续接：编排器收据已写（最新一份的 ``approved_sha256`` 就是这次批准）时不再重算预览，只补 Store 登记。
    """

    orchestrator = _orchestrator()
    campaign_dir = Path(arguments.campaign_dir)
    manifest = orchestrator._require_formal_campaign(campaign_dir)
    approval = getattr(arguments, "approve_sha256", None)
    with orchestrator._campaign_lock(campaign_dir):
        _require_store_campaign_established(campaign_dir, manifest)
        chain = orchestrator._campaign_tool_evolutions(campaign_dir, manifest)
        if approval is not None and chain and chain[-1].get("approved_sha256") == str(approval):
            latest = chain[-1]
            path = campaign_dir / wire_transition.EVOLUTION_DIR / f"evolution-{int(latest['index']):02d}.json"
            reused = True
            summary = _evolution_summary(latest, review_sha256=str(latest["approved_sha256"]))
        else:
            preview = orchestrator._tool_evolution_preview(
                campaign_dir,
                manifest,
                fix_commit=str(arguments.fix_commit),
                reason=str(arguments.reason),
                control_root=getattr(arguments, "control_root", None),
                compatibility_receipt=getattr(arguments, "policy_compatibility_receipt", None),
                activation_certification=getattr(arguments, "policy_activation_certification", None),
            )
            summary = _evolution_summary(preview, review_sha256=str(preview["review_sha256"]))
            if approval is None:
                return {"status": "approval_required", **summary}
            if str(approval) != preview["review_sha256"]:
                raise ConfigurationError("批准摘要与重算的工具演进预览不一致；重新预览后再批准。")
            approved_by = str(getattr(arguments, "approved_by", "") or "").strip()
            if not approved_by:
                raise ConfigurationError("批准工具演进必须提供 --approved-by。")
            payload = {key: value for key, value in preview.items() if key != "review_sha256"}
            payload["approved_sha256"] = preview["review_sha256"]
            payload["approved_by"] = approved_by
            payload["approved_at_utc"] = orchestrator._utc_now()
            payload["receipt_sha256"] = orchestrator._fingerprint(payload)
            try:
                path = write_evolution(campaign_dir, manifest, payload)
            except wire_transition.WireTransitionError as error:
                raise ConfigurationError(str(error)) from error
            latest = payload
            reused = False
        store = ensure_store_tool_registration(campaign_dir, manifest, evolution=latest)
    return {
        "status": "evolution_applied",
        "reused": reused,
        "path": str(path),
        "receipt_sha256": str(latest["receipt_sha256"]),
        "store_registration": store,
        **summary,
    }


def _evolution_summary(payload: Mapping[str, Any], *, review_sha256: str) -> dict[str, Any]:
    return {
        "index": payload["index"],
        "from_summary": payload["from_summary"],
        "to_summary": payload["to_summary"],
        "changes": payload["changes"],
        "impact": payload["impact"],
        "evaluator": payload["evaluator"],
        "bindings": payload["bindings"],
        "policy_transition": payload.get("policy_transition"),
        "review_sha256": review_sha256,
        "live_request_count": 0,
    }
