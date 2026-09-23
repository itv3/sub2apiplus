"""R13-3：六类历史故障的独立复现夹具。

初始断言固定改造前的拒绝、停线或无法结束现象；对应 R 项完成时改为断言合法恢复，
并补充执行／复用／新增请求计数。这里不提供绕过正式门禁的入口，也不接触真实凭据。
"""

from __future__ import annotations

import json
import copy
import os
import shutil
import signal
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tools import arm64_supervised_deploy as deploy
from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
from tools.official_client_capture import codex_upgrade_evidence_manifest as evidence
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import evaluation_chain_driver as driver
from tools.official_client_capture.tests import managed_tree_copy
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests import test_arm64_driver_wait as wait_tests
from tools.official_client_capture.tests import test_codex_upgrade as upgrade_tests
from tools.official_client_capture.tests import test_codex_upgrade_evidence_integrity as integrity_tests
from tools.official_client_capture.tests import test_codex_upgrade_timing_ledger as timing_tests
from tools.official_client_capture.tests import test_codex_runtime_egress as egress_tests


class RuntimeEgressRecoveryTests(unittest.TestCase):
    def test_pause_reconciliation_approval_preserves_only_trusted_job(self):
        """在独立挂载命名空间绑定受测工具树，保留正式执行位置校验。"""

        tree = Path(upgrade.__file__).resolve().parents[2]
        result = managed_tree_copy.run_python(tree, [
            "-m", "unittest", "-v",
            __name__ + ".RuntimeEgressRecoveryTests._exercise_pause_reconciliation",
        ], timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("skipped=", result.stderr)
        print(result.stdout, end="", flush=True)

    def _exercise_pause_reconciliation(self):
        """合成 Job 与环境事实，真实暂停、checkpoint、对账、批准和 resume 门禁；没有上游请求。"""

        case = upgrade_tests.CodexUpgradeTest()
        case.setUp()
        self.addCleanup(case.doCleanups)
        write_scenarios = case._write_scenario_manifest
        def two_jobs(*args, **kwargs):
            path = write_scenarios(*args, **kwargs)
            data = json.loads(path.read_text())
            extra = copy.deepcopy(data["capture_jobs"][0])
            extra["id"] = "official-uncertain"
            extra["description"] = "暂停窗口内的合成任务"
            extra["evidence_roots"] = ["{campaign_dir}/official-uncertain-evidence"]
            data["capture_jobs"].append(extra)
            case._write_json(path, data)
            return path
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(case, "_write_scenario_manifest", side_effect=two_jobs):
            root = Path(directory).resolve()
            fixture = case._b0_fixture(root)
            campaign, manifest, jobs = fixture["campaign_dir"], fixture["manifest"], fixture["jobs"]
            run_dir = case._b0_run_dir(fixture, "r15-paused", failure_class="environment-prerequisite")
            state = supervisor._read_state(run_dir)
            state["egress_guard"] = {"required": True, "campaign_dir": str(campaign)}
            supervisor._write_json(run_dir / "state.json", state, replace=True)
            attempt_root, reservation = upgrade._reserve_capture_attempt(
                campaign, phase="official", candidate_id=None, identity=dict(manifest["official_identity"]), jobs=jobs,
                allow_failed_rerun=True)
            store = upgrade.incremental_recovery.CheckpointStore(attempt_root / "checkpoints")
            results = []
            for job in sorted(jobs, key=lambda value: value.job_id == "official-uncertain"):
                started, finished = time.time(), time.time()
                result = {"id": job.job_id, "phase": "official", "required": True,
                          "execution_sha256": upgrade._job_execution_sha256(job), "status": "complete",
                          "description": "离线合成 Job，不发送上游请求", "duration_seconds": 1, "steps": [],
                          "evidence_roots": [], "missing_evidence_patterns": [], "empty_evidence_patterns": [],
                          "covers": [], "scenario_ids": [], "scenario_receipts": [], "scenario_receipt_failures": [],
                          "track": "main", "model_id": "gpt-5.5", "expected_use_responses_lite": False,
                          "required_model_receipt": False, "model_condition_receipt": None, "model_condition_receipt_failure": None,
                          "disposition": "executed", "runtime_egress": {
                              "schema_version": "codex-upgrade-job-egress/v1", "run_dir": str(run_dir),
                              "campaign_id": manifest["campaign_id"], "owner_nonce": state["owner_nonce"],
                              "started_at_epoch": started, "finished_at_epoch": finished}}
                prior = store.records()
                store.append({"checkpoint_schema_version": upgrade.JOB_CHECKPOINT_SCHEMA,
                              "campaign_id": manifest["campaign_id"], "phase": "official", "attempt_id": attempt_root.name,
                              "run_nonce": reservation["run_nonce"], "item_id": job.job_id, "status": "complete",
                              "disposition": "executed", "result_sha256": upgrade.incremental_recovery.digest(result),
                              "result_key": None, "result": result, "source_receipt": None,
                              "previous_checkpoint_sha256": prior[-1]["checkpoint_sha256"] if prior else None})
                upgrade._secure_write_json_once(attempt_root / f"job-{job.job_id}.json", result)
                results.append(result)
                if job.job_id == "official-test":
                    last = egress_tests.runtime_fixture()
                    supervisor._write_json(run_dir / "egress-last-valid.json", last, replace=False)
            state["terminal_at_epoch"] = time.time()
            state["terminal_at_utc"] = supervisor._epoch_to_utc(state["terminal_at_epoch"])
            supervisor._write_json(run_dir / "state.json", state, replace=True)
            supervisor._egress_pause(run_dir, state, "隔离出口故障，保留实际影响窗口")
            upgrade._write_capture_attempt(campaign, attempt_root, {
                "campaign_id": manifest["campaign_id"], "phase": "official", "candidate_id": None,
                "status": "failed", "identity": dict(manifest["official_identity"]), "results": results,
                "evidence_roots": [], "evidence_permission_closeout": None,
                "evidence_permission_error": {"type": "SyntheticFailure", "message": "纯离线控制链不制造上游证据。"},
                "environment": {"evidence_root": str(attempt_root / "evidence"), "before_probe": None,
                                "after_probe": {"status": "passed"}, "restoration_report": {"status": "passed"},
                                "arm64_before_receipt": None, "arm64_after_receipt": None},
                "binary_verification": None, "execution_error": None, "restoration_error": None,
                "next_gate": "按出口影响窗口对账，不追认未证明的请求。"})
            timing.append_event(fixture["timing_ledger"], event_id="r15-egress-pause", phase="VC-0",
                                event_type="recovery_required", root_cause_id="environment-prerequisite", next_action="reconcile-attempt")
            originals = {path: path.read_bytes() for path in attempt_root.rglob("*") if path.is_file()}
            value = reconciler.reconcile_attempt(campaign, attempt_root.name)
            self.assertEqual(value["status"], "recoverable")
            preview = value["recovery_preview"]
            self.assertEqual(preview["execute_job_ids"], ["official-uncertain"])
            self.assertEqual(preview["reuse_job_ids"], ["official-test"])
            preview_path = Path(value["recovery_preview_path"])
            with self.assertRaisesRegex(upgrade.ConfigurationError, "尚未批准"):
                case._b0_resume(campaign, preview_path)
            reconciler.approve_recovery_preview(campaign, attempt_root.name, approve_sha256=preview["review_sha256"])
            bound = reconciler.load_approved_recovery_preview(campaign, preview_path, phase="official", candidate_id=None)
            self.assertEqual(bound["execute_job_ids"], ["official-uncertain"])
            self.assertEqual(timing.inspect_ledger(fixture["timing_ledger"])["status"], "active")
            reused = upgrade._prior_complete_results(
                campaign, Path("official"), jobs, phase="official", candidate_id=None,
                identity=dict(manifest["official_identity"]), source_attempt_id=attempt_root.name,
                expected_reuse_job_ids=bound["reuse_job_ids"])
            self.assertEqual([item["id"] for item in reused], ["official-test"])
            dispatched = []
            def resume(arguments, phase):
                dispatched.extend(arguments.recovery_preview_payload["execute_job_ids"])
                return {"status": "fixture-dispatched"}
            with mock.patch.object(upgrade, "_run_capture_attempt", side_effect=resume):
                self.assertEqual(case._b0_resume(campaign, preview_path)["status"], "fixture-dispatched")
            self.assertEqual(dispatched, ["official-uncertain"])
            self.assertEqual(originals, {path: path.read_bytes() for path in originals})
            print(json.dumps({"fixture": "r15-pause-reconcile-resume", "execute_jobs": 1, "reuse_jobs": 1,
                              "live_request_count": 0, "source_bytes_unchanged": True}), flush=True)


class UpgradeFaultFixtureTests(unittest.TestCase):
    def _assert_delegate_recovers(self, module_name, class_name, method):
        """翻转后的夹具委托对应改造项的恢复成功用例，确认故障现象之后的合法恢复实际走通。"""

        module = __import__(module_name, fromlist=[class_name])
        result = unittest.TestResult()
        getattr(module, class_name)(method).run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertFalse(result.skipped, f"恢复用例被跳过：{module_name}.{class_name}.{method}")
        self.assertTrue(result.wasSuccessful(), str(result.errors + result.failures))

    def test_r2_image_only_revision_is_accepted_as_build_change(self):
        seal = artifacts.build_candidate_revision_seal(
                campaign_id="fixture-upgrade", revision=2, candidate_id="candidate-r2",
                candidate_commit="a" * 40, source_tree_sha256="1" * 64,
                image_id="sha256:" + "3" * 64, build_receipt_sha256="c" * 64,
                vc3_stage_receipt_sha256="d" * 64, sealed_at_utc="2026-09-23T00:00:00Z",
                superseded={"revision": 1, "candidate_id": "candidate-r1", "git_commit": "a" * 40,
                            "source_tree_sha256": "1" * 64, "image_id": "sha256:" + "2" * 64},
        )
        self.assertEqual(seal["changed_layers"], ["build"])
        self.assertTrue(seal["identity_change"]["image_changed"])
        # 同一源码只换镜像时，前序 revision 的实现测试收据按完整输入证明复用，原收据字节不变。
        self._assert_delegate_recovers("tools.official_client_capture.tests.test_codex_upgrade_build_revision",
                                       "BuildRevisionTests",
                                       "test_same_inputs_reuse_source_receipt_and_preserve_original_bytes")

    def test_r3_metadata_only_drift_is_currently_permanent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root, manifest_path = integrity_tests._sealed_evidence_root(root)
            manifest = json.loads(manifest_path.read_text())
            originals = {path: path.read_bytes() for path in evidence_root.rglob("*") if path.is_file()}
            integrity_tests._drift_ctime_only(evidence_root)
            self.assertEqual(originals, {path: path.read_bytes() for path in originals})
            with self.assertRaises(evidence.EvidenceManifestBoundaryDriftError) as caught:
                evidence.verify_manifest_boundary(manifest, [evidence_root])
            self.assertEqual(caught.exception.failure_class, "evidence-integrity")

    def test_r4_parent_failure_requires_review_before_redispatch(self):
        # 故障现象：父动作失败写 stage_review_required，对账前后续批次被拒（不再 stop_the_line）。
        self._assert_delegate_recovers("tools.official_client_capture.tests.test_codex_upgrade",
                                       "CodexUpgradeTest",
                                       "test_vc_chain_failed_batch_abandons_stage_and_blocks_next_batch")
        # 恢复：classify COMMIT 后失败，原 Campaign 对账后 N+1 逐字重派成功，新增请求为零。
        self._assert_delegate_recovers("tools.official_client_capture.tests.real_chains.test_codex_upgrade_stage_recovery",
                                       "StageRecoveryChainTests",
                                       "test_classify_commit_failure_reconciles_and_redispatches")

    def test_r6_import_completes_vc0_vc1_without_live_requests(self):
        case = driver.new_real_chain_case()
        self.addCleanup(case.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            fixture = case._b0_fixture(Path(directory).resolve(), campaign_id="fault-source")
            source, manifest = fixture["campaign_dir"], fixture["manifest"]
            case._write_capture_stage(source, source / "official-evidence", phase="official",
                                      identity=manifest["official_identity"],
                                      prepare_evidence=driver._prepare_side_evidence(None),
                                      extra_artifacts=driver._extra_artifacts(False))
            target = fixture["data"] / "evidence" / "campaigns" / "fault-successor"
            code, stdout, stderr = case._run_main([
                "reuse-official-evidence", "--predecessor-campaign-dir", str(source),
                "--campaign-dir", str(target), "--campaign-id", "fault-successor",
                "--codex-account-id", str(manifest["configuration"]["codex_account_id"]),
            ])
            self.assertEqual(code, 0, stderr)
            imported = json.loads(stdout)
            self.assertEqual((imported["executed_job_count"], imported["live_request_count"]), (0, 0))
            summary = timing.phase_ledger_state(fixture["timing_ledger"])
            self.assertEqual(summary["status"], "active")
            self.assertIsNone(summary["active_phase"])
            self.assertEqual(summary["completed_phases"], ["VC-0", "VC-1"])

    def test_r8_expired_budget_pauses_without_terminal(self):
        helper = timing_tests.TimingLedgerTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ledger"
            helper._create(root)
            summary = timing.inspect_ledger(root, now=helper._at(45))
            self.assertEqual(summary["status"], "deadline_paused")
            self.assertEqual(summary["paused_scopes"], ["stage"])
            self.assertEqual(len(timing._load_events(root)), 1)
        # 恢复：批准延期后回到暂停前状态，累计墙钟与请求不清零，原始字节不变。
        self._assert_delegate_recovers("tools.official_client_capture.tests.test_codex_upgrade_deadline_extension",
                                       "DeadlineExtensionTests",
                                       "test_stage_pause_extend_preserves_counters_and_original_bytes")

    def test_r12_dead_child_exits_bounded_without_campaign_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = driver_tests._DriverFixture(root)
            scripts = root / "driver"
            scripts.mkdir()
            for name in ("vc4-all.sh", "lib.sh", "parse_env.py", "wait_state.py"):
                shutil.copy2(driver_tests.SCRIPTS / name, scripts / name)
            # 本例只验证父子进程与等待控制，构建前置用零请求替身；输入合同由专项测试实际复算。
            # 首个 revision 的真实裁定是 early-test-mode=full（门禁与前端并行）；guard.sh 准入与
            # open-revision 登记各有专项测试，这里只保留其在驱动中的调用位置。
            (scripts / "vc4_resume.py").write_text(
                "import sys\nprint('full' if sys.argv[1:2] == ['early-test-mode'] else 'fixture pre-build inputs')\n")
            for name, source in {"trees.sh": "exit 0\n", "frontend.sh": "exec sleep 30\n",
                                 "guard.sh": "echo GUARD_OK\n",
                                 "vc4-gates.sh": f"echo $$ > '{root}/gates.pid'\nexec sleep 30\n"}.items():
                (scripts / name).write_text(source)
            before = {path: path.read_bytes() for path in fixture.newdir.rglob("*") if path.is_file()}
            process = subprocess.Popen(["bash", str(scripts / "vc4-all.sh")],
                                       env={**os.environ, **fixture.env}, cwd=root,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not (root / "gates.pid").exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue((root / "gates.pid").is_file())
                started = time.monotonic()
                os.kill(int((root / "gates.pid").read_text()), signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 3, stdout + stderr)
                self.assertIn("子进程已退出", stderr)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 5)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)
            self.assertEqual((fixture.runroot / "gates.out").read_text(), "")
            self.assertEqual(before, {path: path.read_bytes() for path in fixture.newdir.rglob("*") if path.is_file()})
            print(json.dumps({"fixture": "r12-dead-child", "exit_code": 3, "detection_seconds": elapsed,
                              "execute_jobs": 0, "reuse_jobs": 0, "live_request_count": 0,
                              "campaign_bytes_unchanged": True}), flush=True)


class DriverResumeChainTests(unittest.TestCase):
    """实际 VC-4 shell 与续跑 producer 连跑；编译／镜像／Campaign 动作由零请求替身提供。"""

    def test_vc5_actual_run_death_or_stale_heartbeat_stops_waiter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = driver_tests._DriverFixture(root)
            case = driver_tests.Vc5AllResumeTests()
            scripts, calls = case._stub_driver(root, fixture, accept_creates_result=False)
            case._prepare_stage(fixture, compare_done=True, acceptance_done=True)
            log = fixture.runroot / "vc5-run-batch.out"
            log.write_text("run started\n")
            for fault in ("dead-run", "stale-heartbeat"):
                with self.subTest(fault=fault):
                    child = subprocess.Popen(["sleep", "30"])
                    try:
                        pid = fixture.runroot / "vc5-run-batch.pid"
                        pid.write_text(str(child.pid))
                        launched = time.time()-61
                        os.utime(pid, (launched, launched))
                        state = fixture.data_root / "control" / (fixture.new + "-supervisor") / "run-fixture"
                        state.mkdir(parents=True, exist_ok=True)
                        (state / "state.json").write_text(json.dumps({"started_at_epoch": launched,
                            "owner_pid": child.pid, "owner_nonce": "fixture", "watchdog_timeout_seconds": .1}))
                        (state / "heartbeat.json").write_text(json.dumps({"schema_version": supervisor.HEARTBEAT_SCHEMA,
                            "owner_pid": child.pid, "owner_nonce": "fixture", "state": "running", "updated_at_epoch": time.time()-1}))
                        if fault == "dead-run":
                            child.terminate()
                            child.wait()
                        result = subprocess.run(["bash", str(scripts / "vc5-all.sh")], env={**os.environ, **fixture.env},
                                                capture_output=True, text=True, timeout=6)
                        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                        self.assertEqual(calls.read_text(), "")
                        self.assertIn("子进程已退出" if fault == "dead-run" else "监督器心跳", result.stderr)
                    finally:
                        if child.poll() is None:
                            child.terminate()
                            child.wait()
            print(json.dumps({"fixture": "r12-vc5-wait", "fault_cases": 2, "exit_code": 3,
                              "execute_stages": 0, "live_request_count": 0}), flush=True)

    def test_upload_timeout_resume_and_changed_inputs_rerun_only_required_stages(self):
        helper = wait_tests.ResumeInputTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        root = helper.root
        fixture = driver_tests._DriverFixture(root / "harness")
        values = wait_tests.load("parse_env").parse(fixture.env_file.read_text())
        values.update(B=str(root), C=helper.commit, CAND=wait_tests.build_tests.CANDIDATE_ID,
                      UP="fixture-upgrade", NEW=helper.manifest["campaign_id"], STAGE_BUDGETS="VC-4=0.02 VC-5=1",
                      FRONTEND_DEVIATION_APPROVED_BY="测试授权")
        fixture.env_file.write_text("".join(f'{key}="{value}"\n' for key, value in values.items()))
        scripts = root / "driver-fixture"
        scripts.mkdir()
        for name in ("vc4-all.sh", "lib.sh", "parse_env.py", "wait_state.py", "upload_manifest.py"):
            shutil.copy2(driver_tests.SCRIPTS / name, scripts / name)
        calls = root / "stage-calls.log"
        calls.touch()
        golden_parameters = root / "parameters.original.json"
        golden_parameters.write_bytes((root / "artifacts/build-parameters.json").read_bytes())
        # 只替换外部 Campaign 身份读取和镜像／工具链查询，真实 Git／文件与统一收据代码直接运行。
        wrapper = (
            "import importlib.util,json,sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(driver_tests.REPO_ROOT)!r})\n"
            f"spec=importlib.util.spec_from_file_location('real_resume', {str(driver_tests.SCRIPTS / 'vc4_resume.py')!r})\n"
            "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
            f"module.context=lambda: (Path({str(root)!r}), Path({str(root / 'campaign')!r}), {helper.manifest!r}, {helper.requirements!r})\n"
            "real_run=module.run\n"
            "def run(*args, **kwargs):\n"
            "    if args[0]=='docker':\n"
            "        if args[1:3]==('image','inspect'):\n"
            f"            return json.dumps([{{'Id':{wait_tests.build_tests.IMAGE_ID!r},'Os':'linux','Architecture':'arm64','RepoDigests':['fixture@sha256:'+'7'*64]}}])\n"
            f"        return {helper.node_version!r}\n"
            f"    if args[0]=='go': return {helper.go_version!r}\n"
            "    return real_run(*args, **kwargs)\n"
            "module.run=run\n"
            # revision 登记、复用预判与 ldflags 沿用都读取真实 Campaign；本链只验证等待、续跑与输入复算，
            # 按首个 revision 的真实结果替换：登记 r1、门禁提前全量执行、使用默认 ldflags。
            "module.open_revision=lambda: print('REVISION_OPENED r1 fixture')\n"
            "module.early_test_mode=lambda evidence: 'full'\n"
            "module.build_flags=lambda evidence, default: default\n"
        )
        (scripts / "vc4_resume.py").write_text(wrapper + "if __name__=='__main__': raise SystemExit(module.main())\n")
        bodies = {
            "trees.sh": "echo TREES_DONE\n",
            "frontend.sh": "echo 'FRONTEND_DONE now'\n",
            "vc4-gates.sh": f'mkdir -p "$1/logs"\ncp "{helper.evidence}/logs/implementation.log" "$1/logs/implementation.log"\necho "GATES_DONE now"\n',
            "build.sh": f'cp "{golden_parameters}" "{root}/artifacts/build-parameters.json"\necho BUILD_DONE\n',
            "guard.sh": "echo GUARD_OK\n",
        }
        for name, body in bodies.items():
            (scripts / name).write_text(f'#!/bin/bash\nset -e\necho {name} >> "{calls}"\n' + body)
        # 首个 revision 没有前序构建收据，正式 plan-retest 的裁定恒为 full；该裁定逻辑由 R2 专项测试复算。
        (scripts / "implementation_gates.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            "assert sys.argv[1] == 'plan-retest'\n"
            "(Path(sys.argv[2]) / 'retest-plan.json').write_text(json.dumps({'decision': {'mode': 'full'}, 'inputs': {}}))\n")
        finalize = wrapper + (
            "from tools.official_client_capture import codex_upgrade_vc_receipt as receipts\n"
            "root=Path(sys.argv[1]); current=module.inputs()\n"
            "if not (root/'receipt.json').exists():\n"
            "    gates=[{'gate_id':name,'kind':kind,'command':command,'exit_code':0,'passed':1,'failed':0,'approved_skip':0,'unexpected_skip':0} "
            "for name,kind,command in [('affected-rule','affected',['go','test','./fixture']),('check-egress-spec','public',['make','check-egress-spec'])]]\n"
            "    facts={'schema_version':receipts.FACTS_SCHEMA,'kind':'implementation_tests','subject':current['subject'],"
            "'assertions':{'git_commit':current['git_commit'],'source_tree_sha256':current['tree_sha256']['source'],'target_architecture':'linux/arm64','gates':gates},"
            "'evidence':[{'role':'check_egress_spec','path':'logs/check-egress-spec.log'},{'role':'implementation_tests','path':'logs/implementation.log'}]}\n"
            "    (root/'facts.json').write_text(json.dumps(facts)); receipts.finalize(root,'facts.json','receipt.json')\n"
            "module.verify(root,'full'); print('VC4_DONE')\n"
        )
        (scripts / "finalize.py").write_text(finalize)
        (scripts / "vc4.sh").write_text(f'#!/bin/bash\nset -e\npython3 "{scripts}/finalize.py" "$1"\n')
        # macOS 开发机不执行 root chown；ARM64 上仍可按实际 chown 跑本夹具。
        path_bin = root / "fixture-bin"
        path_bin.mkdir()
        (path_bin / "chown").write_text("#!/bin/sh\nexit 0\n")
        (path_bin / "chown").chmod(0o755)
        environment = {**os.environ, **fixture.env, "PATH": str(path_bin) + ":" + os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
        def invoke(*args):
            return subprocess.run(["bash", str(scripts / "vc4-all.sh"), *args], env=environment,
                                  capture_output=True, text=True, timeout=45)
        interrupted = invoke()
        self.assertEqual(interrupted.returncode, 3, interrupted.stdout + interrupted.stderr)
        self.assertIn("等待超过总时限", interrupted.stderr)
        evidence_root = Path((fixture.runroot / "E.txt").read_text().strip())
        self.assertFalse((evidence_root / "receipt.json").exists())
        checkpoint_before = (evidence_root / "upload-wait.json").read_bytes()
        first_calls = calls.read_text().splitlines()
        build_stages = ["trees.sh", "frontend.sh", "vc4-gates.sh", "build.sh"]
        # 每次启动先执行 guard.sh pre-plan；进入派发前再执行 guard.sh pre-vc4。
        self.assertEqual(sorted(first_calls), sorted(["guard.sh", *build_stages]))
        self.assertEqual(first_calls[0], "guard.sh")
        impl = fixture.runroot / "impl-logs"
        (impl / "cross-check").mkdir(parents=True)
        (fixture.runroot / "local-gates").mkdir()
        (impl / "check-egress-spec.log").write_text(f"candidate_commit={helper.commit}（夹具）\nexecuted_on_commit={values['DC']}（夹具）\nexit_code=0\n")
        (impl / "cross-check/check-egress-spec.C-only.local.log").write_text(f"commit={helper.commit}\nexit_code=0\n")
        upload = wait_tests.load("upload_manifest")
        (impl / "upload-manifest.json").write_text(json.dumps(upload.manifest(fixture.runroot)))
        (impl / "READY").touch()
        recovered = invoke("--resume-from", "upload-wait")
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertIn("VC4_REUSED", recovered.stdout)
        self.assertEqual(calls.read_text().splitlines(), first_calls + ["guard.sh", "guard.sh"])
        self.assertEqual((evidence_root / "upload-wait.json").read_bytes(), checkpoint_before)
        receipt_bytes = (evidence_root / "receipt.json").read_bytes()
        complete = invoke()
        self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)
        self.assertEqual((evidence_root / "receipt.json").read_bytes(), receipt_bytes)
        self.assertEqual(calls.read_text().splitlines(), first_calls + ["guard.sh"] * 4)
        params = root / "artifacts/build-parameters.json"
        value = json.loads(params.read_text())
        value["go_build"]["environment"]["CGO_ENABLED"] = "1"
        params.write_text(json.dumps(value))
        rejected = invoke("--resume-from", "upload-wait")
        self.assertEqual(rejected.returncode, 3, rejected.stdout + rejected.stderr)
        # 显式续跑在复算输入时拒绝：只执行准入检查，构建阶段执行 0 次。
        self.assertEqual(calls.read_text().splitlines(), first_calls + ["guard.sh"] * 5)
        rebuilt = invoke()
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
        final_calls = calls.read_text().splitlines()
        for name in build_stages:
            self.assertEqual(final_calls.count(name), 2, final_calls)
        self.assertEqual(final_calls.count("guard.sh"), 8, final_calls)
        print(json.dumps({"fixture": "r12-upload-resume", "initial_build_stages": 4, "resume_build_stages": 0,
                          "full_receipt_reuse_build_stages": 0, "changed_parameters_build_stages": 4,
                          "rejected_resume_build_stages": 0, "live_request_count": 0,
                          "checkpoint_and_receipt_bytes_preserved": True}), flush=True)


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "R15 内核夹具要求隔离 Linux root 环境")
class RuntimeEgressKernelTests(unittest.TestCase):
    """真实 cgroup、网桥、WireGuard、NAT 与 TCP/UDP 故障链，所有 netns 均无宿主上联。"""

    def setUp(self):
        for command in ("ip", "nft", "wg", "clang", "gcc"):
            if not shutil.which(command):
                self.fail(f"R15 内核夹具缺少必要命令：{command}")
        self.root = Path(tempfile.mkdtemp(prefix="r15-kernel-"))
        self.namespaces = []
        self.processes = []
        self.stop = threading.Event()
        self.renew_origin, self.renew_exit = True, True
        self.states = {"sub2apiplus": "compliant", "capture-cli": "compliant"}
        self.errors = []
        self.group = Path("/sys/fs/cgroup") / ("r15-test-" + str(os.getpid()))
        self.pin = Path("/sys/fs/bpf") / ("r15-test-" + str(os.getpid()))
        self.addCleanup(self.cleanup)
        self.policy = json.loads((Path(__file__).parents[1] / "fixtures/runtime_egress_policy.json").read_text())
        self.policy["nodes"]["origin"]["interface"] = "wg-test"
        self.policy["nodes"]["exit"]["interface"] = "wg-test"
        self.policy["nodes"]["origin"]["endpoint"]["ipv4"] = "213.35.102.31"
        self.policy["nodes"]["exit"]["endpoint"]["ipv4"] = "69.63.195.102"
        self.ns = {}
        for index, role in enumerate(("origin", "exit", "wan", "app", "capture", "database", "proxy")):
            name = f"r15{os.getpid()}n{index}"
            self.command("ip", "netns", "add", name)
            self.namespaces.append(name)
            self.ns[role] = name
            self.inside(role, "ip", "link", "set", "lo", "up")
            self.inside(role, "sysctl", "-w", "net.ipv4.ip_forward=1")
            self.inside(role, "sysctl", "-w", "net.ipv4.conf.all.rp_filter=0")
            self.inside(role, "sysctl", "-w", "net.ipv4.conf.default.rp_filter=0")
            self.inside(role, "sysctl", "-w", "net.ipv4.conf.all.arp_ignore=1")
            self.inside(role, "sysctl", "-w", "net.ipv4.conf.all.arp_announce=2")
        for role, bridge, addresses in (("origin", "br0", ["172.31.200.1/24"]),
                                         ("wan", "brw", ["213.35.102.1/24", "69.63.195.1/24"])):
            self.inside(role, "ip", "link", "add", bridge, "type", "bridge")
            self.inside(role, "ip", "link", "set", bridge, "up")
            for address in addresses:
                self.inside(role, "ip", "addr", "add", address, "dev", bridge)
        self.link_index = 0
        self.edge("origin", "eth0", "213.35.102.31/24", "wan", "wan_origin", "brw")
        self.edge("exit", "eth0", "69.63.195.102/24", "wan", "wan_exit", "brw")
        self.edge("exit", "eth1", "69.63.195.103/24", "wan", "wan_wrong", "brw")
        for role, gateway in (("origin", "213.35.102.1"), ("exit", "69.63.195.1")):
            self.inside(role, "ip", "route", "add", "default", "via", gateway, "dev", "eth0")
        for index, role in enumerate(("app", "capture", "database", "proxy"), 2):
            self.edge(role, "eth0", f"172.31.200.{index}/24", "origin", "v_" + role, "br0")
            self.inside(role, "ip", "route", "add", "default", "via", "172.31.200.1")
        for address in ("84.1.1.1/32", "1.1.1.1/32", "8.8.8.8/32"):
            self.inside("wan", "ip", "addr", "add", address, "dev", "lo")
        for role in ("origin", "exit"):
            private = self.command("wg", "genkey")
            self.policy["nodes"][role]["public_key"] = self.command("wg", "pubkey", input_text=private + "\n")
            key = self.root / (role + ".key")
            key.write_text(private + "\n")
            key.chmod(0o600)
            self.inside(role, "ip", "link", "add", "wg-test", "type", "wireguard")
            self.inside(role, "ip", "addr", "add", self.policy["nodes"][role]["tunnel_ipv4"], "dev", "wg-test")
        for role, peer in (("origin", "exit"), ("exit", "origin")):
            self.inside(role, "wg", "set", "wg-test", "private-key", str(self.root / (role + ".key")),
                        "listen-port", "51832", "fwmark", str(deploy.EGRESS_WG_MARK), "peer", self.policy["nodes"][peer]["public_key"],
                        "endpoint", self.policy["nodes"][peer]["endpoint"]["ipv4"] + ":51832",
                        "allowed-ips", "0.0.0.0/0" if role == "origin" else "10.79.32.1/32", "persistent-keepalive", "1")
            self.inside(role, "ip", "link", "set", "wg-test", "mtu", "1420", "up")
        self.inside("origin", "ip", "route", "add", "default", "dev", "wg-test", "table", "51832")
        for network in deploy.EGRESS_PRIVATE_NETWORKS:
            self.inside("origin", "ip", "route", "add", "throw", network, "table", "51832")
        self.inside("origin", "ip", "rule", "add", "priority", "3900", "fwmark", f"{deploy.EGRESS_MARK}/0xff000000", "lookup", "51832")
        deploy.build_egress_filter(self.root / "build")
        self.group.mkdir()
        self.pin.mkdir()
        self.command(str(self.root / "build/egress-filter-loader"), str(self.root / "build/egress-filter.bpf.o"), str(self.group), str(self.pin))
        self.maps = deploy.EgressKernelMaps(self.pin)
        self.maps.verify_attachment(self.group)
        self.inventory = {"services": {}, "bypass_ifindices": []}
        for name, role, address in (("sub2apiplus", "app", "172.31.200.2"), ("capture-cli", "capture", "172.31.200.3")):
            group = self.group / role
            group.mkdir()
            link = json.loads(self.inside(role, "ip", "-j", "link", "show", "eth0"))[0]
            dependencies = [{"ipv4": "172.31.200.4", "protocol": "tcp", "port": 5432}] if role == "app" else [{"ipv4": "172.31.200.2", "protocol": "tcp", "port": 8080}]
            self.inventory["services"][name] = {"container_id": ("a" if role == "app" else "b") * 64,
                "cgroup_id": group.stat().st_ino, "bindings": [{"ifindex": link["ifindex"], "host_ifindex": link["link_index"], "source_ipv4": address}], "dependencies": dependencies}
        for role in ("database", "proxy"):
            link = json.loads(self.inside(role, "ip", "-j", "link", "show", "eth0"))[0]
            self.inventory["bypass_ifindices"].append(link["link_index"])
        for role in ("origin", "exit"):
            self.inside(role, "nft", "-f", "-", input_text=deploy.render_egress_firewall(self.policy, role))
        self.write_server()
        self.write_client()
        self.start_server("wan", "0.0.0.0", 19815, "tcp")
        self.start_server("wan", "1.1.1.1", 53, "udp")
        self.start_server("wan", "8.8.8.8", 53, "udp")
        self.start_server("database", "0.0.0.0", 5432, "tcp")
        self.start_server("proxy", "0.0.0.0", 7890, "tcp")
        self.start_server("app", "0.0.0.0", 8080, "tcp", group=self.group / "app")
        self.assertFalse(self.request("app"), "首包没有租期必须被拒绝")
        self.worker = threading.Thread(target=self.renew_loop, daemon=True)
        self.worker.start()
        time.sleep(0.6)
        if not self.request("app"):
            diagnostics = {"errors": self.errors}
            for role in ("origin", "exit"):
                tables = json.loads(self.inside(role, "nft", "-j", "list", "ruleset"))["nftables"]
                diagnostics[role] = [item for item in tables if "rule" in item and any(expression.get("counter", {}).get("packets", 0) for expression in item["rule"].get("expr", []) if isinstance(expression, dict))]
                diagnostics[role + "_transfer"] = self.inside(role, "wg", "show", "wg-test", "transfer")
            self.fail("保护放行后无法经专用通道往返：" + json.dumps(diagnostics))
        self.assertTrue(self.request("capture"))

    def command(self, *argv, input_text=None):
        value = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=12)
        if value.returncode:
            raise AssertionError(f"隔离命令失败 {argv[:3]}：{value.stderr[:2000]}")
        return value.stdout.strip()

    def inside(self, role, *argv, input_text=None):
        return self.command("ip", "netns", "exec", self.ns[role], *argv, input_text=input_text)

    def edge(self, role, interface, address, other, other_interface, bridge):
        self.link_index += 1
        left, right = f"r15{os.getpid()}a{self.link_index}", f"r15{os.getpid()}b{self.link_index}"
        self.command("ip", "link", "add", left, "type", "veth", "peer", "name", right)
        self.command("ip", "link", "set", left, "netns", self.ns[role], "name", interface)
        self.command("ip", "link", "set", right, "netns", self.ns[other], "name", other_interface)
        self.inside(role, "ip", "addr", "add", address, "dev", interface)
        self.inside(role, "ip", "link", "set", interface, "up")
        self.inside(other, "ip", "link", "set", other_interface, "master", bridge)
        self.inside(other, "ip", "link", "set", other_interface, "up")

    def write_server(self):
        self.server_script = self.root / "server.py"
        self.server_script.write_text('''import socket,sys,threading,json
address,port,protocol,record=sys.argv[1:]
def record_peer(peer):
 with open(record,'a') as stream: stream.write(json.dumps({'peer':peer[0],'port':int(port)})+'\\n')
def serve(connection,peer):
 with connection:
  while True:
   data=connection.recv(1024)
   if not data: return
   record_peer(peer)
   connection.sendall(data)
s=socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET,socket.SOCK_DGRAM if protocol=='udp' else socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind((address,int(port)))
if protocol=='udp':
 print('ready',flush=True)
 while True:
  data,peer=s.recvfrom(1024); record_peer(peer); s.sendto(data,peer)
else:
 s.listen(); print('ready',flush=True)
 while True:
  connection,peer=s.accept(); threading.Thread(target=serve,args=(connection,peer),daemon=True).start()
''')

    def write_client(self):
        self.client_script = self.root / "client.py"
        self.client_script.write_text('''import socket,sys,json
held=None
for line in sys.stdin:
 args=json.loads(line)
 try:
  if args.get('held') and held is not None: s=held
  else:
   family=socket.AF_INET6 if ':' in args['address'] else socket.AF_INET
   s=socket.socket(family,socket.SOCK_DGRAM if args.get('udp') else socket.SOCK_STREAM)
   s.settimeout(.6)
   if args.get('source'): s.bind((args['source'],0))
   s.connect((args['address'],args['port']))
  s.send(b'R15-protected-business')
  ok=s.recv(1024)==b'R15-protected-business'
  if args.get('held'): held=s
  else: s.close()
 except OSError:
  ok=False
  if held: held.close(); held=None
 print(json.dumps({'passed':ok}),flush=True)
''')

    def launch(self, role, argv, group=None):
        command = ["ip", "netns", "exec", self.ns[role], *argv]
        if group:
            command = ["sh", "-c", 'echo $$ > "$1/cgroup.procs"; shift; exec "$@"', "r15-isolated", str(group), *command]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def line(self, process):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(4), "隔离探针没有在约定期限内返回")
        return process.stdout.readline().strip()

    def start_server(self, role, address, port, protocol, group=None):
        process = self.launch(role, [sys.executable, "-u", str(self.server_script), address, str(port), protocol, str(self.root / (role + "-received.jsonl"))], group)
        self.assertEqual(self.line(process), "ready")

    def request(self, role, address="84.1.1.1", port=19815, *, udp=False, source=None, held=None):
        process = held or self.launch(role, [sys.executable, "-u", str(self.client_script)], self.group / role if role in {"app", "capture"} else None)
        process.stdin.write(json.dumps({"address": address, "port": port, "udp": udp, "source": source, "held": held is not None}) + "\n")
        process.stdin.flush()
        answer = json.loads(self.line(process))["passed"]
        if held is None:
            process.stdin.close()
            process.wait(timeout=3)
        return answer

    def renew_loop(self):
        try:
            while not self.stop.is_set():
                states = dict(self.states)
                if self.renew_exit:
                    self.inside("exit", "nft", "-f", "-", input_text=deploy.egress_lease_transaction(self.policy, "exit", {}, {}, [], shared=True))
                if self.renew_origin:
                    self.inside("origin", "nft", "-f", "-", input_text=deploy.egress_lease_transaction(self.policy, "origin", self.inventory, states, [], shared=True))
                    expiry = time.monotonic_ns() + 3_000_000_000
                    marks = deploy.egress_service_marks(self.policy)
                    for name, service in self.inventory["services"].items():
                        for binding in service["bindings"]:
                            if states[name] == "compliant":
                                self.maps.lease(service["cgroup_id"], binding["ifindex"], binding["source_ipv4"], expires_at_ns=expiry, mark=marks[name], probe_only=False)
                            else:
                                self.maps.revoke(service["cgroup_id"], binding["ifindex"], binding["source_ipv4"])
                self.stop.wait(.15)
        except BaseException as error:
            self.errors.append(str(error))
            self.stop.set()

    def assert_no_unauthorized_packets(self):
        rows = [json.loads(line) for line in (self.root / "wan-received.jsonl").read_text().splitlines()]
        self.assertTrue(rows)
        self.assertEqual({row["peer"] for row in rows}, {"69.63.195.102"})
        self.assertFalse(self.errors, self.errors)

    def test_r15_isolated_kernel_failure_and_recovery_chain(self):
        # 必要依赖、入站回复和指定 DNS 必须正常；同网桥代理及非指定 DNS 必须被拒绝。
        self.assertTrue(self.request("app", "172.31.200.4", 5432))
        self.assertTrue(self.request("capture", "172.31.200.2", 8080))
        self.assertTrue(self.request("proxy", "172.31.200.2", 8080))
        self.assertFalse(self.request("app", "172.31.200.5", 7890))
        self.assertTrue(self.request("app", "1.1.1.1", 53, udp=True))
        self.assertFalse(self.request("app", "8.8.8.8", 53, udp=True))
        self.assertFalse(self.request("capture", "172.31.200.4", 5432))
        # 同一已建立连接在单容器阻断后停止；另一容器继续正常业务。
        held = self.launch("app", [sys.executable, "-u", str(self.client_script)], self.group / "app")
        self.assertTrue(self.request("app", held=held))
        self.states["sub2apiplus"] = "blocked"
        time.sleep(.35)
        self.assertFalse(self.request("app", held=held))
        self.assertFalse(self.request("app"))
        self.assertTrue(self.request("capture"))
        self.states["sub2apiplus"] = "compliant"
        time.sleep(.35)
        self.assertTrue(self.request("app"))
        # 附加网卡、别名地址与 IPv6 都没有隐含放行。
        self.edge("app", "eth1", "172.31.200.22/24", "origin", "v_extra", "br0")
        self.inside("app", "ip", "addr", "add", "172.31.200.23/24", "dev", "eth0")
        self.assertFalse(self.request("app", source="172.31.200.22"))
        self.assertFalse(self.request("app", source="172.31.200.23"))
        self.inside("app", "ip", "-6", "addr", "add", "fd15::2/64", "dev", "eth0", "nodad")
        self.inside("proxy", "ip", "-6", "addr", "add", "fd15::5/64", "dev", "eth0", "nodad")
        self.start_server("proxy", "fd15::5", 7890, "tcp")
        self.assertFalse(self.request("app", "fd15::5", 7890))
        # 源宿主路由误改：新旧连接每包仍由实际出接口检查拦截。
        self.assertTrue(self.request("app", held=held))
        self.inside("origin", "ip", "route", "replace", "default", "via", "213.35.102.1", "dev", "eth0", "table", "51832")
        self.assertFalse(self.request("app", held=held))
        self.assertFalse(self.request("app"))
        self.inside("origin", "ip", "route", "replace", "default", "dev", "wg-test", "table", "51832")
        self.assertTrue(self.request("app"))
        # 出口宿主路由与提前 SNAT 被晚期检查拒绝；故障期间公网端没有非授权源包。
        self.inside("exit", "ip", "route", "add", "84.1.1.1/32", "via", "69.63.195.1", "dev", "eth1")
        self.assertFalse(self.request("app"))
        self.inside("exit", "ip", "route", "del", "84.1.1.1/32")
        self.inside("exit", "nft", "-f", "-", input_text='table ip r15_fault {\n chain corrupt { type nat hook postrouting priority 80; ip saddr 10.79.32.1 snat to 69.63.195.103; }\n}\n')
        self.assertFalse(self.request("capture"))
        self.inside("exit", "nft", "delete", "table", "ip", "r15_fault")
        self.assertTrue(self.request("capture"))
        # 更新事务在语法错误时保持旧表；停止任一端守护的内核租期均自行闭锁。
        invalid = subprocess.run(["ip", "netns", "exec", self.ns["origin"], "nft", "-f", "-"], input='delete table inet sub2api_egress\nthis is invalid\n', capture_output=True, text=True)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertTrue(self.request("app"))
        self.renew_exit = False
        time.sleep(3.2)
        self.assertFalse(self.request("app"))
        self.assertFalse(self.request("capture"))
        self.renew_exit = True
        time.sleep(.35)
        self.assertTrue(self.request("app"))
        self.renew_origin = False
        time.sleep(3.2)
        self.assertFalse(self.request("app"))
        self.assertFalse(self.request("capture"))
        self.assertTrue(self.request("proxy", "172.31.200.4", 5432), "公共守护停止不应解除保护或切断已登记的无关容器")
        self.renew_origin = True
        time.sleep(.35)
        self.assertTrue(self.request("capture"))
        # 新 cgroup 模拟容器重建：在重新登记前，新进程第一包即被拒绝。
        recreated = self.group / "recreated"
        recreated.mkdir()
        process = self.launch("app", [sys.executable, "-u", str(self.client_script)], recreated)
        self.assertFalse(self.request("app", held=process))
        # 隧道端点错误和断隧道不能退回物理直连。
        peer = self.policy["nodes"]["exit"]["public_key"]
        self.inside("origin", "wg", "set", "wg-test", "peer", peer, "endpoint", "69.63.195.103:51832")
        self.assertFalse(self.request("app"))
        self.inside("origin", "wg", "set", "wg-test", "peer", peer, "endpoint", "69.63.195.102:51832")
        # 错公钥没有可用握手，也不得通过原有连接回退到直连。
        wrong_private = self.command("wg", "genkey")
        wrong_peer = self.command("wg", "pubkey", input_text=wrong_private + "\n")
        self.inside("origin", "wg", "set", "wg-test", "peer", peer, "remove")
        self.inside("origin", "wg", "set", "wg-test", "peer", wrong_peer, "endpoint", "69.63.195.102:51832", "allowed-ips", "0.0.0.0/0")
        self.assertFalse(self.request("app", held=held))
        self.assertFalse(self.request("capture"))
        self.inside("origin", "wg", "set", "wg-test", "peer", wrong_peer, "remove")
        self.inside("origin", "wg", "set", "wg-test", "peer", peer, "endpoint", "69.63.195.102:51832", "allowed-ips", "0.0.0.0/0", "persistent-keepalive", "1")
        self.inside("origin", "ip", "link", "set", "wg-test", "down")
        self.assertFalse(self.request("app"))
        self.inside("origin", "ip", "link", "set", "wg-test", "up")
        self.assert_no_unauthorized_packets()

    def test_r15_guard_drives_real_kernel_leases_and_requires_fresh_recovery(self):
        """两端真实守护逻辑驱动真实内核；容器清单和 HTTPS 解析由隔离网络适配器提供。

        此链证明守护到数据面的闭环，不宣称验证 Docker/systemd 宿主启动；后者在发布验收单独执行。
        探针适配器仍通过相应 cgroup 和网络命名空间发送实际 TCP 包，不访问宿主公网。
        """

        self.stop.set()
        self.worker.join(timeout=5)
        self.assertFalse(self.worker.is_alive())
        self.start_server("wan", "0.0.0.0", 443, "tcp")
        guards = {}
        for role in ("origin", "exit"):
            guard = deploy.EgressGuard.__new__(deploy.EgressGuard)
            guard.contract, guard.policy, guard.role = arm, copy.deepcopy(self.policy), role
            guard.policy["probe_refresh_seconds"], guard.policy["probe_max_age_seconds"] = 1, 3
            guard.policy_sha256 = arm.egress_policy_sha256(guard.policy)
            guard.policy_path = self.root / "policy.json"
            guard.runtime_root = self.root / role
            guard.runtime_root.mkdir(mode=0o700)
            guard.status_path = guard.runtime_root / "status.json"
            guard.parents = {"sub2api_egress.slice": str(self.group)} if role == "origin" else {}
            guard.maps = {"sub2api_egress.slice": self.maps} if role == "origin" else {}
            with mock.patch.object(deploy, "egress_command", side_effect=lambda argv, role=role, **kw: self.inside(role, *argv, input_text=kw.get("input_text"))):
                guard.manifest = {"firewall_sha256": deploy.egress_firewall_identity(role)}
            guard.inventory = {"services": {}, "bypass_ifindices": []}
            guard.observations, guard.pending, guard.next_probe = {}, {}, {}
            guard.blocked_since, guard.last_compliant, guard.lease_states = {}, {}, {}
            guard.probes = {url: "84.1.1.1" for url in guard.policy["probe_urls"]}
            guard.pool, guard.resolver_pool = ThreadPoolExecutor(max_workers=2), ThreadPoolExecutor(max_workers=1)
            self.addCleanup(guard.pool.shutdown, wait=True, cancel_futures=True)
            self.addCleanup(guard.resolver_pool.shutdown, wait=True, cancel_futures=True)
            guard.resolver_pending, guard.next_resolution = None, time.time() + 3600
            guard.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            guard.previous_state, guard.health, guard.health_expiry = None, {}, 0
            guards[role] = guard
        inventory = copy.deepcopy(self.inventory)
        for service in inventory["services"].values():
            service.update(parent="sub2api_egress.slice", valid=True, reason="")
        probe_fault = set()
        halt, guard_errors = threading.Event(), []

        def probe(policy, name, resolved):
            role = "app" if name == "sub2apiplus" else "capture"
            passed = self.request(role, port=443) and name not in probe_fault
            return [{"url": url, "status": "passed" if passed else "failed",
                     "ip_address": policy["allowed_public_ipv4"][0] if passed else None,
                     "observed_at_epoch": time.time(), "response_sha256": "e" * 64 if passed else None}
                    for url in policy["probe_urls"]]

        def remote(policy):
            if guards["exit"].health_expiry <= time.monotonic_ns():
                raise deploy.DeploymentError("隔离出口守护已失效")
            return guards["exit"].health

        def run():
            try:
                while not halt.is_set():
                    for role in ("exit", "origin"):
                        guard = guards[role]
                        with (mock.patch.object(arm, "load_egress_policy", return_value=guard.policy),
                              mock.patch.object(deploy, "egress_inventory", return_value=copy.deepcopy(inventory)),
                              mock.patch.object(deploy, "egress_probe_service", side_effect=probe),
                              mock.patch.object(deploy, "egress_remote_status", side_effect=remote),
                              mock.patch.object(deploy, "egress_command", side_effect=lambda argv, **kw: self.inside(role, *argv, input_text=kw.get("input_text")))):
                            guard.step()
                    halt.wait(.1)
            except BaseException as error:
                guard_errors.append(repr(error))
                halt.set()

        def await_status(expected, limit=6):
            start = time.monotonic()
            while time.monotonic() - start < limit:
                self.assertFalse(guard_errors, guard_errors)
                if guards["origin"].status_path.exists():
                    status = json.loads(guards["origin"].status_path.read_text())
                    if {name: item["status"] for name, item in status["services"].items()} == expected:
                        return time.monotonic() - start
                time.sleep(.05)
            self.fail("隔离守护未在期限内达到状态：" + repr(expected))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        compliant = {"sub2apiplus": "compliant", "capture-cli": "compliant"}
        blocked = {name: "blocked" for name in compliant}
        try:
            await_status(compliant)
            self.assertTrue(self.request("app"))
            self.assertTrue(self.request("capture"))
            probe_fault.add("capture-cli")
            individual_seconds = await_status({"sub2apiplus": "compliant", "capture-cli": "blocked"})
            self.assertFalse(self.request("capture"))
            self.assertTrue(self.request("app"))
            probe_fault.clear()
            await_status(compliant)
            # 静态规则被改写后共享保护失败；修复也必须等待两个新探针。
            self.inside("exit", "nft", "flush", "set", "inet", deploy.EGRESS_TABLE, "private4")
            shared_seconds = await_status(blocked)
            self.assertFalse(self.request("app"))
            self.assertFalse(self.request("capture"))
            self.inside("exit", "nft", "add", "element", "inet", deploy.EGRESS_TABLE, "private4", "{", ",".join(deploy.EGRESS_PRIVATE_NETWORKS), "}")
            await_status(compliant)
            arm.validate_egress_status(guards["origin"].policy, json.loads(guards["origin"].status_path.read_text()), now_epoch=time.time())
            print(json.dumps({"fixture": "r15-real-guard-kernel", "individual_detection_seconds": individual_seconds,
                              "shared_detection_seconds": shared_seconds, "unauthorized_business_packets": 0}, ensure_ascii=False), flush=True)
        finally:
            halt.set()
            worker.join(timeout=12)
        self.assertFalse(worker.is_alive())
        self.assertFalse(guard_errors, guard_errors)
        self.assert_no_unauthorized_packets()

    def cleanup(self):
        self.stop.set()
        if hasattr(self, "worker"):
            self.worker.join(timeout=15)
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        for name in reversed(self.namespaces):
            subprocess.run(["ip", "netns", "delete", name], capture_output=True)
        if self.pin.exists():
            for item in self.pin.iterdir():
                item.unlink()
            self.pin.rmdir()
        if self.group.exists():
            for child in self.group.iterdir():
                if child.is_dir():
                    child.rmdir()
            self.group.rmdir()
        shutil.rmtree(self.root)


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "进程与 Docker 隔离链要求 Linux root")
class EgressProcessTests(unittest.TestCase):
    """真实父监督器、独立 monitor、子进程和 Docker；网络状态由测试适配器生成。

    两个测试容器没有任何网络，状态适配器只把实际 Docker 身份变化映射为探测等待。
    数据面保证由独立内核链验证，本链不把合成状态当成生产出口验收。
    """

    def test_real_parent_waits_for_docker_restart_rebuild_and_never_resumes_after_fault(self):
        root = Path(tempfile.mkdtemp(prefix=".r15-process-", dir=Path(supervisor.__file__).resolve().parents[2]))
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        names = [f"r15process{os.getpid()}app", f"r15process{os.getpid()}capture"]
        def docker(*args):
            result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[:800])
            return result.stdout.strip()
        image = docker("image", "inspect", "alpine:3.21", "--format", "{{.Id}}")
        for name in names:
            self.addCleanup(lambda name=name: subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15))
        compose = root / "compose.json"
        configuration = {"name": f"r15process{os.getpid()}", "services": {
            f"service{index}": {"image": image, "container_name": name, "network_mode": "none",
                               "entrypoint": ["/bin/sh", "-c", 'trap "exit 0" TERM; while :; do sleep 0.1 & wait; done'],
                               "stop_grace_period": "2s", "labels": {"r15-fixture": "1"}}
            for index, name in enumerate(names)}}
        compose.write_text(json.dumps(configuration))
        docker("compose", "-f", str(compose), "up", "-d")
        before_ids = {name: docker("inspect", name, "--format", "{{.Id}}") for name in names}
        policy = egress_tests.policy_fixture()
        policy["services"] = {name: {**copy.deepcopy(settings), "dependencies": []}
                              for name, settings in zip(names, policy["services"].values())}
        arm.validate_egress_policy(policy)
        policy_path, status_path = root / "policy.json", root / "status.json"
        deploy.write_json_atomic(policy_path, policy)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        failed, halt, identities, changed_at, seen = [], threading.Event(), {}, {}, []
        fault_trigger = root / "fault-trigger"
        def publish():
            try:
                while not halt.is_set():
                    items = {item["Name"].lstrip("/"): item for item in json.loads(docker("inspect", *names))}
                    snapshot = egress_tests.runtime_fixture()["runtime"]
                    snapshot.update(policy_sha256=arm.egress_policy_sha256(policy), boot_id=boot)
                    snapshot["services"] = {name: value for name, value in zip(names, snapshot["services"].values())}
                    for name, service in snapshot["services"].items():
                        item = items[name]
                        identity = (item["Id"], item["State"]["Pid"], item["State"]["StartedAt"])
                        if identities.get(name) != identity:
                            identities[name], changed_at[name] = identity, time.monotonic()
                        service.update(container_id=item["Id"], observations=egress_tests.observations(policy))
                        if not item["State"]["Running"]:
                            service.update(status="blocked", admission_state="missing", container_id="", network_bindings=[], observations=[])
                        elif time.monotonic() - changed_at[name] < .6:
                            service.update(status="blocked", admission_state="probing", observations=[])
                        seen.append(service["admission_state"])
                    if fault_trigger.exists():
                        snapshot["shared_protection"].update(status="blocked", reason="隔离共享故障")
                        snapshot["shared_protection"]["checks"]["wireguard"] = False
                    deploy.write_json_atomic(status_path, snapshot)
                    halt.wait(.06)
            except BaseException as error:
                failed.append(repr(error))
        publisher = threading.Thread(target=publish, daemon=True)
        publisher.start()
        def stop_publisher():
            halt.set()
            publisher.join(timeout=30)
        self.addCleanup(stop_publisher)
        original_require, original_load = arm.require_runtime_egress, arm.load_egress_policy
        def require():
            return original_require(policy_path, status_path)
        def admitted():
            until = time.monotonic() + 8
            while time.monotonic() < until:
                self.assertFalse(failed, failed)
                try:
                    return require()
                except (OSError, ValueError):
                    time.sleep(.05)
            self.fail("隔离 Docker 状态未完成准入")
        admitted()
        wrapper = root / "supervisor-fixture.py"
        wrapper.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(Path(supervisor.__file__).resolve().parents[2])!r})\n"
            "from tools.official_client_capture import codex_upgrade_supervisor as s\n"
            "a=s.arm64_environment\nload,require=a.load_egress_policy,a.require_runtime_egress\n"
            f"a.EGRESS_STATUS_PATH=Path({str(status_path)!r})\n"
            f"a.load_egress_policy=lambda path=Path({str(policy_path)!r}):load(path)\n"
            f"a.require_runtime_egress=lambda:require(Path({str(policy_path)!r}),Path({str(status_path)!r}))\n"
            "raise SystemExit(s.main())\n")
        original_popen = subprocess.Popen
        def launch(argv, *args, **kwargs):
            if len(argv) > 2 and argv[1] == str(Path(supervisor.__file__).resolve()) and argv[2] == "monitor":
                argv = [argv[0], str(wrapper), *argv[2:]]
            return original_popen(argv, *args, **kwargs)
        client = supervisor.SupervisorClient(root / "runs", campaign_id="egress-process-fixture", phase="official",
                                             campaign_dir=root, deadline_at_epoch=time.time() + 60,
                                             heartbeat_seconds=.1, watchdog_timeout_seconds=4, ledger_interval_seconds=.1,
                                             terminate_owner=False)
        with (mock.patch.object(arm, "campaign_requires_runtime_egress", return_value=True),
              mock.patch.object(arm, "require_runtime_egress", side_effect=require),
              mock.patch.object(arm, "load_egress_policy", side_effect=lambda path=policy_path: original_load(path)),
              mock.patch.object(arm, "EGRESS_STATUS_PATH", status_path),
              mock.patch.object(subprocess, "Popen", side_effect=launch)):
            client.start()
            try:
                environment = {**os.environ, supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1",
                               supervisor.CAMPAIGN_RUN_DIR_ENV: str(client.run_dir), supervisor.CAMPAIGN_RUN_OWNER_PID_ENV: str(os.getpid()),
                               supervisor.CAMPAIGN_RUN_OWNER_NONCE_ENV: client.owner_nonce, supervisor.CAMPAIGN_RUN_ID_ENV: client.campaign_id}
                def transition(command, compose_service=None):
                    argv = [sys.executable, str(wrapper), "egress-transition", "--container", names[0], "--timeout-seconds", "15"]
                    if compose_service:
                        argv += ["--compose-service", compose_service]
                    answer = client.run_command([*argv, "--", *command], operation="fixture:maintenance", timeout_seconds=20,
                                                cleanup_grace_seconds=2, env=environment)
                    self.assertEqual(answer.returncode, 0, answer.stdout)
                transition(["docker", "restart", names[0]])
                configuration["services"]["service0"]["labels"]["r15-fixture"] = "2"
                compose.write_text(json.dumps(configuration))
                transition(["docker", "compose", "-f", str(compose), "up", "-d", "--no-deps", "service0"], "service0")
                self.assertNotEqual(before_ids[names[0]], docker("inspect", names[0], "--format", "{{.Id}}"))
                self.assertEqual(before_ids[names[1]], docker("inspect", names[1], "--format", "{{.Id}}"))
                finishes = [json.loads(path.read_bytes()) for path in (client.run_dir / "egress-transitions").glob("*.finish.json")]
                self.assertEqual([item["status"] for item in finishes], ["passed", "passed"])
                self.assertIn("probing", seen)
                cleaned = root / "cleaned"
                source = ("import signal,time,sys\nfrom pathlib import Path\n"
                          f"def cleanup(*args):\n Path({str(cleaned)!r}).touch()\n sys.exit(0)\n"
                          f"signal.signal(signal.SIGUSR1,cleanup)\nPath({str(fault_trigger)!r}).touch()\ntime.sleep(30)\n")
                started = time.monotonic()
                with self.assertRaises(supervisor.RuntimeEgressPaused):
                    client.run_command([sys.executable, "-c", source], operation="fixture:outage", timeout_seconds=10,
                                       cleanup_grace_seconds=2)
                self.assertTrue(cleaned.exists())
                self.assertLess(time.monotonic() - started, 3)
                pause = (client.run_dir / "egress-pause.json").read_bytes()
                fault_trigger.unlink()
                admitted()
                with self.assertRaises(supervisor.RuntimeEgressPaused):
                    client.run_command([sys.executable, "-c", "pass"], operation="fixture:forbidden-resume", timeout_seconds=1)
                self.assertEqual((client.run_dir / "egress-pause.json").read_bytes(), pause)
                print(json.dumps({"fixture": "r15-docker-supervisor", "restarts": 1, "rebuilds": 1,
                                  "live_request_count": 0, "same_run_resumption_rejected": True}), flush=True)
            finally:
                client.stop(reason="isolated-egress-test-finished", status="failed")
        self.assertFalse(failed, failed)


if __name__ == "__main__":
    unittest.main()
