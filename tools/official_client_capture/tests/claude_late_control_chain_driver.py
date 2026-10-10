"""在同一 VC-5 世界验证暂停和延期，然后续接 VC-6 故障；尚非完整 A 组认证。

沿用 claude_late_stage_chain_driver 明示的输入接缝。暂停只在真实 compile 返回后注入异常，
预算只把真实门禁的观察时刻推进到冻结截止之后，保留原门禁计算和原始账本字节。
两类故障修好后沿原 Campaign 顺序派发 compare、builder、accept 和四阶段交付。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests import claude_fault_chain_world as stops
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests


class ClaudeLateControlChainDriver(late.ClaudeLateStageChainDriver):
    """故障点共享一个世界；方法返回六项审计，外层选择是否登记成完整认证场景。"""

    def repeat_root_cause_then_compare(self, world: dict[str, Any], sequence: int) -> dict[str, Any]:
        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        campaign = world["campaign"]
        self.campaign = campaign
        scenario = '后段同根因暂停并续派 compare'
        cursor = f'late-root-cause:{campaign}:{sequence}'
        state = stops.resume_checkpoint(self, cursor)
        if state is None:
            if os.environ.get(stops.METHOD_RESUME_ENV) and stops.load_state('audit-before:' + scenario) is not None:
                raise ValueError('原同根因场景缺游标，禁止重取当前态')
            state = {'phase': 'prepare-fault-1', 'campaign': campaign, 'sequence': sequence, 'messages': [],
                'audit_snapshots': {scenario: 'uninitialized'}}
            stops.checkpoint(self, cursor, **state)
        self.assertEqual((state['campaign'], state['sequence']), (campaign, sequence), '原同根因游标坐标已改变')
        if state['phase'] not in {'prepare-fault-1', 'prepare-fault-2', 'pause-before', 'paused-refusal',
                                 'repair-inputs', 'repair-write', 'repair-register', 'compare-redispatch', 'audit', 'done'}:
            raise ValueError('同根因场景原游标阶段非法，禁止猜测下一动作')
        if state['phase'] == 'done':
            return ScenarioAudit.revalidate(self, scenario)
        action = self.compare_action(campaign, candidate_id=world.get("candidate_id", late.CANDIDATE))
        audit = self.late_audit(scenario, world)
        state['audit_snapshots'][scenario] = 'recorded'
        stops.checkpoint(self, cursor, **state)
        original = upgrade.compile_vc_batch

        def fail_after_prepare(*arguments: Any, **options: Any) -> Any:
            original(*arguments, **options)
            raise RuntimeError("后段连续链：compare 批次 prepare 之后中断")

        staging = campaign / "control" / "vc" / "staging" / f"{sequence:04d}-vc-5"
        for index in (1, 2):
            if state['phase'] == f'prepare-fault-{index}':
                # 两次相同 argv 必须留下两条故障事实，不能由 argv 判重折成一次。
                with mock.patch.object(upgrade, 'compile_vc_batch', side_effect=fail_after_prepare):
                    code, result, stderr = self.dispatch_vc5(world, sequence, action,
                        command_cursor=cursor, command_key=f'prepare-fault-{index}')
                self.assertEqual((code, result), (1, None), stderr)
                state['messages'].append(stderr)
                state['phase'] = 'prepare-fault-2' if index == 1 else 'pause-before'
                stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'pause-before':
            counted = audit.accounting_state()
            old_counts = audit.totals_before['project']['root_cause_counts']
            changed = {key: value for key, value in ledger.replay_head(self.project_root)['root_cause_counts'].items()
                if value != old_counts.get(key, 0)}
            self.assertEqual(len(changed), 1)
            cause, count = next(iter(changed.items()))
            self.assertEqual(count - old_counts.get(cause, 0), 2)
            state.update(phase='paused-refusal', cause=cause, counted=counted,
                attempts=sorted(path.name for path in staging.iterdir()), before_refusal=audit.accounting_state())
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'paused-refusal':
            code, _result, stderr = self.dispatch_vc5(world, sequence, action,
                command_cursor=cursor, command_key='paused-refusal')
            self.assertEqual(code, 1, stderr)
            self.assertIn('根因达上限', stderr)
            self.assertEqual(sorted(path.name for path in staging.iterdir()), state['attempts'])
            state.update(phase='repair-inputs', after_refusal=audit.accounting_state())
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'repair-inputs':
            regression = self.root / 'late-control-regression.txt'
            deploy = self.data / 'control/claude-managed-deploy-20261008t030000z.json'
            # 在首次写入前冻结时间和内容。恢复到写入后窄窗时只核原件，不能重写时间。
            state.update(phase='repair-write', regression=regression, deploy=deploy,
                regression_bytes='夹具故障开关已解除；该文件只作为隔离测试修复登记的输入\n'.encode('utf-8'),
                deployment={'schema_version': ledger_fixture.DEPLOY_RECEIPT_SCHEMA, 'status': 'passed', 'client': 'claude',
                    'created_at_utc': datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                    'identity': {key: self.tool_identity[key] for key in ('policy_version', 'policy_sha256',
                        'wire_producer_sha256', 'evidence_semantics_sha256', 'control_sha256')} |
                        {'tool_files_sha256': self.tool_identity['files_sha256']}})
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'repair-write':
            regression, deploy = state['regression'], state['deploy']
            self.assertFalse(regression.is_symlink(), '原修复输入不能换成符号链接')
            self.assertFalse(deploy.is_symlink(), '原修复部署收据不能换成符号链接')
            if regression.exists():
                self.assertEqual(regression.read_bytes(), state['regression_bytes'], '原修复输入字节已改变')
            else:
                with regression.open('xb') as stream:
                    stream.write(state['regression_bytes'])
            if deploy.exists():
                self.assertEqual(json.loads(deploy.read_bytes()), state['deployment'], '原修复部署收据内容已改变')
            else:
                ledger_fixture.write_claude_deploy_receipt(self.data, self.tool_identity, stamp='20261008t030000z',
                    created_at_utc=state['deployment']['created_at_utc'])
            sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            state.update(phase='repair-register', repair_argv=[
                sys.executable, '-B', '-m', 'tools.official_client_capture.codex_upgrade_project_ledger',
                'record-root-cause-repair', '--ledger-dir', str(self.project_root), '--root-cause-id', state['cause'],
                '--kind', 'code', '--binding', f"fix_commit_sha={'b' * 40}",
                '--binding', f'regression_receipt_sha256={sha(regression)}',
                '--binding', f'deployment_receipt_sha256={sha(deploy)}', '--note', '后段连续链：解除 prepare 故障后继续'],
                repair_inputs=[stops.file_cut(regression), stops.file_cut(deploy)])
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'repair-register':
            for binding in state['repair_inputs']:
                stops.read_cut(binding)
            argv = state['repair_argv']

            def register_repair() -> tuple[int, dict | None, str]:
                pending = stops.begin_command(argv)
                repaired = subprocess.run(argv, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
                stops.record_command(argv, repaired.returncode, repaired.stdout, repaired.stderr, pending=pending)
                try:
                    result = json.loads(repaired.stdout) if repaired.stdout.strip() else None
                except ValueError:
                    result = None
                return repaired.returncode, result, repaired.stderr

            code, _result, stderr = stops.command_step(self, cursor, 'root-cause-repair-register', argv,
                register_repair, campaign=campaign)
            self.assertEqual(code, 0, stderr)
            state['phase'] = 'compare-redispatch'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'compare-redispatch':
            outcome = self.assert_batch_passed(self.dispatch_vc5(world, sequence, action,
                command_cursor=cursor, command_key='compare-after-repair'), sequence, 'VC-5')
            self.assertEqual(outcome['staging_attempt'], len(state['attempts']) + 1)
            self.assertEqual(ledger.replay_head(self.project_root)['root_cause_counts'].get(state['cause']), 0)
            state['phase'] = 'audit'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] != 'audit':
            raise ValueError('同根因场景原游标阶段非法，禁止猜测下一动作')
        if os.environ.get(stops.METHOD_RESUME_ENV) and stops.load_state('audit-complete:' + scenario) is not None:
            record = ScenarioAudit.revalidate(self, scenario)
        else:
            batch_path = campaign / 'control/vc/batches' / f'{sequence:04d}-vc-5.json'
            batch = json.loads(batch_path.read_bytes())
            audit.check_trusted()
            audit.check_closure(executed=batch['execute_item_ids'], expected=['compare'],
                reused=batch['reuse_item_ids'], expected_reused=[], what='同根因修复后的批次',
                source=audit.file_closure_source(batch_path, ('execute_item_ids',), ('reuse_item_ids',)))
            audit.check_accounting(root_causes=2, root_cause_state=state['counted'],
                repeats=[('暂停时拒绝派发', state['before_refusal'], state['after_refusal'])])
            audit.check_elapsed()
            audit.check_history()
            audit.check_entry(state['messages'][-1], 'record-root-cause-repair', '同序号重新派发')
            record = audit.report(campaign_id=campaign.name, root_cause_id=state['cause'], sequence=sequence)
        state['phase'] = 'done'
        stops.checkpoint(self, cursor, **state)
        return record

    def expired_budget_then_assert(self, world: dict[str, Any], sequence: int) -> dict[str, Any]:
        """只注入门禁的观察时间；准入判断、延期批准和后续 builder 均执行产品实现。"""
        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        campaign = world["campaign"]
        scenario = '后段预算到期并延期续派 builder'
        cursor = f'late-deadline:{campaign}:{sequence}'
        state = stops.resume_checkpoint(self, cursor)
        if state is None:
            if os.environ.get(stops.METHOD_RESUME_ENV) and stops.load_state('audit-before:' + scenario) is not None:
                raise ValueError('原延期场景缺游标，禁止重取当前态或冻结字节')
            frozen = campaign / 'control/vc/campaign-plan.json'
            deadline = datetime.fromisoformat(json.loads(frozen.read_bytes())['original_deadline_at_utc'].replace('Z', '+00:00'))
            frozen_paths = (campaign / 'campaign.json', frozen, self.ledger_dir / 'ledger.json', self.project_root / 'plan.json')
            state = {'phase': 'refusal-before', 'campaign': campaign, 'sequence': sequence,
                'frozen': frozen, 'originals': {path: path.read_bytes() for path in frozen_paths},
                'observation': (deadline + timedelta(seconds=1)).isoformat(),
                'new_deadline': (deadline + timedelta(minutes=30)).isoformat(),
                'comparison': upgrade._load_stage_result(campaign, 'compare', world.get('candidate_id', late.CANDIDATE)),
                'audit_snapshots': {scenario: 'uninitialized'}}
            stops.checkpoint(self, cursor, **state)
        self.assertEqual((state['campaign'], state['sequence']), (campaign, sequence), '原延期游标坐标已改变')
        if state['phase'] not in {'refusal-before', 'refusal', 'preview', 'apply', 'builder', 'audit', 'done'}:
            raise ValueError('延期场景原游标阶段非法，禁止猜测下一动作')
        if state['phase'] == 'done':
            return ScenarioAudit.revalidate(self, scenario)
        action = self.assert_action(campaign, state['comparison'])
        audit = self.late_audit(scenario, world)
        state['audit_snapshots'][scenario] = 'recorded'
        stops.checkpoint(self, cursor, **state)
        observed_at = datetime.fromisoformat(state['observation'])
        original = ledger._campaign_deadline_problems

        def observed_after_deadline(*arguments: Any, **options: Any) -> Any:
            options["now"] = observed_at
            return original(*arguments, **options)

        if state['phase'] == 'refusal-before':
            state.update(phase='refusal', before_refusal=audit.accounting_state())
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'refusal':
            with mock.patch.object(ledger, '_campaign_deadline_problems', side_effect=observed_after_deadline):
                code, _result, refused = self.dispatch_vc5(world, sequence, action,
                    command_cursor=cursor, command_key='deadline-expired-refusal')
            self.assertEqual(code, 1, refused)
            self.assertIn('预算已暂停', refused)
            self.assertFalse((campaign / 'control/vc/staging' / f'{sequence:04d}-vc-5').exists())
            state.update(phase='preview', refused=refused, after_refusal=audit.accounting_state())
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'preview':
            argv = ['deadline-extend', 'preview', '--campaign-dir', campaign, '--scope', 'campaign',
                '--new-deadline-at-utc', state['new_deadline'], '--reason', '后段连续链：到期准入拒绝后批准延期']
            code, preview, stderr = stops.command_step(self, cursor, 'deadline-extension-preview', argv,
                lambda: batch_tests.run_main(argv), campaign=campaign)
            self.assertEqual(code, 0, stderr)
            self.assertIsNotNone(preview, stderr)
            state.update(phase='apply', preview=preview)
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'apply':
            argv = ['deadline-extend', 'apply', '--campaign-dir', campaign, '--preview', state['preview']['preview_path'],
                '--approve-sha256', state['preview']['review_sha256'], '--approved-by', '后段连续链']
            code, applied, stderr = stops.command_step(self, cursor, 'deadline-extension-apply', argv,
                lambda: batch_tests.run_main(argv), campaign=campaign)
            self.assertEqual(code, 0, stderr)
            self.assertIsNotNone(applied, stderr)
            state.update(phase='builder', applied=applied)
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'builder':
            self.assert_batch_passed(self.dispatch_vc5(world, sequence, action,
                command_cursor=cursor, command_key='builder-after-deadline-extension'), sequence, 'VC-5')
            state['phase'] = 'audit'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] != 'audit':
            raise ValueError('延期场景原游标阶段非法，禁止猜测下一动作')
        originals = state['originals']
        self.assertEqual(originals, {path: path.read_bytes() for path in originals})
        if os.environ.get(stops.METHOD_RESUME_ENV) and stops.load_state('audit-complete:' + scenario) is not None:
            record = ScenarioAudit.revalidate(self, scenario)
        else:
            batch_path = campaign / 'control/vc/batches' / f'{sequence:04d}-vc-5.json'
            batch = json.loads(batch_path.read_bytes())
            self.assertIn('deadline_extension', batch)
            self.assertEqual(batch['original_deadline_at_utc'], json.loads(originals[state['frozen']])['original_deadline_at_utc'])
            audit.check_trusted()
            audit.check_closure(executed=batch['execute_item_ids'], expected=['assert-rules'],
                reused=batch['reuse_item_ids'], expected_reused=[], what='延期后的 builder 批次',
                source=audit.file_closure_source(batch_path, ('execute_item_ids',), ('reuse_item_ids',)))
            audit.check_accounting(repeats=[('到期时拒绝派发', state['before_refusal'], state['after_refusal'])])
            audit.check_elapsed()
            audit.check_history()
            audit.check_entry(state['refused'], '批准延期')
            record = audit.report(campaign_id=campaign.name, sequence=sequence, observation_at_utc=observed_at.isoformat(),
                observation_injection='真实门禁仅替换 now 输入', extended_to=state['new_deadline'])
        state['phase'] = 'done'
        stops.checkpoint(self, cursor, **state)
        return record

    def exercise_control_and_delivery_faults(self, world: dict[str, Any] | None = None, *, sequence_start: int = 9) -> list[dict[str, Any]]:
        """给定 world 时继续原 Campaign，不重建证据、Store 或两本账。"""

        if world is None and os.environ.get(stops.METHOD_RESUME_ENV):
            raise ValueError('后段恢复必须交入原 world，禁止重新调用 vc5_world 建场')
        world = self.vc5_world() if world is None else world
        self.campaign = world["campaign"]
        cursor = f'late-controls:{self.campaign}:{sequence_start}'
        state = stops.resume_checkpoint(self, cursor)
        if state is None:
            if os.environ.get(stops.METHOD_RESUME_ENV) and any(stops.load_state(name) is not None for name in (
                    f'cursor:late-root-cause:{self.campaign}:{sequence_start}',
                    f'cursor:late-deadline:{self.campaign}:{sequence_start + 1}', 'late-delivery-locator')):
                raise ValueError('原后段控制总游标缺失，禁止从子场景结果反推进度')
            state = {'phase': 'root-cause', 'campaign': self.campaign, 'sequence_start': sequence_start, 'records': []}
            stops.checkpoint(self, cursor, **state)
        self.assertEqual((state['campaign'], state['sequence_start']), (self.campaign, sequence_start), '原后段控制坐标已改变')
        if state['phase'] not in {'root-cause', 'deadline', 'accept-inputs', 'accept-gate-pending', 'accept', 'delivery', 'done'}:
            raise ValueError('后段控制原游标阶段非法，禁止猜测下一动作')
        if state['phase'] == 'root-cause':
            state['records'].append(self.repeat_root_cause_then_compare(world, sequence_start))
            state['phase'] = 'deadline'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'deadline':
            state['records'].append(self.expired_budget_then_assert(world, sequence_start + 1))
            state['phase'] = 'accept-inputs'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'accept-inputs':
            with late.chain.seams(world['config_path']):
                identity = late.compare.candidate_gate_identity(self.campaign, world['manifest'],
                    world.get('candidate_id', late.CANDIDATE), world['build'])
                subject = late.chain.expected_subject(world['manifest'], world['candidate_payload'], identity)
            state.update(phase='accept-gate-pending', receipt='late-control-candidate-receipt.json', gate_subject=subject)
            stops.checkpoint(self, cursor, **state)
            receipt = self.gate_receipt(world, state['receipt'])
            self.assertEqual(receipt, state['receipt'], '后段 accept 的原门禁坐标已改变')
        if state['phase'] == 'accept-gate-pending':
            from tools.official_client_capture import codex_upgrade_gate_receipt as gate

            receipt_path = world['gate_root'] / state['receipt']
            if not receipt_path.is_file():
                raise ValueError('后段 accept 原门禁生成未闭合，禁止覆盖或重新生成原件')
            replayed = gate.replay(world['gate_root'], state['receipt'])
            self.assertEqual({field: replayed['subject'][field] for field in state['gate_subject']}, state['gate_subject'],
                '后段 accept 原门禁不属于冻结的候选')
            state.update(phase='accept', receipt_binding=stops.file_cut(receipt_path))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'accept':
            stops.read_cut(state['receipt_binding'])
            self.assert_batch_passed(self.dispatch_vc5(world, sequence_start + 2,
                self.accept_action(self.campaign, state['receipt'], world['gate_root'],
                    candidate_id=world.get('candidate_id', late.CANDIDATE)),
                command_cursor=cursor, command_key='strict-accept'), sequence_start + 2, 'VC-5')
            self.assertTrue(vc.vc_checkpoint_path(self.campaign, 'VC-5', revision=world.get('revision', 1)).is_file())
            state['phase'] = 'delivery'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'delivery':
            state['records'].extend(self.exercise_delivery_faults(world, sequence_start=sequence_start + 3))
            state['phase'] = 'done'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] != 'done':
            raise ValueError('后段控制原游标阶段非法，禁止猜测下一动作')
        if os.environ.get(stops.METHOD_RESUME_ENV):
            from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit
            records = [ScenarioAudit.revalidate(self, row['scenario']) for row in state['records']]
        else:
            records = state['records']
        self.assertEqual(len(records), 6)
        self.assert_recorded_client_requests(world)
        self.assertEqual(len({row["campaign_id"] for row in records}), 1)
        return records
