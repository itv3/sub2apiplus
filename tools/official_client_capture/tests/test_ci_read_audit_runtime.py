"""B-11 本轮输出、隔离挂载和动态环境合同的拒绝边界。"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.ci import unit_records as records
from tools.ci import unit_executor as executor


class RuntimeReadAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = str(Path(self.directory.name).resolve() / "private" / "output")
        self.contract = {"schema_version": runtime.SCHEMA, "temporary_root": self.root,
                         "mask_roots": [runtime.MASK_ROOT], "environment_paths": []}
        self.entry = runtime.contract_entry(self.contract)
        trace = ('1 mount("none", "/", NULL, MS_REC|MS_PRIVATE, NULL) = 0\n'
                 f'2 mount("tmpfs", "{runtime.MASK_ROOT}", "tmpfs", MS_RDONLY, "size=64k,mode=0755") = 0\n'
                 f'3 mount("tmpfs", "{self.root}", "tmpfs", MS_NOSUID|MS_NODEV, "size=512m,mode=0700") = 0\n')
        self.context = {"schema_version": runtime.CONTEXT_SCHEMA, "contract_entry": self.entry,
                        "parent_namespace": "mnt:[100]", "namespace": "mnt:[101]", "empty_temporary_root": True,
                        "private_mounts": True, "empty_masks": {runtime.MASK_ROOT: True}, "mounts": [
                            {"mount_id": 200, "target": runtime.MASK_ROOT, "filesystem": "tmpfs", "options": ["ro"]},
                            {"mount_id": 201, "target": self.root, "filesystem": "tmpfs", "options": ["rw", "nosuid", "nodev"]}],
                        "setup_commands": ["private", runtime.MASK_ROOT, self.root], "setup_success": True,
                        "setup_trace": trace, "setup_trace_sha256": hashlib.sha256(trace.encode()).hexdigest()}

    def document(self, lines):
        document = ra.filter_stream(lines, ["/"], strict=True, runtime_context=self.context)
        document["runtime"]["completion"] = {"command_finished": True, "host_inputs_unchanged": True,
                                            "environment_unchanged": True, "namespace_unchanged": True,
                                            "host_entries_sha256": runtime.digest([])}
        return document

    def audit(self, document, extra=()):
        return ra.strict_audit_reads(document, [self.entry, *extra], repo_root="/repo", data_root=None)

    def generated_lines(self):
        return [f'1 mkdir("{self.root}/d", 0700) = 0',
                f'1 openat(AT_FDCWD</repo>, "{self.root}/d/a", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{self.root}/d/a>',
                f'1 openat(AT_FDCWD</repo>, "{self.root}/d/a", O_RDONLY) = 3<{self.root}/d/a>']

    def test_fresh_generated_output_can_be_replayed_without_host_prefix_exemption(self):
        document = self.document(self.generated_lines())
        self.assertTrue(self.audit(document)["coverage_complete"])
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA)
        self.assertNotIn("/tmp", document["runtime"]["outputs"]["paths"])
        tampered = copy.deepcopy(document)
        tampered["runtime"]["output_events"] = []
        self.assertFalse(self.audit(tampered)["coverage_complete"])

    def test_historical_runtime_does_not_read_current_devices_but_verifies_saved_digest(self):
        document = self.document(self.generated_lines())
        with mock.patch.object(ra._runtime_module(), "contract_entry", side_effect=RuntimeError("现场环境已变化")):
            self.assertTrue(ra.strict_audit_reads(document, [self.entry], repo_root="/repo", data_root=None,
                                                historical=True)["coverage_complete"])
            self.assertFalse(self.audit(document)["coverage_complete"])
        changed = copy.deepcopy(self.entry)
        changed["detail"]["platform"]["kernel"] = "篡改"
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.validate_context({**self.context, "contract_entry": changed}, [changed], historical=True)

    def test_existing_file_and_read_before_write_never_become_generated_output(self):
        read = f'1 openat(AT_FDCWD</repo>, "{self.root}/old", O_RDONLY) = 3<{self.root}/old>'
        create = f'1 openat(AT_FDCWD</repo>, "{self.root}/old", O_CREAT|O_WRONLY, 0600) = 3<{self.root}/old>'
        for lines in ([read], [read, create]):
            with self.subTest(lines=lines):
                self.assertFalse(self.audit(self.document(lines))["coverage_complete"])

    def test_flag_words_in_filename_cannot_forge_creation(self):
        path = self.root + "/O_CREAT"
        document = self.document([f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY) = 3<{path}>'])
        self.assertEqual(dict(document["accesses"])[path], ["read"])
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_link_escape_and_real_descriptor_outside_private_root_are_refused(self):
        lines = [f'1 symlink("/host/data", "{self.root}/link") = 0',
                 f'1 openat(AT_FDCWD</repo>, "{self.root}/link", O_RDONLY) = 3</host/data>']
        self.assertFalse(self.audit(self.document(lines))["coverage_complete"])
        lines = [f'1 openat(AT_FDCWD</repo>, "{self.root}/a", O_CREAT|O_WRONLY, 0600) = 3</host/data>']
        self.assertFalse(self.audit(self.document(lines))["coverage_complete"])

    def test_namespace_empty_root_mount_and_trace_mismatch_are_refused(self):
        cases = [{"namespace": "mnt:[100]"}, {"empty_temporary_root": False}, {"private_mounts": False},
                 {"mounts": []}, {"empty_masks": {runtime.MASK_ROOT: False}}, {"setup_success": False}, {"setup_commands": []},
                 {"setup_trace_sha256": "0" * 64}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(runtime.RuntimeContractError):
                runtime.validate_context({**self.context, **changes}, [self.entry])
        altered = copy.deepcopy(self.context)
        altered["setup_trace"] = altered["setup_trace"].replace("MS_RDONLY", "0")
        altered["setup_trace_sha256"] = hashlib.sha256(altered["setup_trace"].encode()).hexdigest()
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.validate_context(altered, [self.entry])

    def test_kernel_paths_require_explicit_contract_and_payload_mounts_remain_forbidden(self):
        lines = ['1 openat(AT_FDCWD</repo>, "/proc/self/mountinfo", O_RDONLY) = 3</proc/999/mountinfo>']
        self.assertFalse(self.audit(self.document(lines))["coverage_complete"])
        lines = self.generated_lines() + ['1 mount("tmpfs", "/x", "tmpfs", 0, NULL) = 0']
        self.assertFalse(self.audit(self.document(lines))["coverage_complete"])
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.validate_contract({**self.contract, "environment_paths": ["/proc/self/mountinfo"]})

    def test_missing_completion_and_input_drift_are_refused(self):
        for key in ("command_finished", "host_inputs_unchanged", "environment_unchanged", "namespace_unchanged"):
            document = self.document(self.generated_lines())
            document["runtime"]["completion"][key] = False
            with self.subTest(key=key):
                self.assertFalse(self.audit(document)["coverage_complete"])
        document = self.document(self.generated_lines())
        document["runtime"]["completion"]["host_entries_sha256"] = "0" * 64
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_empty_readonly_mask_only_covers_root_metadata_and_missing_children(self):
        lines = [f'1 newfstatat(AT_FDCWD</repo>, "{runtime.MASK_ROOT}", 0x1, 0) = 0',
                 f'1 openat(AT_FDCWD</repo>, "{runtime.MASK_ROOT}/old/token", O_RDONLY) = -1 ENOENT (No such file or directory)']
        self.assertTrue(self.audit(self.document(lines))["coverage_complete"])
        lines.append(f'1 openat(AT_FDCWD</repo>, "{runtime.MASK_ROOT}/old/token", O_RDONLY) = 3<{runtime.MASK_ROOT}/old/token>')
        self.assertFalse(self.audit(self.document(lines))["coverage_complete"])

    def test_only_proven_failed_terminal_probe_is_covered(self):
        document = self.document(['1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR|O_NONBLOCK) = -1 ENXIO (No such device or address)'])
        self.assertTrue(self.audit(document)["coverage_complete"])
        document["runtime"]["terminal_events"] = []
        self.assertFalse(self.audit(document)["coverage_complete"])
        for line in ['1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR) = 3</dev/tty>',
                     '1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR) = -1 EACCES (Permission denied)',
                     '1 chmod("/dev/tty", 0600) = 0']:
            self.assertFalse(self.audit(self.document([line]))["coverage_complete"])

    def test_runtime_contract_cannot_disable_required_audit(self):
        inputs = records.declared_inputs(None, {"runtime_contract": self.contract, "require_read_audit": False})
        self.assertIn("runtime-contract", {entry["name"] for entry in inputs})
        self.assertIn("require-read-audit", {entry["name"] for entry in inputs})

    def test_unobserved_children_descriptor_cwd_and_namespace_calls(self):
        trace = ra.filter_stream(['1 clone(child_stack=NULL, flags=CLONE_UNTRACED|SIGCHLD) = 2'], ["/"], strict=True)
        self.assertGreater(trace["coverage"]["unresolved"], 0)
        trace = ra.filter_stream(['1 fchdir(3</repo/sub>) = 0', '1 execve("./tool", ["tool"], 0x1) = 0'], ["/"], strict=True)
        self.assertEqual(trace["coverage"]["unresolved"], 0)
        self.assertIn("/repo/sub/tool", dict(trace["accesses"]))
        argv = ra.strace_argv(["true"], output=Path("/out/trace.json"), roots=["/"], strict=True)
        self.assertIn("setns", " ".join(argv))
        self.assertIn("io_uring_setup", " ".join(argv))


class RuntimeTestTransportTests(unittest.TestCase):
    """用真实普通文件核运输边界；此短验不宣称已完成 Linux 挂载及 strace 来源生产。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve() / "private" / "output"
        self.root.mkdir(parents=True, mode=0o700)
        self.contract = {"schema_version": runtime.SCHEMA, "temporary_root": str(self.root),
                         "mask_roots": [], "environment_paths": []}
        self.result = Path(self.directory.name).resolve() / "out" / "units" / "test_native.result.json"
        self.result.parent.mkdir(parents=True)
        unit = executor.Unit("test_native", "test_native", ("test_native.NativeTests.test_one",), executor.Quota(1, 128), False, 1)
        self.spec = executor.unit_spec(unit, start=Path("tests"), pattern="test_*.py", timeout_seconds=30)
        self.transport = {"schema_version": runtime.TEST_TRANSPORT_SCHEMA, "spec": self.spec,
                          "spec_sha256": runtime.digest(self.spec),
                          "payload_sha256": runtime.digest({"unit_id": self.spec["unit_id"], "test_ids": self.spec["test_ids"]}),
                          "temporary_root": str(self.root), "result_path": str(self.result)}
        self.payload = {"schema_version": runtime.TEST_RESULT_SCHEMA, "unit_id": self.spec["unit_id"],
                        "tests": {self.spec["test_ids"][0]: {"outcome": "passed", "seconds": 0.01}},
                        "tests_run": 1, "successful": True}

    def test_original_command_is_rewritten_without_reading_host_test_list(self):
        original = [sys.executable, str(runtime.HERE.with_name("unit_executor.py")), "run-unit", "--start", "tests",
                    "--tests-file", str(self.result).replace(".result.json", ".tests.json"), "--result", str(self.result)]
        argv = runtime.test_transport_argv(self.transport, self.contract, original,
                                          output=self.result.parent.parent / "audit" / "trace.json")
        self.assertIn("--runtime-tests-json", argv)
        self.assertNotIn("--tests-file", argv)
        self.assertEqual(argv[-1], str(self.root / runtime.TEST_RESULT_NAME))
        self.assertEqual(runtime.runtime_test_payload(self.transport, start=Path("tests"), result=Path(argv[-1])),
                         {"unit_id": self.spec["unit_id"], "test_ids": self.spec["test_ids"]})
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.test_transport_argv(self.transport, self.contract, ["true"], output=self.result.parent.parent / "audit" / "trace.json")

    def test_payload_spec_and_destination_are_bound_but_destination_is_not_stable_input(self):
        entry = runtime.test_spec_entry(self.spec)
        runtime._test_transport_check(self.transport, self.contract, [entry])
        changed = copy.deepcopy(self.transport)
        changed["result_path"] = str(self.result.parent.parent.parent / "next" / "units" / self.result.name)
        self.assertEqual(runtime.test_spec_entry(runtime._test_transport_check(changed, self.contract, [entry])["spec"]), entry)
        for field, value in (("spec_sha256", "0" * 64), ("payload_sha256", "0" * 64),
                             ("result_path", str(self.result.parent / "wrong.result.json")),
                             ("result_path", str(self.root / "units" / self.result.name))):
            with self.subTest(field=field, value=value), self.assertRaises(runtime.RuntimeContractError):
                runtime._test_transport_check({**self.transport, field: value}, self.contract, [entry])
        with self.assertRaises(runtime.RuntimeContractError):
            runtime._test_transport_check(self.transport, self.contract, [])

    def test_real_result_file_exports_identical_bytes_and_rejects_links_missing_and_corrupt_data(self):
        source = self.root / runtime.TEST_RESULT_NAME
        runtime.write_test_output(source, self.payload)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [runtime.TEST_RESULT_NAME])
        artifact = runtime._read_test_artifact(source, self.spec, partial=False, returncode=0)
        exported = runtime._export_test_artifact(artifact, self.result)
        self.assertEqual(source.read_bytes(), self.result.read_bytes())
        self.assertEqual(exported["sha256"], hashlib.sha256(self.result.read_bytes()).hexdigest())
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            runtime._read_test_artifact(source, self.spec, partial=False, returncode=0)
        source.symlink_to(self.result)
        with self.assertRaises(OSError):
            runtime._read_test_artifact(source, self.spec, partial=False, returncode=0)
        source.unlink()
        source.write_text("{broken}")
        with self.assertRaises(ValueError):
            runtime._read_test_artifact(source, self.spec, partial=False, returncode=0)
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.write_test_output(self.root / "another.json", self.payload)

    def test_failure_sidecar_is_partial_and_success_never_covers_loader_or_missing_tests(self):
        failed = {**self.payload, "successful": False,
                  "tests": {self.spec["test_ids"][0]: {"outcome": "error", "seconds": 0.01}}}
        partial = {**failed, "partial": True}
        source = self.root / runtime.TEST_FAILURE_NAME
        runtime.write_test_output(source, partial)
        saved = source.read_bytes()
        with self.assertRaises(FileExistsError):
            runtime.write_test_output(source, {**partial, "tests_run": 0})
        self.assertEqual(source.read_bytes(), saved)
        artifact = runtime._read_test_artifact(source, self.spec, partial=True, returncode=-9)
        runtime._export_test_artifact(artifact, self.result.with_suffix(".observed-failure.json"))
        self.assertFalse(json.loads(self.result.with_suffix(".observed-failure.json").read_text())["successful"])
        runtime._check_test_result(failed, self.spec, partial=False, returncode=1)
        for changed in ({**self.payload, "tests": {}}, {**self.payload, "tests_run": 0},
                        {**failed, "successful": True}, {**partial, "successful": True},
                        {**self.payload, "unit_id": "wrong"}):
            with self.subTest(changed=changed), self.assertRaises(runtime.RuntimeContractError):
                runtime._check_test_result(changed, self.spec, partial="partial" in changed, returncode=0)

    def test_export_failures_and_consumer_wrong_binding_fail_closed(self):
        source = self.root / runtime.TEST_RESULT_NAME
        runtime.write_test_output(source, self.payload)
        artifact = runtime._read_test_artifact(source, self.spec, partial=False, returncode=0)
        self.result.with_name(self.result.name + ".runtime-test-export.tmp").write_text("已有文件不得覆盖")
        with self.assertRaises(FileExistsError):
            runtime._export_test_artifact(artifact, self.result)
        self.assertFalse(self.result.exists())
        self.result.with_name(self.result.name + ".runtime-test-export.tmp").unlink()
        exported = runtime._export_test_artifact(artifact, self.result)
        receipt = {"schema_version": runtime.TEST_EXPORT_SCHEMA, "status": "exported", "transport": self.transport,
                   "returncode": 0, "result": exported, "observed_failure": None}
        document = {"runtime": {"completion": {"returncode": 0, "test_export": receipt}},
                    "accesses": [[str(source), ["write"]]]}
        binding = runtime.test_spec_entry(self.spec)
        runtime._replay_test_export(document, [binding], self.contract, {str(source)})
        for mutation in ("missing", "wrong-digest", "wrong-spec", "wrong-kind"):
            changed = copy.deepcopy(document)
            if mutation == "missing":
                changed["runtime"]["completion"].pop("test_export")
            elif mutation == "wrong-digest":
                changed["runtime"]["completion"]["test_export"]["result"]["sha256"] = "0" * 64
            elif mutation == "wrong-spec":
                changed["runtime"]["completion"]["test_export"]["transport"]["spec"]["test_ids"] = ["test_native.NativeTests.other"]
            else:
                changed["accesses"] = [[str(source), ["read"]]]
            with self.subTest(mutation=mutation), self.assertRaises(runtime.RuntimeContractError):
                runtime._replay_test_export(changed, [binding], self.contract, {str(source)})


if __name__ == "__main__":
    unittest.main()
