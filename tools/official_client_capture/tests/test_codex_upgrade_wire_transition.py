"""wire transition 两阶段链、evaluation epoch 链与 attempt 身份裁定。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip
from tools.official_client_capture import codex_upgrade_wire_transition as wt

TOOL_ROOT = Path(__file__).resolve().parents[1]


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
    path.chmod(0o600)


class WireTransitionFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.campaign_dir = root / "campaign"
        self.policy = tip.load_policy()
        self.identity = codex_upgrade._tool_identity(include_git=False)
        self.manifest = {"campaign_id": "c1", "campaign_mode": "formal", "target_version": "0.154.0", "tool_identity": dict(self.identity)}
        _write_json(self.campaign_dir / "campaign.json", self.manifest)
        self.jobs = ["official-core", "official-compact", "official-relay-http-response"]

    def mutated_identity(self, *paths: str) -> dict:
        entries = [dict(e, sha256="0" * 64) if e["path"] in paths else dict(e) for e in self.identity["entries"]]
        v2 = tip.compute_identity_v2(self.policy, TOOL_ROOT, entries)
        return {**self.identity, "entries": entries, **{k: v2[k] for k in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "policy_sha256")}, "files_sha256": codex_upgrade._fingerprint({"entries": entries})}


class WireTransitionTests(unittest.TestCase):
    def test_intent_final_chain_moves_effective_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = WireTransitionFixture(Path(directory).resolve())
            effective = wt.effective_wire_identity(fixture.campaign_dir, fixture.manifest)
            self.assertEqual(effective["source"], "manifest")
            self.assertEqual(effective["wire_producer_sha256"], fixture.identity["wire_producer_sha256"])
            current = fixture.mutated_identity("run_official_relay_scenario.sh")
            path_job_map = {"run_official_relay_scenario.sh": {"official-relay-http-response"}}
            preview = wt.build_intent_preview(fixture.campaign_dir, fixture.manifest, current_identity=current, policy=fixture.policy, path_job_map=path_job_map, planned_job_ids=fixture.jobs)
            self.assertEqual(preview["affected_job_ids"], ["official-relay-http-response"])
            self.assertFalse(preview["all_jobs_affected"])
            with self.assertRaisesRegex(wt.WireTransitionError, "批准摘要"):
                wt.approve_intent(fixture.campaign_dir, preview, "0" * 64)
            wt.approve_intent(fixture.campaign_dir, preview, preview["review_sha256"])
            pending = wt.effective_wire_identity(fixture.campaign_dir, fixture.manifest)
            self.assertEqual(pending["wire_producer_sha256"], fixture.identity["wire_producer_sha256"])
            self.assertEqual(pending["pending_intent"]["to_wire_producer_sha256"], current["wire_producer_sha256"])
            with self.assertRaisesRegex(wt.WireTransitionError, "尚未 final"):
                wt.build_intent_preview(fixture.campaign_dir, fixture.manifest, current_identity=current, policy=fixture.policy, path_job_map=path_job_map, planned_job_ids=fixture.jobs)
            attempt = {"attempt_id": "A2", "results": [{"id": "official-relay-http-response", "status": "failed"}]}
            with self.assertRaisesRegex(wt.WireTransitionError, "未 complete"):
                wt.build_final(fixture.campaign_dir, fixture.manifest, attempt)
            attempt["results"][0]["status"] = "complete"
            wt.build_final(fixture.campaign_dir, fixture.manifest, attempt)
            final = wt.effective_wire_identity(fixture.campaign_dir, fixture.manifest)
            self.assertEqual(final["source"], "final-01")
            self.assertEqual(final["wire_producer_sha256"], current["wire_producer_sha256"])
            self.assertIsNone(final["pending_intent"])
            # 篡改 intent 会被自摘要拦住
            intent_path = fixture.campaign_dir / "control" / "wire-transitions" / "intent-01.json"
            payload = json.loads(intent_path.read_text("utf-8"))
            payload["affected_job_ids"] = fixture.jobs
            intent_path.write_text(json.dumps(payload, sort_keys=True), "utf-8")
            with self.assertRaisesRegex(wt.WireTransitionError, "自摘要不一致"):
                wt.effective_wire_identity(fixture.campaign_dir, fixture.manifest)

    def test_all_jobs_affected_or_orchestrator_closure_change_refuses_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = WireTransitionFixture(Path(directory).resolve())
            current = fixture.mutated_identity("capture.py")
            preview = wt.build_intent_preview(fixture.campaign_dir, fixture.manifest, current_identity=current, policy=fixture.policy, path_job_map={}, planned_job_ids=fixture.jobs)
            self.assertEqual(preview["unmapped_paths"], ["capture.py"])
            self.assertTrue(preview["all_jobs_affected"])
            with self.assertRaisesRegex(wt.WireTransitionError, "拒签 intent"):
                wt.approve_intent(fixture.campaign_dir, preview, preview["review_sha256"])
            same = fixture.identity
            with self.assertRaisesRegex(wt.WireTransitionError, "无需 transition"):
                wt.build_intent_preview(fixture.campaign_dir, fixture.manifest, current_identity=same, policy=fixture.policy, path_job_map={}, planned_job_ids=fixture.jobs)
            policy_changed = dict(current, policy_sha256="9" * 64)
            with self.assertRaisesRegex(wt.WireTransitionError, "策略变化"):
                wt.build_intent_preview(fixture.campaign_dir, fixture.manifest, current_identity=policy_changed, policy=fixture.policy, path_job_map={}, planned_job_ids=fixture.jobs)

    def test_evaluation_epoch_chain_appends_and_detects_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = WireTransitionFixture(Path(directory).resolve())
            attempt_root = fixture.campaign_dir / "official" / "attempts" / "A1"
            attempt_root.mkdir(parents=True, mode=0o700)
            self.assertEqual(wt.current_evidence_semantics(attempt_root, fixture.manifest), fixture.identity["evidence_semantics_sha256"])
            with self.assertRaisesRegex(wt.WireTransitionError, "无需追加"):
                wt.append_epoch(attempt_root, fixture.manifest, current_identity=fixture.identity, reason="none")
            first = fixture.mutated_identity("codex_upgrade_evidence_labels_0_154_0.json")
            wt.append_epoch(attempt_root, fixture.manifest, current_identity=first, reason="标签修复")
            second = fixture.mutated_identity("codex_upgrade_evidence_labels_0_154_0.json", "relay_extract.py")
            wt.append_epoch(attempt_root, fixture.manifest, current_identity=second, reason="解析修复")
            chain = wt.load_epochs(attempt_root)
            self.assertEqual([e["index"] for e in chain], [1, 2])
            self.assertEqual(wt.current_evidence_semantics(attempt_root, fixture.manifest), second["evidence_semantics_sha256"])
            (attempt_root / "evaluation-epoch-01.json").unlink()
            with self.assertRaisesRegex(wt.WireTransitionError, "缺少 evaluation-epoch-01"):
                wt.load_epochs(attempt_root)

    def test_verdict_uses_historical_copy_or_falls_back_to_v1_equality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = WireTransitionFixture(root)
            attempt_root = fixture.campaign_dir / "official" / "attempts" / "A1"
            started = datetime(2026, 9, 14, 23, 20, tzinfo=timezone.utc)
            _write_json(attempt_root / "attempt.json", {"attempt_id": "A1", "started_at_utc": _iso(started)})
            control = root / "control"
            control.mkdir(mode=0o700)
            copy_root = control / f"managed-tools-backup-before-{fixture.identity['files_sha256'][:12]}-x"
            shutil.copytree(TOOL_ROOT, copy_root, ignore=shutil.ignore_patterns("__pycache__", "tests", "versions"))
            _write_json(control / "codex-0154-supervisor-enable-a.json", {"status": "passed", "created_at_utc": _iso(started - timedelta(hours=1)), "tool_files_sha256": fixture.identity["files_sha256"], "rollback_backup": str(control / "older")})
            _write_json(control / "codex-0154-supervisor-enable-b.json", {"status": "passed", "created_at_utc": _iso(started + timedelta(hours=1)), "tool_files_sha256": "1" * 64, "rollback_backup": str(copy_root)})
            verdict = wt.verdict_official_attempt_identity(fixture.campaign_dir, "A1", control_root=control, current_identity=fixture.identity, policy=fixture.policy)
            self.assertEqual(verdict["verdict"], "equal", verdict)
            self.assertEqual(verdict["basis"], "historical_copy_policy_v2")
            self.assertEqual(verdict["problems"], [])
            # 当前 wire 变化 → different
            changed = fixture.mutated_identity("run_official_relay_scenario.sh")
            self.assertEqual(wt.verdict_official_attempt_identity(fixture.campaign_dir, "A1", control_root=control, current_identity=changed, policy=fixture.policy)["verdict"], "different")
            # 当前只改 control 文件 → 仍 equal（wire 不变）
            control_only = fixture.mutated_identity("codex_upgrade_supervisor.py")
            self.assertEqual(wt.verdict_official_attempt_identity(fixture.campaign_dir, "A1", control_root=control, current_identity=control_only, policy=fixture.policy)["verdict"], "equal")
            # 无副本：只有 v1 整树相等才 equal
            (control / "codex-0154-supervisor-enable-b.json").unlink()
            self.assertEqual(wt.verdict_official_attempt_identity(fixture.campaign_dir, "A1", control_root=control, current_identity=fixture.identity, policy=fixture.policy)["basis"], "v1_files_sha256_equal")
            no_copy = wt.verdict_official_attempt_identity(fixture.campaign_dir, "A1", control_root=control, current_identity=control_only, policy=fixture.policy)
            self.assertEqual((no_copy["verdict"], no_copy["basis"]), ("different", "no_copy_and_files_differ"))


if __name__ == "__main__":
    unittest.main()
