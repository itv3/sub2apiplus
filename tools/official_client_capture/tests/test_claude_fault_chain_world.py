"""连续夹具的轻量合同回归：动态候选、完整激活事实和逐场景六项记录；不启动重链。"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import py_compile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_build_runtime_audit as runtime_audit
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture.tests import claude_fault_chain_world as world_fixture
from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
from tools.official_client_capture.tests import claude_vc5_chain_fixture as source_fixture
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as outer


class ContinuousWorldContractTests(unittest.TestCase):
    def test_late_gate_environment_replays_in_independent_reader_and_rejects_damage(self) -> None:
        """直接复现交付读侧边界，不重跑前面十多分钟的候选构造。"""
        from tools.official_client_capture import claude_gate_material_worker as worker
        from tools.official_client_capture.tests import test_claude_vc5_gate_environment_fixture as gate_tests
        fixture = gate_tests.GateEnvironmentFixtureTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.reader = worker.compute_gate_reader_identity(gate_tests.REPOSITORY, formed_context={'fixture_only': True})
        case = late.ClaudeLateStageChainDriver('runTest')
        world = {'campaign': fixture.root / 'campaign', 'manifest': {}, 'candidate_id': fixture.subject['candidate_id'],
                 'build': {}, 'candidate_payload': {}, 'config_path': fixture.config, 'gate_root': fixture.gate_root,
                 'architecture': 'linux/arm64', 'real_candidate': True,
                 'stage': {'store': {'validation_execution_plan_ref': {'sha256': '1' * 64}}},
                 'store': SimpleNamespace(load_object=lambda _: {'payload': {
                     'external_gates': [{'host_id': 'ARM64', 'architecture': 'linux/arm64'}]}})}
        with mock.patch.object(late.compare, 'candidate_gate_identity', return_value={}), \
                mock.patch.object(late.chain, 'expected_subject', return_value=fixture.subject):
            receipt = case.gate_receipt(world, 'late-receipt.json')
        self.assertEqual(fixture.independent_replay(receipt)['result']['status'], 'passed')
        path = fixture.environment_receipt(receipt, 'before')
        payload = json.loads(path.read_bytes())
        payload['schema_version'] = 'invalid-environment/v1'
        source_fixture._write_json(path, payload)
        with self.assertRaises(worker.ReaderError):
            fixture.independent_replay(receipt)

    def test_late_delivery_refuses_legacy_environment_before_writing_gate(self) -> None:
        case = late.ClaudeLateStageChainDriver('runTest')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = source_fixture._write_json(root / 'source.json', {})
            with mock.patch.object(late.chain, 'gate_receipt_file') as create:
                with self.assertRaisesRegex(AssertionError, '实读完整环境收据'):
                    case.gate_receipt({'config_path': config, 'real_candidate': True}, 'rejected.json')
            create.assert_not_called()

    def test_activation_source_matches_complete_runtime_contract(self) -> None:
        build = {'profile': {'profile_digest': '1' * 64, 'wire_sha256': '2' * 64,
                            'release_sha256': '3' * 64, 'bundle_sha256': '4' * 64},
                 'image': {'image_id': 'sha256:' + '5' * 64, 'reference': 'fixture@sha256:' + '6' * 64},
                 'source': {'tree_sha256': '7' * 64}, 'build': {'build_id': 'build-fixture'}, 'deployed_version': '2.1.226'}
        fact = source_fixture.activation_fact(build, '2.1.226')
        self.assertEqual(set(fact), set(runtime_audit.FACT_FIELDS))
        self.assertEqual(runtime_audit.validate_fact(fact), fact)
        changed = {**fact, 'image_id': 'sha256:' + '8' * 64}
        with self.assertRaisesRegex(runtime_audit.RuntimeAuditError, 'event_id'):
            runtime_audit.validate_fact(changed)

    def test_vc5_uses_current_revision_checkpoint_and_candidate(self) -> None:
        case = late.ClaudeLateStageChainDriver('runTest')
        with tempfile.TemporaryDirectory() as directory:
            case.root = Path(directory)
            case.state_dir = case.root / 'state'
            world = {'campaign': case.root / 'campaign', 'revision': 2, 'config_path': case.root / 'source.json'}
            action = case.compare_action(world['campaign'], candidate_id='candidate-r2')
            self.assertIn('candidate-r2', action['command'])
            with mock.patch.object(late.chain, 'subprocess_environment', return_value={}), \
                    mock.patch.object(late.batch_tests, 'run_main', return_value=(0, {}, '')) as command:
                case.dispatch_vc5(world, 19, action)
            argv = command.call_args.args[0]
            checkpoint = Path(argv[argv.index('--predecessor-checkpoint') + 1])
            self.assertEqual(checkpoint, late.vc.vc_checkpoint_path(world['campaign'], 'VC-4', revision=2))

    def test_gate_subject_uses_current_r2_identity(self) -> None:
        case = late.ClaudeLateStageChainDriver('runTest')
        world = {'campaign': Path('/tmp/campaign'), 'manifest': {}, 'candidate_id': 'r2', 'build': {},
                 'candidate_payload': {}, 'config_path': Path('/tmp/source.json'), 'gate_root': Path('/tmp/gates'), 'architecture': 'linux/arm64'}
        with mock.patch.object(late.compare, 'candidate_gate_identity', return_value={'candidate': 'r2'}) as identity, \
                mock.patch.object(late.chain, 'expected_subject', return_value={}), \
                mock.patch.object(late.chain, 'seams', return_value=contextlib.nullcontext()), \
                mock.patch.object(Path, 'read_bytes', return_value=b'{}'), \
                mock.patch.object(late.chain, 'gate_receipt_file', return_value='receipt'):
            case.gate_receipt(world, 'r2.json')
        self.assertEqual(identity.call_args.args[2], 'r2')

    def test_only_official_source_keeps_real_candidate_reader(self) -> None:
        """只给官方来源时，候选 reader 必须继续转调产品封存读侧，不能套用合成 attempt。"""

        original_reader = late.upgrade._capture_stage_attempt_context
        merged_stage = {'recovery': {'recovery_revision': 'ar1'}}
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            source = campaign / 'official-source.json'
            source.write_text(json.dumps({'mode': 'predecessor_reuse'}), encoding='utf-8')
            config = campaign / 'source-config.json'
            config.write_text(json.dumps({'official_source': str(source)}), encoding='utf-8')
            expected = (campaign / 'original-attempt', {'results': [{'id': 'actual-effective-job'}]})
            with mock.patch.object(candidate_seal, 'sealed_attempt_context', return_value=expected) as sealed:
                with source_fixture.seams(config):
                    self.assertIs(late.upgrade._capture_stage_attempt_context, original_reader)
                    actual = late.upgrade._capture_stage_attempt_context(campaign, merged_stage,
                        phase='candidate', candidate_id='current-r2')
                self.assertEqual(actual, expected)
                sealed.assert_called_once_with(campaign, merged_stage, candidate_id='current-r2')
        self.assertIs(late.upgrade._capture_stage_attempt_context, original_reader)

    def test_sequence_continues_without_reusing_prior_commits(self) -> None:
        world = {'next_sequence': 11}
        self.assertEqual(world_fixture.take_sequence(world, 9), 11)
        self.assertEqual(world_fixture.take_sequence(world), 12)
        self.assertEqual(world_fixture.take_sequence(world, 20), 20)
        self.assertEqual(world['next_sequence'], 21)

    def test_multiple_records_require_each_actual_check(self) -> None:
        expected = outer._expected_scenarios({'one_continuous_method': ('first', 'second')})
        rows = [{'scenario': name, 'checks': {key: {'passed': True} for key in outer.SIX_CHECKS}} for name in expected]
        self.assertTrue(outer._records_complete(rows, expected))
        self.assertFalse(outer._records_complete([None], expected))
        self.assertFalse(outer._records_complete(list(reversed(rows)), expected))
        rows[1]['checks']['accounting']['passed'] = False
        self.assertFalse(outer._records_complete(rows, expected))
        del rows[1]['checks']['history']
        self.assertFalse(outer._records_complete(rows, expected))
        with self.assertRaisesRegex(ValueError, '重复'):
            outer._expected_scenarios({'one': ('first', 'first')})
        for check_name in (
            '_assert_completed_audits_publish_only_missing_and_keep_order',
            '_assert_missing_or_changed_audit_refuses_without_publishing',
            '_assert_conflicting_or_duplicate_current_records_are_not_replaced',
            '_assert_only_current_unsealed_invocation_can_be_published',
            '_assert_original_revision_method_publishes_after_world_returns',
        ):
            with self.subTest(publication_check=check_name):
                check = _RevisionAuditPublicationChecks()
                try:
                    check.setUp()
                    getattr(check, check_name)()
                finally:
                    check.doCleanups()

    def test_registered_continuous_entries_keep_distinct_scene_sets(self) -> None:
        """后段七场景与 revision 八场景分别登记，不拼接场景或预填实际通过。"""
        self.assertEqual(set(outer.CHAINS), {'recovery', 'late-continuous', 'revision-continuous'})
        self.assertIn('test_late_stage_faults_resume_seven_scenarios', outer.ClaudeFaultChainTests.__dict__)
        self.assertIn('test_revision_evaluator_faults_resume_eight_scenarios', outer.ClaudeFaultChainTests.__dict__)
        self.assertEqual(outer.CHAINS['revision-continuous']['expected'], outer.REVISION_CONTINUOUS_SCENARIOS)
        self.assertEqual(len(outer.REVISION_CONTINUOUS_SCENARIOS), 8)
        self.assertNotIn('revision-continuous', outer.PENDING_CONTINUOUS_CHAINS)
        self.assertNotIn('test_revision_continuous', outer.ClaudeFaultChainTests.__dict__)
        # 当前控制来源使用独立凭证路径；测试后继仍消费相同产品意图，旧凭证不覆盖。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            relative = 'tools/official_client_capture/claude_qualification_material_source.py'
            test_relative = 'tools/official_client_capture/tests/test_claude_fault_chain_world.py'
            source = {'repository': '/stable/fixture', 'fixtures': {},
                      'files': {relative: '1' * 64, test_relative: '2' * 64}}
            legacy = root / world_fixture.PRODUCT_CONTINUATION_FILE
            legacy.write_bytes(b'original continuation')
            current = world_fixture.current_product_continuation_path(root, source)
            self.assertNotEqual(current, legacy)
            self.assertEqual(world_fixture.selected_product_continuation_path(root, source), legacy)
            current.write_bytes(b'current continuation')
            self.assertEqual(world_fixture.selected_product_continuation_path(root, source), current)
            successor = {**source, 'files': {**source['files'], test_relative: '3' * 64}}
            self.assertEqual(world_fixture.current_product_continuation_path(root, successor), current)
            changed = {**source, 'files': {**source['files'], relative: '4' * 64}}
            self.assertNotEqual(world_fixture.current_product_continuation_path(root, changed), current)
            self.assertEqual(legacy.read_bytes(), b'original continuation')
            with outer._record_scope(root):
                world_fixture.save_state('chain:revision-continuous', {'diagnostic_seconds': None})
            with mock.patch.dict(os.environ, {'CLAUDE_RECORDED_CHAIN_RECORD_DIR': str(root)}), \
                    mock.patch.object(outer, '_source_inputs', return_value=source):
                with self.assertRaisesRegex(ValueError, '准确新路径'):
                    outer.prepare_product_continuation('revision-continuous', 'formed-read-only', {},
                        output_path=legacy)
            # 只读凭证的执行器原件必须来自实际成功停点，不能随意换成另一个绑定。
            method, expected = next(iter(outer._continuation_methods('revision-continuous').items()))
            origin = world_fixture.file_cut(legacy)
            row = {'root': str(root), 'expected': expected, 'executor_origin': origin, 'originals': [origin]}
            document = {'mode': 'formed-read-only', 'chain': 'revision-continuous', 'tree': str(root / 'tree'),
                        'methods': {method: row}}
            context = {'method': method, 'tree': document['tree']}
            result = {'complete': True, 'executor': origin}
            with mock.patch.object(world_fixture, 'record_root', return_value=root), \
                    mock.patch.object(world_fixture, 'load_state', side_effect=lambda name:
                        context if name == 'method-context' else result):
                outer._verify_continuation_method(document, method, expected)
                result['executor'] = world_fixture.file_cut(current)
                with self.assertRaisesRegex(ValueError, '原成功方法'):
                    outer._verify_continuation_method(document, method, expected)
                result.update(complete=False, executor=origin)
                with self.assertRaisesRegex(ValueError, '原成功方法'):
                    outer._verify_continuation_method(document, method, expected)
        # 同一既有 UID 只核方法级缓存与真实进程派发合同；不运行故障方法或长链。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original_source = {'repository': str(root), 'fixtures': {}, 'files': {'old.py': '1' * 64}}
            current_source = {**original_source, 'files': {'old.py': '2' * 64}}
            before_files = {'old.py': {'path': root / 'old-cut', 'sha256': '1' * 64}}
            proof = {'changed_paths': ['old.py'], 'paths_by_layer': {}, 'proofs': {}}
            document = {'mode': 'formed-evidence-read-only', 'original_source': original_source,
                        'current_source': current_source, 'before_files': before_files, 'source_proof': proof}
            with outer._record_scope(root), mock.patch.object(world_fixture, 'verify_product_continuation',
                    side_effect=lambda *args, **kwargs: copy.deepcopy(document)) as full:
                with world_fixture.formed_source_proof_scope({}, current_source=current_source):
                    for _ in range(6):
                        returned = world_fixture.formed_source_continuation_proof(original_source, current_source, before_files)
                        self.assertEqual(returned, proof)
                        returned['changed_paths'].append('不能污染缓存')
                    self.assertEqual(full.call_count, 1)
                    changed = {**current_source, 'files': {'old.py': '3' * 64}}
                    with self.assertRaisesRegex(ValueError, '跨原方法'):
                        world_fixture.formed_source_continuation_proof(original_source, changed, before_files)
                    other = root / 'other-method'
                    other.mkdir()
                    with outer._record_scope(other), self.assertRaisesRegex(ValueError, '跨原方法'):
                        world_fixture.formed_source_continuation_proof(original_source, current_source, before_files)
                self.assertEqual(full.call_count, 2)
            self.assertIsNone(world_fixture._FORMED_SOURCE_PROOF_SCOPE.get())
            drifted = {**document, 'changed_at_exit': True}
            with outer._record_scope(root), mock.patch.object(world_fixture, 'verify_product_continuation',
                    side_effect=[copy.deepcopy(document), drifted]):
                with self.assertRaisesRegex(ValueError, '退出时'):
                    with world_fixture.formed_source_proof_scope({}, current_source=current_source):
                        pass
            self.assertIsNone(world_fixture._FORMED_SOURCE_PROOF_SCOPE.get())

        # 在真实独立小目录启动当前入口替身，验证 runner 的实际 cwd、argv 和退出原件。
        # 替身只用于派发合同，不替代真实材料 reader 或三链产品兼容验证。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            current_root, method_root = root / 'current', root / 'method'
            current_root.mkdir()
            method_root.mkdir()
            method = '_TransientSegmentScenarios._scenario_full_control_delivery'
            expected = list(outer.LATE_CONTINUOUS_SCENARIOS)
            rows = [{'scenario': name, 'checks': {key: {'passed': True} for key in outer.SIX_CHECKS}}
                    for name in expected]
            records = root / 'original-records.jsonl'
            records.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
            source = {'repository': str(current_root), 'fixtures': {}, 'files': {}}
            report = {'schema': 'claude-fault-current-formed-verification/v1',
                'purpose': 'current-formed-read-only', 'method': method, 'current_source': source, 'records': rows}
            module = current_root / (outer.CHAIN_MODULE.replace('.', '/') + '.py')
            module.parent.mkdir(parents=True)
            module.write_text('from types import SimpleNamespace\nfrom pathlib import Path\nimport sys\n'
                'cache_marker="cache-a"\n'
                'stops=SimpleNamespace(_decoded=lambda value:value,_encoded=lambda value:value)\n'
                'def _revalidate_formed_method_current(binding,method):\n'
                '    report=' + repr(report) + '\n'
                '    report["actual_module"]=str(Path(__file__).resolve())\n'
                '    report["cache_marker"]=cache_marker\n'
                '    report["actual_pycache_prefix"]=sys.pycache_prefix\n'
                '    report["dont_write_bytecode"]=sys.dont_write_bytecode\n'
                '    return report\n')
            # 同秒同长度旧 pyc 必须确实存在；当前进程仍须加载变更后的源码。
            os.utime(module, (1_700_000_000, 1_700_000_000))
            old_size = module.stat().st_size
            old_cache = module.parent / '__pycache__' / (module.stem + '.' + outer.sys.implementation.cache_tag + '.pyc')
            py_compile.compile(str(module), cfile=str(old_cache), doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
            old_cache_bytes = old_cache.read_bytes()
            module.write_text(module.read_text().replace('cache-a', 'cache-b'))
            os.utime(module, (1_700_000_000, 1_700_000_000))
            self.assertEqual(module.stat().st_size, old_size)
            self.assertEqual(int(module.stat().st_mtime), 1_700_000_000)
            binding = world_fixture.file_cut(records)
            continuation = (binding, {'mode': 'formed-evidence-read-only', 'current_source': source})
            recorder = outer._ChainRecorder('late-continuous')
            context = {'seconds': 0.0, 'invocations': []}
            with outer._record_scope(method_root), \
                    mock.patch.object(world_fixture, 'executor_invocation_binding', return_value=binding), \
                    mock.patch.object(world_fixture, 'verify_executor_source') as executor_check, \
                    mock.patch.object(world_fixture, 'invocation_product_continuation', return_value=continuation), \
                    mock.patch.object(outer.shell, '_tree_root', return_value=root), \
                    mock.patch.object(outer.diagnostic_budget, 'diagnostic_seconds', return_value=None), \
                    mock.patch.object(recorder, 'step'), mock.patch.object(recorder, 'emit'), \
                    mock.patch.object(outer.subprocess, 'Popen', wraps=outer.subprocess.Popen) as launches:
                world_fixture.save_state('method-result', {'complete': True, 'bindings': {'records': binding}})
                original = world_fixture._state_path('method-result').read_bytes()
                outer._run_group_body(self, recorder, {method: tuple(expected)}, '当前只读接线',
                    method_root=method_root, context=context, verify=True)
                self.assertEqual(launches.call_count, 1)
                argv = launches.call_args.args[0]
                self.assertEqual(argv[1:4], ['-I', '-B', '-c'])
                self.assertEqual(launches.call_args.kwargs['cwd'], current_root)
                executor_check.assert_called_once()
                self.assertEqual(world_fixture._state_path('method-result').read_bytes(), original)
                verified = world_fixture.load_state('method-verification')
                self.assertTrue(verified['complete'])
                self.assertEqual(verified['purpose'], 'current-formed-read-only')
                exit_document = json.loads(world_fixture.read_cut(verified['bindings']['current_exit']))
                self.assertEqual((exit_document['argv'], exit_document['returncode'], exit_document['timed_out']),
                                 (argv, 0, False))
                actual = world_fixture._decoded(json.loads(world_fixture.read_cut(verified['bindings']['current_stdout'])))
                self.assertEqual(actual['actual_module'], str(module))
                self.assertEqual(actual['cache_marker'], 'cache-b')
                self.assertEqual(actual['actual_pycache_prefix'], argv[-1])
                self.assertTrue(actual['dont_write_bytecode'])
                self.assertEqual(exit_document['pycache_prefix'], argv[-1])
                self.assertFalse(exit_document['pycache_generated'])
                self.assertFalse(os.path.lexists(argv[-1]))
                self.assertEqual(old_cache.read_bytes(), old_cache_bytes)
                self.assertEqual(outer._read_records(verified['bindings']['records']['path']), rows)
                self.assertEqual(recorder.facts['scenarios'], rows)
                # 实际当前进程拒绝时保存失败旁侧输出，仍不派第二个旧 unittest。
                module.write_text('raise ValueError("实际当前入口拒绝")\n')
                with self.assertRaisesRegex(AssertionError, '当前历史成功纯验超时或拒绝'):
                    outer._run_group_body(self, recorder, {method: tuple(expected)}, '当前拒绝接线',
                        method_root=method_root, context=context, verify=True)
                self.assertEqual(launches.call_count, 2)
                self.assertFalse(world_fixture.load_state('method-verification')['complete'])
                self.assertEqual(world_fixture._state_path('method-result').read_bytes(), original)


class _RevisionAuditPublicationChecks(unittest.TestCase):
    """既有六项完整性 UID 的私有检查；不增加 discover UID，不生成链认证。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.old = self.root / 'invocation-original'
        self.current = self.root / 'invocation-current'
        self.old.mkdir()
        self.current.mkdir()
        self.output = self.current / 'records.jsonl'
        self.expected = list(outer.REVISION_CONTINUOUS_SCENARIOS)
        self.records = [{'scenario': name, 'checks': {key: {'passed': True} for key in outer.SIX_CHECKS},
                         'campaign_id': 'original-revision-world'} for name in self.expected]
        self.case = outer._RevisionEvaluatorScenarios('_scenario_revision_evaluator_delivery')
        self.addCleanup(self.case.doCleanups)
        environment = mock.patch.dict(os.environ, {world_fixture.METHOD_RECORD_ENV: str(self.root),
            world_fixture.METHOD_RESUME_ENV: '1', outer.RECORDS_ENV: str(self.output),
            outer.METHOD_VERIFY_ENV: '', world_fixture.EXECUTOR_SOURCE_ENV: ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.context = {'method': type(self.case).__name__ + '.' + self.case._testMethodName,
            'expected': self.expected, 'tree': str(outer.REPO_ROOT), 'seconds': 7,
            'invocations': [{'output': self.old, 'state': 'finished', 'verify': False},
                            {'output': self.current, 'state': 'started', 'verify': False}]}
        world_fixture.save_state('method-context', self.context)
        world_fixture.save_state('method-result', {'complete': False, 'returncode': 0})
        for name, raw in (('exit.json', b'{"returncode":0}'), ('stdout.log', b'original output'),
                          ('stderr.log', b'original errors'), ('records.jsonl', self.encode(self.records[3:]))):
            (self.old / name).write_bytes(raw)
        # 用实际文件切点核发布所调用的回调与原件拒绝；不替换产品函数、不形成新 Campaign。
        self.sources = []
        for index, record in enumerate(self.records):
            source = self.root / f'audit-source-{index}.json'
            source.write_bytes(self.encode([record]))
            self.sources.append(source)
            world_fixture.save_state('audit-complete:' + record['scenario'],
                {'record': record, 'binding': world_fixture.file_cut(source)})
        callback = mock.patch.object(self.case, 'revalidate_completed_method', side_effect=self.revalidate)
        self.replays = callback.start()
        self.addCleanup(callback.stop)
        self.output.write_bytes(b'')
        self.originals = {path: path.read_bytes() for path in self.root.rglob('*')
                          if path.is_file() and path != self.output}

    @staticmethod
    def encode(records) -> bytes:
        return ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in records).encode()

    def revalidate(self, expected):
        records = []
        for name in expected:
            saved = world_fixture.load_state('audit-complete:' + name)
            if saved is None:
                raise ValueError('缺少完整审计停点')
            record = json.loads(world_fixture.read_cut(saved['binding']))
            self.assertEqual(record, saved['record'])
            records.append(record)
        return records

    def publish(self) -> None:
        outer._publish_resumed_method_records(self.case, self.records, self.expected)

    def assert_originals_unchanged(self) -> None:
        self.assertEqual({path: path.read_bytes() for path in self.originals}, self.originals)
        self.assertFalse(list(self.current.glob('.records-*')))

    def _assert_completed_audits_publish_only_missing_and_keep_order(self) -> None:
        # 五条已发、八条已发、完成游标零输出及八个独立缺项都走同一个原方法 UID。
        groups = [self.records[3:], self.records, []]
        groups.extend([row for row in self.records if row['scenario'] != name] for name in self.expected)
        for published in groups:
            with self.subTest(published=[row['scenario'] for row in published]):
                self.replays.reset_mock()
                self.output.write_bytes(self.encode(published))
                self.publish()
                self.assertEqual(outer._read_records(self.output), self.records)
                missing = [name for name in self.expected if name not in [row['scenario'] for row in published]]
                if missing:
                    self.replays.assert_called_once_with(missing)
                else:
                    self.replays.assert_not_called()
                self.assert_originals_unchanged()

    def _assert_missing_or_changed_audit_refuses_without_publishing(self) -> None:
        for index, name in enumerate(self.expected):
            for damaged in (False, True):
                with self.subTest(scenario=name, damaged=damaged):
                    self.output.write_bytes(b'')
                    target = (self.sources[index] if damaged else
                              world_fixture._state_path('audit-complete:' + name))
                    raw = target.read_bytes()
                    if damaged:
                        target.write_bytes(b'X' + raw[1:])
                    else:
                        target.unlink()
                    try:
                        with self.assertRaises(ValueError):
                            self.publish()
                        self.assertEqual(self.output.read_bytes(), b'')
                    finally:
                        target.write_bytes(raw)
                    self.assert_originals_unchanged()

    def _assert_conflicting_or_duplicate_current_records_are_not_replaced(self) -> None:
        changed = copy.deepcopy(self.records[3])
        changed['campaign_id'] = 'other-campaign'
        unknown = {**self.records[3], 'scenario': 'another-scenario'}
        for published in ([self.records[3], self.records[3]], list(reversed(self.records[3:])),
                          [changed], [unknown], [None]):
            with self.subTest(published=published):
                self.replays.reset_mock()
                original = self.encode(published)
                self.output.write_bytes(original)
                with self.assertRaisesRegex(ValueError, '有序子集'):
                    self.publish()
                self.assertEqual(self.output.read_bytes(), original)
                self.replays.assert_not_called()
                self.assert_originals_unchanged()

    def _assert_only_current_unsealed_invocation_can_be_published(self) -> None:
        with mock.patch.dict(os.environ, {outer.RECORDS_ENV: str(self.old / 'records.jsonl')}):
            with self.assertRaisesRegex(ValueError, '当前新 invocation'):
                self.publish()
        for marker in ('stdout.log', 'stderr.log', 'exit.json'):
            with self.subTest(marker=marker):
                path = self.current / marker
                path.write_bytes(b'formed')
                try:
                    with self.assertRaisesRegex(ValueError, '已封存'):
                        self.publish()
                finally:
                    path.unlink()
        invalid_context = copy.deepcopy(self.context)
        invalid_context['invocations'][-1]['state'] = 'finished'
        world_fixture.save_state('method-context', invalid_context)
        try:
            with self.assertRaisesRegex(ValueError, '当前新 invocation'):
                self.publish()
        finally:
            world_fixture.save_state('method-context', self.context)
        self.replays.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b'')
        self.assert_originals_unchanged()

    def _assert_original_revision_method_publishes_after_world_returns(self) -> None:
        self.output.write_bytes(self.encode(self.records[3:]))
        with mock.patch.object(self.case, 'run_revision_evaluator_delivery_chain',
                               return_value={'records': self.records}) as restored:
            self.case._scenario_revision_evaluator_delivery()
        restored.assert_called_once_with()
        self.replays.assert_called_once_with(self.expected[:3])
        self.assertEqual(outer._read_records(self.output), self.records)
        self.assert_originals_unchanged()


if __name__ == '__main__':
    unittest.main()
