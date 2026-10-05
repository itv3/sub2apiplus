"""B-09 的确定性合同验收：完整来源、时钟、撤销、身份漂移及全收／全拒。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.ci import full_set_receipt as fs
from tools.ci import read_audit as ra
from tools.ci import unit_records as ur


class FullSetReceiptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.now = fs.utc("2026-10-05T12:00:00Z")
        self.store = ur.RecordStore(self.root / "store")
        self.environment = [ur.value_entry("environment", "executor:arch", "aarch64")]
        executor = {"files": {"unit_executor.py": "e" * 64}}
        executor["sha256"] = fs.digest(executor["files"])
        self.facts = ur.RunFacts("p" * 64, self.environment, ur.entries_sha256(self.environment), executor, 24, self.now)
        self.binding = {key: "a" * 64 for key in fs.DIGEST_FIELDS}
        self.binding.update({key: key + "-value" for key in fs.TEXT_FIELDS})
        self.binding.update(commit="b" * 40, runtime_image_digest="sha256:" + "c" * 64,
                            gate_contract_version=fs.CONTRACT, environment_fingerprint=self.facts.environment_sha256,
                            tool_package_digest=executor["sha256"])
        evidence = {}
        for key, field in (("deployment", "deployment_receipt_digest"), ("dependencies", "external_dependencies_digest"),
                           ("data", "data_snapshot_digest")):
            evidence[key] = self.write(key + ".json", {"status": "passed", "fixture": key})
            self.binding[field] = evidence[key]["sha256"]
        evidence["profile"] = self.write("profile.json", {"target_profile_digest": self.binding["target_profile_digest"]})
        evidence["runtime"] = self.write("runtime.json", {"runtime_image_digest": self.binding["runtime_image_digest"]})
        self.context = {"schema_version": "full-set-context/v1", "binding": self.binding, "evidence": evidence}
        self.context_ref = self.write("context.json", self.context)
        self.clock = self.write("clock.json", {"status": "synchronized", "source": "隔离校时夹具",
                                               "offset_seconds": 0, "sampled_at_utc": fs.timestamp(self.now)})
        self.currents = {}
        entries, rows = [], []
        input_path = self.root / "input.txt"
        input_path.write_text("隔离输入")
        inputs = [ra.host_snapshot(str(input_path))]
        trace = ra.filter_stream([f'1 openat(AT_FDCWD<{self.root}>, "{input_path}", O_RDONLY) = 3<{input_path}>\n'], ["/"], strict=True)
        trace_ref = self.write("trace.json", trace)
        self.store.put_log(Path(trace_ref["path"]), trace_ref["sha256"])
        log = self.root / "run.log"; log.write_text("隔离单元通过\n")
        log_sha = fs.file_digest(log); self.store.put_log(log, log_sha)
        for unit_id in ("a", "b"):
            spec = {"argv": ["fixture", unit_id]}
            current = ur.Current(unit_id, "command", spec, fs.digest(spec), inputs, ur.entries_sha256(inputs), True)
            self.currents[unit_id] = current
            record = ur.seal_record({"unit_id": unit_id, "unit_type": "command", "kind": "formal", "run": {"run_id": "source"},
                "spec": spec, "spec_sha256": current.spec_sha256, "inputs": inputs, "inputs_sha256": current.inputs_sha256,
                "executor": executor, "policy_sha256": self.facts.policy_sha256, "environment": self.environment,
                "environment_sha256": self.facts.environment_sha256, "inheritable": True, "passed": True,
                "exit_code": 0, "signal": None, "timed_out": False, "log": {"sha256": log_sha},
                "completed_at_utc": fs.timestamp(self.now), "read_audit": {"status": "passed", "coverage_complete": True,
                    "coverage_scope": "all-file-paths/v1", "undeclared_count": 0, "inputs_sha256": current.inputs_sha256,
                    "trace": {"sha256": trace_ref["sha256"]}, "repo_root": str(self.root)}})
            path = self.store.put_record(record)
            entries.append({"unit_id": unit_id, "disposition": "executed", "record_path": str(path),
                            "record_sha256": record["record_sha256"], "spec_sha256": current.spec_sha256,
                            "inputs_sha256": current.inputs_sha256})
            rows.append({"unit_id": unit_id, "passed": True, "exit_code": 0, "signal": None, "timed_out": False,
                         "completed_at_utc": fs.timestamp(self.now)})
        self.manifest = ur.build_manifest(run_id="source", mode="re-execute", record_store=str(self.store.root), planned_units=["a", "b"],
            units=entries, environment=self.environment, environment_sha256=self.facts.environment_sha256,
            executor=executor, policy_sha256=self.facts.policy_sha256, inheritance_max_age_hours=24,
            decided_at_utc=fs.timestamp(self.now), test_groups={})
        self.store.put_manifest(self.manifest)
        self.summary = {"status": "passed", "mode": "re-execute", "run_id": "source", "units": rows,
                        "read_audit": {"coverage_complete": True}, "gates": [
                            {"gate_id": g, "status": "passed", "units": ["a", "b"], "test_groups": [], "not_executed": []}
                            for g in sorted(fs.FULL_GATE_IDS)]}
        self.source = {"manifest": self.write("manifest.json", self.manifest), "summary": self.write("summary.json", self.summary),
                       "store": str(self.store.root)}
        self.source["entry"] = self.write("entry.json", {"status": "passed", "profile": "full-gates",
            "source": {"commit": self.binding["commit"], "deploy_receipt": evidence["deployment"]["path"]},
            "executor_summary": self.source["summary"]["path"], "unit_manifest": {"path": self.source["manifest"]["path"]}})
        self.receipt = fs.issue(self.source, self.context, self.clock)
        self.receipt_ref = self.write("receipt.json", self.receipt)
        self.request = {"schema_version": "full-set-request/v1", "reuse_enabled": False, "receipt": self.receipt_ref,
                        "context": self.context_ref, "consumer_clock": self.clock, "approvals": []}
        for role in sorted(fs.APPROVAL_ROLES):
            self.request["approvals"].append(self.write(role + ".json", {"role": role, "status": "approved",
                "account": "隔离夹具审核人", "scope": fs.CONTRACT, "binding_sha256": fs.digest(self.binding),
                "approved_at_utc": fs.timestamp(self.now - 86400), "expires_at_utc": fs.timestamp(self.now + 172800)}))
        self.refresh_dynamic(self.now)

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False))
        return fs.reference(path)

    def refresh_dynamic(self, now, *, offset=0, revoked=False):
        self.request["consumer_clock"] = self.write("consumer-clock.json", {"status": "synchronized", "source": "隔离校时夹具",
            "offset_seconds": offset, "sampled_at_utc": fs.timestamp(now)})
        self.request["revocation"] = self.write("revocation.json", {"schema_version": "full-set-revocation-query/v1", "status": "ok",
            "receipt_sha256": self.receipt["receipt_sha256"], "revoked": revoked, "campaign_status": "active",
            "queried_at_utc": fs.timestamp(now)})

    def assess(self, now=None):
        return fs.assess(self.request, self.currents, self.facts, gate_ids=sorted(fs.FULL_GATE_IDS), now=self.now if now is None else now)

    def test_closed_switch_records_eligibility_without_reusing(self):
        verdict, decisions = self.assess()
        self.assertTrue(verdict["eligible"], verdict)
        self.assertEqual(verdict["action"], "reexecute-all")
        self.assertFalse(any(d.inherit for d in decisions.values()))

    def test_explicit_approved_request_inherits_exactly_one_source_full_set(self):
        self.request["reuse_enabled"] = True
        verdict, decisions = self.assess()
        self.assertEqual(verdict["action"], "inherit-full-set", verdict)
        self.assertEqual(set(decisions), {"a", "b"})
        self.assertTrue(all(d.record["run"]["run_id"] == "source" for d in decisions.values()))

    def test_validity_86400_allowed_86401_refused_and_resigning_does_not_renew(self):
        again = fs.issue(self.source, self.context, self.clock, validity_seconds=86400)
        self.assertEqual(again, self.receipt)
        for length in (86401, 0, -1, True):
            with self.subTest(length=length), self.assertRaises(fs.ContractError):
                fs.issue(self.source, self.context, self.clock, validity_seconds=length)
        with self.assertRaisesRegex(fs.ContractError, "新一次"):
            fs.issue(self.source, self.context, self.clock, predecessor=self.receipt_ref)

    def test_issued_and_expiry_boundaries(self):
        self.request["reuse_enabled"] = True
        for delta, allowed in ((-61, False), (-60, True), (86399, True), (86400, False)):
            with self.subTest(delta=delta):
                self.refresh_dynamic(self.now + delta)
                verdict, decisions = self.assess(self.now + delta)
                self.assertEqual(all(d.inherit for d in decisions.values()), allowed, verdict)

    def test_clock_60_61_and_missing_confirmation(self):
        self.request["reuse_enabled"] = True
        for offset, allowed in ((60, True), (-60, True), (61, False), (-61, False), (True, False), ("0", False)):
            with self.subTest(offset=offset):
                self.refresh_dynamic(self.now, offset=offset)
                verdict, decisions = self.assess()
                self.assertEqual(all(d.inherit for d in decisions.values()), allowed, verdict)
        del self.request["consumer_clock"]
        self.assertFalse(self.assess()[0]["eligible"])

    def test_revocation_unavailable_stale_or_campaign_closed_fails_closed(self):
        self.request["reuse_enabled"] = True
        for change in ({"status": "unavailable"}, {"revoked": True}, {"campaign_status": "closed"},
                       {"queried_at_utc": fs.timestamp(self.now - 61)}, {"receipt_sha256": "0" * 64}):
            with self.subTest(change=change):
                self.refresh_dynamic(self.now)
                value = fs.replay_ref(self.request["revocation"]); value.update(change)
                self.request["revocation"] = self.write("revocation.json", value)
                verdict, decisions = self.assess()
                self.assertFalse(any(d.inherit for d in decisions.values()), verdict)

    def test_every_binding_field_drift_or_missing_refuses_whole_set(self):
        self.request["reuse_enabled"] = True
        for field in sorted(fs.BINDING_FIELDS):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    context = copy.deepcopy(self.context)
                    if missing:
                        del context["binding"][field]
                    else:
                        context["binding"][field] = "different"
                    self.request["context"] = self.write("drift.json", context)
                    self.assertFalse(any(d.inherit for d in self.assess()[1].values()))

    def test_input_change_or_missing_unit_rejects_all_not_partial(self):
        self.request["reuse_enabled"] = True
        self.currents["a"].inputs_sha256 = "0" * 64
        self.assertFalse(any(d.inherit for d in self.assess()[1].values()))
        del self.currents["b"]
        self.assertFalse(any(d.inherit for d in self.assess()[1].values()))

    def test_incomplete_coverage_failed_gate_and_inherited_source_cannot_issue(self):
        for change in ({"read_audit": {"coverage_complete": False}}, {"mode": "full-set-pass"}, {"status": "failed"}, {"gates": []}):
            with self.subTest(change=change):
                source = copy.deepcopy(self.source)
                source["summary"] = self.write("summary.json", {**self.summary, **change})
                with self.assertRaises(fs.ContractError):
                    fs.issue(source, self.context, self.clock)

    def test_approval_missing_expired_wrong_binding_or_withdrawn_refuses(self):
        self.request["reuse_enabled"] = True
        original = list(self.request["approvals"])
        for change in ({"status": "withdrawn"}, {"expires_at_utc": fs.timestamp(self.now)}, {"binding_sha256": "0" * 64}):
            with self.subTest(change=change):
                value = fs.replay_ref(original[0]); value.update(change)
                self.request["approvals"] = [self.write("bad-approval.json", value), original[1]]
                self.assertFalse(any(d.inherit for d in self.assess()[1].values()))
        self.request["approvals"] = original[:1]
        self.assertFalse(self.assess()[0]["eligible"])

    def test_tamper_without_new_digest_is_rejected(self):
        old = self.receipt_ref["sha256"]
        self.write("receipt.json", {**self.receipt, "expires_at_utc": fs.timestamp(self.now + 999999)})
        self.assertEqual(self.request["receipt"]["sha256"], old)
        self.assertFalse(self.assess()[0]["eligible"])

    def test_semantic_digest_excludes_dynamic_fields_but_preserves_results(self):
        changed = copy.deepcopy(self.summary)
        changed["run_id"] = "new-run"
        changed["units"][0]["completed_at_utc"] = fs.timestamp(self.now + 5)
        self.assertEqual(fs.normalized_result(changed), fs.normalized_result(self.summary))
        changed["units"][0]["passed"] = False
        self.assertNotEqual(fs.normalized_result(changed), fs.normalized_result(self.summary))

    def test_live_check_reads_current_commit_deployment_platform_and_container(self):
        deployment = self.root / "codex-test-supervisor-enable-20261005t120000z.json"
        deployment.write_bytes(Path(self.context["evidence"]["deployment"]["path"]).read_bytes())
        runtime = fs.replay_ref(self.context["evidence"]["runtime"])
        runtime["container_name"] = "isolated-fixture"
        self.context["evidence"]["runtime"] = self.write("runtime.json", runtime)
        self.request["context"] = self.write("context.json", self.context)
        host = {k: self.binding[k] for k in fs.platform_fields()}
        def command(argv, **kwargs):
            return mock.Mock(stdout=self.binding["commit"] if argv[0] == "git" else self.binding["runtime_image_digest"])
        with mock.patch.object(fs, "platform_fields", return_value=host), mock.patch.object(fs.subprocess, "run", side_effect=command) as run:
            fs.live_check(self.request, deployment=deployment, store=self.store.root, tree=self.root)
            self.assertEqual([call.args[0][0] for call in run.call_args_list], ["git", "docker"])
            with mock.patch.object(fs.subprocess, "run", return_value=mock.Mock(stdout="wrong")), self.assertRaises(fs.ContractError):
                fs.live_check(self.request, deployment=deployment, store=self.store.root, tree=self.root)
            newer = self.root / "codex-test-supervisor-enable-20261005t120001z.json"
            newer.write_text('{}')
            with self.assertRaisesRegex(fs.ContractError, "最新部署"):
                fs.live_check(self.request, deployment=deployment, store=self.store.root, tree=self.root)

    def test_dynamic_queries_expire_at_declared_windows(self):
        self.request["reuse_enabled"] = True
        for age, allowed in ((300, True), (301, False)):
            self.refresh_dynamic(self.now)
            self.request["consumer_clock"] = self.write("age-clock.json", {"status": "synchronized", "source": "隔离时钟",
                "offset_seconds": 0, "sampled_at_utc": fs.timestamp(self.now - age)})
            self.assertEqual(self.assess()[0]["eligible"], allowed)
        self.refresh_dynamic(self.now)
        value = fs.replay_ref(self.request["revocation"])
        for age, allowed in ((60, True), (61, False)):
            value["queried_at_utc"] = fs.timestamp(self.now - age)
            self.request["revocation"] = self.write("revocation.json", value)
            self.assertEqual(self.assess()[0]["eligible"], allowed)


if __name__ == "__main__":
    unittest.main()
