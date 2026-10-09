"""验证诊断独立预算、集中收错和正式入口隔离，不运行长链。"""

import os
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
        self.assertEqual((chains.CHAIN_TIMEOUT_SECONDS, chains.CHAIN_BUDGET_SECONDS), (870, 900))

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
            with mock.patch.object(recorder, 'emit'), self.assertRaisesRegex(AssertionError, '独立场景诊断已收齐失败'):
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
            with self.assertRaisesRegex(AssertionError, '诊断已执行全部独立场景组'):
                chains.ClaudeFaultChainTests()._exercise_recovery_chain()
        self.assertEqual(called, [step for step, _group in chains.RECOVERY_CHAIN_GROUPS])

    def test_formal_group_still_stops_on_original_failure(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(chains.shell, '_assert_isolation'), \
                mock.patch.object(chains, '_run_group', side_effect=AssertionError('原始失败')) as run:
            with self.assertRaisesRegex(AssertionError, '原始失败'):
                chains.ClaudeFaultChainTests()._exercise_recovery_chain()
            self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
