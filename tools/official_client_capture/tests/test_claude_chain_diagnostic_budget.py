"""验证诊断独立预算、集中收错和正式入口隔离，不运行长链。"""

import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture.tests import claude_chain_diagnostic_budget as budget
from tools.official_client_capture.tests import claude_chain_driver as candidate_driver
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as chains
from tools.official_client_capture.tests.real_chains import test_claude_vc1_recorded_chain as recorded


class DiagnosticBudgetTests(unittest.TestCase):
    def test_candidate_recorded_inner_process_uses_method_budget_and_keeps_timeout_failure(self):
        """内层保留独立预算；外层各退出路径保留原坐标与日志，隔离子进程只写私有根。"""
        chain = "late-continuous"
        metrics = {"steps": ["轻量原件回归"], "upstream_requests": 0,
                   "scenarios": [{"scenario": name, "campaign_id": "同一夹具Campaign",
                                  "checks": {key: {"passed": True} for key in chains.SIX_CHECKS}}
                                 for name in chains.CHAINS[chain]["expected"]]}
        normal_stdout = (chains.METRICS_PREFIX + json.dumps(metrics, ensure_ascii=False) + "\r\n").encode()
        normal_stderr = "原始子进程日志\r\n".encode() + b"\x00"
        for outcome in ("passed", "failed", "timeout", "invalid-metrics"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                external, scratch = root / "external", root / "scratch"
                external.mkdir(mode=0o700)
                scratch.mkdir(mode=0o700)
                original = external / "existing-original.json"
                original.write_bytes(b"existing original remains unchanged")
                stdout = b"invalid metric\xff\r\n" if outcome == "invalid-metrics" else normal_stdout
                stderr = normal_stderr
                observed = {}
                case = chains.ClaudeFaultChainTests()

                def copy_executor_tree(destination):
                    """预算夹具保留合法普通 tree，让原执行器运输与核验实际处理闭包字节。"""
                    destination.mkdir(mode=0o700)
                    for source in chains.trees._closure_files(Path(chains.__file__)):
                        relative = source.relative_to(chains.REPO_ROOT).as_posix()
                        if not chains.stops._executor_path(relative):
                            continue
                        target = destination / relative
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
                    return destination

                def launch(argv, **kwargs):
                    environment = kwargs["env"]
                    private = Path(environment["CLAUDE_RECORDED_CHAIN_RECORD_DIR"])
                    observed["private"] = private
                    observed["original"] = json.dumps({"data_root": str(private / "staging/data")}).encode()
                    self.assertTrue(private.is_relative_to(scratch))
                    self.assertFalse(private.is_relative_to(external))
                    self.assertEqual(environment[chains.shell.ISOLATION_ENV], chains.shell.ISOLATION_NAMESPACE)
                    self.assertIn(chains.shell.NAMESPACE_SCRIPT, argv)
                    self.assertEqual(kwargs["timeout"], 7260)
                    (private / "state-original.json").write_bytes(observed["original"])
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(argv, 7260, output=stdout, stderr=stderr)
                    return subprocess.CompletedProcess(argv, 1 if outcome == "failed" else 0, stdout, stderr)

                with mock.patch.dict(os.environ, {"CLAUDE_RECORDED_CHAIN_RECORD_DIR": str(external)}, clear=True), \
                        mock.patch.object(chains.sys, "platform", "linux"), \
                        mock.patch.object(case, "_isolation_mode", return_value=chains.shell.ISOLATION_NAMESPACE), \
                        mock.patch.object(chains.shell, "_scratch_root", return_value=scratch), \
                        mock.patch.object(chains, "_copy_tree", side_effect=copy_executor_tree), \
                        mock.patch.object(chains.trees, "subprocess_env", side_effect=lambda tree, extra: {**os.environ, **extra}), \
                        mock.patch.object(chains.subprocess, "run", side_effect=launch):
                    if outcome == "passed":
                        case._run_chain(chain)
                    else:
                        expected = UnicodeDecodeError if outcome == "invalid-metrics" else AssertionError
                        with self.assertRaises(expected):
                            case._run_chain(chain)
                    self.assertEqual(os.environ["CLAUDE_RECORDED_CHAIN_RECORD_DIR"], str(external))

                sidecars = list(external.glob("fault-chain-*"))
                self.assertEqual(len(sidecars), 1)
                output = sidecars[0]
                state = json.loads((output / "scratch.json").read_text())
                self.assertEqual((output / "stdout.log").read_bytes(), stdout)
                self.assertEqual((output / "stderr.log").read_bytes(), stderr)
                self.assertEqual(state["returncode"], None if outcome == "timeout" else 1 if outcome == "failed" else 0)
                self.assertEqual(state["timed_out"], outcome == "timeout")
                self.assertEqual(state["error_type"], "UnicodeDecodeError" if outcome == "invalid-metrics" else None)
                self.assertTrue(Path(state["scratch_root"]).is_dir())
                self.assertIsNotNone(state["executor"])
                chains.stops.verify_executor_source(chains.stops._decoded(state["executor"]),
                                                    Path(state["scratch_root"]) / "staging/tree")
                self.assertEqual(Path(state["recorded_worlds"]), observed["private"])
                self.assertEqual((observed["private"] / "state-original.json").read_bytes(), observed["original"])
                self.assertEqual(original.read_bytes(), b"existing original remains unchanged")

        # 两个候选恢复入口共用独立方法预算；内层超时仍保留日志并向外失败。
        with tempfile.TemporaryDirectory() as temporary:
            case = candidate_driver.ClaudeCandidateChainDriver()
            case.root = Path(temporary)
            case.data = case.root / "data"
            environment = {"CLAUDE_2_1_226_RELAY_FIXTURE": "/fixture/relay", "CLAUDE_2_1_226_FREEZE_FIXTURE": "/fixture/freeze"}
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(candidate_driver.platform, "system", return_value="Linux"), \
                    mock.patch.object(candidate_driver.platform, "machine", return_value="aarch64"), \
                    mock.patch.object(candidate_driver.os, "geteuid", return_value=0), \
                    mock.patch.object(candidate_driver.os, "readlink", return_value="ns:[fixture]"):
                for scenario, method in candidate_driver.SCENARIO_METHODS.items():
                    with self.subTest(scenario=scenario), \
                            mock.patch.object(candidate_driver.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "stdout", "stderr")) as launch, \
                            mock.patch.object(candidate_driver.source, "read", return_value={"status": "passed", "scenario_method": method}):
                        self.assertTrue(case.recorded_entry(scenario))
                        self.assertEqual(launch.call_args.kwargs["timeout"], 3540)
                failure = subprocess.TimeoutExpired("fixture", 3540, output=b"original out", stderr=b"original err")
                with mock.patch.object(candidate_driver.subprocess, "run", side_effect=failure), self.assertRaises(subprocess.TimeoutExpired) as caught:
                    case.recorded_entry("reconcile-resume")
                self.assertIs(caught.exception, failure)
                self.assertEqual((case.root / "stdout.log").read_bytes(), b"original out")
                self.assertEqual((case.root / "stderr.log").read_bytes(), b"original err")

    def test_explicit_budget_is_bounded_and_default_is_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(budget.diagnostic_seconds())
        for invalid in ('', '-1', '899', '7201', 'nan', '1.5'):
            with self.subTest(value=invalid), mock.patch.dict(os.environ, {budget.DIAGNOSTIC_TIMEOUT_ENV: invalid}):
                with self.assertRaises(ValueError):
                    budget.diagnostic_seconds()
        with mock.patch.dict(os.environ, {budget.DIAGNOSTIC_TIMEOUT_ENV: '3600'}):
            self.assertEqual(budget.diagnostic_seconds(), 3600)
        self.assertEqual((chains.CHAIN_TIMEOUT_SECONDS, chains.CHAIN_BUDGET_SECONDS), (7260, 7320))

    def test_public_entries_reject_inherited_diagnostic_budget_before_work(self):
        with mock.patch.dict(os.environ, {budget.DIAGNOSTIC_TIMEOUT_ENV: '3600'}), mock.patch.object(chains, '_copy_tree') as copy:
            for run in (lambda: chains.ClaudeFaultChainTests()._run_chain('recovery'),
                        recorded.ClaudeRecordedVC1ChainTests().test_real_vc0_closeout_supervised_copy_and_vc1_seal,
                        recorded.ClaudeRecordedVC1ChainTests()._run_pending_full_validation):
                with self.subTest(run=run), self.assertRaisesRegex(AssertionError, '诊断预算'):
                    run()
            copy.assert_not_called()

    def test_each_scenario_has_full_budget_and_keeps_all_failures(self):
        timeouts, methods = [], []
        class Process:
            pid = 123
            returncode = 1
            def communicate(self, timeout=None):
                timeouts.append(timeout)
                return '', 'Ran 1 test in 0.1s\nFAILED (failures=1)\n真实诊断失败'
        def launch(argv, **kwargs):
            methods.append(argv[-1])
            return Process()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {
                budget.DIAGNOSTIC_TIMEOUT_ENV: '3600', 'CLAUDE_RECORDED_CHAIN_RECORD_DIR': temporary}), \
                mock.patch.object(chains.subprocess, 'Popen', side_effect=launch), \
                mock.patch.object(chains.shell, '_tree_root', return_value=Path(temporary)):
            recorder = chains._ChainRecorder('diagnostic-test')
            recorder.started -= 10000
            with mock.patch.object(recorder, 'emit'), self.assertRaisesRegex(AssertionError, '独立场景已收齐失败'):
                chains._run_group(self, recorder, {'A.first': '第一个', 'A.second': '第二个'}, '两项独立场景')
            self.assertEqual(timeouts, [3600, 3600])
            self.assertEqual(len(methods), 2)
            self.assertEqual(len(recorder.facts['groups']), 2)
            self.assertTrue(all(row['diagnostic_only'] for row in recorder.facts['groups']))
            logs = list((Path(temporary) / 'diagnostic-groups').glob('*/stderr.log'))
            self.assertEqual(len(logs), 2)
            self.assertTrue(all(path.read_text() == 'Ran 1 test in 0.1s\nFAILED (failures=1)\n真实诊断失败'
                                for path in logs))

    def test_group_failure_does_not_prevent_later_diagnostic_groups(self):
        called = []
        def exercise(case, recorder, group, step):
            called.append(step)
            raise AssertionError('本组故障：' + step)
        with mock.patch.dict(os.environ, {budget.DIAGNOSTIC_TIMEOUT_ENV: '3600'}), \
                mock.patch.object(chains.shell, '_assert_isolation'), \
                mock.patch.object(chains, '_run_group', side_effect=exercise), \
                mock.patch.object(chains._ChainRecorder, 'emit'):
            with self.assertRaisesRegex(AssertionError, '已执行全部独立场景组'):
                chains.ClaudeFaultChainTests()._exercise_recovery_chain()
        self.assertEqual(called, [step for step, _group in chains.RECOVERY_CHAIN_GROUPS])

    def test_formal_groups_collect_original_failures_without_claiming_pass(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(chains.shell, '_assert_isolation'), \
                mock.patch.object(chains, '_run_group', side_effect=AssertionError('原始失败')) as run:
            with self.assertRaisesRegex(AssertionError, '原始失败'):
                chains.ClaudeFaultChainTests()._exercise_recovery_chain()
            self.assertEqual(run.call_count, len(chains.RECOVERY_CHAIN_GROUPS))

    def test_formal_methods_have_full_budget_even_after_prior_group_exhaustion(self):
        """前面已用时超过旧整链上限，两个后续方法仍各有完整预算且原失败都保留。"""
        timeouts = []
        class Process:
            returncode = 1
            def communicate(self, timeout=None):
                timeouts.append(timeout)
                return '', 'Ran 1 test in 0.1s\nFAILED (failures=1)\n真实失败'
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(chains.subprocess, 'Popen', side_effect=lambda *args, **kwargs: Process()), \
                mock.patch.object(chains.shell, '_tree_root', return_value=Path(temporary)):
            recorder = chains._ChainRecorder('recovery')
            recorder.started -= 10000
            with mock.patch.object(recorder, 'emit'), self.assertRaisesRegex(AssertionError, '独立场景已收齐失败'):
                chains._run_group(self, recorder, {'A.first': '前项', 'A.second': '后项'}, '独立预算')
        self.assertEqual(timeouts, [3600, 3600])
        self.assertEqual(len(recorder.facts['groups']), 2)
        self.assertTrue(all(not row['diagnostic_only'] for row in recorder.facts['groups']))

    def test_formal_method_timeout_still_kills_its_group_and_fails(self):
        """扩大安全网不能让真实超时算通过，也不能留下超时子进程。"""
        process = mock.Mock(pid=123, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired('fixture', 3600), ('', '超时原件')]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(chains.subprocess, 'Popen', return_value=process), \
                mock.patch.object(chains.os, 'killpg') as kill, \
                mock.patch.object(chains.shell, '_tree_root', return_value=Path(temporary)):
            recorder = chains._ChainRecorder('recovery')
            with mock.patch.object(recorder, 'emit'), self.assertRaisesRegex(AssertionError, '本方法独立预算 3600 秒'):
                chains._run_group(self, recorder, {'A.first': '超时'}, '超时方法')
            kill.assert_called_once_with(123, chains.signal.SIGKILL)
        self.assertIsNone(recorder.facts['groups'][0]['returncode'])

    def test_recorded_chain_shell_and_executor_share_one_hour_contract(self):
        for name in chains.certification.CLAUDE_RECORDED_CHAIN_IDS:
            contract = chains.certification.claude_recorded_time_budget(name)
            self.assertEqual(contract['method_timeout_seconds'], 3600)
            self.assertLess(contract['method_timeout_seconds'], contract['chain_timeout_seconds'])
            self.assertLess(contract['chain_timeout_seconds'], contract['chain_budget_seconds'])
            self.assertLess(contract['chain_budget_seconds'], contract['unit_timeout_seconds'])
        with self.assertRaises(KeyError):
            chains.certification.claude_recorded_time_budget('vc-chain.codex')

    def test_continuous_method_receives_two_hours_without_changing_recorded_or_recovery(self):
        """连续世界只有一个方法，七／八个场景共享两小时；不能被旧一小时子进程上限截断。"""
        for chain in ('late-continuous', 'revision-continuous'):
            process = mock.Mock(returncode=1)
            process.communicate.return_value = ('', 'Ran 1 test in 0.1s\nFAILED (failures=1)')
            with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(chains.subprocess, 'Popen', return_value=process), \
                    mock.patch.object(chains.shell, '_tree_root', return_value=Path(temporary)):
                recorder = chains._ChainRecorder(chain)
                recorder.started -= 10000
                with mock.patch.object(recorder, 'emit'), self.assertRaises(AssertionError):
                    chains._run_group(self, recorder, {'A.continuous': ('前段', '后段')}, '连续方法')
                process.communicate.assert_called_once_with(timeout=7200)

    def test_registered_method_counts_and_nested_limits_close(self):
        """独立方法数与登记一致；外部执行器必须在内层收尾之后才到期。"""
        count = sum(len(group) for _, group in chains.RECOVERY_CHAIN_GROUPS)
        for key, spec in chains.CHAINS.items():
            contract = chains.certification.claude_fault_time_budget(spec['scenario'])
            self.assertEqual(contract['method_count'], count if key == 'recovery' else 1)
            self.assertLess(contract['method_count'] * contract['method_timeout_seconds'], contract['chain_timeout_seconds'])
            self.assertLess(contract['chain_timeout_seconds'], contract['chain_budget_seconds'])
            self.assertLess(contract['chain_budget_seconds'], contract['unit_timeout_seconds'])

    def test_resume_shell_keeps_cumulative_cost_and_limits_only_this_invocation(self):
        """原世界累计超过安全网上限仍能续作；本次超时和子进程失败不能变成通过。"""
        outcomes = (
            ('late-continuous', 5367.511, 2000.0, 'passed'),
            ('revision-continuous', 10000.0, 10.0, 'passed'),
            ('late-continuous', 10000.0, 7320.0, 'limit'),
            ('late-continuous', 10000.0, 10.0, 'timeout'),
            ('revision-continuous', 10000.0, 10.0, 'failed'),
        )
        for chain, prior_seconds, invocation_seconds, outcome in outcomes:
            with self.subTest(chain=chain, outcome=outcome), tempfile.TemporaryDirectory() as directory:
                scratch = Path(directory).resolve()
                world = scratch / 'existing-world'
                tree, recorded_worlds = world / 'staging/tree', world / 'staging/recorded-worlds'
                tree.mkdir(parents=True)
                recorded_worlds.mkdir()
                original = recorded_worlds / 'original.bin'
                original.write_bytes(b'original bytes remain unchanged')
                stop = {'scratch_root': str(world), 'tree': str(tree), 'recorded_worlds': recorded_worlds,
                        'seconds': prior_seconds, 'invocations': []}
                case = chains.ClaudeFaultChainTests()
                contract = chains.certification.claude_fault_time_budget(chains.CHAINS[chain]['scenario'])
                metrics = {'steps': ['明确的短验夹具'], 'upstream_requests': 0, 'scenarios': [
                    {'scenario': name, 'campaign_id': 'same-short-fixture',
                     'checks': {key: {'passed': True} for key in chains.SIX_CHECKS}}
                    for name in chains.CHAINS[chain]['expected']]}
                stdout = (chains.METRICS_PREFIX + json.dumps(metrics) + '\n').encode()
                stderr = b'Ran 1 test in 0.1s\nOK\n'
                result = subprocess.CompletedProcess([], 1 if outcome == 'failed' else 0, stdout, stderr)
                timeout = subprocess.TimeoutExpired('short-fixture', contract['chain_timeout_seconds'],
                                                    output=stdout, stderr=stderr)
                with mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(chains.sys, 'platform', 'linux'), \
                        mock.patch.object(case, '_isolation_mode', return_value=chains.shell.ISOLATION_DEGRADED), \
                        mock.patch.object(chains.shell, '_scratch_root', return_value=scratch), \
                        mock.patch.object(chains.trees, 'subprocess_env', side_effect=lambda tree, extra: dict(extra)), \
                        mock.patch.object(chains.time, 'monotonic', side_effect=[100.0, 100.0 + invocation_seconds]), \
                        mock.patch.object(chains.subprocess, 'run', return_value=result,
                                          side_effect=timeout if outcome == 'timeout' else None) as launch, \
                        mock.patch.object(chains.stops, 'save_state') as save:
                    if outcome == 'passed':
                        case._run_chain_body(chain, stop=stop)
                    else:
                        message = {'limit': '本次调用超过安全网', 'timeout': '执行器超过清理时限',
                                   'failed': '子进程失败'}[outcome]
                        with self.assertRaisesRegex(AssertionError, message):
                            case._run_chain_body(chain, stop=stop)
                    self.assertEqual(launch.call_args.kwargs['timeout'], contract['chain_timeout_seconds'])
                    save.assert_called_once_with('chain:' + chain, stop)
                cumulative_seconds = round(prior_seconds + invocation_seconds, 3)
                self.assertEqual(stop['seconds'], cumulative_seconds)
                self.assertEqual(case.real_chain_metrics['seconds'], cumulative_seconds)
                self.assertEqual(case.real_chain_metrics['invocation_seconds'], invocation_seconds)
                self.assertEqual(case.real_chain_metrics['time_budget'], contract)
                self.assertEqual(case.real_chain_metrics['formal_time_budget_met'], outcome != 'limit')
                self.assertEqual(original.read_bytes(), b'original bytes remain unchanged')


from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_fixture


class FixtureDeadlineTests(batch_fixture._RealTreeBatchCase):
    def test_frozen_campaign_and_both_ledgers_share_one_deadline_before_extension(self):
        """从新世界直接走真实延期，复现原错配而不重跑十分钟候选准备。"""
        import json
        from datetime import datetime, timedelta
        from tools.official_client_capture import codex_upgrade_project_ledger as project
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        campaign = self.build_batch_campaign()
        plan_path = campaign / 'control/vc/campaign-plan.json'
        plan = json.loads(plan_path.read_text())
        current = timing.inspect_ledger(self.ledger_dir)['total_deadline_at_utc']
        registered = project.replay_head(self.project_root)['registered_campaigns'][campaign.name]['deadline_at_utc']
        self.assertEqual(plan['original_deadline_at_utc'], current)
        self.assertEqual(registered, current)
        original = {path: path.read_bytes() for path in (plan_path, campaign / 'campaign.json', self.ledger_dir / 'ledger.json')}
        target = (datetime.fromisoformat(current.replace('Z', '+00:00')) + timedelta(minutes=30)).isoformat()
        preview = project.preview_deadline_extension(campaign, scope='campaign', phase=None,
            new_deadline_at_utc=target, reason='连续链截止同源回归')
        result = project.apply_deadline_extension(campaign, preview_path=Path(preview['preview_path']),
            approve_sha256=preview['review_sha256'], approved_by='离线回归')
        self.assertEqual(result['status'], 'extended')
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)['total_deadline_at_utc'], target)
        self.assertEqual(project.replay_head(self.project_root)['effective_campaign_deadlines'][campaign.name], target)
        self.assertEqual(original, {path: path.read_bytes() for path in original})


if __name__ == '__main__':
    unittest.main()
