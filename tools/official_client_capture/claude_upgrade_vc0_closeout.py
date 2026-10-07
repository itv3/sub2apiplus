#!/usr/bin/env python3
"""Claude VC-0 收口（Claude 升级控制面设计 B2-11）：重放四份输入、建 Formal Campaign 并立即派发首个 VC-1 批次。

一、共享与画像
==============
收口流程只有一份，在 ``codex_upgrade_vc0_closeout``：只读现场判定（Formal 未建／已建未派发／已派发／判定不了）、
收口 attempt、收据副本组、建 Formal、控制产物副本、总账注册推送、「VC-0 完成」「VC-1 开始」两条事件、首批派发、
已派发之后的对账与恢复批次、上限后的账本恢复与收口收据，失败留不可覆盖诊断，同一 Formal ID 重跑即续作。本模块是
它的 Claude 画像（适配器成员 ``closeout_profile``）与 Claude 入口：

* ``client_scope``：同时设定共享控制流程（``upgrade_control_plane``，连带对账器）与收口流程的客户端作用域，流程里的
  ``adapter.orchestrator`` 因此是 Claude 编排器（``load_campaign_manifest``、``create_campaign``、``compile_and_run_vc_batch``、
  工具身份与演进静止等），项目总账按 Claude 的目录名查找；
* 合同名：``official-client-vc0-closeout-*``（收口收据、失败诊断、现场、上限后恢复预览、首批交接）；
* ``validate_inputs``：四份输入按 Claude 合同重放（第二节）；
* ``recover_formal_plan_arguments``：从预检 Campaign 恢复 Formal ``plan`` 参数（第三节）。

二、四份输入（角色与 Codex 相同：arm64_environment、managed_tool_deploy、p0_gate、release_certification）
======================================================================================================
* 预检 Campaign：Claude 总计划、``preflight_only``；冻结的计时绑定是全形，账本属于 Claude、现状是 active VC-0、剩余时间
  不少于 300 秒、累计真实请求与冻结 checkpoint 相同；
* ARM64 环境收据：Claude 合同（``claude_upgrade_arm64_environment_receipt``）重放通过、P0 阶段、主体是账本的升级 ID，
  合同与连续性摘要等于预检冻结值；
* 受管工具部署收据：Claude 合同 ``official-client-managed-deploy/v1``（``load_deployment_receipt``：通过、``client`` 相符、
  五摘要与策略版本等于当前工具身份），ARM64 上执行（架构 aarch64）、部署的数据根就是预检所在数据根、本收口运行的工具树
  就是部署的生产工具树，部署监督器 run 形成完整的 bootstrap 终态；
* 发布认证：Claude 的 ``tool-release-certification/v2``（``certify_release.verify(client="claude")``），五摘要与策略版本等于
  部署收据，绑定的部署收据就是这一份；它绑定的 Job 演练收据按 Claude 合同重放、与预检总计划算出的执行合同兼容
  （``claude_upgrade_campaign.job_rehearsal_contract``）、绑定的就是这个预检 Campaign（路径、ID、清单摘要、模式）；
* P0 门禁收据：两客户端同一合同（``codex-upgrade-vc-receipt/v1``），按共享 ``_validate_p0_gate`` 重放（本轮升级、用途、
  版本、这份发布认证、证据角色闭合与离线门禁证据重验）。

三、Formal plan 参数
====================
全部取自预检 Campaign：取证坐标、Store 身份四项与冻结收据坐标取 ``configuration``；场景清单、证据标签、Active 两件、
静态基线两件与生产现状清单取预检的冻结副本（两份 Campaign 逐字节同源）；基线规则清单必须是仓库里的那一份（目标规则
清单按仓库路径绑定它），其摘要必须等于预检冻结的副本；前序只读复用沿用预检冻结的绑定（``predecessor_binding``）；
控制收据是收口这一组：新写的 active VC-0 checkpoint、预检的环境收据、发布认证绑定的 Job 演练、P0 门禁与发布认证。

四、命令行
==========
与 Codex 收口同一组子命令与参数：缺省是收口（含续作），``inspect`` 只读判定现场，``ledger-resume`` 是 Formal 未建时
同根因到上限后的账本恢复；退出码相同（需要人工处理返回 3）。Codex 的两个历史修复子命令（``repair-failure-accounting``、
``repair-failure-closure``）只修 Codex 旧失败记录里的漏记，Claude 没有这类记录，明确拒绝。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc0_closeout as shared
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane

CLIENT = "claude"
CLOSEOUT_RECEIPT_SCHEMA = "official-client-vc0-closeout-receipt/v1"
CLOSEOUT_DIAGNOSTIC_SCHEMA = "official-client-vc0-closeout-diagnostic/v1"
SITE_SCHEMA = "official-client-vc0-closeout-site/v1"
LEDGER_RESUME_PREVIEW_SCHEMA = "official-client-vc0-ledger-resume-preview/v1"
VC1_HANDOVER_SCHEMA = "official-client-vc0-vc1-handover/v1"
ORCHESTRATOR_CLI_MODULE = "tools.official_client_capture.claude_upgrade"
EXIT_NEEDS_OPERATOR = shared.EXIT_NEEDS_OPERATOR
VC0CloseoutError = shared.VC0CloseoutError
CloseoutBlocked = shared.CloseoutBlocked
REJECTED_SUBCOMMANDS = ("repair-failure-accounting", "repair-failure-closure")
TOOL_ROOT_RELATIVE = Path("tools") / "official_client_capture"


@contextmanager
def client_scope() -> Iterator[Any]:
    """Claude 的客户端作用域：共享控制流程（连带对账器）与收口流程同时取 Claude 适配器。"""

    adapter = upgrade_client_adapter.adapter_for(CLIENT)
    with upgrade_control_plane.adapter_scope(adapter), shared.adapter_scope(adapter):
        yield adapter


# ---------------------------------------------------------------------------
# 二、四份输入
# ---------------------------------------------------------------------------


def _validate_timing_and_environment(
    preflight_dir: Path,
    manifest: Mapping[str, Any],
    *,
    now: str,
) -> tuple[Path, Path, Path, dict[str, Any], shared.ReceiptSource]:
    """预检冻结的计时 checkpoint 与 ARM64 环境收据（与 Codex ``_validate_timing_and_arm64`` 同一判据，环境收据是 Claude 合同）。"""

    from tools.official_client_capture import claude_upgrade_arm64_environment_receipt as environment_module
    from tools.official_client_capture import claude_upgrade_campaign

    del preflight_dir
    controls = shared._expect(manifest.get("control_receipts"), {"upgrade_timing", "arm64_environment"}, "preflight control_receipts")
    timing = shared._expect(controls["upgrade_timing"], set(claude_upgrade_campaign.TIMING_FULL_FIELDS), "preflight upgrade_timing")
    timing_root = shared._private_directory(Path(str(timing["ledger_dir"])), "UpgradeTimingLedger")
    timing_path = shared._bound_control_file(timing_root, timing["receipt"], "preflight timing checkpoint")
    if shared._sha256_file(timing_root / "ledger.json") != shared._sha256(timing["ledger_plan_sha256"], "ledger_plan_sha256"):
        raise VC0CloseoutError("UpgradeTimingLedger 计划摘要漂移")
    try:
        frozen = codex_upgrade_timing_ledger.replay(timing_root, timing_path.relative_to(timing_root).as_posix())
        summary = codex_upgrade_timing_ledger.inspect_ledger(timing_root, now=now)
        plan, _raw = codex_upgrade_timing_ledger._load_json(timing_root / "ledger.json", "计时账本计划")
        client = codex_upgrade_timing_ledger.plan_client(plan)
    except (OSError, ValueError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise VC0CloseoutError(f"UpgradeTimingLedger 重放失败：{error}") from error
    if client != CLIENT:
        raise VC0CloseoutError("预检绑定的计时账本不属于 Claude")
    if frozen.get("summary", {}).get("head_sha256") != timing["checkpoint_head_sha256"]:
        raise VC0CloseoutError("preflight timing checkpoint head 漂移")
    frozen_summary = frozen.get("summary")
    frozen_live_requests = frozen_summary.get("total_live_request_count") if isinstance(frozen_summary, Mapping) else None
    if not isinstance(frozen_live_requests, int) or isinstance(frozen_live_requests, bool) or frozen_live_requests < 0:
        raise VC0CloseoutError("preflight timing checkpoint 请求累计值非法")
    shared._assert_active_vc0(
        summary,
        manifest,
        upgrade_id=str(timing["upgrade_id"]),
        evidence_decision=str(timing["evidence_decision"]),
        expected_total_live_request_count=frozen_live_requests,
        now=now,
    )
    environment = shared._expect(
        controls["arm64_environment"],
        {"evidence_root", "receipt", "subject_id", "contract_sha256", "continuity_identity_sha256"},
        "preflight arm64_environment",
    )
    environment_root = shared._private_directory(Path(str(environment["evidence_root"])), "ARM64 环境证据根")
    environment_path = shared._bound_control_file(environment_root, environment["receipt"], "ARM64 环境收据")
    try:
        receipt = environment_module.replay(environment_root, environment_path.relative_to(environment_root).as_posix())
    except (OSError, environment_module.Arm64EnvironmentReceiptError) as error:
        raise VC0CloseoutError(f"ARM64 环境收据重放失败：{error}") from error
    if (
        receipt.get("status") != "passed"
        or receipt.get("phase") != "p0"
        or receipt.get("subject_id") != summary["upgrade_id"]
        or receipt.get("contract_sha256") != environment["contract_sha256"]
        or receipt.get("continuity_identity_sha256") != environment["continuity_identity_sha256"]
    ):
        raise VC0CloseoutError("ARM64 环境收据与 preflight 或时间账本不一致")
    return timing_root, environment_root, environment_path, summary, shared._binding_source("arm64_environment", environment_path)


def _current_tool_root() -> Path:
    """本收口实际运行的受管工具树（生产上就是部署收据登记的数据根下的 ``tools/official_client_capture``）。"""

    return Path(__file__).resolve().parent


def _validate_managed_tool_deploy(path: Path, preflight_dir: Path) -> tuple[shared.ReceiptSource, dict[str, Any]]:
    """Claude 受管部署收据：合同、通过、client、五摘要与策略版本等于当前工具身份；ARM64、数据根、工具树与部署监督器终态。"""

    from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification

    receipt_path = shared._trusted_file(Path(path), "受管工具部署收据")
    try:
        receipt = policy_certification.load_deployment_receipt(
            receipt_path, expected_identity=policy_certification.current_identity(CLIENT), client=CLIENT
        )
    except (OSError, ValueError, policy_certification.PolicyCertificationError) as error:
        raise VC0CloseoutError(f"受管工具部署收据未通过：{error}") from error
    data_root = shared._formal_host_data_root(preflight_dir)
    production_data_root = Path(str(receipt.get("production_data_root", "")))
    if (
        receipt.get("architecture") != "aarch64"
        or platform.machine() != "aarch64"
        or not production_data_root.is_absolute()
        or production_data_root.resolve(strict=False) != data_root
        or _current_tool_root() != (data_root / TOOL_ROOT_RELATIVE).resolve(strict=False)
    ):
        raise VC0CloseoutError("受管工具部署收据未绑定当前 ARM64 数据根与本收口运行的生产工具树")
    run_dir = Path(str(receipt.get("supervisor_run_dir", "")))
    try:
        run_state = codex_upgrade_supervisor._read_state(run_dir)
        audit = codex_upgrade_supervisor._audit_command(run_dir)
    except (OSError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
        raise VC0CloseoutError(f"受管工具部署监督器审计失败：{error}") from error
    if (
        run_state.get("campaign_id") != receipt.get("campaign_id")
        or run_state.get("phase") != "bootstrap"
        or run_state.get("state") != "stopped"
        or audit.get("run_dir") != str(run_dir.resolve(strict=True))
        or audit.get("state") != "stopped"
        or audit.get("audit_incomplete") is not False
    ):
        raise VC0CloseoutError("受管工具部署监督器未形成完整终态")
    return shared._binding_source("managed_tool_deploy", receipt_path), dict(receipt)


def _validate_job_rehearsal(
    preflight_dir: Path,
    manifest: Mapping[str, Any],
    root: Path,
    receipt_path: Path,
) -> tuple[Path, Path, shared.ReceiptSource]:
    """发布认证绑定的 Job 演练：Claude 合同重放、与预检总计划的执行合同兼容、绑定的就是这个预检 Campaign。"""

    from tools.official_client_capture import claude_upgrade_campaign
    from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as job_rehearsal
    from tools.official_client_capture.capturelib.model import ConfigurationError

    evidence_root = shared._private_directory(root, "Job rehearsal 证据根")
    path = shared._inside(evidence_root, receipt_path, "Job rehearsal 收据")
    try:
        receipt = job_rehearsal.replay(evidence_root, path.relative_to(evidence_root).as_posix(), client=CLIENT)
        expected_contract = claude_upgrade_campaign.job_rehearsal_contract(preflight_dir, manifest)
        job_rehearsal.assert_formal_compatible(receipt, expected_contract)
    except (OSError, ValueError, ConfigurationError, job_rehearsal.JobRehearsalReceiptError) as error:
        raise VC0CloseoutError(f"Job rehearsal 收据重放失败：{error}") from error
    preflight = receipt.get("preflight_campaign")
    if (
        not isinstance(preflight, Mapping)
        or Path(str(preflight.get("path", ""))).resolve(strict=False) != preflight_dir
        or preflight.get("campaign_id") != manifest.get("campaign_id")
        or preflight.get("manifest_sha256") != shared._sha256_file(preflight_dir / "campaign.json")
        or preflight.get("campaign_mode") != "preflight_only"
    ):
        raise VC0CloseoutError("Job rehearsal 未绑定当前 preflight Campaign")
    return evidence_root, path, shared._binding_source("job_rehearsal", path)


def _validate_release_certification(
    path: Path,
    deploy_receipt: Mapping[str, Any],
    deploy_source: shared.ReceiptSource,
    preflight_dir: Path,
    manifest: Mapping[str, Any],
) -> tuple[Path, Path, shared.ReceiptSource]:
    """Claude 发布认证（v2）：五摘要与策略版本等于部署收据、绑定的部署收据就是这一份；从中恢复 Job 演练绑定。"""

    from tools.official_client_capture import certify_release
    from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification

    certification_path = shared._trusted_file(Path(path), "发布认证")
    try:
        certification = certify_release.verify(certification_path, client=CLIENT)
    except (OSError, ValueError, certify_release.ReleaseCertificationError) as error:
        raise VC0CloseoutError(f"发布认证校验失败：{error}") from error
    deployed = policy_certification.deployment_receipt_identity(deploy_receipt, CLIENT)
    identity = certification.get("identity") or {}
    if (
        any(str(deployed.get(field)) != str(identity.get(field)) for field in policy_certification.IDENTITY_FIELDS)
        or deployed.get("policy_version") != certification.get("policy_version")
    ):
        raise VC0CloseoutError("发布认证五摘要或策略版本与受管工具部署收据不一致")
    bound = certification.get("deployment_receipt") or {}
    if (
        Path(str(bound.get("path", ""))).resolve(strict=False) != deploy_source.path
        and bound.get("sha256") != deploy_source.sha256
    ):
        raise VC0CloseoutError("发布认证绑定的部署收据不是本次收口提供的部署收据")
    job_binding = certification.get("job_rehearsal") or {}
    job_root, job_path, job_source = _validate_job_rehearsal(
        preflight_dir,
        manifest,
        Path(str(job_binding.get("evidence_root", ""))),
        Path(str(job_binding.get("receipt", ""))),
    )
    if job_source.sha256 != job_binding.get("sha256"):
        raise VC0CloseoutError("发布认证绑定的 Job rehearsal 收据摘要漂移")
    return job_root, job_path, shared._binding_source("release_certification", certification_path)


def validate_inputs(
    *,
    preflight_campaign_dir: Path,
    p0_gate_root: Path,
    p0_gate_receipt: Path,
    managed_tool_deploy_receipt: Path,
    release_certification: Path,
    now: str | None = None,
) -> shared.ValidatedInputs:
    """收口画像成员：重放四份输入并返回创建 Formal 所需的规范坐标（判据见模块说明第二节）。"""

    observed = now or shared._utc_now()
    preflight_dir, manifest = shared._load_preflight(Path(preflight_campaign_dir))
    if manifest.get("client") != CLIENT:
        raise VC0CloseoutError("Claude 收口只能消费 Claude 预检 Campaign")
    timing_root, environment_root, environment_path, timing_summary, environment_source = _validate_timing_and_environment(
        preflight_dir, manifest, now=observed
    )
    deploy_source, deploy_receipt = _validate_managed_tool_deploy(Path(managed_tool_deploy_receipt), preflight_dir)
    job_root, job_path, release_source = _validate_release_certification(
        Path(release_certification), deploy_receipt, deploy_source, preflight_dir, manifest
    )
    p0_root, p0_path, p0_source = shared._validate_p0_gate(
        Path(p0_gate_root), Path(p0_gate_receipt), manifest, timing_summary, release_source.path
    )
    receipts = tuple(sorted((environment_source, deploy_source, p0_source, release_source), key=lambda item: item.role))
    if tuple(item.role for item in receipts) != shared.INPUT_ROLES:
        raise VC0CloseoutError("VC-0 四份输入角色不闭合")
    return shared.ValidatedInputs(
        preflight_dir=preflight_dir,
        preflight_manifest=dict(manifest),
        timing_ledger_dir=timing_root,
        arm64_root=environment_root,
        arm64_receipt=environment_path,
        job_rehearsal_root=job_root,
        job_rehearsal_receipt=job_path,
        p0_gate_root=p0_root,
        p0_gate_receipt=p0_path,
        release_certification=release_source.path,
        receipts=receipts,
        timing_summary=dict(timing_summary),
    )


# ---------------------------------------------------------------------------
# 三、Formal plan 参数
# ---------------------------------------------------------------------------

# 从预检 configuration 原样恢复的 plan 参数（旗标 → 字段）。
CONFIGURATION_FLAGS = (
    ("--runtime-image", "runtime_image"),
    ("--model", "model"),
    ("--upstream-ip", "upstream_ip"),
    ("--suite", "suite"),
    ("--claude-bin", "claude_bin"),
    ("--capture-root", "capture_root"),
    ("--capture-tool-root", "capture_tool_root"),
    ("--capture-container", "capture_container"),
    ("--synthetic-capture-container", "synthetic_capture_container"),
    ("--credentials-file", "credentials_file"),
    ("--ca-signing-pem", "ca_signing_pem"),
    ("--ca-cert", "ca_cert"),
    ("--target-package", "official_freeze_path"),
    ("--target-package-sha256", "official_freeze_sha256"),
    ("--entrypoint", "entrypoint"),
    ("--privacy-mode", "privacy_mode"),
    ("--oauth-org-id", "oauth_org_id"),
    ("--model-catalog-sha256", "model_catalog_sha256"),
    ("--source-commit", "source_commit"),
)
# 取预检冻结副本的 plan 参数（旗标 → 冻结输入名）。
FROZEN_INPUT_FLAGS = (
    ("--scenario-manifest", "target_scenarios"),
    ("--evidence-labels", "evidence_labels"),
    ("--active-release-catalog", "active_release_catalog"),
    ("--active-profile", "active_profile"),
    ("--static-baseline-anchors", "static_baseline_anchors"),
    ("--static-baseline-reachability", "static_baseline_reachability"),
    ("--production-inventory", "production_inventory"),
)


def recover_formal_plan_arguments(
    validated: shared.ValidatedInputs,
    *,
    formal_campaign_dir: Path,
    formal_campaign_id: str,
    timing_receipt: Path,
) -> argparse.Namespace:
    """收口画像成员：只从预检 Campaign 恢复完整的 Formal ``plan`` 参数（见模块说明第三节）。"""

    from tools.official_client_capture import claude_upgrade
    from tools.official_client_capture import claude_upgrade_plan

    manifest = validated.preflight_manifest
    configuration = manifest.get("configuration")
    inputs = manifest.get("inputs")
    if not isinstance(configuration, Mapping) or not isinstance(inputs, Mapping):
        raise VC0CloseoutError("preflight 缺少 configuration 或 inputs")

    def frozen(name: str) -> Path:
        reference = inputs.get(name)
        if not isinstance(reference, Mapping):
            raise VC0CloseoutError(f"preflight inputs.{name} 缺失")
        path = shared._inside(validated.preflight_dir, str(reference.get("path", "")), f"preflight inputs.{name}")
        if shared._sha256_file(path) != reference.get("sha256"):
            raise VC0CloseoutError(f"preflight inputs.{name} 摘要漂移")
        return path

    baseline_version = str(manifest.get("baseline_version", ""))
    baseline_rules = claude_upgrade_plan.baseline_rule_manifest_path(baseline_version)
    if not baseline_rules.is_file() or shared._sha256_file(baseline_rules) != shared._sha256_file(frozen("baseline_rules")):
        raise VC0CloseoutError("仓库里的基线规则清单与预检冻结的副本不一致（预检之后改过规则清单，须重建预检）")
    command = [
        "plan",
        "--campaign-dir", str(formal_campaign_dir),
        "--campaign-id", shared._safe_id(formal_campaign_id, "formal_campaign_id"),
        "--campaign-mode", "formal",
        "--campaign-purpose", str(manifest.get("campaign_purpose", "")),
        "--baseline-version", baseline_version,
        "--target-version", str(manifest.get("target_version", "")),
        "--target-sha256", str(manifest.get("target_sha256", "")),
        "--rule-manifest", str(baseline_rules),
        "--timing-ledger-dir", str(validated.timing_ledger_dir),
        "--timing-receipt", str(timing_receipt),
        "--arm64-environment-root", str(validated.arm64_root),
        "--arm64-environment-receipt", str(validated.arm64_receipt),
        "--job-rehearsal-root", str(validated.job_rehearsal_root),
        "--job-rehearsal-receipt", str(validated.job_rehearsal_receipt),
        "--p0-gate-root", str(validated.p0_gate_root),
        "--p0-gate-receipt", str(validated.p0_gate_receipt),
        "--release-certification", str(validated.release_certification),
    ]
    for flag, field in CONFIGURATION_FLAGS:
        value = configuration.get(field)
        if not isinstance(value, str) or not value:
            raise VC0CloseoutError(f"preflight configuration.{field} 缺失")
        command += [flag, value]
    for flag, name in FROZEN_INPUT_FLAGS:
        command += [flag, str(frozen(name))]
    try:
        arguments = claude_upgrade._build_parser().parse_args(command)
    except SystemExit as error:
        raise VC0CloseoutError("无法从 preflight 恢复完整 Formal plan 参数") from error
    # 前序只读复用：沿用预检冻结的绑定（命令行没有对应参数，经 Python 接口传给 create_campaign）。
    arguments.predecessor_binding = manifest.get("predecessor")
    return arguments


# ---------------------------------------------------------------------------
# 四、命令行
# ---------------------------------------------------------------------------


def closeout(arguments: argparse.Namespace) -> dict[str, Any]:
    """Claude 作用域里执行（或续作）共享收口。"""

    with client_scope():
        return shared.closeout(arguments)


def inspect(arguments: argparse.Namespace) -> dict[str, Any]:
    """Claude 作用域里的只读现场判定。"""

    with client_scope():
        site = shared.inspect_site(
            timing_root=(
                shared._private_directory(arguments.timing_ledger_dir, "UpgradeTimingLedger")
                if arguments.timing_ledger_dir is not None
                else shared._ledger_dir_from_preflight(arguments.preflight_campaign_dir)
            ),
            formal_campaign_id=shared._safe_id(arguments.formal_campaign_id, "formal_campaign_id"),
            formal_campaign_dir=arguments.formal_campaign_dir,
            supervisor_state_dir=arguments.supervisor_state_dir,
        )
        return shared._public_site(site)


def ledger_resume(arguments: argparse.Namespace) -> dict[str, Any]:
    """Claude 作用域里的上限后账本恢复（不带批准只预览）。"""

    with client_scope():
        return shared.ledger_resume(
            preflight_campaign_dir=arguments.preflight_campaign_dir,
            formal_campaign_id=arguments.formal_campaign_id,
            formal_campaign_dir=arguments.formal_campaign_dir,
            supervisor_state_dir=arguments.supervisor_state_dir,
            fix_commit=arguments.fix_commit,
            regression_receipt=arguments.regression_receipt,
            reason=arguments.reason,
            approve_sha256=arguments.approve_sha256,
            approved_by=arguments.approved_by,
            control_root=arguments.control_root,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = shared.build_parser()
    parser.description = __doc__.splitlines()[1]
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in REJECTED_SUBCOMMANDS:
        print(
            f"Claude VC-0 收口不提供 {argv[0]}：它只修 Codex 旧失败记录里的漏记，Claude 没有这类记录。",
            file=sys.stderr,
        )
        return 1
    if argv and argv[0] == "inspect":
        arguments = shared.build_inspect_parser().parse_args(argv[1:])
        try:
            result = inspect(arguments)
        except (OSError, ValueError, VC0CloseoutError) as error:
            print(f"Claude VC-0 收口现场判定失败：{error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if argv and argv[0] == "ledger-resume":
        arguments = shared.build_ledger_resume_parser().parse_args(argv[1:])
        try:
            result = ledger_resume(arguments)
        except (OSError, ValueError, VC0CloseoutError) as error:
            print(f"Claude VC-0 上限后恢复失败：{error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return EXIT_NEEDS_OPERATOR if result.get("status") == "approval_required" else 0
    arguments = build_parser().parse_args(argv)
    try:
        receipt = closeout(arguments)
    except CloseoutBlocked as blocked:
        # 需要人工批准或处置：不是失败（与 Codex 收口同一输出形状，入口编排器据此打印下一条命令）。
        print(f"Claude VC-0 收口续作需要处理：{blocked}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "kind": blocked.kind,
                    "message": str(blocked),
                    "next_command": blocked.next_command,
                    "details": blocked.details,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_NEEDS_OPERATOR
    except (OSError, ValueError, VC0CloseoutError) as error:
        print(f"Claude VC-0 收口失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
