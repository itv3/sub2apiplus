"""验证诊断独立预算、集中收错和正式入口隔离，不运行长链。"""

import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture.tests import claude_chain_diagnostic_budget as budget
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as chains
from tools.official_client_capture.tests.real_chains import test_claude_vc1_recorded_chain as recorded


class DiagnosticBudgetTests(unittest.TestCase):
    def test_explicit_budget_is_bounded_and_default_is_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(budget.diagnostic_seconds())
        for invalid in ('', '-1', '899', '7201', 'nan', '1.5'):
            with self.subTest(value=invalid), mock.patch.dict(os.environ, {budget.DIAGNOSTIC_TIMEOUT_ENV: invalid}):
                with self.assertRaises(ValueError):
                    budget.diagnostic_seconds()
        with mock.patch.dict(os.environ, {budget.DIAGNOSTIC_TIMEOUT_ENV: '3600'}):
            self.assertEqual(budget.diagnostic_seconds(), 3600)
        self.assertEqual((chains.CHAIN_TIMEOUT_SECONDS, chains.CHAIN_BUDGET_SECONDS), (3660, 3720))

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
            self.assertEqual(len(list((Path(temporary) / 'diagnostic-groups').glob('*.stderr.log'))), 2)

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
            recorder = chains._ChainRecorder('formal-test')
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
            recorder = chains._ChainRecorder('formal-timeout')
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

    def test_registered_method_counts_and_nested_limits_close(self):
        """独立方法数与登记一致；外部执行器必须在内层收尾之后才到期。"""
        count = sum(len(group) for _, group in chains.RECOVERY_CHAIN_GROUPS)
        for key, spec in chains.CHAINS.items():
            contract = chains.certification.claude_fault_time_budget(spec['scenario'])
            self.assertEqual(contract['method_count'], count if key == 'recovery' else 1)
            self.assertLess(contract['method_count'] * contract['method_timeout_seconds'], contract['chain_timeout_seconds'])
            self.assertLess(contract['chain_timeout_seconds'], contract['chain_budget_seconds'])
            self.assertLess(contract['chain_budget_seconds'], contract['unit_timeout_seconds'])


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
