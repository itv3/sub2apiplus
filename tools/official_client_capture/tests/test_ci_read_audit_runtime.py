"""B-11 本轮输出、隔离挂载和动态环境合同的拒绝边界。"""

from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.ci import unit_records as records


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


if __name__ == "__main__":
    unittest.main()
