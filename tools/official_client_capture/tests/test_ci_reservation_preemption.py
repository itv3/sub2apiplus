"""C-03：真实隔离进程验证预约抢占、重新派发、不可重跑边界与收据完整性。"""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from tools.ci import unit_executor as ue
from tools.official_client_capture.tests.test_ci_unit_executor import _config

REPO = Path(__file__).resolve().parents[3]
EXECUTOR = REPO / 'tools/ci/unit_executor.py'


class ReservationPreemptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.tree = self.root / 'tree'
        self.tree.mkdir()
        self.out = self.root / 'out'
        self.state = self.root / 'state'
        self.process = None
        self.addCleanup(self.cleanup_process)
        self.config = _config(self.root, reservation_grace_seconds=.15, reservation_stop_seconds=.15,
                              orphan_grace_seconds=0)

    def cleanup_process(self):
        if self.state.exists():
            request = ue.Reservation(self.state).current()
            if request:
                ue.release(self.state, request['owner'])
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.communicate(timeout=20)

    def wait_for(self, condition, message):
        deadline = time.monotonic() + 20
        while not condition():
            if self.process is not None and self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                self.fail(message + ': ' + stdout[-2000:] + stderr[-3000:])
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(.02)

    def events(self):
        path = self.out / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def start(self, *, exclusive=False, restartable=True, sleep_rounds=1, sleep_seconds=30,
              gates=False, child=False, exit_code=0, test_mode=False):
        counter = self.root / 'counter'
        ready = self.root / 'ready'
        child_code = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("就绪",flush=True);time.sleep(60)'
        body = ("from pathlib import Path\nimport time,os,sys,subprocess\n"
                f"counter=Path({str(counter)!r});n=int(counter.read_text())+1 if counter.exists() else 1;counter.write_text(str(n))\n"
                "print('尝试',n,flush=True)\n")
        if child:
            body += (f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE,text=True)\n"
                     "assert p.stdout.readline().strip()\n"
                     f"Path({str(self.root / 'child-pid')!r}).write_text(str(p.pid))\n")
        body += f"Path({str(ready)!r}).write_text(str(n))\nif n <= {sleep_rounds}: time.sleep({sleep_seconds})\n"
        if child:
            body += 'p.terminate()\np.kill()\np.wait()\n'
        body += f'raise SystemExit({exit_code})\n'
        script = self.tree / 'runner.py'
        script.write_text(body)
        if test_mode:
            script = self.tree / 'test_fixture.py'
            # 将命令体放在测试方法里；中止第一次测试后，重新运行必须只上报一个正式用例。
            method = body.removesuffix(f'raise SystemExit({exit_code})\n')
            script.write_text('import unittest\nclass FixtureTests(unittest.TestCase):\n    def test_resumed(self):\n'
                              + ''.join('        ' + line + '\n' for line in method.splitlines()))
            command = ['run', '--start', str(self.tree)]
        else:
            unit = {'unit_id': 'fixture', 'argv': [sys.executable, '-B', str(script)], 'cwd': str(self.tree),
                    'cores': 1, 'memory_mb': 128, 'exclusive': exclusive, 'reservation_restartable': restartable}
            payload = {'schema_version': ue.COMMANDS_SCHEMA, 'units': [unit]}
            if gates:
                (self.tree / 'input.txt').write_text('初始输入\n')
                for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=验收', '-c', 'user.email=check@example.invalid',
                             '-c', 'commit.gpgsign=false', 'commit', '-qm', '隔离夹具']):
                    subprocess.run(['git', '-C', str(self.tree), *args], check=True, capture_output=True)
                unit['inputs'] = {'files': [{'category': 'source', 'path': 'input.txt'}], 'head': True}
                payload.update(schema_version=ue.GATES_SCHEMA, gates=[{'gate_id': 'fixture-gate', 'units': ['fixture']}])
            manifest = self.root / 'commands.json'
            manifest.write_text(json.dumps(payload))
            command = ['run-gates' if gates else 'run-commands', '--manifest', str(manifest)]
            if gates:
                command += ['--record-store', str(self.root / 'store')]
        argv = [sys.executable, '-B', str(EXECUTOR), *command, '--config', str(self.config), '--out-dir', str(self.out),
                '--state-dir', str(self.state), '--shared-caches', 'off', '--cores', '1', '--parallel', '1']
        self.process = subprocess.Popen(argv, cwd=self.tree, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_for(ready.exists, '隔离单元没有进入运行阶段')
        return counter, ready

    def acquire(self, name='fixture-batch'):
        grant = ue.acquire(self.state, name, os.getpid(), 12)
        self.assertTrue(grant['granted_by'].startswith('scheduler-'))
        self.assertEqual(next(e for e in reversed(self.events()) if e['event'] == 'reservation-granted')['running'], [])
        return grant

    def complete(self, expected_code=0):
        stdout, stderr = self.process.communicate(timeout=25)
        self.assertEqual(self.process.returncode, expected_code, stdout[-1000:] + stderr[-5000:])
        return json.loads((self.out / 'summary.json').read_text())

    def check_restarted(self, summary, expected=1):
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['diagnostic'], [])
        self.assertEqual(len(summary['units']), 1)
        interruptions = summary['reservation_interruptions']
        self.assertEqual(len(interruptions), expected)
        records = [json.loads(p.read_text()) for p in (self.out / 'units').glob('*.record.json')]
        aborted = [r for r in records if r['kind'] == 'reservation-aborted']
        formal = [r for r in records if r['kind'] == 'formal']
        self.assertEqual((len(aborted), len(formal)), (expected, 1))
        self.assertTrue(formal[0]['passed'])
        self.assertEqual(len(formal[0]['resumed_from']), expected)
        self.assertEqual(len({r['log']['path'] for r in records}), expected + 1)
        for row in aborted:
            self.assertFalse(row['passed'])
            self.assertFalse(row['inheritable'])
            self.assertEqual(row['reservation_interruption']['active_session_members'], [])
            for field in ('unit_id', 'spec_sha256', 'inputs_sha256', 'environment_sha256', 'policy_sha256', 'executor'):
                self.assertEqual(row[field], formal[0][field], field)
            self.assertIn('尝试', Path(row['log']['path']).read_text())

    def test_parallel_unit_is_stopped_requeued_and_partial_run_never_counts_as_failure(self):
        counter, _ = self.start()
        self.acquire()
        time.sleep(.2)
        self.assertEqual(counter.read_text(), '1')
        self.assertEqual(len([e for e in self.events() if e['event'] == 'start']), 1)
        ue.release(self.state, 'fixture-batch')
        summary = self.complete()
        self.check_restarted(summary)
        self.assertEqual(counter.read_text(), '2')

    def test_exclusive_unit_observes_reservation_while_waiting(self):
        self.start(exclusive=True)
        self.acquire()
        ue.release(self.state, 'fixture-batch')
        self.check_restarted(self.complete())

    def test_interrupted_unittest_has_one_complete_formal_result(self):
        self.start(test_mode=True)
        self.acquire()
        ue.release(self.state, 'fixture-batch')
        summary = self.complete()
        self.check_restarted(summary)
        self.assertEqual(summary['reported_tests'], 1)
        self.assertTrue(all(not value for value in summary['full_set'].values()))

    @unittest.skipUnless(sys.platform.startswith('linux'), '会话进程核验仅在 Linux 上提供 /proc 证据')
    def test_term_resistant_session_child_is_gone_before_grant(self):
        self.start(child=True)
        child_pid = int((self.root / 'child-pid').read_text())
        self.acquire()
        path = Path('/proc') / str(child_pid) / 'stat'
        if path.exists():
            self.assertIn(path.read_text().rsplit(')', 1)[-1].split()[0], ('Z', 'X'))
        ue.release(self.state, 'fixture-batch')
        self.check_restarted(self.complete())

    def test_multiple_batches_keep_distinct_attempts_and_receipts(self):
        counter, ready = self.start(sleep_rounds=2)
        for attempt in (1, 2):
            self.wait_for(lambda: ready.read_text() == str(attempt), '没有重新进入原单元')
            self.acquire('batch-' + str(attempt))
            ue.release(self.state, 'batch-' + str(attempt))
        self.check_restarted(self.complete(), expected=2)
        self.assertEqual(counter.read_text(), '3')

    def test_withdrawn_reservation_does_not_interrupt_or_redispatch(self):
        self.config = _config(self.root, reservation_grace_seconds=3, orphan_grace_seconds=0)
        counter, _ = self.start(sleep_seconds=1)
        with self.assertRaises(ue.ExecutorError):
            ue.acquire(self.state, 'withdrawn', os.getpid(), .15)
        summary = self.complete()
        self.assertEqual(summary['reservation_interruptions'], [])
        self.assertEqual(counter.read_text(), '1')
        self.assertFalse(any(e['event'] == 'reservation-granted' for e in self.events()))

    def test_true_failure_during_drain_remains_failed_even_if_diagnostic_runs(self):
        self.config = _config(self.root, reservation_grace_seconds=3, orphan_grace_seconds=0)
        self.start(sleep_seconds=.4, exit_code=7)
        self.acquire()
        ue.release(self.state, 'fixture-batch')
        summary = self.complete(1)
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(summary['units'][0]['exit_code'], 7)
        self.assertEqual(summary['reservation_interruptions'], [])

    def test_command_without_restart_contract_drains_naturally(self):
        counter, _ = self.start(restartable=False, sleep_seconds=.5)
        self.acquire()
        ue.release(self.state, 'fixture-batch')
        self.assertEqual(self.complete()['reservation_interruptions'], [])
        self.assertEqual(counter.read_text(), '1')

    def test_null_grace_restores_old_waiting_behavior(self):
        self.config = _config(self.root, reservation_grace_seconds=None, orphan_grace_seconds=0)
        counter, _ = self.start(sleep_seconds=.5)
        self.acquire()
        ue.release(self.state, 'fixture-batch')
        self.assertEqual(self.complete()['reservation_interruptions'], [])
        self.assertEqual(counter.read_text(), '1')

    def test_gate_ledger_only_lists_successful_final_attempt(self):
        self.start(gates=True)
        self.acquire()
        ue.release(self.state, 'fixture-batch')
        summary = self.complete()
        self.check_restarted(summary)
        self.assertEqual(summary['unit_manifest']['self_check'], 'passed')
        manifest = json.loads((self.out / 'unit-manifest.json').read_text())
        self.assertEqual(manifest['counts']['executed'], 1)
        self.assertEqual(manifest['diagnostic'], [])

    def test_changed_input_after_batch_stops_before_restarting(self):
        counter, _ = self.start(gates=True)
        self.acquire()
        (self.tree / 'input.txt').write_text('批次改变输入\n')
        ue.release(self.state, 'fixture-batch')
        stdout, stderr = self.process.communicate(timeout=20)
        self.assertEqual(self.process.returncode, 2, stdout + stderr)
        self.assertIn('输入漂移', stderr)
        self.assertEqual(counter.read_text(), '1')
        self.assertFalse((self.out / 'unit-manifest.json').exists())

    def test_configuration_rejects_nonfinite_negative_and_boolean_values(self):
        for value in (-1, True, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ue.ExecutorError):
                ue.load_config(_config(self.root, reservation_grace_seconds=value))

    def test_grant_cannot_be_reused_by_a_new_request_with_the_same_owner(self):
        reservation = ue.Reservation(self.state)
        original = {'owner': 'same-owner', 'owner_pid': os.getpid(), 'request_id': 'a' * 32,
                    'requested_at_utc': '2026-10-06T00:00:00Z'}
        reservation.request_path.write_text(json.dumps(original))
        self.assertTrue(reservation.grant('fixture', expected=original))
        self.assertIsNotNone(reservation.granted())
        changed = {**original, 'request_id': 'b' * 32}
        reservation.request_path.write_text(json.dumps(changed))
        self.assertIsNone(reservation.granted())
        self.assertFalse(reservation.grant('fixture', expected=original))

    def test_acquire_assigns_distinct_identity_even_with_same_timestamp_and_owner(self):
        requests = []
        with unittest.mock.patch.object(ue, '_utc_now', return_value='2026-10-06T00:00:00Z'):
            for _ in range(2):
                ue.acquire(self.state, 'same-owner', os.getpid(), 2)
                requests.append(ue.Reservation(self.state).current())
                ue.release(self.state, 'same-owner')
        self.assertNotEqual(requests[0]['request_id'], requests[1]['request_id'])
