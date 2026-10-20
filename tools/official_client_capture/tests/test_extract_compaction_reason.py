from __future__ import annotations

import unittest

from tools.official_client_capture.extract_compaction_reason import (
    matches_expected_profile,
    unexpected_reason_matches,
)


def compaction_match(*, trigger: str, reason: str, phase: str, model: str = "gpt-6.1-sol") -> dict:
    return {
        "model": model,
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


class UnexpectedReasonTests(unittest.TestCase):
    """降档之后目标模型上的 context_limit 后续压缩不计为非预期，其余形态照旧判非预期。"""

    def downshift(self, model: str = "gpt-6.1-sol") -> dict:
        return compaction_match(trigger="auto", reason="model_downshift", phase="pre_turn", model=model)

    def context_limit(self, model: str, phase: str = "pre_turn") -> dict:
        return compaction_match(trigger="auto", reason="context_limit", phase=phase, model=model)

    def test_downshift_alone_has_nothing_unexpected(self) -> None:
        exact = [self.downshift()]
        self.assertEqual(unexpected_reason_matches(exact, exact, "model_downshift"), [])

    def test_follow_on_context_limit_on_the_target_model_is_not_unexpected(self) -> None:
        # VC-1 首批实采的形态：gpt-6.1-sol 上降档压缩，随后 gpt-6-astra 上因自己更小的阈值再触发一次 context_limit。
        exact = [self.downshift("gpt-6.1-sol")]
        matches = exact + [self.context_limit("gpt-6-astra")]
        self.assertEqual(unexpected_reason_matches(matches, exact, "model_downshift"), [])

    def test_extra_context_limit_on_the_downshift_model_is_still_unexpected(self) -> None:
        exact = [self.downshift("gpt-6.1-sol")]
        extra = self.context_limit("gpt-6.1-sol")
        self.assertEqual(unexpected_reason_matches(exact + [extra], exact, "model_downshift"), [extra])

    def test_other_reasons_after_the_downshift_are_still_unexpected(self) -> None:
        exact = [self.downshift("gpt-6.1-sol")]
        for reason in ("comp_hash_changed", "user_requested"):
            with self.subTest(reason=reason):
                extra = compaction_match(trigger="auto", reason=reason, phase="pre_turn", model="gpt-6-astra")
                self.assertEqual(unexpected_reason_matches(exact + [extra], exact, "model_downshift"), [extra])

    def test_follow_on_is_only_tolerated_when_a_downshift_actually_matched(self) -> None:
        # 没有精确匹配（降档压缩根本没出现）时，目标模型上的 context_limit 就是孤立的非预期压缩。
        extra = self.context_limit("gpt-6-astra")
        self.assertEqual(unexpected_reason_matches([extra], [], "model_downshift"), [extra])

    def test_other_expected_reasons_get_no_tolerance(self) -> None:
        for expected in ("context_limit", "comp_hash_changed", "user_requested"):
            with self.subTest(expected=expected):
                exact = [compaction_match(trigger="auto", reason=expected, phase="pre_turn", model="gpt-6.1-sol")]
                extra = self.context_limit("gpt-6-astra") if expected != "context_limit" else self.downshift("gpt-6-astra")
                self.assertEqual(unexpected_reason_matches(exact + [extra], exact, expected), [extra])


if __name__ == "__main__":
    unittest.main()
