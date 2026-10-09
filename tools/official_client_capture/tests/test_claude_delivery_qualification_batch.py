"""只读资格批次的收口边界；重放替身只用于观察次数与失败传播。"""

from copy import deepcopy
import unittest

from tools.official_client_capture.tests import test_claude_delivery_configuration_original as configuration_fixture
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.errors import ControlError


class QualificationReadBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = configuration_fixture.ConfigurationOriginalTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.payload["entries"][0]["facts"]["origin"] = deepcopy(self.fixture.origin)

    def test_equal_reports_at_different_paths_replay_once_and_next_call_replays_again(self) -> None:
        fixture = self.fixture
        original = deepcopy(fixture.payload)
        for current in (False, True):
            fixture.import_validation.reset_mock()
            fixture.replay_validation.reset_mock()
            with execution._qualification_read_batch(fixture.store, current_execution=current):
                for number in range(4):
                    fixture.payload = deepcopy(original)
                    fixture.payload["entries"][0]["facts"]["report_binding"] = fixture.write_binding(
                        f"operation-{number}/qualification.json", fixture.report)
                    fixture.consume(current)
                fixture.import_validation.assert_not_called()
            fixture.import_validation.assert_called_once()
            self.assertEqual(fixture.replay_validation.call_count, 2)
            self.assertIsNone(execution._QUALIFICATION_READ_BATCH.get())

    def test_different_full_report_is_not_merged(self) -> None:
        fixture = self.fixture
        with execution._qualification_read_batch(fixture.store, current_execution=False):
            fixture.consume(False)
            changed = deepcopy(fixture.report)
            changed["candidate_compose"] = {"changed": "still-validated"}
            fixture.payload["entries"][0]["facts"]["report_binding"] = fixture.write_binding(
                "other/qualification.json", changed)
            fixture.consume(False)
        self.assertEqual(fixture.import_validation.call_count, 2)
        self.assertEqual(fixture.replay_validation.call_count, 4)

    def test_failed_shared_replay_fails_whole_batch_and_clears_pending(self) -> None:
        fixture = self.fixture
        fixture.replay_validation.side_effect = ControlError("真实资格重放失败")
        with self.assertRaisesRegex(ControlError, "真实资格重放失败"):
            with execution._qualification_read_batch(fixture.store, current_execution=True):
                pending = execution._QUALIFICATION_READ_BATCH.get()
                fixture.consume(True)
        self.assertFalse(pending.active)
        self.assertEqual(pending.pending, [])
        self.assertIsNone(execution._QUALIFICATION_READ_BATCH.get())

    def test_each_original_is_rechecked_after_shared_replay(self) -> None:
        fixture = self.fixture
        binding = fixture.payload["entries"][0]["facts"]["report_binding"]
        def drift(*args, **kwargs):
            path = fixture.root / binding["path"]
            path.write_bytes(path.read_bytes() + b" ")
        fixture.replay_validation.side_effect = drift
        with self.assertRaises(ControlError):
            with execution._qualification_read_batch(fixture.store, current_execution=False):
                fixture.consume(False)

    def test_source_revocation_after_replay_is_rejected(self) -> None:
        fixture = self.fixture
        fixture.replay_validation.side_effect = lambda *a, **k: execution.SOURCE_VERIFIERS.clear()
        with self.assertRaisesRegex(ControlError, "受管通道验收"):
            with execution._qualification_read_batch(fixture.store, current_execution=False):
                fixture.consume(False)

    def test_current_identity_is_rechecked_at_closeout(self) -> None:
        fixture = self.fixture
        def revoke(*args, **kwargs):
            fixture.current_tool.side_effect = ControlError("当前身份已经改变")
        fixture.replay_validation.side_effect = revoke
        with self.assertRaisesRegex(ControlError, "当前身份已经改变"):
            with execution._qualification_read_batch(fixture.store, current_execution=True):
                fixture.consume(True)

    def test_other_store_or_purpose_cannot_join(self) -> None:
        fixture = self.fixture
        with execution._qualification_read_batch(fixture.store, current_execution=False):
            for store, current in ((object(), False), (fixture.store, True)):
                with self.subTest(current=current), self.assertRaises(ControlError):
                    with execution._qualification_read_batch(store, current_execution=current):
                        self.fail("不应进入错误批次")

    def test_nested_same_context_is_validated_by_outer_closeout(self) -> None:
        fixture = self.fixture
        with execution._qualification_read_batch(fixture.store, current_execution=False):
            fixture.consume(False)
            with execution._qualification_read_batch(fixture.store, current_execution=False):
                fixture.consume(False)
            fixture.import_validation.assert_not_called()
        fixture.import_validation.assert_called_once()

    def test_replay_cannot_append_new_pending_work_during_closeout(self) -> None:
        fixture = self.fixture
        fixture.replay_validation.side_effect = lambda *a, **k: fixture.consume(False)
        with self.assertRaisesRegex(ControlError, "只读资格批次与本次证据图不一致"):
            with execution._qualification_read_batch(fixture.store, current_execution=False):
                fixture.consume(False)
        self.assertIsNone(execution._QUALIFICATION_READ_BATCH.get())

    def test_independent_graph_still_replays_immediately(self) -> None:
        fixture = self.fixture
        fixture.consume(False)
        fixture.import_validation.assert_called_once()
        fixture.consume(False)
        self.assertEqual(fixture.import_validation.call_count, 2)

    def test_body_failure_does_not_validate_or_leak_pending(self) -> None:
        fixture = self.fixture
        with self.assertRaisesRegex(ControlError, "上层校验失败"):
            with execution._qualification_read_batch(fixture.store, current_execution=False):
                fixture.consume(False)
                raise ControlError("上层校验失败")
        fixture.import_validation.assert_not_called()
        self.assertIsNone(execution._QUALIFICATION_READ_BATCH.get())
