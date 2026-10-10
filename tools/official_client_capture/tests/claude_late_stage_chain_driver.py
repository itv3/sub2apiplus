"""B5b-03 A 组后段连续链的测试驱动，不单独登记认证入口。

同一 Campaign 从已有 VC-3／VC-4 批次夹具经真实 compare、builder、accept 进入 VC-6。
本文件沿用 claude_vc5_chain_fixture 明示的官方来源、候选来源及画像接缝，以及
claude_typed_vc6_fixture 的 typed 文件现场与独立守卫；不将这些输入接缝算作真机取证。
批次提交、恢复对账、交付阶段失效、Store 收据和 checkpoint 都执行产品代码。

这里先覆盖交付半链，完整 A 组及 ARM64 时长验收完成前不登记 late-stage-faults 通过。
"""

from __future__ import annotations

import os
import contextlib
import json
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import claude_vc5_chain_fixture as chain
from tools.official_client_capture.tests import claude_typed_vc6_fixture as environment
from tools.official_client_capture.tests import claude_fault_chain_world as stops
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_vc6_batch as delivery_tests

CANDIDATE = delivery_tests.CANDIDATE
# 续跑必须经父批次监督器；直接子动作名称不能代替当前合法入口。
RUN_ENTRY_FRAGMENTS = ("compile-and-run-vc-batch", "--phase VC-6", "claude_upgrade_delivery.run_action")
CRASH_MARKER_ENV = "CLAUDE_LATE_CHAIN_FINALIZE_CRASH_MARKER"
# 只在指定 finalize 动作进程、且 Store 已签出收据后的一次性故障；不改变成功判据。
FINALIZE_CRASH = f'''
if os.environ.get("{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV}") == "{delivery.FINALIZE_ACTION_ID}" and os.environ.get("{CRASH_MARKER_ENV}"):
    import signal
    from pathlib import Path
    from tools.official_client_capture import claude_upgrade_vc
    _original_complete = claude_upgrade_vc.complete_candidate_phase

    def _complete_after_one_crash(*args, **kwargs):
        marker = Path(os.environ["{CRASH_MARKER_ENV}"])
        if not marker.exists():
            marker.write_text("交付收据已签出，checkpoint 发布前强杀\\n", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        return _original_complete(*args, **kwargs)

    claude_upgrade_vc.complete_candidate_phase = _complete_after_one_crash
'''


class ClaudeLateStageChainDriver(batch_tests._RealTreeBatchCase):
    """借用输入构造和命令构造，不继承原测试用例，避免每个外层场景重复运行旧用例。"""

    vc5_world = delivery_tests.Vc6BatchChainTests.vc5_world
    assert_action = delivery_tests.Vc6BatchChainTests.assert_action
    run_compare_and_assert = delivery_tests.Vc6BatchChainTests.run_compare_and_assert
    vc5_complete_world = delivery_tests.Vc6BatchChainTests.vc5_complete_world
    set_control = delivery_tests.Vc6BatchChainTests.set_control
    assert_status = delivery_tests.Vc6BatchChainTests.assert_status
    delivery_calls = delivery_tests.Vc6BatchChainTests.delivery_calls
    assert_batch_failed_and_reconcile = delivery_tests.Vc6BatchChainTests.assert_batch_failed_and_reconcile

    def dispatch_vc5(self, world: dict[str, Any], sequence: int, action: dict[str, Any], *,
                     command_cursor: str | None = None, command_key: str | None = None) -> tuple[int, dict | None, str]:
        """当前候选 revision 的真实 VC-5 批次，输入接缝仅由 world 的配置决定。"""

        campaign = world['campaign']
        action_plan = self.root / 'action-plans' / f'vc-5-{sequence:04d}.json'
        document = batch_tests.batch.action_plan([action], phase='VC-5')
        binding_name = 'late-action-plan:' + str(command_cursor) + ':' + str(command_key)
        saved_input = stops.load_state(binding_name) if command_cursor is not None else None
        if saved_input is not None:
            self.assertEqual(json.loads(stops.read_cut(saved_input['binding'])), document,
                'VC-5 原逻辑命令的 action plan 已改变')
        elif command_cursor is not None and stops.load_state('command-step:' + command_cursor + ':' + str(command_key)) is not None:
            raise ValueError('VC-5 原逻辑命令缺副作用前 action plan 原件')
        argv = ['compile-and-run-vc-batch', '--campaign-dir', campaign, '--phase', 'VC-5',
            '--sequence', sequence, '--predecessor-checkpoint', vc.vc_checkpoint_path(campaign, 'VC-4', revision=world.get('revision', 1)),
            '--action-plan', action_plan, '--state-dir', self.state_dir]

        def invoke() -> tuple[int, dict | None, str]:
            chain._write_json(action_plan, document)
            if command_cursor is not None:
                stops.save_state(binding_name, {'binding': stops.file_cut(action_plan)})
            variables = chain.subprocess_environment(self.root / 'inject-vc5', world['config_path'], [str(action['action_id'])])
            with mock.patch.dict(os.environ, variables):
                return batch_tests.run_main(argv)

        if command_cursor is not None:
            if command_key is None:
                raise ValueError('VC-5 停点命令缺少明示逻辑 key')
            return stops.command_step(self, command_cursor, command_key, argv, invoke, campaign=campaign)
        return invoke()

    def compare_action(self, campaign: Path, *, candidate_id: str = CANDIDATE) -> dict[str, Any]:
        return batch_tests.batch.managed_action('compare', 'VC-5:compare',
            ['compare', '--campaign-dir', campaign, '--candidate-id', candidate_id], item_ids=['compare'])

    def accept_action(self, campaign: Path, receipt: str, gate_root: Path, *, candidate_id: str = CANDIDATE) -> dict[str, Any]:
        return batch_tests.batch.managed_action('acceptance', 'VC-5:acceptance',
            ['accept', '--campaign-dir', campaign, '--candidate-id', candidate_id, '--external-gate-root', gate_root,
             '--external-gate-receipt', receipt], item_ids=['acceptance'])

    def gate_receipt(self, world: dict[str, Any], name: str, *, failed_gate: str | None = None) -> str:
        # 连续链在父进程与独立 reader 中消费同一完整环境合同；不把旧占位收据送入交付。
        environment_mode = chain.gate_environment_mode(json.loads(world['config_path'].read_bytes()))
        if world.get('real_candidate'):
            self.assertEqual(environment_mode, chain.REAL_GATE_ENVIRONMENT, '连续交付链必须实读完整环境收据')
        with chain.seams(world['config_path']):
            identity = compare.candidate_gate_identity(world['campaign'], world['manifest'], world.get('candidate_id', CANDIDATE), world['build'])
            subject = chain.expected_subject(world['manifest'], world['candidate_payload'], identity)
            host = delivery_tests.compare_tests.HOST
            architecture = world['architecture']
            if world.get('real_candidate'):
                # 真实候选的收据坐标取当前封存执行计划，不能沿用合成世界的固定宿主名。
                reference = world['stage']['store']['validation_execution_plan_ref']
                execution = world['store'].load_object(reference)['payload']
                coordinates = {(gate['host_id'], gate['architecture']) for gate in execution['external_gates']}
                self.assertEqual(len(coordinates), 1, '本夹具只支持同一执行宿主与架构的门禁集合')
                host, architecture = next(iter(coordinates))
            return chain.gate_receipt_file(world['gate_root'], name=name, subject=subject,
                host=host, architecture=architecture, failed_gate=failed_gate, environment_mode=environment_mode)

    def assert_recorded_client_requests(self, world: dict[str, Any]) -> None:
        """真实候选世界按同 Campaign 总 provenance 核算，原合成世界保留原来的两请求断言。"""

        if world.get('real_candidate'):
            self.assert_campaign_request_accounting(world['campaign'], expected_client_requests=world['expected_client_requests'])
        else:
            delivery_tests.Vc6BatchChainTests.assert_recorded_client_requests(self, world)

    def run_action(self, world: dict[str, Any]) -> dict[str, Any]:
        return world["typed_delivery"].run_action()

    def candidate_status(self, world: dict[str, Any]) -> dict[str, Any]:
        with chain.seams(world["config_path"]):
            code, result, stderr = batch_tests.run_main(['status', '--campaign-dir', world['campaign'],
                '--candidate-id', world.get('candidate_id', CANDIDATE)])
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(result, stderr)
        return result

    def dispatch_vc6(self, world: dict[str, Any], sequence: int, action: dict[str, Any], *, crash_after_receipt: bool = False,
                     command_cursor: str | None = None, command_key: str | None = None,
                     control_fields: dict[str, Any] | None = None) -> tuple[int, dict | None, str]:
        """同一父批次入口；只给 finalize 动作的 usercustomize 增加一次性强杀点。"""

        campaign = world["campaign"]
        plan = self.root / "action-plans" / f"vc-6-{sequence:04d}.json"
        document = delivery.vc6_action_plan(action)
        binding_name = 'late-action-plan:' + str(command_cursor) + ':' + str(command_key)
        saved_input = stops.load_state(binding_name) if command_cursor is not None else None
        if saved_input is not None:
            self.assertEqual(json.loads(stops.read_cut(saved_input['binding'])), document,
                'VC-6 原逻辑命令的 action plan 已改变')
        elif command_cursor is not None and stops.load_state('command-step:' + command_cursor + ':' + str(command_key)) is not None:
            raise ValueError('VC-6 原逻辑命令缺副作用前 action plan 原件')
        argv = ["compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", "VC-6", "--sequence", sequence,
            "--predecessor-checkpoint", vc.vc_checkpoint_path(campaign, "VC-5", revision=world.get("revision", 1)),
            "--action-plan", plan, "--state-dir", self.state_dir]

        def invoke() -> tuple[int, dict | None, str]:
            if control_fields is not None:
                self.set_control(world, **control_fields)
            chain._write_json(plan, document)
            if command_cursor is not None:
                stops.save_state(binding_name, {'binding': stops.file_cut(plan)})
            injection = self.root / "inject-vc6"
            selected = [str(action["action_id"])]
            variables = {
                **chain.subprocess_environment(injection, world["config_path"], selected),
                **environment.subprocess_environment(injection, self.control, selected),
            }
            # 新进程只绑定原世界来源；重验已完成命令时不重写注入文件。
            (injection / "usercustomize.py").write_text(chain.USERCUSTOMIZE + environment.USERCUSTOMIZE, encoding="utf-8")
            if crash_after_receipt:
                self.assertEqual(action["action_id"], delivery.FINALIZE_ACTION_ID)
                with (injection / "usercustomize.py").open("a", encoding="utf-8") as stream:
                    stream.write(FINALIZE_CRASH)
                variables[CRASH_MARKER_ENV] = str(self.root / "finalize-crashed-once")
            with mock.patch.dict(os.environ, variables), \
                    environment.action_diagnostics(sequence, action, variables, self.control) as output_path:
                outcome = batch_tests.run_main(argv)
            if command_cursor is not None and stops.record_root() is not None:
                stops.save_state('vc6-diagnostic:' + command_cursor + ':' + str(command_key), {
                    'path': output_path, 'binding': stops.file_cut(output_path)
                    if output_path is not None and output_path.exists() else None,
                })
                return outcome
            return environment.diagnostic_outcome(outcome, output_path)

        if command_cursor is not None:
            if command_key is None:
                raise ValueError('VC-6 停点命令缺少明示逻辑 key')
            outcome = stops.command_step(self, command_cursor, command_key, argv, invoke, campaign=campaign)
            if stops.record_root() is None:
                return outcome
            diagnostic = stops.load_state('vc6-diagnostic:' + command_cursor + ':' + command_key)
            if diagnostic is None:
                raise ValueError('VC-6 原命令缺动作诊断停点，不得从新输出补原件')
            if diagnostic['binding'] is not None:
                stops.read_cut(diagnostic['binding'])
            elif diagnostic['path'] is not None and diagnostic['path'].exists():
                raise ValueError('VC-6 原命令未执行动作，其诊断坐标后来出现了新输出')
            return environment.diagnostic_outcome(outcome, diagnostic['path'])
        return invoke()

    def late_audit(self, name: str, world: dict[str, Any]) -> Any:
        """共用六项审计；每个故障在同一 Campaign 的当前可信状态上取快照。"""

        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        campaign = world["campaign"]
        # 同次执行的后续停点仍用第一次取的 before；新进程由原审计停点重验。
        audits = self.__dict__.setdefault('_late_audits', {})
        key = (str(campaign), name)
        if key in audits:
            return audits[key]
        control = campaign / "control" / "vc"
        audit = ScenarioAudit(self, name)
        audit.snapshot(
            trusted=[campaign / "campaign.json", control / "commits", control / "batches",
                     *sorted(control.rglob("*-checkpoint.json")), *world.get("trusted_roots", [])],
            history=[world["store"].root, self.ledger_dir, self.project_root,
                     control / "commits", control / "batches", *world.get("trusted_roots", [])],
            timing_dir=self.ledger_dir, project_root=self.project_root,
        )
        audits[key] = audit
        return audit

    def finish_audit(self, audit: Any, *, executed: list[str], expected: list[str], entry: Any,
                     fragments: tuple[str, ...], repeats: list[tuple[str, Any, Any]] | None = None, **facts: Any) -> dict[str, Any]:
        audit.check_trusted()
        control = environment._control(self.control)
        audit.check_closure(executed=executed, expected=expected, what="当前 Campaign 的交付动作",
            source=audit.delivery_closure_source(Path(control['calls']), len(self.delivery_calls()) - len(executed)))
        audit.check_accounting(repeats=repeats or [])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(entry, *fragments)
        return audit.report(**facts)

    def revalidate_completed_method(self, expected: list[str]) -> list[dict[str, Any]]:
        """原方法内部只读承接：原世界与活守卫齐全时，重验指定后段场景的原件。"""

        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        allowed = {'后段同根因暂停并续派 compare', '后段预算到期并延期续派 builder',
            '后段批次 COMMIT 发布中断', '后段交付修复后动作中断', '后段交付阶段批准失效',
            '后段交付收据后中断与重复收尾'}
        if not expected or len(set(expected)) != len(expected) or not set(expected) <= allowed:
            raise ValueError('完成方法重验只接受本后段原场景，不能拼入其它来源')
        locator = stops.load_state('late-delivery-locator')
        if locator is None or not os.environ.get(stops.METHOD_RESUME_ENV):
            raise ValueError('完成后段方法缺原世界定位，禁止重建或追认旧摘要')
        state = stops.resume_checkpoint(self, locator['cursor'])
        if state is None or state['phase'] != 'done':
            raise ValueError('完成后段方法没有原 done 游标')
        scope = self.source_context() if hasattr(self, 'source_context') else contextlib.nullcontext()
        with scope:
            world = stops.restore_world(self, locator['world'])
            with chain.seams(world['config_path']):
                fixture = environment.TypedDeliveryFixture(world['campaign'], world['candidate_id'])
                fixture.__enter__()
                try:
                    self.control = fixture.control_path
                    self.assertEqual(self.control, state['control'], '完成方法的原 typed 坐标已改变')
                    records = [ScenarioAudit.revalidate(self, scenario) for scenario in expected]
                finally:
                    # 六项重验结束仍保留原活守卫，整体链的最终消费者负责有界终结。
                    fixture.close()
        return records

    def exercise_delivery_faults(self, world: dict[str, Any] | None = None, *, sequence_start: int = 12) -> list[dict[str, Any]]:
        """同一世界连续执行批次提交故障、阶段失败、动作强杀、阶段失效及收据后强杀。"""

        if world is None and os.environ.get(stops.METHOD_RESUME_ENV):
            raise ValueError('交付恢复必须交入原 world，禁止重新调用 vc5_complete_world 建场')
        world = self.vc5_complete_world() if world is None else world
        # 资格准备、fork 的独立守卫和父进程后处理共读同份冻结来源配置。
        # 作用域结束恢复原登记；不替换正式布局或资格读侧。
        with chain.seams(world["config_path"]):
            return self._exercise_delivery_faults(world, sequence_start=sequence_start)

    def _exercise_delivery_faults(self, world: dict[str, Any], *, sequence_start: int) -> list[dict[str, Any]]:
        """原四场景逐停点继续；命令原件、调用日志和故障前状态都不重新生成。"""

        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        campaign = world["campaign"]
        self.campaign = campaign
        candidate = world.get("candidate_id", CANDIDATE)
        revision = world.get("revision", 1)
        cursor = f'late-delivery:{campaign}:{sequence_start}'
        typed_name = 'typed-vc6:' + str(campaign.resolve())
        state = stops.resume_checkpoint(self, cursor)
        if state is None:
            if os.environ.get(stops.METHOD_RESUME_ENV) and (
                    stops.load_state('audit-before:后段批次 COMMIT 发布中断') is not None
                    or stops.load_state('late-delivery-locator') is not None):
                raise ValueError('原交付场景缺游标，禁止从当前现场重建故障前态')
            state = {'phase': 'world-save' if world.get('real_candidate') else 'typed-initialize',
                'campaign': campaign, 'candidate': candidate,
                'revision': revision, 'sequence_start': sequence_start, 'records': [], 'audit_snapshots': {},
                'initializations': {typed_name: 'uninitialized'}}
            stops.checkpoint(self, cursor, **state)
        self.assertEqual((state['campaign'], state['candidate'], state['revision'], state['sequence_start']),
                         (campaign, candidate, revision, sequence_start), '原交付游标坐标已改变')
        if state['phase'] not in {'world-save', 'typed-initialize', 'commit-begin', 'commit-fault', 'commit-retry', 'commit-audit',
                                 'kill-begin', 'kill-dispatch', 'kill-reconcile', 'kill-resume', 'kill-audit',
                                 'invalidation-begin', 'invalidation-preview', 'invalidation-apply',
                                 'invalidation-resume', 'invalidation-audit', 'receipt-begin', 'receipt-kill',
                                 'receipt-reconcile', 'receipt-resume', 'finalized-read-1', 'finalized-read-2',
                                 'receipt-audit', 'done'}:
            raise ValueError('原交付游标阶段不在本四场景合同中，禁止猜测下一动作')
        if state['phase'] == 'world-save':
            self.assertTrue(world.get('real_candidate'), '不能拿旧合成夹具追造可恢复现场')
            if stops.load_state('world:late-delivery') is None:
                stops.save_world('late-delivery', world)
            else:
                original_world = stops.restore_world(self, 'late-delivery')
                self.assertEqual((original_world['campaign'], original_world['candidate_id'], original_world['revision']),
                                 (campaign, candidate, revision), '原交付世界坐标已改变')
                world.clear()
                world.update(original_world)
            locator = {'cursor': cursor, 'world': 'late-delivery'}
            saved_locator = stops.load_state('late-delivery-locator')
            if saved_locator is None:
                stops.save_state('late-delivery-locator', locator)
            else:
                self.assertEqual(saved_locator, locator, '原交付定位已改变')
            state['phase'] = 'typed-initialize'
            stops.checkpoint(self, cursor, **state)

        if 'typed_delivery' not in world:
            # 首次建场须由原阶段明确许可；已有现场只绑定同一活守卫，不能另启实例。
            if state['phase'] == 'typed-initialize' and stops.load_state(typed_name) is None:
                with stops.initialization_scope(typed_name, cursor=cursor, phase='typed-initialize'):
                    fixture = environment.TypedDeliveryFixture(campaign, candidate)
                    fixture.__enter__()
            else:
                fixture = environment.TypedDeliveryFixture(campaign, candidate)
                fixture.__enter__()
            self.addCleanup(fixture.close)
            world['typed_delivery'] = fixture
        self.control = world['typed_delivery'].control_path
        if state['phase'] == 'typed-initialize':
            state.update(phase='commit-begin', control=self.control, initializations={typed_name: 'completed'})
            stops.checkpoint(self, cursor, **state)
        self.assertEqual(self.control, state['control'], '原 typed 控制坐标已改变')
        run = self.run_action(world)

        # 第一层提交故障：同序号两次命令有各自的逻辑 key。
        scenario = '后段批次 COMMIT 发布中断'
        if state['phase'] == 'commit-begin':
            state['audit_snapshots'].setdefault(scenario, 'uninitialized')
            stops.checkpoint(self, cursor, **state)
            self.late_audit(scenario, world)
            state['audit_snapshots'][scenario] = 'recorded'
            state['phase'] = 'commit-fault'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'commit-fault':
            original_publish = upgrade._publish_staging_file
            calls = 0

            def fail_commit(source: Path, target: Path, root: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise upgrade.ConfigurationError('后段连续链注入：COMMIT 发布失败')
                original_publish(source, target, root)

            with mock.patch.object(upgrade, '_publish_staging_file', side_effect=fail_commit):
                code, result, stderr = self.dispatch_vc6(world, sequence_start, run,
                    command_cursor=cursor, command_key='commit-publication-fault')
            self.assertEqual((code, result), (1, None), stderr)
            self.assertIn('COMMIT 发布失败', stderr)
            self.assertEqual(self.delivery_calls(), [])
            state['phase'] = 'commit-retry'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'commit-retry':
            retried = self.assert_batch_passed(self.dispatch_vc6(world, sequence_start, run,
                command_cursor=cursor, command_key='commit-redispatch',
                control_fields={'failures': [['dr0', 'candidate_restored', None]]}), sequence_start, 'VC-6')
            self.assertEqual(retried['staging_attempt'], 2)
            state.update(phase='commit-audit', failed=self.assert_status(world, 'delivery_stage_failed'))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'commit-audit':
            record = (ScenarioAudit.revalidate(self, scenario) if os.environ.get(stops.METHOD_RESUME_ENV)
                and stops.load_state('audit-complete:' + scenario) is not None else self.finish_audit(
                    self.late_audit(scenario, world), executed=[f'{a}:{b}' for a, b in self.delivery_calls()],
                    expected=[f'dr0:{stage}' for stage in delivery.STAGES[:3]], entry=state['failed'],
                    fragments=RUN_ENTRY_FRAGMENTS, campaign_id=campaign.name, staging_attempt=2))
            state['records'].append(record)
            state['phase'] = 'kill-begin'
            stops.checkpoint(self, cursor, **state)

        # prepare 前强杀与对账独立留停点；对账完成之前不能跳到下一批。
        scenario = '后段交付修复后动作中断'
        if state['phase'] == 'kill-begin':
            state['audit_snapshots'].setdefault(scenario, 'uninitialized')
            stops.checkpoint(self, cursor, **state)
            self.late_audit(scenario, world)
            state['audit_snapshots'][scenario] = 'recorded'
            state.update(phase='kill-dispatch', before_calls=len(self.delivery_calls()),
                physical_before=len(environment.read_calls(self.control, physical=True)))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'kill-dispatch':
            code, result, stderr = self.dispatch_vc6(world, sequence_start + 1, run,
                command_cursor=cursor, command_key='delivery-action-killed',
                control_fields={'kill_at': ['dr1', 'stable_observed']})
            self.assertIsNotNone(result, stderr)
            self.assertEqual((code, result['campaign_run']['reason']), (1, f'action-failed:{delivery.RUN_ACTION_ID}'), stderr)
            head = timing.inspect_ledger(self.ledger_dir)
            self.assertEqual((head['status'], head['active_phase']), ('recovery_required', 'VC-6'))
            state.update(phase='kill-reconcile', failed_run=Path(result['campaign_run']['run_dir']))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'kill-reconcile':
            argv = ['reconcile-supervisor-run', '--campaign-dir', campaign, '--run-dir', state['failed_run']]
            code, reconciled, stderr = stops.command_step(self, cursor, 'delivery-action-reconcile', argv,
                lambda: batch_tests.run_main(argv), campaign=campaign)
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self.assertIn('逐字重派同一批次', reconciled['next_command'])
            state.update(phase='kill-resume', interrupted=self.assert_status(world, 'delivery_in_progress'))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'kill-resume':
            self.assert_batch_passed(self.dispatch_vc6(world, sequence_start + 2, run,
                command_cursor=cursor, command_key='delivery-action-redispatch', control_fields={}), sequence_start + 2, 'VC-6')
            self.assert_status(world, 'delivery_awaiting_finalize')
            self.assertEqual(environment.read_calls(self.control, physical=True)[state['physical_before']:],
                [('dr1', 'candidate_restored'), ('dr1', 'stable_observed')])
            state['phase'] = 'kill-audit'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'kill-audit':
            record = (ScenarioAudit.revalidate(self, scenario) if os.environ.get(stops.METHOD_RESUME_ENV)
                and stops.load_state('audit-complete:' + scenario) is not None else self.finish_audit(
                    self.late_audit(scenario, world),
                    executed=[f'{a}:{b}' for a, b in self.delivery_calls()[state['before_calls']:]],
                    expected=['dr1:candidate_restored', 'dr1:stable_observed', 'dr1:stable_observed'],
                    entry=state['interrupted'], fragments=RUN_ENTRY_FRAGMENTS, campaign_id=campaign.name))
            state['records'].append(record)
            state['phase'] = 'invalidation-begin'
            stops.checkpoint(self, cursor, **state)

        scenario = '后段交付阶段批准失效'
        arguments = ['--campaign-dir', campaign, '--candidate-id', candidate, '--stage', 'rollback_verified',
                     '--reason', '回退检查在相同冻结环境中重新核验']
        if state['phase'] == 'invalidation-begin':
            state['audit_snapshots'].setdefault(scenario, 'uninitialized')
            stops.checkpoint(self, cursor, **state)
            self.late_audit(scenario, world)
            state['audit_snapshots'][scenario] = 'recorded'
            state.update(phase='invalidation-preview', before_calls=len(self.delivery_calls()))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'invalidation-preview':
            argv = ['delivery-stage-invalidate', 'preview', *arguments]
            code, preview, stderr = stops.command_step(self, cursor, 'stage-invalidation-preview', argv,
                lambda: batch_tests.run_main(argv), campaign=campaign)
            self.assertEqual((code, preview['status']), (2, 'preview'), stderr)
            state.update(phase='invalidation-apply', preview=preview)
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'invalidation-apply':
            argv = ['delivery-stage-invalidate', 'apply', *arguments,
                '--approve-sha256', state['preview']['review_sha256'], '--approved-by', 'fixture-reviewer']
            code, applied, stderr = stops.command_step(self, cursor, 'stage-invalidation-apply', argv,
                lambda: batch_tests.run_main(argv), campaign=campaign)
            self.assertEqual((code, applied['status']), (0, 'applied'), stderr)
            state.update(phase='invalidation-resume', pending=self.assert_status(world, 'delivery_invalidation_pending'))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'invalidation-resume':
            self.assert_batch_passed(self.dispatch_vc6(world, sequence_start + 3, run,
                command_cursor=cursor, command_key='invalidated-stage-redispatch'), sequence_start + 3, 'VC-6')
            state.update(phase='invalidation-audit', ready=self.assert_status(world, 'delivery_awaiting_finalize'))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'invalidation-audit':
            record = (ScenarioAudit.revalidate(self, scenario) if os.environ.get(stops.METHOD_RESUME_ENV)
                and stops.load_state('audit-complete:' + scenario) is not None else self.finish_audit(
                    self.late_audit(scenario, world),
                    executed=[f'{a}:{b}' for a, b in self.delivery_calls()[state['before_calls']:]],
                    expected=[f'dr2:{stage}' for stage in delivery.STAGES[1:]], entry=state['pending'],
                    fragments=RUN_ENTRY_FRAGMENTS, campaign_id=campaign.name))
            state['records'].append(record)
            state['phase'] = 'receipt-begin'
            stops.checkpoint(self, cursor, **state)

        # 已签收据后只补收尾；两个相同 argv 的幂等读取仍是两条真实命令。
        scenario = '后段交付收据后中断与重复收尾'
        if state['phase'] == 'receipt-begin':
            state['audit_snapshots'].setdefault(scenario, 'uninitialized')
            stops.checkpoint(self, cursor, **state)
            self.late_audit(scenario, world)
            state['audit_snapshots'][scenario] = 'recorded'
            state.update(phase='receipt-kill', before_calls=len(self.delivery_calls()),
                finalize=delivery.finalize_action(campaign, candidate, state['ready']['delivery']['review_sha256']))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'receipt-kill':
            code, result, stderr = self.dispatch_vc6(world, sequence_start + 4, state['finalize'], crash_after_receipt=True,
                command_cursor=cursor, command_key='receipt-issued-finalize-killed')
            self.assertIsNotNone(result, stderr)
            self.assertEqual((code, result['campaign_run']['reason']), (1, f'action-failed:{delivery.FINALIZE_ACTION_ID}'), stderr)
            head = timing.inspect_ledger(self.ledger_dir)
            self.assertEqual((head['status'], head['active_phase']), ('recovery_required', 'VC-6'))
            state.update(phase='receipt-reconcile', failed_run=Path(result['campaign_run']['run_dir']))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'receipt-reconcile':
            argv = ['reconcile-supervisor-run', '--campaign-dir', campaign, '--run-dir', state['failed_run']]
            code, reconciled, stderr = stops.command_step(self, cursor, 'receipt-issued-finalize-reconcile', argv,
                lambda: batch_tests.run_main(argv), campaign=campaign)
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self.assertIn('逐字重派同一批次', reconciled['next_command'])
            self.assertTrue((self.root / 'finalize-crashed-once').is_file())
            receipt_refs = world['store'].list_receipt_refs('candidate_delivery')
            self.assertEqual(len(receipt_refs), 1)
            self.assertFalse(vc.vc_checkpoint_path(campaign, 'VC-6', revision=revision).exists())
            state.update(phase='receipt-resume', receipt_refs=receipt_refs,
                interrupted=self.assert_status(world, 'delivery_completion_pending'))
            stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'receipt-resume':
            self.assert_batch_passed(self.dispatch_vc6(world, sequence_start + 5, state['finalize'],
                command_cursor=cursor, command_key='receipt-issued-finalize-redispatch'), sequence_start + 5, 'VC-6')
            checkpoint = vc.vc_checkpoint_path(campaign, 'VC-6', revision=revision)
            self.assertEqual(world['store'].list_receipt_refs('candidate_delivery'), state['receipt_refs'])
            state.update(phase='finalized-read-1', checkpoint_bytes=checkpoint.read_bytes(),
                before_repeat=self.late_audit(scenario, world).accounting_state())
            stops.checkpoint(self, cursor, **state)
        for index in (1, 2):
            if state['phase'] == f'finalized-read-{index}':
                argv = ['deliver-candidate', 'finalize', '--campaign-dir', campaign, '--candidate-id', candidate]
                code, repeated, stderr = stops.command_step(self, cursor, f'finalized-read-{index}', argv,
                    lambda: batch_tests.run_main(argv), campaign=campaign)
                self.assertEqual((code, repeated['status']), (0, 'finalized'), stderr)
                state['phase'] = 'finalized-read-2' if index == 1 else 'receipt-audit'
                stops.checkpoint(self, cursor, **state)
        if state['phase'] == 'receipt-audit':
            self.assertEqual(vc.vc_checkpoint_path(campaign, 'VC-6', revision=revision).read_bytes(), state['checkpoint_bytes'])
            audit = self.late_audit(scenario, world)
            record = (ScenarioAudit.revalidate(self, scenario) if os.environ.get(stops.METHOD_RESUME_ENV)
                and stops.load_state('audit-complete:' + scenario) is not None else self.finish_audit(
                    audit, executed=[f'{a}:{b}' for a, b in self.delivery_calls()[state['before_calls']:]], expected=[],
                    entry=state['interrupted'], fragments=('逐字重派', 'finalize'),
                    repeats=[('重复读取已完成 finalize', state['before_repeat'], audit.accounting_state())],
                    campaign_id=campaign.name, receipt_sha256=state['receipt_refs'][0]['sha256']))
            state['records'].append(record)
            state['phase'] = 'done'
            stops.checkpoint(self, cursor, **state)
        if state['phase'] != 'done':
            raise ValueError('原交付游标阶段不在本四场景合同中，禁止猜测下一动作')
        records = ([ScenarioAudit.revalidate(self, row['scenario']) for row in state['records']]
            if os.environ.get(stops.METHOD_RESUME_ENV) else state['records'])
        self.assertEqual(len(records), 4)
        self.assert_status(world, 'ready_for_operator_release')
        self.assert_recorded_client_requests(world)
        self.assertEqual(len({row['campaign_id'] for row in records}), 1)
        return records
