"""R8：真实 CLI 批准、两账之间 SIGKILL、幂等补齐和原 Campaign 实际派发。

官方证据使用明确的零请求隔离样本；正式预算、批准、admission、COMMIT 和监督器
不打补丁。仅初始预算缩为三分钟，真实等待它到期；故障注入只存在于测试进程。
"""
import argparse
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import evaluation_chain_driver as driver
from tools.official_client_capture.tests import managed_tree_copy as trees
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests import test_codex_upgrade_deadline_extension as fixtures


class DeadlineExtensionChainTests(unittest.TestCase):
    def test_running_command_honors_stage_before_campaign_deadline(self):
        self._runtime_boundary(command=True)

    def test_watchdog_honors_stage_before_campaign_deadline(self):
        self._runtime_boundary(command=False)

    def _runtime_boundary(self, *, command):
        fixture = fixtures.DeadlineExtensionTests()
        fixture.initial_age_seconds = 54
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        client = supervisor.SupervisorClient(fixture.root/'runtime-parent',campaign_id='r8-campaign',phase='VC-0',
            deadline_at_epoch=time.time()+120,heartbeat_seconds=.2,watchdog_timeout_seconds=3,
            ledger_interval_seconds=.2,terminate_owner=False,campaign_dir=fixture.campaign)
        client.start()
        run_dir = client.run_dir
        started = time.monotonic()
        try:
            if command:
                marker=fixture.root/'must-not-finish.txt'
                with self.assertRaises(supervisor.SupervisorError):
                    client.run_command([sys.executable,'-c',
                        'import time,sys; from pathlib import Path; time.sleep(20); Path(sys.argv[1]).write_text("late")',str(marker)],
                        operation='test:stage-boundary',timeout_seconds=30)
                self.assertFalse(marker.exists())
            else:
                while supervisor._read_state(run_dir)['state']=='running' and time.monotonic()-started < 12:
                    client.heartbeat('test:stage-boundary',force=True)
                    time.sleep(.1)
                self.assertEqual(supervisor._read_state(run_dir)['state'],'watchdog-aborted')
                stop=json.loads((run_dir/'stop-receipt.json').read_text())
                self.assertEqual(stop['reason'],'global-wall-clock-deadline-expired')
            self.assertLess(time.monotonic()-started,12)
            self.assertGreater(supervisor._read_state(run_dir)['deadline_at_epoch'],time.time()+90)
            self.assertEqual(timing.inspect_ledger(fixture.ledger)['status'],'deadline_paused')
            self.assertFalse(project.replay_head(fixture.project)['terminal_campaigns'])
        finally:
            client.stop(reason='隔离预算边界验收收口',status='failed')

    def test_cli_sigkill_extension_resumes_original_campaign(self):
        tree = Path(upgrade.__file__).resolve().parents[2]
        result = trees.run_python(tree, ['-m', 'unittest', '-v',
            __name__ + '.DeadlineExtensionChainTests._exercise_cli_sigkill_extension'], timeout=420,
            extra_env={project_ledger_fixture.FIXTURE_ONLY_ENV:'1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('skipped=', result.stderr)
        print(result.stdout, end='', flush=True)

    def _exercise_cli_sigkill_extension(self):
        case = driver.new_real_chain_case()
        self.addCleanup(case.doCleanups)
        tree = Path(upgrade.__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='r8-chain-') as directory:
            root = Path(directory).resolve() / 'staging'
            root.mkdir(mode=0o700)
            create = timing.create_ledger

            def short_initial_budget(*args, **kwargs):
                # 阶段预算还须容纳正式的 120 秒清理窗口与终态排空，不能缩到两分钟。
                kwargs.update(total_budget_minutes=3, stage_budgets_minutes={phase: 3 for phase in timing.PHASE_ORDER})
                return create(*args, **kwargs)

            with mock.patch.object(timing, 'create_ledger', side_effect=short_initial_budget):
                fixture = case._b0_fixture(root, campaign_id='r8-source')
            source, manifest = fixture['campaign_dir'], fixture['manifest']
            case._write_capture_stage(source, source / 'official-evidence', phase='official',
                identity=manifest['official_identity'], prepare_evidence=driver._prepare_side_evidence(None),
                extra_artifacts=driver._extra_artifacts(False))
            campaign = fixture['data'] / 'evidence/campaigns/r8-original'
            code, stdout, stderr = case._run_main(['reuse-official-evidence', '--predecessor-campaign-dir', str(source),
                '--campaign-dir', str(campaign), '--campaign-id', campaign.name,
                '--codex-account-id', str(manifest['configuration']['codex_account_id'])])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)['live_request_count'], 0)
            state_dir = root / 'supervisor'
            state_dir.mkdir(mode=0o700)
            upgrade._bootstrap_noop_first_batch(campaign, upgrade.load_campaign_manifest(campaign), sequence=2,
                state_dir=state_dir, run_arguments=argparse.Namespace(heartbeat_seconds=.2,
                watchdog_timeout_seconds=5, ledger_interval_seconds=.2))
            ledger = fixture['timing_ledger']
            originals = {path: path.read_bytes() for path in (campaign/'campaign.json', campaign/'campaign.sha256',
                campaign/'control/vc/campaign-plan.json', ledger/'ledger.json', fixture['ledger']/'plan.json')}
            expired_at = datetime.fromisoformat(timing.inspect_ledger(ledger)['total_deadline_at_utc']).timestamp()
            while time.time() <= expired_at:
                time.sleep(min(.25, max(.01, expired_at-time.time()+.01)))
            paused = project.pause_campaign_deadline(campaign)
            self.assertEqual(paused['paused_scopes'], ['campaign'])
            before = project.replay_head(fixture['ledger'])
            before_requests = timing.inspect_ledger(ledger)['total_live_request_count']
            cli = ['-m', 'tools.official_client_capture.codex_upgrade']
            marker = root/'executed.txt'
            action = root/'action-plan.json'
            driver._write(action, {'schema_version':artifacts.VC_ACTION_PLAN_SCHEMA,
                'execute_item_ids':['budget-resumed'], 'reuse_item_ids':[], 'actions':[{
                    'action_id':'budget-resumed','operation':'VC-2:budget-resumed','timeout_seconds':30,
                    'command':[sys.executable,'-c','from pathlib import Path; import sys; Path(sys.argv[1]).write_text("executed once")',str(marker)],
                    'item_ids':['budget-resumed']}]})
            dispatch_args = [*cli, 'compile-and-run-vc-batch','--campaign-dir',str(campaign),
                '--state-dir',str(state_dir),'--phase','VC-2','--sequence','2','--predecessor-checkpoint',
                str(campaign/'control/vc/vc-1-checkpoint.json'),'--action-plan',str(action),
                '--heartbeat-seconds','1','--watchdog-timeout-seconds','5','--ledger-interval-seconds','1']
            # R8：暂停期间消费者走真实命令路径同样被拒：不编译批次、不派发动作、不改两本账。
            ledger_head = timing.inspect_ledger(ledger)['head_sha256']
            blocked = trees.run_python(tree, dispatch_args, timeout=90)
            self.assertNotEqual(blocked.returncode, 0, blocked.stdout+blocked.stderr)
            self.assertIn('暂停', blocked.stdout+blocked.stderr)
            self.assertFalse((campaign/'control/vc/batches/0002-vc-2.json').exists())
            self.assertFalse(marker.exists())
            self.assertEqual(timing.inspect_ledger(ledger)['head_sha256'], ledger_head)
            self.assertEqual(project.replay_head(fixture['ledger'])['head_sha256'], before['head_sha256'])
            preview_result = trees.run_python(tree, [*cli, 'deadline-extend', 'preview', '--campaign-dir', str(campaign),
                '--scope', 'campaign', '--new-deadline-at-utc', (datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),
                '--reason', '隔离验收：真实到期后在原 Campaign 继续'], timeout=90)
            self.assertEqual(preview_result.returncode, 0, preview_result.stdout+preview_result.stderr)
            preview = json.loads(preview_result.stdout)
            apply_args = ['deadline-extend', 'apply', '--campaign-dir', str(campaign), '--preview', preview['preview_path'],
                          '--approve-sha256', preview['review_sha256'], '--approved-by', 'fixture-reviewer']
            launcher = (
                'import os,signal,sys\n'
                'from tools.official_client_capture import codex_upgrade as upgrade\n'
                'def crash(frame,event,arg):\n'
                '    if event=="return" and frame.f_code.co_name=="append_project_event" and frame.f_locals.get("event_type")=="deadline_extended":\n'
                '        os.kill(os.getpid(),signal.SIGKILL)\n'
                '    return crash\n'
                'sys.settrace(crash)\n'
                'raise SystemExit(upgrade.main(sys.argv[1:]))\n'
            )
            killed = trees.run_python(tree, ['-c', launcher, *apply_args], timeout=90)
            self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout+killed.stderr)
            self.assertIn('extension_pending', artifacts.effective_deadlines(campaign)['paused_scopes'])
            with self.assertRaises(project.ProjectLedgerError):
                project.assert_campaign_admitted(campaign, command='seal', require=True)
            for expected in ('appended', 'duplicate'):
                completed = trees.run_python(tree, [*cli, *apply_args], timeout=90)
                self.assertEqual(completed.returncode, 0, completed.stdout+completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual((result['project_event'], result['campaign_event']), ('duplicate', expected))
            after = project.replay_head(fixture['ledger'])
            self.assertEqual((before['precise_total'],before['estimated_total'],before['root_cause_counts']),
                             (after['precise_total'],after['estimated_total'],after['root_cause_counts']))
            self.assertEqual(timing.inspect_ledger(ledger)['total_live_request_count'], before_requests)
            self.assertEqual(sum(row['event_type']=='deadline_extended' for row in project._load_events(fixture['ledger'])), 1)
            self.assertEqual(sum(row['event_type']=='deadline_extended' for row,_ in timing._load_events(ledger)), 1)
            dispatched = trees.run_python(tree, dispatch_args, timeout=90)
            self.assertEqual(dispatched.returncode, 0, dispatched.stdout+dispatched.stderr)
            dispatch_result = json.loads(dispatched.stdout)
            self.assertEqual(marker.read_text(),'executed once')
            batch = json.loads((campaign/'control/vc/batches/0002-vc-2.json').read_text())
            self.assertEqual(batch['original_deadline_at_utc'], json.loads(originals[campaign/'control/vc/campaign-plan.json'])['original_deadline_at_utc'])
            self.assertIn('deadline_extension',batch)
            self.assertEqual(originals,{path:path.read_bytes() for path in originals})
            self.assertFalse(project.replay_head(fixture['ledger'])['terminal_campaigns'])
            print(json.dumps({'fixture':'R8 真实延期与续跑','sigkill_count':1,'project_extension_events':1,
                'campaign_extension_events':1,'dispatched_batch_sequence':2,'executed_actions':1,'upstream_requests':0,
                'same_campaign':True,'original_bytes_unchanged':True,'result':dispatch_result['status']},ensure_ascii=False),flush=True)
            print(json.dumps({'r8_schema_samples':{'extension':json.loads(Path(result['receipt_path']).read_text()),
                'batch':batch,'timing':timing.build_checkpoint(ledger)}},ensure_ascii=False),flush=True)
