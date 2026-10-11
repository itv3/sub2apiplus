"""候选 B 组的轻量来源专项：明示合成合同输入，不启动恢复重链或认证入口。"""

from __future__ import annotations

import copy
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture.tests import claude_recorded_candidate_source as candidate
from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import test_claude_recorded_candidate_source as fixtures
from tools.official_client_capture.tests import claude_chain_driver as driver
from tools.official_client_capture.capturelib.model import ConfigurationError


class CandidateRecordedRecoverySourceTests(unittest.TestCase):
    """故障只发生在完整复制之后；原件、运行预约与白名单仍执行既有校验。"""

    def setUp(self) -> None:
        fixture = fixtures.RecordedCandidateSourceTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.data, self.state, self.projection = fixture.data, copy.deepcopy(fixture.state), copy.deepcopy(fixture.projection)
        self.control_root = self.data / "control/candidate-recovery"
        self.control_root.mkdir(mode=0o700)
        self.failures = source.write(self.control_root / "failures.json", {fixtures.PROBE: 1})
        control = source.write(self.data / "control/recovery-control.json", {
            "schema_version": source.RECOVERY_CONTROL_SCHEMA, "fixture_only": True, "purpose": "regression_fixture",
            "control_root": str(self.control_root), "allowed_failure_probe_ids": [fixtures.PROBE],
        })
        self.state["inputs"]["candidate_recovery_control"] = source.binding(control)
        self.state_path = source.write(self.data / "control/recovery-state.json", self.state)

    def copy_step(self, projection: dict, name: str) -> tuple[int, dict]:
        path = source.write(self.data / "control" / (name + ".json"), projection)
        original = source.load_state
        # 这里只验证合成世界的动作合同，不代表 Linux ARM64 命名空间条件通过。
        with mock.patch.object(source, "load_state", side_effect=lambda p, d: original(p, d, isolation=False)):
            code = candidate.copy_registered(self.state_path, source.binding(self.state_path)["sha256"],
                                             path, source.binding(path)["sha256"])
        return code, source.read(path.with_suffix(".copied.json"))

    def resumed_projection(self) -> dict:
        projection = copy.deepcopy(self.projection)
        reservation = source.read(source.bound(projection["runtime"]["reservation"]))
        runtime = source.read(source.bound(projection["runtime"]["receipt"]))
        nonce = capture._step_run_nonce(reservation["run_nonce"], projection["job_id"], 2, 1)
        runtime["run_nonce"] = nonce
        runtime_path = source.write(source.bound(projection["runtime"]["receipt"]).with_name("resumed-runtime.json"), runtime)
        projection["operation"] = "job:candidate-fixture:step-1:attempt-2"
        projection["runtime"].update(receipt=source.binding(runtime_path), run_nonce=nonce)
        projection["output_root"] = str(self.data / "runs/candidate-fixture-resumed" / fixtures.PROBE)
        argv = projection["argv"]
        argv[argv.index("--output-root") + 1] = projection["output_root"]
        return projection

    def test_failure_after_copy_then_repair_keeps_bytes_and_logs_exact_operations(self) -> None:
        code, copied = self.copy_step(self.projection, "failed-step")
        self.assertEqual(code, 2)
        restoration = source.read(source.bound(copied["restoration"]))
        self.assertEqual((restoration["status"], restoration["capture_exit_code"]), ("capture_failed", 2))
        decision = source.read(source.bound(copied["recovery_action"]))
        self.assertTrue(decision["copy_completed"])
        self.assertEqual((decision["return_code"], decision["network_requests_this_run"]), (2, 0))
        origin = source.origin_probe(self.state, fixtures.PROBE)
        for item in copied["files"]:
            original = Path(origin["index"]["root"]) / item["original_relative"]
            self.assertEqual(source.bound(item["copied"]).read_bytes(), original.read_bytes())
        # 修复只改变本世界故障表；旧决策与已复制的请求字节不被覆盖。
        frozen_decision = source.binding(source.bound(copied["recovery_action"]))
        self.failures.write_text(json.dumps({fixtures.PROBE: 0}), encoding="utf-8")
        resumed = self.resumed_projection()
        code, second = self.copy_step(resumed, "resumed-step")
        self.assertEqual(code, 0)
        self.assertEqual(source.binding(source.bound(copied["recovery_action"])), frozen_decision)
        self.assertEqual(source.read(source.bound(second["restoration"]))["status"], "complete")
        rows = [json.loads(line) for line in (self.control_root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual([row["probe_id"] for row in rows], [fixtures.PROBE, fixtures.PROBE])
        self.assertEqual([row["operation"] for row in rows], [self.projection["operation"], resumed["operation"]])
        self.assertEqual([source.read(source.bound(row["decision"]))["return_code"] for row in rows], [2, 0])
        replay.assert_source_untouched(self.state["snapshot"])

    def test_missing_drifted_cross_root_or_wrong_control_identity_fails_before_copy(self) -> None:
        original_path = source.bound(self.state["inputs"]["candidate_recovery_control"])
        original = source.read(original_path)
        variants = ({"fixture_only": False}, {"purpose": "formal"}, {"schema_version": "unknown"},
                    {"control_root": str(self.data / "runs")}, {"allowed_failure_probe_ids": ["unknown"]},
                    {"unexpected": True})
        for index, fields in enumerate(variants):
            with self.subTest(fields=fields):
                path = source.write(self.data / "control" / f"invalid-control-{index}.json", {**original, **fields})
                state = copy.deepcopy(self.state)
                state["inputs"]["candidate_recovery_control"] = source.binding(path)
                with self.assertRaises(replay.ReplayError):
                    source.recovery_control(state)
        before = original_path.read_bytes()
        original_path.write_bytes(before + b" ")
        with self.assertRaisesRegex(replay.ReplayError, "摘要或字节数漂移"):
            self.copy_step(self.projection, "drifted-control-step")
        original_path.write_bytes(before)
        original_path.unlink()
        with self.assertRaises(replay.ReplayError):
            source.recovery_control(self.state)
        self.assertFalse(Path(self.projection["output_root"]).exists())
        self.assertFalse((self.control_root / "calls.jsonl").exists())

    def test_unknown_fault_probe_or_invalid_count_is_rejected_without_execution(self) -> None:
        for values in ({"unknown": 1}, {fixtures.PROBE: -1}, {fixtures.PROBE: True}, {fixtures.PROBE: "1"}):
            with self.subTest(values=values):
                self.failures.write_text(json.dumps(values), encoding="utf-8")
                with self.assertRaisesRegex(replay.ReplayError, "故障表"):
                    source.recovery_control(self.state)
        self.assertFalse((self.control_root / "calls.jsonl").exists())

    def test_recovery_logging_cannot_bypass_runtime_or_origin_identity(self) -> None:
        projection = copy.deepcopy(self.projection)
        projection["runtime"]["run_nonce"] = "f" * 64
        with self.assertRaises(replay.ReplayError):
            self.copy_step(projection, "invalid-runtime-step")
        self.assertFalse(Path(projection["output_root"]).exists())
        self.assertFalse((self.control_root / "calls.jsonl").exists())
        replay.assert_source_untouched(self.state["snapshot"])

    def test_duplicate_operation_cannot_increment_counter_or_append_log(self) -> None:
        self.copy_step(self.projection, "first-step")
        calls = (self.control_root / "calls.jsonl").read_bytes()
        counts = (self.control_root / "counts.json").read_bytes()
        with self.assertRaisesRegex(replay.ReplayError, "禁止覆盖"):
            self.copy_step(self.projection, "duplicate-step")
        self.assertEqual((self.control_root / "calls.jsonl").read_bytes(), calls)
        self.assertEqual((self.control_root / "counts.json").read_bytes(), counts)


class CandidateJobAggregationTests(unittest.TestCase):
    """只构造并重验冻结输入，不调用 VC-0 收口、批次或任何恢复重链。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="claude-candidate-aggregation-")
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve() / "staging/data"
        driver.full.vc1._create_world_roots(self.data)
        self.inputs = driver.inputs(self.data)
        self.state = {"data_root": str(self.data), "inputs": self.inputs, "jobs": self.inputs["jobs"]}
        self.aggregation = driver.validate_aggregation(self.state)

    def test_full_rules_original_probes_steps_and_official_jobs_are_preserved(self) -> None:
        original = source.read(source.bound(self.aggregation["original_scenarios"]))
        grouped = source.read(source.bound(self.aggregation["grouped_scenarios"]))
        self.assertEqual([job for job in grouped["capture_jobs"] if job["phase"] == "official"],
                         [job for job in original["capture_jobs"] if job["phase"] == "official"])
        candidates = [job for job in original["capture_jobs"] if job["phase"] == "candidate"]
        self.assertEqual(set(self.inputs["candidate_jobs"]), {driver.LIVE_JOB, driver.REFRESH_JOB})
        self.assertEqual(self.inputs["candidate_jobs"][driver.REFRESH_JOB], [driver.REFRESH_PROBE])
        original_probes = [step["probe_id"] for job in candidates for step in job["steps"]]
        grouped_probes = [probe for probes in self.inputs["candidate_jobs"].values() for probe in probes]
        self.assertEqual(sorted(original_probes), sorted(grouped_probes))
        self.assertEqual(len(grouped_probes), len(set(grouped_probes)))
        self.assertEqual(len(self.aggregation["step_mapping"]), len(original_probes))
        self.assertEqual(len(source.read(source.bound(self.aggregation["rules"]))["required_rules"]), 40)
        live = next(job for job in grouped["capture_jobs"] if job["id"] == driver.LIVE_JOB)
        for key in ("scenario_ids", "covers", "tool_dependencies", "required_scenario_receipts"):
            self.assertEqual(set(live[key]), {value for job in candidates if job["id"] != driver.REFRESH_JOB for value in job[key]})
        for row in self.aggregation["step_mapping"]:
            old = next(job for job in candidates if job["id"] == row["original_job_id"])["steps"][row["original_step_index"] - 1]
            new = next(job for job in grouped["capture_jobs"] if job["id"] == row["target_job_id"])["steps"][row["target_step_index"] - 1]
            self.assertEqual(new, driver._coordinate_projection(old, row["original_evidence_root"], row["target_evidence_root"]))
        self._assert_partial_initialization_resume()
        self._assert_private_fixture_admin_token()
        self._assert_preflight_store_fact_closure()

    def _assert_preflight_store_fact_closure(self) -> None:
        """原UID内只验七条准备事实的顺序与引用；形状夹具不冒充真实Store重放通过。"""

        from tools.official_client_control.store import ControlStore
        store = ControlStore.initialize(self.data / 'control/preflight-facts-short', '2026-10-11T00:00:00Z')
        facts = []
        kinds = [('discovery', 1, 'discovery_recorded'), ('evidence', 1, 'evidence_recorded'),
            ('evidence', 2, 'rule_classification_recorded'), ('approval', 1, 'evidence_approved'),
            ('approval', 2, 'profile_approved'), ('validation', 1, 'candidate_revision_recorded'),
            ('validation', 2, 'candidate_frozen')]
        for index, (dimension, sequence, kind) in enumerate(kinds, 1):
            facts.append({'campaign_id': 'original-formal', 'dimension': dimension, 'sequence': sequence,
                'fact_kind': kind, '_sha256': f'{index:064x}', 'payload': {},
                'formed_under': {'tool_identity_ref': None, 'candidate_revision': 1, 'evaluation_baseline': None}})
        refs = [store.fact_ref(fact) for fact in facts]
        build_ref = {'receipt_kind': 'candidate_build', 'sha256': 'a' * 64}
        build = {'campaign_id': 'original-formal', 'candidate_id': driver.CANDIDATE, 'candidate_revision': 1,
            'store': {'profile_approval_ref': refs[4], 'candidate_frozen_ref': refs[-1], 'candidate_build_receipt_ref': build_ref}}
        facts[-2]['payload'] = {'revision': 1, 'candidate_id': driver.CANDIDATE, 'supersedes': None, 'invalidation_ref': None}
        facts[-1].update(previous_fact_sha256=refs[-2]['sha256'], payload={
            'candidate_id': driver.CANDIDATE, 'profile_approval_ref': refs[4], 'candidate_build_receipt_ref': build_ref})
        self.assertEqual(driver.preflight_store_fact_refs(store, facts, build), refs)
        for mutate in (
            lambda rows: rows.reverse(),
            lambda rows: rows.append(copy.deepcopy(rows[-1])),
            lambda rows: rows[-2]['payload'].update(revision=2),
            lambda rows: rows[-1].update(previous_fact_sha256='b' * 64),
            lambda rows: rows[-1]['payload'].update(candidate_id='foreign-candidate'),
            lambda rows: rows[-1]['payload'].update(profile_approval_ref=refs[3]),
            lambda rows: rows[-1]['payload'].update(candidate_build_receipt_ref={'receipt_kind': 'candidate_build', 'sha256': 'c' * 64}),
        ):
            damaged = copy.deepcopy(facts)
            mutate(damaged)
            with self.assertRaises(replay.ReplayError):
                driver.preflight_store_fact_refs(store, damaged, build)
        self.assertEqual(driver.preflight_store_fact_refs(store, facts, build), refs)

    def _assert_private_fixture_admin_token(self) -> None:
        """原UID内核隔离文件和环境恢复；假 token 不授予账号资格或正式认证。"""

        path = driver.fixture_admin_token(self.data)
        original = path.read_bytes()
        inode = path.stat().st_ino
        self.assertEqual(original, b'hdr.payload.sig\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o400)
        self.assertEqual(driver.fixture_admin_token(self.data), path)
        self.assertEqual(path.stat().st_ino, inode)
        with mock.patch.dict(os.environ, {'ADMIN_BEARER_TOKEN': 'ambient-inert-token',
                'ADMIN_BEARER_TOKEN_FILE': '/original/inert-file'}), \
                mock.patch.object(source, 'load_state', return_value=self.state), \
                mock.patch.object(driver.full, 'installed', side_effect=lambda *_args: contextlib.nullcontext('original-adapter')):
            before = {key: os.environ.get(key) for key in ('ADMIN_BEARER_TOKEN', 'ADMIN_BEARER_TOKEN_FILE')}
            for fail in (False, True):
                def isolated():
                    with driver.installed(self.data / 'inert-state', '0' * 64) as adapter:
                        self.assertEqual(adapter, 'original-adapter')
                        self.assertEqual(os.environ['ADMIN_BEARER_TOKEN'], '')
                        self.assertEqual(os.environ['ADMIN_BEARER_TOKEN_FILE'], str(path))
                        if fail:
                            raise RuntimeError('原隔离作用域退出')
                if fail:
                    with self.assertRaisesRegex(RuntimeError, '作用域退出'):
                        isolated()
                else:
                    isolated()
                self.assertEqual({key: os.environ.get(key) for key in before}, before)
        path.chmod(0o600)
        with self.assertRaisesRegex(replay.ReplayError, '只读权限'):
            driver.fixture_admin_token(self.data)
        path.write_bytes(original + b'changed')
        path.chmod(0o400)
        with self.assertRaisesRegex(replay.ReplayError, '原字节'):
            driver.fixture_admin_token(self.data)
        self.assertEqual(path.read_bytes(), original + b'changed', '漂移只能拒绝，不能自动覆盖')
        path.unlink()
        target = self.data / 'control/inert-original-token'
        target.write_bytes(original)
        path.symlink_to(target)
        with self.assertRaises(replay.ReplayError):
            driver.fixture_admin_token(self.data)
        self.assertEqual(target.read_bytes(), original)
        self.assertIsNone(driver.token_preflight_retry_proof(SimpleNamespace(), 'candidate-reconcile', [],
            self.data / 'unknown-campaign', {}, (1, None, '其它配置失败\n')))
        with tempfile.TemporaryDirectory() as records, mock.patch.dict(os.environ, {
                driver.stops.METHOD_RECORD_ENV: records, driver.stops.METHOD_RESUME_ENV: '1'}):
            with self.assertRaisesRegex(replay.ReplayError, '原方法'):
                driver.token_preflight_retry_proof(SimpleNamespace(_testMethodName='foreign-method'),
                    'candidate-reconcile', [], self.data / 'unknown-campaign', {},
                    (1, None, driver.TOKEN_PREFLIGHT_ERROR))

    def _assert_partial_initialization_resume(self) -> None:
        """原 UID 内只验初始化运输边界；隔离替身不代表 ARM64 资格或形成链成功。"""

        stops = driver.stops
        arguments = SimpleNamespace(data=str(self.data), recorded=str(self.data.parent / 'original-relay/main'),
            freeze=str(self.data.parent / 'original-freeze'), scenario='reconcile-resume',
            parent_namespaces=json.dumps({'mnt': 'original-parent-mnt', 'net': 'original-parent-net'}))
        error = '录制材料不存在或不可读：' + str(
            Path(arguments.recorded).parent / Path(driver.full.FULL_SOURCE_SELECTION['attempt']).parent)
        argv = ['unshare', '-m', '-n', driver.sys.executable, '-B', '-m', driver.ACTIVATION_MODULE,
            '--data', arguments.data, '--recorded', arguments.recorded, '--freeze', arguments.freeze,
            '--scenario', arguments.scenario, '--parent-namespaces', arguments.parent_namespaces]
        reference = self.data.parent / 'original-tools-reference'
        relative = Path('tools/official_client_capture/product.py')
        for root in (reference, self.data):
            path = root / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text('# 原测试产品字节\n', encoding='utf-8')
        with tempfile.TemporaryDirectory() as records, mock.patch.dict(os.environ, {
                stops.METHOD_RECORD_ENV: records, stops.METHOD_RESUME_ENV: '1'}), \
                mock.patch.object(driver.full.vc1, 'REPOSITORY', reference):
            pending = stops.begin_command(argv)
            stops.record_command(argv, 1, json.dumps({'status': 'unverified', 'fixture_only': True,
                'registered_chains': [], 'error': error}), 'replay.build_snapshot\n_source_plan\n' + error, pending=pending)
            segment = stops.load_state('command-observations')['commands'][-1]
            entry = {'data': self.data, 'recorded': arguments.recorded, 'freeze': arguments.freeze,
                     'scenario': arguments.scenario, 'segments': [segment]}
            stops.save_state('candidate-recorded-world', {'root': self.data.parents[1], 'data': self.data})
            stops.save_state('candidate-entry:' + arguments.scenario, entry)
            self.assertEqual(driver._legacy_partial_initialization(arguments), self.inputs)
            product = self.data / relative
            product_raw = product.read_bytes()
            product.write_bytes(product_raw + b'changed')
            with self.assertRaisesRegex(replay.ReplayError, '非测试工具副本'):
                driver._legacy_partial_initialization(arguments)
            product.write_bytes(product_raw)
            self.assertEqual(stops.load_state('candidate-entry:' + arguments.scenario), entry)
            self.assertEqual(segment['returncode'], 1)
            original_failure = stops.read_cut(segment['stderr'])
            with mock.patch.object(source, 'require_isolation'), \
                    mock.patch.object(driver.full.vc1.subprocess, 'run'), \
                    mock.patch.object(replay, 'build_snapshot', side_effect=RuntimeError('到达原快照续作')) as snapshot:
                def resume() -> None:
                    driver.full.vc1.namespace_main(arguments,
                        inputs_builder=mock.Mock(side_effect=AssertionError('禁止重调原 inputs 生产者')),
                        chain_runner=mock.Mock(side_effect=AssertionError('禁止跳过来源直接进入形成链')),
                        activation_module=driver.ACTIVATION_MODULE, initialization_name=driver.SOURCE_INITIALIZATION,
                        resume_inputs=driver._legacy_partial_initialization, input_verifier=driver._verify_initialization_inputs)
                with self.assertRaisesRegex(RuntimeError, '到达原快照续作'):
                    resume()
                snapshot.assert_called_once_with(self.data / 'control/snapshot', recorded=Path(arguments.recorded),
                    source_selections=tuple(self.inputs['source_selections']))
                self.assertEqual(stops.load_state(driver.SOURCE_INITIALIZATION)['phase'], 'snapshot-started')
                snapshot.reset_mock()
                half = self.data / 'control/snapshot'
                half.mkdir()
                with self.assertRaisesRegex(replay.ReplayError, '快照未闭合'):
                    resume()
                half.rmdir()
                path = source.bound(self.inputs['bindings']['scenarios'])
                before = path.read_bytes()
                path.write_bytes(before + b' ')
                with self.assertRaisesRegex(replay.ReplayError, '摘要或字节数漂移'):
                    resume()
                path.write_bytes(before)
                missing = path.with_suffix('.missing')
                path.rename(missing)
                with self.assertRaisesRegex(replay.ReplayError, '不存在或不可读'):
                    resume()
                missing.rename(path)
                origin = self.data / 'control/fixture-inputs/projection-origin.json'
                raw = origin.read_bytes()
                origin.write_bytes(raw + b' ')
                with self.assertRaisesRegex(replay.ReplayError, '输入来源摘要漂移'):
                    resume()
                origin.write_bytes(raw)
                snapshot.assert_not_called()
            self.assertEqual(stops.read_cut(segment['stderr']), original_failure)
            self.assertEqual(stops.load_state('candidate-entry:' + arguments.scenario), entry)

    def test_missing_probe_or_duplicate_step_mapping_is_rejected_even_with_updated_local_binding(self) -> None:
        for index, mapping in enumerate((self.aggregation["step_mapping"][:-1],
                                         [*self.aggregation["step_mapping"], self.aggregation["step_mapping"][0]])):
            with self.subTest(index=index):
                value = {**self.aggregation, "step_mapping": mapping}
                path = source.write(self.data / "control" / f"bad-map-{index}.json", value)
                state = copy.deepcopy(self.state)
                state["inputs"]["bindings"]["candidate_aggregation"] = source.binding(path)
                with self.assertRaisesRegex(replay.ReplayError, "遗漏、重复或漂移"):
                    driver.validate_aggregation(state)

    def test_rule_coverage_omission_and_duplicate_probe_are_rejected_by_original_contracts(self) -> None:
        original = source.read(source.bound(self.aggregation["original_scenarios"]))
        missing = copy.deepcopy(original)
        rule = "SPEC-BODY-001"
        for job in missing["capture_jobs"]:
            if job["phase"] == "candidate":
                job["covers"] = [value for value in job["covers"] if value != rule]
        with self.assertRaisesRegex(ConfigurationError, "未映射规则"):
            driver.aggregate_scenarios(missing)
        duplicate = copy.deepcopy(original)
        job = next(job for job in duplicate["capture_jobs"] if job["id"] == driver.LIVE_JOB)
        job["steps"].append(copy.deepcopy(job["steps"][0]))
        with self.assertRaises(ConfigurationError):
            driver.aggregate_scenarios(duplicate)

    def test_frozen_map_scenario_or_original_source_bytes_cannot_change(self) -> None:
        # 同一个原UID内核准备交接：这里只验证接缝和真实文件绑定，不冒充原生形成链。
        stops = driver.stops
        state_path = source.write(self.data / 'control/short-prepared-state.json', self.state)
        campaign = self.data / 'control/short-prepared-campaign'
        campaign.mkdir()
        continuation = source.write(self.data / 'control/short-continuation.json', {'fixture_only': True})
        build = source.write(campaign / 'build.json', {'fixture_only': True})
        checkpoint = source.write(campaign / 'vc4.json', {'fixture_only': True})
        runtime = source.write(self.data / 'control/full-runtime-source.json', {'build_receipt': source.binding(build)})
        ready = source.write(self.data / 'control/candidate-recovery-start.json', {
            'schema_version': driver.PREPARED_SCHEMA, 'fixture_only': True, 'purpose': 'regression_fixture',
            'scope': 'VC-0→VC-4', 'state': source.binding(state_path), 'continuation': source.binding(continuation),
            'checkpoint': source.binding(checkpoint), 'build_receipt': source.binding(build),
            'candidate_id': driver.CANDIDATE, 'candidate_revision': 1, 'runtime_source': source.binding(runtime),
            'formal_version_semantics_certified': False})
        prepared = {'state': source.binding(state_path), 'data': self.data, 'phase': 'prepared', 'prepared': source.binding(ready)}
        with tempfile.TemporaryDirectory() as records, mock.patch.dict(os.environ, {
                stops.METHOD_RECORD_ENV: records, stops.METHOD_RESUME_ENV: '1'}), \
                mock.patch.object(source, 'load_state', return_value=self.state), \
                mock.patch.object(driver, 'installed', return_value=contextlib.nullcontext()), \
                mock.patch.object(driver.full.vc1, 'load_continuation', return_value={
                    'data': self.data, 'campaign': campaign, 'manifest': {}}), \
                mock.patch.object(driver.claude_upgrade, '_vc_campaign_plan', return_value={}), \
                mock.patch.object(driver.claude_upgrade, '_replay_vc_checkpoint', return_value=(checkpoint, {})) as reader, \
                mock.patch.object(driver.full.vc1, 'run_chain', side_effect=AssertionError('禁止重新形成VC-0→VC-1')), \
                mock.patch.object(driver.full, 'classify_and_prepare', side_effect=AssertionError('禁止重新分类')), \
                mock.patch.object(driver.full, 'build_candidate', side_effect=AssertionError('禁止重新构建')):
            stops.save_state('candidate-prepare', prepared)
            self.assertEqual(driver.prepare(state_path, source.binding(state_path)['sha256']), ready)
            reader.assert_called_once()
            for phase in ('vc1-started', 'classify-started', 'build-started'):
                stops.save_state('candidate-prepare', {**prepared, 'phase': phase,
                    'continuation': source.binding(continuation),
                    'classified': {'classification': source.binding(build), 'profile': source.binding(build)}})
                with self.assertRaisesRegex(replay.ReplayError, '内部形成动作未闭合'):
                    driver.prepare(state_path, source.binding(state_path)['sha256'])
            stops.save_state('candidate-prepare', prepared)
            runtime.write_bytes(runtime.read_bytes() + b' ')
            with self.assertRaisesRegex(replay.ReplayError, '摘要或字节数漂移'):
                driver.prepare(state_path, source.binding(state_path)['sha256'])
            runtime.write_bytes(runtime.read_bytes().rstrip())
            stops._state_path('candidate-prepare').unlink()
            stops.save_state('candidate-namespace', {'state_binding': source.binding(state_path),
                'data': self.data, 'prepare_started': False})
            def first_prepare(*_args, **_options):
                self.assertTrue(stops.load_state('candidate-namespace')['prepare_started'])
                self.assertEqual(stops.load_state('candidate-prepare')['phase'], 'vc1-started')
                raise RuntimeError('首次准备已开始')
            with mock.patch.object(driver.full.vc1, 'run_chain', side_effect=first_prepare) as first:
                with self.assertRaisesRegex(RuntimeError, '首次准备已开始'):
                    driver.prepare(state_path, source.binding(state_path)['sha256'])
                stops._state_path('candidate-prepare').unlink()
                with self.assertRaisesRegex(replay.ReplayError, '缺明确停点'):
                    driver.prepare(state_path, source.binding(state_path)['sha256'])
                first.assert_called_once()
        for name in ("original_scenarios", "grouped_scenarios", "grouped_labels", "baseline_scenarios"):
            with self.subTest(name=name):
                path = source.bound(self.aggregation[name])
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                with self.assertRaisesRegex(replay.ReplayError, "摘要或字节数漂移"):
                    driver.validate_aggregation(self.state)
                path.write_bytes(before)
        path = source.bound(self.inputs["bindings"]["candidate_aggregation"])
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(replay.ReplayError, "摘要或字节数漂移"):
            driver.validate_aggregation(self.state)

    def test_wrong_job_whitelist_or_cross_root_frozen_binding_fails_closed(self) -> None:
        # 执行副本恢复只接受原字节或冻结后继；丢清单、漂移或另写后继均不覆盖现场。
        stops = driver.stops
        path = self.data / 'tools/official_client_capture/short-candidate-copy.sh'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'original\n')
        with tempfile.TemporaryDirectory() as records, mock.patch.dict(os.environ, {
                stops.METHOD_RECORD_ENV: records, stops.METHOD_RESUME_ENV: ''}):
            tree = driver.ExecutionToolCopy(self.data)
            tree.write('official_client_capture/short-candidate-copy.sh', b'changed\n')
            with mock.patch.dict(os.environ, {stops.METHOD_RESUME_ENV: '1'}):
                resumed = driver.ExecutionToolCopy(self.data)
                self.assertEqual(resumed.originals[path], b'original\n')
                self.assertEqual(path.read_bytes(), b'changed\n')
                with self.assertRaisesRegex(replay.ReplayError, '演进内容已改变'):
                    resumed.write('official_client_capture/short-candidate-copy.sh', b'other\n')
                path.write_bytes(b'drifted\n')
                with self.assertRaisesRegex(replay.ReplayError, '字节不是原件'):
                    driver.ExecutionToolCopy(self.data)
                with self.assertRaisesRegex(replay.ReplayError, '当前字节漂移'):
                    resumed.write('official_client_capture/short-candidate-copy.sh', b'changed\n')
                self.assertEqual(path.read_bytes(), b'drifted\n')
                stops._state_path('candidate-execution-copy').unlink()
                with self.assertRaisesRegex(replay.ReplayError, '原件清单缺失'):
                    driver.ExecutionToolCopy(self.data)
        # 模拟的隔离检查只验恢复接缝：必须挂原来源，不能调用首次建场或复制路径。
        relay = source.write(self.data.parent / 'short-origin/relay.json', {'fixture_only': True})
        frozen = self.data.parent / 'short-freeze'
        frozen.mkdir()
        site = self.data / 'control/site/sitecustomize.py'
        site.parent.mkdir(parents=True, exist_ok=True)
        site.write_text('# 仅验证接缝，不代表原生资格\n', encoding='utf-8')
        roots = [str(relay.parent), str(frozen)]
        namespace_state = {**self.state, 'readonly_sources': roots}
        state_path = source.write(self.data / 'control/short-namespace-state.json', namespace_state)
        saved = {'state_path': state_path, 'digest': source.binding(state_path)['sha256'],
            'state_binding': source.binding(state_path), 'data': self.data, 'roots': roots,
            'site_binding': source.binding(site), 'scenario': 'reconcile-resume'}
        arguments = SimpleNamespace(data=str(self.data), recorded=str(relay), freeze=str(frozen),
            parent_namespaces=json.dumps({'mnt': 'original-parent-mnt', 'net': 'original-parent-net'}), scenario='reconcile-resume')
        with tempfile.TemporaryDirectory() as records, mock.patch.dict(os.environ, {
                stops.METHOD_RECORD_ENV: records, stops.METHOD_RESUME_ENV: '1'}), \
                mock.patch.object(source, 'load_state', return_value=namespace_state), \
                mock.patch.object(source, 'require_isolation') as isolation, \
                mock.patch.object(driver.subprocess, 'run') as mount, \
                mock.patch.object(driver, 'run_scenario', return_value={'fixture_only': True}), \
                mock.patch.object(driver.full.vc1, 'namespace_main', side_effect=AssertionError('禁止重新建场')):
            stops.save_state('candidate-namespace', saved)
            self.assertEqual(driver._resume_namespace(arguments, saved), {'fixture_only': True})
            self.assertEqual([call.args[0] for call in mount.call_args_list], [
                ['mount', '--make-rprivate', '/'], ['mount', '--bind', roots[0], roots[0]],
                ['mount', '-o', 'remount,bind,ro', roots[0]], ['mount', '--bind', roots[1], roots[1]],
                ['mount', '-o', 'remount,bind,ro', roots[1]]])
            isolation.assert_called_once_with({'parent_namespaces': json.loads(arguments.parent_namespaces), 'readonly_sources': []})
            mount.reset_mock()
            site.write_bytes(site.read_bytes() + b' ')
            with self.assertRaisesRegex(replay.ReplayError, '摘要或字节数漂移'):
                driver._resume_namespace(arguments, saved)
            mount.assert_not_called()
            arguments.scenario = 'tool-evolution'
            with self.assertRaisesRegex(replay.ReplayError, '恢复坐标或只读来源发生改变'):
                driver._resume_namespace(arguments, saved)
            mount.assert_not_called()
        state = copy.deepcopy(self.state)
        state["inputs"]["candidate_jobs"][driver.LIVE_JOB].pop()
        with self.assertRaisesRegex(replay.ReplayError, "白名单"):
            driver.validate_aggregation(state)
        outside = source.write(self.data.parent / "outside-scenarios.json",
                               source.read(source.bound(self.aggregation["grouped_scenarios"])))
        value = {**self.aggregation, "grouped_scenarios": source.binding(outside)}
        path = source.write(self.data / "control/cross-root-map.json", value)
        state = copy.deepcopy(self.state)
        state["inputs"]["bindings"]["candidate_aggregation"] = source.binding(path)
        with self.assertRaisesRegex(replay.ReplayError, "越过录制根"):
            driver.validate_aggregation(state)


if __name__ == "__main__":
    unittest.main()
