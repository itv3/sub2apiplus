"""Claude transient 恢复段连续链的离线驱动；不单独登记认证通过。

VC-0/1 使用批次夹具明确的已封存输入，VC-3/4、候选 run/seal、失败 builder、恢复 apply、ar1 run/seal 与 b1 builder
执行产品代码。宿主/Docker/网关来自 CandidateFixture；中继替身按已有连接序列写原始字节，并由可改控制文件注入一条头值故障。
画像仅选择一个请求头检查，官方评估字节采用 evaluator fixture；候选评估必须来自实际 seal 的 evaluation-manifest。
不替换候选阶段结果、attempt 读侧、恢复裁定、权限/封存边界、Store、请求核算或断言结论。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_assertion_profile as assertion_profile
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import claude_upgrade_store as candidate_store
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_boundary_receipt_fixture as boundary_fixture
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator_fixture
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
from tools.official_client_capture.tests import test_claude_upgrade_evaluation_recovery as evaluation_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests

CANDIDATE = candidate_tests.CANDIDATE
FAILED_JOB = candidate_tests.LIVE_JOB
FAILED_PROBE = "v4-replay-baseline"
RULE = "SPEC-HDR-001"
HOST = "arm64-transient-fixture"
BUILDER = Path(__file__).resolve().parents[1] / "claude_build_rule_assertion_results.py"


def _write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


class ClaudeTransientChainDriver(batch_tests._RealTreeBatchCase):
    """只借用既有输入驱动，不继承那些测试用例；所有新段产物均由真实命令产生。"""

    recover = evaluation_tests.EvaluationRecoveryChainTests.recover
    preview = evaluation_tests.EvaluationRecoveryChainTests.preview

    def setUp(self) -> None:
        super().setUp()
        self.runtime = candidate_tests.CandidateFixture(methodName="runTest")
        self.runtime.tool_identity = self.tool_identity
        self.runtime.setUp()
        self.addCleanup(self.runtime.doCleanups)
        # 批次与执行源共享同一宿主数据根，避免用别处的证据或总账拼成连续链。
        self.root, self.data = self.runtime.root, self.runtime.data
        self.relay_control = _write(self.root / "transient-relay-control.json", {"bad_probe": FAILED_PROBE})
        self.mutations = _write(self.root / "transient-relay-mutations.json", {})
        self.install_relay_source()

    def install_relay_source(self) -> None:
        """中继输入保持真实序列与完整候选封套；头值从单独控制文件读取，修复不改工具源码或旧字节。"""

        prefix, tail = candidate_tests.CANDIDATE_RELAY.rsplit("sys.exit(0)", 1)
        source, source_tail = seal_tests.FULL_FAKE_RELAY.rsplit("sys.exit(0)", 1)
        self.assertFalse(tail.strip() or source_tail.strip())
        marker = '        stream += f"{method} {target} HTTP/1.1'
        self.assertEqual(source.count(marker), 1)
        source = source.replace(marker, '''        version = "2023-01-01" if probe == json.load(open(os.environ["CLAUDE_TRANSIENT_RELAY_CONTROL"], encoding="utf-8")).get("bad_probe") else "2023-06-01"
        stream += f"{method} {target} HTTP/1.1''')
        source = source.replace(r'Host: {host}\r\nContent-Length:', r'Host: {host}\r\nAnthropic-Version: {version}\r\nContent-Length:')
        envelope = '''
manifest.update(side="candidate", run_id=value("--run-id"))
manifest["candidate_gateway"] = {
    "base_url": "http://" + os.environ["SERVICE_CONTAINER"] + ":8080",
    "account_id": int(os.environ["ACCOUNT_ID"]), "api_key_id": int(os.environ["API_KEY_ID"]),
    "trigger": os.environ["GATEWAY_TRIGGER"], "client_authentication": "ANTHROPIC_AUTH_TOKEN", "oauth_credentials_read": False,
}
manifest["upstream"].update(relay_mode="connect", relay_listen_port=int(os.environ["RELAY_PORT"]))
manifest["hosts_recovery"] = HOSTS_RECOVERY
manifest["m_binding"] = {"complete": True, "requirements": {key: True for key in REQUIREMENTS}}
manifest["candidate_observation"] = {
    "client_outcome_valid": True, "message_request_expectation_met": True,
    "synthetic_plan_closed": True if not live else None, "dimension_result": "passed",
}
manifest["dimension_evidence"].update(side="candidate", observation_only=True)
if os.environ["GATEWAY_TRIGGER"] == "gateway-account-refresh":
    with open(os.path.join(output, "results", "gateway-trigger.json"), "w", encoding="utf-8") as handle:
        json.dump({"schema_version": "claude-candidate-gateway-trigger/v1", "trigger": "gateway-account-refresh",
                   "account_id": int(os.environ["ACCOUNT_ID"]), "valid": True}, handle)
with open(os.path.join(output, "relay-manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle)
sys.exit(0)
'''
        envelope = "\nHOSTS_RECOVERY = " + repr(evidence.CANDIDATE_HOSTS_RECOVERY) + "\nREQUIREMENTS = " + repr(sorted(evidence.CANDIDATE_M_REQUIREMENTS)) + "\n" + envelope
        relay = self.runtime.tool_copy / "claude_fw_e_relay.py"
        relay.write_text(prefix + source + envelope, encoding="utf-8")
        relay.chmod(0o700)

    def build_campaign(self, **kwargs: Any) -> Path:
        """在两本账注册之前冻结运行坐标、标签和候选模板行，沿用真实全量场景清单。"""

        campaign = super().build_campaign(**kwargs)
        manifest = upgrade.load_campaign_manifest(campaign)
        labels = _write(campaign / "inputs" / "evidence-labels.json", json.loads(plan.evidence_labels_path(capture_tests.TARGET).read_bytes()))
        configuration = {
            **manifest["configuration"], **candidate_tests.CANDIDATE_CONFIGURATION,
            "runtime_image": capture_tests.IMAGE, "capture_root": str(self.data), "capture_tool_root": str(self.runtime.tool_copy),
            "capture_container": capture_tests.CAPTURE, "synthetic_capture_container": capture_tests.SYNTHETIC,
            "claude_bin": f"/opt/claude-{capture_tests.TARGET}/claude", "model": "claude-sonnet-5", "upstream_ip": "160.79.104.10",
            "credentials_file": "/root/.claude/.credentials.json", "ca_signing_pem": "/opt/mitm/mitmproxy-ca.pem",
            "ca_cert": "/opt/mitm/mitmproxy-ca-cert.pem",
        }
        manifest = seal_tests.rewrite_manifest(campaign, configuration=configuration, inputs={**manifest["inputs"], "evidence_labels": {
            "path": labels.relative_to(campaign).as_posix(), "sha256": upgrade.file_sha256(labels),
        }})
        jobs = capture._expand_phase_jobs(campaign, manifest, "candidate", candidate_id=None)
        seal_tests.rewrite_manifest(campaign, jobs=[{"id": job.job_id, "phase": job.phase,
            "execution_sha256": capture._job_execution_sha256(job), "evidence_roots": list(job.evidence_roots)} for job in jobs])
        return campaign

    def prepare_runtime(self, campaign: Path) -> None:
        """环境来源承接已真实构建的候选身份；真实准入继续核 Store、VC-4、源码及镜像引用。"""

        self.build = json.loads((campaign / "candidates" / CANDIDATE / "build-receipt.json").read_bytes())
        gateway = copy.deepcopy(candidate_tests.BASE_GATEWAY)
        gateway["service"]["image_id"] = self.build["image"]["image_id"]
        _write(self.runtime.gateway_file, gateway)
        self.runtime.activation_fact = {
            "schema_version": candidate_capture.ACTIVATION_FACT_SCHEMA, "source": "sub2api-runtime",
            "event_type": "claude_profile_observed", "event_id": "a" * 64, "profile_mode": "validation_candidate",
            "claude_version": capture_tests.TARGET, "profile_digest": self.build["profile"]["profile_digest"],
            "image_id": self.build["image"]["image_id"],
        }

    @contextlib.contextmanager
    def source_context(self) -> Iterator[None]:
        """只替换外部观测和进程来源；不替换 formal_admission、候选读侧、seal 或 provenance。"""

        def probe(argv: Any, **options: Any):
            if list(argv[:5]) == ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps([self.build["image"]["reference"]]), "")
            return self.runtime.host.run(argv, **options)

        with self.runtime.patched(), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(capture, "_run_probe_command", probe))
            stack.enter_context(mock.patch.object(candidate_capture, "_collect_gateway_facts", self.runtime.fake_gateway_facts))
            stack.enter_context(mock.patch.object(candidate_capture, "_read_activation_fact", self.runtime.fake_activation_fact))
            stack.enter_context(mock.patch.object(candidate_capture, "observe_service_image", lambda *_a, **_k: self.build["image"]["image_id"]))
            stack.enter_context(mock.patch.object(candidate_capture, "host_facts", return_value={"host_id": HOST, "architecture": "linux/arm64"}))
            stack.enter_context(mock.patch.dict(os.environ, {
                "FAKE_CANDIDATE_HOST": str(self.runtime.fake_host), "FAKE_CANDIDATE_CONTROL": str(self.runtime.candidate_control),
                "FAKE_CANDIDATE_CALLS": str(self.runtime.candidate_calls_file), "FAKE_GATEWAY_FILE": str(self.runtime.gateway_file),
                "ADMIN_BEARER_TOKEN_FILE": str(self.runtime.admin_token), "FAKE_RELAY_SEQUENCES": str(seal_tests.SEQUENCES),
                "FAKE_RELAY_MUTATIONS": str(self.mutations), "CLAUDE_TRANSIENT_RELAY_CONTROL": str(self.relay_control),
            }))
            yield

    def source_command(self, argv: list[Any]) -> tuple[int, dict | None, str]:
        with self.source_context():
            return batch_tests.run_main(argv)

    def write_clients(self, campaign: Path, attempt_root: Path, attempt: dict) -> list[str]:
        """Mac 与边界收据属于外部来源；严格收据合同由共享 fixture 构造，封存仍由产品代码逐项复核。"""

        from tools.official_client_capture.tests import claude_client_receipt_fixture

        values = {"campaign_id": campaign.name, "candidate_id": CANDIDATE, "attempt_id": attempt_root.name,
                  "run_nonce": attempt["run_nonce"]}
        original_attempt = (attempt_root / "attempt.json").read_bytes()
        argv = []
        for client in ("claude-desktop", "claude-vscode"):
            path = claude_client_receipt_fixture.write_receipt(attempt_root, client, values)
            argv += ["--client-evidence", f"{client}={path}"]
        for check in ("codex-final-wire-isolation", "third-party-ingress-rejection"):
            boundary_fixture.write_boundary_receipt(attempt_root, check)
        self.assertEqual((attempt_root / "attempt.json").read_bytes(), original_attempt,
                         "客户端输入夹具不能改写真实 run 已发布的 attempt 或身份")
        return argv

    def seal(self, campaign: Path, attempt_id: str, *, clients: list[str] | None = None, recovery: str | None = None) -> dict:
        argv = ["capture-candidate", "seal", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--attempt-id", attempt_id]
        argv += ["--attempt-recovery", recovery] if recovery else list(clients or [])
        code, preview, stderr = self.source_command(argv)
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
        code, result, stderr = self.source_command([*argv, "--approve-seal-sha256", preview["review_sha256"], "--approved-by", "连续链审核"])
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(result, stderr)
        return result

    def evaluation_context(self, campaign: Path) -> dict[str, Any]:
        """一个可定位到实际 Job 的请求头检查；官方字节为固定来源，候选来源由每代 seal 绑定。"""

        evaluation = campaign / "transient-evaluation"
        rule_manifest = {**evaluator_fixture.RULE_MANIFEST, "required_rules": [RULE]}
        ledger = copy.deepcopy(evaluator_fixture.LEDGER)
        ledger["scenario_only_owner_ids"] = []
        ledger["assertions"] = [row for row in ledger["assertions"] if row["assertion_id"] == RULE]
        scenarios = copy.deepcopy(evaluator_fixture.SCENARIOS_PAYLOAD)
        scenarios["evidence_scenarios"] = [row for row in scenarios["evidence_scenarios"] if row["scenario_id"] == "transport.http"]
        scenarios["evidence_scenarios"][0]["covers"] = [RULE]
        templates = copy.deepcopy(evaluator_fixture.TEMPLATES)
        templates["templates"] = {RULE: templates["templates"][RULE]}
        templates["templates"][RULE]["checks"] = templates["templates"][RULE]["checks"][:1]
        profile = assertion.build_expectations(claude_version=capture_tests.TARGET, ledger=ledger, scenarios_payload=scenarios,
            templates=templates, release_wire=evaluator_fixture.RELEASE_WIRE, rule_manifest=rule_manifest)
        profile_path = evaluation / "assertion-profile.json"
        profile_path.parent.mkdir(parents=True, mode=0o700)
        profile_path.write_bytes(assertion.render_expectations(profile))
        return {"campaign": campaign, "evaluation": evaluation, "profile_path": profile_path,
                "profile_sha256": upgrade.file_sha256(profile_path), "rules_path": _write(evaluation / "target-rules.json", rule_manifest),
                "official": evaluator_fixture.write_side(evaluation / "official", capture_id="transient-official")}

    def dispatch_builder(self, context: dict[str, Any], sequence: int, *, baseline: int) -> tuple[int, dict | None, str]:
        """真实父批次调度 builder，候选配置指向该基线实际增量封存的 manifest，禁止构造候选评估文件。"""

        campaign = context["campaign"]
        with self.source_context():
            stage = upgrade._load_stage_result(campaign, "capture-candidate", CANDIDATE, _baseline=baseline)
        candidate = compare.candidate_evaluation_source(campaign, upgrade.load_campaign_manifest(campaign), stage)
        target = upgrade._stage_write_target(campaign, CANDIDATE, baseline, "assertions")
        config = {
            "assertion_profile": str(context["profile_path"]), "rule_manifest": str(context["rules_path"]),
            "expected_profile_sha256": context["profile_sha256"], "official_evidence_root": str(context["official"].evidence_root),
            "candidate_evidence_root": candidate["evidence_root"], "official_capture_manifest": str(context["official"].manifest_path),
            "candidate_capture_manifest": str(campaign / stage["assertion_context"]["evaluation_manifest"]["path"]),
            "official_evidence_prefix": "official-run", "candidate_evidence_prefix": "candidate-run", "target_version": capture_tests.TARGET,
            "candidate_id": CANDIDATE, "profile_id": "transient-header", "profile_digest": "d" * 64,
            "official_package_digest": "1" * 64, "candidate_package_digest": "2" * 64, "comparison_package_digest": "3" * 64,
            "official_authority": dict(evaluator_fixture.AUTHORITY), "rules": [RULE], "campaign_dir": str(campaign),
        }
        path = _write(context["evaluation"] / f"builder-config-b{baseline}.json", config)
        action = {"action_id": "assert-rules", "operation": "VC-5:assert-rules", "timeout_seconds": 600.0,
            "command": [sys.executable, str(BUILDER), "--config", str(path), "--output", str(target / "results.json"),
                        "--results-dir", str(target / "machine"), "--evaluation-baseline", str(baseline)], "item_ids": ["assert-rules"]}
        action_plan = _write(self.root / "action-plans" / f"transient-b{baseline}.json", batch_tests.batch.action_plan([action], phase="VC-5"))
        return self.source_command([
            "compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", "VC-5", "--sequence", sequence,
            "--predecessor-checkpoint", vc.vc_checkpoint_path(campaign, "VC-4", revision=1),
            "--action-plan", action_plan, "--state-dir", self.state_dir,
        ])

    @staticmethod
    def boundary(paths: list[Path]) -> dict[str, tuple[str, int, int]]:
        """记录旧文件正文摘要与写边界；续跑不得通过复制或重写旧文件制造表面上的成功。"""

        files = {path for root in paths for path in ([root] if root.is_file() else root.rglob("*")) if path.is_file()}
        return {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_ino, path.stat().st_mtime_ns)
                for path in sorted(files)}

    def run_transient_chain(self) -> None:
        """同一 Campaign 的完整新段链：失败定位、批准、只补 J*、增量封存、单次入账、b1 重新评估及幂等重读。"""

        from tools.official_client_capture.tests import claude_client_receipt_fixture

        campaign = self.drive_vc3_vc4_to_candidate_sealed()
        self.prepare_runtime(campaign)
        manifest = upgrade.load_campaign_manifest(campaign)
        assertion_profile.freeze_approved_assertion_profile(campaign, manifest)
        with claude_client_receipt_fixture.source_scope():
            code, first, stderr = self.source_command([
                "capture-candidate", "run", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--acknowledge-live-requests",
            ])
            self.assertIsNotNone(first, stderr)
            self.assertEqual((code, first["status"]), (2, "awaiting_receipts"), stderr)
            attempt_id = first["attempt_id"]
            with self.source_context():
                attempt_root, attempt = upgrade._load_capture_attempt(campaign, "candidate", CANDIDATE, attempt_id)
            clients = self.write_clients(campaign, attempt_root, attempt)
            self.seal(campaign, attempt_id, clients=clients)
            code, _accounted, stderr = self.source_command(["account-sealed-candidate", "--campaign-dir", campaign, "--candidate-id", CANDIDATE])
            self.assertEqual(code, 0, stderr)
            context = self.evaluation_context(campaign)
            code, failed, stderr = self.dispatch_builder(context, 9, baseline=0)
            self.assertIsNotNone(failed, stderr)
            self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:assert-rules"), stderr)
            code, reconciled, stderr = self.source_command([
                "reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", Path(failed["campaign_run"]["run_dir"]),
            ])
            self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
            with self.source_context():
                preview = self.preview(context)
                self.assertEqual(preview["failure_scope"]["jobs"], [FAILED_JOB])
                code, recovered, stderr = self.recover(context, "apply", "--approve-sha256", preview["review_sha256"],
                    "--root-cause-class", "transient-environment")
            self.assertEqual(code, 0, stderr)
            self.assertIsNotNone(recovered, stderr)
            baseline_root = campaign / "candidates" / CANDIDATE / "revisions" / "b1"
            recovery = json.loads((baseline_root / "recovery.json").read_bytes())
            self.assertEqual((recovery["attempt_id"], recovery["recovery_revision"], recovery["execute_jobs"]), (attempt_id, "ar1", [FAILED_JOB]))
            jobs = {row["id"]: row for row in attempt["results"]}
            self.assertEqual(set(recovery["reuse_jobs"]), set(jobs) - {FAILED_JOB})
            original_stage = candidate_capture.legacy_stage_result_path(campaign, CANDIDATE)
            old_roots = [Path(value) for row in jobs.values() for value in row["evidence_roots"]]
            original_seal_root = campaign / "candidates" / CANDIDATE / "seal"
            old_boundary = self.boundary([original_stage, attempt_root, original_seal_root, *old_roots])
            before_calls = len(self.runtime.candidate_calls())
            before_count = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)["precise_total"]
            _write(self.relay_control, {"bad_probe": None})
            run_argv = ["capture-candidate", "run", "--campaign-dir", campaign, "--candidate-id", CANDIDATE,
                        "--attempt-recovery", "ar1", "--acknowledge-live-requests"]
            code, result, stderr = self.source_command(run_argv)
            self.assertIsNotNone(result, stderr)
            self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
            new_calls = self.runtime.candidate_calls()[before_calls:]
            executed = [row["probe"] for row in new_calls if not row["dry_run"]]
            expected = [row["probe_id"] for row in jobs[FAILED_JOB]["steps"] if row.get("argv")]
            self.assertCountEqual(executed, expected)
            segment = attempt_root / "recovery" / "ar1"
            summary = json.loads((segment / "attempt-recovery.json").read_bytes())
            self.assertEqual([row["id"] for row in summary["results"]], [FAILED_JOB])
            self.assertTrue(all("-recovery-ar1" in root for row in summary["results"] for root in row["evidence_roots"]))
            self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
            self.seal(campaign, attempt_id, recovery="ar1")
            stage_path = upgrade._stage_write_target(campaign, CANDIDATE, 1, "capture-candidate")
            with self.source_context():
                stage = upgrade._load_stage_result(campaign, "capture-candidate", CANDIDATE, _baseline=1)
            self.assertTrue(stage_path.is_file())
            previous_stage = json.loads(original_stage.read_bytes())
            self.assertEqual(stage["client_evidence"], previous_stage["client_evidence"])
            self.assertEqual(set(stage["root_sets"]["dropped_roots"]), set(jobs[FAILED_JOB]["evidence_roots"]))
            effective_path = campaign / stage["effective_results"]["path"]
            self.assertEqual(upgrade.file_sha256(effective_path), stage["effective_results"]["sha256"])
            effective = json.loads(effective_path.read_bytes())
            self.assertEqual((effective["evaluation_baseline"], effective["recovery_revision"]), (1, "ar1"))
            effective_jobs = {row["id"]: row for row in effective["results"]}
            self.assertEqual(effective_jobs, {**jobs, FAILED_JOB: summary["results"][0]})
            self.assertEqual(stage["results"], effective["results"])
            store, store_campaign = candidate_store.open_campaign_store(campaign, manifest)
            validation_attempts = [fact for fact in store.list_facts(store_campaign["campaign_id"])
                                   if fact["fact_kind"] == "validation_attempt_created"]
            self.assertEqual({fact["payload"]["attempt_id"] for fact in validation_attempts},
                             {f"{attempt_id}-v1", f"{attempt_id}-ar1-v1"})
            self.assertNotEqual(stage["store"]["validation_attempt_ref"], previous_stage["store"]["validation_attempt_ref"])
            execution = store.load_object(stage["store"]["validation_execution_plan_ref"])["payload"]
            self.assertEqual(set(execution["execute_item_ids"]) & set(jobs), {FAILED_JOB})
            self.assertEqual(set(execution["reuse_item_ids"]) & set(jobs), set(jobs) - {FAILED_JOB})
            self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
            code, _accounted, stderr = self.source_command(["account-sealed-candidate", "--campaign-dir", campaign,
                "--candidate-id", CANDIDATE, "--attempt-recovery", "ar1"])
            self.assertEqual(code, 0, stderr)
            receipt = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)
            self.assertEqual(receipt["status"], "complete")
            # 离线增量核算只接中继运行根；Job 根里的网关恢复报告须由上面的 Campaign provenance 闭包验证。
            new_probes = [probe for row in summary["results"] for root in row["evidence_roots"]
                          for probe in Path(root).iterdir() if (probe / "relay-manifest.json").is_file()]
            self.assertCountEqual([probe.name for probe in new_probes], expected)
            delta_receipt = provenance.collect_root_provenance(new_probes)
            self.assertEqual(delta_receipt["unresolved_roots"], [])
            delta = delta_receipt["precise_total"]
            self.assertGreater(delta, 0)
            self.assertEqual(receipt["precise_total"] - before_count, delta)
            code, passed, stderr = self.dispatch_builder(context, 10, baseline=1)
            self.assertIsNotNone(passed, stderr)
            self.assertEqual((code, passed["campaign_run"]["reason"]), (0, "queue-complete"), stderr)
            index = json.loads((upgrade._stage_write_target(campaign, CANDIDATE, 1, "assertions") / "evaluation-run.json").read_bytes())
            self.assertEqual(index["evaluation_baseline"], 1)
            self.assertTrue(all(row["status"] == "pass" for row in index["rules"]))
            count, calls = receipt["precise_total"], len(self.runtime.candidate_calls())
            code, reused, stderr = self.source_command(run_argv)
            self.assertEqual((code, reused["reused"], reused["live_request_count"]), (2, True, 0), stderr)
            self.assertEqual(len(self.runtime.candidate_calls()), calls)
            self.assertEqual(provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)["precise_total"], count)
            replay_boundary = self.boundary([segment, stage_path.parent, store.root])
            code, resealed, stderr = self.source_command([
                "capture-candidate", "seal", "--campaign-dir", campaign, "--candidate-id", CANDIDATE,
                "--attempt-id", attempt_id, "--attempt-recovery", "ar1", "--approve-seal-sha256",
                stage["seal"]["review_sha256"], "--approved-by", "连续链审核",
            ])
            self.assertIsNotNone(resealed, stderr)
            self.assertEqual((code, resealed["status"], resealed["reused"]), (0, "complete", True), stderr)
            self.assertEqual(self.boundary([segment, stage_path.parent, store.root]), replay_boundary)
            self.assertEqual(len(self.runtime.candidate_calls()), calls)
            self.assertEqual(provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)["precise_total"], count)
            self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")
