"""R11 四项复用判据：真实文件、追加式 checkpoint 与权限收口；上游请求为零。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_environment_probe as probe
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_evidence_permissions as permissions
from tools.official_client_capture import incremental_recovery as incremental
from tools.official_client_capture.tests import test_codex_upgrade as upgrade_tests


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
        self.write_environment()
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

    def write_environment(self):
        """按真实探针与恢复 finalizer 的布局写前后快照、after 探针清单与机器恢复报告（权限收口之前）。"""

        environment = self.evidence / 'environment'
        snapshots = []
        for phase in ('before', 'after'):
            (environment / phase).mkdir(parents=True, mode=0o700, exist_ok=True)
        environment.chmod(0o700)
        for kind, filename in probe.STATE_FILES.items():
            for phase in ('before', 'after'):
                state = (upgrade_tests.CodexUpgradeTest._database_state(after=phase == 'after') if kind == 'database'
                         else {'probe_kind': f'segment_reuse_{kind}', 'stable_value': 'restored'})
                upgrade_tests.CodexUpgradeTest._write_state_snapshot(environment / phase / filename, state)
            path = environment / 'after' / filename
            snapshots.append(probe._snapshot_binding(path, path.read_bytes(), kind))
        self.write(self.after, {'schema_version': probe.PROBE_MANIFEST_SCHEMA, 'phase': 'after',
                                'observed_at_utc': '2026-09-24T00:00:00Z', 'snapshots': snapshots})
        upgrade._finalize_attempt_restoration(self.evidence, phase='candidate', candidate_id='candidate')

    def rewrite_in_place(self, path, old, new):
        """同大小改写并恢复 mtime：权限收口边界（inode／大小／mtime）不变，只能靠内容摘要发现。"""

        before = path.stat()
        original = path.read_bytes()
        changed = original.replace(old, new, 1)
        self.assertEqual(len(changed), len(original))
        self.assertNotEqual(changed, original)
        path.write_bytes(changed)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

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

    def test_missing_summary_rejects_after_snapshot_rewritten_in_place(self):
        """判据④：摘要缺失时 after 快照被同大小改写，机器恢复报告按原输入重放不一致，不复用。"""

        (self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME).unlink()
        self.rewrite_in_place(self.evidence / 'environment/after/service-state.json', b'restored', b'drifted!')
        self.assertEqual(self.proofs(), {})

    def test_missing_summary_rejects_consistently_rewritten_probe_manifest(self):
        """快照与探针清单一起改写（清单自洽、边界元数据不变），恢复报告仍绑定原内容，拒绝复用。"""

        (self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME).unlink()
        snapshot = self.evidence / 'environment/after/account-state.json'
        old_digest = upgrade.file_sha256(snapshot).encode()
        self.rewrite_in_place(snapshot, b'restored', b'drifted!')
        self.rewrite_in_place(self.after, old_digest, upgrade.file_sha256(snapshot).encode())
        self.assertEqual(self.proofs(), {})

    def test_missing_summary_rejects_probe_listing_that_differs_from_report(self):
        """探针清单少列或错列快照时，与恢复报告的 after 引用不能逐项对应，拒绝复用。"""

        (self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME).unlink()
        self.rewrite_in_place(self.after, b'"kind": "account"', b'"kind": "acc0unt"')
        self.assertEqual(self.proofs(), {})

    def test_summary_path_rejects_after_probe_rewritten_in_place(self):
        """摘要在场时 after 探针同大小改写，摘要绑定的内容摘要不符，不复用。"""

        self.rewrite_in_place(self.after, b'2026-09-24T00:00:00Z', b'2026-09-24T00:00:01Z')
        self.assertEqual(self.proofs(), {})

    def publish_job_b(self, root):
        """补写 job-b 的完成结果与 checkpoint：逐文件清单按实物冻结，其余条件与 job-a 相同。"""

        result = {'id': 'job-b', 'status': 'complete', 'execution_sha256': 'c' * 64,
                  'evidence_roots': [str(root)], 'disposition': 'executed'}
        self.write(self.segment / 'job-job-b.json', result)
        self.store.append({'item_id': 'job-b', 'status': 'complete', 'result': result,
                           'run_nonce': 'nonce', 'attempt_id': 'attempt.ar1', 'phase': 'candidate',
                           'result_sha256': incremental.digest(result),
                           'recovery_evidence': reconciler.recovery_job_inventory([str(root)]),
                           'previous_checkpoint_sha256': self.store.records()[-1]['checkpoint_sha256']})
        self.summary['results'] = [self.result, result]
        self.save_summary()

    def test_digest_consistent_root_inside_permission_boundary_is_reusable(self):
        """对照组：job-b 的证据根在权限收口边界内，四项判据齐全，可复用。"""

        self.publish_job_b(self.job_root)
        self.assertEqual(set(self.proofs()), {'job-a', 'job-b'})

    def test_digest_consistent_root_outside_permission_boundary_is_not_reusable(self):
        """证据根不在权限收口边界内：即使清单摘要与实物一致也不复用（与对照组只差证据根位置）。"""

        outside = self.data / 'runs/job-b-outside'
        outside.mkdir(mode=0o700)
        self.write(outside / 'capture.json', {'bytes': 'original'})
        self.publish_job_b(outside)
        self.assertEqual(set(self.proofs()), {'job-a'})

    def test_symlink_in_evidence_root_is_not_reusable(self):
        """证据根内出现软链接：逐文件清单拒绝非普通条目，复验不复用。"""

        (self.job_root / 'link.json').symlink_to(self.job_root / 'capture.json')
        with self.assertRaisesRegex(reconciler.ReconcilerError, '非普通条目'):
            reconciler.recovery_job_inventory([str(self.job_root)])
        self.assertEqual(self.proofs(), {})

    def test_revalidation_reports_scanned_bytes_and_reads_each_root_once(self):
        """复验返回实际读取的证据字节并累加进调用方统计；同一次复验内同一证据根只读一次。"""

        preview = self.preview()
        expected = sum(path.stat().st_size for path in self.job_root.rglob('*') if path.is_file())
        stats = {'scanned_bytes': 7}
        self.assertEqual(reconciler.validate_segment_reuse_preview(self.campaign, preview, scan_stats=stats), expected)
        self.assertEqual(stats['scanned_bytes'], 7 + expected)
        empty = {**preview, 'reuse_job_ids': [], 'reuse_proofs': {}}
        self.assertEqual(reconciler.validate_segment_reuse_preview(self.campaign, empty), 0)
        memo, counted = {}, {'scanned_bytes': 0}
        first = reconciler.segment_reuse_proofs(self.campaign, 'candidate', 'attempt', 'ar1', ['job-a', 'job-b'],
                                                scan_stats=counted, _inventory_memo=memo)
        self.assertEqual(counted['scanned_bytes'], expected)
        again = reconciler.segment_reuse_proofs(self.campaign, 'candidate', 'attempt', 'ar1', ['job-a', 'job-b'],
                                                scan_stats=counted, _inventory_memo=memo)
        self.assertEqual((again, counted['scanned_bytes']), (first, expected))

    def accounting_check(self):
        reconciler._require_segment_accounting_scope(
            self.campaign, self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME,
            candidate_id='candidate', attempt_id='attempt', recovery_revision='ar1')

    def rebind_reservation(self, reservation):
        reservation = dict(reservation)
        reservation.pop('reservation_digest', None)
        reservation['reservation_digest'] = upgrade._fingerprint(reservation)
        self.write(self.segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME, reservation)
        self.summary['reservation'] = {
            'sha256': upgrade.file_sha256(self.segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME)}
        self.save_summary()

    def test_segment_accounting_requires_scope_matching_frozen_jobs_and_results(self):
        """恢复段入账：执行／复用集合不相交、并集等于冻结 J*，且段结果的执行／复用标记与预约逐项一致。"""

        with self.assertRaisesRegex(reconciler.ReconcilerError, '执行／复用标记'):
            self.accounting_check()  # 历史预约按全部执行核对，结果缺 job-b
        job_b = {'id': 'job-b', 'status': 'complete', 'execution_sha256': 'c' * 64, 'evidence_roots': [],
                 'disposition': 'executed'}
        self.summary['results'] = [self.result, job_b]
        self.save_summary()
        self.accounting_check()
        proofs = {'job-a': {'recovered_from': 'ar0'}}
        reuse = {**self.reservation, 'execute_job_ids': ['job-b'], 'reuse_job_ids': ['job-a'], 'reuse_proofs': proofs,
                 'reuse_proofs_sha256': upgrade._fingerprint(proofs), 'recovery_review_sha256': 'e' * 64}
        self.rebind_reservation(reuse)
        with self.assertRaisesRegex(reconciler.ReconcilerError, '执行／复用标记'):
            self.accounting_check()  # 结果仍把复用的 job-a 记成本段执行
        self.summary['results'] = [{**self.result, 'disposition': 'reused'}, job_b]
        self.save_summary()
        self.accounting_check()
        self.rebind_reservation({**reuse, 'execute_job_ids': ['job-a', 'job-b']})
        with self.assertRaisesRegex(reconciler.ReconcilerError, '无法取得预约'):
            self.accounting_check()  # 执行与复用重叠，预约读取即拒绝
        self.rebind_reservation(reuse)
        self.summary['reservation'] = {'sha256': '0' * 64}
        self.save_summary()
        with self.assertRaisesRegex(reconciler.ReconcilerError, '摘要与预约绑定'):
            self.accounting_check()

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
