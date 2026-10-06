"""Claude 编排器骨架合同测试（Claude 升级控制面设计 B1-09）。

* 服务面每个成员在 Claude 适配器上都能取到，种类一致；函数成员与 Codex 编排器签名逐项相等。
* 迁入薄壳在 Claude 适配器作用域里转调共享实现；照搬的通用函数与 Codex 源码逐字一致，同一组行为用例对两个编排器
  各跑一遍结果相同；待实现成员报 ``ClaudeOrchestratorPending`` 并写明工作项。
* ``claude_upgrade.py --help`` 列出设计规定的全部子命令；Claude 编排器不导入 ``codex_upgrade``；共享模块
  ``upgrade_control_plane`` 不按客户端分支；Claude 身份两根五摘要可算、没有按默认层归类的文件。
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import upgrade_client_adapter as uca
from tools.official_client_capture import upgrade_control_plane
from tools.official_client_capture.tests import orchestrator_surface_inventory as inventory

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
COPIED = (
    "_fingerprint", "_normalized_json_sha256", "_read_json", "_utc_now", "_rfc3339_datetime", "_secure_write_json_once",
    "_secure_copy_file_once", "_write_or_verify_json_ignoring", "_require_file_binding", "_directory_tree_digest",
    "_campaign_lease_pid_alive", "_campaign_lease_staleness", "_campaign_file", "_capture_attempt_relative",
    "_candidate_build_receipt_path", "_candidate_revisions_root", "_candidate_revision_dir", "_assertions_root_for_baseline",
    "_attempt_recovery_segment_root", "_recovery_continuity_receipt_path", "_evaluation_baseline_dir", "_failed_job_ids",
    "_read_campaign_lease", "CampaignLease", "_campaign_lock", "_campaign_operation_lease", "_write_campaign_lease_stop_once",
    "_validate_campaign_lease_stop_receipt", "_lease_identity_matches", "_campaign_coordinates", "_require_formal_campaign",
    "_campaign_effective_tool_identity", "_campaign_tool_evolutions", "_git_commit",
)


class SurfaceContractTests(unittest.TestCase):
    def test_every_surface_member_resolves_on_claude_adapter(self) -> None:
        orchestrator = uca.adapter_for("claude").orchestrator
        for name, signature in uca.ORCHESTRATOR_SURFACE.items():
            with self.subTest(name=name):
                member = getattr(orchestrator, name)
                if signature is None:
                    self.assertFalse(inspect.isfunction(member) and name not in {"file_sha256"})
                elif signature == "class":
                    self.assertTrue(inspect.isclass(member))
                else:
                    self.assertTrue(callable(member))

    def test_function_signatures_match_codex(self) -> None:
        for name, signature in uca.ORCHESTRATOR_SURFACE.items():
            if signature is None or signature == "class":
                continue
            with self.subTest(name=name):
                self.assertEqual(
                    str(inspect.signature(getattr(claude_upgrade, name))),
                    str(inspect.signature(getattr(codex_upgrade, name))),
                )

    def test_copied_functions_are_verbatim(self) -> None:
        """照搬的通用函数与 Codex 逐字一致：Codex 改动时这里提醒同步或登记分叉（附录 E 第 6 条以后合并）。"""

        for name in COPIED:
            with self.subTest(name=name):
                self.assertEqual(inspect.getsource(getattr(claude_upgrade, name)), inspect.getsource(getattr(codex_upgrade, name)))

    def test_shells_run_shared_flow_under_claude_scope(self) -> None:
        observed: list[str] = []

        def fake(*arguments, **options):
            del arguments, options
            observed.append(upgrade_control_plane._adapter().client)
            return "shared"

        shells = sorted(name for name in uca.ORCHESTRATOR_SURFACE if name in set(inventory.migrated_function_names()))
        self.assertGreater(len(shells), 50)
        for name in shells:
            with self.subTest(name=name), mock.patch.object(upgrade_control_plane, name, side_effect=fake) as patched:
                function = getattr(claude_upgrade, name)
                parameters = inspect.signature(function).parameters.values()
                positional = [object() for item in parameters if item.default is inspect.Parameter.empty and item.kind in {item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD}]
                keywords = {item.name: object() for item in parameters if item.default is inspect.Parameter.empty and item.kind is item.KEYWORD_ONLY}
                result = function(*positional, **keywords)
                if inspect.isgenerator(result) or hasattr(result, "__enter__"):
                    with result:
                        pass
                self.assertEqual(patched.call_count, 1)
        self.assertEqual(set(observed), {"claude"})

    def test_pending_members_name_their_work_item(self) -> None:
        with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, r"campaign_status：由 B2-11"):
            claude_upgrade.campaign_status(Path("/nonexistent"))
        with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, r"_campaign_jobs：由 B2-06"):
            claude_upgrade._campaign_jobs(Path("/nonexistent"), {}, "official")
        with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, r"scan_evidence：由 B2-07"):
            claude_upgrade.scan_evidence(Path("/nonexistent"))
        self.assertTrue(issubclass(claude_upgrade.ClaudeOrchestratorPending, codex_upgrade.ConfigurationError))


class CopiedBehaviourContractTests(unittest.TestCase):
    """同一组行为用例对两个编排器各跑一遍，结果必须相同。"""

    def _both(self):
        return (("codex", codex_upgrade), ("claude", claude_upgrade))

    def test_digests_paths_and_timestamps(self) -> None:
        payload = {"b": [1, 2], "a": "值"}
        campaign = Path("/data/evidence/campaigns/c1")
        results = {}
        for label, module in self._both():
            results[label] = (
                module._fingerprint(payload),
                module._normalized_json_sha256(payload),
                module._capture_attempt_relative("candidate", "cand-1"),
                module._candidate_build_receipt_path(campaign, "cand-1"),
                module._candidate_revision_dir(campaign, 3),
                module._assertions_root_for_baseline(campaign, "cand-1", 2),
                module._attempt_recovery_segment_root(campaign / "a1", "ar2"),
                module._recovery_continuity_receipt_path(campaign, "a1"),
                module._evaluation_baseline_dir(campaign, "cand-1", 1),
                module._rfc3339_datetime("2026-10-07T00:00:00Z", "t"),
                module._failed_job_ids([{"id": "j1", "status": "failed"}, {"id": "j2", "status": "passed"}]),
            )
        self.assertEqual(results["codex"], results["claude"])
        for label, module in self._both():
            with self.subTest(label=label):
                stamp = module._utc_now()
                self.assertEqual(datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is not None, True)
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    module._capture_attempt_relative("candidate", "../escape")

    def test_write_once_and_verify(self) -> None:
        for label, module in self._both():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "out"
                root.mkdir(mode=0o700)
                path = root / "receipt.json"
                module._secure_write_json_once(path, {"value": 1})
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    module._secure_write_json_once(path, {"value": 2})
                self.assertEqual(module._read_json(path, "收据"), {"value": 1})
                module._write_or_verify_json_ignoring(path, {"value": 1}, volatile=())
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    module._write_or_verify_json_ignoring(path, {"value": 3}, volatile=())

    def test_lease_staleness(self) -> None:
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        payload = {"owner_pid": 2 ** 30, "heartbeat_at_utc": "2026-10-06T00:00:00+00:00", "heartbeat_seconds": 30,
                   "deadline_at_utc": "2026-10-07T01:00:00+00:00"}
        results = {label: module._campaign_lease_staleness(payload, now=now) for label, module in self._both()}
        self.assertEqual(results["codex"], results["claude"])


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
            "前序绑定": ({"predecessor": {"campaign_id": "x"}}, "B2-09"),
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
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = claude_upgrade.main(["classify", "--campaign-dir", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("B3-02", error.getvalue())

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
