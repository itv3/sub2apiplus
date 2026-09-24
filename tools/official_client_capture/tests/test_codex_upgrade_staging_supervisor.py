"""改造 4（staging/WAL）监督器层回归：prepared 父 run、commit 回调分支、monitor 三分类、history 与后继协议。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.codex_upgrade_supervisor import SupervisorClient, SupervisorError
from tools.official_client_capture.tests import runtime_egress_fixtures

REPO_ROOT = Path(__file__).resolve().parents[3]
CAMPAIGN_ID = "staging-campaign"

# owner 子进程：prepared 起动后按 mode 决定是否写 COMMIT／激活，然后 SIGKILL 自己模拟崩溃。
OWNER_SCRIPT = r"""
import json, os, signal, sys, time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
config = json.loads(sys.argv[2])
root = Path(config["root"])
client = supervisor.SupervisorClient(
    root / "supervisor",
    campaign_id=config["campaign_id"],
    phase="VC-2",
    deadline_at_epoch=time.time() + 30,
    heartbeat_seconds=0.05,
    watchdog_timeout_seconds=1.0,
    ledger_interval_seconds=0.05,
    terminate_owner=False,
    owner_nonce=config["owner_nonce"],
)
binding = dict(config["staging_binding"])
client.start(prepared=True, staging_binding=binding)
mode = config["mode"]
if mode in {"committed", "activated", "integrity"}:
    nonce = client.owner_nonce if mode != "integrity" else "f" * 64
    commit = artifacts.build_vc_commit(
        campaign_id=config["campaign_id"],
        sequence=2,
        phase="VC-2",
        staging_attempt=1,
        batch_sha256="1" * 64,
        manifest_sha256="2" * 64,
        parent_run_dir=str(client.run_dir),
        owner_nonce=nonce,
        ledger_event_ids=[],
        committed_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    commit_path = Path(binding["commit_path"])
    commit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    commit_path.write_text(json.dumps(commit, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    commit_path.chmod(0o600)
    if mode == "activated":
        client.activate_committed(commit)
print(json.dumps({"run_dir": str(client.run_dir), "owner_pid": os.getpid()}), flush=True)
os.kill(os.getpid(), signal.SIGKILL)
"""


class StagingSupervisorTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def _staging_campaign(self, root: Path, *, batch_model: str | None = "staging") -> tuple[Path, dict[str, object]]:
        """只含总计划绑定的最小 Campaign：足够让 campaign_batch_model 判定模型。"""

        campaign_dir = root / "campaign"
        campaign_dir.mkdir(mode=0o700)
        now = datetime.now(timezone.utc)
        plan = artifacts.build_campaign_plan(
            campaign_id=CAMPAIGN_ID,
            campaign_mode="formal",
            campaign_purpose="validation_only",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc=now.isoformat(),
            original_deadline_at_utc=(now + timedelta(minutes=30)).isoformat(),
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64,
            batch_model=batch_model,
        )
        plan_path = campaign_dir / "control" / "vc" / "campaign-plan.json"
        self._write_json(plan_path, plan)
        (campaign_dir / "control").chmod(0o700)
        self._write_json(
            campaign_dir / "campaign.json",
            {
                "campaign_id": CAMPAIGN_ID,
                "campaign_mode": "formal",
                "target_version": "0.154.0",
                "vc_control": {
                    "campaign_plan": {
                        "path": "control/vc/campaign-plan.json",
                        "sha256": supervisor._sha256(plan_path.read_bytes()),
                    }
                },
            },
        )
        return campaign_dir, plan

    def _binding(self, campaign_dir: Path, *, sequence: int = 2, attempt: int = 1) -> dict[str, object]:
        return {
            "campaign_dir": str(campaign_dir.resolve()),
            "sequence": sequence,
            "phase": "VC-2",
            "staging_attempt": attempt,
            "commit_path": str(supervisor._staging_commit_path(campaign_dir.resolve(), sequence, "VC-2")),
            "prepared_marker_sha256": "3" * 64,
        }

    def _client(self, root: Path, *, owner_nonce: str | None = None, timeout: float = 1.0) -> SupervisorClient:
        return SupervisorClient(
            root / "supervisor",
            campaign_id=CAMPAIGN_ID,
            phase="VC-2",
            deadline_at_epoch=time.time() + 30,
            owner_nonce=owner_nonce,
            heartbeat_seconds=0.05,
            watchdog_timeout_seconds=timeout,
            ledger_interval_seconds=0.05,
            terminate_owner=False,
        )

    def _v2_manifest(self, plan: dict[str, object], *, sequence: int = 1, actions: list[dict[str, object]] | None = None) -> dict[str, object]:
        # 改造 2：staging 模型清单携带（Campaign 级为 null 的）候选绑定字段，legacy 不含。
        binding: dict[str, object] = (
            {"candidate_revision": None, "candidate_id": None}
            if artifacts.campaign_plan_batch_model(plan) == "staging"
            else {}
        )
        return supervisor.build_batched_campaign_run_manifest(
            **binding,
            campaign_id=CAMPAIGN_ID,
            campaign_plan_sha256=str(plan["plan_sha256"]),
            batch_id=f"vc-2-{sequence:04d}",
            batch_sequence=sequence,
            batch_sha256=f"{sequence:064d}",
            phase="VC-2",
            predecessor_checkpoint={
                "path": "control/vc/vc-1-checkpoint.json",
                "sha256": "4" * 64,
                "phase": "VC-1",
                "checkpoint_sha256": "5" * 64,
            },
            original_deadline_at_utc=str(plan["original_deadline_at_utc"]),
            actions=actions or [],
            execute_items=["item-a"] if actions else [],
            reuse_items=[],
        )

    @staticmethod
    def _run_arguments() -> argparse.Namespace:
        return argparse.Namespace(heartbeat_seconds=0.05, watchdog_timeout_seconds=1.0, ledger_interval_seconds=0.05)

    def _commit(
        self,
        client: SupervisorClient,
        *,
        sequence: int = 1,
        nonce: str | None = None,
        manifest_sha256: str = "2" * 64,
    ) -> dict[str, object]:
        return artifacts.build_vc_commit(
            campaign_id=CAMPAIGN_ID,
            sequence=sequence,
            phase="VC-2",
            staging_attempt=1,
            batch_sha256=f"{sequence:064d}",
            manifest_sha256=manifest_sha256,
            parent_run_dir=str(client.run_dir),
            owner_nonce=nonce or client.owner_nonce,
            ledger_event_ids=[],
            committed_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def _spawn_owner(self, root: Path, campaign_dir: Path, *, mode: str) -> dict[str, object]:
        config = {
            "root": str(root),
            "campaign_id": CAMPAIGN_ID,
            "owner_nonce": "a" * 64,
            "staging_binding": self._binding(campaign_dir),
            "mode": mode,
        }
        (root / "supervisor").mkdir(mode=0o700, exist_ok=True)
        completed = subprocess.run(
            [sys.executable, "-c", OWNER_SCRIPT, str(REPO_ROOT), json.dumps(config)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, -signal.SIGKILL, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def _wait_state(self, run_dir: Path, expected: set[str], *, timeout: float = 5.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            if state.get("state") in expected:
                return state
            time.sleep(0.05)
        self.fail(f"父 run 未在预算内进入 {sorted(expected)}")

    # ------------------------------------------------------------------
    # T4.1：state schema
    # ------------------------------------------------------------------

    def test_state_schema_declares_prepared_states_and_nested_staging_attempt_only(self) -> None:
        schema = json.loads((Path(supervisor.__file__).parent / "codex_upgrade_supervisor_state.schema.json").read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]["state"]["enum"]), {"prepared", *supervisor.TERMINAL_STATES, "running"})
        self.assertNotIn("staging_attempt", schema["properties"])
        binding = schema["properties"]["staging_binding"]
        self.assertFalse(binding["additionalProperties"])
        self.assertEqual(set(binding["required"]), supervisor.STAGING_BINDING_FIELDS)
        for field in ("committed_at_epoch", "commit_sha256"):
            self.assertIn(field, schema["properties"])

    # ------------------------------------------------------------------
    # T4.4：prepared 起动、attach 拒绝、activate_committed
    # ------------------------------------------------------------------

    def test_prepared_start_persists_binding_and_attach_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, _plan = self._staging_campaign(root)
            client = self._client(root)
            with self.assertRaisesRegex(SupervisorError, "staging_binding"):
                client.start(prepared=True)
            client.start(prepared=True, staging_binding=self._binding(campaign_dir))
            try:
                state = supervisor._read_state(client.run_dir)
                self.assertEqual(state["state"], "prepared")
                self.assertEqual(state["staging_binding"]["staging_attempt"], 1)
                self.assertNotIn("staging_attempt", state)
                self.assertTrue(client.prepared)
                with self.assertRaisesRegex(SupervisorError, "尚未取得执行权"):
                    SupervisorClient.attach(client.run_dir)
                self.assertEqual(supervisor.classify_prepared_run(client.run_dir), "no_commit")
                commit = self._commit(client, sequence=2)
                activated = client.activate_committed(commit)
                self.assertEqual(activated["state"], "running")
                self.assertEqual(activated["commit_sha256"], commit["commit_sha256"])
                self.assertFalse(client.prepared)
                # 幂等：同一 COMMIT 再激活直接返回；不同 COMMIT 拒绝。
                client.activate_committed(commit)
                other = self._commit(client, sequence=3)
                with self.assertRaisesRegex(SupervisorError, "其他 COMMIT"):
                    client.activate_committed(other)
                foreign = self._commit(client, sequence=2, nonce="b" * 64)
                with self.assertRaisesRegex(SupervisorError, "身份不一致"):
                    client.activate_committed(foreign)
            finally:
                client.stop(reason="test-complete", status="stopped")
            self.assertEqual(supervisor._read_state(client.run_dir)["state"], "stopped")

    def test_read_state_rejects_prepared_without_binding_and_bad_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, _plan = self._staging_campaign(root)
            client = self._client(root)
            client.start(prepared=True, staging_binding=self._binding(campaign_dir))
            try:
                path = client.run_dir / "state.json"
                original = json.loads(path.read_text(encoding="utf-8"))
                broken = dict(original)
                broken.pop("staging_binding")
                path.write_text(json.dumps(broken) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(SupervisorError, "staging_binding"):
                    supervisor._read_state(client.run_dir)
                broken = json.loads(json.dumps(original))
                broken["staging_binding"]["commit_path"] = "relative/commit.json"
                path.write_text(json.dumps(broken) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(SupervisorError, "绝对路径"):
                    supervisor._read_state(client.run_dir)
                path.write_text(json.dumps(original) + "\n", encoding="utf-8")
            finally:
                client.stop(reason="test-complete", status="aborted_prepared")

    # ------------------------------------------------------------------
    # T4.4：commit 回调失败分支（_campaign_run_locked）
    # ------------------------------------------------------------------

    def _run_locked(
        self,
        root: Path,
        campaign_dir: Path,
        plan: dict[str, object],
        commit: object,
        *,
        owner_nonce: str = "a" * 64,
    ) -> tuple[int, dict[str, object]]:
        state_dir = root / "supervisor"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        binding = self._binding(campaign_dir, sequence=1)
        # 本 helper 只有合成总计划和零请求动作；真实出口准入由独立 R15 链验收。
        with runtime_egress_fixtures.offline_campaign_egress():
            return supervisor._campaign_run_locked(
                self._run_arguments(),
                manifest=self._v2_manifest(plan),
                state_dir=state_dir,
                campaign_dir=campaign_dir,
                commit=commit,
                owner_nonce=owner_nonce,
                staging_binding=binding,
            )

    def test_commit_failure_before_commit_is_aborted_prepared_with_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)

            def failing(client: SupervisorClient) -> None:
                client.begin_commit_step("nonce-mismatch")
                client.begin_commit_step("commit-ledger")
                client.begin_commit_step("commit-publish")
                raise supervisor.StagingCommitError("commit-publish", "发布失败")

            returncode, run = self._run_locked(root, campaign_dir, plan, failing)
            self.assertEqual(returncode, 1)
            self.assertEqual(run["status"], "aborted_prepared")
            self.assertEqual(run["reason"], "staging-commit-failed:commit-publish")
            self.assertEqual(run["commit_failure"]["classification"], "no_commit")
            self.assertEqual(run["actions"], [])
            run_dir = Path(str(run["run_dir"]))
            state = supervisor._read_state(run_dir)
            self.assertEqual(state["state"], "aborted_prepared")
            stop = json.loads((run_dir / "stop-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual((stop["event_type"], stop["reason"]), ("aborted_prepared", "staging-commit-failed:commit-publish"))
            self.assertFalse(supervisor._action_started_recorded(run_dir))
            # 分钟账本对 aborted_prepared 分类为 failed，审计闭合。
            report = supervisor._audit_command(run_dir)
            self.assertFalse(report["audit_incomplete"], report["integrity_errors"])
            self.assertEqual(set(report["classification_counts"]) - {"active", "waiting"}, {"failed"})
            # aborted_prepared 的 run 不进入历史链：后续同序号派发不受阻。
            history = supervisor._campaign_run_history(root / "supervisor", CAMPAIGN_ID)
            ordered = supervisor._validate_batched_campaign_history(
                self._v2_manifest(plan), history, campaign_dir=campaign_dir, staging_model=True
            )
            self.assertEqual(ordered, [])

    def test_commit_failure_with_nonce_mismatch_is_rejected_before_any_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)

            def mismatched(client: SupervisorClient) -> None:
                client.begin_commit_step("nonce-mismatch")
                if client.owner_nonce != "b" * 64:
                    raise supervisor.StagingCommitError("nonce-mismatch", "PREPARED nonce 不一致")

            _returncode, run = self._run_locked(root, campaign_dir, plan, mismatched)
            self.assertEqual(run["reason"], "staging-commit-failed:nonce-mismatch")
            self.assertEqual(run["status"], "aborted_prepared")

    def test_activation_failure_after_commit_retries_then_parent_start_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)
            attempts = {"count": 0}
            original = SupervisorClient.activate_committed

            def committing(client: SupervisorClient) -> None:
                client.begin_commit_step("commit-publish")
                formal = campaign_dir / "control" / "vc" / "run-manifests" / "0001-vc-2.json"
                manifest = self._v2_manifest(plan)
                self._write_json(formal, manifest)
                client.begin_commit_step("commit-mark")
                commit = self._commit(
                    client, manifest_sha256=supervisor._sha256(supervisor._canonical(manifest))
                )
                commit_path = Path(str(supervisor._read_state(client.run_dir)["staging_binding"]["commit_path"]))
                self._write_json(commit_path, commit)
                client.begin_commit_step("commit-activate")
                raise OSError("state write failed")

            def broken_activate(self_client: SupervisorClient, commit: dict[str, object]) -> dict[str, object]:
                attempts["count"] += 1
                raise SupervisorError("simulated state write failure")

            with unittest.mock.patch.object(SupervisorClient, "activate_committed", broken_activate):
                returncode, run = self._run_locked(root, campaign_dir, plan, committing)
            self.assertEqual(attempts["count"], supervisor.PARENT_START_ACTIVATE_RETRIES)
            self.assertEqual(returncode, 1)
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["reason"], "parent-start-failed")
            self.assertEqual(run["commit_failure"]["classification"], "committed")
            run_dir = Path(str(run["run_dir"]))
            state = supervisor._read_state(run_dir)
            self.assertEqual(state["state"], "failed")
            diagnostic = supervisor.read_parent_start_failure(run_dir, state)
            self.assertIsNotNone(diagnostic)
            self.assertEqual(diagnostic["failure_kind"], "state-write-failed")
            self.assertFalse(diagnostic["action_started"])
            self.assertEqual(supervisor.classify_prepared_run(run_dir), "committed")
            # 序号已占：run 进入历史链；N+1 后继在 reconciler 写出许可前被父启动失败协议拒绝。
            history = supervisor._campaign_run_history(root / "supervisor", CAMPAIGN_ID)
            self.assertEqual(len(history), 1)
            with self.assertRaisesRegex(SupervisorError, "父启动失败重派"):
                supervisor._validate_batched_campaign_history(
                    self._v2_manifest(plan, sequence=2), history, campaign_dir=campaign_dir, staging_model=True
                )
            # 同序号重派：序号链已含 1，请求 1 会被连续性规则拒绝。
            with self.assertRaisesRegex(SupervisorError, "连续递增"):
                supervisor._validate_batched_campaign_history(
                    self._v2_manifest(plan, sequence=1), history, campaign_dir=campaign_dir, staging_model=True
                )
            # 让 monkeypatch 之外的原方法保持可用（防御性断言）。
            self.assertIs(SupervisorClient.activate_committed, original)

    def test_activation_retry_succeeds_when_second_attempt_writes_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)
            attempts = {"count": 0}
            original = SupervisorClient.activate_committed

            def committing(client: SupervisorClient) -> None:
                client.begin_commit_step("commit-mark")
                commit = self._commit(client)
                commit_path = Path(str(supervisor._read_state(client.run_dir)["staging_binding"]["commit_path"]))
                self._write_json(commit_path, commit)
                client.begin_commit_step("commit-activate")
                raise OSError("first activation failed")

            def flaky_activate(self_client: SupervisorClient, commit: dict[str, object]) -> dict[str, object]:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise SupervisorError("transient")
                return original(self_client, commit)

            with unittest.mock.patch.object(SupervisorClient, "activate_committed", flaky_activate):
                returncode, run = self._run_locked(root, campaign_dir, plan, committing)
            self.assertEqual(returncode, 0)
            self.assertEqual(run["status"], "stopped")
            self.assertEqual(run["reason"], "incremental-noop")
            self.assertNotIn("commit_failure", run)
            self.assertEqual(attempts["count"], 2)

    def test_commit_with_foreign_nonce_is_integrity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)

            def foreign(client: SupervisorClient) -> None:
                client.begin_commit_step("commit-mark")
                commit = self._commit(client, nonce="b" * 64)
                commit_path = Path(str(supervisor._read_state(client.run_dir)["staging_binding"]["commit_path"]))
                self._write_json(commit_path, commit)
                raise RuntimeError("commit written by someone else")

            returncode, run = self._run_locked(root, campaign_dir, plan, foreign)
            self.assertEqual(returncode, 1)
            self.assertEqual(run["status"], "audit-incomplete")
            self.assertEqual(run["reason"], "commit-integrity-mismatch")
            run_dir = Path(str(run["run_dir"]))
            self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
            history = supervisor._campaign_run_history(root / "supervisor", CAMPAIGN_ID)
            with self.assertRaisesRegex(SupervisorError, "完整性异常"):
                supervisor._validate_batched_campaign_history(
                    self._v2_manifest(plan, sequence=2), history, campaign_dir=campaign_dir, staging_model=True
                )

    def test_legacy_campaign_rejects_staging_commit_and_staging_rejects_direct_campaign_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "legacy").mkdir(mode=0o700)
            (root / "staging").mkdir(mode=0o700)
            legacy_dir, legacy_plan = self._staging_campaign(root / "legacy", batch_model=None)
            with self.assertRaisesRegex(SupervisorError, "legacy"):
                self._run_locked(root / "legacy", legacy_dir, legacy_plan, lambda client: None)
            staging_dir, staging_plan = self._staging_campaign(root / "staging")
            state_dir = root / "staging" / "supervisor"
            state_dir.mkdir(mode=0o700)
            with self.assertRaisesRegex(SupervisorError, "compile-and-run-vc-batch"):
                supervisor._campaign_run_locked(
                    self._run_arguments(),
                    manifest=self._v2_manifest(staging_plan, sequence=2),
                    state_dir=state_dir,
                    campaign_dir=staging_dir,
                )

    # ------------------------------------------------------------------
    # T4.4：monitor 对 owner 丢失的三分类
    # ------------------------------------------------------------------

    def test_monitor_finalizes_prepared_without_commit_as_aborted_prepared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, _plan = self._staging_campaign(root)
            spawned = self._spawn_owner(root, campaign_dir, mode="none")
            run_dir = Path(str(spawned["run_dir"]))
            state = self._wait_state(run_dir, {"aborted_prepared"})
            stop = json.loads((run_dir / "stop-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual((stop["event_type"], stop["reason"]), ("aborted_prepared", "prepared-abandoned"))
            self.assertEqual(state["staging_binding"]["staging_attempt"], 1)
            report = supervisor._audit_command(run_dir)
            self.assertFalse(report["audit_incomplete"], report["integrity_errors"])
            events = [json.loads(line)["event_type"] for line in (run_dir / "events.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(events[-1], "aborted-prepared")

    def test_monitor_finalizes_committed_but_unstarted_as_parent_start_failed(self) -> None:
        for mode in ("committed", "activated"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                campaign_dir, _plan = self._staging_campaign(root)
                spawned = self._spawn_owner(root, campaign_dir, mode=mode)
                run_dir = Path(str(spawned["run_dir"]))
                state = self._wait_state(run_dir, {"failed"})
                stop = json.loads((run_dir / "stop-receipt.json").read_text(encoding="utf-8"))
                self.assertEqual((stop["event_type"], stop["reason"]), ("failed", "parent-start-failed"))
                diagnostic = supervisor.read_parent_start_failure(run_dir, state)
                self.assertEqual(diagnostic["failure_kind"], "owner-lost")
                self.assertEqual(diagnostic["owner_nonce"], "a" * 64)
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "committed")
                report = supervisor._audit_command(run_dir)
                self.assertFalse(report["audit_incomplete"], report["integrity_errors"])

    def test_monitor_finalizes_integrity_mismatch_as_audit_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)
            spawned = self._spawn_owner(root, campaign_dir, mode="integrity")
            run_dir = Path(str(spawned["run_dir"]))
            self._wait_state(run_dir, {"audit-incomplete"})
            stop = json.loads((run_dir / "stop-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual((stop["event_type"], stop["reason"]), ("audit-incomplete", "commit-integrity-mismatch"))
            self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
            # 完整性异常的序号视为已占且不可重派：history 校验拒绝任何后继。
            history = supervisor._campaign_run_history(root / "supervisor", CAMPAIGN_ID)
            self.assertEqual(history, [])  # 无清单的孤儿不进历史链……
            # ……但入口扫描（classify）与 reconciler 会把它永久停线；这里断言不会被误判为 no_commit。
            self.assertNotEqual(supervisor.classify_prepared_run(run_dir), "no_commit")
            self.assertEqual(plan["batch_model"], "staging")

    def test_classify_treats_only_same_subject_other_attempt_commit_as_no_commit(self) -> None:
        """外来／被替换的 COMMIT 一律完整性异常；只有同 Campaign／阶段／序号／规范路径的另一 attempt 才算无 COMMIT。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, _plan = self._staging_campaign(root)
            client = self._client(root)
            client.start(prepared=True, staging_binding=self._binding(campaign_dir))
            try:
                run_dir = client.run_dir
                commit_path = Path(str(supervisor._read_state(run_dir)["staging_binding"]["commit_path"]))

                def write_commit(**overrides: object) -> None:
                    payload = dict(
                        campaign_id=CAMPAIGN_ID,
                        sequence=2,
                        phase="VC-2",
                        staging_attempt=2,
                        batch_sha256="5" * 64,
                        manifest_sha256="6" * 64,
                        parent_run_dir=str(root / "supervisor" / f"run-{'c' * 64}"),
                        owner_nonce="c" * 64,
                        ledger_event_ids=[],
                        committed_at_utc=datetime.now(timezone.utc).isoformat(),
                    )
                    payload.update(overrides)
                    commit_path.unlink(missing_ok=True)
                    self._write_json(commit_path, artifacts.build_vc_commit(**payload))

                # 同 Campaign／阶段／序号／规范路径，另一 attempt，nonce 与 run_dir 都不同 → no_commit。
                write_commit()
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "no_commit")
                # 同 attempt 却 nonce／run_dir 都不同：同一 attempt 只能有一个父 run → 完整性异常。
                write_commit(staging_attempt=1)
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
                # Campaign／阶段／序号任一不同（外来 COMMIT 被放到本序号路径）→ 完整性异常。
                write_commit(campaign_id="other-campaign")
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
                write_commit(phase="VC-3")
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
                write_commit(sequence=3)
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
                write_commit(campaign_id="other-campaign", phase="VC-3", sequence=7, staging_attempt=9)
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
                # 只有 nonce 匹配而 run_dir 不匹配（或反之）→ 完整性异常。
                write_commit(staging_attempt=1, owner_nonce=client.owner_nonce)
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
                write_commit(staging_attempt=1, parent_run_dir=str(run_dir))
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
                # binding 指向非规范路径上的一份"合法"COMMIT → 完整性异常。
                commit_path.unlink()
                stray = campaign_dir / "control" / "vc" / "commits" / "stray.json"
                self._write_json(
                    stray,
                    artifacts.build_vc_commit(
                        campaign_id=CAMPAIGN_ID,
                        sequence=2,
                        phase="VC-2",
                        staging_attempt=2,
                        batch_sha256="5" * 64,
                        manifest_sha256="6" * 64,
                        parent_run_dir=str(root / "supervisor" / f"run-{'c' * 64}"),
                        owner_nonce="c" * 64,
                        ledger_event_ids=[],
                        committed_at_utc=datetime.now(timezone.utc).isoformat(),
                    ),
                )
                state_path = run_dir / "state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["staging_binding"]["commit_path"] = str(stray)
                self._write_json(state_path, state)
                self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
            finally:
                client.stop(reason="test-complete", status="aborted_prepared")

    def test_finalize_prepared_run_is_idempotent_and_refuses_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, _plan = self._staging_campaign(root)
            client = self._client(root)
            client.start(prepared=True, staging_binding=self._binding(campaign_dir))
            try:
                with self.assertRaisesRegex(SupervisorError, "owner 仍在线"):
                    supervisor.finalize_prepared_run(client.run_dir, operation="test:finalize")
            finally:
                client.stop(reason="test-complete", status="aborted_prepared")
            outcome = supervisor.finalize_prepared_run(client.run_dir, operation="test:finalize")
            self.assertFalse(outcome["finalized"])
            self.assertEqual(outcome["state"], "aborted_prepared")

    # ------------------------------------------------------------------
    # T4.4：history 过滤、COMMIT 要求与旧模型链
    # ------------------------------------------------------------------

    def _legacy_history_fixture(self, root: Path) -> tuple[Path, dict[str, object], list[tuple[dict[str, object], dict[str, object], Path]]]:
        """模拟 v14 那样的旧模型链：run 目录只有 state／manifest，没有 COMMIT。"""

        campaign_dir, plan = self._staging_campaign(root, batch_model=None)
        state_dir = root / "supervisor"
        state_dir.mkdir(mode=0o700)
        history: list[tuple[dict[str, object], dict[str, object], Path]] = []
        for sequence in (1, 2):
            manifest = self._v2_manifest(plan, sequence=sequence)
            run_dir = state_dir / f"run-{sequence:064d}"
            run_dir.mkdir(mode=0o700)
            now = time.time()
            state = {
                "schema_version": supervisor.STATE_SCHEMA,
                "supervisor_schema_version": supervisor.SCHEMA_VERSION,
                "campaign_id": CAMPAIGN_ID,
                "phase": "VC-2",
                "owner_pid": 1,
                "owner_nonce": f"{sequence:064d}",
                "started_at_utc": supervisor._epoch_to_utc(now - 10),
                "started_at_epoch": now - 10,
                "started_monotonic_ns": 1,
                "deadline_at_epoch": now + 100,
                "deadline_monotonic_ns": 2,
                "heartbeat_seconds": 1,
                "watchdog_timeout_seconds": 2,
                "ledger_interval_seconds": 1,
                "state": "stopped",
                "terminate_owner": False,
                "campaign_started_at_epoch": now - 10,
                "predecessor_run_dir": None,
                "predecessor_state_sha256": None,
                "terminal_at_utc": supervisor._epoch_to_utc(now - 1),
                "terminal_at_epoch": now - 1,
            }
            self._write_json(run_dir / "state.json", state)
            self._write_json(
                run_dir / "campaign-run-manifest.json",
                {
                    "schema_version": manifest["schema_version"],
                    "manifest_sha256": supervisor._sha256(supervisor._canonical(manifest)),
                    "manifest": manifest,
                },
            )
            history.append((state, manifest, run_dir))
        return campaign_dir, plan, history

    def test_legacy_model_history_does_not_require_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan, _history = self._legacy_history_fixture(root)
            history = supervisor._campaign_run_history(root / "supervisor", CAMPAIGN_ID)
            self.assertEqual(len(history), 2)
            ordered = supervisor._validate_batched_campaign_history(
                self._v2_manifest(plan, sequence=3), history, campaign_dir=campaign_dir
            )
            self.assertEqual([int(item[1]["batch_sequence"]) for item in ordered], [1, 2])
            self.assertEqual(supervisor.campaign_batch_model(campaign_dir), "legacy")

    def test_staging_model_history_requires_commit_for_sequences_after_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)
            # 序号 1 由 VC-0 在 campaign.json 绑定：这里补上首批绑定。
            first_manifest = self._v2_manifest(plan, sequence=1)
            first_path = campaign_dir / "control" / "vc" / "run-manifests" / "0001-vc-2.json"
            self._write_json(first_path, first_manifest)
            campaign = json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8"))
            campaign["vc_control"]["first_campaign_run_manifest"] = {
                "path": "control/vc/run-manifests/0001-vc-2.json",
                "sha256": supervisor._sha256(first_path.read_bytes()),
            }
            self._write_json(campaign_dir / "campaign.json", campaign)
            # 首批 run（无 staging_binding）+ 一个 stopped 但没有 COMMIT 的序号 2 run（伪造历史）。
            state_dir = root / "supervisor"
            state_dir.mkdir(mode=0o700)
            now = time.time()
            base_state = {
                "schema_version": supervisor.STATE_SCHEMA,
                "supervisor_schema_version": supervisor.SCHEMA_VERSION,
                "campaign_id": CAMPAIGN_ID,
                "phase": "VC-2",
                "owner_pid": 1,
                "started_at_utc": supervisor._epoch_to_utc(now - 10),
                "started_at_epoch": now - 10,
                "started_monotonic_ns": 1,
                "deadline_at_epoch": now + 100,
                "deadline_monotonic_ns": 2,
                "heartbeat_seconds": 1,
                "watchdog_timeout_seconds": 2,
                "ledger_interval_seconds": 1,
                "terminate_owner": False,
                "campaign_started_at_epoch": now - 10,
                "predecessor_run_dir": None,
                "predecessor_state_sha256": None,
                "terminal_at_utc": supervisor._epoch_to_utc(now - 1),
                "terminal_at_epoch": now - 1,
                "state": "stopped",
            }
            for sequence, nonce in ((1, "1" * 64), (2, "2" * 64)):
                run_dir = state_dir / f"run-{nonce}"
                run_dir.mkdir(mode=0o700)
                state = {**base_state, "owner_nonce": nonce}
                if sequence == 2:
                    state["staging_binding"] = self._binding(campaign_dir, sequence=2)
                manifest = first_manifest if sequence == 1 else self._v2_manifest(plan, sequence=2)
                self._write_json(run_dir / "state.json", state)
                self._write_json(
                    run_dir / "campaign-run-manifest.json",
                    {
                        "schema_version": manifest["schema_version"],
                        "manifest_sha256": supervisor._sha256(supervisor._canonical(manifest)),
                        "manifest": manifest,
                    },
                )
            history = supervisor._campaign_run_history(state_dir, CAMPAIGN_ID)
            self.assertEqual(len(history), 2)
            with self.assertRaisesRegex(SupervisorError, "无 COMMIT 却已 stopped"):
                supervisor._validate_batched_campaign_history(
                    self._v2_manifest(plan, sequence=3), history, campaign_dir=campaign_dir
                )
            # 序号 2 run 缺 staging_binding 也拒绝。
            run_dir = state_dir / f"run-{'2' * 64}"
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            state.pop("staging_binding")
            self._write_json(run_dir / "state.json", state)
            history = supervisor._campaign_run_history(state_dir, CAMPAIGN_ID)
            with self.assertRaisesRegex(SupervisorError, "缺少 staging_binding"):
                supervisor._validate_batched_campaign_history(
                    self._v2_manifest(plan, sequence=3), history, campaign_dir=campaign_dir
                )

    def test_staging_model_first_batch_binding_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)
            manifest = self._v2_manifest(plan, sequence=1)
            with self.assertRaisesRegex(SupervisorError, "首批队列清单绑定"):
                supervisor._validate_first_batch_binding(campaign_dir, manifest)

    def test_parent_start_successor_rejects_action_failed_disguise(self) -> None:
        """伪装：state failed + stop reason parent-start-failed 但没有诊断／COMMIT，必须失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)
            prior_manifest = self._v2_manifest(plan, sequence=2)
            run_dir = root / "supervisor" / f"run-{'a' * 64}"
            run_dir.mkdir(parents=True, mode=0o700)
            now = time.time()
            state = {
                "schema_version": supervisor.STATE_SCHEMA,
                "supervisor_schema_version": supervisor.SCHEMA_VERSION,
                "campaign_id": CAMPAIGN_ID,
                "phase": "VC-2",
                "owner_pid": 1,
                "owner_nonce": "a" * 64,
                "started_at_utc": supervisor._epoch_to_utc(now - 10),
                "started_at_epoch": now - 10,
                "started_monotonic_ns": 1,
                "deadline_at_epoch": now + 100,
                "deadline_monotonic_ns": 2,
                "heartbeat_seconds": 1,
                "watchdog_timeout_seconds": 2,
                "ledger_interval_seconds": 1,
                "terminate_owner": False,
                "campaign_started_at_epoch": now - 10,
                "predecessor_run_dir": None,
                "predecessor_state_sha256": None,
                "terminal_at_utc": supervisor._epoch_to_utc(now - 1),
                "terminal_at_epoch": now - 1,
                "state": "failed",
                "staging_binding": self._binding(campaign_dir, sequence=2),
            }
            self._write_json(run_dir / "state.json", state)
            stop = {
                "schema_version": supervisor.STOP_SCHEMA,
                "event_type": "failed",
                "reason": "parent-start-failed",
                "detected_at_utc": supervisor._epoch_to_utc(now - 1),
                "detected_at_epoch": now - 1,
                "owner_pid": 1,
                "owner_nonce": "a" * 64,
                "campaign_id": CAMPAIGN_ID,
                "phase": "VC-2",
            }
            stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
            self._write_json(run_dir / "stop-receipt.json", stop)
            with self.assertRaisesRegex(SupervisorError, "parent-start-failure 诊断"):
                supervisor._validate_batched_parent_start_redispatch_successor(
                    state, prior_manifest, run_dir, self._v2_manifest(plan, sequence=3), campaign_dir=campaign_dir
                )
            # 非 parent-start-failed 的 stop reason 让协议返回 False（交其他协议）。
            other = dict(stop)
            other["reason"] = "action-failed:item-a"
            other.pop("receipt_sha256")
            other["receipt_sha256"] = supervisor._sha256(supervisor._canonical(other))
            self._write_json(run_dir / "stop-receipt.json", other)
            self.assertFalse(
                supervisor._validate_batched_parent_start_redispatch_successor(
                    state, prior_manifest, run_dir, self._v2_manifest(plan, sequence=3), campaign_dir=campaign_dir
                )
            )

    def test_parent_finalize_successor_rejects_disguise_without_segment_facts(self) -> None:
        """伪装：state failed + stop reason parent-finalize-lost 但批次不是单动作恢复段 run／无绑定，必须失败关闭；
        stop-receipt 带 action_outputs_sha256 也拒绝；其他 reason 让协议返回 False。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)
            prior_manifest = self._v2_manifest(plan, sequence=2)
            run_dir = root / "supervisor" / f"run-{'a' * 64}"
            run_dir.mkdir(parents=True, mode=0o700)
            now = time.time()
            state = {
                "schema_version": supervisor.STATE_SCHEMA,
                "supervisor_schema_version": supervisor.SCHEMA_VERSION,
                "campaign_id": CAMPAIGN_ID,
                "phase": "VC-2",
                "owner_pid": 1,
                "owner_nonce": "a" * 64,
                "started_at_utc": supervisor._epoch_to_utc(now - 10),
                "started_at_epoch": now - 10,
                "started_monotonic_ns": 1,
                "deadline_at_epoch": now + 100,
                "deadline_monotonic_ns": 2,
                "heartbeat_seconds": 1,
                "watchdog_timeout_seconds": 2,
                "ledger_interval_seconds": 1,
                "terminate_owner": False,
                "campaign_started_at_epoch": now - 10,
                "predecessor_run_dir": None,
                "predecessor_state_sha256": None,
                "terminal_at_utc": supervisor._epoch_to_utc(now - 1),
                "terminal_at_epoch": now - 1,
                "state": "failed",
                "staging_binding": self._binding(campaign_dir, sequence=2),
            }
            self._write_json(run_dir / "state.json", state)
            stop = {
                "schema_version": supervisor.STOP_SCHEMA,
                "event_type": "failed",
                "reason": supervisor.PARENT_FINALIZE_LOST_REASON,
                "detected_at_utc": supervisor._epoch_to_utc(now - 1),
                "detected_at_epoch": now - 1,
                "owner_pid": 1,
                "owner_nonce": "a" * 64,
                "campaign_id": CAMPAIGN_ID,
                "phase": "VC-2",
            }
            stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
            self._write_json(run_dir / "stop-receipt.json", stop)
            # 前序批次不是单动作恢复段 run（普通 v2 批次）→ 判定不成立，失败关闭。
            with self.assertRaisesRegex(SupervisorError, "恢复段判定不成立"):
                supervisor._validate_batched_parent_finalize_redispatch_successor(
                    state, prior_manifest, run_dir, self._v2_manifest(plan, sequence=3), campaign_dir=campaign_dir
                )
            # 非 parent-finalize-lost 的 stop reason → False（交其他协议）。
            other = dict(stop)
            other["reason"] = "action-failed:item-a"
            other.pop("receipt_sha256")
            other["receipt_sha256"] = supervisor._sha256(supervisor._canonical(other))
            self._write_json(run_dir / "stop-receipt.json", other)
            self.assertFalse(
                supervisor._validate_batched_parent_finalize_redispatch_successor(
                    state, prior_manifest, run_dir, self._v2_manifest(plan, sequence=3), campaign_dir=campaign_dir
                )
            )
            # reconciler 同一分类：伪装同样失败关闭（先核 COMMIT，再核恢复段判定）。
            self._write_json(run_dir / "stop-receipt.json", stop)
            with self.assertRaisesRegex(reconciler.ReconcilerError, "COMMIT 无效或缺失|恢复段判定不成立"):
                reconciler._staging_run_facts(run_dir, state, supervisor.PARENT_FINALIZE_LOST_REASON, None, campaign_dir=campaign_dir)

    def test_timing_closeout_pauses_ledger_for_parent_start_failure(self) -> None:
        """P4 让账本进入 recovery_required（根因 parent-start.failed），强制 reconciler 先行。"""

        from tools.official_client_capture import codex_upgrade_root_cause as root_cause
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, plan = self._staging_campaign(root)
            ledger_dir = root / "timing-ledger"
            timing_ledger.create_ledger(
                ledger_dir,
                upgrade_id="staging-upgrade",
                baseline_version="0.151.0",
                target_version="0.154.0",
                campaign_purpose="validation_only",
                evidence_decision="recapture",
            )
            timing_ledger.append_event(ledger_dir, event_id="vc0-completed", phase="VC-0", event_type="stage_completed", next_action="进入 VC-2")
            timing_ledger.append_event(ledger_dir, event_id="vc2-started", phase="VC-2", event_type="stage_started", next_action="派发")
            campaign = json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8"))
            campaign["campaign_purpose"] = "validation_only"
            campaign["baseline_version"] = "0.151.0"
            campaign["control_receipts"] = {
                "upgrade_timing": {
                    "ledger_dir": str(ledger_dir),
                    "ledger_plan_sha256": supervisor._sha256((ledger_dir / "ledger.json").read_bytes()),
                    "upgrade_id": "staging-upgrade",
                }
            }
            self._write_json(campaign_dir / "campaign.json", campaign)
            manifest = self._v2_manifest(plan, sequence=2)
            closeout = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir, manifest, failed_action_id="commit-activate", failure_class="parent-start-failed"
            )
            self.assertEqual(closeout["ledger_status"], "recovery_required")
            expected = root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="parent-start.failed",
                failed_step="commit-activate",
                stable_dimensions={"phase": "VC-2"},
            )
            self.assertEqual(closeout["root_cause_id"], expected)
            self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "recovery_required")
            # 幂等：同一根因再次收口返回 idempotent。
            again = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir, manifest, failed_action_id="commit-activate", failure_class="parent-start-failed"
            )
            self.assertTrue(again["idempotent"])


if __name__ == "__main__":
    unittest.main()
