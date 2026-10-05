"""A-04 人工批准、零写入预检、打包及中断恢复的隔离验收。"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.official_client_capture.tests.test_arm64_driver_fix_and_continue import _Round, load_helper, _write_json


class FixSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = _Round(self.root)
        self.helper = load_helper()
        self.safety = self.helper.fix_safety
        self.params = self.helper.load_params(self.fixture.params_path)

    def test_dry_run_is_readonly_and_does_not_run_managed_commands(self):
        before = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")}
        result = self.fixture.run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["package"]["action"], "verified")
        self.assertEqual(before, {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")})
        self.assertEqual(self.fixture.calls(), [])

    def test_missing_deploy_approval_stops_before_deployment_and_resume_works(self):
        self.fixture.provide_approvals = False
        result = self.fixture.run()
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertEqual(self.fixture.calls(), [])
        self.assertTrue(list((self.fixture.out / "approval-intents").glob("*.json")))
        self.fixture.approve_fixture_operations()
        resumed = self.fixture.run("--from", "deploy")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.fixture.call_keys().count("arm64_supervised_deploy"), 1)

    def test_scope_subject_expiry_mode_and_identity_mismatch_refuse_approval(self):
        self.fixture.approve_fixture_operations()
        binding = self.safety.approval_binding(self.params, "deploy", self.fixture.head)
        path = self.safety.approval_directory(self.params) / (self.safety.digest(binding) + ".json")
        original = self.safety.read(path)
        for field, value in (("approved_by", "错误批准人"), ("status", "draft"), ("proof_ref", ""),
                             ("expires_at_utc", "2000-01-01T00:00:00Z"), ("review_sha256", "f" * 64)):
            _write_json(path, {**original, field: value})
            with self.assertRaises(self.safety.SafetyError):
                self.safety.require_approval(self.params, "deploy", self.fixture.head)
        _write_json(path, original)
        path.chmod(0o644)
        with self.assertRaises(self.safety.SafetyError):
            self.safety.require_approval(self.params, "deploy", self.fixture.head)
        path.chmod(0o600)
        with self.assertRaises(self.safety.SafetyError):
            self.safety.require_approval(self.params, "evolution", self.fixture.head)
        with self.assertRaises(self.safety.SafetyError):
            self.safety.require_approval(self.params, "deploy", "b" * 40)
        changed = {**self.params, "D": str(self.root / "different-data")}
        with self.assertRaises(self.safety.SafetyError):
            self.safety.require_approval(changed, "deploy", self.fixture.head)

    def test_approval_expiry_boundary_and_file_drift_are_rejected(self):
        self.fixture.approve_fixture_operations()
        binding = self.safety.approval_binding(self.params, "deploy", self.fixture.head)
        path = self.safety.approval_directory(self.params) / (self.safety.digest(binding) + ".json")
        value = self.safety.read(path)
        expires = datetime.fromisoformat(value["expires_at_utc"].replace("Z", "+00:00"))
        self.safety.require_approval(self.params, "deploy", self.fixture.head, now=expires-timedelta(seconds=1))
        with self.assertRaises(self.safety.SafetyError):
            self.safety.require_approval(self.params, "deploy", self.fixture.head, now=expires)
        self.fixture.entry_path.write_text(self.fixture.entry_path.read_text()+"\n")
        with self.assertRaises(self.safety.SafetyError):
            self.safety.require_approval(self.params, "deploy", self.fixture.head)

    def test_package_creation_is_atomic_bound_and_repeated_readonly(self):
        params = {**self.params, "SRC": str(self.root / "repo"), "BUNDLE": str(self.root / "new-upload/new.bundle")}
        plan = self.safety.package(params, apply=False)
        self.assertEqual(plan["action"], "package_required")
        self.assertFalse(Path(params["BUNDLE"]).parent.exists())
        first = self.safety.package(params, apply=True)
        before = {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*")}
        self.assertEqual(self.safety.package(params, apply=True), first)
        self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*")})
        with self.assertRaises(self.safety.SafetyError):
            self.safety.package({**params, "HEAD_COMMIT": "b" * 40}, apply=True)

    def test_interrupted_dispatch_is_not_automatically_repeated(self):
        first = self.fixture.run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        # 模拟进程已启动后、完成步骤收据写入前被打断；占位必须留存。
        (self.fixture.out / "recover.json").unlink()
        before = self.fixture.call_keys().count("vc5-recover.sh")
        again = self.fixture.run("--from", "recover")
        self.assertEqual(again.returncode, 4, again.stdout + again.stderr)
        self.assertIn("禁止自动重派", again.stderr)
        self.assertEqual(self.fixture.call_keys().count("vc5-recover.sh"), before)

    def test_recovery_authorization_requires_its_own_human_approval(self):
        self.fixture.approve_fixture_operations()
        binding = self.safety.approval_binding(self.params, "recovery-authorize", "a" * 64)
        path = self.safety.approval_directory(self.params) / (self.safety.digest(binding) + ".json")
        path.unlink()
        self.fixture.provide_approvals = False
        result = self.fixture.run()
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertEqual(self.fixture.step("authorize")["status"], "needs-operator")
        self.assertTrue(any("reconcile-attempt:approve:" in key for key in self.fixture.call_keys()))
        self.assertFalse(any("reconcile-attempt:authorize:" in key for key in self.fixture.call_keys()))
        self.assertNotIn("vc5-recover.sh", self.fixture.call_keys())

    def test_failed_deploy_keeps_inflight_receipt_and_never_relaunches_implicitly(self):
        from types import SimpleNamespace
        log = self.root / "deploy-failed.log"
        log.write_text("exit=1\n")
        inflight = self.fixture.out / "deploy.inflight.json"
        _write_json(inflight, {"log": str(log), "pid_file": str(self.root / "missing.pid"), "started_at_utc": "2026-10-05T00:00:00Z"})
        result, _ = self.helper._deploy_verify(self.params, SimpleNamespace(run_stamp="isolated", log=str(log)))
        self.assertEqual(result, 1)
        self.assertTrue(inflight.exists())
        result, exports = self.helper._deploy_state(self.params, SimpleNamespace(run_stamp="isolated-next"))
        self.assertEqual(exports["ACTION"], "verify")

    def test_step_receipts_preserve_history_and_refuse_round_identity_rebinding(self):
        first = self.fixture.run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        receipts = {p.name: p.read_bytes() for p in (self.fixture.out / "receipts").glob("*.json")}
        self.assertTrue(receipts)
        self.fixture.write_params({"FIX_COMMIT": "b" * 40})
        second = self.fixture.run()
        self.assertEqual(second.returncode, 4, second.stdout + second.stderr)
        self.assertTrue(all((self.fixture.out / "receipts" / name).read_bytes()==data for name,data in receipts.items()))

    def test_round_lock_refuses_concurrent_start_and_releases_after_process_death(self):
        self.fixture.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = self.fixture.out / ".round.lock"
        code = ('exec 9>>"$1"\npython3 -B "$2" 9 "$1" "$3" || exit $?\n'
                'printf "locked\\n"\nread -r unused\n')
        holder = subprocess.Popen(["bash", "-c", code, "lock-fixture", str(lock), self.safety.__file__,
                                   str(self.fixture.out / ".lock")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            refused = self.fixture.run()
            self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
            self.assertEqual(self.fixture.calls(), [])
        finally:
            holder.kill()
            holder.communicate(timeout=10)
        resumed = self.fixture.run()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.fixture.call_keys().count("vc5-recover.sh"), 1)

    def test_round_lock_rejects_symlink_without_changing_its_target(self):
        self.fixture.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.root / "protected"
        target.write_bytes(b"keep-original")
        (self.fixture.out / ".round.lock").symlink_to(target)
        result = self.fixture.run()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(target.read_bytes(), b"keep-original")
        self.assertEqual(self.fixture.calls(), [])

    def test_deploy_launch_never_overwrites_an_uncertain_previous_launch(self):
        from types import SimpleNamespace
        args = SimpleNamespace(log=str(self.root / "first.log"), pid_file=str(self.root / "first.pid"), run_stamp="first")
        self.helper._deploy_launch(self.params, args)
        path = self.fixture.out / "deploy.inflight.json"
        original = path.read_bytes()
        with self.assertRaises(self.safety.SafetyError):
            self.helper._deploy_launch(self.params, SimpleNamespace(log=str(self.root / "second.log"),
                pid_file=str(self.root / "second.pid"), run_stamp="second"))
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
