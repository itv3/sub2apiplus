"""正式候选验收包生成器的离线测试。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.candidate_acceptance_bundle import (
    ASSERTION_INDEX_SCHEMA_VERSION,
    OFFICIAL_MAP_SCHEMA_VERSION,
    RULE_METADATA_SCHEMA_VERSION,
    BundleConfigurationError,
    finalize_bundle,
    run_assertions,
)
from tools.official_client_capture.candidate_evidence_guard import (
    write_state_snapshot,
)
from tools.official_client_capture.candidate_rule_assertion import (
    ASSERTION_SCHEMA_VERSION,
    build_assertion_command,
    command_sha256,
    file_sha256,
)
from tools.official_client_capture.capturelib.security import secure_write_json


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = REPOSITORY_ROOT / "tools/official_client_capture"


class BundleFixture:
    """构造不依赖生产环境的完整组包输入。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.source_root = root / "source"
        self.evidence_root = root / "evidence"
        self.official_root = root / "official-source"
        self.source_root.mkdir()
        self.evidence_root.mkdir()
        self.official_root.mkdir()
        self._copy_source_inputs()

        manifest = json.loads(
            (TOOL_ROOT / "codex_upgrade_rules_0_145_0.json").read_text(
                encoding="utf-8"
            )
        )
        self.rule_ids = manifest["required_rules"]
        self.checker_path = (
            self.source_root
            / "tools/official_client_capture/candidate_rule_assertion.py"
        )
        self.rule_manifest_path = (
            self.source_root
            / "tools/official_client_capture/codex_upgrade_rules_0_145_0.json"
        )

        implementation = self.source_root / "backend/profile.go"
        implementation.parent.mkdir(parents=True)
        implementation.write_text(
            "package backend\n\nfunc ApplyProfileRule() {}\n",
            encoding="utf-8",
        )
        self.implementation = implementation

        candidate = self.evidence_root / "candidate/raw.pcap"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 20)
        self.candidate_relative = "candidate/raw.pcap"
        self.capture_manifest = self.evidence_root / "capture-manifest.json"
        secure_write_json(
            self.capture_manifest,
            {
                "schema_version": "codex-candidate-capture-manifest/v1",
                "codex_version": "0.145.0",
                "capture_id": "bundle-unit-test",
                "status": "complete",
                "artifacts": [
                    {
                        "path": self.candidate_relative,
                        "sha256": file_sha256(candidate),
                        "kind": "pcap",
                        "parser": "pcap_client_hello",
                        "scenario_ids": [f"A{index:02d}" for index in range(1, 16)],
                        "labels": {},
                    }
                ],
            },
        )

        official = self.official_root / "codex-baseline.pcap"
        official.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x01" * 20)
        self.official = official

        self.rule_metadata = root / "rule-metadata.json"
        secure_write_json(
            self.rule_metadata,
            {
                "schema_version": RULE_METADATA_SCHEMA_VERSION,
                "codex_version": "0.145.0",
                "rules": [
                    {
                        "rule_id": rule_id,
                        "implementation": {
                            "summary": "该规则由版本画像驱动的薄层定型实现",
                            "locations": [
                                {
                                    "path": "backend/profile.go",
                                    "line_start": 1,
                                    "line_end": 3,
                                    "symbol": "ApplyProfileRule",
                                }
                            ],
                        },
                        "trigger_command": [
                            "capture-driver",
                            "--rule-id",
                            rule_id,
                        ],
                    }
                    for rule_id in self.rule_ids
                ],
            },
        )

        self.official_map = root / "official-map.json"
        secure_write_json(
            self.official_map,
            {
                "schema_version": OFFICIAL_MAP_SCHEMA_VERSION,
                "codex_version": "0.145.0",
                "rules": [
                    {
                        "rule_id": rule_id,
                        "observation": "Codex CLI 0.145.0 官方原始证据呈现规格形态",
                        "artifacts": [
                            {
                                "path": "codex-baseline.pcap",
                                "sha256": file_sha256(official),
                                "kind": "pcap",
                            }
                        ],
                    }
                    for rule_id in self.rule_ids
                ],
            },
        )

        self.identity = root / "candidate-identity.json"
        secure_write_json(
            self.identity,
            {
                "git_commit": "c" * 40,
                "source_tree_sha256": "a" * 64,
                "image_reference": "sub2apiplus:bundle-unit-test-immutable",
                "image_digest": "sha256:" + "b" * 64,
                "deployed_version": "0.1.165-bundle-unit-test",
            },
        )

        state_source = root / "state-source.json"
        state_source.write_text(
            json.dumps(
                {
                    "keeper": "running",
                    "proxy": None,
                    "account": {
                        "schedulable": True,
                        "credential_sha256": "d" * 64,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.before = self.evidence_root / "restoration/before.json"
        self.after = self.evidence_root / "restoration/after.json"
        write_state_snapshot(state_source, self.before)
        write_state_snapshot(state_source, self.after)
        self.assertions_dir = "assertions/unit-test"
        self._write_assertion_results()

    def _copy_source_inputs(self) -> None:
        files = (
            "docs/CODEX_CLI_0145_EGRESS_SPEC.md",
            "tools/official_client_capture/candidate_rule_expectations_0_145_0.json",
            "tools/official_client_capture/codex_upgrade_rules_0_145_0.json",
            "tools/official_client_capture/candidate_rule_assertion.py",
            "tools/official_client_capture/candidate_42_acceptance.py",
        )
        for relative in files:
            source = REPOSITORY_ROOT / relative
            destination = self.source_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    @staticmethod
    def check(rule_id: str) -> dict[str, object]:
        return {
            "id": "raw-evidence-bound",
            "description": "候选原始证据已绑定到冻结规则检查",
            "passed": True,
            "expected": {"rule_id": rule_id, "matched": True},
            "actual": {"rule_id": rule_id, "matched": True},
            "evidence_paths": ["candidate/raw.pcap"],
        }

    def _write_assertion_results(self) -> None:
        assertion_directory = self.evidence_root.resolve() / self.assertions_dir
        assertion_directory.mkdir(parents=True)
        results: list[dict[str, object]] = []
        for rule_id in self.rule_ids:
            result_path = assertion_directory / f"{rule_id}.result.json"
            command = build_assertion_command(
                rule_id=rule_id,
                capture_manifest=str(self.capture_manifest.resolve()),
                evidence_root=str(self.evidence_root.resolve()),
                output=str(result_path),
                profile=(
                    "tools/official_client_capture/"
                    "candidate_rule_expectations_0_145_0.json"
                ),
                rule_manifest=(
                    "tools/official_client_capture/"
                    "codex_upgrade_rules_0_145_0.json"
                ),
            )
            secure_write_json(
                result_path,
                {
                    "schema_version": ASSERTION_SCHEMA_VERSION,
                    "rule_id": rule_id,
                    "status": "pass",
                    "started_at": "2026-07-30T10:05:00+08:00",
                    "finished_at": "2026-07-30T10:06:00+08:00",
                    "exit_code": 0,
                    "checker_sha256": file_sha256(self.checker_path),
                    "command_sha256": command_sha256(command),
                    "checks": [self.check(rule_id)],
                },
            )
            results.append(
                {
                    "rule_id": rule_id,
                    "path": (
                        f"{self.assertions_dir}/{rule_id}.result.json"
                    ),
                    "sha256": file_sha256(result_path),
                    "status": "pass",
                    "exit_code": 0,
                }
            )
        profile_path = (
            self.source_root
            / "tools/official_client_capture/"
            "candidate_rule_expectations_0_145_0.json"
        )
        secure_write_json(
            assertion_directory / "assertion-index.json",
            {
                "schema_version": ASSERTION_INDEX_SCHEMA_VERSION,
                "codex_version": "0.145.0",
                "generated_at": "2026-07-30T10:06:01+08:00",
                "capture_manifest": {
                    "path": "capture-manifest.json",
                    "sha256": file_sha256(self.capture_manifest),
                },
                "profile_sha256": file_sha256(profile_path),
                "rule_manifest_sha256": file_sha256(self.rule_manifest_path),
                "checker": {
                    "path": (
                        "tools/official_client_capture/"
                        "candidate_rule_assertion.py"
                    ),
                    "sha256": file_sha256(self.checker_path),
                },
                "results": results,
            },
        )

    def finalize(self, *, after_captured_at: str = "2026-07-30T10:10:00+08:00"):
        def recompute(
            profile: object,
            rule_id: str,
            observations: object,
            capture_manifest: object,
        ) -> list[dict[str, object]]:
            del profile, observations, capture_manifest
            return [self.check(rule_id)]

        manifest = json.loads(self.capture_manifest.read_text(encoding="utf-8"))
        with mock.patch(
            "tools.official_client_capture.candidate_acceptance_bundle."
            "load_observations",
            return_value=(manifest, []),
        ), mock.patch(
            "tools.official_client_capture.candidate_acceptance_bundle.evaluate_rule",
            side_effect=recompute,
        ):
            return finalize_bundle(
                source_root=self.source_root,
                evidence_root=self.evidence_root,
                capture_manifest=self.capture_manifest,
                assertions_dir=self.assertions_dir,
                rule_metadata_path=self.rule_metadata,
                official_map_path=self.official_map,
                official_root=self.official_root,
                official_bundle_prefix="official",
                candidate_identity_path=self.identity,
                before_state=self.before,
                before_captured_at="2026-07-30T10:00:00+08:00",
                after_state=self.after,
                after_captured_at=after_captured_at,
                assessment_id="candidate-bundle-unit-test",
                restoration_name="production-normalized-state",
                restoration_description="测试涉及的稳定环境状态已恢复",
            )


class CandidateAcceptanceBundleTest(unittest.TestCase):
    def test_assert_phase_runs_exact_42_independent_checkers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = BundleFixture(Path(directory))
            # 为 assert 阶段选择全新的归档目录，禁止覆盖 fixture 的既有结果。
            assertion_dir = "assertions/fresh-run"
            calls: list[list[str]] = []

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                del kwargs
                calls.append(command)
                rule_id = command[command.index("--rule-id") + 1]
                output = Path(command[command.index("--output") + 1])
                secure_write_json(
                    output,
                    {
                        "schema_version": ASSERTION_SCHEMA_VERSION,
                        "rule_id": rule_id,
                        "status": "pass",
                        "started_at": "2026-07-30T10:05:00+08:00",
                        "finished_at": "2026-07-30T10:05:01+08:00",
                        "exit_code": 0,
                        "checker_sha256": file_sha256(fixture.checker_path),
                        "command_sha256": command_sha256(command),
                        "checks": [fixture.check(rule_id)],
                    },
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch(
                "tools.official_client_capture.candidate_acceptance_bundle."
                "subprocess.run",
                side_effect=fake_run,
            ):
                index = run_assertions(
                    source_root=fixture.source_root,
                    evidence_root=fixture.evidence_root,
                    capture_manifest=fixture.capture_manifest,
                    assertions_dir=assertion_dir,
                )

            self.assertEqual(len(calls), 42)
            self.assertEqual(len(index["results"]), 42)
            self.assertTrue(all(item["status"] == "pass" for item in index["results"]))
            self.assertEqual(
                {command[command.index("--rule-id") + 1] for command in calls},
                set(fixture.rule_ids),
            )
            with self.assertRaisesRegex(BundleConfigurationError, "输出已存在"):
                run_assertions(
                    source_root=fixture.source_root,
                    evidence_root=fixture.evidence_root,
                    capture_manifest=fixture.capture_manifest,
                    assertions_dir=assertion_dir,
                )

    def test_finalize_generates_accepted_42_rule_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = BundleFixture(Path(directory))
            result = fixture.finalize()
            self.assertTrue(result["accepted"])
            self.assertEqual(result["required_rule_count"], 42)
            self.assertEqual(result["submitted_rule_count"], 42)
            self.assertEqual(result["error_count"], 0)
            submission = json.loads(
                Path(result["submission"]).read_text(encoding="utf-8")
            )
            self.assertEqual(len(submission["rules"]), 42)
            first = submission["rules"][0]
            self.assertEqual(
                first["implementation"]["locations"][0]["sha256"],
                file_sha256(fixture.implementation),
            )
            self.assertEqual(
                first["candidate_raw_evidence"]["artifacts"][0]["path"],
                fixture.candidate_relative,
            )
            self.assertTrue(
                (fixture.evidence_root / "official/codex-baseline.pcap").is_file()
            )

    def test_official_digest_mismatch_fails_before_formal_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = BundleFixture(Path(directory))
            mapping = json.loads(fixture.official_map.read_text(encoding="utf-8"))
            mapping["rules"][0]["artifacts"][0]["sha256"] = "0" * 64
            secure_write_json(fixture.official_map, mapping)
            with self.assertRaisesRegex(BundleConfigurationError, "SHA-256 不匹配"):
                fixture.finalize()
            self.assertFalse(
                (fixture.evidence_root / "candidate-42-acceptance.json").exists()
            )

    def test_after_state_must_be_later_than_all_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = BundleFixture(Path(directory))
            with self.assertRaisesRegex(BundleConfigurationError, "assert 阶段"):
                fixture.finalize(
                    after_captured_at="2026-07-30T10:05:30+08:00"
                )


if __name__ == "__main__":
    unittest.main()
