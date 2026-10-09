"""连续夹具的轻量合同回归：动态候选、完整激活事实和逐场景六项记录；不启动重链。"""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_build_runtime_audit as runtime_audit
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture.tests import claude_fault_chain_world as world_fixture
from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
from tools.official_client_capture.tests import claude_vc5_chain_fixture as source_fixture
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as outer


class ContinuousWorldContractTests(unittest.TestCase):
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

    def test_registered_continuous_entries_keep_distinct_scene_sets(self) -> None:
        """后段七场景与 revision 八场景分别登记，不拼接场景或预填实际通过。"""
        self.assertEqual(set(outer.CHAINS), {'recovery', 'late-continuous', 'revision-continuous'})
        self.assertIn('test_late_stage_faults_resume_seven_scenarios', outer.ClaudeFaultChainTests.__dict__)
        self.assertIn('test_revision_evaluator_faults_resume_eight_scenarios', outer.ClaudeFaultChainTests.__dict__)
        self.assertEqual(outer.CHAINS['revision-continuous']['expected'], outer.REVISION_CONTINUOUS_SCENARIOS)
        self.assertEqual(len(outer.REVISION_CONTINUOUS_SCENARIOS), 8)
        self.assertNotIn('revision-continuous', outer.PENDING_CONTINUOUS_CHAINS)
        self.assertNotIn('test_revision_continuous', outer.ClaudeFaultChainTests.__dict__)


if __name__ == '__main__':
    unittest.main()
