"""Codex CLI 0.149.1 合并终态收据测试。"""

from __future__ import annotations

import copy
import json
import unittest

from tools import check_ledger_completeness as ledger


class Codex01491TerminalStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipt = json.loads(
            ledger.CODEX_01491_TERMINAL_STATE.read_text(encoding="utf-8")
        )

    def test_terminal_state_is_complete(self) -> None:
        additions = ledger.validate_codex_01491_terminal_state(self.receipt)
        self.assertEqual(
            [item["path"] for item in additions],
            ["backend/internal/officialegress/routing_hint.go"],
        )

    def test_identity_drift_is_rejected(self) -> None:
        drifted = copy.deepcopy(self.receipt)
        drifted["result"] = "drifted"
        with self.assertRaisesRegex(RuntimeError, "顶层事实或自摘要"):
            ledger.validate_codex_01491_terminal_state(drifted)


if __name__ == "__main__":
    unittest.main()
