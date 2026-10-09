"""Claude 2.1.226 录制回归：真实 VC-0 收口、首批监督复制、VC-1 seal 与 checkpoint。

本驱动只用于 fixture_only staging。旧 x64 官方来源与本次 ARM64 测试运行明确分开；外部 Docker、
部署观察与发布认证循环前置使用显式夹具，字段与绑定仍经正式校验器。没有预写 VC-1 完成事实，也不替换
closeout.validate_inputs、relay checker、seal、计账或监督器返回值。运行前必须进入独立 mount/net namespace。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from unittest import mock

from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay

BASELINE = "2.1.220"
PREFLIGHT = "claude-recorded-2-1-226-preflight"
FORMAL = "claude-recorded-2-1-226-formal"
UPGRADE = "claude-recorded-2-1-226-upgrade"
IMAGE = "oauth-egress-capture-capture-cli@sha256:" + "b" * 64
REPOSITORY = Path(__file__).resolve().parents[3]
_STACKS: list[contextlib.ExitStack] = []
CONTINUATION_SCHEMA = "claude-recorded-vc1-continuation/v1"


def _inputs(data: Path) -> dict[str, Any]:
    """生成有独立来源说明的控制输入投影；不改任何版本化正式清单或原始录制字段。"""

    from tools.official_client_capture import claude_upgrade_plan as plan
    from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
    from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests

    root = data / "control" / "fixture-inputs"
    root.mkdir(parents=True, mode=0o700)
    baseline = source.read(plan.baseline_rule_manifest_path("2.1.226"))
    baseline["target_version"] = BASELINE
    baseline_path = source.write(root / "baseline.json", baseline)
    rules = source.read(plan.rule_manifest_path("2.1.285"))
    rules.update(claude_version=source.VERSION, baseline_version=BASELINE,
                 description="fixture_only：以既有 40 条规则构成 2.1.226 离线控制链分母，不代表新的历史版本结论。",
                 baseline_rule_manifest={"path": "fixture-only/baseline.json", "sha256": source.binding(baseline_path)["sha256"]})
    rules_path = source.write(root / "rules.json", rules)
    template = source.read(plan.scenario_manifest_path("2.1.285"))
    official_ids = set(seal_tests.SEAL_JOBS)
    official_probes = {step["probe_id"] for job in template["capture_jobs"] if job["id"] in official_ids for step in job["steps"]}
    # full 预检在冻结时就要求两侧覆盖同一规则全集。只选所有步骤都已有官方来源的完整候选 Job，不能删候选侧或拆半个 Job。
    candidate_ids = {job["id"] for job in template["capture_jobs"] if job["phase"] == "candidate"
                     and all(step["probe_id"] in official_probes for step in job["steps"])}
    selected_ids = official_ids | candidate_ids
    scenarios = capture_tests._reduced_payload(selected_ids)
    primary = {step["probe_id"] for job in scenarios["capture_jobs"] if job["phase"] == "official" for step in job["steps"]
               if plan.step_model(step["probe_id"], step["argv"]) is None}
    scenarios["probe_catalog"]["probe_count"] = len(primary)
    scenarios["claude_version"] = source.VERSION
    scenarios["profile_id"] = "claude-recorded-fixture-2.1.226"
    scenarios["rule_manifest"] = {"path": "fixture-only/rules.json", "sha256": source.binding(rules_path)["sha256"], "rule_count": 40}
    guide = REPOSITORY / scenarios["source_spec"]["path"]
    scenarios["source_spec"]["sha256"] = plan.guide_section_sha256(guide, scenarios["source_spec"]["fragment"])
    original_facts = REPOSITORY / scenarios["version_facts"]["path"]
    facts = source.read(original_facts)
    # 版本事实仅是此控制链的输入夹具，不对其业务结论签发版本认证；原始版本文件完全不动。
    facts = json.loads(json.dumps(facts).replace("2.1.285", source.VERSION))
    facts_relative = "tools/official_client_capture/tests/fixtures/claude_recorded_version_facts.json"
    facts_path = source.write(data / facts_relative, facts)
    scenarios["version_facts"] = {"path": facts_relative, "sha256": source.binding(facts_path)["sha256"]}
    for job in scenarios["capture_jobs"]:
        for step in job["steps"]:
            argv = step["argv"]
            argv[argv.index("--fw-f-v5-version-facts") + 1] = "{capture_tool_root}/tests/fixtures/claude_recorded_version_facts.json"
            argv[argv.index("--fw-f-v5-version-facts-sha256") + 1] = scenarios["version_facts"]["sha256"]
    scenarios_path = source.write(root / "scenarios.json", scenarios)
    labels = source.read(plan.evidence_labels_path("2.1.285"))
    labels["claude_version"] = source.VERSION
    labels["description"] = "fixture_only：以原 v4 连接标签承接 2.1.226 录制，只用于控制链回归。"
    labels["entries"] = [item for item in labels["entries"] if item["job_id"] in selected_ids]
    labels_path = source.write(root / "labels.json", labels)
    inventory = copy.deepcopy(seal_tests.TARGET_INVENTORY)
    inventory["target_version"] = source.VERSION
    inventory_path = source.write(root / "target-inventory.json", inventory)
    containment = seal_tests.sink_containment(inventory_path)
    containment["target_version"] = source.VERSION
    containment_path = source.write(root / "sink-containment.json", containment)
    dispositions = copy.deepcopy(seal_tests.DISPOSITIONS)
    dispositions["target_version"] = source.VERSION
    dispositions_path = source.write(root / "dispositions.json", dispositions)
    paths = {"baseline": baseline_path, "rules": rules_path, "scenarios": scenarios_path, "labels": labels_path,
             "version_facts": facts_path, "inventory": inventory_path, "containment": containment_path, "dispositions": dispositions_path}
    records = {key: source.binding(path) for key, path in paths.items()}
    source.write(root / "projection-origin.json", {"fixture_only": True, "purpose": "control_input_projection",
                 "target_version": source.VERSION, "template_version": "2.1.285", "inputs": records,
                 "static_conclusions": "合成控制输入，不是历史 307 sink 的原始分析结论",
                 "template_version_facts": source.binding(original_facts)})
    return {"bindings": records,
            # 候选容器与 7/9 只用于 fixture_only 控制来源；它们随 source state 冻结，后段不得另取真实账号或改值。
            "candidate_configuration": {"service_container": "sub2apiplus", "keeper_container": "sub2apiplus-keeper",
                                        "postgres_container": "sub2apiplus-postgres", "claude_account_id": "7", "api_key_id": "9"},
            "jobs": {job["id"]: [step["probe_id"] for step in job["steps"]] for job in scenarios["capture_jobs"] if job["phase"] == "official"},
            "candidate_jobs": {job["id"]: [step["probe_id"] for step in job["steps"]] for job in scenarios["capture_jobs"] if job["phase"] == "candidate"},
            "repository_bindings": {"fixture-only/baseline.json": records["baseline"],
                                    "fixture-only/rules.json": records["rules"], facts_relative: records["version_facts"]}}


def _environment_facts(state: dict[str, Any], **options: Any) -> dict[str, Any]:
    """仅替换宿主观察源，正式 collect、finalize、replay 与合同校验全部保留。"""

    from tools.official_client_capture import claude_upgrade_arm64_environment_receipt as arm
    from tools.official_client_capture.tests import test_claude_upgrade_arm64_environment_receipt as fixture

    runtime = Path(state["data_root"]) / "control" / "environment-runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    with mock.patch.object(fixture, "BINARY_SHA256", state["freeze"]["binary_sha256"]), \
            mock.patch.object(fixture, "CLAUDE_VERSION", source.VERSION):
        host = fixture.FakeHost()
        with fixture.fake_arm64(host, runtime):
            return arm.collect_facts(**options)


def _binary_observation(state: dict[str, Any], manifest: dict[str, Any], observer: Any, **options: Any) -> dict[str, Any]:
    """先严格重验旧 freeze，再输出正式二进制事实闭集；夹具来源标记只留在独立 source state 与报告。"""

    source.verify_origin_freeze(state, Path(state["freeze"]["files"]["freeze.json"]["path"]),
                                expected_version=manifest["target_version"], expected_binary_sha256=manifest["target_sha256"])
    # Job 演练的 binary_verification/v2 是封闭合同，不能向其追加 fixture_only 或 origin_platform。
    return observer.fake_binaries(manifest, **options)


@contextlib.contextmanager
def installed(state_path: Path, digest: str, *, adapter_factory: Callable[..., Any] = source.SourceAdapter):
    """所有替身集中登记。控制判定函数不在替换清单内，离开作用域后恢复原函数。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_capture as capture
    from tools.official_client_capture import claude_upgrade_plan as plan
    from tools.official_client_capture import claude_upgrade_vc0_closeout as closeout
    from tools.official_client_capture import codex_upgrade_supervisor as supervisor
    from tools.official_client_capture.tests import test_claude_upgrade_capture as fixture
    from tools.official_client_capture.tests import test_claude_upgrade_job_rehearsal as rehearsal_fixture

    adapter = adapter_factory(state_path, digest)
    state, data = adapter.state, Path(adapter.state["data_root"])
    for item in state["inputs"]["bindings"].values():
        source.bound(item, root=data)
    host = fixture.FakeHost()
    observer = rehearsal_fixture.ClaudeRehearsalFixture()
    observer.binary_calls = 0
    original_repo_file, baseline_path, rules_path = plan._repository_file, plan.baseline_rule_manifest_path, plan.rule_manifest_path
    labels_path = plan.evidence_labels_path
    deploy_dir = data / "control" / "fixture-deploy-run"
    original_read, original_audit = supervisor._read_state, supervisor._audit_command

    def repository_file(relative: Any, label: str) -> Path:
        item = state["inputs"]["repository_bindings"].get(str(relative))
        return source.bound(item, root=data) if item else original_repo_file(relative, label)

    def binary(manifest: dict[str, Any], **options: Any) -> dict[str, Any]:
        return _binary_observation(state, manifest, observer, **options)

    def read_deploy(path: Path) -> dict[str, Any]:
        if Path(path) == deploy_dir:
            return {"campaign_id": "fixture-recorded-deploy", "phase": "bootstrap", "state": "stopped"}
        return original_read(path)

    def audit_deploy(path: Path) -> dict[str, Any]:
        if Path(path) == deploy_dir:
            return {"run_dir": str(deploy_dir), "state": "stopped", "audit_incomplete": False}
        return original_audit(path)

    with contextlib.ExitStack() as stack:
        stack.enter_context(adapter.patched())
        stack.enter_context(mock.patch.object(plan, "_repository_file", repository_file))
        stack.enter_context(mock.patch.object(plan, "baseline_rule_manifest_path", lambda version: source.bound(state["inputs"]["bindings"]["baseline"])
                                             if version == BASELINE else baseline_path(version)))
        stack.enter_context(mock.patch.object(plan, "rule_manifest_path", lambda version: source.bound(state["inputs"]["bindings"]["rules"])
                                             if version == source.VERSION else rules_path(version)))
        stack.enter_context(mock.patch.object(plan, "evidence_labels_path", lambda version: source.bound(state["inputs"]["bindings"]["labels"])
                                             if version == source.VERSION else labels_path(version)))
        stack.enter_context(mock.patch.object(capture, "_collect_probe_states", lambda _manifest, **_options: copy.deepcopy(fixture.BASE_ENVIRONMENT)))
        stack.enter_context(mock.patch.object(capture, "_run_probe_command", host.run))
        stack.enter_context(mock.patch.object(upgrade, "_verify_official_binaries", binary))
        stack.enter_context(mock.patch.object(upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", data / "runs"))
        stack.enter_context(mock.patch.object(upgrade, "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS", (PurePosixPath(str(data / "runs")),)))
        stack.enter_context(mock.patch.object(upgrade, "JOB_RETRY_DELAY_SECONDS", 0))
        stack.enter_context(mock.patch.object(closeout, "_current_tool_root", lambda: (data / "tools" / "official_client_capture").resolve()))
        stack.enter_context(mock.patch.object(supervisor, "_read_state", read_deploy))
        stack.enter_context(mock.patch.object(supervisor, "_audit_command", audit_deploy))
        yield adapter


def _plan_and_inputs(state: dict[str, Any]) -> dict[str, Path]:
    """真实创建预检与控制收据；发布认证循环前置的文件明确为夹具并通过正式 verify。"""

    from tools.official_client_capture import certify_release, claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_arm64_environment_receipt as arm
    from tools.official_client_capture import claude_upgrade_campaign as campaign
    from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as rehearsal
    from tools.official_client_capture import codex_upgrade_policy_certification as policy
    from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3_contract
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing
    from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
    from tools.official_client_capture.tests import test_claude_upgrade_campaign as campaign_fixture
    from tools.official_client_capture.tests import test_claude_upgrade_job_rehearsal as rehearsal_fixture
    from tools.official_client_capture.tests.control_receipt_fixtures import create_p0_gate_receipt

    data, control = Path(state["data_root"]), Path(state["data_root"]) / "control"
    ledger_fixture.install_claude_project_ledger(data)
    ledger = control / "timing"
    timing.create_ledger(ledger, upgrade_id=UPGRADE, baseline_version=BASELINE, target_version=source.VERSION,
                         campaign_purpose="validation_only", evidence_decision="recapture", client="claude")
    timing.checkpoint(ledger, "receipts/vc0-input.json")
    environment = control / "environment-p0"
    environment.mkdir(mode=0o700)
    facts = _environment_facts(state, phase="p0", subject_id=UPGRADE, claude_version=source.VERSION)
    source.write(environment / "facts.json", facts)
    arm.finalize(environment, "facts.json", "receipt.json")
    arm.replay(environment, "receipt.json")
    frozen = state["inputs"]["bindings"]
    def baseline_input(name: str, filename: str, fallback: dict[str, Any]) -> Path:
        # 完整链在 VC-0 前提供全部输入；旧 VC-1 专用世界保留显式占位，禁止封存后再补改。
        return source.bound(frozen[name], root=data) if name in frozen else source.write(control / filename, fallback)

    active = baseline_input("active_release_catalog", "active.json", {"fixture_only": True, "release": "claude-recorded-baseline"})
    profile = baseline_input("active_profile", "profile.json", {"fixture_only": True})
    anchors = baseline_input("static_baseline_anchors", "anchors.json", {"fixture_only": True})
    reachability = baseline_input("static_baseline_reachability", "reachability.json", {"fixture_only": True})
    predecessor, predecessor_id = campaign_fixture.build_predecessor_store(data / "fixture-predecessor")
    preflight = data / "evidence" / "campaigns" / PREFLIGHT
    inputs = state["inputs"]["bindings"]
    candidate_configuration = state["inputs"]["candidate_configuration"]
    values = {
        "--campaign-dir": preflight, "--campaign-id": PREFLIGHT, "--campaign-mode": "preflight_only",
        "--campaign-purpose": "validation_only", "--baseline-version": BASELINE, "--target-version": source.VERSION,
        "--target-sha256": state["freeze"]["binary_sha256"], "--target-package": state["freeze"]["files"]["freeze.json"]["path"],
        "--target-package-sha256": state["freeze"]["files"]["freeze.json"]["sha256"], "--runtime-image": IMAGE,
        "--model": "claude-sonnet-5", "--upstream-ip": "160.79.104.10", "--capture-root": data,
        "--capture-tool-root": data / "tools" / "official_client_capture", "--claude-bin": "/opt/claude-2.1.226/claude",
        "--capture-container": "capture-cli", "--synthetic-capture-container": "capture-cli-synthetic",
        "--timing-ledger-dir": ledger, "--timing-receipt": "receipts/vc0-input.json", "--arm64-environment-root": environment,
        "--arm64-environment-receipt": "receipt.json", "--active-release-catalog": active, "--active-profile": profile,
        "--static-baseline-anchors": anchors, "--static-baseline-reachability": reachability,
        "--entrypoint": "sdk-cli", "--privacy-mode": "essential-traffic", "--oauth-org-id": "claude-org-fixture",
        "--model-catalog-sha256": "f" * 64, "--source-commit": "e" * 40,
        "--service-container": candidate_configuration["service_container"],
        "--keeper-container": candidate_configuration["keeper_container"],
        "--postgres-container": candidate_configuration["postgres_container"],
        "--claude-account-id": candidate_configuration["claude_account_id"], "--api-key-id": candidate_configuration["api_key_id"],
        "--production-inventory-store": predecessor, "--production-inventory-campaign": predecessor_id,
        "--rule-manifest": inputs["baseline"]["path"], "--scenario-manifest": inputs["scenarios"]["path"],
        "--evidence-labels": inputs["labels"]["path"],
    }
    argv = ["plan", *(part for key, value in values.items() for part in (key, str(value)))]
    result = upgrade.main(argv)
    if result != 0:
        raise replay.ReplayError("真实预检 plan 未完成")
    manifest = upgrade.load_campaign_manifest(preflight)
    # 外部命令来源沿用已有 Job fixture；collect/finalize/replay/assert_formal_compatible 不替换。
    fixture = rehearsal_fixture.ClaudeRehearsalFixture()
    fixture.data, fixture.bin, fixture.calls_file = data, data / "fixture-bin", control / "rehearsal-docker-calls.jsonl"
    fixture.evidence, fixture.binary_calls, fixture.lifecycle_calls = control / "job-rehearsal", 0, 0
    fixture.bin.mkdir(mode=0o700)
    fixture.evidence.mkdir(mode=0o700)
    docker = fixture.bin / "docker"
    docker.write_text(rehearsal_fixture.FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
    docker.chmod(0o700)
    required_commands = rehearsal_fixture.claude_profile.CONTAINER_REQUIRED_COMMANDS
    for name in required_commands:
        if shutil.which(name) is None:
            # 外部容器命令的可执行文件观察源：仅允许依赖探针取摘要，真正执行时失败关闭。
            path = fixture.bin / name
            path.write_text("#!/bin/sh\n# 仅供离线依赖观察，禁止执行工具行为。\nexit 125\n", encoding="utf-8")
            path.chmod(0o700)
    verified_binary_observation = upgrade._verify_official_binaries
    # 同一录制世界在 collect 与后续发布认证重放时使用同一 runs 别名全集。
    # 通用 Job fixture 另带的历史别名不属于本世界，不能在退出 fixture 后改变读侧期望。
    registered_runs = tuple(upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS)
    with mock.patch.object(rehearsal_fixture, "TEST_CONTAINER_COMMANDS", required_commands), fixture.patched(), \
            mock.patch.object(upgrade, "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS", registered_runs), \
            mock.patch.object(upgrade, "_verify_official_binaries", verified_binary_observation):
        rehearsal.collect(fixture.evidence, "facts.json", campaign_dir=preflight, client="claude")
        rehearsal.finalize(fixture.evidence, "facts.json", "receipt.json", client="claude")
        job_receipt = rehearsal.replay(fixture.evidence, "receipt.json", client="claude")
    rehearsal.assert_formal_compatible(job_receipt, campaign.job_rehearsal_contract(preflight, manifest))
    identity = upgrade._tool_identity(include_git=False)
    deploy = ledger_fixture.write_claude_deploy_receipt(data, identity, stamp="recorded-fixture")
    deployed = source.read(deploy)
    deploy_dir = control / "fixture-deploy-run"
    deploy_dir.mkdir(mode=0o700)
    deployed.update(campaign_id="fixture-recorded-deploy", architecture="aarch64", production_data_root=str(data), supervisor_run_dir=str(deploy_dir))
    deploy.write_text(json.dumps(deployed), encoding="utf-8")
    # 这是消解「测试本链前先要有本链认证」的来源夹具，不是实际链执行结果；最终报告只登记真实完成的 VC-1。
    required_chains = (
        "vc-chain.claude-reconcile", "vc-chain.claude-deadline", "vc-chain.claude-root-cause", "vc-chain.claude-tool-evolution",
        "vc-chain.claude-vc1-capture", "vc-chain.claude-full-validation-only", "vc-chain.claude-recovery-chain", "vc-chain.claude-late-stage-faults",
    )
    required_chains = getattr(certify_release, "RELEASE_REQUIRED_CHAINS", {}).get("claude", required_chains)
    registration = [{"id": name, "test": "fixture_only.circular_prerequisite:" + name} for name in required_chains]
    prerequisite = {"schema_version": pre_a3_contract.SCHEMA_VERSION_V3, "client": "claude", "fixture_only": True,
                    "purpose": "circular_prerequisite_source", "not_an_execution_report": True, "real_chain_registration": registration,
                    "scenarios": [{"name": row["id"], "test": row["test"], "status": "passed"} for row in registration]}
    pre_a3 = source.write(control / "fixture-pre-a3.json", prerequisite)
    atomic = source.write(control / "fixture-atomic.json", {"fixture_only": True, "purpose": "circular_prerequisite_source"})
    certificate = certify_release.compose_certification(
        identity=policy.current_identity("claude"), deployment_receipt=source.binding(deploy),
        pre_a3_certification=source.binding(pre_a3), policy_activation=None,
        real_chain_coverage=pre_a3_contract.real_chain_coverage(prerequisite, historical=True, client="claude"),
        # 由当前正式读侧从真实演练收据推导完整绑定，避免手工字段表落后于 Job 合同。
        job_rehearsal=certify_release.replay_job_rehearsal(fixture.evidence, Path("receipt.json"), "claude"),
        atomic_double_rehearsal={"evidence_root": str(control), "receipt": atomic.name, "sha256": source.binding(atomic)["sha256"]},
        campaign_run_rehearsal=None, client="claude")
    release = source.write(control / "fixture-release-certification.json", certificate)
    certify_release.verify(release, client="claude")
    p0 = create_p0_gate_receipt(control / "p0-gate", upgrade_id=UPGRADE, baseline_version=BASELINE,
                               target_version=source.VERSION, campaign_purpose="validation_only", release_certification=release)
    return {"preflight": preflight, "ledger": ledger, "environment": environment, "deploy": deploy, "release": release, "p0": p0}


def _batch(
    data: Path, campaign: Path, name: str, argv: list[str], *, phase: str = "VC-1", timeout_seconds: float = 600,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """同一 Campaign 的真实监督批次；前序只从已验证 checkpoint 取，不能由夹具预写完成。"""
    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_batch as batch
    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

    if phase not in artifacts.VC_PHASES[1:]:
        raise replay.ReplayError(f"录制链不接受此动作阶段：{phase}")
    manifest = upgrade.load_campaign_manifest(campaign)
    previous = artifacts.VC_PHASES[artifacts.VC_PHASES.index(phase) - 1]
    revision = upgrade._current_candidate_revision(campaign, manifest) if previous in upgrade.CANDIDATE_VC_PHASES else None
    predecessor = upgrade._vc_checkpoint_path(campaign, previous, revision=revision)
    plan = upgrade._vc_campaign_plan(campaign, manifest)
    upgrade._replay_vc_checkpoint(campaign, plan, previous, revision=revision)
    sequence = max(upgrade._committed_vc_sequences(campaign, manifest), default=0) + 1
    dispatched = (batch.managed_action(name, phase + ":" + name, argv, item_ids=[name], timeout_seconds=timeout_seconds)
                  if action is None else action)
    if dispatched.get("action_id") != name or dispatched.get("item_ids") != [name] or dispatched.get("operation") != phase + ":" + name:
        raise replay.ReplayError("显式批次动作必须与本阶段、动作名和唯一执行项闭合")
    action = source.write(data / "control" / "actions" / f"{sequence}-{name}.json", {
        "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA, "execute_item_ids": [name], "reuse_item_ids": [],
        "actions": [dispatched]})
    result, code = upgrade.compile_and_run_vc_batch(argparse.Namespace(
        campaign_dir=campaign, state_dir=data / "supervisor", phase=phase, sequence=sequence,
        predecessor_checkpoint=predecessor, action_plan=action,
        heartbeat_seconds=0.5, watchdog_timeout_seconds=30.0, ledger_interval_seconds=0.5))
    if code != 0:
        raise replay.ReplayError(f"真实 {name} 批次失败：{result}")
    return result


def write_continuation(state_path: Path, digest: str, campaign: Path, paths: dict[str, Path]) -> Path:
    """冻结只读接续凭证；引用真实 VC-1 结果，后段不能另建 Campaign 或替换封存材料。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_control.store import ControlStore

    state = source.load_state(state_path, digest)
    data = Path(state["data_root"])
    manifest = upgrade.load_campaign_manifest(campaign)
    plan = upgrade._vc_campaign_plan(campaign, manifest)
    checkpoints = {}
    for phase in ("VC-0", "VC-1"):
        path, _checkpoint = upgrade._replay_vc_checkpoint(campaign, plan, phase)
        checkpoints[phase] = source.binding(path)
    stage = upgrade._load_stage_result(campaign, "capture-official")
    if stage.get("status") != "complete" or stage.get("store", {}).get("checkpoint") != "evidence_recorded":
        raise replay.ReplayError("录制接续仅接受真实 VC-1 封存结果")
    store = ControlStore(campaign / "control-store")
    store_campaign = store.load_campaign(str(manifest["campaign_id"]))
    document = {
        "schema_version": CONTINUATION_SCHEMA, "fixture_only": True, "purpose": "regression_fixture",
        "scope": "VC-0→VC-1", "target_version": source.VERSION, "data_root": str(data),
        "state": source.binding(state_path), "campaign": source.binding(campaign / "campaign.json"),
        "campaign_checksum": source.binding(campaign / "campaign.sha256"),
        "campaign_plan": source.binding(campaign / "control/vc/campaign-plan.json"),
        "official_stage": source.binding(campaign / "official/result.json"), "checkpoints": checkpoints,
        "store_campaign_id": store_campaign["campaign_id"], "official_store": stage["store"],
        "timing_ledger_plan": source.binding(paths["ledger"] / "ledger.json"),
        "tool_identity": manifest["tool_identity"], "source_snapshot": state["snapshot"],
        "full_vc0_vc6_certified": False,
    }
    return source.write(data / "control/recorded-vc1-continuation.json", document)


def load_continuation(path: Path, digest: str) -> dict[str, Any]:
    """逐项重验只读接续凭证；凭证存在并不授予 VC-2～VC-6 的完成状态。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_control.store import ControlStore

    if source.binding(path)["sha256"] != digest:
        raise replay.ReplayError("录制接续凭证外持摘要漂移")
    document = source.read(path)
    if (document.get("schema_version") != CONTINUATION_SCHEMA or document.get("fixture_only") is not True
            or document.get("purpose") != "regression_fixture" or document.get("scope") != "VC-0→VC-1"
            or document.get("target_version") != source.VERSION or document.get("full_vc0_vc6_certified") is not False):
        raise replay.ReplayError("录制接续凭证合同非法")
    data = Path(document["data_root"])
    if "staging" not in data.parts or data.resolve() != data:
        raise replay.ReplayError("录制接续必须留在原 staging 数据根")
    state_path = source.bound(document["state"], root=data)
    state = source.load_state(state_path, document["state"]["sha256"])
    if state["data_root"] != str(data) or state["snapshot"] != document["source_snapshot"]:
        raise replay.ReplayError("录制接续的来源或数据根漂移")
    campaign_file = source.bound(document["campaign"], root=data / "evidence/campaigns")
    campaign = campaign_file.parent
    expected_paths = {
        "campaign": campaign / "campaign.json", "campaign_checksum": campaign / "campaign.sha256",
        "campaign_plan": campaign / "control/vc/campaign-plan.json", "official_stage": campaign / "official/result.json",
        "timing_ledger_plan": data / "control/timing/ledger.json",
    }
    for name, expected in expected_paths.items():
        if source.bound(document[name], root=data) != expected:
            raise replay.ReplayError(f"录制接续绑定坐标错位：{name}")
    manifest = upgrade.load_campaign_manifest(campaign)
    if (manifest["campaign_id"] != document["store_campaign_id"] or manifest["tool_identity"] != document["tool_identity"]
            or manifest["campaign_purpose"] != "validation_only" or manifest["target_version"] != source.VERSION):
        raise replay.ReplayError("录制接续 Campaign 身份漂移")
    plan = upgrade._vc_campaign_plan(campaign, manifest)
    if set(document["checkpoints"]) != {"VC-0", "VC-1"}:
        raise replay.ReplayError("录制接续的前序 checkpoint 集不闭合")
    for phase, item in document["checkpoints"].items():
        checkpoint, _payload = upgrade._replay_vc_checkpoint(campaign, plan, phase)
        if source.bound(item, root=campaign) != checkpoint:
            raise replay.ReplayError("录制接续的 checkpoint 绑定错位")
    stage = upgrade._load_stage_result(campaign, "capture-official")
    if stage["store"] != document["official_store"]:
        raise replay.ReplayError("录制接续的官方封存对象引用漂移")
    store = ControlStore(campaign / "control-store")
    if store.load_campaign(document["store_campaign_id"])["campaign_id"] != manifest["campaign_id"]:
        raise replay.ReplayError("录制接续不能另建 Store 世界")
    replay.load_snapshot(state["snapshot"])
    return {"document": document, "state_path": state_path, "state_digest": document["state"]["sha256"],
            "state": state, "data": data, "campaign": campaign, "manifest": manifest}


def run_chain(state_path: Path, digest: str, *, installer: Callable[..., Any] = installed) -> dict[str, Any]:
    from tools.official_client_capture import claude_upgrade_vc0_closeout as closeout
    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing
    from tools.official_client_control.store import ControlStore

    state = source.load_state(state_path, digest)
    replay.load_snapshot(state["snapshot"])
    data, started = Path(state["data_root"]), time.monotonic()
    with installer(state_path, digest):
        paths = _plan_and_inputs(state)
        campaign = data / "evidence/campaigns" / FORMAL
        argv = ["--preflight-campaign-dir", str(paths["preflight"]), "--formal-campaign-dir", str(campaign),
                "--formal-campaign-id", FORMAL, "--p0-gate-root", str(paths["p0"].parent), "--p0-gate-receipt", paths["p0"].name,
                "--managed-tool-deploy-receipt", str(paths["deploy"]), "--release-certification", str(paths["release"]),
                "--supervisor-state-dir", str(data / "supervisor"), "--audit-dir", str(data / "control/closeout-audit")]
        if closeout.main(argv) != 0:
            raise replay.ReplayError("真实 VC-0 收口或首批执行失败")
        attempts = [path for path in (campaign / "official/attempts").glob("*/attempt.json")
                    if source.read(path).get("status") == "awaiting_receipts"]
        if len(attempts) != 1:
            raise replay.ReplayError("首批未产生唯一待封存 attempt")
        attempt = attempts[0].parent
        inputs = state["inputs"]["bindings"]
        # seal 的显式静态输入必须在 Campaign 内；副本仍按 fixture 合同逐字绑定。
        static_paths = {}
        for name in ("inventory", "containment", "dispositions"):
            original = source.bound(inputs[name], root=data)
            target = campaign / "analysis" / original.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as handle:
                handle.write(original.read_bytes())
            target.chmod(0o600)
            static_paths[name] = str(target)
        seal_argv = ["capture-official", "seal", "--campaign-dir", str(campaign), "--attempt-id", attempt.name,
                     "--target-inventory", static_paths["inventory"], "--sink-containment", static_paths["containment"],
                     "--discovery-dispositions", static_paths["dispositions"]]
        preview_batch = _batch(data, campaign, "vc1-02-seal-preview", seal_argv)
        previews = list((campaign / "official/seal").glob("*/seal-preview.json"))
        if len(previews) != 1:
            raise replay.ReplayError("真实 seal 未形成唯一预览")
        preview = source.read(previews[0])
        approval = _batch(data, campaign, "vc1-03-seal-approve", [*seal_argv, "--approve-seal-sha256", preview["review_sha256"]])
        stage = upgrade._load_stage_result(campaign, "capture-official")
        checkpoint = campaign / "control/vc/vc-1-checkpoint.json"
        if not checkpoint.is_file() or stage.get("status") != "complete" or stage.get("store", {}).get("checkpoint") != "evidence_recorded":
            # 状态合同以真实结果为准，不能在这里补 checkpoint。
            raise replay.ReplayError(f"VC-1 未形成正式封存终态：{stage.get('status')}")
        store = ControlStore(campaign / "control-store")
        store_campaign = store.load_campaign(FORMAL)
        copied = []
        for path in sorted(attempt.rglob("*.projection.copied.json")):
            receipt = source.read(path)
            projection = source.read(source.bound(receipt["projection"], root=data))
            source.verify_projection(state, projection)
            for item in receipt["files"]:
                source.bound(item, root=data)
            copied.append({"operation": receipt["operation"], "runtime": receipt["runtime"], "origin": receipt["origin"]})
        expected = sum(map(len, state["jobs"].values()))
        if len(copied) != expected or len({row["operation"] for row in copied}) != expected:
            raise replay.ReplayError("复制动作未按白名单一一闭合")
        continuation = write_continuation(state_path, digest, campaign, paths)
        elapsed = time.monotonic() - started
        from tools.official_client_capture.tests import claude_chain_diagnostic_budget as diagnostic_budget
        diagnostic_seconds = diagnostic_budget.diagnostic_seconds()
        from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
        time_budget = certification.claude_recorded_time_budget("vc-chain.claude-vc1-capture")
        if elapsed > (diagnostic_seconds or time_budget["method_timeout_seconds"]):
            raise replay.ReplayError("VC-0→VC-1 回归超过当前独立方法预算")
        report = {"schema_version": "claude-recorded-vc0-vc1-chain/v1", "fixture_only": True, "status": "passed",
                  "registered_chains": ["vc-chain.claude-vc1-capture"], "scope": "VC-0→VC-1", "target_version": source.VERSION,
                  "origin_platform": "linux/amd64", "execution_platform": "linux/arm64", "elapsed_seconds": elapsed,
                  "diagnostic_only": diagnostic_seconds is not None, "formal_time_budget_met": elapsed <= time_budget["method_timeout_seconds"], "time_budget": time_budget,
                  "network_requests_this_run": 0, "historical_request_accounting": timing.inspect_ledger(paths["ledger"]),
                  "campaign": str(campaign), "store_campaign_id": store_campaign["campaign_id"], "checkpoint": source.binding(checkpoint),
                  "preview_batch": preview_batch, "approval_batch": approval, "copy_projections": copied,
                  "continuation": source.binding(continuation),
                  "external_fixture_sources": ["v1 单 npm 来源适配", "Docker 与宿主环境观察", "部署状态观察", "Job 演练外部观察",
                                               "发布认证循环前置", "静态控制输入投影"], "full_vc0_vc6_certified": False}
    replay.assert_source_untouched(state["snapshot"])
    for path, expected in state["freeze"]["source_sentinel"].items():
        if replay._stat(Path(path)) != expected:
            raise replay.ReplayError("旧冻结来源元数据发生变化")
    source.write(data / "control/recorded-chain-result.json", report)
    return report


def activate(*, installer: Callable[..., Any] = installed) -> None:
    """sitecustomize 只承接登记的受管编排器脚本；安装失败必须终止，不能悄悄执行真实客户端。"""

    state_path, digest = Path(os.environ[source.STATE_ENV]), os.environ[source.DIGEST_ENV]
    state = source.load_state(state_path, digest)
    stack = contextlib.ExitStack()
    stack.enter_context(installer(state_path, digest))
    _STACKS.append(stack)
    raw = [item.decode() for item in Path("/proc/self/cmdline").read_bytes().split(b"\0") if item]
    managed = {str(Path(state["data_root"]) / "tools/official_client_capture/claude_upgrade.py"),
               str(REPOSITORY / "tools/official_client_capture/claude_upgrade.py")}
    matches = [index for index, value in enumerate(raw) if value in managed]
    matches += [index + 1 for index, value in enumerate(raw[:-1])
                if value == "-m" and raw[index + 1] == "tools.official_client_capture.claude_upgrade"]
    if matches:
        from tools.official_client_capture import claude_upgrade
        code = 1
        try:
            code = claude_upgrade.main(raw[matches[0] + 1:])
        except BaseException:
            traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code or 0)


def _create_world_roots(data: Path) -> None:
    """逐级建私有控制根，避免 parents=True 把审计父目录隐式建成 755；已有目录不修权限。"""

    data.mkdir(parents=True, mode=0o700)
    for name in ("runs", "evidence", "control"):
        (data / name).mkdir(mode=0o700)
    (data / "evidence/campaigns").mkdir(mode=0o700)


def namespace_main(
    arguments: argparse.Namespace, *, inputs_builder: Callable[[Path], dict[str, Any]] = _inputs,
    chain_runner: Callable[[Path, str], dict[str, Any]] = run_chain,
    activation_module: str = "tools.official_client_capture.tests.claude_vc1_recorded_chain_driver",
) -> dict[str, Any]:
    """外壳已 unshare；原始目录只读挂载后才建副本与控制输入。"""

    data = Path(arguments.data).resolve()
    roots = [str(Path(arguments.recorded).resolve().parent), str(Path(arguments.freeze).resolve())]
    parent_namespaces = json.loads(arguments.parent_namespaces)
    # 任何 mount 或目录写入前先验真隔离；直接在普通宿主启动驱动必须失败关闭。
    source.require_isolation({"parent_namespaces": parent_namespaces, "readonly_sources": []})
    if "staging" not in data.parts or data.exists() or any(data.is_relative_to(Path(root)) or Path(root).is_relative_to(data) for root in roots):
        raise replay.ReplayError("数据根须为独立、尚不存在的 staging 目录，不能与历史来源重叠")
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    for root in roots:
        subprocess.run(["mount", "--bind", root, root], check=True)
        subprocess.run(["mount", "-o", "remount,bind,ro", root], check=True)
    _create_world_roots(data)
    shutil.copytree(REPOSITORY / "tools", data / "tools", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    snapshot = replay.build_snapshot(data / "control/snapshot", recorded=Path(arguments.recorded))
    freeze = source.stage_freeze(data / "official/claude-code-2.1.226", Path(arguments.freeze))
    inputs = inputs_builder(data)
    state = {"schema_version": source.SCHEMA, "fixture_only": True, "purpose": "regression_fixture", "target_version": source.VERSION,
             "data_root": str(data), "snapshot": snapshot, "freeze": freeze, "inputs": inputs, "jobs": inputs["jobs"],
             "parent_namespaces": parent_namespaces, "readonly_sources": roots}
    state_path = source.write(data / "control/recorded-source.json", state)
    digest = source.binding(state_path)["sha256"]
    site = data / "control/site"
    site.mkdir(mode=0o700)
    (site / "sitecustomize.py").write_text(
        "import os,traceback\ntry:\n from " + activation_module + " import activate\n activate()\n"
        "except BaseException:\n traceback.print_exc()\n os._exit(97)\n", encoding="utf-8")
    from tools.official_client_capture.tests.project_ledger_fixture import FIXTURE_ONLY_ENV
    with mock.patch.dict(os.environ, {source.STATE_ENV: str(state_path), source.DIGEST_ENV: digest,
                                     FIXTURE_ONLY_ENV: "1", "PYTHONDONTWRITEBYTECODE": "1",
                                     "PYTHONPATH": os.pathsep.join([str(site), str(REPOSITORY)])}):
        return chain_runner(state_path, digest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仅隔离环境可运行的 Claude VC-0→VC-1 录制回归")
    parser.add_argument("--data", required=True)
    parser.add_argument("--recorded", required=True)
    parser.add_argument("--freeze", required=True)
    parser.add_argument("--parent-namespaces", required=True)
    args = parser.parse_args(argv)
    try:
        result = namespace_main(args)
    except BaseException as error:
        traceback.print_exc()
        print(json.dumps({"status": "unverified", "fixture_only": True, "registered_chains": [], "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
