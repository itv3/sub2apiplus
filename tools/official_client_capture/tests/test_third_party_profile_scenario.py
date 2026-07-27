from __future__ import annotations

import struct
import unittest

from tools.official_client_capture.run_third_party_profile_scenario import (
    anthropic_payload,
    openai_payload,
    websocket_frame,
)


class ThirdPartyProfileScenarioTest(unittest.TestCase):
    def test_openai_payload_contains_profile_breakers_and_business_fields(self) -> None:
        payload = openai_payload("gpt-5.4")

        self.assertEqual("required", payload["tool_choice"])
        self.assertEqual(123, payload["max_output_tokens"])
        self.assertTrue(payload["store"])
        self.assertFalse(payload["stream"])
        self.assertEqual("none", payload["reasoning"]["context"])
        self.assertEqual("THIRD_PARTY_NONLITE_SYSTEM_V1", payload["instructions"])
        self.assertEqual("third_party_lookup", payload["tools"][0]["name"])

    def test_anthropic_payload_exercises_system_and_dynamic_beta(self) -> None:
        payload = anthropic_payload("claude-opus-4-8")

        self.assertEqual(3, len(payload["system"]))
        self.assertTrue(payload["tools"][0]["custom"]["defer_loading"])

    def test_websocket_frame_is_masked_and_round_trips(self) -> None:
        payload = b"profile-contract"
        frame = websocket_frame(payload)

        self.assertEqual(1, frame[0] & 0x0F)
        self.assertTrue(frame[1] & 0x80)
        length = frame[1] & 0x7F
        offset = 2
        if length == 126:
            length = struct.unpack("!H", frame[offset : offset + 2])[0]
            offset += 2
        self.assertEqual(len(payload), length)
        mask = frame[offset : offset + 4]
        encoded = frame[offset + 4 :]
        decoded = bytes(
            byte ^ mask[index % 4] for index, byte in enumerate(encoded)
        )
        self.assertEqual(payload, decoded)


if __name__ == "__main__":
    unittest.main()
