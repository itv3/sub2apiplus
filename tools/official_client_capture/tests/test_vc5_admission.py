"""A-01 隔离验收：不连接 Docker、数据库、上游或任何生产 Campaign。"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import runpy
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.arm64_capture_driver.driver import vc5_admission as admission
from tools.arm64_capture_driver.driver import parse_env
from tools.official_client_capture.tests.test_arm64_capture_driver import SCRIPTS, _DriverFixture, _run


def token(*, claims=None, header=None, secret="隔离验收签名密钥"):
    now = int(time.time())
    payload = {"user_id": 1, "email": "admin@fixture.invalid", "role": "admin", "token_version": 0,
               "iat": now - 60, "nbf": now - 60, "exp": now + 86400}
    if claims is not None:
        payload = claims
    parts = [base64.urlsafe_b64encode(admission.canonical(value)).rstrip(b"=")
             for value in (header or {"alg": "HS256", "typ": "JWT"}, payload)]
    signed = b".".join(parts)
    return signed + b"." + base64.urlsafe_b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).rstrip(b"=")


class Facts:
    """仅在测试进程注入；正式命令行和环境无法选择此替身。"""

    def __init__(self, profile):
        self.profile_bytes = admission.canonical({"Digest": "d" * 64, "Version": "9.1.0"})
        self.profile = profile
        self.issued = 0
        self.fail_after_issue = False
        self.failure = None
        self.generation = 1
        self.container_drift = False

    def catalog(self):
        return {"profile_bytes": self.profile_bytes, "profile_sha256": hashlib.sha256(self.profile_bytes).hexdigest(),
                "profile_digest": "d" * 64, "profile_id": "fixture-profile", "vc3_receipt_sha256": "3" * 64,
                "build_receipt_sha256": "4" * 64, "build_id": "fixture-build", "image_id": "sha256:" + "a" * 64}

    def state(self):
        if self.failure or (self.fail_after_issue and self.issued):
            raise admission.AdmissionError(self.failure or "background_validation_not_passed")
        return {"deployment": self.generation, "background_validation": "passed"}

    def container(self):
        return {"container_id": "fixture-container", "image_id": "fixture-image", "mount_destination": "/capture"}

    def container_profile(self, path):
        if not path.exists():
            return None
        return {"symlink": False, "uid": os.geteuid(), "mode": stat.S_IMODE(path.stat().st_mode),
                "sha256": "f" * 64 if self.container_drift else hashlib.sha256(path.read_bytes()).hexdigest()}

    def issue_token(self, signing):
        self.issued += 1
        return token()


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        compose = self.root / "compose"
        compose.mkdir()
        admission.atomic_write(compose / ".env", 'JWT_SECRET="隔离验收签名密钥"\nADMIN_EMAIL=admin@fixture.invalid\nPOSTGRES_USER=fixture\nPOSTGRES_PASSWORD=fixture\nPOSTGRES_DB=fixture\n'.encode())
        signer = self.root / "signer"
        admission.atomic_write(signer, b"#!/bin/sh\nexit 1\n", 0o700)
        self.config = {"D": str(self.root / "data"), "RUNROOT": str(self.root / "round"), "NEW": "fixture-campaign",
                       "CAND": "fixture-candidate", "UP": "fixture", "TARGET_VERSION": "9.1.0", "PROFILE_ID": "fixture-profile",
                       "PROFILE_DIGEST": "b" * 64, "EVIDENCE_DECISION": "recapture", "OFFICIAL_CAMPAIGN": "old-campaign",
                       "B": str(self.root / "candidate"), "COMPOSE_DIR": str(compose), "JWTGEN_BIN": str(signer)}
        profile = self.root / "data/runtime/codex-profile-9.1.0.json"
        self.facts = Facts(profile)
        self.subject = admission.Admission(self.config, self.facts)

    def approval(self, **changes):
        report = self.subject.inspect()
        now = datetime.now(timezone.utc)
        value = {"schema_version": admission.APPROVAL_SCHEMA, "approved_by": "隔离验收操作者",
                 "approval_id": "fixture-approval", "approved_at_utc": (now - timedelta(minutes=1)).isoformat(),
                 "expires_at_utc": (now + timedelta(minutes=10)).isoformat(), "admission_key": report["admission_key"],
                 "actions": report["actions"], "seal_admission": True}
        value.update(changes)
        path = self.root / "approval.json"
        admission.write_json(path, value)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode))
                for p in self.root.rglob("*") if p.is_file()}

    def healthy(self, mode=0o600):
        admission.atomic_write(self.subject.profile, self.facts.profile_bytes)
        admission.atomic_write(self.subject.token, token(), mode)

    def test_dry_run_missing_assets_has_no_writes_or_issuance(self):
        before = self.snapshot()
        report = self.subject.inspect()
        self.assertEqual(report["status"], "needs_apply")
        self.assertEqual(report["actions"], ["install_profile", "issue_token"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.subject.root.exists())
        self.assertEqual(self.facts.issued, 0)

    def test_apply_completes_profile_and_token_then_repeat_is_read_only(self):
        path = self.approval()
        receipt = self.subject.apply(path)
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(self.subject.profile.read_bytes(), self.facts.profile_bytes)
        self.assertEqual(stat.S_IMODE(self.subject.token.stat().st_mode), 0o600)
        self.assertNotEqual(receipt["bindings"]["target_profile_digest"], receipt["profile_file_sha256"])
        self.assertEqual(receipt["effective_parameters"]["PROFILE_DIGEST"], "d" * 64)
        self.assertTrue(receipt["effective_parameters"]["OFFICIAL_CAMPAIGN"].endswith("/fixture-campaign"))
        before = self.snapshot()
        self.assertEqual(self.subject.apply(path), receipt)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.facts.issued, 1)
        encoded = json.dumps(receipt)
        self.assertNotIn(self.subject.token.read_text(), encoded)
        self.assertNotIn("admin@fixture.invalid", encoded)

    def test_healthy_assets_still_require_explicit_approval_to_seal(self):
        self.healthy()
        self.assertEqual(self.subject.inspect()["status"], "ready")
        with self.assertRaises(FileNotFoundError):
            self.subject.consume()
        self.subject.apply(self.approval())
        self.assertEqual(self.facts.issued, 0)

    def test_legacy_readonly_token_mode_is_only_normalized_by_approval(self):
        self.healthy(mode=0o400)
        self.assertEqual(self.subject.inspect()["actions"], ["normalize_token_mode"])
        self.assertEqual(stat.S_IMODE(self.subject.token.stat().st_mode), 0o400)
        self.subject.apply(self.approval())
        self.assertEqual(stat.S_IMODE(self.subject.token.stat().st_mode), 0o600)
        self.assertEqual(self.facts.issued, 0)

    def test_wrong_expired_or_incomplete_approval_never_writes(self):
        cases = [{"admission_key": "f" * 64}, {"actions": []}, {"seal_admission": False}, {"approved_by": ""},
                 {"schema_version": "unknown"}, {"expires_at_utc": "2000-01-01T00:00:00Z"}]
        for change in cases:
            with self.subTest(change=change):
                path = self.approval(**change)
                before = self.snapshot()
                with self.assertRaises(admission.AdmissionError):
                    self.subject.apply(path)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.facts.issued, 0)
        with self.assertRaises(FileNotFoundError):
            self.subject.apply(self.root / "missing-approval")

    def test_environment_drift_requires_new_approval(self):
        path = self.approval()
        self.facts.generation += 1
        with self.assertRaisesRegex(admission.AdmissionError, "approval_binding"):
            self.subject.apply(path)
        self.assertEqual(self.facts.issued, 0)

    def test_busy_or_unknown_state_never_issues_token(self):
        for reason in ("driver_run_active", "supervisor_run_active", "campaign_ledger_not_active",
                       "background_validation_not_passed", "project_ledger_blocked"):
            with self.subTest(reason=reason):
                self.facts.failure = reason
                self.assertEqual(self.subject.inspect()["status"], "blocked")
                with self.assertRaises(admission.AdmissionError):
                    self.subject.apply(self.approval())
                self.assertEqual(self.facts.issued, 0)

    def test_owner_modes_and_links_are_rejected(self):
        self.healthy()
        self.subject.token.chmod(0o644)
        self.assertEqual(self.subject.inspect()["status"], "blocked")
        with mock.patch.object(admission.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(admission.AdmissionError, "owner"):
                admission.read_private(self.subject.token, modes=(0o644,))
        self.subject.token.unlink()
        self.subject.token.symlink_to(self.root / "missing")
        self.assertEqual(self.subject.inspect()["status"], "blocked")

    def test_capture_profile_drift_is_rejected(self):
        self.healthy()
        self.facts.container_drift = True
        self.assertEqual(self.subject.inspect()["status"], "blocked")

    def test_reuse_only_accepts_same_target_frozen_predecessor(self):
        self.healthy()
        official = self.root / "data/evidence/campaigns/predecessor"
        self.config.update(EVIDENCE_DECISION="reuse", PREDECESSOR_CAMPAIGN=str(official))
        source = {"campaign_id": "predecessor", "target_version": "9.1.0"}
        current = {"predecessor": {"campaign_dir": str(official), "campaign_id": "predecessor", "campaign_manifest_sha256": "e" * 64}}
        upgrade = mock.Mock()
        upgrade._require_formal_campaign.side_effect = lambda path: source if path == official else current
        upgrade.file_sha256.return_value = "e" * 64
        self.facts.upgrade = upgrade
        report = self.subject.inspect()
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["effective_parameters"]["OFFICIAL_CAMPAIGN"], str(official))
        source["target_version"] = "9.0.0"
        self.assertEqual(self.subject.inspect()["status"], "blocked")
        source["target_version"] = "9.1.0"
        current["predecessor"]["campaign_manifest_sha256"] = "wrong"
        self.assertEqual(self.subject.inspect()["status"], "blocked")

    def test_failure_compensates_previous_bytes_and_modes(self):
        now = int(time.time())
        claims = {"user_id": 1, "email": "admin@fixture.invalid", "role": "admin", "token_version": 0,
                  "iat": now - 60, "nbf": now - 60, "exp": now + 60}
        admission.atomic_write(self.subject.token, token(claims=claims), 0o400)
        admission.atomic_write(self.subject.profile, b"old-profile")
        old = {p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in (self.subject.token, self.subject.profile)}
        path = self.approval()
        self.facts.fail_after_issue = True
        with self.assertRaisesRegex(admission.AdmissionError, "apply_failed_compensated"):
            self.subject.apply(path)
        for p, (raw, mode) in old.items():
            self.assertEqual((p.read_bytes(), stat.S_IMODE(p.stat().st_mode)), (raw, mode))
        event = json.loads(next((self.subject.root / "vc5-admission-events").glob("*.json")).read_text())
        self.assertEqual((event["status"], event["compensation_passed"]), ("failed", True))
        self.assertFalse(self.subject.receipt.exists())

    def test_expired_token_cannot_silently_reuse_prior_approval(self):
        path = self.approval()
        self.subject.apply(path)
        self.subject.token.write_bytes(token(claims={"user_id": 1, "email": "admin@fixture.invalid", "role": "admin",
            "token_version": 0, "iat": 1, "nbf": 1, "exp": 2}))
        with self.assertRaisesRegex(admission.AdmissionError, "approval_binding_or_actions_invalid"):
            self.subject.apply(path)
        self.assertEqual(self.facts.issued, 1)
        fresh = self.approval(approval_id="fixture-renewal")
        receipt = self.subject.apply(fresh)
        self.assertEqual(receipt["applied_actions"], ["renew_token"])
        self.assertEqual(self.facts.issued, 2)

    def test_dispatch_lock_excludes_apply_and_rejects_foreign_mode(self):
        path = self.approval()
        self.subject.root.mkdir(mode=0o700)
        lock = self.subject.root / "vc5-dispatch.lock"
        with admission.dispatch_lock(lock):
            with self.assertRaisesRegex(admission.AdmissionError, "already_running"):
                self.subject.apply(path)
        self.assertEqual(self.facts.issued, 0)
        lock.chmod(0o644)
        with self.assertRaisesRegex(admission.AdmissionError, "owner_mode_or_type"):
            self.subject.apply(path)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)

    def test_receipt_parameters_and_apply_event_drift_are_rejected(self):
        path = self.approval()
        self.subject.apply(path)
        for target in (self.subject.receipt, self.subject.effective,
                       next((self.subject.root / "vc5-admission-events").glob("*.json"))):
            with self.subTest(target=target.name):
                original = target.read_bytes()
                value = json.loads(original)
                value["status"] = "tampered"
                admission.write_json(target, value)
                with self.assertRaises(admission.AdmissionError):
                    self.subject.consume()
                admission.atomic_write(target, original)

    def test_main_errors_never_echo_credential_or_payload(self):
        secret = token().decode()
        self.facts.state = mock.Mock(side_effect=RuntimeError(secret))
        with mock.patch.dict(sys.modules, {"driver_config": mock.Mock(load_config=lambda: self.config)}), \
                mock.patch.object(admission, "ManagedFacts", return_value=self.facts), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(admission.main(["--dry-run"]), 3)
        self.assertNotIn(secret, output.getvalue() + error.getvalue())
        self.assertIn("state_replay_failed", output.getvalue())

    def test_drift_between_inspection_and_locked_apply_is_rejected(self):
        path = self.approval()
        original = self.facts.state
        calls = 0

        def changing_state():
            nonlocal calls
            calls += 1
            if calls >= 3:
                self.facts.generation += 1
            return original()

        self.facts.state = changing_state
        with self.assertRaisesRegex(admission.AdmissionError, "changed_before_apply"):
            self.subject.apply(path)
        self.assertEqual(self.facts.issued, 0)
        self.assertFalse(self.subject.profile.exists())

    def test_secret_in_signer_failure_is_redacted_and_profile_compensated(self):
        path = self.approval()
        self.facts.issue_token = mock.Mock(side_effect=RuntimeError(token().decode()))
        with self.assertRaisesRegex(admission.AdmissionError, "apply_failed_compensated"):
            self.subject.apply(path)
        self.assertFalse(self.subject.profile.exists())
        event = json.loads(next((self.subject.root / "vc5-admission-events").glob("*.json")).read_text())
        self.assertEqual(event["reason"], "apply_failed")
        self.assertNotIn(token().decode(), json.dumps(event))

    def test_compensation_does_not_overwrite_foreign_write(self):
        path = self.approval()

        def fail_with_foreign_write(signing):
            admission.atomic_write(self.subject.profile, b"foreign-write")
            raise RuntimeError("隔离故障")

        self.facts.issue_token = fail_with_foreign_write
        with self.assertRaisesRegex(admission.AdmissionError, "requires_manual_recovery"):
            self.subject.apply(path)
        self.assertEqual(self.subject.profile.read_bytes(), b"foreign-write")

    def test_rejected_token_is_never_automatically_replaced(self):
        self.healthy()
        for raw in (b"bad", token(secret="wrong"), token(header={"alg": "none"})):
            with self.subTest(credential=hashlib.sha256(raw).hexdigest()[:8]):
                self.subject.token.write_bytes(raw)
                report = self.subject.inspect()
                self.assertEqual(report["status"], "blocked")
                self.assertFalse(any(action in report["actions"] for action in ("issue_token", "renew_token")))
                self.assertEqual(self.facts.issued, 0)


class TokenContractTests(unittest.TestCase):
    def verify(self, raw, **kwargs):
        return admission.verify_token(raw, "隔离验收签名密钥", "admin@fixture.invalid", 1800, **kwargs)

    def test_ttl_1799_and_1800_boundary(self):
        now = int(time.time())
        claims = {"user_id": 1, "email": "admin@fixture.invalid", "role": "admin", "token_version": 0,
                  "iat": now - 10, "nbf": now - 10, "exp": now + 1799}
        with self.assertRaises(admission.TokenExpiry):
            self.verify(token(claims=claims), now=now)
        claims["exp"] += 1
        self.assertEqual(self.verify(token(claims=claims), now=now)["credential_sha256"], hashlib.sha256(token(claims=claims)).hexdigest())

    def test_structure_algorithm_signature_and_identity_contract(self):
        now = int(time.time())
        base = {"user_id": 1, "email": "admin@fixture.invalid", "role": "admin", "token_version": 0,
                "iat": now - 10, "nbf": now - 10, "exp": now + 86400}
        invalid = [b"bad", b"a.b", b"a.b.c.d", b"a..c", token(header={"alg": "none"}),
                   token(header={"alg": "HS512"}), token(secret="wrong")]
        for change in ({"role": "user"}, {"email": "wrong@fixture.invalid"}, {"user_id": 0}, {"user_id": True},
                       {"token_version": -1}, {"nbf": now + 1}, {"iat": now + 1}, {"exp": now - 10}, {"aud": "undeclared"}):
            invalid.append(token(claims={**base, **change}))
        for field in admission.REQUIRED_CLAIMS:
            invalid.append(token(claims={k: v for k, v in base.items() if k != field}))
        for raw in invalid:
            with self.subTest(case=hashlib.sha256(raw).hexdigest()[:8]):
                with self.assertRaises(admission.AdmissionError):
                    self.verify(raw, now=now)


class ManagedReplayTests(unittest.TestCase):
    """核验正式适配器调用实际只读 API、阶段边界及 Catalog 的两个摘要合同。"""

    def test_state_uses_readonly_project_replay_and_rejects_real_live_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_root = root / "supervisor"
            state_root.mkdir()
            project_root = root / "project"
            project_root.mkdir()
            config = {"D": str(root), "NEW": "fixture", "TARGET_VERSION": "9.1.0", "RUNROOT": str(root / "round"), "EVIDENCE_DECISION": "recapture"}
            subject = admission.ManagedFacts.__new__(admission.ManagedFacts)
            subject.config = config
            subject.upgrade = mock.Mock()
            subject.upgrade._require_formal_campaign.return_value = {}
            subject.upgrade._campaign_timing_ledger_dir.return_value = root / "timing"
            project = mock.Mock()
            project.find_project_ledger.return_value = project_root
            project._load_plan.return_value = ({}, b"{}")
            project._load_events.return_value = []
            project._replay.return_value = {"blocked": False, "remaining_live_requests": 10}
            project.root_causes_at_limit_for.return_value = []
            ledger = mock.Mock()
            ledger.inspect_ledger.return_value = {"status": "active", "evidence_decision": "recapture"}
            supervisor = mock.Mock(ACTIVE_STATES={"prepared", "running"})
            modules = {"tools.official_client_capture.codex_upgrade_project_ledger": project,
                       "tools.official_client_capture.codex_upgrade_timing_ledger": ledger,
                       "tools.official_client_capture.codex_upgrade_supervisor": supervisor,
                       "background_validation": mock.Mock(check=mock.Mock(return_value=(0, "passed")),
                                                          latest_deployment=mock.Mock(return_value={"deployment": "fixture"}))}
            with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {"VC_STATE_DIR": str(state_root)}):
                self.assertEqual(subject.state()["background_validation"], "passed")
                project._replay.assert_called_once_with(project_root, {}, [], rebuild_cache=False)
                self.assertEqual(list(project_root.iterdir()), [])
                pid = root / "round/vc5-run-batch.pid"
                admission.atomic_write(pid, str(os.getpid()).encode())
                with self.assertRaisesRegex(admission.AdmissionError, "driver_run_active"):
                    subject.state()
                pid.unlink()
                (state_root / "run-fixture").mkdir()
                supervisor._read_state.return_value = {"state": "prepared"}
                with self.assertRaisesRegex(admission.AdmissionError, "supervisor_run_active"):
                    subject.state()

    def test_catalog_replays_vc3_and_vc4_and_separates_content_and_file_digests(self):
        from tools.official_client_capture import codex_upgrade as upgrade
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            catalog = root / "catalog"
            profile_digest = "d" * 64
            relative = f"catalogdata/runtime/profiles/9.1.0/{profile_digest}.json"
            raw = admission.canonical({"Digest": profile_digest})
            admission.atomic_write(catalog / relative, raw)
            stage = {"campaign_id": "fixture", "target_version": "9.1.0", "profile_id": "fixture-profile",
                     "target_profile_digest": profile_digest, "classification_sha256": "c" * 64,
                     "inventory": [{"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}]}
            stage["inventory_sha256"] = upgrade._fingerprint(stage["inventory"])
            receipt = catalog / "catalog-stage-receipt.json"
            admission.write_json(receipt, stage)
            config = {"D": str(root), "B": str(root / "candidate"), "NEW": "fixture", "CAND": "candidate",
                      "TARGET_VERSION": "9.1.0", "PROFILE_ID": "fixture-profile", "LIFECYCLE_DIR": "docs/lifecycle/fixture", "C": "a" * 40}
            admission.atomic_write(root / "candidate/source/docs/lifecycle/fixture/catalog-stage/catalog-stage-receipt.json", receipt.read_bytes())
            build_path = root / "evidence/campaigns/fixture/candidates/candidate/build-receipt.json"
            admission.write_json(build_path, {})
            fake = mock.Mock()
            fake._load_stage_result.return_value = {"joint_manifest_sha256": "c" * 64}
            fake._replay_vc_checkpoint.return_value = (None, {"stage_receipt": {"path": str(receipt), "sha256": upgrade.file_sha256(receipt)}})
            fake.file_sha256 = upgrade.file_sha256
            fake._verify_catalog_stage_output = upgrade._verify_catalog_stage_output
            fake._profile_binding_from_manifest.return_value = ("fixture-profile", profile_digest)
            fake._replay_candidate_build_receipt.return_value = ({"image": {"reference": "fixture@sha256:" + "b" * 64,
                                                                           "image_id": "sha256:" + "b" * 64},
                                                                "build": {"build_id": "fixture-build"}}, {})
            fake._current_candidate_revision_record.return_value = (1, None)
            fake._bind_candidate_identity_to_build_receipt.return_value = {"git_commit": "a" * 40}
            subject = admission.ManagedFacts.__new__(admission.ManagedFacts)
            subject.config, subject.upgrade = config, fake
            result = subject.catalog()
            self.assertNotEqual(result["profile_digest"], result["profile_sha256"])
            self.assertEqual(result["profile_sha256"], hashlib.sha256(raw).hexdigest())
            fake._replay_vc_checkpoint.assert_any_call(mock.ANY, mock.ANY, "VC-4", revision=1)
            admission.atomic_write(catalog / relative, b"drift")
            with self.assertRaises(upgrade.ConfigurationError):
                subject.catalog()


class EntryOrderingTests(unittest.TestCase):
    def test_approved_start_dispatches_one_isolated_batch(self):
        """真实准入、入口和生成器配合记录器替身；不切生产网关，不执行采集请求。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = _DriverFixture(root)
            drv = root / "drv"
            drv.mkdir()
            for name in ("lib.sh", "parse_env.py", "driver_config.py", "vc5_admission.py", "vc5-start.sh"):
                shutil.copy(SCRIPTS / name, drv / name)
            compose = root / "compose"
            compose.mkdir()
            admission.atomic_write(compose / ".env", 'JWT_SECRET="隔离验收签名密钥"\nADMIN_EMAIL=admin@fixture.invalid\nPOSTGRES_USER=fixture\nPOSTGRES_PASSWORD=fixture\nPOSTGRES_DB=fixture\n'.encode())
            signer = root / "signer"
            admission.atomic_write(signer, b"#!/bin/sh\nexit 1\n", 0o700)
            with fixture.env_file.open("a") as handle:
                handle.write(f'JWTGEN_BIN="{signer}"\n')
            image = "sha256:" + "a" * 64
            admission.write_json(fixture.candidate_dir / "artifacts/build-parameters.json", {"docker_build": {"image_id": image}})
            admission.write_json(fixture.newdir / "candidates" / fixture.cand / "build-receipt.json", {"build": {"build_id": "fixture-build"}})
            admission.write_json(fixture.newdir / "control/vc/batches/000001-fixture.json", {})
            (fixture.data_root / "tools/official_client_capture/codex_upgrade.py").write_text(
                "def _directory_tree_digest(path):\n    return 'e' * 64\n")
            runner = root / "facts_runner.py"
            runner.write_text(f'''import sys,runpy
from pathlib import Path
from unittest import mock
sys.path.insert(0,{str(drv)!r})
sys.path.append({str(SCRIPTS.parents[2])!r})
import vc5_admission as module
from driver_config import load_config
from tools.official_client_capture.tests.test_vc5_admission import Facts
config=load_config()
facts=Facts(Path(config["D"])/"runtime"/("codex-profile-"+config["TARGET_VERSION"]+".json"))
original=facts.catalog
facts.catalog=lambda: {{**original(),"profile_id":config["PROFILE_ID"]}}
with mock.patch.object(module,"ManagedFacts",return_value=facts):
    if sys.argv[1]=="--generate":
        sys.argv=["gen_vc5_plans.py",*sys.argv[2:]]
        runpy.run_path({str(SCRIPTS / "gen_vc5_plans.py")!r},run_name="__main__")
    else:
        raise SystemExit(module.main(sys.argv[1:]))
''')
            (drv / "vc5-precheck.sh").write_text(
                f'#!/bin/bash\ncase "$1" in --*) exec python3 -B "{runner}" "$@" ;; *) exit 0 ;; esac\n')
            (drv / "gen_vc5_plans.py").write_text(
                f'import os,sys\nos.execv(sys.executable,[sys.executable,"-B",{str(runner)!r},"--generate",*sys.argv[1:]])\n')
            calls = root / "dispatch.log"
            for name in ("guard.sh", "vc5-switch.sh", "vc-batch.sh"):
                (drv / name).write_text(f'#!/bin/bash\necho "{name} $*" >> "{calls}"\necho FIXTURE_DONE\n')
            binaries = root / "bin"
            binaries.mkdir()
            for name, body in {"git": f"echo {'c' * 40}", "pgrep": "exit 1", "docker": "exit 0",
                               "setsid": 'test "$1" != -f || shift\nexec "$@"'}.items():
                admission.atomic_write(binaries / name, ("#!/bin/bash\n" + body + "\n").encode(), 0o700)
            environment = {**os.environ, **fixture.env, "PATH": f"{binaries}:{os.environ['PATH']}", "PYTHONDONTWRITEBYTECODE": "1"}
            dry = subprocess.run([sys.executable, "-B", str(runner), "--dry-run"], env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(dry.returncode, 3, dry.stderr)
            report = json.loads(dry.stdout)
            now = datetime.now(timezone.utc)
            approval = root / "approval.json"
            admission.write_json(approval, {"schema_version": admission.APPROVAL_SCHEMA, "approved_by": "隔离验收操作者",
                "approval_id": "fixture-start", "approved_at_utc": (now - timedelta(minutes=1)).isoformat(),
                "expires_at_utc": (now + timedelta(minutes=10)).isoformat(), "admission_key": report["admission_key"],
                "actions": report["actions"], "seal_admission": True})
            applied = subprocess.run([sys.executable, "-B", str(runner), "--apply", "--approval", str(approval)],
                                     env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            first = _run(drv / "vc5-start.sh", env=environment, cwd=root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            log = fixture.runroot / "vc5-run-batch.out"
            self.assertIn("RUN_BATCH_DONE", log.read_text())
            repeat = _run(drv / "vc5-start.sh", env=environment, cwd=root)
            self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
            self.assertIn("VC5_START_SKIP", repeat.stdout)
            dispatched = calls.read_text().splitlines()
            self.assertEqual([line.split()[0] for line in dispatched], ["guard.sh", "vc5-switch.sh", "vc-batch.sh"])
            self.last_start_receipt = {"schema_version": "upgrade-a01-isolated-dispatch/v1", "mode": "isolated_recorders",
                "status": "passed", "first_exit_code": first.returncode, "repeat_exit_code": repeat.returncode,
                "dispatch_log": dispatched, "batch_log": log.read_text(), "repeat_log": repeat.stdout,
                "admission": json.loads((fixture.runroot / "vc5-admission.json").read_text()),
                "approval": json.loads(approval.read_text()),
                "apply_event": json.loads(next((fixture.runroot / "vc5-admission-events").glob("*.json")).read_text()),
                "effective_parameters": json.loads((fixture.runroot / "vc5-effective-parameters.json").read_text())}

    def test_dispatch_lock_remains_held_by_background_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = _DriverFixture(root)
            drv = root / "drv"
            drv.mkdir()
            for name in ("lib.sh", "parse_env.py"):
                shutil.copy(SCRIPTS / name, drv / name)
            pid_file = root / "child.pid"
            ready = root / "ready"
            launcher = drv / "launcher.sh"
            launcher.write_text('set -Eeuo pipefail\nsource "$(dirname "$0")/lib.sh"\nvc5_dispatch_lock\n'
                                f'bash -c \'echo $$ > "{pid_file}"; touch "{ready}"; exec sleep 30\' > "{root / "child.log"}" 2>&1 &\n')
            result = _run(launcher, env=fixture.env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            pid = int(pid_file.read_text())
            try:
                contender = drv / "contender.sh"
                contender.write_text('set -Eeuo pipefail\nsource "$(dirname "$0")/lib.sh"\nvc5_dispatch_lock\n')
                result = _run(contender, env=fixture.env, cwd=root)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("禁止抢派", result.stdout + result.stderr)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)

    def test_generator_uses_admission_parameters_and_rejects_drift_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = _DriverFixture(root)
            config = parse_env.parse(fixture.env_file.read_text())
            config.update(parse_env.derive(config))
            image = "sha256:" + "a" * 64
            receipt = {"effective_parameters": {"PROFILE_ID": config["PROFILE_ID"], "PROFILE_DIGEST": "d" * 64,
                                                "OFFICIAL_CAMPAIGN": str(fixture.newdir)},
                       "bindings": {"campaign": fixture.new, "candidate": fixture.cand, "image_id": image, "build_id": "build"}}
            fake_admission = mock.Mock()
            fake_admission.Admission.return_value.consume.return_value = receipt
            fake_config = mock.Mock(load_config=lambda: dict(config))
            modules = {"vc5_admission": fake_admission, "driver_config": fake_config}
            output = root / "plans"
            args = ["gen_vc5_plans.py", str(output), fixture.new, fixture.cand, image, "build"]
            environment = {**config, "CANDIDATE_DIR": str(fixture.candidate_dir)}
            with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, environment), \
                    mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                runpy.run_path(str(SCRIPTS / "gen_vc5_plans.py"), run_name="__main__")
            command = json.loads((output / "action-plan-vc5-run.json").read_text())["actions"][0]["command"]
            self.assertEqual(command[command.index("--profile-digest") + 1], "d" * 64)
            for index in (2, 3, 4, 5):
                with self.subTest(argument=index):
                    rejected = list(args)
                    rejected[1] = str(root / f"rejected-{index}")
                    rejected[index] = "wrong"
                    with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, environment), \
                            mock.patch.object(sys, "argv", rejected), self.assertRaises(SystemExit):
                        runpy.run_path(str(SCRIPTS / "gen_vc5_plans.py"), run_name="__main__")
                    self.assertFalse(Path(rejected[1]).exists())

    def test_dry_run_never_sources_mutating_lib(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            shutil.copy(SCRIPTS / "vc5-precheck.sh", root)
            marker = root / "marker"
            (root / "lib.sh").write_text(f"touch '{marker}'\n")
            (root / "vc5_admission.py").write_text("import sys; print('dry-run-only', *sys.argv[1:])\n")
            result = _run(root / "vc5-precheck.sh", "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dry-run-only --dry-run", result.stdout)
            self.assertFalse(marker.exists())

    def test_rejected_admission_prevents_start_recover_all_and_switch(self):
        for name, args in (("vc5-start.sh", []), ("vc5-recover.sh", ["/missing-preview"]),
                           ("vc5-all.sh", []), ("vc5-switch.sh", ["candidate", "tag", "image", "tree", "build"])):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                fixture = _DriverFixture(root)
                (root / "compose").mkdir()
                drv = root / "drv"
                drv.mkdir()
                for file in ("lib.sh", "parse_env.py", name):
                    shutil.copy(SCRIPTS / file, drv / file)
                calls = root / "calls"
                (drv / "vc5-precheck.sh").write_text(f"#!/bin/bash\necho admission >> '{calls}'\nexit 3\n")
                for file in ("guard.sh", "vc5-switch.sh", "gen_vc5_plans.py", "gen_vc5_recovery_plans.py", "vc-batch.sh"):
                    if file != name:
                        (drv / file).write_text(f"echo forbidden >> '{calls}'\nexit 88\n")
                result = _run(drv / name, *args, env=fixture.env, cwd=root)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertEqual(calls.read_text().splitlines(), ["admission"])
                self.assertFalse((fixture.runroot / "vc5-run-batch.pid").exists())
                self.assertFalse((fixture.runroot / "vc5-run-batch.sh").exists())


if __name__ == "__main__":
    unittest.main()
