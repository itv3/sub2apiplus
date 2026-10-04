"""A-02 离线验收：完整历史、vendor 回退、网络批准和历史快照索引闭合。

建树使用真实 Git 和不访问网络的本地 Go 模块；网络测试只写隔离收据，不调用生产 Docker。
快照校验执行候选源码里的真实 Go 合同，覆盖伪造内容摘要、缺失 blob 和历史覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture.tests.test_arm64_capture_driver import (
    REPO_ROOT, SCRIPTS, _DriverFixture, _git, _history_repo, _run, _write_json, load_script,
)


class BuildNetworkContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = _DriverFixture(self.root)
        self.contract = load_script("vc4_contract")
        self.parser = load_script("parse_env")
        values = self.parser.parse(self.fixture.env_file.read_text())
        self.config = {**values, **self.parser.derive(values)}
        Path(self.config["BUNDLE"]).write_bytes("本轮隔离 bundle".encode())
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.approval = self.root / "network-approval.json"

    def approve(self, mode="host"):
        self.config["VC4_BUILD_NETWORK"] = mode
        binding = self.contract.network_binding(self.config)
        value = {"schema_version": self.contract.APPROVAL_SCHEMA, "status": "approved", "binding": binding,
                 "approved_by": "隔离夹具批准身份", "approved_at_utc": (self.now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "expires_at_utc": (self.now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        _write_json(self.approval, value)
        self.config["VC4_BUILD_NETWORK_APPROVAL"] = str(self.approval)
        return value

    def test_default_check_is_read_only_and_host_requires_exact_approval(self):
        before = {str(path): path.stat().st_mtime_ns for path in self.root.rglob("*")}
        self.assertIsNone(self.contract.network_inputs(self.config)["approval"])
        self.assertEqual(before, {str(path): path.stat().st_mtime_ns for path in self.root.rglob("*")})
        self.config["VC4_BUILD_NETWORK"] = "host"
        with self.assertRaisesRegex(ValueError, "缺少专项批准"):
            self.contract.network_inputs(self.config)
        self.approve()
        result = self.contract.network_inputs(self.config, now=self.now)
        self.assertEqual(result["binding"]["network_mode"], "host")
        self.assertEqual(result["approval"]["approved_by"], "隔离夹具批准身份")

    def test_cross_campaign_candidate_host_and_bundle_approval_are_rejected(self):
        original = self.approve()
        for field in ("campaign_id", "candidate_id", "git_commit", "host", "bundle", "driver", "impact_scope", "network_mode"):
            with self.subTest(field=field):
                value = json.loads(json.dumps(original))
                value["binding"][field] = "错误绑定"
                _write_json(self.approval, value)
                with self.assertRaisesRegex(ValueError, "输入绑定不一致"):
                    self.contract.network_inputs(self.config, now=self.now)

    def test_expiry_future_time_permissions_and_symlink_are_rejected(self):
        original = self.approve()
        for field, delta in (("expires_at_utc", 0), ("approved_at_utc", 1)):
            value = dict(original)
            value[field] = (self.now + timedelta(seconds=delta)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _write_json(self.approval, value)
            with self.assertRaisesRegex(ValueError, "尚未生效或已过期"):
                self.contract.network_inputs(self.config, now=self.now)
        _write_json(self.approval, original)
        self.approval.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "属主或权限"):
            self.contract.network_inputs(self.config, now=self.now)
        self.approval.chmod(0o600)
        link = self.root / "approval-link.json"
        link.symlink_to(self.approval)
        self.config["VC4_BUILD_NETWORK_APPROVAL"] = str(link)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.contract.network_inputs(self.config, now=self.now)

    def test_parser_clears_inherited_network_and_rejects_invalid_mode(self):
        result = subprocess.run(["python3", "-B", str(SCRIPTS / "parse_env.py"), str(self.fixture.env_file)],
            env=dict(os.environ, VC4_BUILD_NETWORK="host", VC4_BUILD_NETWORK_APPROVAL="/old/approval.json"),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("export VC4_BUILD_NETWORK=default", result.stdout)
        self.assertIn("unset VC4_BUILD_NETWORK_APPROVAL", result.stdout)
        with self.assertRaisesRegex(ValueError, "只能是"):
            self.parser.parse(self.fixture.env_file.read_text() + "VC4_BUILD_NETWORK=bridge\n")

    def test_installed_standalone_intent_loads_tools_from_round_file_without_inherited_D(self):
        # 驱动安装目录位于仓库之外，不能用 __file__ 的父目录猜测受管工具坐标。
        copied = self.root / "installed/driver"
        copied.mkdir(parents=True)
        for name in ("vc4_contract.py", "driver_config.py", "parse_env.py", "build.sh"):
            shutil.copy2(SCRIPTS / name, copied / name)
        shutil.rmtree(self.fixture.data_root / "tools")
        (self.fixture.data_root / "tools").symlink_to(REPO_ROOT / "tools", target_is_directory=True)
        environment = dict(os.environ, ARM64_VC_ENV=str(self.fixture.env_file), PYTHONDONTWRITEBYTECODE="1")
        for name in ("D", "PYTHONPATH", "VC4_BUILD_NETWORK", "VC4_BUILD_NETWORK_APPROVAL"):
            environment.pop(name, None)
        result = subprocess.run(["python3", "-B", str(copied / "vc4_contract.py"), "network-intent"],
            env=environment, cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        intent = json.loads(result.stdout)
        self.assertEqual(intent["status"], "requires_manual_approval")
        self.assertEqual(intent["binding"]["data_root"], str(self.fixture.data_root))
        self.assertEqual(intent["binding"]["network_mode"], "default")

    def prepare_receipt(self):
        base = Path(self.config["B"])
        (base / "artifacts/ctx").mkdir(parents=True, exist_ok=True)
        (base / "artifacts/docker-build.log").write_text("隔离构建日志\n")
        pre_build = self.root / "pre-build.json"
        self.contract.write_once(pre_build, {"schema_version": self.contract.RESUME_SCHEMA, "stage": "pre-build",
            "inputs": {"build_network": self.contract.network_inputs(self.config)}})
        command = ["docker", "build", "--platform", "linux/arm64"]
        if self.config["VC4_BUILD_NETWORK"] != "default":
            command += ["--network=" + self.config["VC4_BUILD_NETWORK"]]
        command += [str(base / "artifacts/ctx")]
        return base, pre_build, command

    def test_success_binds_log_image_frozen_input_and_command(self):
        self.approve()
        base, before, command = self.prepare_receipt()
        image = "sha256:" + "a" * 64
        self.contract.record_network(base, self.config, before, exit_code=0, image_id=image,
            started_at=self.now.strftime("%Y-%m-%dT%H:%M:%SZ"), command=command)
        self.contract.verify_network(base, self.config, before, image)
        with self.assertRaisesRegex(ValueError, "镜像"):
            self.contract.verify_network(base, self.config, before, "sha256:" + "b" * 64)
        (base / "artifacts/docker-build.log").write_text("日志被改写\n")
        with self.assertRaisesRegex(ValueError, "日志"):
            self.contract.verify_network(base, self.config, before, image)

    def test_failed_build_records_failure_and_cannot_resume(self):
        base, before, command = self.prepare_receipt()
        receipt = self.contract.record_network(base, self.config, before, exit_code=7, image_id="",
            started_at=self.now.strftime("%Y-%m-%dT%H:%M:%SZ"), command=command)
        self.assertEqual((receipt["status"], receipt["exit_code"]), ("failed", 7))
        with self.assertRaisesRegex(ValueError, "收据"):
            self.contract.verify_network(base, self.config, before, "")

    def test_approval_expiring_during_build_is_recorded_as_failure(self):
        self.approve()
        base, before, command = self.prepare_receipt()
        receipt = self.contract.record_network(base, self.config, before, exit_code=0, image_id="sha256:" + "a" * 64,
            started_at=self.now.strftime("%Y-%m-%dT%H:%M:%SZ"), command=command, completed_at=self.now + timedelta(hours=1))
        self.assertEqual((receipt["status"], receipt["approval_window_respected"]), ("failed", False))
        with self.assertRaises(ValueError):
            self.contract.verify_network(base, self.config, before, "sha256:" + "a" * 64)

    def test_unapproved_or_duplicate_network_option_and_extra_entitlement_are_rejected(self):
        base, _, command = self.prepare_receipt()
        for flags in (["--network=host"], ["--network=none", "--network=host"], ["--allow=network.host"]):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                self.contract.check_command(command[:-1] + flags + command[-1:], "default", base)

    def test_tampered_prebuild_or_approval_cannot_produce_receipt(self):
        base, before, command = self.prepare_receipt()
        value = json.loads(before.read_text())
        value["inputs"]["build_network"]["binding"]["network_mode"] = "host"
        _write_json(before, value)
        with self.assertRaisesRegex(ValueError, "自摘要"):
            self.contract.record_network(base, self.config, before, exit_code=0, image_id="sha256:" + "a" * 64,
                started_at=self.now.strftime("%Y-%m-%dT%H:%M:%SZ"), command=command)
        self.assertFalse((base / "artifacts/build-network-receipt.json").exists())

    def test_real_build_step_runs_once_and_never_automatically_escalates_network(self):
        # 执行 build.sh 的原始 Docker 步骤；Docker 是隔离记录器，批准和收据合同使用真实实现。
        code = (SCRIPTS / "build.sh").read_text()
        step = code[code.index("NETWORK_ARGS=()") : code.index("docker image inspect --format")]
        for mode, exit_code in (("default", 7), ("host", 0)):
            with self.subTest(mode=mode):
                if mode == "host":
                    self.approve()
                base, before, _ = self.prepare_receipt()
                receipt_path = base / "artifacts/build-network-receipt.json"
                receipt_path.unlink(missing_ok=True)
                self.fixture.env_file.write_text("".join(f'{key}="{value}"\n' for key, value in self.config.items() if key in {*self.parser.REQUIRED_KEYS, *self.parser.OPTIONAL_KEYS}))
                binary = self.root / "bin"
                binary.mkdir(exist_ok=True)
                record = self.root / "docker-calls.jsonl"
                record.unlink(missing_ok=True)
                docker = binary / "docker"
                docker.write_text('#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n'
                    + f'path=Path({str(record)!r})\nwith path.open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                    + f'if sys.argv[1]=="build": print("隔离 BuildKit 网络记录"); raise SystemExit({exit_code})\n'
                    + 'print("sha256:"+"a"*64)\n')
                docker.chmod(0o700)
                exports = subprocess.check_output(["python3", "-B", str(SCRIPTS / "parse_env.py"), str(self.fixture.env_file)], text=True)
                script = "set -Eeuo pipefail\n" + exports + "\n" + \
                    f'export DRV={shlex.quote(str(SCRIPTS))}\nE={shlex.quote(str(before.parent))}\n' + \
                    'BASE_ARGS=(); CTX=$B/artifacts/ctx; C9=${C:0:9}; TAG=isolated:test; VERSION_LABEL=isolated\nutc_now(){ date -u +%Y-%m-%dT%H:%M:%SZ; }\n' + step
                # build.sh 固定读取 E/pre-build.json，本用例的文件也采用这一标准名。
                result = subprocess.run(["bash", "-c", script], env=dict(os.environ, ARM64_VC_ENV=str(self.fixture.env_file),
                    PATH=str(binary) + ":" + os.environ["PATH"], PYTHONPATH=str(REPO_ROOT), PYTHONDONTWRITEBYTECODE="1"),
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                commands = [json.loads(line) for line in record.read_text().splitlines()]
                builds = [args for args in commands if args[0] == "build"]
                self.assertEqual(len(builds), 1)
                self.assertEqual("--network=host" in builds[0], mode == "host")
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt["status"], "complete" if exit_code == 0 else "failed")
                # 下一种模式用全新冻结输入，历史失败收据仍留在测试日志中。
                before.unlink()


class TreePreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.template = Path(cls.temporary.name).resolve() / "history"
        _history_repo(cls.template, commits=10001)
        files = {".gitignore": "node_modules/\nbackend/vendor/\n", "backend/go.mod": "module fixture\n\ngo 1.20\n\nrequire example.invalid/local v0.0.0\nreplace example.invalid/local => ./localdep\n",
                 "backend/go.sum": "", "backend/main.go": 'package main\nimport _ "example.invalid/local"\nfunc main() {}\n',
                 "backend/localdep/go.mod": "module example.invalid/local\ngo 1.20\n", "backend/localdep/local.go": "package local\n",
                 "docs/egress/maintenance/x.json": "{}\n"}
        for name, content in files.items():
            path = cls.template / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        _git(cls.template, "add", "-A")
        _git(cls.template, "commit", "-q", "-m", "离线模块夹具")
        _git(cls.template, "branch", "codex/x")
        cls.commit = _git(cls.template, "rev-parse", "HEAD")
        cls.bundle = cls.template.parent / "candidate.bundle"
        _git(cls.template, "bundle", "create", str(cls.bundle), "--all")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def fixture(self, root):
        fixture = _DriverFixture(root)
        hist = fixture.data_root / "candidates/hist"
        _git(root, "clone", "-q", str(self.template), str(hist))
        previous = fixture.data_root / "candidates/prev/source/backend"
        shutil.copytree(hist / "backend", previous)
        # 子进程读取真实工具合同；所有落盘路径均在本用例的独立数据根中。
        shutil.rmtree(fixture.data_root / "tools")
        (fixture.data_root / "tools").symlink_to(REPO_ROOT / "tools", target_is_directory=True)
        drv = root / "driver"
        drv.mkdir()
        for name in ("lib.sh", "parse_env.py", "driver_config.py", "vc4_contract.py", "trees.sh", "build.sh"):
            shutil.copy2(SCRIPTS / name, drv / name)
        parser = load_script("parse_env")
        values = parser.parse(fixture.env_file.read_text())
        values.update(C=self.commit, DC=self.commit)
        fixture.env_file.write_text("".join(f'{key}="{value}"\n' for key, value in values.items()))
        shutil.copyfile(self.bundle, Path(values["BUNDLE"]))
        (fixture.candidate_dir / "original-marker").write_text("原树必须可回退\n")
        return fixture, hist, previous, drv, values

    def test_missing_vendor_is_regenerated_only_in_build_tree_and_old_tree_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, _, _, drv, values = self.fixture(root)
            result = _run(drv / "trees.sh", env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((fixture.candidate_dir / "tree-preparation.json").read_text())
            self.assertEqual(receipt["vendor_mode"], "regenerated_missing")
            self.assertTrue((fixture.candidate_dir / "build-tree/backend/vendor/modules.txt").is_file())
            for name in ("source", "gate-tree", "plan-source"):
                self.assertFalse((fixture.candidate_dir / name / "backend/vendor").exists())
                self.assertGreater(int(_git(fixture.candidate_dir / name, "rev-list", "--count", "HEAD")), 10000)
            previous = list(fixture.candidate_dir.parent.glob(fixture.cand + ".previous-*"))
            self.assertEqual(len(previous), 1)
            self.assertEqual((previous[0] / "original-marker").read_text(), "原树必须可回退\n")
            load_script("vc4_contract").verify_trees(fixture.candidate_dir, values)
            (fixture.candidate_dir / "source/README.md").write_text("树内容漂移\n")
            with self.assertRaisesRegex(ValueError, "不洁净"):
                load_script("vc4_contract").verify_trees(fixture.candidate_dir, values)

    def test_existing_vendor_with_identical_modules_is_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, _, source, drv, _ = self.fixture(root)
            subprocess.run(["go", "mod", "vendor"], cwd=source, check=True, capture_output=True, timeout=120)
            previous_vendor = fixture.data_root / "candidates/prev/build-tree/backend/vendor"
            shutil.copytree(source / "vendor", previous_vendor)
            result = _run(drv / "trees.sh", env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((fixture.candidate_dir / "tree-preparation.json").read_text())
            self.assertEqual(receipt["vendor_mode"], "copied_previous")
            self.assertEqual((fixture.candidate_dir / "build-tree/backend/vendor/modules.txt").read_bytes(), (previous_vendor / "modules.txt").read_bytes())

    def test_shallow_or_vendor_polluted_history_is_rejected_before_replacing_original(self):
        for kind in ("shallow", "vendor"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                fixture, hist, _, drv, _ = self.fixture(root)
                if kind == "shallow":
                    (hist / ".git/shallow").write_text(self.commit + "\n")
                else:
                    (hist / "backend/vendor").mkdir()
                result = _run(drv / "trees.sh", env=fixture.env, cwd=root)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("拒绝浅克隆" if kind == "shallow" else "不得含 backend/vendor", result.stderr)
                self.assertTrue((fixture.candidate_dir / "original-marker").is_file())
                self.assertEqual(list(fixture.candidate_dir.parent.glob(fixture.cand + ".staging-*")), [])
                self.assertFalse((fixture.candidate_dir / "tree-preparation.json").exists())

    def test_host_without_approval_stops_before_any_tree_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, _, _, drv, _ = self.fixture(root)
            fixture.env_file.write_text(fixture.env_file.read_text() + "VC4_BUILD_NETWORK=host\n")
            result = _run(drv / "trees.sh", env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("缺少专项批准", result.stderr)
            self.assertTrue((fixture.candidate_dir / "original-marker").is_file())
            self.assertEqual(list(fixture.candidate_dir.parent.glob(fixture.cand + ".staging-*")), [])

    def test_failed_vendor_regeneration_preserves_original_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, hist, _, drv, values = self.fixture(root)
            _git(hist, "checkout", "-q", "-B", "codex/x")
            (hist / "backend/localdep/local.go").unlink()
            _git(hist, "add", "-A")
            _git(hist, "commit", "-q", "-m", "本地模块缺失故障")
            commit = _git(hist, "rev-parse", "HEAD")
            values.update(C=commit, DC=commit)
            fixture.env_file.write_text("".join(f'{key}="{value}"\n' for key, value in values.items()))
            _git(hist, "bundle", "create", values["BUNDLE"], "--all")
            result = _run(drv / "trees.sh", env=fixture.env, cwd=root)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("no required module provides package", result.stderr)
            self.assertTrue((fixture.candidate_dir / "original-marker").is_file())
            self.assertEqual(list(fixture.candidate_dir.parent.glob(fixture.cand + ".staging-*")), [])


class SnapshotIndexMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.checker = cls.root / "snapshot-check"
        backend = REPO_ROOT / "backend"
        with tempfile.TemporaryDirectory(prefix=".snapshot-contract-test-", dir=backend) as directory:
            main = Path(directory) / "main.go"
            main.write_bytes((SCRIPTS / "snapshot_catalog_check.go").read_bytes())
            result = subprocess.run(["go", "build", "-mod=readonly", "-o", str(cls.checker), str(main)],
                cwd=backend, capture_output=True, text=True, timeout=300)
        if result.returncode:
            cls.temporary.cleanup()
            raise AssertionError(result.stdout + result.stderr)
        cls.catalog = load_script("catalog_chain")
        cls.testdata = backend / "internal/officialegress/profilecontract/testdata"
        cls.entries = json.loads((cls.testdata / "snapshot-catalog.json").read_text())["snapshots"]

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def validate(self, repository, checks):
        result = subprocess.run([str(self.checker)], input=json.dumps(checks), capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError(result.stderr)
        return {**json.loads(result.stdout), "checker_sha256": hashlib.sha256((SCRIPTS / "snapshot_catalog_check.go").read_bytes()).hexdigest()}

    def fixture(self, root):
        source, destination = root / "source", root / "destination"
        previous, staged = self.entries[:2], self.entries[-1:]
        for base, rows in ((source, staged), (destination, previous)):
            _write_json(base / self.catalog.SNAPSHOT_INDEX, {"schema_version": 1, "snapshots": rows})
            for row in rows:
                path = base / "profilecontract/testdata" / row["file"]
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.testdata / row["file"], path)
        return source, destination, previous, staged

    def test_new_version_preserves_all_history_and_repeated_merge_has_identical_bytes(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(self.catalog, "validate_snapshot_contract", side_effect=self.validate):
            root = Path(directory).resolve()
            source, destination, previous, staged = self.fixture(root)
            before = {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*.json")}
            data, receipt = self.catalog.merge_snapshot_index(source, destination, REPO_ROOT)
            self.assertEqual(len(json.loads(data)["snapshots"]), len(previous) + len(staged))
            self.assertEqual(json.loads(data)["snapshots"][:len(previous)], previous)
            self.assertEqual(receipt["contract_verification"]["catalog_count"], 3)
            self.assertEqual(before, {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*.json")})
            (destination / self.catalog.SNAPSHOT_INDEX).write_bytes(data)
            row = staged[0]
            path = destination / "profilecontract/testdata" / row["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / "profilecontract/testdata" / row["file"], path)
            repeated, _ = self.catalog.merge_snapshot_index(source, destination, REPO_ROOT)
            self.assertEqual(repeated, data)

    def test_missing_blob_fake_digest_bad_path_and_unknown_field_are_rejected_before_write(self):
        for kind in ("missing", "digest", "path", "field"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, mock.patch.object(self.catalog, "validate_snapshot_contract", side_effect=self.validate):
                root = Path(directory).resolve()
                source, destination, _, staged = self.fixture(root)
                original = (destination / self.catalog.SNAPSHOT_INDEX).read_bytes()
                blob = source / "profilecontract/testdata" / staged[0]["file"]
                if kind == "missing":
                    blob.unlink()
                elif kind == "digest":
                    value = json.loads(blob.read_text())
                    value["Version"] = "9.9.9"
                    _write_json(blob, value)
                else:
                    value = json.loads((source / self.catalog.SNAPSHOT_INDEX).read_text())
                    if kind == "path":
                        value["snapshots"][0]["file"] = "../outside.json"
                    else:
                        value["snapshots"][0]["extra"] = True
                    _write_json(source / self.catalog.SNAPSHOT_INDEX, value)
                with self.assertRaises(ValueError):
                    self.catalog.merge_snapshot_index(source, destination, REPO_ROOT)
                self.assertEqual((destination / self.catalog.SNAPSHOT_INDEX).read_bytes(), original)

    def test_duplicate_coordinate_and_conflicting_blob_hash_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(self.catalog, "validate_snapshot_contract", side_effect=self.validate):
            root = Path(directory).resolve()
            source, destination, previous, _ = self.fixture(root)
            _write_json(source / self.catalog.SNAPSHOT_INDEX, {"schema_version": 1, "snapshots": [previous[0], previous[0]]})
            with self.assertRaisesRegex(ValueError, "重复坐标"):
                self.catalog.merge_snapshot_index(source, destination, REPO_ROOT)
            conflicting = dict(previous[0], blob_sha256="0" * 64)
            old = dict(previous[0], blob_sha256="1" * 64)
            _write_json(destination / self.catalog.SNAPSHOT_INDEX, {"schema_version": 1, "snapshots": [old]})
            _write_json(source / self.catalog.SNAPSHOT_INDEX, {"schema_version": 1, "snapshots": [conflicting]})
            with self.assertRaisesRegex(ValueError, "冲突"):
                self.catalog.merge_snapshot_index(source, destination, REPO_ROOT)


if __name__ == "__main__":
    unittest.main()
