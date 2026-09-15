"""结构化根因编码的稳定性、拒绝边界与枚举表身份校验。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_root_cause as root_cause


class StructuredRootCauseTests(unittest.TestCase):
    def test_same_inputs_yield_same_id_across_campaigns(self) -> None:
        """根因 ID 只由稳定输入决定，与 Campaign、时间、路径无关。"""

        first = root_cause.structured_root_cause(
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            failed_step="prepare-official-assertion-bundle",
            stable_dimensions={"phase": "VC-1"},
        )
        second = root_cause.structured_root_cause(
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            failed_step="prepare-official-assertion-bundle",
            stable_dimensions={"phase": "VC-1"},
        )
        self.assertEqual(first, second)
        self.assertTrue(root_cause.is_structured(first))
        other_phase = root_cause.structured_root_cause(
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            failed_step="prepare-official-assertion-bundle",
            stable_dimensions={"phase": "VC-2"},
        )
        self.assertNotEqual(first, other_phase)
        other_step = root_cause.structured_root_cause(
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            failed_step="seal-official-preview",
            stable_dimensions={"phase": "VC-1"},
        )
        self.assertNotEqual(first, other_step)

    def test_describe_exposes_audit_payload_without_diagnostic(self) -> None:
        described = root_cause.describe_root_cause(
            component="vc0-closeout",
            stable_error_code="vc0-closeout.step-failed",
            failed_step="validate-inputs",
        )
        self.assertEqual(
            set(described),
            {
                "algorithm_version",
                "component",
                "stable_error_code",
                "failed_step",
                "stable_dimensions",
                "root_cause_id",
                "codes_sha256",
            },
        )
        self.assertEqual(described["stable_dimensions"], {})
        self.assertEqual(described["algorithm_version"], root_cause.ALGORITHM_VERSION)

    def test_unregistered_code_or_wrong_component_is_rejected(self) -> None:
        with self.assertRaisesRegex(root_cause.RootCauseError, "未登记"):
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="campaign-run.unknown",
                failed_step="x",
                stable_dimensions={"phase": "VC-1"},
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "不能由"):
            root_cause.structured_root_cause(
                component="reconciler",
                stable_error_code="campaign-run.action-failed",
                failed_step="x",
                stable_dimensions={"phase": "VC-1"},
            )

    def test_dimension_keys_must_match_whitelist_exactly(self) -> None:
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="campaign-run.action-failed",
                failed_step="x",
                stable_dimensions={},
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="campaign-run.action-failed",
                failed_step="x",
                stable_dimensions={"phase": "VC-1", "job_id": "official-core"},
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="vc0-closeout",
                stable_error_code="vc0-closeout.step-failed",
                failed_step="x",
                stable_dimensions={"phase": "VC-0"},
            )

    def test_volatile_values_are_rejected_not_stripped(self) -> None:
        """Campaign ID、attempt ID、run ID、时间戳、路径、长摘要一律拒绝。"""

        volatile = (
            "c0154-formal-vc1-bwg-fresh-20260914t223835z",
            "20260914T102852Z-04996800fbbe4e94",
            "run-" + "a" * 64,
            "2026-09-15T08:12:43+00:00",
            "/root/docker/capture-cli/data/runs",
            "step-" + "b" * 32,
        )
        for value in volatile:
            with self.subTest(value=value):
                with self.assertRaisesRegex(root_cause.RootCauseError, "不是稳定根因输入"):
                    root_cause.structured_root_cause(
                        component="vc0-closeout",
                        stable_error_code="vc0-closeout.step-failed",
                        failed_step=value,
                    )
                with self.assertRaisesRegex(root_cause.RootCauseError, "不是稳定根因输入"):
                    root_cause.structured_root_cause(
                        component="supervisor",
                        stable_error_code="campaign-run.action-failed",
                        failed_step="x",
                        stable_dimensions={"phase": value},
                    )

    def test_legacy_literals_map_to_structured_ids(self) -> None:
        mapped = root_cause.legacy_root_cause_id("vc1-parent-lease-deadline-missing")
        self.assertTrue(root_cause.is_structured(mapped))
        self.assertEqual(
            mapped,
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="vc1-parent-lease-deadline-missing",
                failed_step="",
                stable_dimensions={},
            ),
        )
        with self.assertRaisesRegex(root_cause.RootCauseError, "历史字面量"):
            root_cause.legacy_root_cause_id("campaign-run.action-failed")

    def test_codes_identity_is_enforced(self) -> None:
        table = root_cause.load_codes()
        self.assertEqual(len(table["codes_sha256"]), 64)
        root_cause.assert_codes_identity(
            expected_codes_sha256=table["codes_sha256"],
            expected_algorithm_version=root_cause.ALGORITHM_VERSION,
        )
        with self.assertRaisesRegex(root_cause.RootCauseError, "枚举表摘要"):
            root_cause.assert_codes_identity(
                expected_codes_sha256="0" * 64,
                expected_algorithm_version=root_cause.ALGORITHM_VERSION,
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "算法版本"):
            root_cause.assert_codes_identity(
                expected_codes_sha256=table["codes_sha256"],
                expected_algorithm_version="structured-root-cause/v0",
            )

    def test_modified_codes_file_changes_identity_and_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codes.json"
            payload = json.loads(root_cause.DEFAULT_CODES_PATH.read_text("utf-8"))
            payload["codes"]["new.code"] = {
                "component": "reconciler",
                "stable_dimensions": ["phase"],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            table = root_cause.load_codes(path)
            self.assertNotEqual(
                table["codes_sha256"], root_cause.load_codes()["codes_sha256"]
            )
            self.assertTrue(
                root_cause.is_structured(
                    root_cause.structured_root_cause(
                        component="reconciler",
                        stable_error_code="new.code",
                        failed_step="x",
                        stable_dimensions={"phase": "VC-1"},
                        codes=table,
                    )
                )
            )
            payload["codes"]["bad code"] = {"component": "x", "stable_dimensions": []}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(root_cause.RootCauseError, "错误码非法"):
                root_cause.load_codes(path)


if __name__ == "__main__":
    unittest.main()
