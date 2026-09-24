"""R12 驱动等待与续跑合同：真实子进程、实际文件／Git 树，网络和镜像查询使用零请求替身。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_vc_receipt as receipts
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests import test_codex_upgrade_candidate_build as build_tests

SCRIPTS = driver_tests.SCRIPTS


def load(name):
    spec = importlib.util.spec_from_file_location("r12_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resume = load("vc4_resume")


class DriverWaitTests(unittest.TestCase):
    def _wait(self, root, *args, timeout=6):
        return subprocess.run([sys.executable, str(SCRIPTS / "wait_state.py"), *map(str, args)],
                              cwd=root, capture_output=True, text=True, timeout=timeout)

    def test_success_marker_total_timeout_and_tail_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "child.log"
            log.write_text("".join(f"L{i}\n" for i in range(250)))
            start = time.monotonic()
            failed = self._wait(root, "marker", log, "--regex", "^DONE$", "--max-seconds", ".2")
            self.assertEqual(failed.returncode, 3)
            self.assertLess(time.monotonic() - start, 2)
            self.assertNotIn("\nL49\n", failed.stderr)
            self.assertIn("\nL50\n", failed.stderr)
            log.write_text(log.read_text() + "DONE\n")
            self.assertEqual(self._wait(root, "marker", log, "--regex", "^DONE$", "--max-seconds", ".2").returncode, 0)

    def test_dead_peer_is_detected_while_primary_is_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = subprocess.Popen(["sleep", "30"])
            dead = subprocess.Popen(["true"])
            dead.wait()
            try:
                result = self._wait(root, "marker", root / "primary", "--regex", "DONE", "--pid", live.pid,
                    "--peer-log", root / "peer", "--peer-pid", dead.pid, "--peer-regex", "DONE", "--max-seconds", "2")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("并行子进程已退出", result.stderr)
            finally:
                live.terminate()
                live.wait()

    def test_upload_stale_missing_and_total_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            heart = root / "HEARTBEAT"
            for kind in ("missing", "stale", "fresh"):
                with self.subTest(kind=kind):
                    if kind != "missing":
                        heart.touch()
                        if kind == "stale":
                            os.utime(heart, (time.time()-10, time.time()-10))
                    result = self._wait(root, "heartbeat", heart, "--stale-seconds", ".3", "--max-seconds", ".15")
                    self.assertEqual(result.returncode, 3)
            (root / "READY").touch()
            self.assertEqual(self._wait(root, "heartbeat", heart, "--stale-seconds", ".1", "--max-seconds", ".2").returncode, 0)

    def test_actual_run_pid_and_supervisor_heartbeat_both_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = subprocess.Popen(["sleep", "30"])
            try:
                pid = root / "run.pid"
                pid.write_text(str(child.pid))
                log = root / "run.log"
                state = root / "supervisor/run-fixture"
                state.mkdir(parents=True)
                payload = {"started_at_epoch": time.time(), "owner_pid": child.pid, "owner_nonce": "fixture"}
                (state / "state.json").write_text(json.dumps(payload))
                heart = {"schema_version": "codex-upgrade-supervisor-heartbeat/v1", "owner_pid": child.pid,
                         "owner_nonce": "fixture", "state": "running", "updated_at_epoch": time.time()-10}
                (state / "heartbeat.json").write_text(json.dumps(heart))
                args = ("marker", log, "--regex", "DONE", "--pid-file", pid, "--supervisor-root", state.parent,
                        "--startup-seconds", ".1", "--stale-seconds", ".1", "--max-seconds", "2")
                failed = self._wait(root, *args)
                self.assertEqual(failed.returncode, 3, failed.stderr)
                self.assertIn("监督器心跳", failed.stderr)
                heart["updated_at_epoch"] = time.time()
                (state / "heartbeat.json").write_text(json.dumps(heart))
                child.terminate()
                child.wait()
                failed = self._wait(root, *args)
                self.assertEqual(failed.returncode, 3, failed.stderr)
                self.assertIn("子进程已退出", failed.stderr)
                log.write_text("DONE\n")
                self.assertEqual(self._wait(root, *args).returncode, 0)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait()


class LibWaitForMarkerArgumentTests(unittest.TestCase):
    """lib.sh 的 wait_for_marker：PID 位置参数可选，省略时选项原样交给 wait_state.py。"""

    def _probe(self, root: Path) -> tuple[Path, dict[str, str]]:
        fixture = driver_tests._DriverFixture(root)
        drv = root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "wait_state.py"):
            (drv / name).write_bytes((SCRIPTS / name).read_bytes())
        probe = drv / "probe.sh"
        probe.write_text(
            "#!/bin/bash\nset -Eeuo pipefail\n"
            "source \"$(dirname \"${BASH_SOURCE[0]}\")/lib.sh\"\n"
            "wait_for_marker \"$@\"\necho WAIT_OK\n",
            encoding="utf-8",
        )
        return probe, fixture.env

    def test_pid_is_optional_and_options_are_passed_through(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            probe, env = self._probe(root)
            done = root / "done.log"
            done.write_text("DONE\n")
            extra = root / "extra.log"
            extra.write_text("EXTRA-TAIL\n")
            pending = root / "pending.log"
            pending.write_text("still running\n")
            # 省略 PID 直接跟 --log：标记已出现即成功，选项不会被当成 PID。
            ok = driver_tests._run(probe, str(done), "^DONE$", "5", "--log", str(extra), env=env, cwd=root)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("WAIT_OK", ok.stdout)
            # 省略 PID 且标记未出现：总时限到期退出 3，--log 指定文件的日志尾被打印，证明选项确实交给了等待器。
            late = driver_tests._run(probe, str(pending), "^DONE$", ".3", "--log", str(extra), env=env, cwd=root)
            self.assertEqual(late.returncode, 3, late.stdout + late.stderr)
            self.assertIn("等待超过总时限", late.stderr)
            self.assertIn("EXTRA-TAIL", late.stderr)
            self.assertNotIn("WAIT_OK", late.stdout)
            # 空串 PID（vc5-all.sh 的用法）：不绑定 PID，后续选项照常生效。
            empty = driver_tests._run(probe, str(done), "^DONE$", "5", "", "--log", str(extra), env=env, cwd=root)
            self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)

    def test_positional_pid_binds_child_and_invalid_pid_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            probe, env = self._probe(root)
            pending = root / "pending.log"
            pending.write_text("still running\n")
            dead = subprocess.Popen(["true"])
            dead.wait()
            exited = driver_tests._run(probe, str(pending), "^DONE$", "5", str(dead.pid), env=env, cwd=root)
            self.assertEqual(exited.returncode, 3, exited.stdout + exited.stderr)
            self.assertIn("子进程已退出", exited.stderr)
            for invalid in ("abc", "0", "-5", "12x"):
                with self.subTest(pid=invalid):
                    result = driver_tests._run(probe, str(pending), "^DONE$", "5", invalid, env=env, cwd=root)
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertIn("PID 必须是正整数", result.stderr)
                    self.assertNotIn("WAIT_OK", result.stdout)


class UploadHeartbeatTests(unittest.TestCase):
    def test_real_upload_loop_pulses_and_stops_after_ready(self):
        """SSH 替身只把流解到私有临时目录；tar、周期心跳与退出清理执行实际脚本。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, remote, tools = root / "source", root / "remote", root / "bin"
            (source / "impl-logs/cross-check").mkdir(parents=True)
            (source / "local-gates").mkdir()
            (source / "impl-logs/check-egress-spec.log").write_text("spec\n")
            (source / "impl-logs/cross-check/check-egress-spec.C-only.local.log").write_text("cross\n")
            tools.mkdir()
            calls = root / "ssh.log"
            (tools / "ssh").write_text(
                f"#!{sys.executable}\nimport subprocess,sys,time\n"
                f"with open({str(calls)!r},'a') as stream: stream.write(sys.argv[-1]+'\\n')\n"
                "command=sys.argv[-1].split(' && stat -c')[0]\n"
                "if 'tar -xf -' in command: time.sleep(.5)\n"
                "raise SystemExit(subprocess.call(['bash','-c',command]))\n")
            (tools / "sleep").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(.03)\n")
            (tools / "chown").write_text("#!/bin/sh\nexit 0\n")
            for path in tools.iterdir():
                path.chmod(0o755)
            result = subprocess.run(["bash", str(SCRIPTS / "local/local-upload.sh"), str(source), str(remote)],
                env={**os.environ, "PATH": str(tools) + ":" + os.environ["PATH"]},
                capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pulses = [line for line in calls.read_text().splitlines() if line.startswith("touch ")]
            self.assertGreaterEqual(len(pulses), 2)
            self.assertTrue((remote / "impl-logs/READY").is_file())
            snapshot = calls.read_bytes()
            time.sleep(.15)
            self.assertEqual(snapshot, calls.read_bytes(), "上传结束后必须停止后台心跳")
            uploader = load("upload_manifest")
            self.assertEqual(json.loads((remote / "impl-logs/upload-manifest.json").read_text()), uploader.manifest(remote))


class ResumeInputTests(unittest.TestCase):
    """只替换 Campaign 读取与镜像工具查询；真实摘要、严格装配和统一收据 producer 不打桩。"""

    def setUp(self):
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
        case = build_tests.CandidateBuildReceiptTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case
        root = case.root.resolve()
        self.root = root
        (root / "artifacts").mkdir()
        case.context.rename(root / "artifacts/ctx")
        case.context = root / "artifacts/ctx"
        case.binary.rename(root / "artifacts/sub2api")
        case.binary = root / "artifacts/sub2api"
        case.dist_source.rename(root / "frontend-dist")
        case.dist_source = root / "frontend-dist"
        source = case.source
        for name in ("backend", "backend/resources"):
            (source / name).mkdir(exist_ok=True)
        (source / ".gitignore").write_text("backend/vendor/\nbackend/internal/web/dist/\n")
        (source / "backend/go.mod").write_text("module fixture\n")
        (source / "backend/go.sum").write_text("fixture sum\n")
        (source / "backend/resources/models.json").write_text("{}\n")
        (source / "Dockerfile.goreleaser").write_text("ARG ALPINE_IMAGE=alpine:3.21\nARG POSTGRES_IMAGE=postgres:18-alpine\nFROM ${ALPINE_IMAGE}\n")
        for path in source.rglob("*"):
            path.chmod(0o755 if path.is_dir() or path.suffix == ".sh" else 0o644)
        self.git("init", "-q", cwd=source)
        self.git("-c", "user.name=测试", "-c", "user.email=test@example.invalid", "add", ".", cwd=source)
        self.git("-c", "user.name=测试", "-c", "user.email=test@example.invalid", "commit", "-qm", "夹具源码", cwd=source)
        self.commit = self.git("rev-parse", "HEAD", cwd=source)
        for name in ("build-tree", "gate-tree", "plan-source"):
            tree = root / name
            if tree.exists():
                shutil.rmtree(tree)
            self.git("clone", "-q", str(source), str(tree))
        vendor = root / "build-tree/backend/vendor"
        vendor.mkdir()
        (vendor / "modules.txt").write_text("fixture vendor\n")
        shutil.copytree(case.dist_source, root / "build-tree/backend/internal/web/dist")
        case._copy_file(source / "Dockerfile.goreleaser", case.context / "Dockerfile")
        case._copy_file(source / "backend/resources/models.json", case.context / "backend/resources/models.json")
        self.env = {"B": str(root), "C": self.commit, "CAND": build_tests.CANDIDATE_ID, "UP": "fixture-upgrade",
                    "FRONTEND_DEVIATION_APPROVED_BY": "测试授权"}
        env_patch = mock.patch.dict(os.environ, self.env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.manifest = {"campaign_id": "fixture-campaign", "campaign_purpose": "production_replacement",
                         "baseline_version": "0.151.0", "target_version": "0.154.0"}
        requirements = {"requirements": [{"kind": "affected_rule", "gate_id": "affected-rule"}] +
                        [{"kind": "public", "gate_id": "public-" + str(index)} for index in range(4)]}
        self.requirements = requirements
        context_patch = mock.patch.object(resume, "context", return_value=(root, root / "campaign", self.manifest, requirements))
        context_patch.start()
        self.addCleanup(context_patch.stop)
        self.node_version = "v20.19.0"
        self.go_version = "go version go1.26.0 linux/arm64"
        self.base_digest = "7"*64
        real_run = resume.run
        def command(*args, **kwargs):
            if args[0] == "docker":
                if args[1:3] == ("image", "inspect"):
                    return json.dumps([{"Id": build_tests.IMAGE_ID, "Os": "linux", "Architecture": "arm64",
                                        "RepoDigests": ["fixture@sha256:" + self.base_digest]}])
                return self.node_version
            if args[0] == "go":
                return self.go_version
            return real_run(*args, **kwargs)
        run_patch = mock.patch.object(resume, "run", side_effect=command)
        run_patch.start()
        self.addCleanup(run_patch.stop)
        # 使用既有严格构建夹具，改为本次真实 Git commit，再按正式 producer 生成前端证明。
        commit_patch = mock.patch.object(build_tests, "COMMIT", self.commit)
        commit_patch.start()
        self.addCleanup(commit_patch.stop)
        case._write_builder_receipt()
        case.parameters = case._parameters()
        self.node_version = case.parameters["frontend"]["node_version"]
        driver_tests._write_json(root / "artifacts/build-parameters.json", case.parameters)
        (root / "artifacts/source-transition.json").write_text("{}\n")
        (root / "artifacts/built-at-utc.txt").write_text("2026-09-24T00:00:00Z\n")
        self.evidence = root / "evidence"
        self.evidence.mkdir(mode=0o700)
        (self.evidence / "logs").mkdir()
        self.current = resume.inputs()
        tree = self.current["tree_sha256"]["source"]
        (self.evidence / "logs/implementation.log").write_text(
            f"commit={self.commit}\ngate_tree_sha256={tree}\n" + "exit_code=0\n"*5 +
            f"gate_tree_sha256_after={tree}\nGATES_DONE now\n")

    @staticmethod
    def git(*args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def checkpoint(self):
        resume.write_once(self.evidence / "upload-wait.json", {"schema_version": resume.SCHEMA, "stage": "upload-wait",
                          "inputs": self.current, "outputs": resume.outputs(self.evidence, self.current)})

    def full_receipt(self):
        gates = []
        for name, kind, command in [("affected-rule", "affected", ["go", "test", "./fixture"]),
                                     ("check-egress-spec", "public", ["make", "check-egress-spec"])]:
            gates.append({"gate_id": name, "kind": kind, "command": command, "exit_code": 0,
                          "passed": 1, "failed": 0, "approved_skip": 0, "unexpected_skip": 0})
        (self.evidence / "logs/spec.log").write_text("exit_code=0\n")
        facts = {"schema_version": receipts.FACTS_SCHEMA, "kind": "implementation_tests",
                 "subject": self.current["subject"], "assertions": {"git_commit": self.commit,
                 "source_tree_sha256": self.current["tree_sha256"]["source"], "target_architecture": "linux/arm64", "gates": gates},
                 "evidence": [{"role": "check_egress_spec", "path": "logs/spec.log"},
                              {"role": "implementation_tests", "path": "logs/implementation.log"}]}
        driver_tests._write_json(self.evidence / "facts.json", facts)
        receipts.finalize(self.evidence, "facts.json", "receipt.json")

    def test_upload_checkpoint_without_final_receipt_and_full_replay(self):
        self.checkpoint()
        before = {path: path.read_bytes() for path in self.evidence.rglob("*") if path.is_file()}
        resume.verify(self.evidence, "upload-wait")
        with self.assertRaises((OSError, receipts.VCReceiptError)):
            resume.verify(self.evidence, "full")
        self.full_receipt()
        resume.verify(self.evidence, "full")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_changed_toolchain_dependency_source_and_affected_reject_reuse(self):
        self.checkpoint()
        for field in ("go", "node", "base", "vendor", "sum", "source", "affected"):
            with self.subTest(field=field):
                if field in {"go", "node", "base"}:
                    attr = "base_digest" if field == "base" else field + "_version"
                    value = getattr(self, attr)
                    setattr(self, attr, value + "-changed")
                    try:
                        with self.assertRaisesRegex(ValueError, "构建输入漂移"):
                            resume.verify(self.evidence, "upload-wait")
                    finally:
                        setattr(self, attr, value)
                elif field == "affected":
                    self.requirements["requirements"].append({"kind": "affected_rule", "gate_id": "new-rule"})
                    with self.assertRaisesRegex(ValueError, "构建输入漂移"):
                        resume.verify(self.evidence, "upload-wait")
                    self.requirements["requirements"].pop()
                else:
                    path = self.root / {"vendor": "build-tree/backend/vendor/modules.txt", "sum": "source/backend/go.sum",
                                        "source": "source/frontend/package.json"}[field]
                    original = path.read_bytes()
                    try:
                        path.write_bytes(original + b"drift\n")
                        with self.assertRaises(ValueError):
                            resume.verify(self.evidence, "upload-wait")
                    finally:
                        path.write_bytes(original)

    def test_plan_generated_output_must_equal_approved_plan(self):
        source = self.root / "source"
        plan = source / "docs/egress/lifecycle/fixture/gate-plan.json"
        plan.parent.mkdir(parents=True)
        plan.write_text('{"plan":1}\n')
        self.git("add", ".", cwd=source)
        self.git("-c", "user.name=测试", "-c", "user.email=test@example.invalid", "commit", "-qm", "批准门禁计划", cwd=source)
        plan_root = self.root / "plan-source"
        self.git("fetch", "-q", str(source), "HEAD", cwd=plan_root)
        self.git("checkout", "-q", "FETCH_HEAD", cwd=plan_root)
        expected = resume.source_digest(plan_root, plan=True)
        generated = plan_root / "docs/egress/lifecycle/fixture/gate-plan-dispatch.json"
        generated.write_bytes(plan.read_bytes())
        self.assertEqual(resume.source_digest(plan_root, plan=True), expected)
        generated.write_text('{"plan":2}\n')
        with self.assertRaisesRegex(ValueError, "批准计划不同"):
            resume.source_digest(plan_root, plan=True)

    def test_changed_build_parameters_binary_and_log_reject_resume(self):
        self.checkpoint()
        for path in (self.root / "artifacts/build-parameters.json", self.root / "artifacts/sub2api",
                     self.evidence / "logs/implementation.log"):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                try:
                    if path.suffix == ".json":
                        value = json.loads(original)
                        value["go_build"]["environment"]["CGO_ENABLED"] = "1"
                        path.write_text(json.dumps(value))
                    else:
                        path.write_bytes(original + b"drift\n")
                    with self.assertRaises((ValueError, resume.build.CandidateBuildError)):
                        resume.verify(self.evidence, "upload-wait")
                finally:
                    path.write_bytes(original)

    def test_missing_or_tampered_old_checkpoint_never_implies_reuse(self):
        with self.assertRaisesRegex(ValueError, "缺少"):
            resume.verify(self.evidence, "upload-wait")
        self.checkpoint()
        checkpoint = self.evidence / "upload-wait.json"
        value = json.loads(checkpoint.read_text())
        value["inputs"]["git_commit"] = "f"*40
        checkpoint.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "摘要"):
            resume.verify(self.evidence, "upload-wait")


if __name__ == "__main__":
    unittest.main()
