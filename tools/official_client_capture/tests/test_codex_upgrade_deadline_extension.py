"""R8 三层预算、批准闭集、两账中断补齐与历史回放；全部使用隔离零请求文件。"""
import hashlib
import io
import json
import shutil
import tempfile
from contextlib import redirect_stderr, redirect_stdout
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


    def test_plan_validation_reads_only_binding_fields(self):
        """R8 审核修正：计划校验只用绑定字段，不打开总账；新绑定冻结开始时刻的项目有效截止。"""

        plan, _raw = timing._load_plan(self.ledger)
        binding = plan['project_ledger_binding']
        self.assertEqual(binding['effective_deadline_at_start_utc'], self.at(600))
        archived = self.root / 'archived-project-ledger'
        self.project.rename(archived)
        self.addCleanup(lambda: archived.exists() and archived.rename(self.project))
        timing._load_plan(self.ledger)
        with self.assertRaisesRegex(timing.TimingLedgerError, '重新定位'):
            timing.inspect_ledger(self.ledger, now=self.at(20))
        legacy = {**plan, 'project_ledger_binding': {key: value for key, value in binding.items()
                                                     if key != 'effective_deadline_at_start_utc'}}
        timing._validate_plan(legacy)
        tampered = {**plan, 'project_ledger_binding': {**binding, 'effective_deadline_at_start_utc': self.at(500)}}
        with self.assertRaisesRegex(timing.TimingLedgerError, '不得早于总账原绝对截止'):
            timing._validate_plan(tampered)

    def test_whole_data_root_move_keeps_ledger_and_extension_replayable(self):
        """数据根整体迁移后，绑定与延期收据里的旧绝对路径不再存在，账本仍按计划摘要定位总账并可重放。"""

        self.apply(self.preview('campaign', now=20, new=1200), now=21)
        # 夹具事件时间领先真实时钟，前后都用同一固定时刻重放全部事件再比对。
        expected = timing.inspect_ledger(self.ledger, now=self.at(30))
        moved = self.root.parent / 'moved'
        moved.mkdir(mode=0o700)
        shutil.move(str(self.root), str(moved / 'staging'))
        ledger = moved / 'staging' / 'timing'
        self.assertFalse(self.project.exists())
        replayed = timing.inspect_ledger(ledger, now=self.at(30))
        for key in ('status', 'head_sequence', 'head_sha256', 'total_deadline_at_utc', 'stage_deadline_at_utc', 'deadline_extensions'):
            self.assertEqual(replayed[key], expected[key], key)
        self.assertEqual(replayed['total_deadline_at_utc'], self.at(1200))

    def test_committed_proof_closes_project_extension_after_owner_directory_removed(self):
        """项目延期写完两本账后追加提交证明；发起方目录被清理后，其他 Campaign 的准入与截止仍可判定。"""

        other = self.campaign.with_name('other-campaign')
        other_ledger = self.root / 'other-timing'
        timing.create_ledger(other_ledger, upgrade_id='other-campaign', baseline_version='0.154.0',
            target_version='0.156.1', campaign_purpose='validation_only', evidence_decision='recapture',
            started_at_utc=self.at(0), total_budget_minutes=5, project_ledger_dir=self.project,
            stage_budgets_minutes={phase: 5 for phase in timing.PHASE_ORDER})
        self.write(other / 'campaign.json', {'campaign_id': 'other-campaign', 'campaign_mode': 'formal',
            'target_version': '0.156.1', 'control_receipts': {'upgrade_timing': {'ledger_dir': str(other_ledger)}}})
        self.write(other / 'control/vc/campaign-plan.json', {'campaign_id': 'other-campaign', 'original_deadline_at_utc': self.at(300)})
        project.register_existing_campaign(other)
        result = self.apply(self.preview('project', now=20, new=1200), now=21)
        head = project.replay_head(self.project)
        self.assertIn(result['receipt_sha256'], head['committed_deadline_extensions'])
        shutil.rmtree(self.campaign)
        self.assertNotIn('extension_pending', artifacts.effective_deadlines(other, now=self.moment(22))['paused_scopes'])
        project.assert_campaign_admitted(other, command='campaign-run', require=True, now=self.moment(22))

    def test_own_extension_pending_detected_when_upgrade_id_differs_from_campaign_id(self):
        """计时账本按批准时绑定的本账本 head 认领延期；upgrade_id 与 campaign_id 不同时也能报出未闭合事务。"""

        campaign = self.campaign.with_name('mismatch-campaign')
        ledger = self.root / 'mismatch-timing'
        timing.create_ledger(ledger, upgrade_id='upgrade-mismatch', baseline_version='0.154.0',
            target_version='0.156.1', campaign_purpose='validation_only', evidence_decision='recapture',
            started_at_utc=self.at(0), total_budget_minutes=5, project_ledger_dir=self.project,
            stage_budgets_minutes={phase: 5 for phase in timing.PHASE_ORDER})
        self.write(campaign / 'campaign.json', {'campaign_id': 'mismatch-campaign', 'campaign_mode': 'formal',
            'target_version': '0.156.1', 'control_receipts': {'upgrade_timing': {'ledger_dir': str(ledger)}}})
        self.write(campaign / 'control/vc/campaign-plan.json', {'campaign_id': 'mismatch-campaign', 'original_deadline_at_utc': self.at(300)})
        project.register_existing_campaign(campaign)
        preview = project.preview_deadline_extension(campaign, scope='campaign', phase=None, new_deadline_at_utc=self.at(1200),
                                                     reason='隔离测试：账本 upgrade_id 与 Campaign 不同', now=self.moment(20))
        with mock.patch.object(timing, 'append_event', side_effect=RuntimeError('模拟总账后进程中断')):
            with self.assertRaisesRegex(RuntimeError, '进程中断'):
                project.apply_deadline_extension(campaign, preview_path=Path(preview['preview_path']),
                    approve_sha256=preview['review_sha256'], approved_by='fixture-reviewer', now=self.moment(21))
        self.assertEqual(timing.inspect_ledger(ledger, now=self.at(22))['status'], 'deadline_paused')

    def test_attempt_expiry_ignores_extensions_approved_after_reference(self):
        """先延期后对账：attempt 当时的到期只按参考时刻之前已批准的延期判断，根因不随后续延期漂移。"""

        extension = {'scope': 'campaign', 'phase': None, 'approved_at_utc': self.at(400),
                     'original_deadline_at_utc': self.at(300), 'new_deadline_at_utc': self.at(1200)}
        ledger = {'total_deadline_at_utc': self.at(1200), 'stage_deadline_at_utc': None, 'deadline_extensions': [extension]}
        plan = {'absolute_deadline_utc': self.at(99999)}
        def expired(completed):
            return reconciler._attempt_deadline_expired(attempt={'completed_at_utc': self.at(completed)}, ledger=ledger,
                plan=plan, campaign_deadline_at_utc=self.at(1200), now=self.at(900), project_ledger_root=None)
        self.assertTrue(expired(350))
        self.assertFalse(expired(450))

    def _ledger_bytes(self, ledger):
        return {path.relative_to(ledger): path.read_bytes() for path in sorted(ledger.rglob('*'))
                if path.is_file() and not path.name.endswith('.lock')}

    def test_copied_data_root_refuses_ambiguous_project_ledger(self):
        """R8 复审修正：数据根被复制、旧位置仍在时，同一计划的总账有两份，定位失败关闭而不是取第一个命中。

        修改前按记录路径优先，副本树里的计时账本会定位回旧位置的总账，按它的事件流判定截止与延期，
        写入也会落到旧位置。
        """

        plan, _raw = timing._load_plan(self.ledger)
        binding = plan['project_ledger_binding']
        copied = self.root.parent / 'copied'
        shutil.copytree(self.root, copied)
        copied_ledger = copied / 'timing'
        with self.assertRaisesRegex(timing.TimingLedgerError, '存在多份'):
            timing._locate_project_root(copied_ledger, recorded_path=binding['path'], plan_sha256=binding['plan_sha256'])
        before = self._ledger_bytes(copied_ledger)
        original_project = self._ledger_bytes(self.project)
        with self.assertRaisesRegex(timing.TimingLedgerError, '存在多份'):
            timing.append_event(copied_ledger, event_id='copied-stop', phase='VC-0', event_type='stop_the_line',
                                root_cause_id='integrity', recorded_at_utc=self.at(20), next_action='副本树写入')
        self.assertEqual(self._ledger_bytes(copied_ledger), before)
        self.assertEqual(self._ledger_bytes(self.project), original_project)

    def test_missing_plan_digest_accepts_no_candidate(self):
        """R8 复审修正：没有计划摘要就无法确认总账身份，记录路径与祖先链上的候选一律不接受。"""

        with self.assertRaisesRegex(timing.TimingLedgerError, '计划摘要'):
            timing._locate_project_root(self.ledger, recorded_path=str(self.project), plan_sha256=None)

    def test_unbound_timing_ledger_extension_is_located_by_commit_proof(self):
        """R8 复审修正：指南允许不绑定总账的计时账本，它的延期按"含这次延期提交证明"唯一定位总账。

        无绑定账本可以注册、延期并正常重放；数据根被复制、旧位置仍在时，两份总账都含同一份证明，写路径失败
        关闭、只读入口降级，不再像修改前那样接受第一个命中。
        """

        ledger = self.root / 'unbound-timing'
        timing.create_ledger(ledger, upgrade_id='unbound-campaign', baseline_version='0.154.0',
            target_version='0.156.1', campaign_purpose='validation_only', evidence_decision='recapture',
            started_at_utc=self.at(0), total_budget_minutes=5,
            stage_budgets_minutes={phase: 5 for phase in timing.PHASE_ORDER})
        campaign = self.campaign.with_name('unbound-campaign')
        self.write(campaign / 'campaign.json', {'campaign_id': 'unbound-campaign', 'campaign_mode': 'formal',
            'target_version': '0.156.1', 'control_receipts': {'upgrade_timing': {'ledger_dir': str(ledger)}}})
        self.write(campaign / 'control/vc/campaign-plan.json', {'campaign_id': 'unbound-campaign', 'original_deadline_at_utc': self.at(300)})
        project.register_existing_campaign(campaign)
        preview = project.preview_deadline_extension(campaign, scope='campaign', phase=None, new_deadline_at_utc=self.at(1200),
                                                     reason='隔离测试：无绑定账本延期', now=self.moment(20))
        project.apply_deadline_extension(campaign, preview_path=Path(preview['preview_path']),
            approve_sha256=preview['review_sha256'], approved_by='fixture-reviewer', now=self.moment(21))
        summary = timing.inspect_ledger(ledger, now=self.at(30))
        self.assertEqual(summary['total_deadline_at_utc'], self.at(1200))
        self.assertNotIn('project_ledger_unreachable', summary)
        copied = self.root.parent / 'copied'
        shutil.copytree(self.root, copied)
        copied_ledger = copied / 'unbound-timing'
        with self.assertRaisesRegex(timing.TimingLedgerError, '多份'):
            timing.inspect_ledger(copied_ledger, now=self.at(30))
        before = self._ledger_bytes(copied_ledger)
        with self.assertRaisesRegex(timing.TimingLedgerError, '多份'):
            timing.append_event(copied_ledger, event_id='copied-unbound-stop', phase='VC-0', event_type='stop_the_line',
                                root_cause_id='integrity', recorded_at_utc=self.at(31), next_action='副本树写入')
        self.assertEqual(self._ledger_bytes(copied_ledger), before)
        degraded = timing.inspect_ledger(copied_ledger, now=self.at(30), project_ledger_optional=True)
        marker = degraded.pop('project_ledger_unreachable')
        self.assertRegex(marker['reason'], '多份')
        self.assertEqual(marker['unverified_deadline_extensions'], [summary['deadline_extensions'][0]['receipt_sha256']])
        self.assertEqual(degraded, summary)

    def _archive_project(self):
        archived = self.root / 'archived-project-ledger'
        self.project.rename(archived)
        self.addCleanup(lambda: archived.exists() and archived.rename(self.project))
        return archived

    def test_read_only_inspect_degrades_when_project_ledger_unreachable(self):
        """R8 复审修正（选项 B）：总账不可达时只读 inspect 降级而不失败，写入仍失败关闭。

        项目层截止改用绑定里冻结的开始时刻有效截止，本账本延期照常计入但记为未核实，摘要标注
        project_ledger_unreachable；默认调用（写路径与准入前置）在定位失败时仍抛错。
        """

        self.apply(self.preview('campaign', now=20, new=1200), now=21)
        expected = timing.inspect_ledger(self.ledger, now=self.at(30))
        self.assertNotIn('project_ledger_unreachable', expected)
        self._archive_project()
        with self.assertRaisesRegex(timing.TimingLedgerError, '重新定位'):
            timing.inspect_ledger(self.ledger, now=self.at(30))
        degraded = timing.inspect_ledger(self.ledger, now=self.at(30), project_ledger_optional=True)
        marker = degraded.pop('project_ledger_unreachable')
        self.assertEqual(degraded, expected)
        self.assertRegex(marker['reason'], '重新定位')
        self.assertEqual(marker['project_deadline_source'], 'binding_frozen_at_start')
        self.assertEqual(marker['unverified_deadline_extensions'],
                         [row['receipt_sha256'] for row in expected['deadline_extensions']])
        before = self._ledger_bytes(self.ledger)
        with self.assertRaisesRegex(timing.TimingLedgerError, '重新定位'):
            timing.append_event(self.ledger, event_id='write-while-unreachable', phase='VC-0', event_type='stop_the_line',
                                root_cause_id='integrity', recorded_at_utc=self.at(31), next_action='总账不可达时写入')
        with self.assertRaisesRegex(timing.TimingLedgerError, '重新定位'):
            timing.build_checkpoint(self.ledger, observed_at_utc=self.at(31))
        self.assertEqual(self._ledger_bytes(self.ledger), before)

    def test_read_only_replay_degrades_when_project_ledger_unreachable(self):
        """R8 复审修正（选项 B）：历史 checkpoint 在总账不可达时可只读回放，依赖总账的摘要字段登记为未核实。

        计划绑定、事件头与不依赖总账的摘要字段仍逐字节比对，篡改照样失败；程序内的默认回放仍失败关闭。
        命令行 replay 与 status 是人工只读入口，同样降级并在输出里标注。
        """

        self.apply(self.preview('campaign', now=20, new=1200), now=21)
        with mock.patch.object(timing, '_utc_now', return_value=self.at(30)):
            timing.checkpoint(self.ledger, 'receipts/r8-review2.json')
        self._archive_project()
        with self.assertRaisesRegex(timing.TimingLedgerError, '重新定位'):
            timing.replay(self.ledger, 'receipts/r8-review2.json')
        receipt = timing.replay(self.ledger, 'receipts/r8-review2.json', project_ledger_optional=True)
        marker = receipt['project_ledger_unreachable']
        self.assertRegex(marker['reason'], '重新定位')
        self.assertEqual(marker['unverified_summary_fields'], [])
        for argv in (['replay', '--ledger-dir', str(self.ledger), '--receipt', 'receipts/r8-review2.json'],
                     ['status', '--ledger-dir', str(self.ledger)]):
            output = io.StringIO()
            with self.subTest(command=argv[0]), redirect_stdout(output), redirect_stderr(io.StringIO()), \
                    mock.patch.object(timing, '_utc_now', return_value=self.at(30)):
                self.assertEqual(timing.main(argv), 0)
                self.assertIn('project_ledger_unreachable', json.loads(output.getvalue()))
        tampered = json.loads((self.ledger / 'receipts/r8-review2.json').read_bytes())
        tampered['summary']['total_live_request_count'] += 1
        forged = self.ledger / 'receipts/r8-review2-tampered.json'
        forged.write_bytes(timing._canonical(tampered))
        forged.chmod(0o600)
        with self.assertRaisesRegex(timing.TimingLedgerError, '重放结果不一致'):
            timing.replay(self.ledger, 'receipts/r8-review2-tampered.json', project_ledger_optional=True)

    def test_campaign_status_survives_unreachable_project_ledger(self):
        """R8 复审修正（选项 B）：main 上 status 在总账不可达时返回 project_ledger=None 而不失败，R8 的截止投影
        让它改为抛错，属于回归。只读投影降级并透出标注；准入与写入路径的默认调用仍失败关闭。
        """

        self._archive_project()
        with self.assertRaisesRegex(timing.TimingLedgerError, '重新定位'):
            artifacts.effective_deadlines(self.campaign, now=self.moment(30))
        deadlines = artifacts.effective_deadlines(self.campaign, now=self.moment(30), project_ledger_optional=True)
        self.assertRegex(deadlines['project_ledger_unreachable']['reason'], '重新定位')
        with mock.patch.object(upgrade, 'campaign_status', return_value={'status': 'planned', 'project_ledger': None}), \
             mock.patch.object(timing, '_utc_now', return_value=self.at(30)), \
             mock.patch.object(artifacts, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.moment(30)
            result = upgrade._campaign_status_with_deadlines(self.campaign, None)
        self.assertEqual(result['status'], 'planned')
        self.assertRegex(result['project_ledger_unreachable']['reason'], '重新定位')

    def test_ambiguous_project_ledger_degrades_read_only(self):
        """R8 复审修正：同一计划的总账有多份也属于无法确认总账，只读入口同样降级，写入在小目标 1 的用例里被拒。"""

        copied = self.root.parent / 'copied'
        shutil.copytree(self.root, copied)
        degraded = timing.inspect_ledger(copied / 'timing', now=self.at(30), project_ledger_optional=True)
        self.assertRegex(degraded['project_ledger_unreachable']['reason'], '存在多份')


class PausedPredecessorTests(unittest.TestCase):
    """R8：前任 Campaign 预算暂停时，复用或后继新建不能借新 Campaign 取得新预算（含真实 CLI 路径）。"""

    def setUp(self):
        self.case = DeadlineExtensionTests('test_paused_consumers_all_reject')
        # 起点 120 秒前：阶段预算 1 分钟已到期，Campaign 预算 5 分钟与项目截止 10 分钟都未到。
        self.case.initial_age_seconds = 120
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_paused_or_expired_predecessor_is_refused_until_explicit_terminal(self):
        with self.assertRaisesRegex(upgrade.ConfigurationError, '前任 Campaign 处于预算暂停'):
            upgrade._require_predecessor_not_budget_paused(self.case.campaign)
        project.pause_campaign_deadline(self.case.campaign, now=self.case.moment(90))
        with self.assertRaisesRegex(upgrade.ConfigurationError, '前任 Campaign 处于预算暂停'):
            upgrade._require_predecessor_not_budget_paused(self.case.campaign)
        successor = self.case.root / 'evidence/campaigns/successor'
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = upgrade.main(['reuse-official-evidence', '--predecessor-campaign-dir', str(self.case.campaign),
                                 '--campaign-dir', str(successor), '--campaign-id', 'successor', '--codex-account-id', '1'])
        self.assertEqual(code, 1)
        self.assertIn('前任 Campaign 处于预算暂停', stderr.getvalue())
        self.assertFalse((successor / 'campaign.json').exists())
        project.abandon_campaign(self.case.campaign, approved_by='fixture-reviewer', reason='隔离测试：显式放弃后允许新建')
        upgrade._require_predecessor_not_budget_paused(self.case.campaign)


if __name__ == '__main__':
    unittest.main()
