from __future__ import annotations

import unittest

from tools.official_client_capture.extract_compaction_reason import matches_expected_profile


def compaction_match(*, trigger: str, reason: str, phase: str) -> dict:
    return {
        "request_kind": "compaction",
        "compaction": {
            "trigger": trigger,
            "reason": reason,
            "implementation": "responses_compaction_v2",
            "phase": phase,
            "strategy": "memento",
        },
    }


class CompactionReasonProfileTests(unittest.TestCase):
    def test_user_requested_is_manual_standalone_turn(self) -> None:
        match = compaction_match(
            trigger="manual",
            reason="user_requested",
            phase="standalone_turn",
        )
        self.assertTrue(matches_expected_profile(match, "user_requested"))

    def test_context_limit_accepts_pre_or_mid_turn(self) -> None:
        for phase in ("pre_turn", "mid_turn"):
            with self.subTest(phase=phase):
                match = compaction_match(
                    trigger="auto",
                    reason="context_limit",
                    phase=phase,
                )
                self.assertTrue(matches_expected_profile(match, "context_limit"))

    def test_model_change_reasons_require_pre_turn(self) -> None:
        for reason in ("model_downshift", "comp_hash_changed"):
            with self.subTest(reason=reason):
                match = compaction_match(
                    trigger="auto",
                    reason=reason,
                    phase="pre_turn",
                )
                self.assertTrue(matches_expected_profile(match, reason))

    def test_rejects_wrong_phase(self) -> None:
        match = compaction_match(
            trigger="auto",
            reason="model_downshift",
            phase="mid_turn",
        )
        self.assertFalse(matches_expected_profile(match, "model_downshift"))


if __name__ == "__main__":
    unittest.main()
