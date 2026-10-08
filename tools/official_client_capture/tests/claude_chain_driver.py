"""候选 B 组录制恢复驱动：同一录制世界的真实封存、构建、取证、对账、批准与续跑。

只接受显式历史 2.1.226 来源。在 VC-0 前冻结 40 规则、全部原候选 probe 与两 Job 的调度聚合映射。
原始 linux/amd64 事实、R/P 字节和宿主收据保持；本次 ARM64 runtime 与预约独立生成并重验。
Docker、构建和审核输入沿用已有明示离线来源；不替换正式准入、封存、Store、账本或恢复判定。
本模块只允许 fixture_only/regression_fixture，不登记任何正式来源或完整升级认证。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_full_validation_driver as full
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_recovery as recovery_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests

CANDIDATE = full.CANDIDATE
LIVE_JOB = candidate_tests.LIVE_JOB
REFRESH_JOB = candidate_tests.REFRESH_JOB
REFRESH_PROBE = "v4-oauth-refresh"
EVOLVED_DEPENDENCY = "claude_run_candidate_capture.sh"
ACTIVATION_MODULE = "tools.official_client_capture.tests.claude_chain_driver"
AGGREGATION_SCHEMA = "claude-recorded-candidate-job-aggregation/v1"
PREPARED_SCHEMA = "claude-recorded-candidate-recovery-start/v1"
PREPARED_ENV = "CLAUDE_CANDIDATE_RECORDED_START"
SCENARIO_METHODS = {
    "reconcile-resume": "_scenario_candidate_reconcile_resume",
    "tool-evolution": "_scenario_candidate_tool_evolution",
}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _coordinate_projection(value: Any, original: str, target: str) -> Any:
    """聚合只改独占证据根的精确前缀；业务参数、probe、触发条件和原步骤其它字段保持。"""

    if isinstance(value, dict):
        return {key: _coordinate_projection(item, original, target) for key, item in value.items()}
    if isinstance(value, list):
        return [_coordinate_projection(item, original, target) for item in value]
    if isinstance(value, str) and (value == original or value.startswith(original + "/")):
        return target + value[len(original):]
    return value


def aggregate_scenarios(original: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """40/40 与所有原步骤保留，只在建 Campaign 前改变 B 组控制回归的调度粒度。"""

    plan.validate_scenario_manifest(original)
    candidates = [job for job in original["capture_jobs"] if job["phase"] == "candidate"]
    by_id = {job["id"]: job for job in candidates}
    if LIVE_JOB not in by_id or REFRESH_JOB not in by_id:
        raise replay.ReplayError("聚合来源缺少原主作业或独立刷新作业")
    non_refresh = [job for job in candidates if job["id"] != REFRESH_JOB]
    live = copy.deepcopy(by_id[LIVE_JOB])
    if any(job["suites"] != live["suites"] or job["required"] is not True for job in non_refresh):
        raise replay.ReplayError("B 组聚合不能改变原套件或把可选作业冒充必需作业")
    live.update(description="fixture_only：全部原非刷新候选步骤的调度聚合；不替代正式 Job 分母。", steps=[])
    for key in ("scenario_ids", "covers", "tool_dependencies", "required_scenario_receipts"):
        live[key] = sorted({value for job in non_refresh for value in job[key]})
    target_root, mapping = live["evidence_roots"][0], []
    for job in candidates:
        target_id = REFRESH_JOB if job["id"] == REFRESH_JOB else LIVE_JOB
        for index, step in enumerate(job["steps"], 1):
            projected = (copy.deepcopy(step) if target_id == REFRESH_JOB else
                         _coordinate_projection(step, job["evidence_roots"][0], target_root))
            number = index if target_id == REFRESH_JOB else len(live["steps"]) + 1
            if target_id == LIVE_JOB:
                live["steps"].append(projected)
            mapping.append({"original_job_id": job["id"], "original_step_index": index, "probe_id": step["probe_id"],
                "target_job_id": target_id, "target_step_index": number,
                "original_step_sha256": _digest(step), "projected_step_sha256": _digest(projected),
                "original_evidence_root": job["evidence_roots"][0],
                "target_evidence_root": by_id[REFRESH_JOB]["evidence_roots"][0] if target_id == REFRESH_JOB else target_root})
    grouped = copy.deepcopy(original)
    grouped["capture_jobs"] = [copy.deepcopy(job) for job in original["capture_jobs"] if job["phase"] == "official"]
    grouped["capture_jobs"].extend([live, copy.deepcopy(by_id[REFRESH_JOB])])
    plan.validate_scenario_manifest(grouped)
    rules = sorted({rule for job in grouped["capture_jobs"] if job["phase"] == "official" for rule in job["covers"]})
    if len(rules) != 40:
        raise replay.ReplayError("候选聚合来源必须保留完整40规则")
    plan.validate_full_coverage([argparse.Namespace(**{**job, "job_id": job["id"]}) for job in grouped["capture_jobs"]], rules)
    return grouped, mapping


def _extend_refresh(original: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """已有完整录制链没执行刷新；B 组从同一版本模板声明并补回官方与候选的完整刷新 Job。"""

    result = copy.deepcopy(original)
    identifiers = {job["id"] for job in result["capture_jobs"]}
    example = result["capture_jobs"][0]["steps"][0]["argv"]
    facts = {flag: example[example.index(flag) + 1]
             for flag in ("--fw-f-v5-version-facts", "--fw-f-v5-version-facts-sha256")}
    for job in template["capture_jobs"]:
        if job["id"] not in {"official-synthetic-oauth-refresh", REFRESH_JOB} or job["id"] in identifiers:
            continue
        projected = copy.deepcopy(job)
        for step in projected["steps"]:
            for flag, value in facts.items():
                step["argv"][step["argv"].index(flag) + 1] = value
        result["capture_jobs"].append(projected)
    scenario_ids = {row["scenario_id"] for row in result["evidence_scenarios"]}
    result["evidence_scenarios"].extend(copy.deepcopy(row) for row in template["evidence_scenarios"]
                                        if REFRESH_PROBE in row["probe_ids"] and row["scenario_id"] not in scenario_ids)
    result["probe_catalog"]["probe_count"] = len({step["probe_id"] for job in result["capture_jobs"] if job["phase"] == "official"
        for step in job["steps"] if plan.step_model(step["probe_id"], step["argv"]) is None})
    plan.validate_scenario_manifest(result)
    return result


def _aggregate_labels(original: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    labels = copy.deepcopy(original)
    present = {entry["job_id"] for entry in labels["entries"]}
    labels["entries"].extend(copy.deepcopy(entry) for entry in template["entries"]
                            if entry["job_id"] in {"official-synthetic-oauth-refresh", REFRESH_JOB} and entry["job_id"] not in present)
    live = {"job_id": LIVE_JOB, "side": "candidate", "rules": []}
    entries = []
    for entry in labels["entries"]:
        if entry["side"] == "candidate" and entry["job_id"] != REFRESH_JOB:
            live["rules"].extend(copy.deepcopy(entry["rules"]))
        else:
            entries.append(entry)
    labels["entries"] = [*entries, live]
    return labels


def inputs(data: Path) -> dict[str, Any]:
    """完整控制输入先形成原件，再写独立聚合副本和映射；一切在 source state 与 Campaign 冻结之前完成。"""

    configured = full._inputs(data)
    original_binding = configured["bindings"]["scenarios"]
    original = source.read(source.bound(original_binding, root=data))
    template_path, label_template_path = plan.scenario_manifest_path("2.1.285"), plan.evidence_labels_path("2.1.285")
    extended = _extend_refresh(original, source.read(template_path))
    grouped, mapping = aggregate_scenarios(extended)
    root = data / "control/candidate-recovery"
    root.mkdir(mode=0o700)
    extended_path = source.write(root / "original-scenarios.json", extended)
    grouped_path = source.write(root / "grouped-scenarios.json", grouped)
    labels = _aggregate_labels(source.read(source.bound(configured["bindings"]["labels"], root=data)), source.read(label_template_path))
    labels_path = source.write(root / "grouped-labels.json", labels)
    aggregation = source.write(root / "aggregation.json", {
        "schema_version": AGGREGATION_SCHEMA, "fixture_only": True, "purpose": "regression_fixture",
        "baseline_scenarios": original_binding, "refresh_template": source.binding(template_path),
        "original_scenarios": source.binding(extended_path), "grouped_scenarios": source.binding(grouped_path),
        "original_labels": configured["bindings"]["labels"], "label_template": source.binding(label_template_path),
        "grouped_labels": source.binding(labels_path), "rules": configured["bindings"]["rules"], "step_mapping": mapping,
        "scope": "只改变候选 B 组控制回归调度粒度；原40规则和全部步骤保留，不替代原正式Job分母。",
        "formal_version_semantics_certified": False,
    })
    source.write(root / "failures.json", {})
    control = source.write(root / "control.json", {"schema_version": source.RECOVERY_CONTROL_SCHEMA,
        "fixture_only": True, "purpose": "regression_fixture", "control_root": str(root), "allowed_failure_probe_ids": [REFRESH_PROBE]})
    configured["bindings"].update(scenarios=source.binding(grouped_path), labels=source.binding(labels_path),
                                    candidate_aggregation=source.binding(aggregation))
    configured["candidate_recovery_control"] = source.binding(control)
    configured["jobs"] = {job["id"]: [step["probe_id"] for step in job["steps"]]
                            for job in grouped["capture_jobs"] if job["phase"] == "official"}
    configured["candidate_jobs"] = {job["id"]: [step["probe_id"] for step in job["steps"]]
                                     for job in grouped["capture_jobs"] if job["phase"] == "candidate"}
    validate_aggregation({"data_root": str(data), "inputs": configured})
    return configured


def validate_aggregation(state: dict[str, Any]) -> dict[str, Any]:
    """逐次复算映射和来源；丢步骤、重复归属、规则遗漏或冻结后改字节均失败关闭。"""

    data = Path(state["data_root"])
    configured = state["inputs"]
    aggregation = source.read(source.bound(configured["bindings"]["candidate_aggregation"], root=data))
    if (aggregation.get("schema_version") != AGGREGATION_SCHEMA or aggregation.get("fixture_only") is not True
            or aggregation.get("purpose") != "regression_fixture" or aggregation.get("formal_version_semantics_certified") is not False):
        raise replay.ReplayError("B 组聚合合同身份非法")
    baseline = source.read(source.bound(aggregation["baseline_scenarios"], root=data))
    template = source.read(source.bound(aggregation["refresh_template"]))
    original = source.read(source.bound(aggregation["original_scenarios"], root=data))
    if original != _extend_refresh(baseline, template):
        raise replay.ReplayError("聚合原步骤与完整来源及刷新声明不闭合")
    expected, mapping = aggregate_scenarios(original)
    actual = source.read(source.bound(aggregation["grouped_scenarios"], root=data))
    if actual != expected or aggregation["step_mapping"] != mapping:
        raise replay.ReplayError("候选聚合步骤、规则或映射有遗漏、重复或漂移")
    if (configured["bindings"]["scenarios"] != aggregation["grouped_scenarios"]
            or configured["bindings"]["labels"] != aggregation["grouped_labels"]
            or configured["bindings"]["rules"] != aggregation["rules"]):
        raise replay.ReplayError("聚合绑定不是本次冻结输入")
    original_labels = source.read(source.bound(aggregation["original_labels"], root=data))
    label_template = source.read(source.bound(aggregation["label_template"]))
    label_path = source.bound(aggregation["grouped_labels"], root=data)
    if source.read(label_path) != _aggregate_labels(original_labels, label_template):
        raise replay.ReplayError("候选聚合标签未完整保留原来源")
    plan.load_evidence_labels(label_path, expected_version=source.VERSION, scenario_payload=actual)
    rules = source.read(source.bound(aggregation["rules"], root=data))["required_rules"]
    jobs = [argparse.Namespace(**{**job, "job_id": job["id"]}) for job in actual["capture_jobs"]]
    if len(rules) != 40:
        raise replay.ReplayError("B 组聚合不能裁剪完整40规则")
    plan.validate_full_coverage(jobs, rules)
    for phase, name in (("official", "jobs"), ("candidate", "candidate_jobs")):
        wanted = {job["id"]: [step["probe_id"] for step in job["steps"]] for job in actual["capture_jobs"] if job["phase"] == phase}
        if configured[name] != wanted or (phase == "official" and state.get("jobs", wanted) != wanted):
            raise replay.ReplayError("聚合作业白名单与冻结场景不符")
    source.recovery_control(state)
    return aggregation


@contextlib.contextmanager
def installed(state_path: Path, digest: str) -> Iterator[Any]:
    state = source.load_state(state_path, digest)
    validate_aggregation(state)
    with full.installed(state_path, digest) as adapter:
        yield adapter


def activate() -> None:
    """受监督子动作承接同一聚合与来源合同，安装失败不得回落到真实客户端。"""

    full.vc1.activate(installer=installed)


def prepare(state_path: Path, digest: str) -> Path:
    """沿用已有真实 VC-0→VC-4 形成链；不预写官方终态或候选准入成功事实。"""

    state = source.load_state(state_path, digest)
    validate_aggregation(state)
    first = full.vc1.run_chain(state_path, digest, installer=installed)
    continuation = first["continuation"]
    with installed(state_path, digest):
        world = full.vc1.load_continuation(source.bound(continuation), continuation["sha256"])
        data, campaign = world["data"], world["campaign"]
        full.classify_and_prepare(data, campaign)
        built = full.build_candidate(data, campaign)
    return source.write(data / "control/candidate-recovery-start.json", {
        "schema_version": PREPARED_SCHEMA, "fixture_only": True, "purpose": "regression_fixture", "scope": "VC-0→VC-4",
        "state": source.binding(state_path), "continuation": continuation, "build_receipt": built["build_receipt"],
        "checkpoint": built["checkpoint"], "candidate_id": CANDIDATE, "candidate_revision": 1,
        "runtime_source": source.binding(data / "control/full-runtime-source.json"),
        "formal_version_semantics_certified": False,
    })


class ExecutionToolCopy:
    """仅同步本世界执行副本的单个演进文件；实际 Store 仍在隔离源码树上现算身份。"""

    def __init__(self, data: Path) -> None:
        self.data, self.originals = data, {}

    def write(self, key: str, content: bytes) -> None:
        path = self.data / "tools" / key
        if path.resolve() != path or not path.is_relative_to(self.data / "tools") or not path.is_file():
            raise replay.ReplayError("演进文件不在本世界执行副本")
        self.originals.setdefault(path, path.read_bytes())
        path.write_bytes(content)

    def restore(self) -> None:
        for path, raw in self.originals.items():
            path.write_bytes(raw)
        self.originals.clear()


class ClaudeCandidateChainDriver(unittest.TestCase):
    """B 组只保留控制动作辅助方法；来源全部转至真实录制、封存与构建形成链。"""

    run_main = staticmethod(seal_tests.run_main)
    reconcile = recovery_tests.RecoveryChainCase.reconcile
    approve = recovery_tests.RecoveryChainCase.approve

    def setUp(self) -> None:
        super().setUp()
        if os.environ.get(source.STATE_ENV):
            self.state_path = Path(os.environ[source.STATE_ENV])
            self.state_digest = os.environ[source.DIGEST_ENV]
            state = source.load_state(self.state_path, self.state_digest)
            self.data = Path(state["data_root"])
            self.root = self.data.parent
            return
        configured = os.environ.get("CLAUDE_RECORDED_CHAIN_RECORD_DIR") or os.environ.get("CLAUDE_FAULT_CHAIN_RECORDS")
        if configured:
            parent = Path(configured) if os.environ.get("CLAUDE_RECORDED_CHAIN_RECORD_DIR") else Path(configured).parent
            parent = parent.resolve() / "candidate-recorded-worlds"
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root = Path(tempfile.mkdtemp(prefix="candidate-b-", dir=parent)).resolve()
        else:
            directory = tempfile.TemporaryDirectory(prefix="claude-candidate-recorded-")
            self.addCleanup(directory.cleanup)
            self.root = Path(directory.name).resolve()
        self.data = self.root / "staging/data"

    def recorded_entry(self, scenario: str) -> bool:
        """旧场景入口先建立独立的断网与只读来源世界；内部方法执行原有全部断言后才返回。"""

        if scenario not in SCENARIO_METHODS:
            raise replay.ReplayError("未知候选 B 组场景")
        if os.environ.get(source.STATE_ENV):
            return False
        if platform.system() != "Linux" or platform.machine() not in {"arm64", "aarch64"} or os.geteuid() != 0:
            raise replay.ReplayError("候选录制恢复链未验证：需要 Linux ARM64 root 与 mount/net namespace")
        recorded, frozen = os.environ.get("CLAUDE_2_1_226_RELAY_FIXTURE"), os.environ.get("CLAUDE_2_1_226_FREEZE_FIXTURE")
        if not recorded or not frozen:
            raise replay.ReplayError("候选 B 组需要显式历史 relay 与 freeze；不允许回落 fake_admission")
        command = ["unshare", "-m", "-n", sys.executable, "-B", "-m", ACTIVATION_MODULE,
            "--data", str(self.data), "--recorded", recorded, "--freeze", frozen, "--scenario", scenario,
            "--parent-namespaces", json.dumps({kind: os.readlink(f"/proc/self/ns/{kind}") for kind in ("mnt", "net")})]
        try:
            completed = subprocess.run(command, cwd=full.vc1.REPOSITORY, capture_output=True, text=True,
                                       timeout=850, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired as error:
            for name, raw in (("stdout.log", error.stdout or b""), ("stderr.log", error.stderr or b"")):
                (self.root / name).write_bytes(raw.encode() if isinstance(raw, str) else raw)
            raise
        (self.root / "stdout.log").write_text(completed.stdout, encoding="utf-8")
        (self.root / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr[-12000:] + completed.stdout[-12000:])
        result = source.read(self.data / "control/candidate-recovery-case-result.json")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scenario_method"], SCENARIO_METHODS[scenario])
        return True

    @contextlib.contextmanager
    def source_context(self) -> Iterator[None]:
        with installed(self.state_path, self.state_digest):
            yield

    def chain_patched(self) -> contextlib.AbstractContextManager[None]:
        return self.source_context()

    def candidate_world(self) -> Path:
        """只消费同一世界已由真实命令形成的 VC-4 起点、官方封存、Store 和两本账。"""

        state = source.load_state(self.state_path, self.state_digest)
        ready_path = source.bound(json.loads(os.environ[PREPARED_ENV]), root=self.data / "control")
        if ready_path != self.data / "control/candidate-recovery-start.json":
            raise replay.ReplayError("候选恢复起点不是本世界的固定路径")
        ready = source.read(ready_path)
        if (ready.get("schema_version") != PREPARED_SCHEMA or ready.get("fixture_only") is not True
                or ready.get("purpose") != "regression_fixture" or ready.get("scope") != "VC-0→VC-4"
                or ready.get("formal_version_semantics_certified") is not False or ready.get("state") != source.binding(self.state_path)
                or ready.get("candidate_id") != CANDIDATE or ready.get("candidate_revision") != 1):
            raise replay.ReplayError("候选恢复起点不是本次录制世界的真实 VC-4")
        with self.source_context():
            continuation = ready["continuation"]
            world = full.vc1.load_continuation(source.bound(continuation, root=self.data), continuation["sha256"])
            campaign, manifest = world["campaign"], world["manifest"]
            checkpoint, _ = claude_upgrade._replay_vc_checkpoint(campaign, claude_upgrade._vc_campaign_plan(campaign, manifest), "VC-4", revision=1)
            if source.bound(ready["checkpoint"], root=campaign) != checkpoint:
                raise replay.ReplayError("候选恢复起点 checkpoint 坐标漂移")
            runtime_path = source.bound(ready["runtime_source"], root=self.data)
            if runtime_path != self.data / "control/full-runtime-source.json":
                raise replay.ReplayError("候选观察来源不是本世界固定路径")
            runtime_source = source.read(runtime_path)
            if runtime_source["build_receipt"] != ready["build_receipt"]:
                raise replay.ReplayError("候选运行观察与本次真实构建来源不符")
            source.bound(ready["build_receipt"], root=campaign)
            self.ledger_dir = claude_upgrade._campaign_timing_ledger_dir(campaign, manifest)
            self.project_root = project.find_project_ledger(campaign, client="claude")
            self.tool_identity = manifest["tool_identity"]
            self.tool_copy = self.data / "tools/official_client_capture"
            self.tree = ExecutionToolCopy(self.data)
            self.failures_file = self.data / "control/candidate-recovery/failures.json"
            self.call_log = self.data / "control/candidate-recovery/calls.jsonl"
            head = timing.phase_ledger_state(self.ledger_dir)
            if head["active_phase"] is None and head["completed_phases"][-1:] == ["VC-4"]:
                timing.append_event(self.ledger_dir, event_id="recorded-candidate-recovery-vc5-start", phase="VC-5",
                    event_type="stage_started", next_action="真实录制候选恢复首批开始", receipts=[])
            else:
                raise replay.ReplayError("候选恢复起点时间账不是已完成 VC-4 的原账本")
        self.assert_recorded_sources_untouched()
        return campaign

    def fail_probe(self, probe: str, times: int) -> None:
        state = source.load_state(self.state_path, self.state_digest)
        configured = source.recovery_control(state)
        if configured is None or probe not in configured[1]["allowed_failure_probe_ids"] or type(times) is not int or times < 0:
            raise replay.ReplayError("故障不在冻结的候选白名单")
        self.failures_file.write_text(json.dumps({probe: times}), encoding="utf-8")

    def relay_probe_calls(self) -> list[str]:
        if not self.call_log.exists():
            return []
        rows = [json.loads(line) for line in self.call_log.read_text(encoding="utf-8").splitlines() if line]
        for row in rows:
            decision = source.read(source.bound(row["decision"], root=self.data))
            if decision["operation"] != row["operation"] or decision["probe_id"] != row["probe_id"] or decision["copy_completed"] is not True:
                raise replay.ReplayError("候选调用日志与真实复制决策不符")
        return [row["probe_id"] for row in rows]

    def candidate_run(self, campaign: Path, *, fail_refresh: bool = False) -> dict[str, Any]:
        self.fail_probe(REFRESH_PROBE, 1 if fail_refresh else 0)
        with self.source_context(), mock.patch.object(claude_upgrade, "JOB_RETRY_LIMIT", 0):
            code, result, stderr = self.run_main(["capture-candidate", "run", "--campaign-dir", str(campaign),
                "--candidate-id", CANDIDATE, "--acknowledge-live-requests"])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "failed" if fail_refresh else "awaiting_receipts"), stderr)
        self.fail_probe(REFRESH_PROBE, 0)
        return result

    def candidate_reconcile(self, campaign: Path, attempt_id: str, *extra: str) -> dict[str, Any]:
        return self.reconcile(campaign, attempt_id, *extra)

    def candidate_approve(self, campaign: Path, attempt_id: str, preview: dict[str, Any]) -> dict[str, Any]:
        return self.approve(campaign, attempt_id, preview)

    def candidate_resume(self, campaign: Path, preview_path: str) -> dict[str, Any]:
        with self.source_context():
            code, result, stderr = self.run_main(["resume", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE,
                "--rerun-failed", "--recovery-preview", preview_path, "--acknowledge-live-requests"])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
        return result

    def candidate_attempt(self, campaign: Path, attempt_id: str) -> tuple[Path, dict[str, Any]]:
        with self.source_context():
            return claude_upgrade._load_capture_attempt(campaign, "candidate", CANDIDATE, attempt_id)

    def candidate_job_roots(self, campaign: Path) -> dict[str, Path]:
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with self.source_context():
            layout = capture._capture_layout(campaign, manifest)
            return {job.job_id: capture._host_path(job.evidence_roots[0], layout)
                    for job in claude_upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=CANDIDATE)}

    def assert_recorded_sources_untouched(self) -> dict[str, Any]:
        state = source.load_state(self.state_path, self.state_digest)
        replay.assert_source_untouched(state["snapshot"])
        for path, expected in state["freeze"]["source_sentinel"].items():
            if replay._stat(Path(path)) != expected:
                raise replay.ReplayError("候选 B 组改变了历史冻结原件")
        index = replay.load_snapshot(state["snapshot"])
        for relative, expected in index["files"].items():
            source_id, _, member = relative.partition("/")
            origin_root = Path(index["sources"][source_id])
            original = origin_root / member
            replay._regular(original, origin_root)
            if replay._digest(original) != expected:
                raise replay.ReplayError("候选 B 组的历史来源原字节发生漂移")
        freeze_origin = next(Path(path).parent for path in state["freeze"]["source_sentinel"] if Path(path).name == "freeze.json")
        for relative, expected in state["freeze"]["files"].items():
            original = freeze_origin / relative
            replay._regular(original, freeze_origin)
            if replay._digest(original) != {name: expected[name] for name in ("sha256", "bytes")}:
                raise replay.ReplayError("候选 B 组的历史冻结原字节发生漂移")
        return {"fixture_only": True, "purpose": "regression_fixture", "snapshot": state["snapshot"],
            "source_file_count": len(index["files"]), "source_probe_count": len(index["probes"]),
            "source_sentinel_count": len(index["source_sentinel"]), "origin_platform": "linux/amd64",
            "target_version": source.VERSION, "network_requests_this_run": 0, "formal_version_semantics_certified": False}

    def recorded_audit_report(self, record: dict[str, Any]) -> None:
        from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as cases

        if not cases._records_complete([record], [record["scenario"]]):
            raise replay.ReplayError("候选录制恢复场景未完成六项真实检查")
        facts = self.assert_recorded_sources_untouched()
        source.write(self.data / "control/candidate-recovery-case-result.json", {
            "status": "passed", "scenario_method": self._testMethodName, "source": facts, "audit": record,
            "registered_chains": [], "full_vc0_vc6_certified": False,
        })


def run_scenario(state_path: Path, digest: str, scenario: str) -> dict[str, Any]:
    prepared = prepare(state_path, digest)
    from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as cases

    data = Path(source.load_state(state_path, digest)["data_root"])
    output = io.StringIO()
    with mock.patch.dict(os.environ, {PREPARED_ENV: json.dumps(source.binding(prepared))}):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.TestSuite([
            cases._CandidateFaultScenarios(SCENARIO_METHODS[scenario])]))
    path = source.write(data / "control/candidate-recovery-test-run.json", {
        "fixture_only": True, "purpose": "regression_fixture", "scenario": scenario,
        "tests_run": result.testsRun, "skipped": len(result.skipped), "errors": len(result.errors),
        "failures": len(result.failures), "output": output.getvalue(),
    })
    if result.testsRun != 1 or result.skipped or not result.wasSuccessful():
        raise replay.ReplayError(f"候选 B 组真实恢复场景失败；凭证 {path}：{output.getvalue()}")
    return source.read(data / "control/candidate-recovery-case-result.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="隔离录制候选 B 组恢复场景；不签正式认证")
    for name in ("data", "recorded", "freeze", "parent-namespaces"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIO_METHODS), required=True)
    arguments = parser.parse_args(argv)
    try:
        result = full.vc1.namespace_main(arguments, inputs_builder=inputs,
            chain_runner=lambda path, digest: run_scenario(path, digest, arguments.scenario), activation_module=ACTIVATION_MODULE)
    except BaseException as error:
        traceback.print_exc()
        print(json.dumps({"status": "unverified", "fixture_only": True, "registered_chains": [], "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
