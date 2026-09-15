"""计划身份校验的策略 v2 分支：control 放行、evidence 需 epoch、wire 需 intent/final、策略变化拒绝。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip
from tools.official_client_capture import codex_upgrade_wire_transition as wt

TOOL_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
    path.chmod(0o600)


class PolicyV2IdentityFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.campaign_dir = root / "campaign"
        self.policy = tip.load_policy()
        self.identity = codex_upgrade._tool_identity(include_git=False)
        package = {"asset_sha256": "a" * 64, "code_mode_host_sha256": "b" * 64}
        self.manifest = {
            "campaign_id": "c1",
            "campaign_mode": "formal",
            "target_version": "0.154.0",
            "target_sha256": "c" * 64,
            "configuration": {"target_source": str(root / "source"), "target_package": str(root / "package.tar.zst")},
            "official_identity": {"source_tree_sha256": "d" * 64, "cargo_lock_sha256": None, "package": package},
            "tool_identity": dict(self.identity),
        }
        (root / "source").mkdir(mode=0o700)
        _write_json(self.campaign_dir / "campaign.json", self.manifest)
        self.attempt_root = self.campaign_dir / "official" / "attempts" / "A1"
        self.attempt = {"attempt_id": "A1", "campaign_id": "c1", "phase": "official", "status": "awaiting_receipts", "results": [{"id": "official-core", "status": "complete"}]}
        _write_json(self.attempt_root / "attempt.json", self.attempt)

    def mutated(self, *paths: str, policy_sha256: str | None = None) -> dict:
        entries = [dict(e, sha256="0" * 64) if e["path"] in paths else dict(e) for e in self.identity["entries"]]
        v2 = tip.compute_identity_v2(self.policy, TOOL_ROOT, entries)
        components = codex_upgrade._tool_component_identities(entries)
        identity = {
            **self.identity,
            "entries": entries,
            "components": components["components"],
            "component_identity_sha256": codex_upgrade._fingerprint(components),
            **codex_upgrade._tool_identity_sides(entries),
            **{k: v2[k] for k in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "policy_sha256")},
            "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
        }
        if policy_sha256 is not None:
            identity["policy_sha256"] = policy_sha256
        return identity

    def verify(self, current: dict, *, operation: str | None, with_attempt: bool = True) -> dict | None:
        with mock.patch.object(codex_upgrade, "_tool_identity", return_value=current), mock.patch.object(codex_upgrade, "_verify_control_receipts", return_value=None), mock.patch.object(codex_upgrade, "_directory_tree_digest", return_value="d" * 64), mock.patch.object(codex_upgrade, "_verify_codex_package", return_value=self.manifest["official_identity"]["package"]):
            return codex_upgrade._verify_plan_identity(
                self.campaign_dir,
                self.manifest,
                operation=operation,
                attempt_root=self.attempt_root if with_attempt else None,
                attempt=self.attempt if with_attempt else None,
            )


class PolicyV2IdentityTests(unittest.TestCase):
    def test_unchanged_tree_and_control_only_changes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PolicyV2IdentityFixture(Path(directory).resolve())
            self.assertIsNone(fixture.verify(fixture.identity, operation="capture-official-seal"))
            result = fixture.verify(fixture.mutated("codex_upgrade_supervisor.py"), operation="capture-official-seal")
            self.assertEqual(result["kind"], "policy_v2_identity")
            self.assertEqual(result["wire_status"], "equal")
            self.assertEqual(result["changed_components"], ["control"])
            self.assertFalse(result["evidence_semantics_changed"])
            drift_ledger = json.loads((fixture.campaign_dir / "tool-evaluation-drift.json").read_text("utf-8"))
            self.assertEqual(drift_ledger["records"][0]["changed_files"], ["codex_upgrade_supervisor.py"])

    def test_evidence_semantics_change_requires_epoch_for_evaluation_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PolicyV2IdentityFixture(Path(directory).resolve())
            current = fixture.mutated("codex_upgrade_evidence_labels_0_154_0.json")
            run = fixture.verify(current, operation="capture-run")
            self.assertTrue(run["evidence_semantics_changed"])
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "追加 evaluation epoch"):
                fixture.verify(current, operation="capture-official-seal")
            wt.append_epoch(fixture.attempt_root, fixture.manifest, current_identity=current, reason="标签修复")
            sealed = fixture.verify(current, operation="capture-official-seal")
            self.assertFalse(sealed["evidence_semantics_changed"])

    def test_wire_change_needs_intent_then_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PolicyV2IdentityFixture(Path(directory).resolve())
            current = fixture.mutated("run_official_relay_scenario.sh")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "wire producer 身份漂移"):
                fixture.verify(current, operation="capture-run")
            preview = wt.build_intent_preview(fixture.campaign_dir, fixture.manifest, current_identity=current, policy=fixture.policy, path_job_map={"run_official_relay_scenario.sh": {"official-core"}}, planned_job_ids=["official-core", "official-compact"])
            wt.approve_intent(fixture.campaign_dir, preview, preview["review_sha256"])
            run = fixture.verify(current, operation="capture-run")
            self.assertEqual(run["kind"], "wire_producer_transition")
            self.assertEqual(run["affected_job_ids"], ["official-core"])
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "尚未 final"):
                fixture.verify(current, operation="capture-official-seal")
            wt.build_final(fixture.campaign_dir, fixture.manifest, fixture.attempt)
            sealed = fixture.verify(current, operation="capture-official-seal")
            self.assertEqual(sealed["kind"], "policy_v2_identity")
            self.assertEqual(sealed["effective_source"], "final-01")
            # 与 intent 无关的另一次 wire 变化仍被拒绝
            other = fixture.mutated("run_official_relay_scenario.sh", "capture.py")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "wire producer 身份漂移"):
                fixture.verify(other, operation="capture-run")

    def test_policy_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PolicyV2IdentityFixture(Path(directory).resolve())
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "策略已变化"):
                fixture.verify(fixture.mutated("codex_upgrade_supervisor.py", policy_sha256="9" * 64), operation=None)

    def test_v1_campaign_keeps_v1_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PolicyV2IdentityFixture(Path(directory).resolve())
            v1 = {k: v for k, v in fixture.identity.items() if k not in {"policy_version", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256"}}
            fixture.manifest["tool_identity"] = v1
            _write_json(fixture.campaign_dir / "campaign.json", fixture.manifest)
            self.assertFalse(codex_upgrade._is_policy_v2_identity(v1))
            # v1 语义：监督器属评估侧，只留痕放行（返回 None），不会进入 v2 分支。
            result = fixture.verify(fixture.mutated("codex_upgrade_supervisor.py"), operation=None)
            self.assertIsNone(result)
            self.assertTrue((fixture.campaign_dir / "tool-evaluation-drift.json").is_file())


if __name__ == "__main__":
    unittest.main()
