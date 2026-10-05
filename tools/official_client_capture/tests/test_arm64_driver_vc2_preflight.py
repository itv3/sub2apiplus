"""A-03：使用真实 HTTP 原始字节和正式判据器，隔离批准／采集的外部副作用。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture.tests.test_arm64_capture_driver import REPO_ROOT, SCRIPTS, _write_json, load_script


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.module = load_script("vc2_assertion_preflight")
        self.config = {"D": str(self.root), "NEW": "isolated-a03", "IN": "a03-inputs", "TARGET_VERSION": "0.160.0",
                       "PROFILE_ID": "isolated-profile", "ACTIVE_PROFILE": str(self.root / "active.json"),
                       "PROFILE_PATCH_JSON": str(self.root / "patch.json")}
        self.inputs = self.root / "control/a03-inputs"
        self.inputs.mkdir(parents=True)
        source = json.loads((REPO_ROOT / "tools/official_client_capture/candidate_rule_expectations_0_160_0.json").read_text())
        rule = next(item for item in source["rules"] if item["rule_id"] == "SPEC-EP-022")
        checks = {item["id"]: item for item in rule["checks"]}
        self.profile = {**source, "rules": [{**rule, "checks": [checks["edit-file-id-reference"], checks["edit-json-data-url"]]}],
                        "scenarios": [{**next(item for item in source["scenarios"] if item["scenario_id"] == "A09"),
                                       "required_artifact_kinds": ["relay_binary"]}]}
        # 类型化输入由正式 HTTP 解析器产生，报告不能包含该敏感值。
        self.bundle = self.root / "predecessor/assertion-bundle"
        self.bundle.mkdir(parents=True)
        self.raw = self.bundle / "request.bin"
        body = b'{"images":[{"file_id":"fixture-private-file-id"}]}'
        self.raw.write_bytes(b'POST /backend-api/codex/images/edits HTTP/1.1\r\nHost: example.test\r\n'
                            b'Content-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        self.capture_path = self.bundle / "capture-manifest.json"
        self.capture = {"schema_version": self.module.checker.CAPTURE_MANIFEST_SCHEMA_VERSION, "codex_version": "0.160.0",
                        "capture_id": "isolated-official", "status": "complete", "artifacts": [{"path": self.raw.name,
                        "sha256": self.module.checker.file_sha256(self.raw), "kind": "relay_binary", "parser": "h1_request_stream",
                        "scenario_ids": ["A09"], "labels": {"image_edit_input": "file_id"}}]}
        _write_json(self.capture_path, self.capture)
        self.official = {"status": "complete", "predecessor_import": {"path": "isolated-import.json", "sha256": "a" * 64},
                         "assertion_context": {"evidence_root": str(self.bundle), "capture_manifest_path": str(self.capture_path),
                         "capture_manifest": {"path": self.capture_path.name, "sha256": self.module.checker.file_sha256(self.capture_path)},
                         "evidence_prefix": "original/assertion-bundle"}}
        _write_json(Path(self.config["ACTIVE_PROFILE"]), {"fixture": "active"})
        _write_json(Path(self.config["PROFILE_PATCH_JSON"]), {"fixture": "patch"})
        self.write_inputs()

    def write_inputs(self):
        values = {"target_rule_manifest": {"required_rules": ["SPEC-EP-022"]}, "migration_manifest": {"status": "approved"},
                  "scenario_manifest": {"profile_id": "isolated-profile"},
                  "profile_manifest": {"profile_id": "isolated-profile", "profile_digest": "a" * 64},
                  "assertion_profile_manifest": self.profile}
        for key, value in values.items():
            _write_json(self.inputs / self.module.INPUTS[key], value)
        self.joint = self.module.upgrade._fingerprint({key: self.module.upgrade._normalized_json_sha256(value) for key, value in values.items()})

    def collect(self):
        # 仅隔离正式 Campaign 控制链，画像加载、原始字节解析、判据执行及报告绑定均使用真实实现。
        with mock.patch.object(self.module.upgrade, "load_campaign_manifest", return_value={"campaign_id": "isolated-a03", "target_version": "0.160.0"}), \
             mock.patch.object(self.module.upgrade, "classify_campaign", return_value={"status": "approval_required", "joint_manifest_sha256": self.joint}) as preview, \
             mock.patch.object(self.module.upgrade, "_load_stage_result", return_value=self.official) as sealed:
            result = self.module.collect(self.config, self.joint)
        self.assertNotIn("approve_manifest_sha256", preview.call_args.kwargs)
        self.assertEqual(sealed.call_args.args[1], "capture-official")
        self.assertFalse(sealed.call_args.kwargs.get("_skip_evidence_scan", False))
        return result

    def test_mixed_rule_wire_is_executed_internal_is_pending_and_report_redacts_values(self):
        result = self.collect()
        self.assertEqual(result["results"]["counts"], {"passed": 1, "pending_candidate": 1})
        self.assertEqual(result["results"]["checks"][0]["validation_mode"], "candidate_profile")
        self.assertEqual(result["results"]["candidate_acceptance"], "required_at_vc5")
        self.assertNotIn("fixture-private-file-id", json.dumps(result))
        self.assertIn("predecessor", result["capture_manifest"]["path"])

    def test_wrong_fullmatch_is_failed_and_all_candidate_checks_cannot_make_it_green(self):
        self.profile["rules"][0]["checks"][0]["assertion"]["value"] = "^str:"
        self.write_inputs()
        result = self.collect()
        self.assertEqual(result["results"]["status"], "failed")
        self.assertEqual(result["results"]["counts"], {"failed": 1, "pending_candidate": 1})

    def test_fullmatch_empty_literal_and_typed_values_have_distinct_semantics(self):
        observation = self.module.checker.Observation("a", "A09", "http_request", "a.bin", ("a.bin",), {}, {"value": ""})
        for value, pattern, expected in [("", "^str:", False), ("str:", "^str:", True), ("str:opaque", "^str:", False),
                                         ("", "^$", True), ("str:opaque", "^str:.+$", True)]:
            actual = self.module.checker.Observation(**{**observation.__dict__, "data": {"value": value}})
            passed, _ = self.module.checker._evaluate_assertion([actual], {"operator": "all_match", "path": "data.value", "value": pattern})
            self.assertEqual(passed, expected)

    def test_empty_selection_and_missing_scenario_artifacts_cannot_pass(self):
        for mutation in ("selector", "coverage"):
            with self.subTest(mutation=mutation):
                profile = copy.deepcopy(self.profile)
                if mutation == "selector":
                    profile["rules"][0]["checks"][0]["select"]["where"][0]["value"] = "/missing"
                else:
                    profile["scenarios"][0]["required_artifact_kinds"].append("pcap")
                capture, observations = self.module.checker.load_observations(self.capture_path, self.bundle, "0.160.0")
                self.assertEqual(self.module.evaluate(profile, capture, observations)["status"], "failed")

    def test_zero_count_is_valid_but_still_requires_original_scenario_evidence(self):
        check = self.profile["rules"][0]["checks"][0]
        check["assertion"] = {"operator": "count_equal", "value": 0}
        check["select"]["where"][0]["value"] = "/retired"
        self.write_inputs()
        self.assertEqual(self.collect()["results"]["status"], "passed")
        self.profile["scenarios"][0]["required_artifact_kinds"].append("pcap")
        self.write_inputs()
        self.assertEqual(self.collect()["results"]["status"], "failed")

    def test_internal_invalid_regex_is_rejected_without_candidate_samples(self):
        self.profile["rules"][0]["checks"][1]["assertion"] = {"operator": "all_match", "path": "data.value", "value": "["}
        self.write_inputs()
        with self.assertRaises(self.module.re.error):
            self.collect()

    def test_unregistered_record_type_and_selector_scenario_are_rejected(self):
        original = copy.deepcopy(self.profile)
        for field, value in (("record_type", "unregistered"), ("scenario_ids", ["A99"])):
            self.profile = copy.deepcopy(original)
            self.profile["rules"][0]["checks"][1]["select"][field] = value
            self.write_inputs()
            with self.assertRaises((ValueError, self.module.contract.AcceptanceContractError)):
                self.collect()

    def test_deferred_candidate_operand_shape_is_checked(self):
        for assertion in ({"operator": "all_equal", "path": "data.x"},
                          {"operator": "count_equal", "value": True},
                          {"operator": "all_fields_equal", "left_path": "", "right_path": "data.x"},
                          {"operator": "all_match", "path": "data.x", "value": ".*", "typo": True}):
            self.profile["rules"][0]["checks"][1]["assertion"] = assertion
            self.write_inputs()
            with self.assertRaises(ValueError):
                self.collect()

    def test_profile_version_and_rule_universe_cannot_be_relabelled(self):
        self.profile["codex_version"] = "0.157.0"
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, "版本"):
            self.collect()
        self.profile["codex_version"] = "0.160.0"
        self.write_inputs()
        _write_json(self.inputs / "target-rules.json", {"required_rules": ["SPEC-EP-001"]})
        documents = {key: json.loads((self.inputs / name).read_text()) for key, name in self.module.INPUTS.items()}
        self.joint = self.module.upgrade._fingerprint({key: self.module.upgrade._normalized_json_sha256(value) for key, value in documents.items()})
        with self.assertRaisesRegex(ValueError, "规则全集"):
            self.collect()

    def test_failed_report_and_cross_round_report_are_not_consumable(self):
        self.profile["rules"][0]["checks"][0]["assertion"]["value"] = "^str:"
        self.write_inputs()
        path = self.module.save_report(self.config, self.collect())
        with self.assertRaisesRegex(ValueError, "未通过"):
            self.module.consume(self.config, self.joint, path)
        with self.assertRaisesRegex(ValueError, "不属于"):
            self.module.consume({**self.config, "IN": "other-inputs"}, self.joint, path)

    def test_missing_or_changed_raw_evidence_fails_closed(self):
        self.raw.write_bytes(self.raw.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.collect()
        self.raw.unlink()
        with self.assertRaises(ValueError):
            self.collect()

    def test_input_joint_version_and_campaign_changes_are_rejected(self):
        for field in ("NEW", "TARGET_VERSION", "PROFILE_ID"):
            old = self.config[field]
            self.config[field] = "wrong"
            with self.assertRaises((ValueError, OSError)):
                self.collect()
            self.config[field] = old
        _write_json(self.inputs / "rule-migration.json", {"status": "changed"})
        with self.assertRaisesRegex(ValueError, "联合摘要"):
            self.collect()

    def test_official_source_contract_failure_cannot_be_bypassed(self):
        with mock.patch.object(self.module.upgrade, "load_campaign_manifest", side_effect=self.module.upgrade.ConfigurationError("正式链错误")):
            with self.assertRaisesRegex(self.module.upgrade.ConfigurationError, "正式链错误"):
                self.module.collect(self.config, self.joint)

    def test_report_is_content_addressed_readonly_on_repeat_and_replayed(self):
        before = {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*")}
        result = self.collect()
        self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*")})
        path = self.module.save_report(self.config, result)
        first = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(self.module.save_report(self.config, result), path)
        self.assertEqual(first, (path.read_bytes(), path.stat().st_mtime_ns))
        with mock.patch.object(self.module, "collect", return_value=result) as replay:
            self.module.consume(self.config, self.joint, path)
            replay.assert_called_once_with(self.config, self.joint)
        changed = copy.deepcopy(result)
        changed["official_stage_sha256"] = "b" * 64
        with mock.patch.object(self.module, "collect", return_value=changed):
            with self.assertRaisesRegex(ValueError, "重放不一致"):
                self.module.consume(self.config, self.joint, path)
        stored = json.loads(path.read_text())
        stored["result"]["results"]["counts"] = {}
        _write_json(path, stored)
        with self.assertRaisesRegex(ValueError, "篡改"):
            self.module.consume(self.config, self.joint, path)

    def test_actual_target_universe_is_fully_enumerated(self):
        profile = self.module.checker.load_profile(REPO_ROOT / "tools/official_client_capture/candidate_rule_expectations_0_160_0.json",
            REPO_ROOT / "tools/official_client_capture/codex_upgrade_rules_0_160_0.json", verify_frozen_digest=False, expected_codex_version="0.160.0")
        result = self.module.evaluate(profile, {"artifacts": []}, [])
        self.assertEqual((result["rule_count"], result["check_count"]), (43, 138))
        self.assertEqual(result["counts"], {"failed": 94, "pending_candidate": 44})


class ApprovalSequenceTests(unittest.TestCase):
    def test_real_shell_sequence_stops_before_approval_or_stage(self):
        for fail in ("", "record", "verify-before", "verify-after", "preview", "approve"):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                drv = root / "driver"
                drv.mkdir()
                campaign = root / "campaign"
                (campaign / "control/vc").mkdir(parents=True)
                log = root / "actions.jsonl"
                shutil.copy2(SCRIPTS / "vc2-approve-and-stage.sh", drv)
                (drv / "lib.sh").write_text('export DRV="' + str(drv) + '" D="' + str(root) + '" NEW=isolated IN=inputs NEWDIR="' + str(campaign) + '"\n')
                (drv / "vc-batch.sh").write_text('''#!/bin/bash
set -eu
case "$4" in 4) step=preview;; 5) step=approve;; 6) step=stage;; esac
echo "$step" >> "$A03_LOG"
test "$step" != "$A03_FAIL"
if [ "$step" = approve ]; then touch "$NEWDIR/control/vc/vc-2-checkpoint.json"; fi
if [ "$step" = stage ]; then touch "$NEWDIR/control/vc/vc-3-checkpoint.json"; fi
echo ok
''')
                (drv / "vc2_assertion_preflight.py").write_text('''import os, sys
from pathlib import Path
step = sys.argv[1]
if step == "verify":
    step += "-after" if (Path(os.environ["NEWDIR"]) / "control/vc/vc-2-checkpoint.json").exists() else "-before"
with open(os.environ["A03_LOG"], "a") as stream: stream.write(step + "\\n")
if step == os.environ["A03_FAIL"]: sys.exit(3)
print("isolated-report.json")
''')
                result = subprocess.run(["bash", str(drv / "vc2-approve-and-stage.sh"), "a" * 64],
                    env=dict(os.environ, A03_LOG=str(log), A03_FAIL=fail, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, text=True, timeout=30)
                steps = ["preview", "record", "verify-before", "approve", "verify-after", "stage"]
                expected = steps[:steps.index(fail) + 1] if fail else steps
                self.assertEqual(log.read_text().splitlines(), expected)
                self.assertEqual(result.returncode == 0, not bool(fail), result.stderr)

    def test_installed_standalone_entry_loads_data_root_from_parameters(self):
        from tools.official_client_capture.tests.test_arm64_capture_driver import _DriverFixture
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = _DriverFixture(root)
            installed = root / "installed/driver"
            installed.mkdir(parents=True)
            for name in ("vc2_assertion_preflight.py", "driver_config.py", "parse_env.py"):
                shutil.copy2(SCRIPTS / name, installed / name)
            values = load_script("parse_env").parse(fixture.env_file.read_text())
            data = Path(values["D"])
            data.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(data / "tools")
            # 与正式安装一致：工具根必须是普通目录，不能靠链接绕过根校验。
            shutil.copytree(REPO_ROOT / "tools", data / "tools", ignore=shutil.ignore_patterns("__pycache__"))
            env = {k: v for k, v in os.environ.items() if k not in {"D", "PYTHONPATH"}}
            env.update(ARM64_VC_ENV=str(fixture.env_file), PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run(["python3", "-B", str(installed / "vc2_assertion_preflight.py"), "--help"],
                                    env=env, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dry-run", result.stdout)


if __name__ == "__main__":
    unittest.main()
