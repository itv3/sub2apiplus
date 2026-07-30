"""候选侧 42 条规则严格验收器的离线门禁测试。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.candidate_42_acceptance import (
    ASSERTION_SCHEMA_VERSION,
    command_sha256,
    file_sha256,
    validate_acceptance,
)


class AcceptanceFixture:
    """在临时目录构造一份完整且可通过的 42 条验收归档。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.source_root = root / "source"
        self.evidence_root = root / "evidence"
        self.source_root.mkdir()
        self.evidence_root.mkdir()
        repository_root = Path(__file__).resolve().parents[3]
        self.manifest_path = (
            repository_root
            / "tools"
            / "official_client_capture"
            / "codex_upgrade_rules_0_145_0.json"
        )
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.rule_ids = manifest["required_rules"]

        implementation = self.source_root / "backend/profile.go"
        implementation.parent.mkdir(parents=True)
        implementation.write_text(
            "package backend\n\nfunc ApplyProfileRule() {}\n",
            encoding="utf-8",
        )
        checker = self.source_root / "tools/check_rule.py"
        checker.parent.mkdir(parents=True)
        checker.write_text(
            "#!/usr/bin/env python3\nprint('执行逐规则语义检查')\n",
            encoding="utf-8",
        )

        official = self.evidence_root / "official/codex-0.145.0.pcap"
        candidate = self.evidence_root / "candidate/sub2api.pcap"
        before = self.evidence_root / "restoration/before.json"
        after = self.evidence_root / "restoration/after.json"
        for path in (official, candidate, before, after):
            path.parent.mkdir(parents=True, exist_ok=True)
        official.write_bytes(b"\xd4\xc3\xb2\xa1official-wire-record")
        candidate.write_bytes(b"\xd4\xc3\xb2\xa1candidate-wire-record")
        normalized_state = json.dumps(
            {
                "proxy": None,
                "fallback": None,
                "keeper": "running",
                "account_schedulable": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        before.write_text(normalized_state, encoding="utf-8")
        after.write_text(normalized_state, encoding="utf-8")

        self.implementation_path = "backend/profile.go"
        self.checker_path = "tools/check_rule.py"
        self.official_path = "official/codex-0.145.0.pcap"
        self.candidate_path = "candidate/sub2api.pcap"
        self.before_path = "restoration/before.json"
        self.after_path = "restoration/after.json"
        self.rules = [self._build_rule(rule_id) for rule_id in self.rule_ids]
        self.submission = {
            "schema_version": "codex-candidate-42-acceptance/v1",
            "codex_version": "0.145.0",
            "assessment_id": "unit-test-complete-42",
            "generated_at": "2026-07-30T10:15:00+08:00",
            "rule_manifest_sha256": file_sha256(self.manifest_path),
            "candidate_identity": {
                "git_commit": "c" * 40,
                "source_tree_sha256": "a" * 64,
                "image_reference": "sub2apiplus:acceptance-test-immutable",
                "image_digest": "sha256:" + "b" * 64,
                "deployed_version": "0.1.165-acceptance-test",
            },
            "rules": self.rules,
        }
        self.submission_path = root / "submission.json"
        self.write_submission()

    def _build_rule(self, rule_id: str) -> dict[str, object]:
        command = [
            "python3",
            self.checker_path,
            "--rule",
            rule_id,
        ]
        assertion_result = {
            "schema_version": ASSERTION_SCHEMA_VERSION,
            "rule_id": rule_id,
            "status": "pass",
            "started_at": "2026-07-30T10:05:00+08:00",
            "finished_at": "2026-07-30T10:06:00+08:00",
            "exit_code": 0,
            "checker_sha256": file_sha256(
                self.source_root / self.checker_path
            ),
            "command_sha256": command_sha256(command),
            "checks": [
                {
                    "id": "wire-shape",
                    "description": "候选原始抓包中的出站形态与规则期望一致",
                    "passed": True,
                    "expected": {"rule_id": rule_id, "matched": True},
                    "actual": {"rule_id": rule_id, "matched": True},
                    "evidence_paths": [self.candidate_path],
                }
            ],
        }
        result_relative = f"assertions/{rule_id}.json"
        result_path = self.evidence_root / result_relative
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(assertion_result, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "rule_id": rule_id,
            "implementation": {
                "summary": "该规则由版本画像驱动的候选出站定型实现",
                "locations": [
                    {
                        "path": self.implementation_path,
                        "sha256": file_sha256(
                            self.source_root / self.implementation_path
                        ),
                        "line_start": 1,
                        "line_end": 3,
                        "symbol": "ApplyProfileRule",
                    }
                ],
            },
            "trigger": {
                "description": "使用固定账号与固定请求触发该规则对应场景",
                "preconditions": ["候选镜像健康且抓包环境已完成前置快照"],
                "command": ["capture-driver", "--rule", rule_id],
                "expected_observation": "候选出站原始字节满足该规则定义",
            },
            "official_evidence": {
                "observation": "Codex CLI 0.145.0 官方原始抓包呈现规则定义的形态",
                "artifacts": [
                    {
                        "path": self.official_path,
                        "sha256": file_sha256(
                            self.evidence_root / self.official_path
                        ),
                        "kind": "pcap",
                    }
                ],
            },
            "candidate_raw_evidence": {
                "observation": "Sub2API 候选原始抓包呈现相同规则形态",
                "artifacts": [
                    {
                        "path": self.candidate_path,
                        "sha256": file_sha256(
                            self.evidence_root / self.candidate_path
                        ),
                        "kind": "pcap",
                    }
                ],
            },
            "assertion": {
                "checker": {
                    "path": self.checker_path,
                    "sha256": file_sha256(
                        self.source_root / self.checker_path
                    ),
                },
                "command": command,
                "result": {
                    "path": result_relative,
                    "sha256": file_sha256(result_path),
                    "kind": "assertion_result",
                },
            },
            "environment_restoration": {
                "description": "代理、CA、keeper 和账号调度状态均恢复到抓包前状态",
                "state_pairs": [
                    {
                        "name": "production-normalized-state",
                        "before": {
                            "path": self.before_path,
                            "sha256": file_sha256(
                                self.evidence_root / self.before_path
                            ),
                            "kind": "normalized_state",
                            "captured_at": "2026-07-30T10:00:00+08:00",
                        },
                        "after": {
                            "path": self.after_path,
                            "sha256": file_sha256(
                                self.evidence_root / self.after_path
                            ),
                            "kind": "normalized_state",
                            "captured_at": "2026-07-30T10:10:00+08:00",
                        },
                    }
                ],
            },
        }

    def write_submission(self) -> None:
        self.submission_path.write_text(
            json.dumps(self.submission, ensure_ascii=False),
            encoding="utf-8",
        )

    def write_assertion_result(
        self, index: int, result: dict[str, object]
    ) -> None:
        result_ref = self.submission["rules"][index]["assertion"]["result"]
        result_path = self.evidence_root / result_ref["path"]
        result_path.write_text(
            json.dumps(result, ensure_ascii=False),
            encoding="utf-8",
        )
        result_ref["sha256"] = file_sha256(result_path)
        self.write_submission()

    def validate(self) -> dict[str, object]:
        return validate_acceptance(
            manifest_path=self.manifest_path,
            submission_path=self.submission_path,
            source_root=self.source_root,
            evidence_root=self.evidence_root,
        )


class Candidate42AcceptanceTest(unittest.TestCase):
    def test_complete_42_rule_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(Path(directory))
            report = fixture.validate()
            self.assertTrue(report["accepted"], report["errors"])
            self.assertEqual(report["required_rule_count"], 42)
            self.assertEqual(report["submitted_rule_count"], 42)

    def test_missing_or_unknown_rule_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(Path(directory))
            missing_rule = fixture.submission["rules"].pop()["rule_id"]
            fixture.submission["rules"].append(
                {
                    **copy.deepcopy(fixture.submission["rules"][0]),
                    "rule_id": "SPEC-UNKNOWN-999",
                }
            )
            fixture.write_submission()
            report = fixture.validate()
            codes = {error["code"] for error in report["errors"]}
            self.assertFalse(report["accepted"])
            self.assertIn("missing_rule", codes)
            self.assertIn("unexpected_rule", codes)
            self.assertTrue(
                any(error.get("rule_id") == missing_rule for error in report["errors"])
            )

    def test_each_required_rule_section_is_mandatory(self) -> None:
        sections = (
            "implementation",
            "trigger",
            "official_evidence",
            "candidate_raw_evidence",
            "assertion",
            "environment_restoration",
        )
        for section in sections:
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                fixture = AcceptanceFixture(Path(directory))
                del fixture.submission["rules"][0][section]
                fixture.write_submission()
                report = fixture.validate()
                self.assertFalse(report["accepted"])
                self.assertTrue(
                    any(
                        error["code"] == "missing_field"
                        and error["field"].endswith(f".{section}")
                        for error in report["errors"]
                    )
                )

    def test_directory_and_text_placeholder_do_not_count_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(Path(directory))
            placeholder_directory = fixture.evidence_root / "candidate/empty-directory"
            placeholder_directory.mkdir()
            artifact = fixture.submission["rules"][0]["candidate_raw_evidence"][
                "artifacts"
            ][0]
            artifact["path"] = "candidate/empty-directory"
            fixture.write_submission()
            report = fixture.validate()
            self.assertFalse(report["accepted"])
            self.assertTrue(
                any(error["code"] == "not_regular_file" for error in report["errors"])
            )

        with tempfile.TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(Path(directory))
            placeholder = fixture.evidence_root / fixture.candidate_path
            placeholder.write_text("TODO：待采集", encoding="utf-8")
            artifact = fixture.submission["rules"][0]["candidate_raw_evidence"][
                "artifacts"
            ][0]
            artifact["sha256"] = file_sha256(placeholder)
            fixture.write_submission()
            report = fixture.validate()
            self.assertFalse(report["accepted"])
            self.assertTrue(
                any(error["code"] == "placeholder_artifact" for error in report["errors"])
            )

    def test_exit_zero_without_semantic_checks_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(Path(directory))
            fixture.write_assertion_result(0, {"exit_code": 0})
            report = fixture.validate()
            codes = {error["code"] for error in report["errors"]}
            self.assertFalse(report["accepted"])
            self.assertIn("missing_semantic_checks", codes)
            self.assertIn("assertion_schema_mismatch", codes)

    def test_file_labeled_as_pcap_must_have_real_pcap_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(Path(directory))
            fake_pcap = fixture.evidence_root / "candidate/fake.pcap"
            fake_pcap.write_text(
                "这只是普通文本，不是原始网络抓包文件。",
                encoding="utf-8",
            )
            artifact = fixture.submission["rules"][0]["candidate_raw_evidence"][
                "artifacts"
            ][0]
            artifact["path"] = "candidate/fake.pcap"
            artifact["sha256"] = file_sha256(fake_pcap)
            fixture.write_submission()
            report = fixture.validate()
            self.assertFalse(report["accepted"])
            self.assertTrue(
                any(error["code"] == "invalid_pcap" for error in report["errors"])
            )

    def test_failed_check_or_unbound_raw_evidence_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(Path(directory))
            result_ref = fixture.submission["rules"][0]["assertion"]["result"]
            result_path = fixture.evidence_root / result_ref["path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["checks"][0]["passed"] = False
            result["checks"][0]["evidence_paths"] = ["candidate/not-declared.pcap"]
            fixture.write_assertion_result(0, result)
            report = fixture.validate()
            codes = {error["code"] for error in report["errors"]}
            self.assertFalse(report["accepted"])
            self.assertIn("semantic_check_failed", codes)
            self.assertIn("unknown_candidate_evidence", codes)
            self.assertIn("unreferenced_candidate_evidence", codes)

    def test_changed_environment_state_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(Path(directory))
            after = fixture.evidence_root / fixture.after_path
            after.write_text(
                json.dumps({"proxy": "still-enabled"}),
                encoding="utf-8",
            )
            for rule in fixture.submission["rules"]:
                rule["environment_restoration"]["state_pairs"][0]["after"][
                    "sha256"
                ] = file_sha256(after)
            fixture.write_submission()
            report = fixture.validate()
            self.assertFalse(report["accepted"])
            self.assertTrue(
                any(
                    error["code"] == "environment_not_restored"
                    for error in report["errors"]
                )
            )

    def test_schema_and_example_cover_exact_manifest_universe(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (tool_root / "candidate_42_acceptance.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assertion_schema = json.loads(
            (tool_root / "candidate_rule_assertion_result.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assertion_example = json.loads(
            (tool_root / "candidate_rule_assertion_result.example.json").read_text(
                encoding="utf-8"
            )
        )
        example = json.loads(
            (tool_root / "candidate_42_acceptance.example.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = json.loads(
            (tool_root / "codex_upgrade_rules_0_145_0.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["rules"]["minItems"], 42)
        self.assertEqual(assertion_schema["properties"]["status"]["const"], "pass")
        self.assertEqual(assertion_example["rule_id"], "SPEC-TLS-001")
        self.assertEqual(
            [rule["rule_id"] for rule in example["rules"]],
            manifest["required_rules"],
        )


if __name__ == "__main__":
    unittest.main()
