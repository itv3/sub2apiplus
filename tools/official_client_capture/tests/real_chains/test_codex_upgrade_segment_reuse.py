"""R11：真实派发、逐段收敛、SIGKILL 和增量封存；外部取证使用明确的零请求夹具。"""
import json
import os
import platform
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests import managed_tree_copy as trees
from tools.official_client_capture.tests.real_chains import test_codex_upgrade_evaluation_attempt_recovery_real_chain as chains


class SegmentReuseChainTests(unittest.TestCase):
    def _assert_reservation_schema(self, reservation):
        """实际写出的恢复段预约按 schema 做结构校验：必填、常量、复用字段成组出现、复用证明字段闭合。"""

        schema = json.loads((Path(__file__).resolve().parents[2]
                             / 'codex_upgrade_attempt_recovery_reservation.schema.json').read_text(encoding='utf-8'))
        self.assertTrue(set(schema['required']) <= set(reservation))
        self.assertEqual(reservation['schema_version'], schema['properties']['schema_version']['const'])
        for field, group in schema['dependentRequired'].items():
            if field in reservation:
                self.assertTrue(set(group) <= set(reservation), field)
        for proof in reservation['reuse_proofs'].values():
            self.assertEqual(set(proof), set(schema['$defs']['proof']['required']))
        scanned = reservation['reuse_validation_scanned_bytes']
        self.assertTrue(isinstance(scanned, int) and not isinstance(scanned, bool) and scanned >= 0)

    def test_three_segments_reuse_completed_jobs_and_seal(self):
        with tempfile.TemporaryDirectory(prefix='r11-staging-') as directory:
            root = Path(directory).resolve() / 'staging'
            root.mkdir(mode=0o700)
            harness = chains._AttemptRecoveryHarness(self, root)
            tree = harness.tree_b()
            trees.replace_once(tree, 'tests/evaluation_chain_driver.py',
                'if (Path(state["root"]) / AR_FAIL_FLAG).exists():',
                'if (Path(state["root"]) / AR_FAIL_FLAG).exists() and job.job_id == JOB_B:')
            trees.replace_once(tree, 'codex_upgrade.py',
                '    planned = {item["id"]: item["execution_sha256"] for item in reservation["planned_jobs"]}\n',
                '    if reservation["recovery_revision"] == "ar2":\n'
                '        import signal\n'
                '        os.kill(os.getpid(), signal.SIGKILL)  # 四项证明已写，摘要发布前真实中断。\n'
                '    planned = {item["id"]: item["execution_sha256"] for item in reservation["planned_jobs"]}\n')
            state = harness.init_ar(tree, candidate_surface='other', h1_method='GET')
            campaign_bytes = (harness.campaign_dir() / 'campaign.json').read_bytes()
            original_attempt = (harness.attempt_root() / 'attempt.json').read_bytes()
            self.assertEqual(harness.dispatch(tree, 'compare0', ['compare'])['returncode'], 0)
            harness.run(tree, 'gate', '--tag', 'b')
            failed = harness.dispatch(tree, 'b0', ['assert'], reuse_items=['compare'])
            self.assertEqual(failed['returncode'], 1)
            self.assertEqual(harness.run(tree, 'reconcile', '--run-dir', failed['campaign_run']['run_dir'])['status'], 'recoverable')
            applied = harness.apply_transient(tree)
            first = harness.ar_run(tree, 'ar1', applied, segment_fail=True)
            self.assertEqual(first['returncode'], 1)
            source_root = Path(state['job_roots'][chains.JOB_A] + '-recovery-ar1')
            source_files = {path: path.read_bytes() for path in source_root.rglob('*') if path.is_file()}
            reconciled1 = harness.run(tree, 'reconcile-ar', '--recovery-revision', 'ar1', '--approve')
            preview1 = json.loads(Path(reconciled1['recovery_preview_path']).read_text())
            self.assertEqual(preview1['reuse_job_ids'], [chains.JOB_A])
            self.assertEqual(preview1['execute_job_ids'], [chains.JOB_B])
            second = harness.ar_run(tree, 'ar2', {**applied, 'recovery_revision': 'ar2'},
                                    recovery_preview=reconciled1['recovery_preview_path'])
            self.assertEqual(second['returncode'], 1)
            self.assertFalse((harness.segment_root('ar2') / 'attempt-recovery.json').exists())
            second_jobs = [json.loads(path.read_text()) for path in harness.segment_root('ar2').glob('job-*.json')]
            self.assertEqual({row['id'] for row in second_jobs if row.get('started_at_utc')}, {chains.JOB_B})
            self.assertEqual({row['id'] for row in second_jobs if row.get('recovered_from')}, {chains.JOB_A})
            reconciled2 = harness.run(tree, 'reconcile-ar', '--recovery-revision', 'ar2', '--approve')
            preview2 = json.loads(Path(reconciled2['recovery_preview_path']).read_text())
            self.assertEqual(preview2['reuse_job_ids'], sorted([chains.JOB_A, chains.JOB_B]))
            self.assertEqual(preview2['execute_job_ids'], [])
            self.assertEqual(preview2['expected_new_requests']['known_total'], 0)
            successor = {**applied, 'recovery_revision': 'ar3'}
            third = harness.ar_run(tree, 'ar3', successor, recovery_preview=reconciled2['recovery_preview_path'],
                                   crash_at='after-binding', expect_exit=137)
            self.assertEqual(third['returncode'], 137, third)
            # R11：预约记录复验实际读取的证据字节。ar3 复验沿 ar2→ar1 递归，JOB_A 的证据在两层都出现，只读一次。
            reservation3 = json.loads((harness.segment_root('ar3') / 'recovery-reservation.json').read_text())
            reused_roots = sorted({root for row in second_jobs for root in row.get('evidence_roots', [])})
            self.assertEqual(len(reused_roots), 2, reused_roots)
            self.assertEqual(reservation3['reuse_validation_scanned_bytes'],
                             sum(path.stat().st_size for root in reused_roots for path in Path(root).rglob('*') if path.is_file()))
            self._assert_reservation_schema(reservation3)
            run_dir = max((path for path in Path(state['state_dir']).iterdir() if path.name.startswith('run-')),
                          key=lambda path: path.stat().st_mtime)
            self.assertEqual(harness.wait_run_state(str(run_dir), {'failed', 'watchdog-aborted'})['state'], 'failed')
            recovered_parent = harness.run(tree, 'reconcile', '--run-dir', str(run_dir))
            self.assertEqual(recovered_parent['status'], 'recoverable', recovered_parent)
            summary = harness.segment_summary('ar3')
            self.assertEqual(summary['execute_jobs'], [])
            self.assertTrue(all(row['recovered_from'] == 'ar2' and 'started_at_utc' not in row for row in summary['results']))
            summary_bytes = (harness.segment_root('ar3') / 'attempt-recovery.json').read_bytes()
            events_before = harness.events()
            again = harness.ar_run(tree, 'ar3-again', successor, recovery_preview=reconciled2['recovery_preview_path'])
            self.assertEqual(again['returncode'], 0, again)
            self.assertEqual(harness.events(), events_before)
            self.assertEqual((harness.segment_root('ar3') / 'attempt-recovery.json').read_bytes(), summary_bytes)
            self.assertEqual(source_files, {path: path.read_bytes() for path in source_files})
            self.assertEqual((harness.campaign_dir() / 'campaign.json').read_bytes(), campaign_bytes)
            self.assertEqual((harness.attempt_root() / 'attempt.json').read_bytes(), original_attempt)
            ledger = harness.summary()
            self.assertEqual([ledger['attempt_recoveries'][f"{state['attempt_id']}:ar{i}"]['status'] for i in (1, 2, 3)],
                             ['failed', 'failed', 'completed'])
            sealed = False
            if chains.CANDIDATE_IDENTITY_AVAILABLE and platform.system() == 'Linux' and os.geteuid() == 0:
                harness.ar_prepare(tree, successor)
                self.assertEqual(harness.ar_seal(tree, 'ar-seal', successor)['returncode'], 0)
                effective = harness.effective_results(1)
                self.assertTrue(all(row.get('recovered_from') == 'ar2' for row in effective['entries']))
                harness.ar_account(tree)
                self.assertEqual(harness.dispatch(tree, 'compare1', ['compare'], baseline=1)['returncode'], 0)
                harness.run(tree, 'gate', '--tag', 'b1')
                accepted = harness.dispatch(tree, 'b1', ['assert', 'accept'], baseline=1,
                    reuse_from=harness.b0_index_path(), authority='anchored', reuse_items=['compare'])
                self.assertEqual(accepted['returncode'], 0)
                harness.assert_accepted_to_completion(1)
                sealed = True
            print(json.dumps({'fixture': 'R11 三段收敛', 'execute_jobs_by_segment': [2, 1, 0],
                'reuse_jobs_by_segment': [0, 1, 2], 'upstream_requests': 0, 'full_seal': sealed}, ensure_ascii=False), flush=True)
            samples = {'reservation': json.loads((harness.segment_root('ar3') / 'recovery-reservation.json').read_text())}
            if sealed:
                samples['effective_results'] = harness.effective_results(1)
            print(json.dumps({'r11_schema_samples': samples}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    unittest.main()
