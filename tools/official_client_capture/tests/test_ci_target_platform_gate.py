"""B-10：单一全集来源、差异选择、动态失效、正式证据和归档边界。"""
from __future__ import annotations

import copy
import contextlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import target_platform_gate as target
from tools.ci import full_set_receipt as fs
from tools.ci import unit_records as records
from tools.ci import unit_executor as executor
from tools.official_client_capture.tests import test_ci_full_set_receipt as fixture
from tools.official_client_capture.tests import test_ci_unit_records as record_fixture


class TargetPlatformGateTests(unittest.TestCase):
    write = fixture.FullSetReceiptTests.write
    refresh_dynamic = fixture.FullSetReceiptTests.refresh_dynamic

    def setUp(self):
        fixture.FullSetReceiptTests.setUp(self)
        self.binding.update(architecture="aarch64", os_version="Linux-fixture", kernel_version="linux-6-fixture")
        self.request["context"] = self.write("target-context.json", self.context)
        self.receipt = fs.issue(self.source, self.context, self.clock)
        self.request["receipt"] = self.write("target-receipt.json", self.receipt)
        self.request["schema_version"] = target.REQUEST_SCHEMA
        self.request["reuse_enabled"] = True
        self.request["target"] = {"campaign_id": self.binding["campaign"], "candidate_id": self.binding["candidate"],
                                  "profile_digest": self.binding["target_profile_digest"], "target_architecture": "linux/arm64",
                                  "candidate_image_id": "sha256:" + "d" * 64, "candidate_source_tree_sha256": "3" * 64}
        approvals = []
        for index, ref in enumerate(self.request["approvals"]):
            value = fs.replay_ref(ref)
            value["scope"] = target.CONTRACT
            value["binding_sha256"] = fs.digest(self.binding)
            value["target_sha256"] = fs.digest(self.request["target"])
            approvals.append(self.write(f"target-approval-{index}.json", value))
        self.request["approvals"] = approvals
        self.request["platform_compatibility"] = self.write("platform.json", {
            "status": "approved", "account": "隔离平台审核人", "scope": target.CONTRACT,
            "binding_sha256": fs.digest(self.binding), "approved_at_utc": fs.timestamp(self.now - 1),
            "target_sha256": fs.digest(self.request["target"]),
            "expires_at_utc": fs.timestamp(self.now + 172800)})
        self.refresh_dynamic(self.now)

    def assess(self, now=None):
        return target.assess(self.request, self.currents, self.facts, gate_ids=sorted(target.GATE_IDS),
                             now=self.now if now is None else now)

    def assert_refused(self):
        verdict, decisions = self.assess()
        self.assertEqual(verdict["action"], "reexecute-all", verdict)
        self.assertFalse(any(decision.inherit for decision in decisions.values()), verdict)

    def test_subset_inherits_without_requiring_background_only_gates(self):
        del self.currents["b"]
        verdict, decisions = self.assess()
        self.assertEqual(verdict["action"], "inherit-matching-units", verdict)
        self.assertEqual(set(decisions), {"a"})
        self.assertTrue(decisions["a"].inherit)
        self.assertEqual(verdict["expires_at_utc"], self.receipt["expires_at_utc"])

    def test_spec_input_and_new_units_are_differences_not_reused(self):
        for field in ("spec_sha256", "inputs_sha256"):
            with self.subTest(field=field):
                before = getattr(self.currents["b"], field)
                setattr(self.currents["b"], field, "0" * 64)
                verdict, decisions = self.assess()
                self.assertTrue(decisions["a"].inherit, verdict)
                self.assertFalse(decisions["b"].inherit)
                setattr(self.currents["b"], field, before)
        self.currents["c"] = records.Current("c", "command", {}, "0" * 64, [], "1" * 64, False)
        _, decisions = self.assess()
        self.assertEqual({key for key, value in decisions.items() if value.inherit}, {"a", "b"})
        self.assertFalse(decisions["c"].inherit)
        self.assertEqual(set(decisions), set(self.currents))

    def test_current_coverage_or_shared_policy_environment_executor_drift_refuses_all(self):
        self.currents["b"].inheritable = False
        self.assert_refused()
        self.currents["b"].inheritable = True
        for name, value in (("policy_sha256", "0" * 64), ("environment_sha256", "0" * 64), ("executor", {"sha256": "0" * 64})):
            with self.subTest(field=name):
                old = getattr(self.facts, name)
                setattr(self.facts, name, value)
                self.assert_refused()
                setattr(self.facts, name, old)

    def test_every_identity_field_change_refuses_whole_set(self):
        for field in sorted(fs.BINDING_FIELDS):
            context = copy.deepcopy(self.context)
            context["binding"][field] = "漂移"
            self.request["context"] = self.write("drift-context.json", context)
            with self.subTest(field=field):
                self.assert_refused()

    def test_expiry_revocation_and_query_age_are_inherited_not_renewed(self):
        for delta, allowed in ((86399, True), (86400, False)):
            self.refresh_dynamic(self.now + delta)
            verdict, decisions = self.assess(self.now + delta)
            self.assertEqual(all(d.inherit for d in decisions.values()), allowed, verdict)
        self.refresh_dynamic(self.now, revoked=True)
        self.assert_refused()
        self.refresh_dynamic(self.now - 61)
        self.assert_refused()

    def test_platform_signature_missing_wrong_binding_or_expired_refuses(self):
        original = self.request["platform_compatibility"]
        for change in ({"status": "pending"}, {"account": ""}, {"scope": fs.CONTRACT},
                       {"binding_sha256": "0" * 64}, {"expires_at_utc": fs.timestamp(self.now)}):
            with self.subTest(change=change):
                self.request["platform_compatibility"] = self.write("bad-platform.json", {**fs.replay_ref(original), **change})
                self.assert_refused()
        del self.request["platform_compatibility"]
        self.assert_refused()

    def test_b09_approval_does_not_authorize_b10(self):
        value = fs.replay_ref(self.request["approvals"][0]); value["scope"] = fs.CONTRACT
        self.request["approvals"][0] = self.write("b09-only-approval.json", value)
        self.assert_refused()

    def test_missing_gate_or_extra_gate_refuses(self):
        for gates in (sorted(target.GATE_IDS)[:-1], sorted(target.GATE_IDS) + ["backend-unit"], sorted(target.GATE_IDS) + ["backend-lint"]):
            verdict, decisions = target.assess(self.request, self.currents, self.facts, gate_ids=gates, now=self.now)
            self.assertFalse(any(d.inherit for d in decisions.values()), verdict)

    def test_disabled_switch_keeps_all_commands_scheduled(self):
        self.request["reuse_enabled"] = False
        verdict, decisions = self.assess()
        self.assertTrue(verdict["eligible"], verdict)
        self.assertEqual(verdict["action"], "reexecute-all")
        self.assertFalse(any(d.inherit for d in decisions.values()))

    def test_source_log_tamper_cannot_be_treated_as_just_one_difference(self):
        source = self.manifest["units"][0]
        record = fs.read(source["record_path"])
        self.store.log_path(record["log"]["sha256"]).write_text("篡改")
        self.currents["b"].spec_sha256 = "0" * 64
        self.assert_refused()

    def target_evidence(self):
        verdict, decisions = self.assess()
        rows = [{**row, "disposition": "inherited"} for row in self.manifest["units"]]
        manifest = records.build_manifest(**{**{key: value for key, value in self.manifest.items() if key != "manifest_sha256"},
            "run_id": "target", "mode": records.FULL_SET_PASS, "units": rows, "target_platform_decision": verdict})
        self.store.put_manifest(manifest)
        plan = {"profile": "preflight", "units": [{"unit_id": "a"}, {"unit_id": "b"}],
                "gates": [{"gate_id": gate, "units": ["a", "b"]} for gate in sorted(target.GATE_IDS)]}
        summary = {**self.summary, "mode": records.FULL_SET_PASS, "run_id": "target",
                   "gates": [row for row in self.summary["gates"] if row["gate_id"] in target.GATE_IDS]}
        request = self.write("request.json", self.request)
        return {"schema_version": target.EVIDENCE_SCHEMA, "contract": target.CONTRACT, "tree": str(self.root),
                "plan": self.write("target-plan.json", plan), "summary": self.write("target-summary.json", summary),
                "manifest": self.write("target-manifest.json", manifest), "request": request,
                "counts": {"executed": 0, "inherited": 2}, "checked_at_utc": fs.timestamp(self.now),
                "expires_at_utc": self.receipt["expires_at_utc"]}

    def test_complete_target_proof_replays_and_checks_subject(self):
        proof = self.target_evidence()
        subject = self.request["target"]
        self.assertEqual(target.verify_evidence(proof, subject=subject), {"executed": 0, "inherited": 2})
        for key in subject:
            with self.subTest(key=key), self.assertRaises(ValueError):
                target.verify_evidence(proof, subject={**subject, key: "wrong"})

    def test_proof_rejects_counts_expiry_missing_or_repeated_units(self):
        proof = self.target_evidence()
        for change in ({"counts": {"executed": 1, "inherited": 1}}, {"expires_at_utc": fs.timestamp(self.now + 86401)}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                target.verify_evidence({**proof, **change})
        summary = fs.replay_ref(proof["summary"])
        summary["gates"][0]["units"] = ["a", "a"]
        proof["summary"] = self.write("broken-summary.json", summary)
        with self.assertRaises(ValueError):
            target.verify_evidence(proof)

    def test_cached_result_checks_current_revocation_and_keeps_history(self):
        proof = self.target_evidence()
        snapshot = target.snapshot_request(Path(proof["request"]["path"]), self.root / "saved-auth")
        proof["request"] = snapshot
        self.refresh_dynamic(self.now, revoked=True)
        path = Path(self.write("new-request.json", self.request)["path"])
        with mock.patch.object(target.fs, "live_check"), self.assertRaises(ValueError):
            target.verify_evidence(proof, current_request=path, deployment=self.root / "unused", now=self.now)
        self.assertEqual(target.verify_evidence(proof)["inherited"], 2)

    def test_host_change_keeps_historical_replay_but_never_authorizes_new_inheritance(self):
        proof = self.target_evidence()
        (self.root / "input.txt").write_text("历史阶段完成之后的宿主变化")
        self.assertEqual(target.verify_evidence(proof)["inherited"], 2)
        with self.assertRaises(ValueError):
            target.verify_evidence(proof, now=self.now)
        self.facts.historical_read_audit = True
        self.assert_refused()

    def test_changed_target_requires_new_specific_approvals(self):
        self.request["target"]["candidate_image_id"] = "sha256:" + "e" * 64
        self.assert_refused()

    def test_cached_pass_without_unit_proof_is_never_a_new_command(self):
        logs = self.root / "logs"; logs.mkdir()
        self.write("logs/target-platform.gate.json", {"exit_code": 0, "command": target.COMMAND})
        with self.assertRaises(ValueError):
            target.check_cached(self.root, None, self.root)

    def test_archive_keeps_local_gates_and_all_target_artifacts(self):
        for name in ("logs", "environment", "target-units", "local"):
            (self.root / name).mkdir()
        for name in ("logs/target-platform.gate.json", "environment/attempt-before.json", "target-units/evidence.json",
                     "candidate-gates.receipt.json", "candidate-gates.facts.json", "local/full-regression.gate.json"):
            self.write(name, {"fixture": name})
        archived = Path(target.archive(self.root, "attempt"))
        self.assertTrue((archived / "target-units/evidence.json").is_file())
        self.assertTrue((archived / "candidate-gates.receipt.json").is_file())
        self.assertTrue((self.root / "local/full-regression.gate.json").is_file())
        self.assertFalse((self.root / "target-units").exists())

    def executor_fixture(self):
        """元数据来自隔离来源，调度、命令进程、写库和清单自检使用真实实现。"""
        config = record_fixture._config(self.root)
        markers = {key: self.root / (key + ".executed") for key in ("a", "b")}
        plan = {"schema_version": executor.GATES_SCHEMA, "profile": "preflight", "test_groups": [],
                "units": [{"unit_id": key, "argv": [sys.executable, "-B", "-c",
                    "from pathlib import Path; Path(" + repr(str(path)) + ").write_text('执行一次')"],
                    "cwd": str(self.root), "cores": 1, "memory_mb": 128} for key, path in markers.items()],
                "gates": [{"gate_id": key, "units": ["a", "b"]} for key in sorted(target.GATE_IDS)]}
        plan_path = Path(self.write("executor-plan.json", plan)["path"])
        _, units, _, _ = executor.load_gates_manifest(plan_path, machine_cores=4)
        for unit in units:
            spec = executor.unit_spec(unit, start=None, pattern=None, timeout_seconds=120)
            self.currents[unit.unit_id].spec = spec
            self.currents[unit.unit_id].spec_sha256 = fs.digest(spec)
        rows = []
        for row in self.manifest["units"]:
            old = fs.read(row["record_path"])
            current = self.currents[row["unit_id"]]
            spec = {**current.spec}
            if row["unit_id"] == "b":
                spec["argv"] = [sys.executable, "-B", "-c", "print('来源旧作业')"]
            record = records.seal_record({**{key: value for key, value in old.items() if key != "record_sha256"},
                "run": {"run_id": "source-executor"}, "spec": spec, "spec_sha256": fs.digest(spec),
                "started_at_utc": fs.timestamp(self.now - 1)})
            path = self.store.put_record(record)
            rows.append({**row, "spec_sha256": record["spec_sha256"], "record_sha256": record["record_sha256"], "record_path": str(path)})
        self.manifest = records.build_manifest(**{**{key: value for key, value in self.manifest.items() if key != "manifest_sha256"},
            "run_id": "source-executor", "units": rows})
        self.store.put_manifest(self.manifest)
        self.summary["run_id"] = "source-executor"
        self.source["manifest"] = self.write("executor-source-manifest.json", self.manifest)
        self.source["summary"] = self.write("executor-source-summary.json", self.summary)
        entry = fs.replay_ref(self.source["entry"])
        entry["executor_summary"] = self.source["summary"]["path"]
        entry["unit_manifest"] = {"path": self.source["manifest"]["path"]}
        self.source["entry"] = self.write("executor-source-entry.json", entry)
        runtime = fs.replay_ref(self.context["evidence"]["runtime"])
        runtime["container_name"] = "isolated-fixture"
        self.context["evidence"]["runtime"] = self.write("executor-runtime.json", runtime)
        self.request["context"] = self.write("executor-context.json", self.context)
        self.receipt = fs.issue(self.source, self.context, self.clock)
        self.request["receipt"] = self.write("executor-source-receipt.json", self.receipt)
        self.refresh_dynamic(self.now)
        request_path = Path(self.write("executor-request.json", self.request)["path"])
        deployment = self.root / "codex-test-supervisor-enable-20261005t120000z.json"
        deployment.write_bytes(Path(self.context["evidence"]["deployment"]["path"]).read_bytes())
        out = self.root / "executor-result"
        args = ["run-gates", "--manifest", str(plan_path), "--config", str(config), "--weights", str(self.root / "none.json"),
                "--durations", str(self.root / "none.json"), "--cores", "4", "--state-dir", str(self.root / "state"),
                "--out-dir", str(out), "--shared-caches", "off", "--record-store", str(self.store.root),
                "--mode", "full-set-pass", "--target-platform-request", str(request_path), "--full-set-deployment", str(deployment)]
        return markers, request_path, out, args

    def run_executor(self, revoke_at=None):
        markers, request, out, args = self.executor_fixture()
        record_module = executor._records_module()
        original_run = executor.subprocess.run
        original_lock = executor.hold_scheduler_lock
        original_alone = executor.Scheduler.run_alone

        def command(argv, **kwargs):
            if argv[0] == "git" and argv[-2:] == ["rev-parse", "HEAD"]:
                return mock.Mock(stdout=self.binding["commit"])
            if argv[:2] == ["docker", "inspect"]:
                return mock.Mock(stdout=self.binding["runtime_image_digest"])
            return original_run(argv, **kwargs)

        def revoke():
            self.refresh_dynamic(self.now, revoked=True)
            request.write_text(json.dumps(self.request))

        def lock(*a, **kw):
            fd = original_lock(*a, **kw)
            if revoke_at == "lock":
                revoke()
            return fd

        def alone(scheduler, *a, **kw):
            if revoke_at == "end":
                revoke()
            return original_alone(scheduler, *a, **kw)

        with contextlib.ExitStack() as stack:
            for obj, name, value in ((executor, "_gate_currents", lambda *a, **kw: self.currents),
                                    (executor, "gates_policy_digest", lambda *a, **kw: self.facts.policy_sha256),
                                    (record_module, "executor_version", lambda *a: self.facts.executor),
                                    (record_module, "executor_environment", lambda *a: self.environment),
                                    (record_module, "utc_now", lambda: fs.timestamp(self.now)),
                                    (executor.time, "time", lambda: self.now),
                                    (executor, "_utc_now", lambda: fs.timestamp(self.now)),
                                    (executor.subprocess, "run", command),
                                    (executor, "hold_scheduler_lock", lock),
                                    (executor.Scheduler, "run_alone", alone)):
                stack.enter_context(mock.patch.object(obj, name, value))
            for name, field in (("machine", "architecture"), ("platform", "os_version"), ("release", "kernel_version")):
                stack.enter_context(mock.patch.object(fs.platform, name, return_value=self.binding[field]))
            rc = executor.main(args)
        summary, manifest = fs.read(out / "summary.json"), fs.read(out / "unit-manifest.json")
        return rc, markers, summary, manifest

    def test_executor_only_runs_difference_and_publishes_complete_manifest(self):
        rc, markers, summary, manifest = self.run_executor()
        self.assertEqual(rc, 0, summary)
        self.assertFalse(markers["a"].exists())
        self.assertTrue(markers["b"].exists())
        self.assertEqual({row["unit_id"]: row["disposition"] for row in manifest["units"]}, {"a": "inherited", "b": "executed"})
        self.assertEqual(summary["unit_manifest"]["self_check"], "passed")

    def test_waiting_for_lock_cannot_bypass_revocation_and_runs_every_unit(self):
        rc, markers, summary, manifest = self.run_executor(revoke_at="lock")
        self.assertEqual(rc, 0, summary)
        self.assertTrue(all(path.exists() for path in markers.values()))
        self.assertEqual({row["disposition"] for row in manifest["units"]}, {"executed"})

    def test_revocation_during_difference_execution_withholds_passed_manifest(self):
        rc, markers, summary, manifest = self.run_executor(revoke_at="end")
        self.assertEqual(rc, 1, summary)
        self.assertFalse(markers["a"].exists())
        self.assertTrue(markers["b"].exists())
        self.assertIsNone(records.RecordStore(self.store.root).manifest(manifest["run_id"]))


if __name__ == "__main__":
    unittest.main()
