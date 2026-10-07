"""Claude 编排器骨架合同测试（Claude 升级控制面设计 B1-09）。

* 服务面合同（两个适配器同一组用例）在 ``test_orchestrator_surface_contract``。
* Claude 总计划读取与拒绝、计时账本归属、v3 两根工具身份、以真实 Claude 总计划获取并释放租约。
* ``claude_upgrade.py --help`` 列出设计规定的全部子命令；Claude 编排器不导入 ``codex_upgrade``；共享模块
  ``upgrade_control_plane`` 不按客户端分支；Claude 身份两根五摘要可算、没有按默认层归类的文件。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import codex_upgrade

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
# 设计 B1-09 规定的子命令（与 Codex 同名；带「|」的拆成两个子命令或一个位置参数）
DESIGN_SUBCOMMANDS = (
    "plan", "freeze", "import-predecessor-evidence", "compile-and-run-vc-batch", "capture-official", "classify",
    "prepare-profile", "stage-profile", "plan-candidate-gates", "record-candidate-build", "revision-open",
    "invalidate-candidate", "capture-candidate", "compare", "accept", "evaluation-recover", "evaluation-epoch",
    "tool-evolution", "tool-evolution-status", "deadline-extend", "request-budget-extend", "campaign-resume",
    "campaign-abandon", "accounting-resolve", "environment-isolate", "evidence-conflict-quarantine",
    "harden-evidence-permissions", "wire-transition-intent", "wire-transition-final", "reconcile-supervisor-run",
    "reconcile-attempt", "account-sealed-official", "account-sealed-candidate", "resume", "deliver-candidate",
    "delivery-stage-invalidate", "status",
)


def _claude_campaign(root: Path, **overrides) -> Path:
    """在 ``<root>/evidence/campaigns/claude-c1`` 写一份合法的 Claude Campaign 总计划（含一个计划期输入）。"""

    campaign = root / "evidence" / "campaigns" / "claude-c1"
    (campaign / "inputs").mkdir(parents=True, mode=0o700)
    rules = campaign / "inputs" / "rules.json"
    rules.write_text('{"rules": []}\n', encoding="utf-8")
    manifest = {
        "schema_version": "official-client-upgrade-campaign/v1", "client": "claude", "campaign_id": "claude-c1",
        "created_at_utc": "2026-10-07T00:00:00Z", "campaign_mode": "formal", "campaign_purpose": "validation_only",
        "baseline_version": "2.1.226", "target_version": "2.1.245", "target_sha256": "a" * 64,
        "official_identity": {"npm": {}, "gcs": {}, "binary_sha256": "b" * 64, "binary_equivalence": "npm=gcs"},
        "baseline_identity": {}, "tool_identity": {"client": "claude"}, "control_receipts": {},
        "inputs": {"rules": {"path": "inputs/rules.json", "sha256": codex_upgrade.file_sha256(rules)}},
        "configuration": {"platform": "linux/arm64", "entrypoint": "claude-cli", "privacy_mode": "default",
                          "oauth_org_id": "org-1", "model_catalog_sha256": "c" * 64},
        "required_rules": [], "coverage_plan": {}, "jobs": [], "vc_control": {}, "predecessor": None,
    }
    manifest.update(overrides)
    raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    (campaign / "campaign.json").write_bytes(raw)
    (campaign / "campaign.json").chmod(0o600)
    (campaign / "campaign.sha256").write_text(__import__("hashlib").sha256(raw).hexdigest() + "\n", encoding="utf-8")
    return campaign


class ClaudeCampaignInfrastructureTests(unittest.TestCase):
    """B1-09 续作：Claude 总计划读取、计时账本绑定、工具身份与租约。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_load_valid_plan_and_formal_gate(self) -> None:
        campaign = _claude_campaign(self.root)
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.assertEqual((manifest["client"], manifest["campaign_id"]), ("claude", "claude-c1"))
        self.assertEqual(claude_upgrade._require_formal_campaign(campaign)["campaign_mode"], "formal")
        preflight = _claude_campaign(self.root / "p", campaign_mode="preflight_only")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "preflight_only"):
            claude_upgrade._require_formal_campaign(preflight)

    def test_plan_rejections(self) -> None:
        cases = {
            "Codex 合同": ({"schema_version": "codex-upgrade-campaign/v3"}, "不是 Claude Campaign"),
            "client 不符": ({"client": "codex"}, "不是 Claude Campaign"),
            "缺字段": ({"coverage_plan": None}, None),
            "平台不符": ({"configuration": {"platform": "linux/amd64"}}, "linux/arm64"),
            # B2-09 起前序绑定只接受 null 或 {mode, campaign_id, campaign_identity_sha256}（合法形状见 test_claude_upgrade_predecessor）。
            "前序绑定": ({"predecessor": {"campaign_id": "x"}}, "predecessor 必须为 null"),
            "工具身份归属": ({"tool_identity": {}}, "工具身份"),
        }
        for index, (label, (overrides, message)) in enumerate(cases.items()):
            with self.subTest(label=label):
                campaign = _claude_campaign(self.root / f"case-{index}", **overrides)
                if overrides.get("coverage_plan", 0) is None:
                    payload = json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))
                    payload.pop("coverage_plan")
                    raw = json.dumps(payload).encode("utf-8")
                    (campaign / "campaign.json").write_bytes(raw)
                    (campaign / "campaign.sha256").write_text(__import__("hashlib").sha256(raw).hexdigest() + "\n", encoding="utf-8")
                    message = "缺少字段"
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, message):
                    claude_upgrade.load_campaign_manifest(campaign)
        drifted = _claude_campaign(self.root / "drift")
        (drifted / "inputs" / "rules.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "摘要漂移"):
            claude_upgrade.load_campaign_manifest(drifted)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "control epoch"):
            claude_upgrade.load_campaign_manifest(_claude_campaign(self.root / "epoch"), _control_epoch_bootstrap=True)

    def test_timing_ledger_binding_must_be_claude(self) -> None:
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing

        fields = dict(baseline_version="2.1.226", target_version="2.1.245", campaign_purpose="validation_only",
                      evidence_decision="recapture")
        for client, expect_ok in (("claude", True), ("codex", False)):
            with self.subTest(client=client):
                ledger_dir = self.root / f"ledger-{client}"
                timing.create_ledger(ledger_dir, upgrade_id=f"u-{client}", client=client, **fields)
                binding = {"upgrade_timing": {"ledger_dir": str(ledger_dir), "ledger_plan_sha256": codex_upgrade.file_sha256(ledger_dir / "ledger.json")}}
                manifest = {"control_receipts": binding}
                if expect_ok:
                    self.assertEqual(claude_upgrade._campaign_timing_ledger_dir(self.root, manifest), ledger_dir.resolve())
                else:
                    with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不属于 Claude"):
                        claude_upgrade._campaign_timing_ledger_dir(self.root, manifest)

    def test_tool_identity_is_v3_two_root(self) -> None:
        identity = claude_upgrade._tool_identity(include_git=False)
        computed = claude_identity.compute_identity(claude_identity.load_policy(), REPOSITORY_ROOT)
        self.assertEqual(identity["client"], "claude")
        self.assertEqual(identity["files_sha256"], claude_upgrade._fingerprint({"entries": identity["entries"]}))
        self.assertEqual(identity["files_sha256"], computed["tool_files_sha256"])
        for field in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "policy_sha256"):
            self.assertEqual(identity[field], computed[field])
        roots = {entry["path"].split("/", 1)[0] for entry in identity["entries"]}
        self.assertEqual(roots, {"official_client_capture", "official_client_control"})

    def test_lease_acquires_against_claude_plan(self) -> None:
        from tools.official_client_capture import incremental_recovery

        campaign = _claude_campaign(self.root)
        deadline = incremental_recovery.WallClockDeadline(30)
        deadline.heartbeat_seconds = 1
        lease = claude_upgrade.CampaignLease(
            campaign, phase="official", candidate_id=None, deadline=deadline, command="capture-official", campaign_id="claude-c1",
        )
        lease.acquire()
        try:
            payload = json.loads((campaign / claude_upgrade.CAMPAIGN_LEASE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual((payload["state"], payload["campaign_id"]), ("active", "claude-c1"))
        finally:
            lease.release()
        self.assertNotEqual(json.loads((campaign / claude_upgrade.CAMPAIGN_LEASE_FILENAME).read_text(encoding="utf-8"))["state"], "active")


class CommandLineAndBoundaryTests(unittest.TestCase):
    def test_help_lists_design_subcommands(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            claude_upgrade.main(["--help"])
        listed = set(claude_upgrade._build_parser()._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
        self.assertEqual(set(DESIGN_SUBCOMMANDS) - listed, set())
        self.assertIn("compile-and-run-vc-batch", output.getvalue())

    def test_own_subcommands_report_pending(self) -> None:
        # classify（B3-02）与 prepare-profile／stage-profile（B3-03）已落实，这里换成仍待实现的 plan-candidate-gates（B3-04）。
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = claude_upgrade.main(["plan-candidate-gates", "--campaign-dir", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("B3-04", error.getvalue())

    def test_claude_orchestrator_does_not_import_codex_upgrade(self) -> None:
        tree = ast.parse((TOOL_ROOT / "claude_upgrade.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {f"{node.module}.{alias.name}" for alias in node.names} | {str(node.module)}
        self.assertEqual(sorted(name for name in imported if name.rsplit(".", 1)[-1] == "codex_upgrade"), [])

    def test_control_plane_has_no_client_branches(self) -> None:
        tree = ast.parse((TOOL_ROOT / "upgrade_control_plane.py").read_text(encoding="utf-8"))
        branches = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(isinstance(item, ast.Constant) and item.value in {"codex", "claude"} for item in [node.left, *node.comparators])
        ]
        self.assertEqual(branches, [])

    def test_claude_identity_two_roots_five_digests(self) -> None:
        policy = claude_identity.load_policy()
        identity = claude_identity.compute_identity(policy, REPOSITORY_ROOT)
        for field in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "policy_sha256", "tool_files_sha256"):
            self.assertRegex(identity[field], r"^[0-9a-f]{64}$")
        self.assertEqual(identity["defaulted_paths"], [])
        self.assertEqual(identity["client"], "claude")


if __name__ == "__main__":
    unittest.main()
