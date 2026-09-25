"""R18 ②③：录制回放 VC-1 链的阶段驱动（测试夹具，不进受管身份）。

每个阶段由真实链用例经 ``vc1_recorded_replay.namespace_argv`` 在私有挂载＋网络命名空间内以
``python3 -m tools.official_client_capture.tests.vc1_recorded_chain_driver --tree <副本根> <阶段>`` 启动，
进程带回放状态环境变量，副本根 sitecustomize 已安装替身。阶段之间的事实写在 ``<副本根>/control/r18-chain``。

VC-0 收口：preflight 与 Formal Campaign 用录制 Campaign 的真实配置（0.156.1 源码、官方包、二进制、运行镜像、
模型、容器名）与合成控制收据（计时账本、ARM64 P0、Job 演练、发布认证、P0 门禁）真实创建；closeout 的输入
联合校验（validate_inputs）由单元测试覆盖、这里替换为同一批收据组成的 ValidatedInputs，其余收口步骤（入账、
checkpoint、恢复 formal plan 参数、建 Formal Campaign、账本推进、首批派发、失败收口）全部真跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture.tests import vc1_recorded_replay as replay

UPGRADE_ID = "codex-0154-to-01561-r18-replay"
PREFLIGHT_ID = "r18-replay-preflight"
CONTRACT_CONFIGURATION_FIELDS = (
    "runtime_image", "model", "lite_model", "capture_root", "capture_container", "service_container",
    "keeper_container", "postgres_container", "redis_container", "capture_codex_bin", "relay_codex_bin",
    "capture_code_mode_host_bin", "relay_code_mode_host_bin", "codex_account_id", "api_key_id",
    "live_attestation_compose_dir", "live_attestation_compose_files",
)


def _write(path: Path, payload: Any) -> Path:
    return replay._write(path, payload)


def _read(path: Path) -> Any:
    return replay._read(path)


def _chain_dir(tree: Path) -> Path:
    path = tree / "control" / "r18-chain"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _recorded_manifest() -> dict[str, Any]:
    return _read(replay.recorded_campaign_dir() / "campaign.json")


def _tool_root() -> Path:
    from tools.official_client_capture import codex_upgrade

    return Path(codex_upgrade.__file__).resolve().parent


def declared_target_scenarios(tree: Path) -> Path:
    """0.156.1 场景清单的 R16 声明副本：执行字段逐字不变，只补分析器生成的逐作业 tool_dependencies。"""

    from tools.official_client_capture.tests import job_dependency_analyzer as analyzer

    source = _tool_root() / "codex_upgrade_scenarios_0_156_1.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    declared = analyzer.analyze_manifest(source)
    for job in payload["capture_jobs"]:
        job["tool_dependencies"] = list(declared[job["id"]])
    target = tree / "control" / "r18-inputs" / "target-scenarios-0.156.1-declared.json"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target


def plan_arguments(
    *,
    campaign_dir: Path,
    campaign_id: str,
    mode: str,
    recorded: dict[str, Any],
    timing_root: Path,
    timing_receipt: Path,
    arm_root: Path,
    arm_receipt: Path,
    target_scenarios: Path,
    rehearsal_root: Path | None = None,
    rehearsal_receipt: Path | None = None,
    p0_root: Path | None = None,
    p0_receipt: Path | None = None,
    release: Path | None = None,
) -> argparse.Namespace:
    configuration = recorded["configuration"]
    package = recorded["official_identity"]["package"]
    tool_root = _tool_root()
    return argparse.Namespace(
        command="plan", campaign_dir=campaign_dir, output=None, dry_run=False, execute=False,
        acknowledge_live_requests=False,
        baseline_version=replay.BASELINE_VERSION, target_version=replay.TARGET_VERSION,
        campaign_mode=mode, campaign_purpose=recorded["campaign_purpose"],
        timing_ledger_dir=timing_root, timing_receipt=timing_receipt,
        arm64_environment_root=arm_root, arm64_environment_receipt=arm_receipt,
        job_rehearsal_root=rehearsal_root, job_rehearsal_receipt=rehearsal_receipt,
        p0_gate_root=p0_root, p0_gate_receipt=p0_receipt, release_certification=release,
        baseline_source=Path(configuration["baseline_source"]), target_source=Path(configuration["target_source"]),
        baseline_evidence=Path(configuration["baseline_evidence"]),
        target_sha256=recorded["target_sha256"], target_package=Path(configuration["target_package"]),
        target_package_sha256=package["asset_sha256"], target_code_mode_host_sha256=package["code_mode_host_sha256"],
        runtime_image=configuration["runtime_image"],
        rule_manifest=tool_root / "codex_upgrade_rules_0_154_0.json",
        scenario_manifest=tool_root / "codex_upgrade_scenarios_0_154_0.json",
        target_scenario_manifest=target_scenarios, extra_jobs=None, suite=recorded["suite"],
        campaign_id=campaign_id, model=configuration["model"], lite_model=configuration["lite_model"],
        capture_root=Path(configuration["capture_root"]), capture_container=configuration["capture_container"],
        service_container=configuration["service_container"], keeper_container=configuration["keeper_container"],
        postgres_container=configuration["postgres_container"], redis_container=configuration["redis_container"],
        capture_codex_bin=configuration["capture_codex_bin"], relay_codex_bin=configuration["relay_codex_bin"],
        capture_code_mode_host_bin=configuration["capture_code_mode_host_bin"],
        relay_code_mode_host_bin=configuration["relay_code_mode_host_bin"],
        codex_account_id=int(configuration["codex_account_id"]), api_key_id=int(configuration["api_key_id"]),
        live_attestation_compose_dir=configuration["live_attestation_compose_dir"],
        live_attestation_compose_files=configuration["live_attestation_compose_files"],
        candidate_id=None, candidate_purpose=None, profile_id=None, profile_digest=None, target_rule_manifest=None,
        migration_manifest=None, assertion_profile_manifest=None, approve_manifest_sha256=None, assertions=None,
    )


def stage_init(arguments: argparse.Namespace) -> dict[str, Any]:
    """VC-0 收口 + VC-1 首批（Formal Campaign 沿用录制 campaign_id）。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as rehearsal
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing
    from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
    from tools.official_client_capture.tests import control_receipt_fixtures as crf
    from tools.official_client_capture.tests import project_ledger_fixture

    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    state = _read(Path(os.environ[replay.REPLAY_STATE_ENV]))
    replay.install_orchestrator_patches(codex_upgrade, state)
    recorded = _recorded_manifest()
    campaigns = tree / "evidence" / "campaigns"
    control = tree / "control"
    for path in (tree / "evidence", campaigns, control, tree / "supervisor"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    project_ledger_fixture.install_fixture_ledger(tree)
    purpose = str(recorded["campaign_purpose"])
    timing_root = control / "UpgradeTimingLedger"
    timing_receipt = crf.create_timing_checkpoint(
        timing_root, upgrade_id=UPGRADE_ID, baseline_version=replay.BASELINE_VERSION,
        target_version=replay.TARGET_VERSION, campaign_purpose=purpose,
    )
    arm_root = control / "arm64-p0"
    arm_receipt = crf.create_arm_receipt(arm_root, phase="p0", subject_id=UPGRADE_ID, prefix="p0",
                                         rust_tls_codex_version=replay.TARGET_VERSION)
    target_scenarios = declared_target_scenarios(tree)
    preflight_dir = campaigns / PREFLIGHT_ID
    codex_upgrade.create_campaign(plan_arguments(
        campaign_dir=preflight_dir, campaign_id=PREFLIGHT_ID, mode="preflight_only", recorded=recorded,
        timing_root=timing_root, timing_receipt=timing_receipt, arm_root=arm_root, arm_receipt=arm_receipt,
        target_scenarios=target_scenarios,
    ))
    preflight_manifest = codex_upgrade.load_campaign_manifest(preflight_dir)
    identity = codex_upgrade._tool_identity()
    package = recorded["official_identity"]["package"]
    contract = rehearsal.build_execution_contract(
        target_version=replay.TARGET_VERSION, target_sha256=recorded["target_sha256"],
        target_package_sha256=package["asset_sha256"], target_code_mode_host_sha256=package["code_mode_host_sha256"],
        suite=recorded["suite"], tool_files_sha256=identity["files_sha256"],
        wire_producer_sha256=identity.get("wire_producer_sha256"), policy_sha256=identity.get("policy_sha256"),
        configuration={field: recorded["configuration"][field] for field in CONTRACT_CONFIGURATION_FIELDS},
        target_scenario=json.loads(target_scenarios.read_text(encoding="utf-8")), extra_jobs=None,
    )
    rehearsal_root = control / "job-rehearsal"
    rehearsal_receipt = crf.create_job_rehearsal_receipt(
        rehearsal_root, contract=contract, preflight_campaign_id=PREFLIGHT_ID, preflight_campaign_dir=preflight_dir,
        preflight_manifest_sha256=codex_upgrade.file_sha256(preflight_dir / "campaign.json"),
    )
    release = crf.create_release_certification(control / "release-certification", job_rehearsal_root=rehearsal_root,
                                               job_rehearsal_receipt=rehearsal_receipt)
    p0_root = control / "p0-gate"
    p0_receipt = crf.create_p0_gate_receipt(p0_root, upgrade_id=UPGRADE_ID, baseline_version=replay.BASELINE_VERSION,
                                            target_version=replay.TARGET_VERSION, campaign_purpose=purpose,
                                            release_certification=release)
    deploy = _write(control / "managed-tool-deploy.json", {
        "schema_version": codex_upgrade.ARM64_SUPERVISED_DEPLOY_RECEIPT_SCHEMA, "status": "passed",
        "campaign_id": "r18-replay-deploy", "architecture": "aarch64", **{
            key: identity.get(key) for key in ("files_sha256", "policy_version", "policy_sha256", "wire_producer_sha256",
                                               "evidence_semantics_sha256", "control_sha256")},
    })
    validated = closeout.ValidatedInputs(
        preflight_dir=preflight_dir.resolve(), preflight_manifest=preflight_manifest, timing_ledger_dir=timing_root,
        arm64_root=arm_root, arm64_receipt=arm_receipt, job_rehearsal_root=rehearsal_root,
        job_rehearsal_receipt=rehearsal_receipt, p0_gate_root=p0_root, p0_gate_receipt=p0_receipt,
        release_certification=release,
        receipts=tuple(closeout._binding_source(role, path) for role, path in (
            ("arm64_environment", arm_receipt), ("managed_tool_deploy", deploy), ("p0_gate", p0_receipt),
            ("release_certification", release))),
        timing_summary=timing.inspect_ledger(timing_root),
    )
    formal_dir = campaigns / replay.RECORDED_CAMPAIGN_ID
    if arguments.first_batch_timeout:
        # ③ 首批超时（D1）：只缩短首批动作超时（在 closeout 进程内按作业数估算前替换两个常量），其余合同不变。
        codex_upgrade.FIRST_OFFICIAL_BATCH_MIN_TIMEOUT_SECONDS = int(arguments.first_batch_timeout)
        codex_upgrade.FIRST_OFFICIAL_BATCH_SECONDS_PER_JOB = 0
    closeout_arguments = argparse.Namespace(
        preflight_campaign_dir=preflight_dir, formal_campaign_dir=formal_dir,
        formal_campaign_id=replay.RECORDED_CAMPAIGN_ID, p0_gate_root=p0_root, p0_gate_receipt=p0_receipt,
        managed_tool_deploy_receipt=deploy, release_certification=release,
        supervisor_state_dir=tree / "supervisor" / "vc1", audit_dir=control / "closeout-audit",
        heartbeat_seconds=float(arguments.heartbeat_seconds), watchdog_timeout_seconds=float(arguments.watchdog_seconds),
        ledger_interval_seconds=float(arguments.ledger_interval_seconds),
    )
    error: str | None = None
    with mock.patch.object(closeout, "validate_inputs", return_value=validated):
        try:
            receipt = closeout.closeout(closeout_arguments)
        except closeout.VC0CloseoutError as failure:
            receipt, error = None, str(failure)
    result = {
        "status": "passed" if error is None else "failed", "error": error, "closeout_receipt": receipt,
        "campaign_dir": str(formal_dir), "timing_ledger": str(timing_root), "supervisor_state_dir": str(tree / "supervisor"),
        "attempts": _attempt_summaries(formal_dir),
    }
    _write(_chain_dir(tree) / "init.json", result)
    return result


def _attempt_summaries(campaign_dir: Path) -> list[dict[str, Any]]:
    rows = []
    attempts_root = campaign_dir / "official" / "attempts"
    if not attempts_root.is_dir():
        return rows
    for attempt_root in sorted(attempts_root.iterdir()):
        path = attempt_root / "attempt.json"
        payload = _read(path) if path.is_file() else {}
        results = payload.get("results", [])
        rows.append({
            "attempt_id": attempt_root.name, "status": payload.get("status"),
            "complete": sum(1 for item in results if item.get("status") == "complete"),
            "failed": [item.get("id") for item in results if item.get("status") != "complete"],
            "executed": sum(1 for item in results if item.get("disposition") == "executed"),
            "reused": sum(1 for item in results if item.get("disposition") == "reused"),
        })
    return rows


def _formal(tree: Path) -> tuple[Path, dict[str, Any]]:
    from tools.official_client_capture import codex_upgrade

    campaign = tree / "evidence" / "campaigns" / replay.RECORDED_CAMPAIGN_ID
    return campaign, codex_upgrade._require_formal_campaign(campaign)


def _awaiting_attempt(campaign: Path) -> str:
    rows = [row for row in _attempt_summaries(campaign) if row["status"] == "awaiting_receipts"]
    if len(rows) != 1:
        raise SystemExit(f"应恰好有一个 awaiting_receipts 官方 attempt：{_attempt_summaries(campaign)}")
    return str(rows[0]["attempt_id"])


def dispatch(tree: Path, phase: str, name: str, actions: list[dict[str, Any]], *, heartbeat: float = 0.5) -> dict[str, Any]:
    """以正式原子入口派发一个 VC 批次（序号按既有 COMMIT 推算），返回派发结果与序号。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

    campaign, manifest = _formal(tree)
    sequence = max(codex_upgrade._committed_vc_sequences(campaign, manifest), default=0) + 1
    plan = _write(_chain_dir(tree) / "action-plans" / f"{sequence:04d}-{name}.json", {
        "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
        "execute_item_ids": [item for action in actions for item in action["item_ids"]],
        "reuse_item_ids": [], "actions": actions,
    })
    order = artifacts.VC_PHASES
    predecessor = order[order.index(phase) - 1]
    result, code = codex_upgrade.compile_and_run_vc_batch(argparse.Namespace(
        campaign_dir=campaign, state_dir=tree / "supervisor" / "vc1", phase=phase, sequence=sequence,
        predecessor_checkpoint=campaign / "control" / "vc" / f"{predecessor.lower()}-checkpoint.json",
        action_plan=plan, heartbeat_seconds=heartbeat, watchdog_timeout_seconds=30.0, ledger_interval_seconds=0.5,
    ))
    diagnostics = []
    run_dir = (result.get("campaign_run") or {}).get("run_dir")
    if code != 0 and run_dir:
        diagnostics = [_read(path).get("message") for path in sorted(Path(run_dir).glob("action-diagnostics/*.json"))]
    duplicate = None
    if code == 0:
        # 已提交批次逐字重派必须在执行前拒绝，Campaign、计时账本与总账字节不变、请求增量为 0。
        from tools.official_client_capture.tests.real_chains import test_codex_upgrade_full_chain as full_chain

        namespace = argparse.Namespace(
            campaign_dir=campaign, state_dir=tree / "supervisor" / "vc1", phase=phase, sequence=sequence,
            predecessor_checkpoint=campaign / "control" / "vc" / f"{predecessor.lower()}-checkpoint.json",
            action_plan=plan, heartbeat_seconds=heartbeat, watchdog_timeout_seconds=30.0, ledger_interval_seconds=0.5,
        )
        before, after = full_chain.assert_duplicate_dispatch_unchanged(namespace)
        duplicate = (after[1] - before[1]) + (after[2] - before[2])
    return {"sequence": sequence, "returncode": code, "status": (result.get("campaign_run") or {}).get("status"),
            "reason": (result.get("campaign_run") or {}).get("reason"), "run_dir": run_dir, "diagnostics": diagnostics,
            "duplicate_dispatch_requests": duplicate}


def batches_report(tree: Path) -> list[dict[str, Any]]:
    """逐批次：阶段、序号、执行／复用项与 Campaign 账本记录的请求数（录制证据按身份入账）。"""

    from tools.official_client_capture.tests.real_chains import test_codex_upgrade_full_chain as full_chain

    campaign, _manifest = _formal(tree)
    per_batch, _ledger_total, _project_total = full_chain.ledger_request_counts(campaign)
    return [{"phase": batch["phase"], "sequence": batch["sequence"], "execute": len(batch["execute_item_ids"]),
             "reuse": len(batch["reuse_item_ids"]), "live_request_count": per_batch.get(batch["sequence"], 0)}
            for path in sorted((campaign / "control" / "vc" / "batches").glob("*.json")) for batch in [_read(path)]]


def _seal_command(tree: Path, campaign: Path, attempt_id: str, *extra: str) -> list[str]:
    from tools.official_client_capture import codex_upgrade

    bundle = campaign / "official" / "attempts" / attempt_id / "evidence" / "assertion-bundle"
    return [sys.executable, str(Path(codex_upgrade.__file__).resolve()), "capture-official", "seal",
            "--campaign-dir", str(campaign), "--attempt-id", attempt_id,
            "--capture-manifest", str(bundle / "capture-manifest.json"), "--assertion-evidence-root", str(bundle),
            *extra, "--max-wall-seconds", "1440", "--heartbeat-seconds", "5"]


def stage_seal(arguments: argparse.Namespace) -> dict[str, Any]:
    """VC-1 封存：assertion bundle + seal 预览批次（录制批次 0007 同形），读取预览摘要后以批准批次封存。"""

    from tools.official_client_capture import codex_upgrade

    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    campaign, _manifest = _formal(tree)
    attempt_id = _awaiting_attempt(campaign)
    tools_root = Path(codex_upgrade.__file__).resolve().parent
    preview = dispatch(tree, "VC-1", "seal-preview", [
        {"action_id": "prepare-official-assertion-bundle", "operation": "VC-1:prepare-official-assertion-bundle",
         "timeout_seconds": 300.0, "item_ids": ["prepare-official-assertion-bundle"],
         "command": ["/usr/bin/env", f"CAMPAIGN_DIR={campaign}", f"ATTEMPT_ID={attempt_id}", "SIDE=official",
                     f"REPO_ROOT={tree}", f"TOOL_ROOT={tools_root}", "/usr/bin/bash", str(tree / "tools" / "prepare_assertion_bundle.sh")]},
        {"action_id": "seal-official-preview", "operation": "VC-1:capture-official-seal-preview",
         "timeout_seconds": 1500.0, "item_ids": ["seal-official-preview"],
         "command": _seal_command(tree, campaign, attempt_id)},
    ])
    result: dict[str, Any] = {"status": "failed", "attempt_id": attempt_id, "preview": preview}
    if preview["returncode"] != 0:
        _write(_chain_dir(tree) / "seal.json", result)
        return result
    attempt_root = campaign / "official" / "attempts" / attempt_id
    seal_preview = _read(codex_upgrade._seal_preview_path(attempt_root, 1))
    approve = dispatch(tree, "VC-1", "seal-approve", [
        {"action_id": "seal-official-approve", "operation": "VC-1:capture-official-seal-approve",
         "timeout_seconds": 1500.0, "item_ids": ["seal-official-approve"],
         "command": _seal_command(tree, campaign, attempt_id, "--approve-seal-sha256", str(seal_preview["review_sha256"]))},
    ])
    stage = codex_upgrade._load_stage_result(campaign, "capture-official")
    gate = stage.get("assertion_gate") or {}
    result.update(status="passed" if approve["returncode"] == 0 else "failed", approve=approve,
                  stage_status=stage.get("status"), assertion_gate=gate,
                  vc1_checkpoint=(campaign / "control" / "vc" / "vc-1-checkpoint.json").is_file())
    _write(_chain_dir(tree) / "seal.json", result)
    return result


def stage_account(arguments: argparse.Namespace) -> dict[str, Any]:
    """官方封存入账（account-sealed-official）：按证据核算录制请求身份并推送 fixture 总账。"""

    from tools.official_client_capture import codex_upgrade

    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    campaign, _manifest = _formal(tree)
    output = _chain_dir(tree) / "account-stdout.json"
    import contextlib
    import io

    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = codex_upgrade.main(["account-sealed-official", "--campaign-dir", str(campaign)])
    output.write_text(stream.getvalue(), encoding="utf-8")
    result = {"status": "passed" if code == 0 else "failed", "returncode": code, "output": stream.getvalue()[-4000:]}
    _write(_chain_dir(tree) / "account.json", result)
    return result


def stage_classify(arguments: argparse.Namespace) -> dict[str, Any]:
    """VC-2 首批：生产形态直接调用 classify 生成分类草案。"""

    from tools.official_client_capture import codex_upgrade

    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    campaign, _manifest = _formal(tree)
    batch = dispatch(tree, "VC-2", "classify-draft", [
        {"action_id": "classify-draft", "operation": "VC-2:classify-draft", "timeout_seconds": 1800.0,
         "item_ids": ["classify-draft"],
         "command": [sys.executable, str(Path(codex_upgrade.__file__).resolve()), "classify", "--campaign-dir", str(campaign)]},
    ])
    drafts = sorted((campaign / "classification" / "draft").glob("*/draft.json"))
    result = {"status": "passed" if batch["returncode"] == 0 and len(drafts) == 1 else "failed", "batch": batch,
              "drafts": [str(path) for path in drafts], "batches": batches_report(tree)}
    if drafts:
        draft = _read(drafts[0])
        result["draft_summary"] = {key: draft.get(key) for key in ("status", "rule_count", "entry_count", "discovery_count")
                                   if key in draft}
    _write(_chain_dir(tree) / "classify.json", result)
    return result


def _official_attempts(campaign: Path, status: str) -> list[str]:
    return [row["attempt_id"] for row in _attempt_summaries(campaign) if row["status"] == status]


def stage_reconcile_attempt(arguments: argparse.Namespace) -> dict[str, Any]:
    """reservation 后恢复：reconcile-attempt 出零请求恢复预览 → 同命令带摘要批准 → 授权（账本回 active）。
    默认取最近一个 failed 官方 attempt；预览同时附 R17 复用复算结论。"""

    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    campaign, _manifest = _formal(tree)
    attempt_id = arguments.attempt or (_official_attempts(campaign, "failed") or [None])[-1]
    if attempt_id is None:
        raise SystemExit(f"没有 failed 官方 attempt 可对账：{_attempt_summaries(campaign)}")
    started = __import__("time").monotonic()
    first = reconciler.reconcile_attempt(campaign, attempt_id)
    result: dict[str, Any] = {"status": "failed", "attempt_id": attempt_id, "reconcile_status": first.get("status"),
                              "resume_reuse_check": first.get("resume_reuse_check"), "next_command": first.get("next_command")}
    if first.get("status") != "recoverable":
        result["decision"] = first.get("decision")
        _write(_chain_dir(tree) / f"reconcile-attempt-{attempt_id}.json", result)
        return result
    preview_path = Path(str(first["recovery_preview_path"]))
    preview = _read(preview_path)
    reconciler.reconcile_attempt(campaign, attempt_id, approve_recovery_sha256=str(first["recovery_preview"]["review_sha256"]))
    authorized = reconciler.authorize_recovery_preview(campaign, attempt_id, preview_path)
    result.update(status="passed", preview_path=str(preview_path), authorized=authorized.get("status"),
                  execute_job_ids=preview.get("execute_job_ids"), reuse_job_ids=preview.get("reuse_job_ids") or preview.get("reused_job_ids"),
                  seconds=round(__import__("time").monotonic() - started, 3))
    _write(_chain_dir(tree) / f"reconcile-attempt-{attempt_id}.json", result)
    return result


def stage_reconcile_run(arguments: argparse.Namespace) -> dict[str, Any]:
    """父批次失败对账（reconcile-supervisor-run）：VC-1 已发布预约只能走 attempt 恢复，结论应为阶段审核。"""

    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    campaign, _manifest = _formal(tree)
    reconciled = reconciler.reconcile_supervisor_run(Path(arguments.run_dir), campaign)
    result = {"status": "passed", "reconcile_status": reconciled.get("status"),
              "decision": (reconciled.get("decision") or {}).get("decision"),
              "stage_replay": reconciled.get("stage_replay"), "root_cause": reconciled.get("root_cause")}
    _write(_chain_dir(tree) / f"reconcile-run-{Path(arguments.run_dir).name}.json", result)
    return result


def _recovery_actions(tree: Path, *, preview: str | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """与录制动作计划同形的恢复批次：零请求预览（--preview-recovery）或按已批准预览真实补跑。"""

    from tools.official_client_capture import codex_upgrade

    campaign, _manifest = _formal(tree)
    command = [sys.executable, str(Path(codex_upgrade.__file__).resolve()), "resume", "--campaign-dir", str(campaign), "--rerun-failed"]
    source = _read(Path(preview)) if preview else None
    if preview:
        command += ["--recovery-preview", preview, "--acknowledge-live-requests"]
        action_id, timeout = "run-official-recovery", 5400.0
    else:
        command += ["--preview-recovery"]
        action_id, timeout = "preview-official-recovery", 3600.0
    authorized = sorted(_chain_dir(tree).glob("reconcile-attempt-*.json"), key=lambda path: path.stat().st_mtime_ns)
    latest = _read(authorized[-1]) if authorized else {}
    execute = list((source or latest).get("execute_job_ids") or [])
    reuse = list((source or latest).get("reuse_job_ids") or (source or latest).get("reused_job_ids") or [])
    return [{"action_id": action_id, "operation": "VC-1:official-recovery", "timeout_seconds": timeout,
             "command": command, "item_ids": execute}], execute, reuse


def _dispatch_recovery(tree: Path, name: str, *, preview: str | None) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

    actions, execute, reuse = _recovery_actions(tree, preview=preview)
    campaign, manifest = _formal(tree)
    sequence = max(codex_upgrade._committed_vc_sequences(campaign, manifest), default=0) + 1
    plan = _write(_chain_dir(tree) / "action-plans" / f"{sequence:04d}-{name}.json", {
        "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA, "execute_item_ids": execute, "reuse_item_ids": reuse,
        "actions": actions,
    })
    started = __import__("time").monotonic()
    namespace = argparse.Namespace(
        campaign_dir=campaign, state_dir=tree / "supervisor" / "vc1", phase="VC-1", sequence=sequence,
        predecessor_checkpoint=campaign / "control" / "vc" / "vc-0-checkpoint.json", action_plan=plan,
        heartbeat_seconds=0.5, watchdog_timeout_seconds=30.0, ledger_interval_seconds=0.5,
    )
    result, code = codex_upgrade.compile_and_run_vc_batch(namespace)
    run = result.get("campaign_run") or {}
    diagnostics = []
    if code != 0 and run.get("run_dir"):
        diagnostics = [_read(path).get("message") for path in sorted(Path(run["run_dir"]).glob("action-diagnostics/*.json"))]
    seconds = round(__import__("time").monotonic() - started, 3)
    duplicate = None
    if code == 0:
        # 与 dispatch() 同一检查：已提交的恢复批次逐字重派必须在执行前拒绝，Campaign、计时账本与总账字节不变、
        # 请求增量为 0（恢复批次的承接判定与补跑派发同样不得被重放）。
        from tools.official_client_capture.tests.real_chains import test_codex_upgrade_full_chain as full_chain

        before, after = full_chain.assert_duplicate_dispatch_unchanged(namespace)
        duplicate = (after[1] - before[1]) + (after[2] - before[2])
    batch = {"status": "passed" if code == 0 else "failed", "sequence": sequence, "returncode": code,
             "reason": run.get("reason"), "run_dir": run.get("run_dir"), "diagnostics": diagnostics,
             "execute": execute, "reuse_count": len(reuse), "seconds": seconds,
             "attempts": _attempt_summaries(campaign), "duplicate_dispatch_requests": duplicate}
    _write(_chain_dir(tree) / f"{sequence:04d}-{name}.json", batch)
    return batch


def stage_preview(arguments: argparse.Namespace) -> dict[str, Any]:
    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    return _dispatch_recovery(tree, "recovery-preview", preview=None)


def stage_rerun(arguments: argparse.Namespace) -> dict[str, Any]:
    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    return _dispatch_recovery(tree, "recovery-run", preview=str(arguments.preview))


def stage_epoch(arguments: argparse.Namespace) -> dict[str, Any]:
    """evidence semantics 变化后为待封存 attempt 追加 evaluation epoch（CLI 同一入口）。"""

    import contextlib
    import io

    from tools.official_client_capture import codex_upgrade

    tree = Path(arguments.tree).resolve()
    replay.assert_namespace(tree)
    replay.assert_network_isolated()
    campaign, _manifest = _formal(tree)
    attempt_id = _awaiting_attempt(campaign)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = codex_upgrade.main(["evaluation-epoch", "--campaign-dir", str(campaign), "--attempt-id", attempt_id,
                                   "--reason", "R18 恢复链：封存前证据语义修复已部署"])
    result = {"status": "passed" if code == 0 else "failed", "returncode": code, "attempt_id": attempt_id,
              "output": stream.getvalue()[-3000:],
              "epochs": sorted(path.name for path in (campaign / "official" / "attempts" / attempt_id).glob("evaluation-epoch-*.json"))}
    _write(_chain_dir(tree) / "epoch.json", result)
    return result


STAGES = {
    "init": stage_init, "seal": stage_seal, "account": stage_account, "classify": stage_classify,
    "reconcile-attempt": stage_reconcile_attempt, "reconcile-run": stage_reconcile_run,
    "preview": stage_preview, "rerun": stage_rerun, "epoch": stage_epoch,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--heartbeat-seconds", type=float, default=2.0)
    parser.add_argument("--watchdog-seconds", type=float, default=30.0)
    parser.add_argument("--ledger-interval-seconds", type=float, default=2.0)
    parser.add_argument("--first-batch-timeout", type=int, default=0)
    parser.add_argument("--attempt")
    parser.add_argument("--run-dir")
    parser.add_argument("--preview")
    parser.add_argument("stage", choices=sorted(STAGES))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    result = STAGES[arguments.stage](arguments)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
