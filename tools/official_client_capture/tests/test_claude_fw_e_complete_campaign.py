"""Claude Code 2.1.226 完整候选分母测试。"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.claude_fw_e_complete_campaign import (
    EXPECTED_COUNTS,
    CompleteCampaignError,
    build_denominator,
    freeze_campaign,
    validate_denominator,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


class ClaudeFWCompleteCampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = build_denominator(
            REPO_ROOT,
            "claude-code-2_1_226-fw-e-complete-v4-test",
        )

    def test_five_orthogonal_denominators_are_exact(self) -> None:
        self.assertEqual(self.value["counts"], EXPECTED_COUNTS)
        self.assertEqual(self.value["total_orthogonal_candidates"], 593)

    def test_every_candidate_requires_target_measurement(self) -> None:
        for items in self.value["candidate_groups"].values():
            self.assertTrue(items)
            self.assertTrue(
                all(item["required_target_conclusion"] is True for item in items)
            )
            self.assertTrue(
                all(item["target_measurement_status"] == "pending" for item in items)
            )

    def test_scope_reduction_and_unmeasured_boundary_are_forbidden(self) -> None:
        policy = self.value["closure_policy"]
        self.assertFalse(policy["allow_unmeasured_feature_boundary"])
        self.assertFalse(policy["support_envelope_reduction_closes_candidate"])
        self.assertFalse(policy["telemetry_absence_generates_rule"])
        self.assertFalse(policy["nonessential_absence_generates_rule"])

    def test_count_drift_fails_closed(self) -> None:
        changed = copy.deepcopy(self.value)
        changed["candidate_groups"]["target_send_points"].pop()
        with self.assertRaisesRegex(CompleteCampaignError, "分母应为 331"):
            validate_denominator(changed)

    def test_output_is_append_only(self) -> None:
        from tools.official_client_capture.claude_fw_e_complete_campaign import (
            _write_new_json,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "denominator.json"
            _write_new_json(output, self.value)
            with self.assertRaisesRegex(CompleteCampaignError, "拒绝覆盖"):
                _write_new_json(output, self.value)

    def test_complete_campaign_binds_catalog_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "complete-v4"
            manifest = freeze_campaign(
                REPO_ROOT,
                "claude-code-2_1_226-complete-v4-test",
                output,
            )

            self.assertEqual(manifest["candidate_denominator"]["total"], 593)
            self.assertEqual(manifest["scenario_catalog"]["probe_count"], 77)
            expected_complete = [
                path
                for path in (
                    REPO_ROOT / "local-analysis/fw-f/claude-code-2.1.226"
                ).iterdir()
                if path.is_dir()
                and path.name.startswith("complete-v")
                and len(path.name.rsplit("-", 1)[-1]) == 12
            ]
            self.assertEqual(
                len(manifest["historical_evidence"]), 3 + len(expected_complete)
            )
            bound_paths = {
                item["path"] for item in manifest["historical_evidence"]
            }
            self.assertTrue(
                all(
                    str(path.relative_to(REPO_ROOT)) in bound_paths
                    for path in expected_complete
                )
            )
            self.assertTrue(
                all(
                    item["preservation"] == "read_only_not_overwritten"
                    for item in manifest["historical_evidence"]
                )
            )
            self.assertTrue((output / "campaign.json").is_file())
            self.assertTrue((output / "candidate-denominator.json").is_file())
            self.assertTrue((output / "scenario-catalog.json").is_file())
            frozen_source = output / "source/tools/official_client_capture"
            self.assertTrue((frozen_source / "claude_fw_e_relay.py").is_file())
            self.assertTrue(
                (frozen_source / "claude_fw_f_complete_runner.py").is_file()
            )
            self.assertTrue((frozen_source / "runtime_host_receipt.py").is_file())
            self.assertFalse(any(frozen_source.rglob("*.pyc")))
            self.assertFalse(any(path.name == "__pycache__" for path in frozen_source.rglob("*")))
            for item in manifest["capture_source_bundle"]["files"]:
                path = frozen_source / item["path"]
                self.assertEqual(path.stat().st_size, item["size"])
            with self.assertRaisesRegex(CompleteCampaignError, "拒绝覆盖"):
                freeze_campaign(
                    REPO_ROOT,
                    "claude-code-2_1_226-complete-v4-test",
                    output,
                )


if __name__ == "__main__":
    unittest.main()
