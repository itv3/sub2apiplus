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

    def __init__(self, profile, config=None):
        self.profile_bytes = admission.canonical({"Digest": "d" * 64, "Version": "9.1.0"})
        self.profile = profile
        self.config = config
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

    def official_campaign(self):
        """官方证据所在的 Campaign 走正式适配器（B5b-01：本客户端的 ``vc5_official_campaign``），测试按需替换编排器的只读成员。"""

        subject = admission.ManagedFacts.__new__(admission.ManagedFacts)
        subject.config = self.config
        return subject.official_campaign()

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
        self.facts = Facts(profile, self.config)
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

    def test_a_subset_background_conclusion_is_stated_in_the_report_and_changes_the_admission_key(self):
        """Codex 升级工具优化第 9 项：依据子集结论的准入，报告的 state 检查项写明范围与跳过的模块数，准入键也不同；全集结论的报告形状不变。"""
        full = self.subject.inspect()
        self.assertNotIn("note", next(item for item in full["checks"] if item["check"] == "state"))
        self.facts.state = lambda: {"deployment": self.facts.generation, "background_validation": "passed",
                                    "background_validation_scope": "codex 计划，跳过 40/395 个测试模块，基点 bbbbbbbbbbbb"}
        subset = self.subject.inspect()
        state_check = next(item for item in subset["checks"] if item["check"] == "state")
        self.assertEqual(state_check["status"], "passed")
        self.assertEqual(state_check["note"], "依据的后台验证是子集：codex 计划，跳过 40/395 个测试模块，基点 bbbbbbbbbbbb")
        self.assertNotEqual(subset["admission_key"], full["admission_key"], "子集与全集依据的准入键不会相同，子集准入的收据不能当全集的用")

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
        # B5b-01：reuse 分支搬进 Codex 适配器（codex_upgrade_client_adapter.vc5_official_campaign），只读成员取 codex_upgrade。
        from tools.official_client_capture import codex_upgrade as upgrade
        with mock.patch.object(upgrade, "_require_formal_campaign", side_effect=lambda path: source if path == official else current), \
                mock.patch.object(upgrade, "file_sha256", return_value="e" * 64):
            report = self.subject.inspect()
            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["effective_parameters"]["OFFICIAL_CAMPAIGN"], str(official))
            source["target_version"] = "9.0.0"
            blocked = self.subject.inspect()
            self.assertEqual(blocked["status"], "blocked")
            self.assertIn({"check": "official_campaign", "status": "blocked", "reason": "official_reuse_binding_invalid"}, blocked["checks"])
            self.assertEqual(blocked["effective_parameters"]["OFFICIAL_CAMPAIGN"], str(official))
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


    # ---- Codex 升级工具优化第 8、9 项：库表前置与剩余有效期的只读预览、只续令牌的明确批准续签 ----

    GOOD_DB = {"account_found": 1, "key_found": 1, "group_id": 3, "model_mapping_type": "missing", "model_mapping_count": 0,
               "allow_live": True, "active_keys": [130]}

    def database(self, **changes):
        """给事实替身装上库表查询（真实实现是 ManagedFacts.candidate_database）：返回好库加上给定的偏差。"""

        self.config.update(CODEX_ACCOUNT_ID="90", API_KEY_ID="130")
        value = {**self.GOOD_DB, **changes}
        self.facts.candidate_database = lambda signing, account_id, key_id: value

    def expiring_token(self, remaining):
        now = int(time.time())
        self.subject.token.write_bytes(token(claims={"user_id": 1, "email": "admin@fixture.invalid", "role": "admin", "token_version": 0,
                                                     "iat": now - 60, "nbf": now - 60, "exp": now + remaining}))

    def test_dry_run_states_the_token_margin_and_warns_before_consume_would_fail(self):
        self.healthy()
        report = self.subject.dry_run_report(skip_database=True)
        token_ttl = report["ttl"]["token"]
        self.assertEqual(token_ttl["status"], "ok")
        self.assertEqual(token_ttl["minimum_seconds"], 43200)
        self.assertGreater(token_ttl["margin_seconds"], 40000, "24 小时的令牌减去 12 小时的准入要求，富余约 12 小时")
        self.assertEqual(report["ttl"]["warnings"], [])
        self.expiring_token(43200 + 3600)   # 再过约 1 小时就会低于准入要求
        report = self.subject.dry_run_report(skip_database=True)
        self.assertEqual(report["ttl"]["token"]["status"], "ok")
        self.assertTrue(any("就会低于准入要求" in text for text in report["ttl"]["warnings"]), report["ttl"])
        self.expiring_token(3000)           # 已经低于：consume 会失败，报告写 renew_required 与需要 renew_token
        report = self.subject.dry_run_report(skip_database=True)
        self.assertEqual(report["ttl"]["token"]["status"], "renew_required")
        self.assertLess(report["ttl"]["token"]["margin_seconds"], 0)
        self.assertEqual(report["actions"], ["renew_token"])
        self.assertTrue(any("renew_token" in text for text in report["ttl"]["warnings"]))
        self.assertNotIn(self.subject.token.read_text(), json.dumps(report), "报告里不能出现令牌本体")

    def test_ttl_report_tells_the_applied_and_the_pending_approval_apart(self):
        self.subject.apply(self.approval())
        report = self.subject.ttl_report()
        applied = report["approval"]["applied"]
        self.assertEqual(applied["approved_by"], "隔离验收操作者")
        self.assertIn("apply 之后不影响 consume", applied["note"])
        pending = self.root / "pending-approval.json"
        now = datetime.now(timezone.utc)
        admission.write_json(pending, {"approval_id": "p", "expires_at_utc": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        self.config["VC5_ADMISSION_APPROVAL"] = str(pending)
        report = self.subject.ttl_report()
        self.assertEqual(report["approval"]["pending"]["status"], "valid")
        self.assertTrue(0 < report["approval"]["pending"]["remaining_seconds"] <= 600)
        self.assertTrue(any("尽快 --apply" in text for text in report["warnings"]), "剩余不足 30 分钟要提醒")
        admission.write_json(pending, {"approval_id": "p", "expires_at_utc": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        report = self.subject.ttl_report()
        self.assertEqual(report["approval"]["pending"]["status"], "expired")
        self.assertTrue(any("已过期" in text for text in report["warnings"]))

    def test_database_prerequisites_pass_on_a_good_database(self):
        self.healthy()
        self.database()
        report = self.subject.dry_run_report()
        self.assertEqual([(row["check"], row["status"]) for row in report["candidate_prerequisites"]],
                         [("capture_identity", "passed"), ("model_mapping_empty", "passed"), ("group_allow_live", "passed"),
                          ("group_single_active_key", "passed")])
        self.assertEqual(report["status"], "ready", "库表前置全过时顶层状态沿用准入自己的判定（healthy 的环境就是 ready）")

    def test_each_unmet_database_prerequisite_blocks_the_dry_run_and_explains_why_how_and_restore(self):
        """三种不满足（上一轮 VC-5 连败两次、还差一个没撞上的就是它们）：各自在构造的库上被只读预览报出，提示写清为什么、怎么临时改、VC-6 之后怎么恢复。"""

        cases = {
            "model_mapping_empty": ({"model_mapping_type": "object", "model_mapping_count": 4}, "model_mapping_not_empty",
                                    ("credentials - 'model_mapping'", "jsonb_set", "VC-6 之后")),
            "group_allow_live": ({"allow_live": False}, "group_allow_live_off", ("update groups set allow_live=true", "allow_live=false", "VC-6 之后")),
            "group_single_active_key": ({"active_keys": [3, 130]}, "group_has_other_active_keys", ("status='inactive'", "改回 'active'", "VC-6 之后")),
        }
        for check, (change, reason, words) in cases.items():
            with self.subTest(check=check):
                self.healthy()
                self.database(**change)
                before = self.snapshot()
                report = self.subject.dry_run_report()
                self.assertEqual(self.snapshot(), before, "只读预览不写任何文件")
                self.assertEqual(self.facts.issued, 0)
                self.assertEqual(report["status"], "blocked")
                rows = {row["check"]: row for row in report["candidate_prerequisites"]}
                self.assertEqual({name for name, row in rows.items() if row["status"] == "blocked"}, {check}, "只报不满足的那一项")
                self.assertEqual(rows[check]["reason"], reason)
                for word in ("为什么", "怎么临时改", *words):
                    self.assertIn(word, rows[check]["detail"])
                self.assertIn({"check": f"candidate_prerequisite.{check}", "status": "blocked", "reason": reason}, report["checks"])
                self.assertIn("库表前置 " + check + "：blocked", admission.dry_run_summary(report))
        # 三种同时不满足：一次预览全部报出，不用等到作业跑起来再一个个撞
        self.healthy()
        self.database(model_mapping_type="object", model_mapping_count=4, allow_live=False, active_keys=[3, 130])
        blocked = [row["check"] for row in self.subject.dry_run_report()["candidate_prerequisites"] if row["status"] == "blocked"]
        self.assertEqual(blocked, ["model_mapping_empty", "group_allow_live", "group_single_active_key"])

    def test_capture_identity_that_does_not_match_the_database_stops_the_other_checks(self):
        self.healthy()
        self.database(account_found=0)
        rows = {row["check"]: row for row in self.subject.dry_run_report()["candidate_prerequisites"]}
        self.assertEqual(rows["capture_identity"]["reason"], "capture_identity_mismatch")
        self.assertIn("CODEX_ACCOUNT_ID", rows["capture_identity"]["detail"])
        self.assertEqual({row["status"] for name, row in rows.items() if name != "capture_identity"}, {"blocked"})
        self.database(key_found=1, group_id=None)
        self.assertIn("没有绑定分组", self.subject.dry_run_report()["candidate_prerequisites"][0]["detail"])

    def test_unavailable_database_is_a_blocked_row_and_does_not_leak_details(self):
        self.healthy()
        self.config.update(CODEX_ACCOUNT_ID="90", API_KEY_ID="130")

        def broken(signing, account_id, key_id):
            raise RuntimeError(signing["POSTGRES_PASSWORD"] + " 连接失败")
        self.facts.candidate_database = broken
        report = self.subject.dry_run_report()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["candidate_prerequisites"][0]["reason"], "candidate_database_unavailable")
        self.assertNotIn("fixture 连接失败", json.dumps(report))

    def test_database_prerequisites_are_not_part_of_apply_or_consume(self):
        """库表前置只属于只读预览：采集作业跑完后环境（如恢复 Key）变化，不能让后面的 consume 误失败。"""

        self.database(allow_live=False)
        receipt = self.subject.apply(self.approval())
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(self.subject.consume()["receipt_sha256"], receipt["receipt_sha256"])

    def test_candidate_database_runs_one_readonly_select_without_the_password_in_argv(self):
        facts = admission.ManagedFacts.__new__(admission.ManagedFacts)
        output = json.dumps({**self.GOOD_DB})
        with mock.patch.object(admission.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="noise\n" + output + "\n", stderr="")) as run:
            value = facts.candidate_database({"POSTGRES_USER": "u", "POSTGRES_DB": "d", "POSTGRES_PASSWORD": "秘密口令"}, "90", "130")
        self.assertEqual(value, self.GOOD_DB)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:6], ["docker", "exec", "-e", "PGPASSWORD", "sub2apiplus-postgres", "psql"])
        self.assertNotIn("秘密口令", " ".join(argv))
        self.assertEqual(run.call_args.kwargs["env"]["PGPASSWORD"], "秘密口令")
        sql = argv[argv.index("-c") + 1].lower()
        self.assertTrue(sql.startswith("with "))
        for forbidden in ("update ", "delete ", "insert ", "drop ", "alter "):
            self.assertNotIn(forbidden, sql)
        for bad in (("9;drop", "130"), ("90", "1 or 1=1")):
            with self.assertRaisesRegex(admission.AdmissionError, "capture_identity_not_numeric"):
                facts.candidate_database({"POSTGRES_USER": "u", "POSTGRES_DB": "d", "POSTGRES_PASSWORD": "p"}, *bad)
        with mock.patch.object(admission.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="口令错误")):
            with self.assertRaisesRegex(admission.AdmissionError, "candidate_database_query_failed"):
                facts.candidate_database({"POSTGRES_USER": "u", "POSTGRES_DB": "d", "POSTGRES_PASSWORD": "p"}, "90", "130")

    def test_renew_only_replaces_the_expiring_token_under_an_explicit_approver(self):
        self.subject.apply(self.approval())
        self.assertEqual(self.facts.issued, 1)
        self.assertEqual(self.subject.renew("测试批准人：老板授权自动续签")["status"], "ready", "令牌仍够用时只读复核，不重复签发")
        self.assertEqual(self.facts.issued, 1)
        self.expiring_token(3000)
        with self.assertRaises(admission.AdmissionError):
            self.subject.consume()
        with self.assertRaisesRegex(admission.AdmissionError, "renew_requires_explicit_approver"):
            self.subject.renew("  ")
        receipt = self.subject.renew("测试批准人：老板授权自动续签")
        self.assertEqual(receipt["applied_actions"], ["renew_token"])
        self.assertEqual(receipt["approved_by"], "测试批准人：老板授权自动续签")
        self.assertEqual(self.facts.issued, 2)
        self.assertEqual(self.subject.consume()["receipt_sha256"], receipt["receipt_sha256"])
        approvals = list(self.subject.root.glob("vc5-admission-approval-renew-*.json"))
        self.assertEqual(len(approvals), 1)
        self.assertEqual(stat.S_IMODE(approvals[0].stat().st_mode), 0o600)
        self.assertEqual(json.loads(approvals[0].read_text())["actions"], ["renew_token"])

    def test_renew_refuses_everything_beyond_a_token_renewal(self):
        # 从没批准过：不自动签（首次准入必须走人工批准件）
        self.healthy()
        with self.assertRaisesRegex(admission.AdmissionError, "renew_requires_existing_receipt"):
            self.subject.renew("批准人")
        self.assertEqual(self.facts.issued, 0)
        # 令牌快过期的同时画像也要重装：动作超出令牌，拒绝
        self.subject = admission.Admission(self.config, self.facts)
        self.subject.apply(self.approval())
        self.expiring_token(3000)
        self.subject.profile.write_bytes(b"{}")
        self.subject.profile.chmod(0o600)
        with self.assertRaisesRegex(admission.AdmissionError, "renew_refused_actions_beyond_token"):
            self.subject.renew("批准人")
        self.assertEqual(self.facts.issued, 0, "healthy 环境本来就带令牌，拒绝路径更不会签发")

    def test_renew_refuses_when_the_admission_binding_has_drifted(self):
        self.subject.apply(self.approval())
        self.expiring_token(3000)
        self.facts.generation += 1   # 部署变了：环境指纹与收据里的不同，不能当作“只是令牌快过期”
        with self.assertRaisesRegex(admission.AdmissionError, "renew_refused_binding_drift"):
            self.subject.renew("批准人")
        self.assertEqual(self.facts.issued, 1)

    def test_renew_flag_is_wired_through_main_and_the_shell_entry(self):
        script = (SCRIPTS / "vc5-precheck.sh").read_text()
        self.assertIn("--dry-run|--apply|--consume|--renew)", script)
        with mock.patch.object(admission, "load_config", create=True), mock.patch.dict(sys.modules, {"driver_config": mock.Mock(load_config=lambda: self.config)}), \
                mock.patch.object(admission, "_require_client_admission"), mock.patch.object(admission, "ManagedFacts", return_value=self.facts):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(admission.main(["--renew"]), 3, "没给 --approver 拒绝")
            self.assertIn("renew_requires_explicit_approver", stderr.getvalue())


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

    def test_state_carries_the_scope_of_a_subset_conclusion_and_nothing_for_a_full_one(self):
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
            subset_message = ("后台验证通过（提交 aaaaaaaaaaaa，结论 /r.json，日志 /r.log；按客户端筛选（codex 计划的子集，不是全集）"
                              "【子集：codex 计划，跳过 40/395 个测试模块，基点 bbbbbbbbbbbb，筛选摘要 ssssssssssss】）")
            for message, expected in (("后台验证通过（提交 aaaaaaaaaaaa）", None),
                                      ("后台验证通过（…；门禁范围 auto：全集（回退原因：x））", None),
                                      (subset_message, "codex 计划，跳过 40/395 个测试模块，基点 bbbbbbbbbbbb，筛选摘要 ssssssssssss")):
                with self.subTest(message=message[:30]):
                    modules = {"tools.official_client_capture.codex_upgrade_project_ledger": project,
                               "tools.official_client_capture.codex_upgrade_timing_ledger": ledger,
                               "tools.official_client_capture.codex_upgrade_supervisor": supervisor,
                               "background_validation": mock.Mock(check=mock.Mock(return_value=(0, message)),
                                                                  latest_deployment=mock.Mock(return_value={"deployment": "fixture"}))}
                    with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {"VC_STATE_DIR": str(state_root)}):
                        state = subject.state()
                    self.assertEqual(state["background_validation"], "passed")
                    if expected is None:
                        self.assertNotIn("background_validation_scope", state, "全集结论的准入状态与改造前逐字相同")
                    else:
                        self.assertEqual(state["background_validation_scope"], expected)

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
            config = {"D": str(root), "B": str(root / "candidates/candidate"), "NEW": "fixture", "CAND": "candidate",
                      "RUNROOT": str(root / "round"), "IN": "inputs", "UP": "upgrade", "BASELINE_VERSION": "9.0.0",
                      "TARGET_VERSION": "9.1.0", "PROFILE_ID": "fixture-profile", "LIFECYCLE_DIR": "docs/lifecycle/fixture", "C": "a" * 40}
            admission.atomic_write(root / "candidates/candidate/source/docs/lifecycle/fixture/catalog-stage/catalog-stage-receipt.json", receipt.read_bytes())
            build_path = root / "evidence/campaigns/fixture/candidates/candidate/build-receipt.json"
            admission.write_json(build_path, {})
            campaign = root / "evidence/campaigns/fixture"
            manifest = {"campaign_id": "fixture", "baseline_version": "9.0.0", "target_version": "9.1.0",
                        "vc_control": {"campaign_plan": {"path": "control/vc/campaign-plan.json"}}}
            admission.write_json(campaign / "campaign.json", manifest)
            admission.atomic_write(campaign / "campaign.sha256", upgrade.file_sha256(campaign / "campaign.json").encode())
            admission.write_json(campaign / "control/vc/campaign-plan.json", {})
            admission.write_json(root / "control/upgrade-timing-ledger/ledger.json", {})
            revision = {"campaign_id": "fixture", "candidate_id": "candidate"}
            admission.write_json(campaign / "control/vc/revisions/r1/revision.json", revision)
            admission.write_json(campaign / "control/vc/revisions/r1/COMMIT", {})
            fake = mock.Mock()
            fake._require_formal_campaign.return_value = manifest
            fake._campaign_timing_ledger_dir.return_value = root / "control/upgrade-timing-ledger"
            fake._candidate_revision_dir.return_value = campaign / "control/vc/revisions/r1"
            fake._load_stage_result.return_value = {"joint_manifest_sha256": "c" * 64}
            fake._replay_vc_checkpoint.return_value = (None, {"stage_receipt": {"path": str(receipt), "sha256": upgrade.file_sha256(receipt)}})
            fake.file_sha256 = upgrade.file_sha256
            fake._verify_catalog_stage_output = upgrade._verify_catalog_stage_output
            fake._profile_binding_from_manifest.return_value = ("fixture-profile", profile_digest)
            fake._replay_candidate_build_receipt.return_value = ({"image": {"reference": "fixture@sha256:" + "b" * 64,
                                                                           "image_id": "sha256:" + "b" * 64},
                                                                "build": {"build_id": "fixture-build"}}, {})
            fake._current_candidate_revision_record.return_value = (1, revision)
            fake._bind_candidate_identity_to_build_receipt.return_value = {"git_commit": "a" * 40}
            subject = admission.ManagedFacts.__new__(admission.ManagedFacts)
            subject.config, subject.upgrade = config, fake
            # B5b-01：重放搬进 Codex 适配器（codex_upgrade_client_adapter.catalog_stage_profile），它直接取 codex_upgrade 的成员，
            # 把同一组替身装到 codex_upgrade 上（轮次根解析仍经 subject.upgrade）。
            replaced = {name: getattr(fake, name) for name in (
                "_require_formal_campaign", "_load_stage_result", "_vc_campaign_plan", "_replay_vc_checkpoint",
                "_verify_catalog_stage_output", "_profile_binding_from_manifest", "_replay_candidate_build_receipt",
                "_current_candidate_revision_record", "_candidate_identity_for_run", "_bind_candidate_identity_to_build_receipt",
            )}
            with mock.patch.multiple(upgrade, **replaced):
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
            for name in ("lib.sh", "parse_env.py", "client_table.py", "driver_config.py", "vc5_admission.py", "vc5-start.sh"):
                shutil.copy(SCRIPTS / name, drv / name)
            from tools.official_client_capture.tests.test_arm64_capture_driver import _stub_phase_context
            _stub_phase_context(drv)
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
facts=Facts(Path(config["D"])/"runtime"/("codex-profile-"+config["TARGET_VERSION"]+".json"),config)
original=facts.catalog
facts.catalog=lambda: {{**original(),"profile_id":config["PROFILE_ID"]}}
with mock.patch.object(module,"ManagedFacts",return_value=facts):
    if sys.argv[1]=="--generate":
        sys.argv=["gen_vc5_plans.py",*sys.argv[2:]]
        phase=mock.Mock()
        phase.resolve.return_value={{"parameters":{{"IMAGE_ID":facts.catalog()["image_id"],"BUILD_ID":"fixture-build",
            "BUILD_RECEIPT":str(Path(config["NEWDIR"])/"candidates"/config["CAND"]/"build-receipt.json"),
            "PROFILE_ID":config["PROFILE_ID"],"PROFILE_DIGEST":"d"*64,"CAPTURE_WRITE":"current-result"}}}}
        with mock.patch.dict(sys.modules,{{"phase_context":phase}}):
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
            for name in ("lib.sh", "parse_env.py", "client_table.py"):
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
            fake_phase = mock.Mock()
            fake_phase.resolve.return_value = {"parameters": {"IMAGE_ID": image, "BUILD_ID": "build",
                "BUILD_RECEIPT": str(fixture.newdir / "candidates" / fixture.cand / "build-receipt.json"),
                "PROFILE_ID": config["PROFILE_ID"], "PROFILE_DIGEST": "d" * 64, "CAPTURE_WRITE": "current-result"}}
            modules = {"vc5_admission": fake_admission, "driver_config": fake_config, "phase_context": fake_phase}
            output = Path(config["W"])
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
                for file in ("lib.sh", "parse_env.py", "client_table.py", name):
                    shutil.copy(SCRIPTS / file, drv / file)
                from tools.official_client_capture.tests.test_arm64_capture_driver import _stub_phase_context
                _stub_phase_context(drv)
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
