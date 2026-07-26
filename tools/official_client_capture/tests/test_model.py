"""任务矩阵与 URL 边界测试。"""

from __future__ import annotations

import datetime as dt
import unittest

from tools.official_client_capture.capturelib.model import (
    ConfigurationError,
    build_suite_plans,
    make_batch_id,
    normalized_api_urls,
)


class PlanTest(unittest.TestCase):
    def test_all_keeps_two_independent_campaigns(self) -> None:
        plans = build_suite_plans(
            task="all",
            batch_id="20260726T010203Z",
            scenarios=("s1", "s2", "s4"),
            evidence_modes=("direct", "mitm"),
            sub2api_base_url="https://gateway.example.com",
            api_key_env="SUB2API_CAPTURE_API_KEY",
        )
        self.assertEqual([plan.task for plan in plans], ["oauth", "api"])
        self.assertEqual([len(plan.cases) for plan in plans], [6, 6])
        self.assertNotEqual(plans[0].run_id, plans[1].run_id)
        self.assertTrue(all(case.task == "oauth" for case in plans[0].cases))
        self.assertTrue(all(case.task == "api" for case in plans[1].cases))
        self.assertFalse(any(plan.external_ab_executed for plan in plans))

    def test_api_uses_public_https_and_codex_v1(self) -> None:
        claude, codex, host = normalized_api_urls(
            "https://gateway.example.com/prefix/v1"
        )
        self.assertEqual(claude, "https://gateway.example.com/prefix")
        self.assertEqual(codex, "https://gateway.example.com/prefix/v1")
        self.assertEqual(host, "gateway.example.com")

    def test_api_rejects_plaintext_local_entry(self) -> None:
        for value in (
            "http://gateway.example.com",
            "https://127.0.0.1:18081",
            "https://localhost/v1",
            "https://gateway.example.com/v1?token=x",
            "https://gateway.example.com:not-a-port/v1",
            "https://gateway.example.com:70000/v1",
        ):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                normalized_api_urls(value)

    def test_batch_id_is_stable_utc(self) -> None:
        value = make_batch_id(dt.datetime(2026, 7, 26, 1, 2, 3, tzinfo=dt.timezone.utc))
        self.assertEqual(value, "20260726T010203Z")


if __name__ == "__main__":
    unittest.main()
