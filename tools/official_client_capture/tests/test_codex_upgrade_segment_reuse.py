"""R11 四项复用判据：真实文件、追加式 checkpoint 与权限收口；上游请求为零。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_evidence_permissions as permissions
from tools.official_client_capture import incremental_recovery as incremental


class SegmentReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / 'data'
        self.campaign = self.data / 'evidence/campaigns/campaign'
        self.segment = self.campaign / 'candidates/candidate/attempts/attempt/recovery/ar1'
        self.segment.mkdir(parents=True, mode=0o700)
        self.job_root = self.data / 'runs/job-a-recovery-ar1'
        self.job_root.mkdir(parents=True, mode=0o700)
        self.evidence = self.segment / 'evidence'
        self.evidence.mkdir(mode=0o700)
        self.log = self.segment / 'logs'
        self.log.mkdir(mode=0o700)
        self.write(self.job_root / 'capture.json', {'bytes': 'original'})
        self.after = self.evidence / 'environment/after/probe-manifest.json'
        self.restoration = self.evidence / 'receipts/restoration-report.json'
        self.write(self.after, {'status': 'passed'})
        self.write(self.restoration, {'status': 'restored'})
        self.reservation = {
            'schema_version': upgrade.ATTEMPT_RECOVERY_RESERVATION_SCHEMA,
            'candidate_id': 'candidate', 'attempt_id': 'attempt', 'recovery_revision': 'ar1',
            'run_nonce': 'nonce', 'planned_jobs': [
                {'id': 'job-a', 'execution_sha256': 'a' * 64, 'source_execution_sha256': 'b' * 64},
                {'id': 'job-b', 'execution_sha256': 'c' * 64, 'source_execution_sha256': 'd' * 64}],
        }
        self.reservation['reservation_digest'] = upgrade._fingerprint(self.reservation)
        self.write(self.segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME, self.reservation)
        self.result = {'id': 'job-a', 'status': 'complete', 'execution_sha256': 'a' * 64,
                       'evidence_roots': [str(self.job_root)], 'disposition': 'executed'}
        self.write(self.segment / 'job-job-a.json', self.result)
        self.store = incremental.CheckpointStore(self.segment / 'checkpoints')
        self.store.append({'item_id': 'job-a', 'status': 'complete', 'result': self.result,
                           'run_nonce': 'nonce', 'attempt_id': 'attempt.ar1', 'phase': 'candidate',
                           'result_sha256': incremental.digest(self.result),
                           'recovery_evidence': reconciler.recovery_job_inventory([str(self.job_root)])})
        path, _ = permissions.close_evidence_permissions(
            self.segment, [self.job_root, self.evidence, self.log], managed_data_root=self.data,
            logical_runs_roots=(self.data / 'runs',),
        )
        self.binding = permissions.receipt_binding(self.segment, path)
        self.summary = {
            'schema_version': upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA, 'candidate_id': 'candidate',
            'attempt_id': 'attempt', 'recovery_revision': 'ar1', 'run_nonce': 'nonce', 'status': 'failed',
            'reservation': {'sha256': upgrade.file_sha256(self.segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME)},
            'results': [self.result], 'restoration_error': None, 'evidence_permission_closeout': self.binding,
            'environment': {'after_probe': upgrade._attempt_evidence_binding(self.evidence, self.after),
                            'restoration_report': upgrade._attempt_evidence_binding(self.evidence, self.restoration)},
        }
        self.save_summary()
        for patch in (
            mock.patch.object(upgrade, '_authoritative_recovery_execute_jobs', return_value=(1, {}, {'execute_jobs': ['job-a', 'job-b']})),
            mock.patch.object(upgrade, '_load_capture_reservation', return_value={'planned_jobs': [
                {'id': 'job-a', 'execution_sha256': 'b' * 64}, {'id': 'job-b', 'execution_sha256': 'd' * 64}]}),
            mock.patch.object(upgrade, '_replay_evidence_permission_closeout', side_effect=lambda segment, roots, binding:
                permissions.replay_evidence_permission_closeout(segment, roots, binding, managed_data_root=self.data,
                                                               logical_runs_roots=(self.data / 'runs',))),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_text(json.dumps(value) + '\n')
        path.chmod(0o600)

    def save_summary(self):
        self.summary.pop('attempt_recovery_digest', None)
        self.summary['attempt_recovery_digest'] = upgrade._fingerprint(self.summary)
        self.write(self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME, self.summary)

    def proofs(self):
        return reconciler.segment_reuse_proofs(self.campaign, 'candidate', 'attempt', 'ar1', ['job-a', 'job-b'])

    def preview(self):
        jobs = {'planned_job_ids': ['job-a', 'job-b'], 'groups': {
            'complete': ['job-a'], 'failed': ['job-b'], 'pending': [], 'indeterminate': []}}
        return reconciler._recovery_preview(self.campaign, self.campaign / 'control/reconciliation/attempt-attempt-ar1',
            manifest={'campaign_id': 'campaign'}, attempt_id='attempt', phase='candidate', candidate_id='candidate',
            attempt_exists=True, jobs=jobs, environment_status='restored', provenance_copy={'jobs': [
                {'job_id': 'job-a', 'precise_count': 2}, {'job_id': 'job-b', 'precise_count': 3}]}, current={},
            reconciliation_receipt_sha256='e' * 64, campaign_ledger_head={}, project_ledger_head={},
            now='2026-09-24T00:00:00Z', recovery_revision='ar1', recovery_execute_jobs=['job-a', 'job-b'])

    def test_preview_reuses_only_proven_complete_and_estimates_execute(self):
        preview = self.preview()
        self.assertEqual(preview['reuse_job_ids'], ['job-a'])
        self.assertEqual(preview['execute_job_ids'], ['job-b'])
        self.assertEqual(preview['expected_new_requests']['known_total'], 3)
        self.assertGreater(preview['scanned_bytes'], 0)
        self.assertIsNone(supervisor.recovery_preview_scope_violation(preview, ['job-a', 'job-b']))
        reconciler.validate_segment_reuse_preview(self.campaign, preview)

    def test_same_size_content_drift_with_restored_mtime_is_not_reusable(self):
        path = self.job_root / 'capture.json'
        before = path.stat()
        original = path.read_bytes()
        path.write_bytes(original.replace(b'original', b'changed!'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.proofs(), {})
        self.assertEqual(self.preview()['execute_job_ids'], ['job-a', 'job-b'])

    def test_missing_after_or_restoration_error_never_reuses(self):
        self.summary['restoration_error'] = {'type': 'failed'}
        self.save_summary()
        self.assertEqual(self.proofs(), {})
        self.summary['restoration_error'] = None
        self.save_summary()
        self.after.unlink()
        self.assertEqual(self.proofs(), {})

    def test_source_execution_drift_never_reuses(self):
        with mock.patch.object(upgrade, '_load_capture_reservation', return_value={'planned_jobs': [
            {'id': 'job-a', 'execution_sha256': 'f' * 64}]}):
            self.assertEqual(self.proofs(), {})

    def test_checkpoint_result_mismatch_never_reuses(self):
        self.result['extra'] = 'drift'
        self.write(self.segment / 'job-job-a.json', self.result)
        self.summary['results'] = [self.result]
        self.save_summary()
        self.assertEqual(self.proofs(), {})

    def test_missing_summary_after_closeout_still_has_sufficient_proof(self):
        (self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME).unlink()
        self.assertEqual(set(self.proofs()), {'job-a'})

    def test_historical_checkpoint_without_inventory_stays_readable_and_executes(self):
        path = self.segment / 'checkpoints/00000001.json'
        record = json.loads(path.read_text())
        record.pop('recovery_evidence')
        record.pop('checkpoint_sha256')
        record['checkpoint_sha256'] = incremental.digest(record)
        self.write(path, record)
        self.assertEqual(len(self.store.records()), 1)
        self.assertEqual(self.proofs(), {})
        self.assertEqual(self.preview()['execute_job_ids'], ['job-a', 'job-b'])

    def test_checkpoint_from_another_reservation_cannot_prove_completion(self):
        path = self.segment / 'checkpoints/00000001.json'
        record = json.loads(path.read_text())
        record['run_nonce'] = 'different'
        record.pop('checkpoint_sha256')
        record['checkpoint_sha256'] = incremental.digest(record)
        self.write(path, record)
        self.assertEqual(self.proofs(), {})

    def test_scope_and_forged_proof_are_rejected(self):
        preview = self.preview()
        self.assertIsNotNone(supervisor.recovery_preview_scope_violation(
            {**preview, 'execute_job_ids': ['job-a', 'job-b']}, ['job-a', 'job-b']))
        forged = json.loads(json.dumps(preview))
        forged['reuse_proofs']['job-a']['content_sha256'] = '0' * 64
        with self.assertRaisesRegex(reconciler.ReconcilerError, '判据漂移'):
            reconciler.validate_segment_reuse_preview(self.campaign, forged)


if __name__ == '__main__':
    unittest.main()
