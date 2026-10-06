"""Claude 根因码表（Claude 升级控制面设计 D5、B1-06）。

覆盖：共享加载器加载 Claude 码表；复制的码与 Codex 码表全部非 legacy 码逐项对应、稳定维度追加 client；新增五个
Claude 码；维度缺 client 即拒绝；同码同步骤因 client 不同而根因 ID 不同；Claude Campaign ID 作维度值经适配器前置
检查被拒；Codex 码表与 Codex 适配器行为不变。
"""

from __future__ import annotations

import unittest

from tools.official_client_capture import codex_upgrade_root_cause as rc
from tools.official_client_capture import upgrade_client_adapter as uca

CLAUDE_CODES = {
    "claude.capture.relay-failed": ("claude-capture", ("phase", "client", "scenario")),
    "claude.capture.auth-gate": ("claude-capture", ("phase", "client")),
    "claude.bundle.extract-failed": ("claude-bundle", ("phase", "client")),
    "claude.store.gate-rejected": ("claude-store", ("phase", "client", "fact_kind")),
    "claude.delivery.stage-failed": ("claude-delivery", ("phase", "client", "stage")),
}


class ClaudeRootCauseCodesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = uca.adapter_for("claude")
        self.table = rc.load_codes(self.adapter.root_cause_codes_path)
        self.codex = rc.load_codes()

    def test_copies_every_non_legacy_codex_code_with_client_dimension(self) -> None:
        copied = {code: entry for code, entry in self.codex["codes"].items() if not entry["legacy"]}
        self.assertEqual(len(copied), 18)
        for code, entry in self.codex["codes"].items():
            with self.subTest(code=code):
                if entry["legacy"]:
                    self.assertNotIn(code, self.table["codes"])
                    continue
                claude = self.table["codes"][code]
                self.assertEqual(claude["component"], entry["component"])
                self.assertEqual(claude["stable_dimensions"], (*entry["stable_dimensions"], "client"))
                self.assertFalse(claude["legacy"])

    def test_claude_specific_codes(self) -> None:
        self.assertEqual(set(self.table["codes"]) - set(self.codex["codes"]), set(CLAUDE_CODES))
        for code, (component, dimensions) in CLAUDE_CODES.items():
            with self.subTest(code=code):
                self.assertEqual(self.table["codes"][code]["component"], component)
                self.assertEqual(self.table["codes"][code]["stable_dimensions"], dimensions)

    def test_every_code_requires_client_dimension(self) -> None:
        for code, entry in self.table["codes"].items():
            with self.subTest(code=code):
                self.assertIn("client", entry["stable_dimensions"])
                dimensions = {key: "x" for key in entry["stable_dimensions"] if key != "client"}
                with self.assertRaisesRegex(rc.RootCauseError, "维度键"):
                    rc.describe_root_cause(
                        component=entry["component"], stable_error_code=code, failed_step="step",
                        stable_dimensions=dimensions, codes=self.table,
                    )

    def test_client_dimension_separates_root_cause_ids(self) -> None:
        def root_cause(client: str) -> str:
            return rc.structured_root_cause(
                component="supervisor", stable_error_code="campaign-run.action-failed", failed_step="capture-official",
                stable_dimensions={"phase": "VC-1", "client": client}, codes=self.table,
            )

        self.assertNotEqual(root_cause("claude"), root_cause("codex"))
        self.assertEqual(root_cause("claude"), root_cause("claude"))

    def test_claude_campaign_id_rejected_before_root_cause_api(self) -> None:
        for value in (
            "claude-code-2_1_226-fw-g-production-replacement-v2-20260821",
            "claude-code-2.1.286-formal-20261010t120000z",
            "scenario-for-claude-code-2.1.286",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(rc.RootCauseError, "Claude Campaign ID"):
                self.adapter.reject_volatile_dimensions({"phase": "VC-1", "client": "claude", "scenario": value})
        self.adapter.reject_volatile_dimensions({"phase": "VC-1", "client": "claude", "scenario": "tui-oauth-basic"})
        self.adapter.reject_volatile_dimensions({"phase": "VC-1", "client": "claude", "scenario": "claude-fw-e-test"})

    def test_codex_table_and_adapter_unchanged(self) -> None:
        codex = uca.adapter_for("codex")
        self.assertEqual(rc.load_codes(codex.root_cause_codes_path), self.codex)
        self.assertEqual(len(self.codex["codes"]), 26)
        self.assertNotEqual(self.table["codes_sha256"], self.codex["codes_sha256"])


if __name__ == "__main__":
    unittest.main()
