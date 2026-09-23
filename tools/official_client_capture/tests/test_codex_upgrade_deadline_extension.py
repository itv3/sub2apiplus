"""R8 三层预算、批准闭集、两账中断补齐与历史回放；全部使用隔离零请求文件。"""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor


class DeadlineExtensionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'staging'
        self.root.mkdir(mode=0o700)
        self.project = self.root / project.LEDGER_DIR_NAME
        self.start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=getattr(self, 'initial_age_seconds', 10))
        project.create_project_ledger(self.project, project_id='r8-fixture',
            absolute_deadline_utc=self.at(600), deadline_approved_by='fixture',
            estimation_policy='none', estimation_policy_approved_by='fixture', fixture_only=True,
            started_at_utc=self.at(0), initial_precise_count=7)
        self.campaign = self.root / 'evidence/campaigns/r8-campaign'
        self.campaign.mkdir(parents=True, mode=0o700)
        self.ledger = self.root / 'timing'
        timing.create_ledger(self.ledger, upgrade_id='r8-campaign', baseline_version='0.154.0',
            target_version='0.156.1', campaign_purpose='validation_only', evidence_decision='recapture',
            started_at_utc=self.at(0), total_budget_minutes=5,
            stage_budgets_minutes={phase: 1 for phase in timing.PHASE_ORDER}, project_ledger_dir=self.project)
        self.write(self.campaign / 'campaign.json', {'campaign_id':'r8-campaign', 'campaign_mode':'formal',
            'target_version':'0.156.1', 'control_receipts':{'upgrade_timing':{'ledger_dir':str(self.ledger),
            'ledger_plan_sha256':hashlib.sha256((self.ledger/'ledger.json').read_bytes()).hexdigest()}}})
        self.write(self.campaign / 'control/vc/campaign-plan.json',
                   {'campaign_id':'r8-campaign','original_deadline_at_utc':self.at(300)})
        project.register_existing_campaign(self.campaign)
        self.original = {path:path.read_bytes() for path in (self.campaign/'campaign.json',
            self.campaign/'control/vc/campaign-plan.json', self.ledger/'ledger.json', self.project/'plan.json')}

    def at(self, seconds):
        return (self.start + timedelta(seconds=seconds)).isoformat()

    def moment(self, seconds):
        return self.start + timedelta(seconds=seconds)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for parent in path.parents:
            if parent == self.root:
                break
            parent.chmod(0o700)
        path.write_text(json.dumps(value)+'\n')
        path.chmod(0o600)

    def preview(self, scope='stage', *, now=91, new=1000):
        return project.preview_deadline_extension(self.campaign, scope=scope, phase='VC-0' if scope=='stage' else None,
            new_deadline_at_utc=self.at(new), reason='隔离测试中继续原证据链', now=self.moment(now))

    def apply(self, preview, *, now=92):
        return project.apply_deadline_extension(self.campaign, preview_path=Path(preview['preview_path']),
            approve_sha256=preview['review_sha256'], approved_by='fixture-reviewer', now=self.moment(now))

    def test_stage_pause_extend_preserves_counters_and_original_bytes(self):
        paused = project.pause_campaign_deadline(self.campaign, now=self.moment(90))
        self.assertEqual(paused['status'], 'deadline_paused')
        before = timing.inspect_ledger(self.ledger, now=self.at(92))
        approved = self.apply(self.preview())
        after = timing.inspect_ledger(self.ledger, now=self.at(92))
        self.assertEqual(after['status'], 'active')
        self.assertEqual(after['total_elapsed_seconds'], before['total_elapsed_seconds'])
        self.assertEqual(after['total_live_request_count'], before['total_live_request_count'])
        self.assertEqual(approved['effective_deadlines']['stage_deadline_at_utc'], self.at(1000))
        self.assertEqual(project.replay_head(self.project)['precise_total'], 7)
        self.assertEqual(self.original, {path:path.read_bytes() for path in self.original})

    def test_all_three_scopes_pause_and_extend_independently(self):
        paused = project.pause_campaign_deadline(self.campaign, now=self.moment(650))
        self.assertEqual(paused['paused_scopes'], ['campaign','project','stage'])
        self.assertNotIn('r8-campaign', project.replay_head(self.project)['terminal_campaigns'])
        for index, scope in enumerate(('project','campaign','stage')):
            preview = self.preview(scope, now=651+index*2, new=2000+index*100)
            result = self.apply(preview, now=652+index*2)
            self.assertEqual(len(result['effective_deadlines']['paused_scopes']), 2-index)
        self.assertEqual(timing.inspect_ledger(self.ledger, now=self.at(656))['status'], 'active')
        self.assertEqual(project.replay_head(self.project)['precise_total'], 7)

    def test_72_hours_only_reminds_and_does_not_make_terminal(self):
        project.pause_campaign_deadline(self.campaign, now=self.moment(90))
        state = artifacts.effective_deadlines(self.campaign, now=self.moment(73*3600))
        self.assertTrue(state['review_reminder'])
        self.assertGreaterEqual(state['paused_hours'], 72)
        self.assertFalse(project.replay_head(self.project)['terminal_campaigns'])

    def test_paused_consumers_all_reject(self):
        project.pause_campaign_deadline(self.campaign, now=self.moment(90))
        for command in ('campaign-run','resume','reuse-official-evidence','seal'):
            with self.subTest(command=command), self.assertRaisesRegex(project.ProjectLedgerError, '暂停'):
                project.assert_campaign_admitted(self.campaign, command=command, require=True, now=self.moment(91))

    def test_missing_approval_and_stale_project_head_reject(self):
        preview = self.preview()
        with self.assertRaisesRegex(project.ProjectLedgerError, '批准摘要'):
            project.apply_deadline_extension(self.campaign, preview_path=Path(preview['preview_path']),
                approve_sha256='0'*64, approved_by='fixture', now=self.moment(92))
        project.pause_campaign_deadline(self.campaign, now=self.moment(92))
        with self.assertRaisesRegex(project.ProjectLedgerError, 'head 已过期'):
            self.apply(preview, now=93)

    def test_crash_between_ledgers_recovers_once_and_stays_closed(self):
        project.pause_campaign_deadline(self.campaign, now=self.moment(90))
        preview = self.preview()
        with mock.patch.object(timing, 'append_event', side_effect=RuntimeError('模拟总账后进程中断')):
            with self.assertRaisesRegex(RuntimeError, '进程中断'):
                self.apply(preview)
        partial = artifacts.effective_deadlines(self.campaign, now=self.moment(92))
        self.assertIn('extension_pending', partial['paused_scopes'])
        with self.assertRaises(project.ProjectLedgerError):
            project.assert_campaign_admitted(self.campaign, command='seal', require=True, now=self.moment(92))
        first = self.apply(preview, now=93)
        second = self.apply(preview, now=94)
        self.assertEqual((first['project_event'], first['campaign_event']), ('duplicate','appended'))
        self.assertEqual((second['project_event'], second['campaign_event']), ('duplicate','duplicate'))
        events = timing._load_events(self.ledger)
        self.assertEqual(sum(event['event_type']=='deadline_extended' for event,_ in events), 1)
        self.assertFalse(artifacts.effective_deadlines(self.campaign, now=self.moment(94))['paused_scopes'])

    def test_unapproved_standalone_timing_extension_rejects(self):
        preview = self.preview()
        document = json.loads(Path(preview['preview_path']).read_text())
        document.update(schema_version=artifacts.DEADLINE_EXTENSION_SCHEMA, approved_by='fixture', approved_at_utc=self.at(92))
        document['receipt_sha256'] = artifacts.digest(document)
        with self.assertRaisesRegex(project.ProjectLedgerError, '尚未进入项目总账'):
            timing.append_event(self.ledger, event_id='forged-extension', phase='VC-0', event_type='deadline_extended',
                deadline_control=document, recorded_at_utc=self.at(92))

    def test_explicit_abandon_is_only_budget_terminal_and_is_idempotent(self):
        project.pause_campaign_deadline(self.campaign, now=self.moment(90))
        first = project.abandon_campaign(self.campaign, approved_by='fixture', reason='测试明确放弃', now=self.moment(91))
        second = project.abandon_campaign(self.campaign, approved_by='fixture', reason='测试明确放弃', now=self.moment(92))
        self.assertEqual((first['project_event'], second['project_event']), ('appended','duplicate'))
        self.assertEqual(timing.inspect_ledger(self.ledger, now=self.at(92))['status'], 'abandoned')
        self.assertEqual(project.replay_head(self.project)['terminal_campaigns']['r8-campaign']['terminal_reason'], 'operator_abandoned')
        with self.assertRaisesRegex(project.ProjectLedgerError, '历史回放'):
            project.append_project_event(self.project, operation_id='forbidden-timeout', event_type='campaign_terminal',
                payload={'campaign_id':'r8-campaign','terminal_reason':'deadline_wall_clock'}, source_batch_sha256=None)

    def test_old_checkpoint_retains_historical_expiry_semantics(self):
        receipt = timing.build_checkpoint(self.ledger, observed_at_utc=self.at(90))
        for key in ('status_before_pause','paused_scopes','paused_since_utc','paused_hours','review_reminder',
                    'original_total_deadline_at_utc','deadline_extensions'):
            receipt['summary'].pop(key)
        receipt['summary']['status']='stop_required'
        timing._write_once(self.ledger/'receipts/historical-expired.json',receipt)
        self.assertEqual(timing.replay(self.ledger,'receipts/historical-expired.json'), receipt)
        state = artifacts.effective_deadlines(self.campaign, now=self.moment(30))
        self.assertEqual(state['total_deadline_at_utc'], self.at(300))
        self.assertEqual(state['project_deadline_at_utc'], self.at(600))

    def test_abandon_interrupted_between_ledgers_still_closes_consumers(self):
        with mock.patch.object(project, 'append_project_event', side_effect=RuntimeError('放弃总账前中断')):
            with self.assertRaisesRegex(RuntimeError, '总账前中断'):
                project.abandon_campaign(self.campaign, approved_by='fixture', reason='明确放弃', now=self.moment(20))
        self.assertFalse(project.replay_head(self.project)['terminal_campaigns'])
        plan,_ = project._load_plan(self.project)
        with self.assertRaisesRegex(reconciler.ReconcilerError, '显式放弃'):
            reconciler._decide(head=project.replay_head(self.project), plan=plan,
                ledger=timing.inspect_ledger(self.ledger, now=self.at(21)), identity={'unchanged':True},
                environment_status='restored', campaign_deadline_at_utc=self.at(300),
                root_cause_id='fixture', request_status='resolved', now=self.at(21))
        for command in ('campaign-run', 'resume', 'reuse-official-evidence', 'seal'):
            with self.subTest(command=command), self.assertRaisesRegex(project.ProjectLedgerError, '显式放弃'):
                project.assert_campaign_admitted(self.campaign, command=command, require=True, now=self.moment(21))
        with mock.patch.object(upgrade, 'campaign_status', return_value={'status':'active', 'project_ledger':{}}), \
             mock.patch.object(timing, '_utc_now', return_value=self.at(21)), \
             mock.patch.object(artifacts, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.moment(21)
            self.assertEqual(upgrade._campaign_status_with_deadlines(self.campaign, None)['status'], 'abandoned')
        for expected in ('appended', 'duplicate'):
            result = project.abandon_campaign(self.campaign, approved_by='fixture', reason='明确放弃', now=self.moment(22))
            self.assertEqual(result['project_event'], expected)
        self.assertEqual(sum(row['event_type']=='campaign_abandoned' for row,_ in timing._load_events(self.ledger)), 1)

    def test_old_checkpoint_with_microseconds_retains_second_precision_projection(self):
        historical = self.root/'historical-timing'
        timing.create_ledger(historical, upgrade_id='historical', baseline_version='0.154.0', target_version='0.156.1',
            campaign_purpose='validation_only', evidence_decision='recapture', started_at_utc=self.at(.123456),
            total_budget_minutes=5, stage_budgets_minutes={phase:1 for phase in timing.PHASE_ORDER})
        receipt = timing.build_checkpoint(historical, observed_at_utc=self.at(30))
        for key in ('status_before_pause','paused_scopes','paused_since_utc','paused_hours','review_reminder',
                    'original_total_deadline_at_utc','deadline_extensions'):
            receipt['summary'].pop(key)
        for key in ('total_deadline_at_utc', 'stage_deadline_at_utc'):
            receipt['summary'][key] = datetime.fromisoformat(receipt['summary'][key]).replace(microsecond=0).isoformat()
        timing._write_once(historical/'receipts/historical-microseconds.json', receipt)
        self.assertEqual(timing.replay(historical, 'receipts/historical-microseconds.json'), receipt)

    def test_decision_pauses_deadline_but_integrity_still_stops(self):
        head=project.replay_head(self.project)
        plan,_=project._load_plan(self.project)
        kwargs={'head':head,'plan':plan,'ledger':timing.inspect_ledger(self.ledger,now=self.at(90)),
            'identity':{'unchanged':True},'environment_status':'restored','campaign_deadline_at_utc':self.at(300),
            'root_cause_id':'fixture','request_status':'resolved','now':self.at(90)}
        self.assertEqual(reconciler._decide(**kwargs)['decision'], 'paused')
        decision=reconciler._decide(**kwargs,forced_terminal_reason='integrity_mismatch')
        self.assertEqual((decision['decision'], decision['terminal_reason']), ('permanent_stop','integrity_mismatch'))

    def test_partial_extension_resets_only_review_reminder(self):
        project.pause_campaign_deadline(self.campaign, now=self.moment(73*3600))
        preview = self.preview('project', now=73*3600+1, new=100*3600)
        self.apply(preview, now=73*3600+2)
        effective = artifacts.effective_deadlines(self.campaign, now=self.moment(73*3600+3))
        summary = timing.inspect_ledger(self.ledger, now=self.at(73*3600+3))
        self.assertGreater(effective['paused_hours'], 72)
        self.assertFalse(effective['review_reminder'])
        self.assertFalse(summary['review_reminder'])
        self.assertEqual(effective['paused_scopes'], ['campaign', 'stage'])

    def test_partial_extension_retains_pause_start_and_full_extension_resets_it(self):
        project.pause_campaign_deadline(self.campaign, now=self.moment(650))
        for index, scope in enumerate(('stage', 'campaign', 'project')):
            self.apply(self.preview(scope, now=651+index*2, new=2000+index*100), now=652+index*2)
            effective = artifacts.effective_deadlines(self.campaign, now=self.moment(653+index*2))
            summary = timing.inspect_ledger(self.ledger, now=self.at(653+index*2))
            for field in ('paused_scopes', 'paused_since_utc', 'paused_hours', 'review_reminder'):
                self.assertEqual(effective[field], summary[field], field)
            self.assertEqual(summary['paused_since_utc'], self.at(60) if index < 2 else None)
        later = timing.inspect_ledger(self.ledger, now=self.at(2050))
        self.assertEqual(later['paused_since_utc'], self.at(2000))

    def test_running_parent_blocks_extension_and_abandon(self):
        with project.deadline_control_scope(self.campaign, executing=True):
            with self.assertRaisesRegex(project.ProjectLedgerError, '仍在执行'):
                self.preview()
            with self.assertRaisesRegex(project.ProjectLedgerError, '仍在执行'):
                project.abandon_campaign(self.campaign, approved_by='fixture', reason='并发必须拒绝')

    def test_project_extension_pending_closes_other_campaign(self):
        other = self.campaign.with_name('other-campaign')
        other_ledger = self.root/'other-timing'
        timing.create_ledger(other_ledger, upgrade_id='other-campaign', baseline_version='0.154.0',
            target_version='0.156.1', campaign_purpose='validation_only', evidence_decision='recapture',
            started_at_utc=self.at(0), total_budget_minutes=5, project_ledger_dir=self.project,
            stage_budgets_minutes={phase:1 for phase in timing.PHASE_ORDER})
        self.write(other/'campaign.json', {'campaign_id':'other-campaign','campaign_mode':'formal','target_version':'0.156.1',
            'control_receipts':{'upgrade_timing':{'ledger_dir':str(other_ledger)}}})
        self.write(other/'control/vc/campaign-plan.json', {'campaign_id':'other-campaign','original_deadline_at_utc':self.at(300)})
        project.register_existing_campaign(other)
        preview = self.preview('project',now=20,new=1200)
        with mock.patch.object(timing, 'append_event', side_effect=RuntimeError('模拟中断')):
            with self.assertRaises(RuntimeError):
                self.apply(preview,now=21)
        self.assertIn('extension_pending', artifacts.effective_deadlines(other,now=self.moment(22))['paused_scopes'])
        self.assertEqual(timing.inspect_ledger(other_ledger,now=self.at(22))['status'],'deadline_paused')
        self.apply(preview,now=23)
        self.assertFalse(artifacts.effective_deadlines(other,now=self.moment(24))['paused_scopes'])
        self.assertEqual(timing.inspect_ledger(other_ledger,now=self.at(24))['status'],'active')

    def test_new_campaign_uses_project_deadline_approved_before_creation(self):
        self.apply(self.preview('project',now=20,new=3600),now=21)
        new = self.root/'later-timing'
        timing.create_ledger(new, upgrade_id='later',baseline_version='0.154.0',target_version='0.156.1',
            campaign_purpose='validation_only',evidence_decision='recapture', started_at_utc=self.at(700),
            total_budget_minutes=30, stage_budgets_minutes={phase:20 for phase in timing.PHASE_ORDER},
            project_ledger_dir=self.project)
        plan,_=timing._load_plan(new)
        self.assertEqual(plan['project_ledger_binding']['absolute_deadline_utc'],self.at(600))
        with self.assertRaisesRegex(timing.TimingLedgerError,'总墙钟预算'):
            timing.create_ledger(self.root/'before-approval', upgrade_id='before',baseline_version='0.154.0',target_version='0.156.1',
                campaign_purpose='validation_only',evidence_decision='recapture', started_at_utc=self.at(0),
                total_budget_minutes=30, project_ledger_dir=self.project)

    def test_four_consumers_share_deadlines_and_extension_keeps_other_limits(self):
        self.apply(self.preview('stage',now=20,new=900),now=21)
        self.apply(self.preview('campaign',now=22,new=1200),now=23)
        with mock.patch.object(artifacts, 'datetime', wraps=datetime) as clock, \
             mock.patch.object(timing, '_utc_now', return_value=self.at(24)):
            clock.now.return_value=self.moment(24)
            deadlines=artifacts.effective_deadlines(self.campaign)
            self.assertEqual(upgrade._campaign_plan_deadline(self.campaign),self.at(1200))
            state={'deadline_at_epoch':self.moment(1200).timestamp(),'budget_guard':{'campaign_dir':str(self.campaign)}}
            self.assertEqual(supervisor._runtime_budget_deadline(state),self.moment(600).timestamp())
        summary=timing.inspect_ledger(self.ledger,now=self.at(24))
        self.assertEqual((summary['total_deadline_at_utc'],summary['stage_deadline_at_utc']),
                         (deadlines['total_deadline_at_utc'],deadlines['stage_deadline_at_utc']))
        project.assert_campaign_admitted(self.campaign,command='campaign-run',require=True,now=self.moment(24))
        head=project.replay_head(self.project)
        plan,_=project._load_plan(self.project)
        for changed in ({'blocked':True},{'remaining_live_requests':0},{'root_causes_at_limit':['fixture']}):
            decision=reconciler._decide(head={**head,**changed},plan=plan,ledger=summary,identity={'unchanged':True},
                environment_status='restored',campaign_deadline_at_utc=deadlines['total_deadline_at_utc'],
                root_cause_id='fixture',request_status='resolved',now=self.at(24))
            self.assertEqual(decision['decision'],'permanent_stop')

    def test_live_deadline_read_does_not_precede_concurrent_event_snapshot(self):
        inspect = timing.inspect_ledger
        def read_after_append(root, *, now=None, **kwargs):
            with mock.patch.object(timing, 'inspect_ledger', side_effect=inspect):
                timing.append_event(root, event_id='concurrent-metadata', phase='VC-0', event_type='receipt_passed',
                    recorded_at_utc=self.at(21), next_action='模拟读取前刚追加的事件')
            return inspect(root, now=now, **kwargs)
        with mock.patch.object(artifacts, 'datetime', wraps=datetime) as clock, \
             mock.patch.object(timing, '_utc_now', return_value=self.at(22)), \
             mock.patch.object(timing, 'inspect_ledger', side_effect=read_after_append):
            clock.now.return_value = self.moment(20)
            result = artifacts.effective_deadlines(self.campaign)
        self.assertEqual(result['total_elapsed_seconds'], 22)
        with self.assertRaisesRegex(timing.TimingLedgerError, '早于最新 event'):
            artifacts.effective_deadlines(self.campaign, now=self.moment(20))

    def test_stopped_status_is_not_overwritten_by_later_budget_expiry(self):
        timing.append_event(self.ledger,event_id='stop',phase='VC-0',event_type='stop_the_line',
                            root_cause_id='integrity',recorded_at_utc=self.at(20),next_action='保留完整性停线')
        with mock.patch.object(upgrade,'campaign_status',return_value={'status':'stopped','project_ledger':{}}), \
             mock.patch.object(timing, '_utc_now', return_value=self.at(900)), \
             mock.patch.object(artifacts,'datetime',wraps=datetime) as clock:
            clock.now.return_value=self.moment(900)
            self.assertEqual(upgrade._campaign_status_with_deadlines(self.campaign,None)['status'],'stopped')
        with self.assertRaisesRegex(project.ProjectLedgerError,'既有停线'):
            self.preview('project',now=900,new=2000)

    def test_stage_extension_across_revision_preserves_consumed_time(self):
        def event(name,phase,kind,second,**kwargs):
            return timing.append_event(self.ledger,event_id=name,phase=phase,event_type=kind,
                                       recorded_at_utc=self.at(second),next_action='隔离 revision 验证',**kwargs)
        event('c0','VC-0','stage_completed',1)
        for index,phase in enumerate(('VC-1','VC-2','VC-3'),1):
            event('s'+str(index),phase,'stage_started',index*2)
            event('c'+str(index),phase,'stage_completed',index*2+1)
        event('r1','VC-4','stage_revision',8,revision=1,candidate_id='candidate-a',revision_commit_sha256='a'*64)
        event('s4','VC-4','stage_started',9)
        first=project.preview_deadline_extension(self.campaign,scope='stage',phase='VC-4',
            new_deadline_at_utc=self.at(150),reason='r1 扩展',now=self.moment(20))
        self.apply(first,now=21)
        event('a4','VC-4','stage_abandoned',25,root_cause_id='fixture')
        event('review','VC-4','candidate_review_required',26,candidate_id='candidate-a',root_cause_id='fixture')
        event('invalid','VC-4','candidate_invalidated',27,candidate_id='candidate-a',root_cause_id='fixture')
        event('r2','VC-4','stage_revision',28,revision=2,candidate_id='candidate-b',revision_commit_sha256='b'*64,supersedes_revision=1)
        event('s4-r2','VC-4','stage_started',29)
        state=timing.inspect_ledger(self.ledger,now=self.at(30))
        self.assertEqual(state['stage_elapsed_seconds'],17)
        self.assertEqual(state['stage_deadline_at_utc'],self.at(154))
        second=project.preview_deadline_extension(self.campaign,scope='stage',phase='VC-4',
            new_deadline_at_utc=self.at(200),reason='r2 扩展',now=self.moment(31))
        self.apply(second,now=32)
        state=timing.inspect_ledger(self.ledger,now=self.at(33))
        self.assertEqual(state['stage_elapsed_seconds'],20)
        self.assertEqual(state['stage_deadline_at_utc'],self.at(200))

    def test_historical_deadline_terminal_remains_readable(self):
        plan,_=project._load_plan(self.project)
        events=project._load_events(self.project)
        payload={'campaign_id':'r8-campaign','terminal_reason':'deadline_wall_clock'}
        event={'schema_version':project.EVENT_SCHEMA,'sequence':len(events)+1,'operation_id':'historical-timeout',
            'recorded_at_utc':self.at(650),'event_type':'campaign_terminal','payload':payload,
            'payload_sha256':project._digest(payload),'source_batch_sha256':None,
            'previous_event_sha256':events[-1]['event_sha256']}
        event['event_sha256']=project._digest(event)
        result=project._replay(self.project,plan,[*events,event],rebuild_cache=False)
        self.assertEqual(result['terminal_campaigns']['r8-campaign']['terminal_reason'],'deadline_wall_clock')


if __name__ == '__main__':
    unittest.main()
