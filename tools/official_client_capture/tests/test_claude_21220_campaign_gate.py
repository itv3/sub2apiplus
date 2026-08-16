from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.claude_21220 import check_coverage as checker


def _catalog_entry(run_id: str, relative: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "paths": [relative],
        "sha256_by_path": {relative: "a" * 64},
    }


def _synthetic_catalog() -> tuple[dict[str, object], dict[str, str]]:
    catalog: dict[str, object] = {}
    categories: dict[str, str] = {}
    serial = 0
    for report_id, expected_categories in checker.PENDING_REPORT_CATEGORY_MAPPING.items():
        grouped: dict[str, list[dict[str, object]]] = {}
        flat: list[dict[str, object]] = []
        for category, count in expected_categories.items():
            group: list[dict[str, object]] = []
            for _ in range(count):
                serial += 1
                run_id = f"run-{serial:02d}"
                relative = (
                    f"{checker.PENDING_CAPTURE_RELATIVE}/runs/{run_id}/manifest.json"
                )
                group.append(_catalog_entry(run_id, relative))
                categories[run_id] = category
            grouped[category.removeprefix("agent-")] = group
            flat.extend(group)
        catalog[report_id] = grouped if report_id == "AGENT-depths" else flat
    return catalog, categories


class ClaudePendingCampaignGateTests(unittest.TestCase):
    def test_report_catalog_projection_covers_nine_groups_and_22_runs(self) -> None:
        report_catalog, categories = _synthetic_catalog()
        errors: list[str] = []
        projected, actual_categories = checker.project_pending_report_catalogs(
            report_catalog, errors
        )
        self.assertEqual(errors, [])
        self.assertEqual(set(projected), set(checker.PENDING_REPORT_CATALOG_MAPPING.values()))
        self.assertEqual(actual_categories, categories)
        self.assertEqual(len(actual_categories), 22)

    def test_report_catalog_projection_rejects_hash_coverage_and_duplicate_run(self) -> None:
        report_catalog, _ = _synthetic_catalog()
        negative = report_catalog["HEADER-negative"]
        client = report_catalog["HEADER-client"]
        self.assertIsInstance(negative, list)
        self.assertIsInstance(client, list)
        negative[0]["sha256_by_path"] = {}
        client[0]["run_id"] = negative[1]["run_id"]
        errors: list[str] = []
        checker.project_pending_report_catalogs(report_catalog, errors)
        self.assertTrue(any("未精确覆盖 paths" in error for error in errors))
        self.assertTrue(any("运行重复归档" in error for error in errors))

    def test_full_gate_replays_report_and_checks_rule_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            analyzer_path = root / checker.PENDING_ANALYZER_RELATIVE
            secret_tool_path = root / checker.POST_RUN_SECRET_TOOL_RELATIVE
            capture_root = root / checker.PENDING_CAPTURE_RELATIVE
            report_path = capture_root / "pending-evidence-analysis.json"
            analyzer_path.parent.mkdir(parents=True)
            capture_root.mkdir(parents=True)
            analyzer_path.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "sys.stdout.buffer.write((Path(sys.argv[1]) / "
                "'pending-evidence-analysis.json').read_bytes())\n",
                encoding="utf-8",
            )
            secret_tool_path.write_text("# synthetic post-run scanner\n", encoding="utf-8")
            analyzer_sha256 = hashlib.sha256(analyzer_path.read_bytes()).hexdigest()
            secret_tool_sha256 = hashlib.sha256(
                secret_tool_path.read_bytes()
            ).hexdigest()
            report_catalog, categories = _synthetic_catalog()
            # 合成 campaign 的产出侧工具树保持一致：22 个 run 都用同一份 driver 与 addon。
            synthetic_driver_sha256 = "5" * 64
            synthetic_addon_sha256 = "6" * 64
            for run_id in sorted(categories):
                run_manifest = capture_root / "runs" / run_id / "manifest.json"
                run_manifest.parent.mkdir(parents=True, exist_ok=True)
                run_manifest.write_text(
                    json.dumps(
                        {
                            "runtime": {
                                "capture_tools": {
                                    "execution_sources": {
                                        "files": [
                                            {
                                                "path": "capturelib/scenarios.py",
                                                "sha256": synthetic_driver_sha256,
                                            },
                                            {
                                                "path": "addons/mitm_capture.py",
                                                "sha256": synthetic_addon_sha256,
                                            },
                                        ]
                                    }
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
            run_bindings = [
                {
                    "run_id": run_id,
                    "category": categories[run_id],
                    "manifest_sha256": "1" * 64,
                    "receipt_sha256": "2" * 64,
                    "post_run_secret_receipt_sha256": "3" * 64,
                    "source_bundle_sha256": "4" * 64,
                }
                for run_id in sorted(categories)
            ]
            report = {
                "schema_version": "claude-code-2.1.220-pending-evidence-analysis/v1",
                "passed": True,
                "campaign_name": capture_root.name,
                "analyzer_identity": {
                    "path": checker.PENDING_ANALYZER_RELATIVE,
                    "sha256": analyzer_sha256,
                },
                "run_count": 22,
                "matrix_counts": checker.EXPECTED_PENDING_MATRIX_COUNTS,
                "campaign_binding_sha256": checker.canonical_json_sha256(
                    run_bindings
                ),
                "run_bindings": run_bindings,
                "integrity": {
                    field: 22
                    for field in (
                        "complete_m_runs",
                        "manifest_secret_scan_self_report_consistent_runs",
                        "exact_secret_scan_passed_runs",
                        "post_run_secret_scan_verified_runs",
                        "host_receipt_sha_verified_runs",
                        "artifact_inventory_verified_runs",
                        "unique_post_run_secret_receipts",
                        "unique_receipts",
                        "unique_run_nonces",
                    )
                },
                "evidence_catalog": report_catalog,
            }
            report["integrity"]["post_run_secret_scan_tool_sha256"] = (
                secret_tool_sha256
            )
            report_bytes = (
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode()
            report_path.write_bytes(report_bytes)
            projection_errors: list[str] = []
            projected, _ = checker.project_pending_report_catalogs(
                report_catalog, projection_errors
            )
            self.assertEqual(projection_errors, [])
            rules = {
                "common_scope": {
                    "verified_campaign": {
                        "archive_root": checker.PENDING_CAPTURE_RELATIVE,
                        "complete_runs": 22,
                        "analyzer_path": checker.PENDING_ANALYZER_RELATIVE,
                        "analyzer_sha256": analyzer_sha256,
                        "report_path": checker.PENDING_REPORT_RELATIVE,
                        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
                        "post_run_secret_scan_tool_path": (
                            checker.POST_RUN_SECRET_TOOL_RELATIVE
                        ),
                        "post_run_secret_scan_tool_sha256": secret_tool_sha256,
                        "campaign_binding_sha256": report[
                            "campaign_binding_sha256"
                        ],
                        "matrix_counts": checker.EXPECTED_PENDING_MATRIX_COUNTS,
                        "capture_toolchain_consistency": {
                            "consistent": True,
                            "driver_variants": [
                                {
                                    "sha256": synthetic_driver_sha256,
                                    "run_count": 22,
                                    "source_recoverable": True,
                                }
                            ],
                            "addon_variants": [
                                {
                                    "sha256": synthetic_addon_sha256,
                                    "run_count": 22,
                                    "source_recoverable": True,
                                }
                            ],
                            "unreproducible_driver_runs": 0,
                        },
                    }
                },
                "evidence_catalog": projected,
            }
            errors: list[str] = []
            with (
                mock.patch.object(checker, "ROOT", root),
                mock.patch.object(checker, "PENDING_ANALYZER_PATH", analyzer_path),
                mock.patch.object(checker, "PENDING_CAPTURE_ROOT", capture_root),
                mock.patch.object(checker, "PENDING_REPORT_PATH", report_path),
                mock.patch.object(
                    checker, "POST_RUN_SECRET_TOOL_PATH", secret_tool_path
                ),
            ):
                checker.validate_pending_evidence_campaign(rules, errors)
            self.assertEqual(errors, [])

            analyzer_path.write_text(
                analyzer_path.read_text(encoding="utf-8")
                + "sys.stdout.buffer.write(b' ')\n",
                encoding="utf-8",
            )
            replay_errors: list[str] = []
            with (
                mock.patch.object(checker, "ROOT", root),
                mock.patch.object(checker, "PENDING_ANALYZER_PATH", analyzer_path),
                mock.patch.object(checker, "PENDING_CAPTURE_ROOT", capture_root),
                mock.patch.object(checker, "PENDING_REPORT_PATH", report_path),
                mock.patch.object(
                    checker, "POST_RUN_SECRET_TOOL_PATH", secret_tool_path
                ),
            ):
                checker.validate_pending_evidence_campaign(rules, replay_errors)
            self.assertTrue(
                any("不是字节级等价" in error for error in replay_errors)
            )


if __name__ == "__main__":
    unittest.main()
