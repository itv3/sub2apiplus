"""门禁第二根的轻量严格验收；所有重放调用原 gate/environment reader。

事实是离线合成数据，原 finalizer 真实封存。不启动 Docker、守卫、账号或
整条 VC-6，不用 mock 把任一原 reader 改成 passed。
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from tools.official_client_capture import codex_upgrade_gate_receipt as gate
from tools.official_client_capture import claude_gate_material_worker as worker
from tools.official_client_capture.tests.control_receipt_fixtures import create_arm_receipt
from tools.official_client_control.canonical import canonical_json_bytes


REPOSITORY = Path(__file__).resolve().parents[3]


def write(root: Path, relative: str, document) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(canonical_json_bytes(document))
    path.chmod(0o600)
    return path


def gate_world(root: Path) -> tuple[dict, str]:
    root.mkdir(mode=0o700)
    subject = {"campaign_id": "claude-sealed-gate-fixture", "campaign_mode": "formal",
        "campaign_purpose": "validation_only", "candidate_id": "candidate-current",
        "candidate_purpose": "validation_only", "target_version": "2.1.285",
        "target_architecture": "linux/arm64", "profile_id": "claude-code-fixture",
        "profile_digest": "1" * 64, "candidate_package_digest": "2" * 64,
        "candidate_source_tree_sha256": "3" * 64, "candidate_image_id": "sha256:" + "4" * 64,
        "candidate_image_reference": "fixture/sub2api@sha256:" + "4" * 64,
        "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None}
    environment = {}
    for role, phase in (("before", "gate_before"), ("after", "gate_after")):
        path = create_arm_receipt(root, phase=phase, subject_id="gate-attempt-001", prefix="environment-" + role)
        environment[role] = {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    gates = []
    for index, (name, (cwd, command)) in enumerate(sorted(gate.CANDIDATE_COMMANDS.items())):
        path = write(root, "evidence/" + name + ".json", {"gate_id": name, "passed": True})
        gates.append({"gate_id": name, "working_directory": cwd, "command": list(command), "host": "ARM64",
            "architecture": "linux/arm64", "started_at_utc": f"2026-10-08T00:0{index}:00Z",
            "completed_at_utc": f"2026-10-08T00:0{index}:30Z", "exit_code": 0, "status": "passed",
            "passed_count": 1, "failed_count": 0, "skipped_count": 0, "stdout_sha256": "6" * 64,
            "stderr_sha256": "7" * 64, "evidence": [{"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]})
    facts = {"schema_version": gate.FACTS_SCHEMA, "phase": gate.CANDIDATE_PHASE,
        "attempt": {"attempt_id": "gate-attempt-001", "root_cause_id": None, "previous_receipt": None},
        "subject": subject, "inputs": [], "environment": environment, "gate_plan": None, "gates": gates}
    write(root, "candidate-facts.json", facts)
    gate.finalize(root, "candidate-facts.json", "candidate-receipt.json")
    return subject, "candidate-receipt.json"


def view_spec(identity, logical_root: Path, physical_root: Path, *, selected=None):
    files = []
    for path in sorted(physical_root.rglob("*")):
        if path.is_file() and (selected is None or path.relative_to(physical_root).as_posix() in selected):
            raw = path.read_bytes()
            files.append({"logical_path": str(logical_root / path.relative_to(physical_root)), "physical_path": str(path),
                "root_id": "fixture-gate-root", "relative_path": path.relative_to(physical_root).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "mode": f"{path.stat().st_mode & 0o7777:04o}"})
    directories = {str(logical_root): {"logical_path": str(logical_root), "physical_path": str(physical_root)}}
    for path in sorted(physical_root.rglob("*")):
        if path.is_dir():
            logical = str(logical_root / path.relative_to(physical_root))
            directories[logical] = {"logical_path": logical, "physical_path": str(path)}
    code = [*identity.document["code_bindings"], *identity.document["target_bindings"], *identity.document["runtime_manifest"]["files"]]
    missing = []
    for name in [str(logical_root), *[row["absolute_path"] for row in code]]:
        for parent in Path(name).parents:
            directories.setdefault(str(parent), {"logical_path": str(parent), "physical_path": str(parent)})
    for row in code:
        if row["absolute_path"].endswith(".py"):
            cache = worker.reader_bytecode_probe(identity.document, row["absolute_path"])
            if not Path(cache).exists():
                missing.append(cache)
    return files, list(directories.values()), missing


class GateReaderWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = worker.compute_gate_reader_identity(REPOSITORY,
            formed_context={"fixture_only": True})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sealed-gate-reader-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.original = self.root / "original"
        self.subject, self.receipt = gate_world(self.original)

    def replay(self, physical=None, selected=None):
        physical = physical or self.original
        files, directories, missing = view_spec(self.identity, self.original, physical, selected=selected)
        return worker.run_gate_material_reader(self.identity, files, directories, missing,
            str(self.original), self.receipt, view="original" if physical == self.original else "imported", expected_subject=self.subject)

    def test_original_and_imported_are_true_reader_results_without_parent_hooks(self):
        original_open = open
        old_reader_bytes = Path(gate.__file__).read_bytes()
        expected = gate.replay(self.original, self.receipt)
        first = self.replay()
        imported = self.root / "imported"
        shutil.copytree(self.original, imported)
        # 原根移走后仍只能读取精确副本，不准降级读原坐标。
        shutil.rmtree(self.original)
        second = self.replay(imported)
        self.assertEqual(first, second)
        self.assertEqual(first["result"], expected)
        self.assertIs(open, original_open)
        self.assertEqual(Path(gate.__file__).read_bytes(), old_reader_bytes)
        self.assertTrue(any(path.endswith("environment-before-facts.json") for path in first["reader_readset"]))
        self.assertTrue(any(row["operation"] == "lstat" for row in first["reader_accesses"]))

    def test_missing_environment_facts_is_not_replaced_by_receipt_summary(self):
        paths = {path.relative_to(self.original).as_posix() for path in self.original.rglob("*") if path.is_file()}
        paths.remove("environment-before-facts.json")
        with self.assertRaisesRegex(worker.ReaderError, "未授权|无法独立重放"):
            self.replay(selected=paths)

    def test_same_sha_unlisted_coordinate_has_no_permission(self):
        shutil.copyfile(self.original / self.receipt, self.original / "same-sha.json")
        files, directories, missing = view_spec(self.identity, self.original, self.original, selected={self.receipt})
        with self.assertRaises(worker.ReaderError):
            worker.run_gate_material_reader(self.identity, files, directories, missing, str(self.original), "same-sha.json",
                view="original", expected_subject=self.subject)

    def test_identity_ordinary_descriptor_does_not_launch(self):
        with self.assertRaisesRegex(worker.ReaderError, "受管身份"):
            worker.run_gate_material_reader(dict(self.identity.document), [], [], [], str(self.original), self.receipt,
                view="original", expected_subject=self.subject)

    def test_runtime_manifest_or_code_byte_drift_rejects(self):
        document = copy.deepcopy(self.identity.document)
        document["runtime_manifest"]["files"][0]["sha256"] = "f" * 64
        document["identity_sha256"] = worker._digest({key: value for key, value in document.items() if key != "identity_sha256"})
        with self.assertRaisesRegex(worker.ReaderError, "运行库清单摘要"):
            worker.verify_reader_identity(document)

    def test_target_reader_uses_real_fd_and_historical_authorization_chain(self):
        from tools.official_client_capture.tests.test_ci_target_platform_gate import TargetPlatformGateTests
        from tools.ci import target_platform_gate
        case = TargetPlatformGateTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        proof = case.target_evidence()
        self.subject.update(case.request["target"], candidate_image_reference="fixture/sub2api@" + case.request["target"]["candidate_image_id"])
        facts = json_read(self.original / "candidate-facts.json")
        facts["subject"] = self.subject
        target_path = write(self.original, "target-units/evidence.json", proof)
        row = next(row for row in facts["gates"] if row["gate_id"] == "target-platform")
        row.update(command=target_platform_gate.COMMAND, unit_execution={"path": "target-units/evidence.json", "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest()})
        write(self.original, "unit-facts.json", facts)
        self.receipt = "unit-receipt.json"
        # finalize 的实时有效期取 fixture 已冻结时间；生成和后续历史重放
        # 都是真实原合同。仅这里用历史 facts 直接重建并写 canonical receipt。
        receipt = gate.build_receipt(self.original, "unit-facts.json", _historical=True)
        write(self.original, self.receipt, receipt)
        auxiliary = case.root.resolve()
        for path in auxiliary.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        auxiliary.chmod(0o700)
        gate_files, gate_dirs, missing = view_spec(self.identity, self.original, self.original)
        auxiliary_files, auxiliary_dirs, auxiliary_missing = view_spec(self.identity, auxiliary, auxiliary)
        result = worker.run_gate_material_reader(self.identity, gate_files + auxiliary_files,
            gate_dirs + auxiliary_dirs, sorted(set(missing + auxiliary_missing)), str(self.original), self.receipt,
            view="original", expected_subject=self.subject)
        self.assertEqual(result["result"], gate.replay(self.original, self.receipt))
        self.assertTrue(any(row["operation"] == "fstat" for row in result["reader_accesses"]))
        self.assertTrue(any(path.endswith("request.json") for path in result["reader_readset"]))
        self.assertFalse(any(path.startswith("/etc/") or path == "/dev/tty" for path in result["reader_readset"]))
        imported_gate, imported_auxiliary = self.root / "imported-gate", self.root / "imported-auxiliary"
        shutil.copytree(self.original, imported_gate)
        shutil.copytree(auxiliary, imported_auxiliary)
        imported_files, imported_dirs, imported_missing = view_spec(self.identity, self.original, imported_gate)
        aux_files, aux_dirs, aux_missing = view_spec(self.identity, auxiliary, imported_auxiliary)
        shutil.rmtree(self.original)
        shutil.rmtree(auxiliary)
        copied = worker.run_gate_material_reader(self.identity, imported_files + aux_files,
            imported_dirs + aux_dirs, sorted(set(imported_missing + aux_missing)), str(self.original), self.receipt,
            view="imported", expected_subject=self.subject)
        self.assertEqual(copied, result)


class ReaderIsolationDenialTests(unittest.TestCase):
    """子进程直接检验所有底层钩子；不用替换原 gate 的成功结果。"""
    def test_io_native_side_effects_and_swallowed_violations_all_fail(self):
        import subprocess, sys
        temporary = tempfile.TemporaryDirectory(prefix="sealed-gate-denial-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        path = root / "material.bin"
        path.write_bytes(b"immutable raw evidence")
        path.chmod(0o600)
        probe = root / "probe.py"
        probe.write_text(PROBE_SOURCE, encoding="utf-8")
        operations = ["builtins-open", "io-open", "lowlevel-open", "fileio", "posix-open", "stat", "scandir", "readlink",
            "unknown-fd", "fd-stat", "write-mode", "write-flags", "pread", "dup2", "dir-fd", "unknown-native",
            "process", "network", "mmap", "ctypes", "unknown-python", "unknown-exec", "symlink", "source-drift", "swallowed", "ticket-native-bypass"]
        for operation in operations:
            with self.subTest(operation=operation):
                outcome = subprocess.run([str(Path(sys.executable).resolve()), "-I", "-B", "-S", str(probe), str(REPOSITORY), str(path), operation],
                    env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, capture_output=True, check=False, timeout=15)
                self.assertEqual(outcome.returncode, 72, outcome.stderr.decode())
                self.assertIn(b'"fatal":true', outcome.stdout.replace(b" ", b""))


class FrozenActiveDerivationProofTests(unittest.TestCase):
    """真实离线 VC-3／VC-4 前缀的最小来源专项，不执行 VC-5、VC-6 或真实账号。

    原批次生产一次精确准备包和 Store，再给每个负例复制独立数据根。
    所有原 reader 与纯派生函数照常执行；不把 bootstrap 占位当成旧
    Active Snapshot，也不改种子原件或正式来源注册表。
    """

    @classmethod
    def setUpClass(cls):
        from tools.official_client_capture.tests.test_claude_upgrade_batch import _RealTreeBatchCase
        from tools.official_client_capture import claude_qualification_material_source as material
        cls.material = material
        cls.seed = _RealTreeBatchCase("runTest")
        cls.addClassCleanup(cls.seed.doCleanups)
        cls.seed.setUpClass()
        cls.seed.setUp()
        cls.seed.drive_vc3_vc4_to_candidate_sealed()
        cls.seed_originals = {path.relative_to(cls.seed.data).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in cls.seed.data.rglob("*") if path.is_file()}

    @classmethod
    def tearDownClass(cls):
        actual = {path.relative_to(cls.seed.data).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in cls.seed.data.rglob("*") if path.is_file()}
        if actual != cls.seed_originals:
            raise AssertionError("冻结 Active 专项的种子历史原件发生漂移")

    def setUp(self):
        from tools.official_client_capture import claude_upgrade_vc as vc
        from tools.official_client_control.store import ControlStore
        temporary = tempfile.TemporaryDirectory(prefix="frozen-active-proof-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data"
        shutil.copytree(self.seed.data, self.data)
        self.campaign = self.data / self.seed.campaign.relative_to(self.seed.data)
        self.manifest = json_read(self.campaign / "campaign.json")
        self.vc4 = json_read(vc.vc_checkpoint_path(self.campaign, "VC-4", revision=1))
        self.build = json_read(self.campaign / self.vc4["stage_receipt"]["path"])
        self.store = ControlStore.open_read_only(self.campaign / "control-store")
        self.originals, self.parents = {}, {}

    def retain(self, path, expected_sha256=None):
        from tools.official_client_control.errors import ControlError
        path = Path(path)
        if not path.is_relative_to(self.data):
            raise ControlError("冻结 Active 专项不得读取数据根以外的原件")
        raw, _metadata = self.material._plain_file(path)
        row = {"path": path.relative_to(self.data).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        if expected_sha256 is not None and row["sha256"] != expected_sha256:
            raise ControlError("冻结 Active 专项原件摘要错配")
        if self.originals.setdefault(row["path"], row) != row:
            raise ControlError("冻结 Active 专项同坐标原件漂移")
        return raw

    def project_parent(self, path, base, metadata=(), *, coordinate_roots=()):
        """按原 subject 相同的闭合字段构造专项投影，权限仅在证明返回后消费。"""
        from tools.official_client_control.canonical import canonical_sha256
        raw = self.retain(path)
        document = self.material.parse_json_bytes(raw)
        domains = []
        for coordinate in metadata:
            node = document
            for key in coordinate:
                node = node[int(key)] if isinstance(node, list) else node[key]
            domains.append((coordinate, canonical_sha256(node)))
        binding = self.originals[path.relative_to(self.data).as_posix()]
        projection = self.material.VerifiedQualificationSubjectProjection(binding,
            base.relative_to(self.data).as_posix(), tuple(domains),
            tuple((coordinate, root.relative_to(self.data).as_posix()) for coordinate, root in coordinate_roots),
            self.material._TOKEN)
        self.assertEqual(self.parents.setdefault(binding["path"], projection), projection)

    def prove(self):
        self.material._prove_frozen_active_derivation(self.store, self.data, self.campaign, self.manifest, self.build,
            retain=self.retain, project_parent=self.project_parent)

    def source_and_projection(self, source_files=None):
        from tools.official_client_control.delivery_execution import VerifiedSource
        from tools.official_client_control import sealed_gate_bindings as bindings
        from tools.official_client_capture import claude_upgrade_compare as compare
        # 实际 subject 在 helper 之前已由同一原 VC-4 reader 核构建父。
        # 专项也实跑这个前提，再沿原合同保留宿主构建元数据域；不
        # 给绝对路径外部读取权，不伪造验收四根或 gate 成功结果。
        build, binding = compare.load_candidate_build_receipt(self.campaign, self.manifest,
            self.build["candidate_id"], self.build["candidate_revision"])
        self.assertEqual(build, self.build)
        self.project_parent(self.campaign / binding["path"], self.campaign,
            (("binary",), ("build", "parameters"), ("implementation_tests",)))
        # 专项仅消费 helper 已证明的明确父域与完整读集，不伪造门禁或资格四根。
        source = VerifiedSource("0" * 64, "1" * 64, source_files or {}, "0" * 64, "fixture-only", self.data)
        projection = bindings.VerifiedGateProjection((), (), tuple(self.originals.values()), (),
            bindings._TOKEN, tuple(self.parents.values()))
        return source, projection

    def binding(self, path):
        self.retain(path)
        return self.originals[path.relative_to(self.data).as_posix()]

    def test_exact_prepared_revision_survives_later_unrelated_material(self):
        from tools.official_client_capture import claude_upgrade_profile as profile
        stage = json_read(self.campaign / profile.STAGE_RESULT_RELATIVE)
        self.assertEqual(stage["prepared_revision"], "rev-0002")
        result = json_read(self.campaign / stage["prepare_result"]["path"])
        successor = copy.deepcopy(result)
        successor.update(revision="rev-0003", target_profile_sha256="f" * 64)
        write(self.campaign, profile.prepare_result_path(self.campaign, "rev-0003").relative_to(self.campaign).as_posix(), successor)
        self.prove()
        names = set(self.originals)
        self.assertTrue(any("approved/rev-0002/active-profile.json" in name for name in names))
        self.assertFalse(any("rev-0003" in name for name in names))
        active = self.campaign / "inputs/active-profile.json"
        row = self.binding(active)
        self.assertEqual(self.parents[row["path"]].project(row, active.read_bytes()), [])
        from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
        source, projection = self.source_and_projection()
        closed = project_delivery_raw_bindings(source, [row], gate_projection=projection)
        self.assertTrue(names <= {binding["path"] for binding in closed})

    def test_every_material_read_by_original_readers_is_retained(self):
        from unittest import mock
        import builtins
        import io
        observed = set()
        def observe(original):
            def call(name, *arguments, **options):
                if isinstance(name, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(name))
                    if path.is_absolute() and path.is_relative_to(self.data):
                        observed.add(path.relative_to(self.data).as_posix())
                return original(name, *arguments, **options)
            return call
        # 只记录原 I/O 再原样委托，不替换 reader 返回值或父语义。
        with mock.patch.object(builtins, "open", observe(builtins.open)), \
             mock.patch.object(io, "open", observe(io.open)), \
             mock.patch.object(os, "open", observe(os.open)):
            self.prove()
        self.assertFalse(observed - set(self.originals), sorted(observed - set(self.originals)))

    def test_non_digest_active_field_change_rejects_without_new_active_permission(self):
        from tools.official_client_control.errors import ControlError
        from tools.official_client_capture.capturelib.model import ConfigurationError
        active = self.campaign / "inputs/active-profile.json"
        document = json_read(active)
        document["identity"]["privacy_mode"] = "changed-non-digest-field"
        active.write_bytes(canonical_json_bytes(document))
        with self.assertRaises((ControlError, ConfigurationError)):
            self.prove()
        self.assertNotIn(active.relative_to(self.data).as_posix(), self.parents)

    def test_approved_active_policy_selector_and_attestation_mismatches_each_reject(self):
        from tools.official_client_control.errors import ControlError
        from tools.official_client_capture.capturelib.model import ConfigurationError
        from tools.official_client_capture import claude_upgrade_profile as profile
        stage = json_read(self.campaign / profile.STAGE_RESULT_RELATIVE)
        prepared = profile.load_prepare_result(self.campaign, self.manifest, stage["prepared_revision"])
        release = self.store.load_object(self.build["store"]["release_artifact_ref"])["payload"]
        mutations = [
            (self.campaign / prepared["prepared"]["active-profile.json"]["path"],
                lambda document: document["identity"].update(privacy_mode="approved-active-mismatch")),
            (self.campaign / profile.PREPARE_DRAFT_ROOT / stage["prepared_revision"] / profile.PREPARE_FILES["policy"],
                lambda document: document.update(target_version="2.1.999")),
            (self.campaign / "inputs/active-release-catalog.json",
                lambda document: document["selectors"].update(production_active="f" * 64)),
            (self.store.object_path(release["snapshot_ref"]),
                lambda document: document["payload"].update(compiler_attestation_sha256="f" * 64)),
        ]
        for path, mutate in mutations:
            with self.subTest(path=path.relative_to(self.data).as_posix()):
                raw = path.read_bytes()
                self.addCleanup(path.write_bytes, raw)
                document = self.material.parse_json_bytes(raw)
                mutate(document)
                path.write_bytes(canonical_json_bytes(document))
                self.originals, self.parents = {}, {}
                with self.assertRaises((ControlError, ConfigurationError)):
                    self.prove()
                self.assertNotIn((self.campaign / "inputs/active-profile.json").relative_to(self.data).as_posix(), self.parents)
                path.write_bytes(raw)

    def test_same_active_bytes_at_other_coordinate_remain_strict_and_reject(self):
        from tools.official_client_control.errors import ControlError
        from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
        self.prove()
        active = self.campaign / "inputs/active-profile.json"
        duplicate = self.campaign / "inputs/same-bytes-active.json"
        duplicate.write_bytes(active.read_bytes())
        duplicate.chmod(0o600)
        row = self.binding(duplicate)
        source, projection = self.source_and_projection()
        self.assertIsNone(projection.project(row, active.read_bytes()))
        with self.assertRaises(ControlError):
            project_delivery_raw_bindings(source, [row], gate_projection=projection)

    def test_unknown_json_missing_binding_still_rejects(self):
        from tools.official_client_control.errors import ControlError
        from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
        self.prove()
        unknown = write(self.campaign, "inputs/unknown-json.json", {
            "unverified": {"path": "unbound/missing.json", "sha256": "f" * 64, "bytes": 20}})
        row = self.binding(unknown)
        source, projection = self.source_and_projection()
        self.assertIsNone(projection.project(row, unknown.read_bytes()))
        with self.assertRaises(ControlError):
            project_delivery_raw_bindings(source, [row], gate_projection=projection)

    def test_source_view_bytes_different_from_disk_original_reject(self):
        from tools.official_client_control.errors import ControlError
        from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
        self.prove()
        active = self.campaign / "inputs/active-profile.json"
        raw = active.read_bytes()
        row = self.binding(active)
        source, projection = self.source_and_projection({row["path"]: raw + b" "})
        with self.assertRaisesRegex(ControlError, "字节漂移"):
            project_delivery_raw_bindings(source, [row], gate_projection=projection)
        self.assertEqual(active.read_bytes(), raw)


PROBE_SOURCE = r'''"""专项负例：钩子由真实实现安装，只读进程不执行任何真实副作用。"""
import sys, os, io, _io, posix, json, socket, mmap, ctypes, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.official_client_capture import claude_gate_material_worker as worker
repository, material, operation = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
worker._preload(repository)
runtime = worker._runtime_manifest(repository)
code = [worker._file_identity(Path(__file__)), worker._file_identity(Path(worker.__file__))]
code += runtime["files"]
for name in ("tools/official_client_capture", "tools/official_client_control"):
    code += [worker._file_identity(path) for path in (repository/name).rglob("*.py") if "tests" not in path.parts]
raw = material.read_bytes()
logical = str(material)
physical = material
if operation == "symlink":
    physical = material.parent/"symlink.bin"
    physical.symlink_to(material)
row = {"logical_path":logical,"physical_path":str(physical),"sha256":worker.hashlib.sha256(raw).hexdigest(),"bytes":len(raw),"mode":"0600"}
if operation == "source-drift":
    material.write_bytes(b"X"*len(raw))
unknown_python = compile("def illegal():\n return 1\n", "<evidence-unbound>", "exec")
space = {}
exec(unknown_python,space)
unknown_exec = compile("pass", "<evidence-unbound>", "exec")
view = worker.GateMaterialReadView([row], [{"logical_path":str(material.parent),"physical_path":str(material.parent)}], [], code)
def attempt():
    if operation == "builtins-open": return open("/etc/passwd")
    if operation == "io-open": return io.open("/etc/passwd")
    if operation == "lowlevel-open": return _io.open("/etc/passwd")
    if operation == "fileio": return _io.FileIO("/etc/passwd")
    if operation == "posix-open": return posix.open("/etc/passwd",os.O_RDONLY)
    if operation == "stat": return posix.stat("/etc/passwd")
    if operation == "scandir": return os.scandir("/etc")
    if operation == "readlink": return posix.readlink("/etc/passwd")
    if operation == "unknown-fd": return os.read(0,1)
    if operation == "fd-stat": return posix.fstat(1)
    if operation == "write-mode": return io.open(logical,"w")
    if operation == "write-flags": return os.open(logical,os.O_RDWR|os.O_TRUNC)
    if operation == "pread": return os.pread(0,1,0)
    if operation == "dup2": return os.dup2(0,1)
    if operation == "dir-fd": return os.stat(logical,dir_fd=0)
    if operation == "unknown-native": return posix.getcwd()
    if operation == "process": return subprocess.run(["/usr/bin/true"])
    if operation == "network": return socket.socket()
    if operation == "mmap": return mmap.mmap(0,1)
    if operation == "ctypes": return ctypes.string_at(0,1)
    if operation == "unknown-python": return space["illegal"]()
    if operation == "unknown-exec": return exec(unknown_exec)
    if operation == "symlink": return os.stat(logical)
    if operation == "source-drift": return open(logical,"rb")
    if operation == "swallowed":
        try: io.open("/etc/passwd")
        except Exception: return {"passed":True}
    if operation == "ticket-native-bypass":
        view.ticket=("open",(str(material),os.O_RDONLY))
        return posix.getcwd()
sys.stdin.close()
view.install()
try:
    attempt()
except Exception:
    pass
finally:
    # 在清理前保存真实操作的拒绝，禁止把测试外壳关闭 profiler 自身的
    # 拒绝误计为负例通过。原 worker 的关闭语句在固定 worker 源码里。
    operation_violations=tuple(view.violations)
    try: sys.setprofile(None)
    except worker.ReaderError: pass
fatal=bool(operation_violations)
sys.stdout.write(json.dumps({"fatal":fatal,"violations":operation_violations},separators=(",",":"))+"\n")
raise SystemExit(72 if fatal else 0)
'''


class IntakePreparationBoundaryTests(unittest.TestCase):
    """无 ARM64 现场时必须零写拒绝，描述性 JSON 永远不是来源能力。"""

    def test_uninstalled_formal_driver_refuses_before_any_write(self):
        import platform
        import subprocess
        import sys
        if platform.system() == "Linux" and platform.machine().lower() in {"aarch64", "arm64"} and os.geteuid() == 0:
            self.skipTest("本机拒绝边界不读取实际 ARM64 受管安装")
        with tempfile.TemporaryDirectory(prefix="sealed-gate-formal-refusal-", dir="/private/tmp") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            driver = REPOSITORY / "tools/arm64_capture_driver/driver/qualification_materials.py"
            for mode in ("preview", "prepare"):
                with self.subTest(mode=mode):
                    outcome = subprocess.run([str(Path(sys.executable).resolve()), "-I", "-B", "-S", str(driver), mode,
                        "--data-root", str(root), "--configuration", str(root / "draft.json"),
                        "--candidate-env", str(root / "candidate.env"), "--rollback-env", str(root / "rollback.env")],
                        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=15, check=False)
                    self.assertEqual(outcome.returncode, 2, outcome.stdout.decode())
                    self.assertIn("formal_intake_not_ready", outcome.stderr.decode())
                    self.assertEqual(list(root.iterdir()), [])

    def test_json_descriptor_cannot_replace_formal_capability(self):
        from tools.official_client_capture import claude_qualification_material_source as source_module
        from tools.official_client_control.errors import ControlError
        for purpose in source_module.PURPOSES:
            with self.subTest(purpose=purpose), self.assertRaisesRegex(ControlError, "JSON descriptor"):
                source_module.resolve_qualification_source(None, {},
                    {"authority": source_module.FORMAL_AUTHORITY, "source_scope": "formal"}, purpose=purpose)

    def test_missing_formal_contracts_never_fall_back_to_repository_code(self):
        from tools.official_client_capture import claude_qualification_material_source as source_module
        from tools.official_client_control.errors import ControlError
        if Path("/root/arm64-capture-driver").exists():
            self.skipTest("不读取本机已经存在的受管安装")
        for name in ("install", "parse_env", "vc5_admission"):
            for current in (True, False):
                with self.subTest(name=name, current=current), self.assertRaisesRegex(ControlError, "不得替换为仓库实现"):
                    source_module.managed_driver_module(name, current=current)


class MaterialImportProofTests(unittest.TestCase):
    """明确隔离 policy12 验证树；不冒充已部署后继策略或正式认证。"""
    @classmethod
    def setUpClass(cls):
        from tools.official_client_control.tests.fixtures import ManagedToolTree
        cls.temporary = tempfile.TemporaryDirectory(prefix="sealed-gate-policy12-", dir="/private/tmp")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.tree = ManagedToolTree(Path(cls.temporary.name) / "repo")
        policy_path = cls.tree.path("official_client_capture/claude_tool_identity_policy_v2.json")
        policy = json_read(policy_path)
        policy["policy_version"] = 12
        policy_path.write_bytes(canonical_json_bytes(policy))
        shutil.copytree(REPOSITORY / "tools/ci", cls.tree.root / "tools/ci", ignore=shutil.ignore_patterns("__pycache__"))
        cls.active = cls.tree.active()
        cls.active.__enter__()
        cls.addClassCleanup(cls.active.__exit__, None, None, None)

    def setUp(self):
        from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
        from tools.official_client_capture.tests.claude_qualification_source_fixture import QualificationSubjectWorld, QualificationDeliveryCampaign
        from tools.official_client_capture import claude_upgrade_accept as accept
        from tools.official_client_control.canonical import canonical_sha256
        temporary = tempfile.TemporaryDirectory(prefix="sealed-gate-materials-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.configuration, _, _ = fixture_world(self.root)
        self.world = self.root / "qualification"
        self.world.mkdir(mode=0o700)
        f = QualificationDeliveryCampaign(self.world, data_root=self.root, schema="v2")
        self.fixture = f
        f.materials = self.configuration
        f.bootstrap_and_campaign(platforms=["linux/arm64"], campaign_purpose="validation_only")
        f.discovery_and_evidence()
        f.profile_objects()
        for role, tag in (("rollback", "baseline"), ("candidate", "strict")):
            f.select_profile(role)
            f.profile_approve("validation_only")
            f.open_candidate_revision("synthetic-candidate-" + tag)
            f.strict_validate(tag=tag)
            suffix = "" if tag == "strict" else "-baseline"
            q = {"store_relative": f.store.root.relative_to(self.root).as_posix(),
                 "external_root_relative": f.external_root.relative_to(self.root).as_posix(),
                 "campaign_id": f.campaign_id, "candidate_id": "synthetic-candidate-" + tag,
                 "candidate_ref": f.references["candidate-" + tag], "build_receipt_ref": f.references["candidate-build-receipt" + suffix],
                 "validation_ref": f.references["validation-completed" + suffix], "acceptance_ref": f.references["acceptance-" + tag]}
            parent = {"schema_version": "claude-upgrade-acceptance/v1", "candidate_id": q["candidate_id"],
                "store": {"acceptance_ref": q["acceptance_ref"], "validation_completed_ref": q["validation_ref"]},
                "production_state": "not_activated"}
            if role == "candidate":
                self.gate_root = self.root / "original-gate"
                _unused, receipt = gate_world(self.gate_root)
                self.source_world = QualificationSubjectWorld(self.root, f, q,
                    self.root / self.configuration[role]["profile_binding"]["path"])
                identity = self.source_world.identity
                facts = json_read(self.gate_root / "candidate-facts.json")
                expected = accept.expected_gate_subject(self.source_world.manifest,
                    f.store.load_fact(q["candidate_ref"])["payload"], identity)
                facts["subject"] = {**expected, "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None}
                write(self.gate_root, "candidate-facts.json", facts)
                (self.gate_root / receipt).unlink()
                # v4 gate 的 producer 坐标不可迁移；由同一隔离树的原
                # finalizer 创建新原件，不改写 producer 或 mock reader。
                import subprocess, sys
                outcome = subprocess.run([str(Path(sys.executable).resolve()), "-I", "-B", "-S",
                    str(self.tree.root / "tools/official_client_capture/codex_upgrade_gate_receipt.py"), "finalize",
                    "--evidence-root", str(self.gate_root), "--facts", "candidate-facts.json", "--output", receipt],
                    env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, capture_output=True, check=False, timeout=30)
                self.assertEqual(outcome.returncode, 0, outcome.stderr.decode())
                raw = (self.gate_root / receipt).read_bytes()
                parent = self.source_world.seal_accept({"evidence_root": str(self.gate_root),
                    "receipt": {"path": receipt, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}})
            else:
                path = write(self.root, "parents/" + role + ".json", parent)
                q["sealed_result_binding"] = {"path": path.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            self.configuration[role]["qualification"] = q
        from tools.official_client_control.store import ControlStore
        self.store = ControlStore.open_read_only(f.store.root)

    def prepare(self):
        from tools.official_client_capture.tests.claude_typed_vc6_fixture import prepare_fixture_qualification_materials
        return prepare_fixture_qualification_materials(self.store, self.configuration, self.root, gate_root=self.gate_root)

    def validate(self, binding, *, purpose="formed_replay"):
        from tools.official_client_control.qualification_material_import import validate_qualification_material_import
        from tools.official_client_control.delivery_execution import VerifiedSource
        source = VerifiedSource("0" * 64, "1" * 64, {}, "0" * 64, "fixture-only", self.root)
        return validate_qualification_material_import(self.store, binding, source,
            {role: self.configuration[role]["qualification"] for role in ("candidate", "rollback")},
            purpose=purpose, expected_source_scope="regression_fixture")

    def subject(self, *, qualification=None, read_bindings=None, read_projections=None):
        from tools.official_client_capture.claude_qualification_material_source import qualification_gate_subject
        q = qualification or self.configuration["candidate"]["qualification"]
        return qualification_gate_subject(self.store, q, self.root,
            json_read(self.root / q["sealed_result_binding"]["path"]),
            read_bindings=read_bindings, read_projections=read_projections)

    def gated_historical_rollback(self):
        """在同一 Store 内独立生产回滚四根和真实 gate，不借候选换角色。"""
        import subprocess, sys
        from tools.official_client_capture.tests.claude_qualification_source_fixture import QualificationSubjectWorld, QualificationDeliveryCampaign
        from tools.official_client_capture import claude_upgrade_accept as accept
        root = self.root / "historical-rollback-world"
        root.mkdir(mode=0o700)
        fixture = QualificationDeliveryCampaign(root, data_root=self.root, schema="v2",
            store=self.fixture.store, external_root=self.fixture.external_root,
            external_prefix="independent-gated-rollback")
        fixture.campaign_id = "synthetic-gated-rollback-1.0.0-campaign"
        fixture.persona = {**fixture.persona, "official_product": "synthetic-rollback-cli"}
        fixture.materials = self.configuration
        fixture.bootstrap_and_campaign(platforms=["linux/arm64"], campaign_purpose="validation_only")
        fixture.discovery_and_evidence()
        fixture.profile_objects()
        fixture.select_profile("rollback")
        fixture.profile_approve("validation_only")
        fixture.open_candidate_revision("synthetic-candidate-baseline")
        fixture.strict_validate(tag="baseline")
        q = {"store_relative": fixture.store.root.relative_to(self.root).as_posix(),
            "external_root_relative": fixture.external_root.relative_to(self.root).as_posix(),
            "campaign_id": fixture.campaign_id, "candidate_id": "synthetic-candidate-baseline",
            "candidate_ref": fixture.references["candidate-baseline"],
            "build_receipt_ref": fixture.references["candidate-build-receipt-baseline"],
            "validation_ref": fixture.references["validation-completed-baseline"],
            "acceptance_ref": fixture.references["acceptance-baseline"]}
        world = QualificationSubjectWorld(self.root, fixture, q,
            self.root / self.configuration["rollback"]["profile_binding"]["path"])
        gate_root = self.root / "original-rollback-gate"
        _unused, receipt = gate_world(gate_root)
        expected = accept.expected_gate_subject(world.manifest,
            fixture.store.load_fact(q["candidate_ref"])["payload"], world.identity)
        facts = json_read(gate_root / "candidate-facts.json")
        facts["subject"] = {**expected, "production_tree_sha256": None,
            "acceptance_sha256": None, "promotion_receipt_sha256": None}
        write(gate_root, "candidate-facts.json", facts)
        (gate_root / receipt).unlink()
        outcome = subprocess.run([str(Path(sys.executable).resolve()), "-I", "-B", "-S",
            str(self.tree.root / "tools/official_client_capture/codex_upgrade_gate_receipt.py"), "finalize",
            "--evidence-root", str(gate_root), "--facts", "candidate-facts.json", "--output", receipt],
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, capture_output=True, check=False, timeout=30)
        self.assertEqual(outcome.returncode, 0, outcome.stderr.decode())
        raw = (gate_root / receipt).read_bytes()
        world.seal_accept({"evidence_root": str(gate_root),
            "receipt": {"path": receipt, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}})
        self.configuration["rollback"]["qualification"] = q
        # 较新 baseline 与 revision 都由原 Store 生产器追加。历史 accept
        # 仍封存 baseline 0，不能因“当前”视图移动而改读新的资格或首轮。
        fixture.open_evaluation_baseline(1, tag="baseline", kind="evaluator-only")
        newer = fixture.open_candidate_revision("synthetic-candidate-later")
        fixture.strict_validate(tag="later")
        self.assertGreater(fixture.store.load_fact(newer)["payload"]["revision"], world.revision)
        self.assertEqual(fixture.store.load_fact(fixture.references["evaluation-baseline-baseline-b1"])["payload"]["baseline"], 1)
        newer_stage = world.campaign / "candidates/synthetic-candidate-later/result.json"
        write(self.root, newer_stage.relative_to(self.root).as_posix(),
            {"candidate_id": "synthetic-candidate-later", "status": "complete", "evaluation_baseline": 1})
        return fixture, world, gate_root, facts["subject"]

    def prepare_gated_pair(self, rollback_gate_root):
        """两份独立 gate 沿原来源、复制器和 reader 取得；只临时登记 fixture。"""
        from unittest import mock
        from tools.official_client_capture import claude_qualification_material_source as source_module
        from tools.official_client_capture import claude_qualification_material_import as importer
        from tools.official_client_control.canonical import canonical_sha256
        identity = worker.compute_gate_reader_identity(self.tree.root, formed_context={"fixture_only": True})
        sources = []
        for role, gate_root in (("candidate", self.gate_root), ("rollback", rollback_gate_root)):
            provenance = {"fixture_contract": "qualified-gate-fixture/v1", "formation_context": {"fixture_only": True}}
            capability = source_module.freeze_root_capability(gate_root, "gate_evidence",
                authority=source_module.FIXTURE_AUTHORITY, reference_id="fixture-gated-" + role,
                reference_sha256=canonical_sha256(provenance),
                allowed_relative_paths=[path.relative_to(gate_root).as_posix() for path in sorted(gate_root.rglob("*")) if path.is_file()])
            intake = source_module.VerifiedIntakeContext(source_module.FIXTURE_AUTHORITY, "regression_fixture",
                self.root, self.tree.root, provenance, (capability,), identity, source_module._TOKEN)
            sources.append(source_module.prepare_qualification_source(self.store,
                self.configuration[role]["qualification"], role=role, intake_context=intake))
        def resolve(store, qualification, reference, *, purpose):
            self.assertIn(reference, sources)
            self.assertEqual(reference.qualification, qualification)
            source_module._check_four_roots(store, qualification)
            return reference
        with mock.patch.dict(source_module.QUALIFICATION_SOURCE_RESOLVERS,
                {(source_module.FIXTURE_AUTHORITY, purpose): resolve for purpose in source_module.PURPOSES}):
            return importer.prepare_qualification_material_import(self.store, sources, managed_data_root=self.root)

    def test_gated_historical_rollback_replays_exact_originals_after_newer_baseline_and_revision(self):
        from tools.official_client_control.canonical import canonical_sha256
        fixture, world, gate_root, expected = self.gated_historical_rollback()
        q = self.configuration["rollback"]["qualification"]
        original_paths = [world.campaign / "campaign.json", world.build_path, world.round_path,
            self.root / q["sealed_result_binding"]["path"], gate_root / "candidate-receipt.json"]
        originals = {path: path.read_bytes() for path in original_paths}
        self.assertEqual(self.subject(qualification=q), expected)
        self.assertNotEqual(q["campaign_id"], self.configuration["candidate"]["qualification"]["campaign_id"])
        proof = self.prepare_gated_pair(gate_root)
        projection = self.validate(proof)
        rollback = next(row for row in projection.domains if row["role"] == "rollback")
        self.assertEqual(rollback["gate_subject_sha256"], canonical_sha256(expected))
        shutil.rmtree(gate_root)
        self.assertEqual(self.validate(proof), projection)
        self.assertEqual({path: path.read_bytes() for path in original_paths[:-1]},
                         {path: originals[path] for path in original_paths[:-1]})

    def test_gated_historical_rollback_missing_original_cannot_use_newer_material(self):
        from tools.official_client_capture.capturelib.model import ConfigurationError
        from tools.official_client_control.errors import ControlError
        _fixture, world, gate_root, _expected = self.gated_historical_rollback()
        newer = world.campaign / "candidates/synthetic-candidate-later/build-receipt.json"
        newer.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        newer.write_bytes(world.build_path.read_bytes())
        newer.chmod(0o600)
        world.build_path.unlink()
        q = self.configuration["rollback"]["qualification"]
        with self.assertRaises((ControlError, ConfigurationError, OSError)):
            self.subject(qualification=q)
        with self.assertRaises((ControlError, ConfigurationError, OSError)):
            self.prepare_gated_pair(gate_root)
        self.assertFalse((self.root / "control/qualification-imports").exists())

    def replace_build(self, document):
        """只重建当前独立反例的来源绑定；不修改历史失败世界或门禁原件。"""
        from tools.official_client_capture import claude_upgrade_vc as vc
        from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
        document["receipt_digest"] = artifacts.digest({key: value for key, value in document.items() if key != "receipt_digest"})
        write(self.root, self.source_world.build_path.relative_to(self.root).as_posix(), document)
        previous_path, previous = vc.replay_vc_checkpoint(self.source_world.campaign,
            self.source_world.plan, "VC-3")
        self.source_world.checkpoint("VC-4", self.source_world.build_path, previous_path, previous)
        q = self.configuration["candidate"]["qualification"]
        parent = json_read(self.root / q["sealed_result_binding"]["path"])
        self.source_world.seal_accept(parent["external_gate"])

    def test_subject_uses_real_accept_fields_and_independent_originals(self):
        from tools.official_client_capture import claude_upgrade_acceptance as acceptance
        from tools.official_client_capture import claude_upgrade_accept as accept
        q = self.configuration["candidate"]["qualification"]
        parent = json_read(self.root / q["sealed_result_binding"]["path"])
        self.assertEqual(set(parent), acceptance.STAGE_RESULT_FIELDS)
        self.assertNotIn("candidate_identity", parent)
        self.assertNotIn("campaign_mode", parent)
        expected = accept.expected_gate_subject(self.source_world.manifest,
            self.store.load_fact(q["candidate_ref"])["payload"], self.source_world.identity)
        self.assertEqual(self.subject(), {**expected, "production_tree_sha256": None,
            "acceptance_sha256": None, "promotion_receipt_sha256": None})

    def test_subject_keeps_historical_revision_and_fixed_first_round(self):
        original = self.subject()
        self.fixture.open_candidate_revision("synthetic-candidate-next")
        # 故意放一份不能被读取的最大轮次：首轮身份不得随当前 revision
        # 或后继评估轮次漂移，历史原件仍由精确 VC-4 与首轮 reader 核验。
        write(self.root, (self.source_world.round_path.parent / "validation-rounds/v2/result.json").relative_to(self.root).as_posix(),
            {"candidate_id": "synthetic-candidate-next", "status": "failed"})
        self.assertEqual(self.subject(), original)

    def test_subject_rejects_changed_frozen_plan_even_with_valid_new_digest(self):
        from tools.official_client_control.errors import ControlError
        path = self.source_world.campaign / "campaign.json"
        manifest = json_read(path)
        manifest["created_at_utc"] = "2026-08-19T00:00:00Z"
        write(self.root, path.relative_to(self.root).as_posix(), manifest)
        (self.source_world.campaign / "campaign.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
        with self.assertRaisesRegex(ControlError, "封印.*冻结总计划"):
            self.subject()

    def test_subject_rejects_vc4_store_build_from_other_candidate(self):
        from tools.official_client_control.errors import ControlError
        build = copy.deepcopy(self.source_world.build)
        build["store"]["candidate_build_receipt_ref"] = self.configuration["rollback"]["qualification"]["build_receipt_ref"]
        self.replace_build(build)
        with self.assertRaisesRegex(ControlError, "精确候选.*revision"):
            self.subject()

    def test_subject_rejects_fixed_first_package_from_other_candidate(self):
        from tools.official_client_control.errors import ControlError
        q = self.configuration["rollback"]["qualification"]
        foreign = self.store.load_fact(q["validation_ref"])["payload"]["candidate_evidence_package_ref"]
        stage = json_read(self.source_world.round_path)
        stage["store"]["candidate_evidence_package_ref"] = foreign
        write(self.root, self.source_world.round_path.relative_to(self.root).as_posix(), stage)
        self.source_world.identity["candidate_package_digest"] = foreign["sha256"]
        parent = json_read(self.root / self.configuration["candidate"]["qualification"]["sealed_result_binding"]["path"])
        self.source_world.seal_accept(parent["external_gate"])
        with self.assertRaisesRegex(ControlError, "首轮制品.*精确候选"):
            self.subject()

    def test_subject_cannot_replace_missing_first_round_with_newer_result(self):
        from tools.official_client_capture.capturelib.model import ConfigurationError
        stage = json_read(self.source_world.round_path)
        write(self.root, (self.source_world.round_path.parent / "validation-rounds/v2/result.json").relative_to(self.root).as_posix(), stage)
        self.source_world.round_path.unlink()
        with self.assertRaisesRegex(ConfigurationError, "尚未封存.*capture-candidate"):
            self.subject()

    def test_subject_readset_and_two_parent_projections_close_exact_materials(self):
        from tools.official_client_control.canonical import canonical_sha256
        from tools.official_client_control.delivery_execution import VerifiedSource
        from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
        reads, parents = [], []
        self.subject(read_bindings=reads, read_projections=parents)
        paths = {row["path"] for row in reads}
        q = self.configuration["candidate"]["qualification"]
        required = [self.source_world.campaign / "campaign.json", self.source_world.campaign / "campaign.sha256",
            self.source_world.build_path, self.source_world.release_path, self.source_world.profile_path,
            self.source_world.round_path, self.store.receipt_path(q["build_receipt_ref"]),
            self.store.object_path({"object_kind": "candidate_evidence_package", "sha256": self.source_world.identity["candidate_package_digest"]})]
        self.assertTrue({path.relative_to(self.root).as_posix() for path in required} <= paths)
        self.assertTrue(any(row.metadata_domains == ((("build_identity", "parameters"),
            canonical_sha256(
                self.store.load_receipt(q["build_receipt_ref"])["build_identity"]["parameters"])),) for row in parents))
        proof = self.prepare()
        projection = self.validate(proof)
        source = VerifiedSource("0" * 64, "1" * 64, {}, "0" * 64, "fixture-only", self.root)
        closed = project_delivery_raw_bindings(source, [q["sealed_result_binding"]], gate_projection=projection)
        self.assertTrue(paths <= {row["path"] for row in closed})
        self.assertTrue(any(row["path"].endswith("external/candidate/sub2api.bin") for row in closed))

    def test_subject_projection_does_not_authorize_same_sha_wrong_coordinate(self):
        from tools.official_client_control.delivery_execution import VerifiedSource
        from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
        from tools.official_client_control.errors import ControlError
        proof = self.prepare()
        projection = self.validate(proof)
        raw = self.source_world.build_path.read_bytes()
        duplicate = self.source_world.campaign / "candidates/copied-build.json"
        duplicate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        duplicate.write_bytes(raw)
        row = {"path": duplicate.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        self.assertIsNone(projection.project(row, raw))
        source = VerifiedSource("0" * 64, "1" * 64, {}, "0" * 64, "fixture-only", self.root)
        with self.assertRaises(ControlError):
            project_delivery_raw_bindings(source, [row], gate_projection=projection)

    def test_subject_consumer_checks_received_original_bytes(self):
        from tools.official_client_control.delivery_execution import VerifiedSource
        from tools.official_client_control.qualification_material_import import validate_qualification_material_import
        from tools.official_client_control.errors import ControlError
        proof = self.prepare()
        path = self.source_world.campaign / "campaign.json"
        raw = path.read_bytes()
        # 受管通道给出的形成期字节不同时，即使当前磁盘同名原件正确，
        # 消费端仍须拒绝，不能以磁盘新读替代冻结来源能力的原件。
        source = VerifiedSource("0" * 64, "1" * 64,
            {path.relative_to(self.root).as_posix(): raw.replace(b'formal', b'invalid', 1)}, "0" * 64, "fixture-only", self.root)
        with self.assertRaisesRegex(ControlError, "字节漂移"):
            validate_qualification_material_import(self.store, proof, source,
                {role: self.configuration[role]["qualification"] for role in ("candidate", "rollback")},
                purpose="formed_replay", expected_source_scope="regression_fixture")

    def test_gate_self_report_cannot_change_subject_expected_from_originals(self):
        from tools.official_client_control.errors import ControlError
        facts = json_read(self.gate_root / "candidate-facts.json")
        facts["subject"]["candidate_package_digest"] = "f" * 64
        write(self.gate_root, "candidate-facts.json", facts)
        self.refinalize()
        with self.assertRaises((ControlError, worker.ReaderError)):
            self.prepare()

    def test_typed_fixture_uses_frozen_accept_root_and_original_receipt(self):
        from tools.official_client_capture.tests.claude_typed_vc6_fixture import (
            _fixture_qualification_gate_root, prepare_fixture_qualification_materials,
        )
        from tools.official_client_capture import claude_qualification_material_source as source_module
        q = self.configuration["candidate"]["qualification"]
        parent = json_read(self.root / q["sealed_result_binding"]["path"])
        originals = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                     for path in [self.root / q["sealed_result_binding"]["path"],
                                  *sorted(self.gate_root.rglob("*"))] if path.is_file()}
        registrations = dict(source_module.QUALIFICATION_SOURCE_RESOLVERS)
        root = _fixture_qualification_gate_root(self.store, q, self.root)
        self.assertEqual(str(root), parent["external_gate"]["evidence_root"])
        binding = prepare_fixture_qualification_materials(self.store, self.configuration, self.root, gate_root=root)
        projection = self.validate(binding, purpose="current_import")
        self.assertTrue(any(row["source_relative_path"] == parent["external_gate"]["receipt"]["path"]
                            for row in projection.material_files))
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                          for path in originals}, originals)
        self.assertEqual(source_module.QUALIFICATION_SOURCE_RESOLVERS, registrations)

    def test_typed_fixture_rejects_drifted_accept_before_intake(self):
        from tools.official_client_capture.tests.claude_typed_vc6_fixture import _fixture_qualification_gate_root
        from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
        q = copy.deepcopy(self.configuration["candidate"]["qualification"])
        q["sealed_result_binding"]["bytes"] += 1
        with self.assertRaisesRegex(guard.GuardError, "文件绑定漂移"):
            _fixture_qualification_gate_root(self.store, q, self.root)
        self.assertFalse((self.root / "control/qualification-imports").exists())

    def test_typed_fixture_uses_read_only_handle_without_mutating_writable_store(self):
        from tools.official_client_capture.tests.claude_typed_vc6_fixture import prepare_fixture_qualification_materials
        writable = self.fixture.store
        self.assertFalse(writable.read_only)
        originals = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                     for path in writable.root.rglob("*") if path.is_file()}
        binding = prepare_fixture_qualification_materials(writable, self.configuration, self.root,
                                                         gate_root=self.gate_root)
        self.assertTrue(self.validate(binding, purpose="current_import").material_files)
        self.assertFalse(writable.read_only)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                          for path in originals}, originals)

    def test_typed_fixture_rejects_drifted_original_receipt_without_replacement(self):
        from tools.official_client_capture.tests.claude_typed_vc6_fixture import _fixture_qualification_gate_root
        from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
        q = copy.deepcopy(self.configuration["candidate"]["qualification"])
        path = self.root / q["sealed_result_binding"]["path"]
        parent = json_read(path)
        receipt = self.gate_root / parent["external_gate"]["receipt"]["path"]
        original_receipt = receipt.read_bytes()
        # 仅形成一份无效的测试父声明；原门禁收据及其 producer 完整保留。
        parent["external_gate"]["receipt"]["bytes"] += 1
        write(self.root, q["sealed_result_binding"]["path"], parent)
        q["sealed_result_binding"].update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        with self.assertRaisesRegex(guard.GuardError, "文件绑定漂移"):
            _fixture_qualification_gate_root(self.store, q, self.root)
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self.assertFalse((self.root / "control/qualification-imports").exists())

    def test_complete_import_replays_real_four_roots_and_survives_original_removal(self):
        from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
        from tools.official_client_control.delivery_execution import VerifiedSource
        parents_before = {self.root / self.configuration[role]["qualification"]["sealed_result_binding"]["path"]:
            (self.root / self.configuration[role]["qualification"]["sealed_result_binding"]["path"]).read_bytes()
            for role in ("candidate", "rollback")}
        proof = self.prepare()
        before = {path: path.read_bytes() for path in (self.root / "control/qualification-imports").rglob("*") if path.is_file()}
        projection = self.validate(proof, purpose="current_import")
        self.assertTrue(projection.domains)
        shutil.rmtree(self.gate_root)
        replayed = self.validate(proof)
        self.assertEqual(projection, replayed)
        source = VerifiedSource("0" * 64, "1" * 64, {}, "0" * 64, "fixture-only", self.root)
        closed = project_delivery_raw_bindings(source, [q["sealed_result_binding"] for q in [self.configuration["candidate"]["qualification"], self.configuration["rollback"]["qualification"]]], gate_projection=projection)
        self.assertTrue(any(row["path"].endswith("environment-before-facts.json") for row in closed))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual({path: path.read_bytes() for path in parents_before}, parents_before)

    def refinalize(self):
        """只在未封存的合成 fixture 更新事实并由精确原 finalizer 生成。"""
        import subprocess, sys
        from tools.official_client_control.canonical import canonical_sha256
        q = self.configuration["candidate"]["qualification"]
        facts = json_read(self.gate_root / "candidate-facts.json")
        for row in facts["gates"]:
            for binding in row["evidence"]:
                binding["sha256"] = hashlib.sha256((self.gate_root / binding["path"]).read_bytes()).hexdigest()
        write(self.gate_root, "candidate-facts.json", facts)
        receipt_path = self.gate_root / "candidate-receipt.json"
        receipt_path.unlink()
        outcome = subprocess.run([str(Path(sys.executable).resolve()), "-I", "-B", "-S",
            str(self.tree.root / "tools/official_client_capture/codex_upgrade_gate_receipt.py"), "finalize", "--evidence-root", str(self.gate_root),
            "--facts", "candidate-facts.json", "--output", "candidate-receipt.json"],
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, capture_output=True, check=False, timeout=30)
        self.assertEqual(outcome.returncode, 0, outcome.stderr.decode())
        parent_path = self.root / q["sealed_result_binding"]["path"]
        parent = json_read(parent_path)
        parent["external_gate"]["receipt"].update(sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(), bytes=receipt_path.stat().st_size)
        # 只重建当前用例尚未导入的合成封印和 accept；原历史世界不修改。
        self.source_world.seal_accept(parent["external_gate"])

    def test_unknown_json_keeps_strict_transitive_binding_and_raw_bytes(self):
        leaf = write(self.gate_root, "unknown/leaf.json", {"original": "byte-exact"})
        facts = json_read(self.gate_root / "candidate-facts.json")
        evidence = facts["gates"][0]["evidence"][0]["path"]
        parent = write(self.gate_root, evidence, {"not_a_known_schema": {"path": "unknown/leaf.json", "sha256": hashlib.sha256(leaf.read_bytes()).hexdigest(), "bytes": leaf.stat().st_size}})
        raw = parent.read_bytes()
        self.refinalize()
        proof = self.prepare()
        doc = json_read(self.root / proof["path"])
        rows = [row for row in doc["files"] if row["source_relative_path"] == "unknown/leaf.json"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reader_role"], "strict")
        self.assertEqual(parent.read_bytes(), raw)
        self.validate(proof)

    def test_unknown_json_missing_descendant_refuses_before_any_copy(self):
        from tools.official_client_control.errors import ControlError
        facts = json_read(self.gate_root / "candidate-facts.json")
        evidence = facts["gates"][0]["evidence"][0]["path"]
        write(self.gate_root, evidence, {"unknown_child": {"path": "unknown/missing.json", "sha256": "f" * 64, "bytes": 12}})
        self.refinalize()
        with self.assertRaisesRegex(ControlError, "下层绑定"):
            self.prepare()
        self.assertFalse((self.root / "control/qualification-imports").exists())

    def test_source_id_cannot_be_reauthorized_by_resealing_proof(self):
        from tools.official_client_control.canonical import canonical_sha256
        from tools.official_client_control.errors import ControlError
        proof = self.prepare()
        doc = json_read(self.root / proof["path"])
        acquisition = doc["source_acquisition_bindings"][0]["binding"]
        acq = json_read(self.root / acquisition["path"])
        acq["source_id"] = "f" * 64
        acq["acquisition_content_sha256"] = canonical_sha256({key: value for key, value in acq.items() if key != "acquisition_content_sha256"})
        path = write(self.root, acquisition["path"], acq)
        acquisition.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        doc["identity_sha256"] = canonical_sha256({key: value for key, value in doc.items() if key != "identity_sha256"})
        path = write(self.root, proof["path"], doc)
        proof.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        with self.assertRaisesRegex(ControlError, "来源 ID"):
            self.validate(proof)

    def test_acquisition_source_original_metadata_is_also_archived(self):
        # 不启动正式 ARM64 factory；直接通过真实复制器核实实际参数与
        # 清单被复制成不同原件，不能被来源上下文元数据占位。
        from tools.official_client_capture import claude_qualification_material_source as source_module
        from tools.official_client_capture import claude_qualification_material_import as importer
        from tools.official_client_control.canonical import canonical_sha256
        from unittest import mock
        identity = worker.compute_gate_reader_identity(self.tree.root, formed_context={"fixture_only": True})
        original = write(self.root, "source-layout/manifest.json", {"actual": "driver-manifest-raw"})
        raw = original.read_bytes()
        descriptor = {"absolute_path": str(original), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        sources = []
        for role in ("candidate", "rollback"):
            provenance = {"fixture_contract": "qualified-gate-fixture/v1" if role == "candidate" else "explicit-no-gate-rollback/v1", "formation_context": {"fixture_only": True}, "original_descriptors": {"driver_manifest": descriptor}}
            roots = ()
            if role == "candidate":
                roots = (source_module.freeze_root_capability(self.gate_root, "gate_evidence", authority=source_module.FIXTURE_AUTHORITY,
                    reference_id="fixture-qualified-gate", reference_sha256=canonical_sha256(provenance), allowed_relative_paths=[path.relative_to(self.gate_root).as_posix() for path in sorted(self.gate_root.rglob("*")) if path.is_file()]),)
            intake = source_module.VerifiedIntakeContext(source_module.FIXTURE_AUTHORITY, "regression_fixture", self.root, self.tree.root, provenance, roots, identity, source_module._TOKEN,
                (source_module.SourceOriginal("driver_manifest", original, raw),))
            sources.append(source_module.prepare_qualification_source(self.store, self.configuration[role]["qualification"], role=role, intake_context=intake))
        def resolve(store, q, ref, *, purpose):
            self.assertIn(ref, sources)
            source_module._check_four_roots(store, q)
            return ref
        with mock.patch.dict(source_module.QUALIFICATION_SOURCE_RESOLVERS, {(source_module.FIXTURE_AUTHORITY, purpose): resolve for purpose in source_module.PURPOSES}):
            proof = importer.prepare_qualification_material_import(self.store, sources, managed_data_root=self.root)
        doc = json_read(self.root / proof["path"])
        acquisition = json_read(self.root / doc["source_acquisition_bindings"][0]["binding"]["path"])
        self.assertNotEqual(acquisition["source_driver_manifest_binding"], acquisition["source_context_binding"])
        self.assertEqual((self.root / acquisition["source_driver_manifest_binding"]["path"]).read_bytes(), raw)
        projection = self.validate(proof)
        self.assertIn(acquisition["source_driver_manifest_binding"], projection.archive_bindings)


    def test_missing_copy_fails_without_reopening_original(self):
        from tools.official_client_control.errors import ControlError
        proof = self.prepare()
        doc = json_read(self.root / proof["path"])
        row = next(row for row in doc["files"] if row["source_relative_path"].endswith("environment-before-facts.json"))
        (self.root / row["archive_binding"]["path"]).unlink()
        with self.assertRaises((ControlError, worker.ReaderError, OSError)):
            self.validate(proof)

    def test_existing_complete_import_is_only_revalidated(self):
        proof = self.prepare()
        directory = self.root / "control/qualification-imports"
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.rglob("*") if path.is_file()}
        self.assertEqual(self.prepare(), proof)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.rglob("*") if path.is_file()}, before)

    def test_incomplete_existing_import_is_never_repaired_in_place(self):
        from tools.official_client_control.errors import ControlError
        proof = self.prepare()
        (self.root / proof["path"]).unlink()
        directory = self.root / "control/qualification-imports"
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(ControlError, "缺完整 proof"):
            self.prepare()
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.rglob("*") if path.is_file()}, before)

    def test_fixture_proof_cannot_select_formal_scope(self):
        from tools.official_client_control.errors import ControlError
        from tools.official_client_control.qualification_material_import import validate_qualification_material_import
        from tools.official_client_control.delivery_execution import VerifiedSource
        proof = self.prepare()
        source = VerifiedSource("0" * 64, "1" * 64, {}, "0" * 64, "fixture-only", self.root)
        with self.assertRaisesRegex(ControlError, "来源用途"):
            validate_qualification_material_import(self.store, proof, source,
                {role: self.configuration[role]["qualification"] for role in ("candidate", "rollback")},
                purpose="current_delivery", expected_source_scope="formal")

    def test_actual_policy11_reader_refuses_before_new_import_directory(self):
        # 旧身份来自独立策略11源码树的真实测量；不改自报字段、不 mock
        # reader。资格四根仍按当前 Store 原合同准备，但旧 reader 无写权。
        from tools.official_client_control.tests.fixtures import ManagedToolTree
        from tools.official_client_control.errors import ControlError
        from tools.official_client_control.canonical import canonical_sha256
        from tools.official_client_capture import claude_qualification_material_source as source_module
        from tools.official_client_capture import claude_qualification_material_import as importer
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="sealed-gate-policy11-", dir="/private/tmp") as temporary:
            tree = ManagedToolTree(Path(temporary) / "repo")
            policy_path = tree.path("official_client_capture/claude_tool_identity_policy_v2.json")
            policy = json_read(policy_path)
            policy["policy_version"] = 11
            policy_path.write_bytes(canonical_json_bytes(policy))
            shutil.copytree(REPOSITORY / "tools/ci", tree.root / "tools/ci", ignore=shutil.ignore_patterns("__pycache__"))
            identity = worker.compute_gate_reader_identity(tree.root, formed_context={"fixture_only": True})
            self.assertEqual(identity.document["tool_identity"]["policy_version"], 11)
            sources = []
            for role in ("candidate", "rollback"):
                provenance = {"fixture_contract": "qualified-gate-fixture/v1" if role == "candidate" else "explicit-no-gate-rollback/v1",
                    "formation_context": {"fixture_only": True}}
                roots = ()
                if role == "candidate":
                    roots = (source_module.freeze_root_capability(self.gate_root, "gate_evidence", authority=source_module.FIXTURE_AUTHORITY,
                        reference_id="fixture-policy11", reference_sha256=canonical_sha256(provenance),
                        allowed_relative_paths=[path.relative_to(self.gate_root).as_posix() for path in sorted(self.gate_root.rglob("*")) if path.is_file()]),)
                intake = source_module.VerifiedIntakeContext(source_module.FIXTURE_AUTHORITY, "regression_fixture", self.root,
                    tree.root, provenance, roots, identity, source_module._TOKEN)
                sources.append(source_module.prepare_qualification_source(self.store, self.configuration[role]["qualification"], role=role, intake_context=intake))

            def resolver(store, qualification, reference, *, purpose):
                self.assertIn(reference, sources)
                source_module._check_four_roots(store, qualification)
                return reference

            with mock.patch.dict(source_module.QUALIFICATION_SOURCE_RESOLVERS,
                    {(source_module.FIXTURE_AUTHORITY, purpose): resolver for purpose in source_module.PURPOSES}):
                with self.assertRaisesRegex(ControlError, "写入前拒绝旧读取身份"):
                    importer.prepare_qualification_material_import(self.store, sources, managed_data_root=self.root)
            self.assertFalse((self.root / "control/qualification-imports").exists())


def json_read(path):
    import json
    return json.loads(path.read_bytes())


if __name__ == "__main__":
    unittest.main()
