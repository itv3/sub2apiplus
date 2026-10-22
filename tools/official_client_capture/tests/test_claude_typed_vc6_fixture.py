"""共用 typed 夹具的输入拒绝和 armed 后强杀；重型整链另行排队。

轻量组核原 Store 原件保留、缺配置先于写入拒绝和正式登记不泄漏。
恢复组走既有组合世界的真实 Store、独立守卫与发布流程，不下调策略版本。
"""

from __future__ import annotations

import argparse
import copy
import json
import multiprocessing
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import claude_typed_vc6_fixture as fixture
from tools.official_client_capture.tests import test_claude_real_delivery_integration as integration_fixture
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256
from tools.official_client_control.tests.fixtures import SyntheticCampaign


class TypedFixtureInputTests(unittest.TestCase):
    """不启动守卫、Docker、账号或整链，只核允许接入的测试输入。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="typed-input-", dir="/private/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        os.chown(self.root, -1, os.getegid())

    def test_guard_diagnostics_preserves_exception_and_private_original(self) -> None:
        engine = mock.create_autospec(guard.GuardEngine, instance=True)
        original = RuntimeError("测试守卫底层异常")
        engine.prepare.side_effect = original
        operation = engine.prepare
        with mock.patch.dict(os.environ, {fixture.DIAGNOSTIC_ROOT_ENV: str(self.root)}):
            with fixture.guard_diagnostics(engine), self.assertRaises(RuntimeError) as caught:
                engine.prepare({"request": "不写入日志"}, {"owner": "不写入日志"})
        self.assertIs(caught.exception, original)
        self.assertIs(engine.prepare, operation)
        records = list(self.root.glob("guard-prepare-*/exception.json"))
        self.assertEqual(len(records), 1)
        record = guard.read_json(records[0])
        self.assertEqual(record["exception_type"], "RuntimeError")
        self.assertEqual(record["message"], str(original))
        self.assertIn("RuntimeError: 测试守卫底层异常", record["traceback"])
        self.assertNotIn("不写入日志", records[0].read_text())
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)

    def test_guard_diagnostics_keeps_success_and_default_behavior(self) -> None:
        engine = mock.create_autospec(guard.GuardEngine, instance=True)
        result = {"original": True}
        engine.status.return_value = result
        operation = engine.status
        with mock.patch.dict(os.environ, {fixture.DIAGNOSTIC_ROOT_ENV: str(self.root)}):
            with fixture.guard_diagnostics(engine):
                self.assertIs(engine.status(), result)
        self.assertEqual(list(self.root.iterdir()), [])
        with mock.patch.dict(os.environ, {fixture.DIAGNOSTIC_ROOT_ENV: ""}):
            with fixture.guard_diagnostics(engine):
                self.assertIs(engine.status, operation)

    def test_guard_diagnostic_write_failure_does_not_mask_original(self) -> None:
        engine = mock.create_autospec(guard.GuardEngine, instance=True)
        original = RuntimeError("保留此异常")
        engine.run.side_effect = original
        for failure in (OSError("日志失败"), guard.GuardError("日志原件非法"), execution.ControlError("规范化失败")):
            with self.subTest(failure=type(failure).__name__), mock.patch.dict(os.environ, {fixture.DIAGNOSTIC_ROOT_ENV: str(self.root)}):
                with fixture.guard_diagnostics(engine), mock.patch.object(fixture.guard, "write_json_once", side_effect=failure):
                    with self.assertRaises(RuntimeError) as caught:
                        engine.run({})
            self.assertIs(caught.exception, original)

    def test_same_store_constructor_preserves_original_campaign_and_external_bytes(self) -> None:
        original = SyntheticCampaign(self.root, schema="v2")
        original.bootstrap_and_campaign()
        before = {path: path.read_bytes() for path in original.external_root.rglob("*") if path.is_file()}
        original_campaign = canonical_json_bytes(original.store.load_campaign(original.campaign_id))
        rollback_root = self.root / "rollback"
        rollback_root.mkdir(mode=0o700)
        rollback = SyntheticCampaign(rollback_root, schema="v2", store=original.store,
                                     external_root=original.external_root, external_prefix="rollback-source")
        rollback.campaign_id = "independent-rollback-campaign"
        rollback.bootstrap_and_campaign(platforms=["linux/arm64"], campaign_purpose="validation_only")
        self.assertIs(rollback.store, original.store)
        self.assertEqual(rollback.external_root, original.external_root)
        self.assertEqual(canonical_json_bytes(original.store.load_campaign(original.campaign_id)), original_campaign)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertTrue(all(binding["path"].startswith("rollback-source/") for binding in
                            (rollback.contract_binding, rollback.official_binding, rollback.private_binding)))
        self.assertGreaterEqual(rollback.tool_identity["policy_version"], 4)

        # 沿原 UID 覆盖首次初始化中断：复用基础文件和同 Store 的已封存资格，
        # 不再调用任何建场生产器；只有原游标明确许可的未启动现场可以承接。
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        configuration, _context, environment = fixture_world(self.root)
        candidate_qualification = copy.deepcopy(configuration['candidate']['qualification'])
        campaign = self.root / 'evidence/campaigns/original-typed-campaign'
        guard.private_directory(campaign)
        control_path = self.root / 'control/recorded-delivery-control.json'
        qualified_root = self.root / 'control/typed-vc6-rollback'
        guard.private_directory(qualified_root)
        qualified = integration_fixture.DeliveryCampaign(qualified_root, schema='v2', store=original.store,
            external_root=self.root, external_prefix='control/typed-vc6-rollback/external')
        qualified.materials = configuration
        qualified.campaign_id = 'typed-rollback-' + canonical_sha256(candidate_qualification)[:32]
        qualified.bootstrap_and_campaign(platforms=['linux/arm64'], campaign_purpose='validation_only')
        qualified.discovery_and_evidence()
        qualified.profile_objects()
        qualified.select_profile('rollback')
        qualified.profile_approve('validation_only')
        qualified.strict_validate(tag='baseline')
        sealed_path = qualified_root / 'sealed-result.json'
        guard.write_json_once(sealed_path, {'schema_version': 'claude-upgrade-acceptance/v1',
            'candidate_id': 'synthetic-candidate-baseline', 'production_state': 'not_activated',
            'store': {'acceptance_ref': qualified.references['acceptance-baseline'],
                'validation_completed_ref': qualified.references['validation-completed-baseline']}})
        record_name = 'typed-vc6:' + str(campaign)
        records = self.root / 'records'
        records.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: str(records), stops.METHOD_RESUME_ENV: '1'}):
            stops.save_state('cursor:typed-input', {'phase': 'VC6',
                'initializations': {record_name: 'uninitialized'}})
            with self.assertRaisesRegex(guard.GuardError, '游标许可'):
                fixture._read_unstarted_fixture_world(self.root, campaign, control_path, record_name)
            # 原许可已签 started 后中断，只读现场检查才可承接；缺检查或检查失败都保留原件。
            with self.assertRaisesRegex(RuntimeError, '首次初始化中断'):
                with stops.initialization_scope(record_name, cursor='typed-input', phase='VC6'):
                    raise RuntimeError('首次初始化中断')
            initial_path = stops._state_path('initialization:' + record_name)
            initial_raw = initial_path.read_bytes()
            with self.assertRaisesRegex(ValueError, '首次初始化许可'):
                with stops.initialization_scope(record_name, cursor='typed-input', phase='VC6'):
                    self.fail('缺只读现场检查不得续作')
            rejected_check = mock.Mock(side_effect=guard.GuardError('现场检查拒绝'))
            with self.assertRaisesRegex(guard.GuardError, '现场检查拒绝'):
                with stops.initialization_scope(record_name, cursor='typed-input', phase='VC6',
                        resume_check=rejected_check):
                    self.fail('检查拒绝后不得调用构造器')
            rejected_check.assert_called_once_with()
            self.assertEqual(initial_path.read_bytes(), initial_raw)
            self.assertFalse(stops.initialization_allowed(record_name))
            # 同时核游标许可与 started 的准确归属；completed、外来名称和额外字段均拒绝。
            started = stops.load_state('initialization:' + record_name)
            rejected = [{**started, 'cursor': 'other'}, {**started, 'phase': 'other'},
                        {**started, 'state': 'completed'}, {**started, 'extra': True}]
            for invalid in rejected:
                with self.subTest(initialization=invalid):
                    stops.save_state('initialization:' + record_name, invalid)
                    check = mock.Mock()
                    try:
                        with self.assertRaisesRegex(ValueError, '首次初始化许可'):
                            with stops.initialization_scope(record_name, cursor='typed-input', phase='VC6',
                                    resume_check=check):
                                self.fail('外来或完成的初始化不得进入动作体')
                        check.assert_not_called()
                    finally:
                        initial_path.write_bytes(initial_raw)
            cursor_path = stops._state_path('cursor:typed-input')
            cursor_raw = cursor_path.read_bytes()
            stops.save_state('cursor:typed-input', {'phase': 'VC6'})
            try:
                with self.assertRaisesRegex(ValueError, '首次初始化许可'):
                    with stops.initialization_scope(record_name, cursor='typed-input', phase='VC6',
                            resume_check=mock.Mock()):
                        self.fail('旧游标缺许可不得追认')
            finally:
                cursor_path.write_bytes(cursor_raw)
            check = lambda: fixture._read_unstarted_fixture_world(self.root, campaign, control_path, record_name)
            with stops.initialization_scope(record_name, cursor='typed-input', phase='VC6', resume_check=check), \
                    mock.patch.object(fixture, 'fixture_world', side_effect=AssertionError('不重建基础世界')), \
                    mock.patch.object(fixture, 'DeliveryCampaign', side_effect=AssertionError('不重建回滚资格')):
                self.assertEqual(initial_path.read_bytes(), initial_raw)
                originals = {path: path.read_bytes() for path in self.root.rglob('*')
                             if path.is_file() and records not in path.parents}
                restored_configuration, restored_environment = fixture._read_unstarted_fixture_world(
                    self.root, campaign, control_path, record_name)
                self.assertEqual(restored_configuration, configuration)
                self.assertEqual(restored_environment, environment)
                restored = fixture._read_initial_rollback_qualification(
                    original.store, self.root, restored_configuration, candidate_qualification)
                self.assertEqual(restored['campaign_id'], qualified.campaign_id)
                self.assertEqual(restored['candidate_ref'], qualified.references['candidate-baseline'])
                self.assertEqual(restored['build_receipt_ref'], qualified.references['candidate-build-receipt-baseline'])
                self.assertEqual(restored['validation_ref'], qualified.references['validation-completed-baseline'])
                self.assertEqual(restored['acceptance_ref'], qualified.references['acceptance-baseline'])
                self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*')
                    if path.is_file() and records not in path.parents}, originals)
                # 集中核拒绝边界，每项只改本测试现场，复验结束恢复相同原字节。
                compose = self.root / configuration['paths']['compose_binding']['path']
                state = self.root / 'state.json'
                source_configuration = self.root / 'inputs/configuration.json'
                mutations = [(compose, compose.read_bytes() + b' '),
                    (state, canonical_json_bytes({**guard.read_json(state), 'switch_count': 1})),
                    (source_configuration, canonical_json_bytes({**configuration, 'instance_id': 'changed-fixture'}))]
                for path, content in mutations:
                    with self.subTest(drift=path.name):
                        previous = path.read_bytes()
                        try:
                            path.write_bytes(content)
                            with self.assertRaises(guard.GuardError):
                                fixture._read_unstarted_fixture_world(self.root, campaign, control_path, record_name)
                        finally:
                            path.write_bytes(previous)
                for path in (self.root / 'guard', control_path.with_name('typed-vc6-guard.log'),
                             campaign / 'control/typed-vc6-configuration.json'):
                    with self.subTest(already_started=path.name):
                        guard.write_json_once(path, {'fixture': '必须拒绝'})
                        try:
                            with self.assertRaisesRegex(guard.GuardError, '发布配置或守卫痕迹'):
                                fixture._read_unstarted_fixture_world(self.root, campaign, control_path, record_name)
                        finally:
                            path.unlink()
                raw = sealed_path.read_bytes()
                try:
                    sealed_path.unlink()
                    with self.assertRaises(guard.GuardError):
                        fixture._read_initial_rollback_qualification(
                            original.store, self.root, restored_configuration, candidate_qualification)
                finally:
                    sealed_path.write_bytes(raw)
                self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*')
                    if path.is_file() and records not in path.parents}, originals)
        self.assertEqual(canonical_json_bytes(original.store.load_campaign(original.campaign_id)), original_campaign)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_same_store_requires_nonempty_unique_source_prefix(self) -> None:
        original = SyntheticCampaign(self.root, schema="v2")
        for options in ({"store": original.store}, {"store": original.store, "external_root": original.external_root},
                        {"external_root": original.external_root, "external_prefix": "rollback"}):
            with self.assertRaises(ValueError):
                SyntheticCampaign(self.root / "unused", schema="v2", **options)
        rollback = SyntheticCampaign(self.root / "unused", schema="v2", store=original.store,
                                     external_root=original.external_root, external_prefix="unique-source")
        with self.assertRaises(FileExistsError):
            rollback._write_external("official/synthetic-cli.bin", "不能覆盖原件".encode("utf-8"))

    def test_current_policy_missing_configuration_rejects_before_any_write(self) -> None:
        campaign = SyntheticCampaign(self.root, schema="v2")
        campaign.bootstrap_and_campaign()
        context = SimpleNamespace(store=campaign.store, campaign=campaign.campaign_document)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        arguments = argparse.Namespace(campaign_dir=self.root, candidate_id="current-candidate",
                                       executor_configuration=None, executor_configuration_sha256=None)
        with mock.patch.object(delivery, "_require_parent_batch"), mock.patch.object(delivery, "_resolve", return_value=context), \
                mock.patch.object(delivery, "_delivery_lock") as write_lock, \
                mock.patch.object(delivery, "_environment_executor") as factory:
            with self.assertRaisesRegex(delivery.ConfigurationError, "缺配置零变更拒绝"):
                delivery._run(arguments)
            write_lock.assert_not_called()
            factory.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertGreaterEqual(campaign.tool_identity["policy_version"], 4)

    def test_formal_source_rejected_before_test_registry_changes(self) -> None:
        configuration, context, _environment = fixture_world(self.root)
        configuration = copy.deepcopy(configuration)
        configuration["source_scope"] = "formal"
        raw_path = self.root / "inputs/forbidden-formal.json"
        guard.write_json_once(raw_path, configuration)
        context["configuration_ref"] = guard.binding(self.root, raw_path)
        control_path = self.root / "control.json"
        guard.write_json_once(control_path, {"schema_version": fixture.CONTROL_SCHEMA, "fixture_only": True,
            "formal_version_semantics_certified": False, "context": context})
        before_factories = dict(delivery.ENVIRONMENT_EXECUTORS)
        before_sources = dict(execution.SOURCE_VERIFIERS)
        with mock.patch.object(executor, "create_executor") as create:
            with self.assertRaisesRegex(guard.GuardError, "不能安装正式执行器"):
                with fixture.source_scope(control_path):
                    self.fail("正式来源不能进入测试登记作用域")
            create.assert_not_called()
        self.assertEqual(delivery.ENVIRONMENT_EXECUTORS, before_factories)
        self.assertEqual(execution.SOURCE_VERIFIERS, before_sources)

    def test_configuration_bound_action_keeps_exact_batch_and_postrun_classification(self) -> None:
        configuration, _context, _environment = fixture_world(self.root)
        campaign = self.root / "evidence/campaigns/typed-current-campaign"
        guard.private_directory(campaign)
        path = campaign / "configuration.json"
        guard.write_json_once(path, configuration)
        binding = guard.binding(self.root, path)
        action = delivery.run_action(campaign, "candidate-current", environment_executor=fixture.EXECUTOR_NAME,
            observation_window_seconds=1, executor_configuration=path, executor_configuration_sha256=binding["sha256"])
        manifest = {"schema_version": supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "client": "claude", "phase": "VC-6",
            "campaign_id": campaign.name, "candidate_id": "candidate-current", "actions": [action],
            "execute_items": [delivery.RUN_ACTION_ID]}
        self.assertEqual(delivery.batch_action_kind(action, manifest), "run")
        self.assertEqual(supervisor.candidate_post_run_recovery_action(manifest, delivery.RUN_ACTION_ID), "post-run")
        raw = path.read_bytes()
        path.write_bytes(raw + b" ")
        self.assertIsNone(delivery.batch_action_kind(action, manifest))
        self.assertIsNone(supervisor.candidate_post_run_recovery_action(manifest, delivery.RUN_ACTION_ID))
        path.write_bytes(raw)
        # 同一个原短验复用独立守卫与真实资格世界，核三种纯读消费者的索引接线。
        # 仅封存读取所需的真实 Freeze/Plan；不派发阶段，旧 guard 冷失效不能换 PID 追认。
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        from tools.official_client_control.tests.fixtures import ManagedToolTree
        from tools.official_client_control.canonical import canonical_sha256
        tree = ManagedToolTree(self.root / 'qualified-managed')
        active = tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        helper, configuration = integration_fixture.qualified_configuration_fixture(self, tree)
        data = helper.root
        qualified_config = data / 'inputs/live-recorded-configuration.json'
        guard.write_json_once(qualified_config, configuration)
        q = configuration['candidate']['qualification']
        context = {**_context, 'data_root': str(data), 'campaign_id': q['campaign_id'],
            'candidate_id': q['candidate_id'], 'delivery_id': q['candidate_id'] + '-d1',
            'configuration_ref': guard.binding(data, qualified_config)}
        state = guard.read_json(data / 'state.json')
        state['environment'].update(configuration_sha256=canonical_sha256(configuration), architecture='linux/arm64',
            environment_sha256=canonical_sha256(integration_fixture.fixture_projection()))
        guard.atomic_json(data / 'state.json', state)
        control_path = data / 'typed-control.json'
        guard.write_json_once(control_path, {'schema_version': fixture.CONTROL_SCHEMA, 'fixture_only': True,
            'formal_version_semantics_certified': False, 'context': context,
            'calls': str(data / 'calls.jsonl'), 'physical_calls': str(data / 'physical.jsonl'),
            'failures': [], 'kill_at': None, 'crash_at': None, 'failed_gates': []})
        records = self.root / 'method-records'
        records.mkdir(mode=0o700)
        first = object.__new__(fixture.TypedDeliveryFixture)
        first.__dict__.update(campaign=campaign, candidate_id=context['candidate_id'], data=data,
            configuration=configuration, configuration_path=guard.check_binding(data, context['configuration_ref']),
            configuration_binding=context['configuration_ref'], context=context, control_path=control_path,
            process=None, recorded_pid=None, saved_guard_identity=None, restored_guard=False,
            record_name='typed-short-live-guard')
        import contextlib
        from collections import Counter
        from tools.official_client_control.errors import ControlError
        from tools.official_client_control.store import ControlStore

        original_handoff, original_plan = delivery._vc5_handoff, execution.validate_plan
        original_graph, original_index = execution.validate_evidence_graph, stops._read_only_fact_index
        indexed_reads, late_change = [], [None]
        scan_comparison = []

        @contextlib.contextmanager
        def observed_index(reader):
            # 只观察原读者的实际完整扫描；消费者、来源、尾验均不替换成通过结论。
            with mock.patch.object(reader, 'list_facts', wraps=reader.list_facts) as scans:
                with original_index(reader) as indexed:
                    record = {'index': indexed, 'tail_started': False, 'tail_completed': False}
                    indexed_reads.append(record)
                    original_verify = indexed.verify_fact_snapshots
                    def verify():
                        record['tail_started'] = True
                        if late_change[0] is not None:
                            mutation, late_change[0] = late_change[0], None
                            mutation()
                        original_verify()
                        record['tail_completed'] = True
                    indexed.verify_fact_snapshots = verify
                    try:
                        yield indexed
                    finally:
                        record['scans'] = list(scans.call_args_list)

        def check_expired(record, reference):
            self.assertFalse(record['index'].active)
            with self.assertRaisesRegex(ValueError, '已退出'):
                record['index'].load_fact(reference)

        with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: str(records)}), \
                mock.patch.object(stops, '_read_only_fact_index', observed_index):
            try:
                first.__enter__()
                identity = dict(first.saved_guard_identity)
                writer = ControlStore(helper.store.root)
                store_campaign = writer.load_campaign(q['campaign_id'])
                original_deployment = writer.list_facts(q['campaign_id'], 'deployment')
                handoff = original_handoff(writer, store_campaign, q['candidate_id'])
                candidate_campaign = data / 'evidence/campaigns' / q['campaign_id']
                guard.private_directory(candidate_campaign)
                read_context = delivery.DeliveryContext(campaign_dir=candidate_campaign, manifest={},
                    candidate_id=q['candidate_id'], revision=1, store=writer, campaign=store_campaign,
                    ledger_dir=data / 'typed-read-ledger', data_root=data, handoff=handoff, entry={})
                live_executor = executor.create_executor(fixture.EXECUTOR_NAME, configuration, context)
                snapshot = delivery._freeze(read_context, live_executor, fixture.EXECUTOR_NAME,
                    context['delivery_id'], configuration['limits']['observation_window_seconds'])
                opened = delivery._open_typed_delivery(read_context, 1, snapshot,
                    configuration['limits']['observation_window_seconds'])
                plan = opened['plan']
                freeze_payload = writer.load_object(plan['environment_freeze_ref'])['payload']
                consumers = (
                    ('交接', lambda: original_handoff(writer, store_campaign, q['candidate_id']),
                     lambda: delivery._vc5_handoff(writer, store_campaign, q['candidate_id'])),
                    ('Freeze图', lambda: original_graph(writer, freeze_payload, current_execution=True,
                        frozen_configuration_ref=plan['executor_configuration_ref']),
                     lambda: execution.validate_evidence_graph(writer, freeze_payload, current_execution=True,
                        frozen_configuration_ref=plan['executor_configuration_ref'])),
                    ('Plan', lambda: original_plan(writer, plan, current_execution=True),
                     lambda: execution.validate_plan(writer, plan, current_execution=True)),
                )
                for label, raw_consume, indexed_consume in consumers:
                    with self.subTest(consumer=label):
                        initial = len(indexed_reads)
                        with mock.patch.object(writer, 'list_facts', wraps=writer.list_facts) as scans:
                            expected = raw_consume()
                        self.assertEqual(len(indexed_reads), initial, '原基线不能借用测试索引')
                        baseline_count = scans.call_count
                        for _ in range(2):
                            initial = len(indexed_reads)
                            actual = indexed_consume()
                            self.assertEqual(len(indexed_reads), initial + 1)
                            record = indexed_reads[-1]
                            counts = Counter(call.args[0] for call in record['scans'])
                            self.assertTrue(counts)
                            self.assertTrue(all(count == 2 for count in counts.values()),
                                '每个已消费Campaign必须首读/收尾各完整扫描一次')
                            self.assertTrue(all(len(call.args) == 1 and not call.kwargs
                                for call in record['scans']), '索引底层不能继续按引用反复扫描维度')
                            self.assertLess(len(record['scans']), baseline_count)
                            scan_comparison.append({'消费者': label, '原扫描次数': baseline_count,
                                                    '索引扫描次数': len(record['scans'])})
                            self.assertTrue(record['tail_completed'])
                            check_expired(record, q['candidate_ref'])
                            if label == 'Freeze图':
                                for field in ('content_sha256', 'deployment_sha256', 'configuration_sha256',
                                              'service_instance_id', 'trusted_data_root'):
                                    self.assertEqual(getattr(actual, field), getattr(expected, field))
                                self.assertEqual(dict(actual.bytes_by_path), dict(expected.bytes_by_path))
                                self.assertFalse(hasattr(actual, 'store'))
                            else:
                                self.assertEqual(actual, expected)
                            self.assertIsNone(execution._SOURCE_BATCH.get())
                            self.assertIsNone(execution._QUALIFICATION_READ_BATCH.get())
                self.assertFalse(writer.read_only)
                self.assertEqual(writer._file_modes, (0o600,))

                reference = q['candidate_ref']
                fact_path = writer.root / 'facts' / reference['campaign_id'] / reference['dimension'] / (
                    f"{reference['sequence']:08d}-{reference['sha256']}.json")
                fact_raw = fact_path.read_bytes()
                try:
                    # 原消费者和索引消费者都必须拒绝0400，不能借只读打开放宽0600合同。
                    fact_path.chmod(0o400)
                    for consume in (consumers[0][1], consumers[0][2]):
                        with self.assertRaises(ControlError):
                            consume()
                finally:
                    fact_path.chmod(0o600)
                configuration_path = writer.object_path(plan['executor_configuration_ref'])
                try:
                    configuration_path.chmod(0o400)
                    for consume in (consumers[2][1], consumers[2][2]):
                        with self.assertRaises(ControlError):
                            consume()
                finally:
                    configuration_path.chmod(0o600)

                # 返回前才真实改原件，原图已完成也不得交出来源结果或交接结论。
                for consume, mutation in ((consumers[1][2], lambda: fact_path.write_bytes(fact_raw + b' ')),
                                          (consumers[0][2], lambda: fact_path.chmod(0o400))):
                    initial = len(indexed_reads)
                    late_change[0] = mutation
                    try:
                        with self.assertRaises((ControlError, guard.GuardError)):
                            consume()
                        self.assertEqual(len(indexed_reads), initial + 1)
                        record = indexed_reads[-1]
                        self.assertTrue(record['tail_started'])
                        self.assertFalse(record['tail_completed'])
                        check_expired(record, reference)
                    finally:
                        late_change[0] = None
                        fact_path.chmod(0o600)
                        fact_path.write_bytes(fact_raw)
                # 异常后下一次也必须重新首读/尾验，不能承接前次通过或失败前索引。
                initial = len(indexed_reads)
                self.assertEqual(consumers[0][2](), handoff)
                self.assertEqual(len(indexed_reads), initial + 1)
                self.assertEqual(indexed_reads[-1]['scans'], [mock.call(q['campaign_id'])] * 2)
                self.assertTrue(indexed_reads[-1]['tail_completed'])
                check_expired(indexed_reads[-1], reference)
                self.assertEqual(writer.list_facts(q['campaign_id'], 'deployment'), original_deployment)
                self.assertFalse(Path(control_path.parent / 'calls.jsonl').exists())
                self.assertFalse(Path(control_path.parent / 'physical.jsonl').exists())
                print(json.dumps({'派发前实际扫描对照': scan_comparison}, ensure_ascii=False), flush=True)
                first.close()
                self.assertTrue(guard.owner_alive({key: identity[key]
                    for key in ('boot_id', 'pid', 'process_start', 'process_group_id')}))
                resumed = object.__new__(fixture.TypedDeliveryFixture)
                resumed.__dict__.update(first.__dict__, process=None, restored_guard=True)
                with mock.patch.object(os, 'fork', side_effect=AssertionError('恢复不能新建 guard')):
                    resumed.__enter__()
                    self.assertEqual(resumed.saved_guard_identity, identity)
                    resumed.close()
                resumed.saved_guard_identity = {**identity, 'service_instance_id': '0' * 64}
                with self.assertRaisesRegex(guard.GuardError, '完整身份'):
                    resumed._bind_live_guard()
                # 原记录环境已退出也必须识别保留实例，不能落到 process=None 的空清理。
                with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: ''}):
                    first.close(force=True)
                resumed.saved_guard_identity = identity
                with self.assertRaisesRegex(guard.GuardError, '原守卫已死'):
                    resumed._bind_live_guard()
            except Exception:
                log = control_path.with_name('typed-vc6-guard.log')
                if log.is_file():
                    print(log.read_text(encoding='utf-8'), flush=True)
                raise
            finally:
                first.close(force=True)


def armed_worker(context: delivery.DeliveryContext, configuration: dict, execution_context: dict, plan: dict) -> None:
    """实际 action owner 单独进程组；强杀时不终止持有日志的独立守卫。"""

    os.setsid()
    instance = executor.create_executor("arm64-isolated", configuration, execution_context)
    delivery._execute_typed_stage(context, instance, plan, "candidate_active", None, None)


class TypedArmedRecoveryTests(unittest.TestCase):
    """通过模块属性复用构造方法，避免 unittest 将导入的 TestCase 再次加载。"""

    setUpClass = classmethod(integration_fixture.RealDeliveryIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration_fixture.RealDeliveryIntegrationTests.tearDownClass.__func__)
    setUp = integration_fixture.RealDeliveryIntegrationTests.setUp
    _stop_guard = integration_fixture.RealDeliveryIntegrationTests._stop_guard
    _seal_review = integration_fixture.RealDeliveryIntegrationTests._seal_review

    def _poll_state(self) -> dict:
        """轮询 worker 进度时读取 state.json，遇到“实读期间漂移”就短暂重试。

        worker 在另一进程里按原子替换写 state.json；守卫的严格读在读前读后核对 inode、大小与时间戳，
        读的过程中文件恰好被替换就失败关闭（生产原件本该如此）。10-23 CI 满载时这一轮询就撞上过一次。
        轮询只需要某个一致时刻的读数，所以这里重试；退出轮询后的断言仍直接用严格读，口径不变。
        """

        path = self.root / "state.json"
        for _ in range(50):
            try:
                return guard.read_json(path)
            except guard.GuardError:
                time.sleep(0.01)
        return guard.read_json(path)

    def test_armed_worker_kill_restores_then_publishes_original_failure_and_resumes_new_segment(self) -> None:
        guard.atomic_json(self.root / "fault.json", {"execute": "wait", "restore": "normal", "freeze": "normal"})
        worker = multiprocessing.get_context("fork").Process(target=armed_worker,
            args=(self.context, self.configuration, self.executor.context, self.delivery))
        worker.start()
        self.addCleanup(self.stop_worker, worker)
        started = time.monotonic()
        # 进入物理动作前依次走 status、prepare、run，后两者仍做完整真实资格复核。
        # 测试等待覆盖既有三次 RPC 的独立保护；不改变通道、租约或产品阶段时限。
        wait_seconds = 3 * self.executor.client.timeout
        deadline = started + wait_seconds
        state = self._poll_state()
        while state["switch_count"] != 1:
            alive = worker.is_alive()
            if not alive or time.monotonic() >= deadline:
                index = guard.read_json(self.root / "guard/index.json")
                operations = []
                for row in index["operations"]:
                    operation = self.root / "guard/operations" / row["operation_id"]
                    operations.append({"operation_id": row["operation_id"],
                        "prepared": (operation / "prepared.json").is_file(),
                        "armed": (operation / "armed.json").is_file(),
                        "physical_intent": (operation / "physical-intent.json").is_file(),
                        "journal_file_count": len(list((operation / "journal").glob("*.json")))})
                diagnostic = {"reason": "worker提前退出" if not alive else "测试等待保护耗尽",
                    "elapsed_seconds": round(time.monotonic() - started, 3), "wait_seconds": wait_seconds,
                    "worker_pid": worker.pid, "worker_alive": alive, "worker_exitcode": worker.exitcode,
                    "guard_pid": self.guard_process.pid, "guard_alive": self.guard_process.is_alive(),
                    "guard_exitcode": self.guard_process.exitcode,
                    "last_state": {key: state[key] for key in ("switch_count", "restore_count")},
                    "operations": operations}
                # 诊断留在原 unittest 日志中，不泄漏配置、凭据或现场内容。
                self.fail("动作没有在独立守卫 armed 后开始物理操作：" + json.dumps(diagnostic, ensure_ascii=False))
            time.sleep(0.01)
            state = self._poll_state()
        print(json.dumps({"typed_armed_wait": {"elapsed_seconds": round(time.monotonic() - started, 3),
            "wait_seconds": wait_seconds, "switch_count": state["switch_count"]}}, ensure_ascii=False), flush=True)
        index = guard.read_json(self.root / "guard/index.json")
        self.assertEqual(len(index["operations"]), 1)
        operation_id = index["operations"][0]["operation_id"]
        operation = self.root / "guard/operations" / operation_id
        self.assertTrue((operation / "prepared.json").is_file())
        self.assertTrue((operation / "armed.json").is_file())
        self.assertFalse((operation / "physical-intent.json").exists())
        os.kill(worker.pid, signal.SIGKILL)
        worker.join(timeout=5)
        self.assertEqual(worker.exitcode, -signal.SIGKILL)
        summary = self.executor.recover_pending({"reason": "entry"})
        self.assertFalse(summary["blocked"])
        core = summary["operations"][0]["execution"]
        self.assertEqual((core["result"], core["restoration"]["state"]), ("failed", "verified"))
        state = guard.read_json(self.root / "state.json")
        self.assertEqual((state["switch_count"], state["restore_count"]), (1, 1))
        self.assertEqual(state["image_digest"], self.configuration["rollback"]["image_digest"])
        self.assertEqual(state["runtime_profile_sha256"], self.configuration["rollback"]["runtime_profile_sha256"])
        original_core = canonical_json_bytes(core)
        # 物理恢复后只能补原发布预登记，不能为失败事实生成新的形成期或 producer。
        first = delivery._publish_execution(self.context, self.delivery, core)
        publication_path = delivery._publication_root(self.context, self.delivery["delivery_id"]) / (operation_id + ".json")
        original_publication = publication_path.read_bytes()
        second = delivery._publish_execution(self.context, self.delivery, core)
        self.assertEqual(first["fact_ref"], second["fact_ref"])
        publication = guard.read_json(publication_path)
        fact = self.store.load_fact(first["fact_ref"])
        for field in ("issued_at_utc", "producer_run", "formed_under", "idempotency_key"):
            self.assertEqual(fact[field], publication[field])
        self.assertEqual(publication_path.read_bytes(), original_publication)
        repeated = self.executor.recover_pending({"reason": "entry"})
        self.assertEqual(canonical_json_bytes(repeated["operations"][0]["execution"]), original_core)
        self.assertEqual(guard.read_json(self.root / "state.json")["restore_count"], 1)
        guard.atomic_json(self.root / "fault.json", {"execute": "normal", "restore": "normal", "freeze": "normal"})
        snapshot = delivery._freeze(self.context, self.executor, "arm64-isolated", self.delivery["delivery_id"], 1)
        chain = delivery._chain(self.context, self.delivery["delivery_id"])
        reservation = delivery._reserve(self.context, self.delivery, chain, snapshot, None)
        self.assertEqual((reservation["recovery_revision"], reservation["rerun_from_stage"]), ("dr1", "candidate_active"))
        recovery_ref = reservation["recovery_ref"]
        recovery = ("dr1", recovery_ref, self.store.load_object(recovery_ref)["payload"])
        previous = None
        for stage in delivery.STAGES:
            result = delivery._execute_typed_stage(self.context, self.executor, self.delivery, stage, previous, recovery)
            self.assertEqual(result["result"], "pass")
            previous = result["fact_ref"]
        self.assertEqual((guard.read_json(self.root / "state.json")["switch_count"],
                          guard.read_json(self.root / "state.json")["restore_count"]), (5, 1))
        self.assertTrue(delivery._chain(self.context, self.delivery["delivery_id"])["complete"])
        self.assertEqual(publication_path.read_bytes(), original_publication)
        self.assertEqual(canonical_json_bytes(self.store.load_fact(first["fact_ref"])), canonical_json_bytes(fact))

    @staticmethod
    def stop_worker(worker: multiprocessing.Process) -> None:
        if worker.is_alive():
            worker.kill()
        worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
