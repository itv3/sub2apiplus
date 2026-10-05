"""阶段参数解析：原生路径规则、当前身份绑定及入口失败前零副作用。"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import runpy
import sys
import unittest
from unittest import mock

from tools.arm64_capture_driver.driver import phase_context
from tools.official_client_capture import codex_upgrade as native
from tools.official_client_capture.tests import test_arm64_round_context as root_tests
from tools.official_client_capture.tests.test_arm64_capture_driver import SCRIPTS


class PhaseContextTests(unittest.TestCase):
    def setUp(self):
        base = root_tests.RoundContextTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.base = base
        self.config, self.campaign, self.cu = base.config, base.campaign, base.upgrade
        self.config["RETIRE_VERSION"] = "0.150.0"
        self.root, self.write = base.root, base.write
        self.baseline, self.commit, self.reopened = 0, None, None
        self.cu._current_evaluation_baseline.side_effect = lambda *args: (self.baseline, self.commit)
        self.cu._evaluation_baseline_dir.side_effect = native._evaluation_baseline_dir
        self.cu._candidate_build_receipt_path.side_effect = native._candidate_build_receipt_path
        self.cu._capture_attempt_path.side_effect = native._capture_attempt_path
        self.cu._stage_read_source.side_effect = self.read_source
        self.cu._stage_write_target.side_effect = self.write_target
        self.cu._vc_checkpoint_path.side_effect = self.checkpoint_path
        self.cu._vc_completion_receipt_path.side_effect = self.receipt_path
        self.cu._replay_vc_checkpoint.side_effect = lambda c, p, phase, revision=None: (self.checkpoint_path(c, phase, revision=revision), {})
        self.cu._load_stage_result.side_effect = lambda c, stage, candidate, **kwargs: json.loads(self.read_source(c, candidate, self.baseline, stage)["path"].read_text())
        self.cu.codex_upgrade_vc_artifacts = mock.Mock(wraps=native.codex_upgrade_vc_artifacts)
        self.build = {"source": {"root": str(Path(self.config["B"]) / "source"), "git_commit": self.config["C"], "tree_sha256": "4" * 64},
                      "profile": {"profile_id": self.config["PROFILE_ID"], "profile_digest": "3" * 64},
                      "image": {"image_id": "sha256:" + "5" * 64, "reference": "repo@sha256:" + "5" * 64},
                      "build": {"build_id": "build-current"}, "target_version": self.config["TARGET_VERSION"],
                      "receipt_digest": "8" * 64, "campaign_id": self.config["NEW"], "candidate_id": self.config["CAND"],
                      "campaign_manifest_sha256": hashlib.sha256((self.campaign / "campaign.json").read_bytes()).hexdigest()}
        self.cu.codex_upgrade_vc_artifacts.validate_candidate_build_receipt.return_value = self.build
        self.write(native._candidate_build_receipt_path(self.campaign, self.config["CAND"]), self.build)
        self.cu._replay_candidate_build_receipt.return_value = (self.build, {})
        self.attempt_id = "20261005T000000Z-0123456789abcdef"
        self.attempt_root = self.campaign / "candidates" / self.config["CAND"] / "attempts" / self.attempt_id
        self.attempt_root.mkdir(parents=True)
        self.attempt = {"status": "awaiting_receipts", "attempt_id": self.attempt_id, "run_nonce": "6" * 64,
                        "started_at_utc": "2026-10-05T00:00:00Z", "identity": {
                            "source_root": self.build["source"]["root"], "source_tree_sha256": "4" * 64,
                            "git_commit": self.config["C"], "image_reference": self.build["image"]["reference"],
                            "image_id": self.build["image"]["image_id"], "build_id": "build-current",
                            "deployed_version": self.config["TARGET_VERSION"], "profile_id": self.config["PROFILE_ID"], "profile_digest": "3" * 64}}
        self.write(self.attempt_root / "attempt.json", self.attempt)
        self.cu._load_capture_attempt.return_value = self.attempt_root, self.attempt
        self.cu._active_unsealed_attempts.return_value = [self.config["CAND"] + ":" + self.attempt_id]
        self.cu._load_evaluation_run_index.return_value = ({"checked": True}, {})

    def read_source(self, campaign, candidate, baseline, stage):
        with mock.patch.object(native, "_read_evaluation_baseline_commit", return_value=self.commit):
            return native._stage_read_source(campaign, candidate, baseline, stage)

    def write_target(self, campaign, candidate, baseline, stage):
        with mock.patch.object(native, "_read_evaluation_baseline_commit", return_value=self.commit):
            return native._stage_write_target(campaign, candidate, baseline, stage)

    def checkpoint_path(self, campaign, phase, revision=None):
        with mock.patch.object(native, "_vc5_reopened_baseline", return_value=self.reopened):
            return native._vc_checkpoint_path(campaign, phase, revision=revision)

    def receipt_path(self, campaign, candidate, kind):
        with mock.patch.object(native, "_vc5_reopened_baseline", return_value=self.reopened):
            return native._vc_completion_receipt_path(campaign, candidate, kind)

    def baseline_two(self, *, reuse_capture=False):
        self.baseline = 2
        candidate = self.config["CAND"]
        self.commit = {"stage_sources": {stage: {"source": "local", "target": f"{root}/{candidate}/revisions/b2" + ("" if stage == "assertions" else "/result.json")}
                                           for stage, root in (("capture-candidate", "candidates"), ("compare", "comparisons"), ("assertions", "assertions"), ("accept", "acceptance"))}}
        if reuse_capture:
            reference = self.capture_result(baseline=0)
            self.commit["stage_sources"]["capture-candidate"] = {"source": "reused", "baseline": 0,
                                                                  "path": str(reference.relative_to(self.campaign)),
                                                                  "sha256": hashlib.sha256(reference.read_bytes()).hexdigest()}
        self.write(native._evaluation_baseline_dir(self.campaign, candidate, 2) / "COMMIT", self.commit)

    def capture_result(self, baseline=None):
        path = self.read_source(self.campaign, self.config["CAND"], self.baseline if baseline is None else baseline, "capture-candidate")["path"]
        build = native._candidate_build_receipt_path(self.campaign, self.config["CAND"])
        identity = {"build_receipt": {"path": str(build.relative_to(self.campaign)), "sha256": hashlib.sha256(build.read_bytes()).hexdigest(),
                                      "bytes": build.stat().st_size}, "build_receipt_digest": self.build["receipt_digest"]}
        self.write(path, {"status": "complete", "identity": identity, "attempt": {"path": str((self.attempt_root / "attempt.json").relative_to(self.campaign)),
                                      "sha256": hashlib.sha256((self.attempt_root / "attempt.json").read_bytes()).hexdigest()}})
        return path

    def resolve(self, **kwargs):
        return phase_context.resolve(self.config, upgrade=self.cu, **kwargs)

    def batch(self, phase="VC-5", predecessor="VC-4"):
        self.write(Path(self.config["W"]) / "plan.json", {"schema_version": "codex-upgrade-vc-action-plan/v1",
                                                         "execute_item_ids": [], "reuse_item_ids": [], "actions": []})
        self.write(self.checkpoint_path(self.campaign, predecessor, revision=int(self.config.get("CANDIDATE_REVISION") or 1)), {})
        return {"mode": "batch", "phase": phase, "predecessor": predecessor, "campaign_id": self.config["NEW"],
                "inputs": self.config["IN"], "plan_name": "plan.json"}

    def test_native_r1_and_active_r2_ignore_pending_r99(self):
        for revision in (1, 2):
            with self.subTest(revision=revision):
                self.base.activate(revision)
                self.write(self.campaign / "control/vc/revisions/r99/vc-4-checkpoint.json", {"pending": True})
                args = self.batch()
                self.write(self.checkpoint_path(self.campaign, "VC-4", revision=revision), {})
                result = self.resolve(**args)
                self.assertEqual(result["parameters"]["PRED_CKPT"], str(self.checkpoint_path(self.campaign, "VC-4", revision=revision)))
                self.assertNotIn("r99", result["parameters"]["PRED_CKPT"])

    def test_current_baseline_read_and_write_paths_are_separate(self):
        self.baseline_two(reuse_capture=True)
        params = self.resolve()["parameters"]
        self.assertEqual(params["EVALUATION_BASELINE"], "2")
        self.assertEqual(params["CAPTURE_WRITE"], "")
        self.assertTrue(params["CAPTURE_RESULT"].endswith(f'{self.config["CAND"]}/result.json'))
        for stage in ("COMPARE", "ACCEPT"):
            self.assertTrue(params[stage + "_WRITE"].endswith("/revisions/b2/result.json"))
        self.assertTrue(params["ASSERTIONS_WRITE"].endswith("/revisions/b2"))
        self.assertNotEqual(params["GATE_ROOT"], self.config["G"])

    def test_reused_source_digest_drift_is_rejected(self):
        self.baseline_two(reuse_capture=True)
        self.capture_result(baseline=0).write_text("{}")
        with self.assertRaisesRegex(native.ConfigurationError, "摘要漂移"):
            self.resolve()

    def test_current_attempt_does_not_follow_directory_mtime(self):
        stale = self.attempt_root.parent / "20990101T000000Z-ffffffffffffffff"
        self.write(stale / "attempt.json", {"old": True})
        result = self.resolve(mode="attempt")
        self.assertEqual(result["parameters"]["ATT"], self.attempt_id)
        self.cu._load_capture_attempt.assert_called_with(self.campaign, "candidate", self.config["CAND"], self.attempt_id,
                                                       _verified_campaign_manifest=self.base.manifest)

    def test_sealed_current_attempt_comes_from_stage_binding(self):
        self.baseline_two(reuse_capture=True)
        self.cu._active_unsealed_attempts.return_value = []
        self.assertEqual(self.resolve(mode="attempt")["parameters"]["ATT"], self.attempt_id)

    def test_explicit_old_attempt_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "显式 ATT"):
            self.resolve(mode="attempt", attempt_id="old-attempt")

    def test_missing_reserved_ambiguous_and_conflicting_attempts_are_rejected(self):
        scope = self.config["CAND"] + ":"
        for active in ([], [scope + self.attempt_id + ":reserved_or_interrupted"], [scope + self.attempt_id, scope + "another"]):
            with self.subTest(active=active):
                self.cu._active_unsealed_attempts.return_value = active
                with self.assertRaises(ValueError):
                    self.resolve(mode="attempt")
        self.capture_result()
        self.cu._active_unsealed_attempts.return_value = [scope + "another"]
        with self.assertRaises(ValueError):
            self.resolve(mode="attempt")

    def test_foreign_candidate_attempt_is_not_selected(self):
        self.cu._active_unsealed_attempts.return_value = ["other:" + self.attempt_id]
        with self.assertRaisesRegex(ValueError, "尚无有效"):
            self.resolve(mode="attempt")

    def test_attempt_digest_and_build_identity_must_match(self):
        path = self.capture_result()
        payload = json.loads(path.read_text()); payload["attempt"]["sha256"] = "0" * 64; self.write(path, payload)
        with self.assertRaisesRegex(ValueError, "attempt 路径或摘要"):
            self.resolve(mode="attempt")
        self.capture_result()
        for key in ("image_id", "build_id", "profile_digest", "source_root", "deployed_version"):
            original = self.attempt["identity"][key]
            with self.subTest(key=key):
                self.attempt["identity"][key] = "old"
                with self.assertRaisesRegex(ValueError, key):
                    self.resolve(mode="attempt")
            self.attempt["identity"][key] = original

    def test_native_attempt_replay_errors_are_never_bypassed(self):
        self.cu._load_capture_attempt.side_effect = ValueError("证据权限边界漂移")
        before = self.base.snapshot()
        with self.assertRaisesRegex(ValueError, "证据权限"):
            self.resolve(mode="attempt")
        self.assertEqual(before, self.base.snapshot())

    def test_build_source_and_commit_mismatch_fail_closed(self):
        for key in ("root", "git_commit"):
            old = self.build["source"][key]
            self.build["source"][key] = "old"
            with self.assertRaisesRegex(ValueError, "构建收据"):
                self.resolve(mode="build")
            self.build["source"][key] = old

    def test_completion_reopened_paths_use_native_replay(self):
        self.base.activate(2); self.reopened = 3
        report = self.resolve(mode="attempt")["parameters"]
        self.assertIn("/r2/reopen-b3/", report["VC5_CHECKPOINT"])
        self.assertIn("/reopen-b3/", report["VC5_RECEIPT"])
        self.assertIn("/r2/vc-6-", report["VC6_CHECKPOINT"])
        self.write(Path(report["VC5_RECEIPT"]), {"receipt": True})
        self.write(Path(report["VC5_CHECKPOINT"]), {"checkpoint": True})
        self.assertEqual(self.resolve(mode="attempt")["parameters"]["VC5_COMPLETE"], "1")
        self.cu._replay_vc_completion.assert_called_with(self.campaign, self.base.manifest, phase="VC-5",
                                                       candidate_id=self.config["CAND"], attempt_id=self.attempt_id)

    def test_existing_completion_is_not_enough_when_native_replay_fails(self):
        self.write(self.receipt_path(self.campaign, self.config["CAND"], "vc5_completion"), {})
        self.cu._replay_vc_completion.side_effect = ValueError("完成收据被篡改")
        with self.assertRaisesRegex(ValueError, "篡改"):
            self.resolve(mode="attempt")

    def test_assertion_result_without_index_fails_closed(self):
        path = self.read_source(self.campaign, self.config["CAND"], 0, "assertions")["path"]
        self.write(path, {})
        self.cu._load_evaluation_run_index.return_value = None
        with self.assertRaisesRegex(ValueError, "索引"):
            self.resolve()

    def test_batch_old_campaign_input_and_non_direct_predecessor_rejected(self):
        args = self.batch()
        for key, value in (("campaign_id", "old"), ("inputs", "old"), ("predecessor", "VC-3"), ("plan_name", "../old.json")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolve(**{**args, key: value})

    def test_symlink_in_phase_path_is_rejected(self):
        root = self.campaign / "comparisons"
        target = self.root / "old-comparisons"; target.mkdir()
        root.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.resolve()

    def test_read_only_repeatable_without_identity_cache_writes(self):
        before = self.base.snapshot()
        with mock.patch.dict(os.environ, {"CODEX_UPGRADE_IDENTITY_MEMO": str(self.root / "memo")}):
            one = self.resolve(mode="attempt")
            self.assertEqual(one, self.resolve(mode="attempt"))
            self.assertEqual(os.environ["CODEX_UPGRADE_IDENTITY_MEMO"], str(self.root / "memo"))
        self.assertEqual(before, self.base.snapshot())

    def test_input_mutation_during_replay_is_rejected(self):
        def mutate(*args):
            self.write(native._candidate_build_receipt_path(self.campaign, self.config["CAND"]), {"changed": True})
            return self.build, {}
        self.cu._replay_candidate_build_receipt.side_effect = mutate
        with self.assertRaisesRegex(ValueError, "已绑定文件"):
            self.resolve()

    def test_sealed_path_read_does_not_require_deleted_build_tree_or_authorize_dispatch(self):
        self.capture_result()
        self.cu._replay_candidate_build_receipt.side_effect = ValueError("构建实物缺失")
        self.assertEqual(self.resolve(mode="attempt")["parameters"]["BUILD_REPLAY_STATE"], "sealed_receipt_binding")
        with self.assertRaisesRegex(ValueError, "构建实物缺失"):
            self.resolve(mode="build")

    def test_sealed_build_binding_cannot_be_replaced(self):
        path = self.capture_result()
        payload = json.loads(path.read_text())
        payload["identity"]["build_receipt"]["sha256"] = "0" * 64
        self.write(path, payload)
        with self.assertRaisesRegex(ValueError, "未绑定当前构建"):
            self.resolve(mode="attempt")

    def test_receipt_checkpoint_time_must_match_current_attempt(self):
        path = self.attempt_root / "evidence/environment/client-after/probe-manifest.json"
        self.write(path, {"observed_at_utc": "2026-10-05T00:01:00Z"})
        self.resolve(mode="attempt", client_checkpoint_at_utc="2026-10-05T00:01:00Z")
        with self.assertRaisesRegex(ValueError, "checkpoint 时间"):
            self.resolve(mode="attempt", client_checkpoint_at_utc="2026-10-04T00:01:00Z")

    def test_unsuccessful_stage_cannot_be_skipped(self):
        path = self.capture_result()
        payload = json.loads(path.read_text()); payload["status"] = "failed"; self.write(path, payload)
        with self.assertRaisesRegex(ValueError, "尚未成功完成"):
            self.resolve()

    def vc6_context(self):
        context = phase_context.Context(self.config, upgrade=self.cu)
        context.stage_paths(); context.build(); context.attempt()
        self.cu._canonical_latest_checkpoint.return_value = {"campaign": {"campaign_id": self.config["NEW"],
            "candidate_id": self.config["CAND"], "attempt_id": self.attempt_id, "target_version": self.config["TARGET_VERSION"],
            "campaign_manifest_sha256": context.bindings["campaign"]["sha256"]},
            "plan": {"execute_item_ids": ["production-activation", "rollback-verification", "retire-" + self.config["RETIRE_VERSION"]]},
            "items": [{"item_id": "acceptance", "source": {"path": "acceptance.json", "sha256": "9" * 64}}]}
        path = self.campaign / "canonical/checkpoints/00000001.json"
        self.write(path, self.cu._canonical_latest_checkpoint.return_value)
        self.cu._canonical_checkpoint_file.return_value = path
        self.cu._canonical_item_index.side_effect = native._canonical_item_index
        self.cu.CANONICAL_REMOVAL_RECEIPT_SCHEMA = native.CANONICAL_REMOVAL_RECEIPT_SCHEMA
        return context

    def vc6_plan(self, context):
        from tools.official_client_capture.tests.test_codex_upgrade_vc6_canonical_dispatch import advance_action
        activation = self.campaign / "control/current/activation.json"
        removal = self.campaign / "control/current/removal.json"
        self.write(activation, {"activation": "本轮"})
        self.write(removal, {"schema_version": native.CANONICAL_REMOVAL_RECEIPT_SCHEMA, "status": "complete",
            "campaign_id": self.config["NEW"], "active_version": self.config["TARGET_VERSION"],
            "rollback_version": self.config["BASELINE_VERSION"], "removed_version": self.config["RETIRE_VERSION"],
            "production_activation_sha256": hashlib.sha256(activation.read_bytes()).hexdigest(),
            "consumer_scan": {"catalog_references": 0, "selector_references": 0, "unknown_references": 0},
            "runtime_catalog_removed": True, "historical_evidence_preserved": True})
        actions = [advance_action("step-1", "production-activation", "production-activation", step_receipt=str(activation)),
                   advance_action("step-2", "rollback-verification", "rollback-verification", step_receipt=str(activation)),
                   advance_action("step-3", "retire-" + self.config["RETIRE_VERSION"], "retire", step_receipt=str(removal), retire_version=self.config["RETIRE_VERSION"])]
        for action in actions:
            command = action["command"]
            for flag, value in (("--campaign-dir", str(self.campaign)), ("--candidate-id", self.config["CAND"]), ("--attempt-id", self.attempt_id)):
                command[command.index(flag) + 1] = value
        return {"actions": actions, "execute_item_ids": [item for action in actions for item in action["item_ids"]]}

    def test_vc6_three_step_receipts_bind_current_acceptance_without_writing(self):
        context = self.vc6_context()
        plan = self.vc6_plan(context)
        before = self.base.snapshot()
        phase_context._vc6_receipts(context, plan)
        self.assertEqual(self.cu._canonical_activation_receipt.call_count, 2)
        self.cu._canonical_production_step.assert_not_called()
        self.assertEqual(before, self.base.snapshot())

    def test_completed_vc6_replay_preserves_native_byte_count_binding(self):
        context = self.vc6_context(); plan = self.vc6_plan(context)
        checkpoint = self.cu._canonical_latest_checkpoint.return_value
        for action in plan["actions"]:
            item = native.codex_upgrade_vc_artifacts.canonical_action_binding(action)
            path = Path(item["step_receipt"])
            # 直接用原生绑定生成器，避免夹具遗漏 bytes 后掩盖真实协议差异。
            source = native._canonical_file_binding(self.campaign, path, "已完成步骤收据")
            checkpoint["items"].append({"item_id": item["item_id"], "source": source})
        phase_context._vc6_receipts(context, plan)
        checkpoint["items"][-1]["source"]["bytes"] += 1
        with self.assertRaisesRegex(ValueError, "已完成步骤"):
            phase_context._vc6_receipts(context, plan)

    def test_vc6_wrong_attempt_and_activation_replay_failure_are_rejected(self):
        context = self.vc6_context(); plan = self.vc6_plan(context)
        wrong = copy.deepcopy(plan)
        for action in wrong["actions"]:
            command = action["command"]; command[command.index("--attempt-id") + 1] = "old"
        with self.assertRaisesRegex(ValueError, "VC-6 计划未绑定"):
            phase_context._vc6_receipts(context, wrong)
        self.cu._canonical_activation_receipt.side_effect = ValueError("激活收据篡改")
        with self.assertRaisesRegex(ValueError, "篡改"):
            phase_context._vc6_receipts(context, plan)

    def test_vc6_removal_requires_current_versions_and_activation_digest(self):
        for key in ("active_version", "production_activation_sha256", "removed_version"):
            with self.subTest(key=key):
                context = self.vc6_context(); plan = self.vc6_plan(context)
                path = self.campaign / "control/current/removal.json"
                payload = json.loads(path.read_text()); payload[key] = "old"; self.write(path, payload)
                with self.assertRaisesRegex(ValueError, "退役收据"):
                    phase_context._vc6_receipts(context, plan)

    def generate(self, *, old_candidate_dir=None):
        report = self.resolve(mode="attempt")
        fake_config, fake_admission, fake_phase = mock.Mock(), mock.Mock(), mock.Mock()
        fake_config.load_config.return_value = self.config
        fake_config.candidate_job_ids.return_value = ["job-one", "job-two"]
        fake_phase.resolve.return_value = report
        fake_admission.Admission.return_value.consume.return_value = {"effective_parameters": {}, "bindings": {
            "campaign": self.config["NEW"], "candidate": self.config["CAND"], "image_id": self.build["image"]["image_id"], "build_id": "build-current"}}
        argv = [str(SCRIPTS / "gen_vc5_plans.py"), self.config["W"], self.config["NEW"], self.config["CAND"],
                self.build["image"]["image_id"], "build-current", self.attempt_id]
        with mock.patch.dict(sys.modules, {"driver_config": fake_config, "vc5_admission": fake_admission, "phase_context": fake_phase}), \
                mock.patch.dict(os.environ, {"CANDIDATE_DIR": old_candidate_dir or self.config["B"]}), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(argv[0], run_name="__main__")
        return report

    def test_plan_generator_uses_b2_and_never_writes_reused_capture(self):
        self.baseline_two(reuse_capture=True)
        self.generate()
        output = Path(self.config["W"])
        self.assertFalse((output / "action-plan-vc5-run.json").exists())
        self.assertFalse((output / "action-plan-vc5-seal-checkpoint.json").exists())
        command = json.loads((output / "action-plan-vc5-assert.json").read_text())["actions"][0]["command"]
        self.assertEqual(command[command.index("--evaluation-baseline") + 1], "2")
        self.assertIn("/revisions/b2/results.json", command[command.index("--output") + 1])
        context = phase_context.Context(self.config, upgrade=self.cu)
        context.stage_paths(); context.build(materialized=False)
        for path in output.glob("*.json"):
            phase_context._vc5_plan(context, json.loads(path.read_text()))

    def test_old_candidate_dir_is_rejected_before_plan_directory_creation(self):
        output = Path(self.config["W"])
        with self.assertRaisesRegex(SystemExit, "CANDIDATE_DIR"):
            self.generate(old_candidate_dir="/old/candidate")
        self.assertFalse(output.exists())

    def test_vc5_old_output_baseline_and_reused_write_are_rejected(self):
        self.baseline_two(reuse_capture=True)
        self.generate()
        context = phase_context.Context(self.config, upgrade=self.cu)
        context.stage_paths(); context.build(materialized=False)
        path = Path(self.config["W"]) / "action-plan-vc5-assert.json"
        plan = json.loads(path.read_text())
        for flag, value in (("--evaluation-baseline", "0"), ("--output", "/old/results.json")):
            with self.subTest(flag=flag):
                bad = copy.deepcopy(plan); command = bad["actions"][0]["command"]
                command[command.index(flag) + 1] = value
                with self.assertRaisesRegex(ValueError, flag):
                    phase_context._vc5_plan(context, bad)
        with self.assertRaisesRegex(ValueError, "reused"):
            phase_context._vc5_plan(context, {"execute_item_ids": ["candidate-seal"], "actions": []})

    def test_standalone_without_bytecode_flag_keeps_driver_read_only(self):
        driver = self.root / "standalone"; driver.mkdir()
        for name in ("phase_context.py", "round_context.py", "parse_env.py"):
            (driver / name).write_bytes((SCRIPTS / name).read_bytes())
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONPATH"}}
        before = self.base.snapshot()
        result = subprocess.run([sys.executable, str(driver / "phase_context.py"), "--env", str(self.base.fixture.env_file)],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(before, self.base.snapshot())
        self.assertFalse((driver / "__pycache__").exists())

    def test_all_entrypoints_reject_before_creating_runroot(self):
        env_file = self.root / "invalid.env"
        runroot = self.root / "must-not-exist"
        env_file.write_text(self.base.fixture.env_file.read_text().replace(str(self.base.fixture.runroot), str(runroot)))
        for script, args in (("vc5-start.sh", []), ("vc5-all.sh", []), ("vc5-recover.sh", ["/bad-preview"]),
                             ("vc5-seal.sh", [self.attempt_id]), ("vc5-accept.sh", [self.attempt_id]),
                             ("vc5-canonical2.sh", [self.attempt_id]), ("vc5-kilo.sh", [self.attempt_id]),
                             ("vc5-seal-receipts.sh", [self.attempt_id, "now"]),
                             ("vc-batch.sh", [self.config["NEW"], self.config["IN"], "VC-6", "2", "VC-5", "plan.json"])):
            with self.subTest(script=script):
                result = subprocess.run(["bash", str(SCRIPTS / script), *args],
                                        env={**os.environ, "ARM64_VC_ENV": str(env_file), "PYTHONDONTWRITEBYTECODE": "1"},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("阶段解析拒绝", result.stderr)
                self.assertFalse(runroot.exists())


if __name__ == "__main__":
    unittest.main()
