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
        from tools.official_client_capture.tests.test_claude_real_delivery_integration import DeliveryCampaign
        from tools.official_client_capture import claude_upgrade_accept as accept
        from tools.official_client_control.canonical import canonical_sha256
        temporary = tempfile.TemporaryDirectory(prefix="sealed-gate-materials-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.configuration, _, _ = fixture_world(self.root)
        self.world = self.root / "qualification"
        self.world.mkdir(mode=0o700)
        f = DeliveryCampaign(self.world, schema="v2")
        self.fixture = f
        f.materials = self.configuration
        f.bootstrap_and_campaign(platforms=["linux/arm64"], campaign_purpose="validation_only")
        f.discovery_and_evidence()
        f.profile_objects()
        for role, tag in (("rollback", "baseline"), ("candidate", "strict")):
            f.select_profile(role)
            f.profile_approve("validation_only")
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
                identity = {"profile_id": "claude-code-fixture", "profile_digest": self.configuration[role]["runtime_profile_sha256"],
                    "candidate_package_digest": "2" * 64, "candidate_image_id": self.configuration[role]["image_digest"],
                    "candidate_image_reference": "fixture/sub2api@" + self.configuration[role]["image_digest"]}
                parent.update(campaign_mode="formal", campaign_purpose="validation_only", candidate_identity=identity)
                facts = json_read(self.gate_root / "candidate-facts.json")
                expected = accept.expected_gate_subject({"campaign_id": q["campaign_id"], "campaign_mode": "formal", "campaign_purpose": "validation_only", "target_version": f.campaign_document["target_version"]}, f.store.load_fact(q["candidate_ref"])["payload"], identity)
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
                parent["external_gate"] = {"evidence_root": str(self.gate_root), "receipt": {"path": receipt, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}
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

    def test_complete_import_replays_real_four_roots_and_survives_original_removal(self):
        from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
        from tools.official_client_control.delivery_execution import VerifiedSource
        parents_before = {path: path.read_bytes() for path in (self.root / "parents").glob("*.json")}
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
        write(self.root, q["sealed_result_binding"]["path"], parent)
        q["sealed_result_binding"].update(sha256=hashlib.sha256(parent_path.read_bytes()).hexdigest(), bytes=parent_path.stat().st_size)

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
