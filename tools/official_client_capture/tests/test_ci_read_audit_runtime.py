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
from tools.ci import control_read_audit as control_audit
from tools.ci import control_read_audit_runtime as control_runtime
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

    def control_document(self, lines):
        """四control模型凭证仅用于核对重放，不冒充真实Linux来源生产。"""
        document = control_audit.filter_stream(lines, ["/"], strict=True, runtime_context=self.control_context())
        document["runtime"]["completion"] = {"command_finished": True, "host_inputs_unchanged": True,
            "environment_unchanged": True, "namespace_unchanged": True, "host_entries_sha256": runtime.digest([]),
            "control_isolation_unchanged": True}
        return document

    def audit(self, document, extra=()):
        control = (document.get("runtime") or {}).get("context", {}).get("control_chain") is not None
        module = control_audit if control else ra
        entry = self.control_context()["contract_entry"] if control else self.entry
        return module.strict_audit_reads(document, [entry, *extra], repo_root="/repo", data_root=None)

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
        for operation in (
            f'1 rename("{self.root}/d/a", "{self.root}/d/b") = 0',
            f'1 renameat(AT_FDCWD</repo>, "{self.root}/d/a", AT_FDCWD</repo>, "{self.root}/d/b") = 0',
            f'1 renameat2(AT_FDCWD</repo>, "{self.root}/d/a", AT_FDCWD</repo>, "{self.root}/d/b", 0) = 0',
            f'1 link("{self.root}/d/a", "{self.root}/d/b") = 0',
            f'1 linkat(AT_FDCWD</repo>, "{self.root}/d/a", AT_FDCWD</repo>, "{self.root}/d/b", 0) = 0',
        ):
            with self.subTest(operation=operation):
                moved = self.control_document(self.generated_lines()[:2] + [operation,
                    f'1 openat(AT_FDCWD</repo>, "{self.root}/d/b", O_RDONLY) = 3<{self.root}/d/b>'])
                self.assertTrue(self.audit(moved)["coverage_complete"], moved["runtime"]["outputs"])
                # openat 的请求路径和返回 fd 会各记一次，不能按固定下标误删重复读取。
                # 精确删除移动的任一端，均必须使同一次调用的双端证明失效。
                move_name = operation.split(" ", 1)[1].split("(", 1)[0]
                for endpoint in ("a", "b"):
                    with self.subTest(missing_endpoint=endpoint):
                        changed = copy.deepcopy(moved)
                        events = changed["runtime"]["output_events"]
                        index = next(index for index, event in enumerate(events)
                                     if event["name"] == move_name and event["path"] == f"{self.root}/d/{endpoint}")
                        events.pop(index)
                        self.assertFalse(self.audit(changed)["coverage_complete"])
        duplicate = self.control_document(self.generated_lines()[:2] + [
            f'1 mkdir("{self.root}/d", 0700) = -1 EEXIST (File exists)',
            f'1 link("{self.root}/d/a", "{self.root}/d/b") = 0',
            f'1 link("{self.root}/d/a", "{self.root}/d/b") = -1 EEXIST (File exists)',
            f'1 openat(AT_FDCWD</repo>, "{self.root}/d/b", O_RDONLY) = 3<{self.root}/d/b>'])
        self.assertTrue(self.audit(duplicate)["coverage_complete"])

        # 真实调用确认必经目录归档与对称恢复的坐标；下方模型轨迹只验证重放合同，不代表完整实测来源。
        from tools.official_client_capture import claude_upgrade_capture as capture
        source = Path(self.root) / "d"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "receipt.json").write_text("{}", encoding="utf-8")
        actual_rename = Path.rename
        observed_moves = []
        def record_rename(current, target):
            moved = actual_rename(current, target)
            observed_moves.append((str(current), str(target)))
            return moved
        result = {"evidence_roots": ["/capture/runs/d"], "receipt": "/capture/runs/d/nested/receipt.json"}
        with mock.patch.object(capture.claude_upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", Path(self.root)), \
                mock.patch.object(Path, "rename", record_rename):
            self.assertEqual(capture._archive_failed_job_evidence(result, 1), ["/capture/runs/d.failed-attempt1"])
            self.assertEqual(result["receipt"], "/capture/runs/d.failed-attempt1/nested/receipt.json")
            source.with_name("d.failed-attempt1").rename(source)
        self.assertEqual(observed_moves, [(str(source), str(source.with_name("d.failed-attempt1"))),
                                         (str(source.with_name("d.failed-attempt1")), str(source))])
        self.assertEqual((source / "nested" / "receipt.json").read_text(encoding="utf-8"), "{}")
        subtree = [f'1 mkdir("{self.root}/d", 0700) = 0',
                   f'1 mkdir("{self.root}/d/nested", 0700) = 0',
                   f'1 openat(AT_FDCWD</repo>, "{self.root}/d/nested/receipt.json", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{self.root}/d/nested/receipt.json>']
        for archive_name in ("d.failed-attempt1", "d.failed-attempt1-2"):
            with self.subTest(archive_name=archive_name):
                archived = f"{self.root}/{archive_name}"
                moved = self.control_document(subtree + [
                    f'1 rename("{self.root}/d", "{archived}") = 0',
                    f'1 openat(AT_FDCWD</repo>, "{archived}/nested/receipt.json", O_RDONLY) = 3<{archived}/nested/receipt.json>',
                    f'1 rename("{archived}", "{self.root}/d") = 0',
                    f'1 openat(AT_FDCWD</repo>, "{self.root}/d/nested/receipt.json", O_RDONLY) = 3<{self.root}/d/nested/receipt.json>'])
                self.assertTrue(self.audit(moved)["coverage_complete"], moved["runtime"]["outputs"])
                changed = copy.deepcopy(moved)
                first_move = next(index for index, event in enumerate(changed["runtime"]["output_events"])
                                  if event["name"] == "rename")
                changed["runtime"]["output_events"].pop(first_move + 1)
                self.assertFalse(self.audit(changed)["coverage_complete"])
        for before, operation in (
            (subtree, f'1 rename("{self.root}/d", "{self.root}/d.failed-attempt0") = 0'),
            (subtree, f'1 rename("{self.root}/d", "{self.root}/d.failed-attempt1-1") = 0'),
            (subtree, f'1 rename("{self.root}/d", "{self.root}/d.failed-attempt1x") = 0'),
            (subtree + [f'1 mkdir("{self.root}/other", 0700) = 0'],
             f'1 rename("{self.root}/d", "{self.root}/other/d.failed-attempt1") = 0'),
            (subtree + [f'1 mkdir("{self.root}/d.failed-attempt1", 0700) = 0'],
             f'1 rename("{self.root}/d", "{self.root}/d.failed-attempt1") = 0'),
            ([], f'1 rename("{self.root}/old", "{self.root}/old.failed-attempt1") = 0'),
            ([f'1 mkdir("{self.root}/d.failed-attempt1", 0700) = 0'],
             f'1 rename("{self.root}/d.failed-attempt1", "{self.root}/d") = 0'),
            (subtree + [f'1 rename("{self.root}/d", "{self.root}/d.failed-attempt1") = 0',
                        f'1 mkdir("{self.root}/d", 0700) = 0'],
             f'1 rename("{self.root}/d.failed-attempt1", "{self.root}/d") = 0'),
            (subtree + [f'1 symlink("{self.root}/d/nested/receipt.json", "{self.root}/d/linked") = 0'],
             f'1 rename("{self.root}/d", "{self.root}/d.failed-attempt1") = 0'),
            (subtree + [f'1 rmdir("{self.root}/d/nested") = 0'],
             f'1 rename("{self.root}/d", "{self.root}/d.failed-attempt1") = 0'),
        ):
            with self.subTest(directory_operation=operation):
                self.assertFalse(self.audit(self.control_document(before + [operation]))["coverage_complete"])

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
        for operation in (
            f'1 rename("/host/old", "{self.root}/d/b") = 0',
            f'1 rename("{self.root}/d/a", "/host/new") = 0',
            f'1 rename("{self.root}/d", "{self.root}/new-directory") = 0',
            f'1 renameat2(AT_FDCWD</repo>, "{self.root}/d/a", AT_FDCWD</repo>, "{self.root}/d/b", RENAME_NOREPLACE) = 0',
            f'1 link("/host/old", "{self.root}/d/b") = 0',
            f'1 link("{self.root}/d/a", "/host/new") = 0',
            f'1 linkat(AT_FDCWD</repo>, "{self.root}/d/a", AT_FDCWD</repo>, "{self.root}/d/b", AT_SYMLINK_FOLLOW) = 0',
            f'1 mkdir("{self.root}/unknown", 0700) = -1 EEXIST (File exists)',
        ):
            with self.subTest(operation=operation):
                self.assertFalse(self.audit(self.control_document(self.generated_lines()[:2] + [operation]))["coverage_complete"])

    def control_context(self):
        """原完整四链命令的模型凭证；空遮挡由control启动建立，反例不授予其它场景该合同。"""
        context = copy.deepcopy(self.context)
        context["contract_entry"] = runtime.contract_entry({**self.contract, "mask_roots": [],
                                                             "environment_paths": sorted(runtime.ENVIRONMENT_PATHS)})
        mask_mount = context["mounts"][0]
        context["mounts"] = [context["mounts"][1]]
        context["empty_masks"] = {}
        context["setup_commands"] = ["private", self.root]
        context["setup_trace"] = "\n".join(line for line in context["setup_trace"].splitlines() if runtime.MASK_ROOT not in line) + "\n"
        context["setup_trace_sha256"] = hashlib.sha256(context["setup_trace"].encode()).hexdigest()
        mask_trace = f'1 mount("tmpfs", "{runtime.MASK_ROOT}", "tmpfs", MS_RDONLY, "size=64k,mode=0755") = 0\n'
        path = str(Path(self.directory.name).resolve() / "control.context.json")
        context["control_chain"] = {"scenario": "vc-chain.claude-reconcile", "parent_network_namespace": "net:[200]",
            "network_namespace": "net:[201]", "interfaces": ["lo"], "data_mount": None,
            "mask_mount": mask_mount, "empty_mask": True, "context_path": path,
            "setup_trace": mask_trace, "setup_trace_sha256": hashlib.sha256(mask_trace.encode()).hexdigest(),
            "filesystem_ids": {self.root: "42", runtime.MASK_ROOT: "43"}}
        return context

    def test_control_runtime_requires_real_namespace_tmpfs_and_does_not_trust_plain_tmpdir(self):
        """核对原 runner 的真实接线；独立 TMPDIR、凭证漂移与可见旧挂载均不能跳过原外壳。"""
        from tools.official_client_capture.tests.real_chains import test_claude_upgrade_control_chain as shell
        context = self.control_context()
        control = context["control_chain"]
        path = Path(control["context_path"])
        path.write_text(json.dumps(context), encoding="utf-8")
        path.chmod(0o600)
        values = {control_runtime.CONTROL_CONTEXT_ENV: str(path), control_runtime.CONTROL_CONTEXT_DIGEST_ENV: runtime.digest(context),
                  "TMPDIR": self.root}
        real_is_dir = Path.is_dir
        def directories(target):
            if str(target) == control_runtime.CONTROL_DATA_ROOT:
                return False
            if str(target) == runtime.MASK_ROOT:
                return True
            return real_is_dir(target)
        def filesystem(target):
            return type("Filesystem", (), {"f_fsid": int(control["filesystem_ids"][str(target)]),
                                          "f_flag": 0 if str(target) == self.root else shell.os.ST_RDONLY})()
        with mock.patch.dict(shell.os.environ, {"TMPDIR": self.root}, clear=True), \
                mock.patch.object(shell.sys, "platform", "linux"):
            self.assertEqual(shell._scratch_root(), Path("/var/tmp"))
        with mock.patch.dict(shell.os.environ, values, clear=True), mock.patch.object(Path, "is_dir", directories), \
                mock.patch.object(shell.os, "readlink", side_effect=lambda path: "mnt:[101]" if path.endswith("mnt") else "net:[201]"), \
                mock.patch.object(shell.os, "statvfs", side_effect=filesystem):
            self.assertEqual(shell._scratch_root(), Path(self.root))
            self.assertEqual(shell._namespace_argv(["true"]), ["true"])
            with mock.patch.object(shell.os, "statvfs", return_value=type("OldMount", (), {"f_fsid": 99, "f_flag": 0})()):
                with self.assertRaises(ValueError):
                    shell._scratch_root()
            with mock.patch.dict(shell.os.environ, {"TMPDIR": self.root + "/old-world"}):
                with self.assertRaises(ValueError):
                    shell._scratch_root()
        argv = [sys.executable, "-m", control_runtime.CONTROL_MODULE, "run-scenario", "--client", "claude", "--name",
                "vc-chain.claude-reconcile", "--staging-parent", self.root + "/staging"]
        self.assertEqual(control_runtime.control_scenario(argv, self.root), "vc-chain.claude-reconcile")
        self.assertIsNone(control_runtime.control_scenario([*argv[:-1], "/old/staging"], self.root))
        self.assertIsNone(control_runtime.control_scenario([*argv[:7], "vc-chain.claude-full-validation-only", *argv[8:]], self.root))

    def test_control_kernel_reads_are_bound_to_exact_self_namespace_and_bootstrap_context(self):
        context = self.control_context()
        path = context["control_chain"]["context_path"]
        lines = [f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY) = 3<{path}>',
                 '1 readlink("/proc/self/ns/mnt", "mnt:[101]", 4096) = 9',
                 '1 readlink("/proc/self/ns/net", "net:[201]", 4096) = 9',
                 *self.generated_lines()]
        document = control_audit.filter_stream(lines, ["/"], strict=True, runtime_context=context)
        document["runtime"]["completion"] = {"command_finished": True, "host_inputs_unchanged": True,
            "environment_unchanged": True, "namespace_unchanged": True, "host_entries_sha256": runtime.digest([]),
            "control_isolation_unchanged": True}
        self.assertTrue(self.audit(document)["coverage_complete"])
        for key in ("control_events", "control_isolation_unchanged"):
            changed = copy.deepcopy(document)
            if key == "control_events":
                changed["runtime"][key] = []
            else:
                changed["runtime"]["completion"][key] = False
            self.assertFalse(self.audit(changed)["coverage_complete"])

        for line in ('1 readlink("/proc/999/ns/mnt", "mnt:[101]", 4096) = 9',
                     '1 readlink("/proc/self/ns/net", "net:[202]", 4096) = 9',
                     '1 unshare(CLONE_NEWNET) = 0',
                     '1 openat(AT_FDCWD</repo>, "/proc/self/mountinfo", O_RDONLY) = 3</proc/1/mountinfo>'):
            changed = control_audit.filter_stream([*lines, line], ["/"], strict=True, runtime_context=context)
            changed["runtime"]["completion"] = document["runtime"]["completion"]
            self.assertFalse(self.audit(changed)["coverage_complete"])

    def test_policy_producer_consumes_published_failed_capture_without_rewriting_old_record(self):
        """首轮实际读集只准备新策略；原 passed／轨迹／发布清单逐字保留，缺发布或未解析流拒绝。"""
        base = Path(self.directory.name).resolve()
        host = base / "source.txt"
        host.write_text("真实宿主输入", encoding="utf-8")
        document = self.control_document([*self.generated_lines(),
            f'1 openat(AT_FDCWD</repo>, "{host}", O_RDONLY) = 3<{host}>'])
        store = records.RecordStore(base / "store")
        unit_id = "pre-a3:claude:vc-chain.claude-reconcile"
        argv = [sys.executable, "-m", control_runtime.CONTROL_MODULE, "run-scenario", "--client", "claude", "--name",
                "vc-chain.claude-reconcile", "--staging-parent", self.root + "/staging"]
        spec = {"type": "command", "unit_id": unit_id, "argv": argv, "cwd": "/repo", "env": {},
                "cores": 1, "memory_mb": 128, "exclusive": False, "timeout_seconds": 120, "reservation_restartable": False}
        trace = base / "source.trace.json"
        trace.write_text(json.dumps(document), encoding="utf-8")
        trace_sha256 = hashlib.sha256(trace.read_bytes()).hexdigest()
        trace_path = store.put_log(trace, trace_sha256)
        inputs = [document["runtime"]["context"]["contract_entry"]]
        record = records.seal_record({"unit_id": unit_id, "unit_type": "command", "kind": "formal", "run": {"run_id": "capture-1"},
            "spec": spec, "spec_sha256": records.sha256_json(spec), "inputs": inputs, "inputs_sha256": records.entries_sha256(inputs),
            "environment": [], "environment_sha256": records.entries_sha256([]), "policy_sha256": "policy", "executor": {"sha256": "executor"},
            "completed_at_utc": records.utc_now(), "passed": False, "inheritable": False, "exit_code": 0, "signal": None, "timed_out": False,
            "read_audit": {"status": "failed", "coverage_complete": False, "coverage_scope": "all-file-paths/v1",
                           "inputs_sha256": records.entries_sha256(inputs), "repo_root": "/repo", "data_root": None,
                           "trace": {"sha256": trace_sha256}}})
        path = store.put_record(record)
        item = {"unit_id": unit_id, "disposition": "executed", "record_path": str(path), "record_sha256": record["record_sha256"],
                "spec_sha256": record["spec_sha256"], "inputs_sha256": record["inputs_sha256"]}
        manifest = records.build_manifest(run_id="capture-1", mode="re-execute", planned_units=[unit_id], units=[item],
            environment=[], environment_sha256=records.entries_sha256([]), executor={"sha256": "executor"},
            policy_sha256="policy", inheritance_max_age_hours=24, decided_at_utc=records.utc_now(), record_store=str(store.root))
        manifest_path = store.put_manifest(manifest)
        plan = {"schema_version": "unit-executor-gates/v1", "test_groups": [], "gates": [{"gate_id": "capture", "units": [unit_id]}],
                "units": [{key: value for key, value in spec.items() if key != "type"}]}
        plan_path = base / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        before = {target: target.read_bytes() for target in (path, trace_path, manifest_path)}
        bound = control_audit.bind_audit_policy(plan_path, manifest_path, [unit_id])
        self.assertEqual(bound["policy"]["units"][unit_id]["host_paths"], [str(host)])
        self.assertEqual(bound["plan"]["units"][0]["inputs"], bound["policy"]["units"][unit_id])
        self.assertFalse(bound["provenance"]["reuse_enabled"])
        self.assertEqual({target: target.read_bytes() for target in before}, before)
        source_entries = records.declared_inputs(None, bound["policy"]["units"][unit_id])
        # 同一旧轨迹不能成为第二轮的新来源；实测完成凭证绑定的仍是首轮空 host 明细。
        self.assertFalse(control_audit.strict_audit_reads(document, source_entries, repo_root="/repo", data_root=None)["coverage_complete"])
        with self.assertRaises(control_audit.AuditError):
            control_audit.bind_audit_policy(plan_path, manifest_path, ["cmd:unknown"])
        changed = copy.deepcopy(plan)
        changed["units"][0]["argv"] = ["false"]
        changed_path = base / "changed-plan.json"
        changed_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(control_audit.AuditError, "规格"):
            control_audit.bind_audit_policy(changed_path, manifest_path, [unit_id])
        trace_path.write_bytes(before[trace_path] + b" ")
        with self.assertRaisesRegex(control_audit.AuditError, "完整解析"):
            control_audit.bind_audit_policy(plan_path, manifest_path, [unit_id])
        trace_path.write_bytes(before[trace_path])
        copied = base / "copied-manifest.json"
        copied.write_bytes(before[manifest_path])
        manifest_path.unlink()
        with self.assertRaises(OSError):
            control_audit.bind_audit_policy(plan_path, copied, [unit_id])

    def test_control_prepare_policy_binds_new_full_command_and_preserves_other_units(self):
        """直接消费原 plan_scenario_commands 的完整九项；只把选中的四链输出移到新空盘，不触碰 full79旧根。"""
        from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
        base = Path(self.directory.name).resolve()
        raw = certification.plan_scenario_commands(base / "original" / "staging", client="claude")
        plan = {"schema_version": "unit-executor-gates/v1", "units": raw["units"], "test_groups": [], "gates": []}
        path = base / "actual-nine-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        selected = ["pre-a3:claude:" + scenario for scenario in sorted(control_runtime.CONTROL_SCENARIOS)]
        prepared = control_audit.prepare_audit_policy(path, selected, self.root)
        before = {item["unit_id"]: item for item in raw["units"]}
        for item in prepared["plan"]["units"]:
            if item["unit_id"] in selected:
                self.assertEqual(control_runtime.control_scenario(item["argv"], self.root), item["unit_id"].removeprefix("pre-a3:claude:"))
                self.assertEqual(item["argv"][:-1], before[item["unit_id"]]["argv"][:-1])
            else:
                self.assertEqual(item, before[item["unit_id"]])
        self.assertEqual(set(prepared["policy"]["units"]), set(selected))
        self.assertEqual(json.loads(path.read_text()), plan)

        # 用原记录库、三个复核点及封存context引用核真实消费路径；此模型不代表四链外层已实测。
        from tools.ci import entry_gates, unit_inherit
        from tools.official_client_capture.tests import unit_inherit_fixture as fixture
        adapter = entry_gates.control_read_audit_for_plan(prepared["plan"])
        self.assertIsNotNone(adapter)
        self.assertIsNone(entry_gates.control_read_audit_for_plan(plan))
        private_records = adapter._records_module()
        self.assertIsNot(private_records, unit_inherit._LEGACY_RECORDS())
        token = executor._CONTROL_READ_AUDIT.set(adapter)
        try:
            current_executor = private_records.executor_version(executor._executor_files(executor.DEFAULT_BYTECODE_HELPER))
            self.assertIs(executor._records_module(), private_records)
            self.assertIs(executor._audit_module(), adapter)
        finally:
            executor._CONTROL_READ_AUDIT.reset(token)
        self.assertNotIn("control_read_audit.py", records.executor_version()["files"])
        unit_id = "pre-a3:claude:vc-chain.claude-reconcile"
        spec = next(item for item in prepared["plan"]["units"] if item["unit_id"] == unit_id)
        with mock.patch.object(fixture, "ur", private_records), mock.patch.dict(fixture.TESTS, {}, clear=True):
            world = fixture.World(base / "control-consumer", scenarios=("claude:vc-chain.claude-reconcile",))
            world.units = (unit_id,)
            world.executor = current_executor
            world.declared[unit_id] = private_records.declared_inputs(None, prepared["policy"]["units"][unit_id])
            world.spec = lambda selected_id: {"type": "command", **spec}
            world.record_inputs = lambda selected_id: world.base_inputs(selected_id)
            trace = base / "control-consumer.trace.json"
            trace.write_text(json.dumps(self.control_document(self.generated_lines()[:2] + [
                f'1 rename("{self.root}/d/a", "{self.root}/d/b") = 0',
                f'1 openat(AT_FDCWD</repo>, "{self.root}/d/b", O_RDONLY) = 3<{self.root}/d/b>'])), encoding="utf-8")
            world.trace_sha256 = private_records.file_sha256(trace)
            world.store.put_log(trace, world.trace_sha256)
            source = world.source_record(unit_id, run_id="control-source")
            world.records = {unit_id: source}
            world.source_manifest = world.publish_run("control-source", "re-execute", {unit_id: (source, "executed")},
                                                      decided=fixture.T0 - 3600)
            currents = {unit_id: world.current(unit_id)}
            self.assertEqual(unit_inherit.propose(world.store.root, currents, world.facts(fixture.T0))["units"], {})
            proposal = unit_inherit.propose(world.store.root, currents, world.facts(fixture.T0), client="claude")
            self.assertEqual(proposal["units"][unit_id]["record_sha256"], source["record_sha256"])
            context = world.context(client="claude")
            request = world.request(context=context, sources=proposal)
            result = world.run(request, context=lambda checkpoint: context)
            self.assertEqual(result.problems, [])
            self.assertEqual([item["unit_id"] for item in result.manifest["units"] if item["disposition"] == "inherited"], [unit_id])
        # 离开形成期scope后，独立消费必须从decision封存context引用重新选中同一adapter。
        proof = unit_inherit.verify_published(result.manifest, world.store.root, purpose="control-regression", historical=True)
        self.assertEqual(proof["inherited"], 1)
        for sidecar in adapter.sidecar_paths():
            self.assertEqual(proof["verifier"][sidecar.name], current_executor["files"][sidecar.name])
        self.assertNotIn("control_read_audit.py", unit_inherit.verifier_files())
        changed_executor = copy.deepcopy(current_executor)
        changed_executor["files"]["control_read_audit_runtime.py"] = "0" * 64
        with self.assertRaises(RuntimeError):
            unit_inherit.propose(world.store.root, currents, world.facts(fixture.T0, executor=changed_executor), client="claude")

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
        for flag in ("CLONE_NEWNS", "CLONE_NEWNET", "CLONE_NEWUSER", "CLONE_NEWPID"):
            trace = control_audit.filter_stream([f'1 clone(child_stack=NULL, flags={flag}|SIGCHLD) = 2'], ["/"], strict=True,
                                                runtime_context=self.control_context())
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
