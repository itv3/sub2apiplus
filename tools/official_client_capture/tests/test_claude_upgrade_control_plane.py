"""Claude 升级控制面：共享监督器、对账器、项目总账与计时账本按客户端工作（Claude 升级控制面设计 B1-02～B1-05）。

监督器（B1-02）：
* campaign-run 清单：Codex 沿用 v1～v3、不带 client、隐含 codex；Claude 用 ``official-client-campaign-run/v1``（v2 结构加
  client，只允许 claude）；字段不闭合、client 非法、与 Campaign 总计划归属不符一律拒绝；
* 预声明动作按清单所属客户端识别编排器 CLI 与禁止子命令；
* 后继协议与批次历史要求前后清单属于同一客户端；
* 根因维度按码表补齐 client（Codex 码表不含，根因 ID 逐字不变），Claude Campaign ID 作维度值被拒；
* 监督器不再导入编排器 codex_upgrade（编排器符号全部经适配器）。

对账器（B1-03）：入口按 adapter 参数或 Campaign 归属设定适配器、传错即拒绝；内部函数在入口之外缺省 Codex；
恢复接口用失败源的候选坐标。
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_root_cause as rc
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import upgrade_client_adapter as uca

TOOL_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = {"path": "control/vc/vc-0-checkpoint.json", "sha256": "c" * 64, "phase": "VC-0", "checkpoint_sha256": "d" * 64}


def _build(**extra):
    arguments = dict(
        campaign_id="fixture-campaign", campaign_plan_sha256="a" * 64, batch_id="batch-0001", batch_sequence=1,
        batch_sha256="b" * 64, phase="VC-1", predecessor_checkpoint=CHECKPOINT,
        original_deadline_at_utc="2026-10-10T00:00:00Z", actions=[], execute_items=[], reuse_items=[],
    )
    arguments.update(extra)
    return supervisor.build_batched_campaign_run_manifest(**arguments)


def _build_with_action(command: list[str], client: str | None = None):
    action = {
        "action_id": "step-1", "operation": "VC-1:capture-official", "timeout_seconds": 60,
        "command": command, "item_ids": ["step-1"],
    }
    return _build(actions=[action], execute_items=["step-1"], client=client)


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class RunManifestClientTests(unittest.TestCase):
    def test_codex_manifest_keeps_v2_and_implies_codex(self) -> None:
        manifest = _build()
        self.assertEqual(manifest["schema_version"], supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA)
        self.assertNotIn("client", manifest)
        self.assertEqual(supervisor.run_manifest_client(manifest), "codex")

    def test_claude_manifest_uses_new_contract(self) -> None:
        manifest = _build(client="claude")
        self.assertEqual(manifest["schema_version"], supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA)
        self.assertEqual(manifest["client"], "claude")
        self.assertEqual(supervisor.run_manifest_client(manifest), "claude")
        codex = _build()
        strip = lambda payload, keys: {key: value for key, value in payload.items() if key not in keys}  # noqa: E731
        self.assertEqual(strip(manifest, {"schema_version", "client"}), strip(codex, {"schema_version"}))

    def test_new_contract_rejects_codex_and_unknown_clients(self) -> None:
        for client in ("codex", "gemini", ""):
            with self.subTest(client=client), self.assertRaises(supervisor.SupervisorError):
                _build(client=client)

    def test_parser_requires_closed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = dict(_build(client="claude"))
            cases = {
                "新合同缺 client": {key: value for key, value in claude.items() if key != "client"},
                "v2 带 client": dict(_build(), client="claude"),
            }
            for label, payload in cases.items():
                with self.subTest(label=label), self.assertRaises(supervisor.SupervisorError):
                    supervisor._campaign_run_manifest(_write(root / f"{len(label)}.json", payload))

    def test_campaign_owner_must_match_manifest_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude_campaign = root / "claude"
            _write(claude_campaign / "campaign.json", {"schema_version": "official-client-upgrade-campaign/v1", "client": "claude"})
            codex_campaign = root / "codex"
            _write(codex_campaign / "campaign.json", {"schema_version": "codex-upgrade-campaign/v3"})
            self.assertEqual(supervisor._run_manifest_adapter(_build(client="claude"), claude_campaign).client, "claude")
            self.assertEqual(supervisor._run_manifest_adapter(_build(), codex_campaign).client, "codex")
            self.assertEqual(supervisor._run_manifest_adapter(_build(), None).client, "codex")
            for manifest, campaign in ((_build(client="claude"), codex_campaign), (_build(), claude_campaign)):
                with self.subTest(schema=manifest["schema_version"]), self.assertRaises(supervisor.SupervisorError):
                    supervisor._run_manifest_adapter(manifest, campaign)

    def test_supervisor_no_longer_imports_orchestrator(self) -> None:
        tree = ast.parse((TOOL_ROOT / "codex_upgrade_supervisor.py").read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [alias.name for alias in node.names if alias.name.rsplit(".", 1)[-1] == "codex_upgrade"]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").rsplit(".", 1)[-1] == "codex_upgrade":
                    offenders.append(node.module or "")
                offenders += [alias.name for alias in node.names if alias.name == "codex_upgrade"]
        self.assertEqual(offenders, [])


class PredeclaredActionClientTests(unittest.TestCase):
    def test_forbidden_subcommands_follow_manifest_client(self) -> None:
        for subcommand in ("plan", "freeze", "import-predecessor-evidence", "reconcile-attempt"):
            with self.subTest(client="claude", subcommand=subcommand), self.assertRaisesRegex(supervisor.SupervisorError, "控制面"):
                _build_with_action(["python3", "claude_upgrade.py", subcommand], client="claude")
        _build_with_action(["python3", "claude_upgrade.py", "capture-official", "run"], client="claude")
        with self.assertRaisesRegex(supervisor.SupervisorError, "控制面"):
            _build_with_action(["python3", "codex_upgrade.py", "plan"])
        # Codex 清单与改造前一致：claude_upgrade.py 不是 Codex 编排器，不按编排器子命令拦截。
        _build_with_action(["python3", "claude_upgrade.py", "plan"])


class SuccessorClientTests(unittest.TestCase):
    def test_successor_protocols_require_same_client(self) -> None:
        codex, claude = _build(), _build(client="claude")
        self.assertTrue(supervisor._same_client_batched_runs(codex, codex))
        self.assertTrue(supervisor._same_client_batched_runs(claude, claude))
        self.assertFalse(supervisor._same_client_batched_runs(codex, claude))
        self.assertFalse(supervisor._same_client_batched_runs(claude, codex))
        self.assertFalse(supervisor._same_client_batched_runs({"schema_version": supervisor.CAMPAIGN_RUN_SCHEMA}, codex))


class RootCauseDimensionTests(unittest.TestCase):
    def test_dimensions_follow_client_code_table(self) -> None:
        codex, claude = uca.adapter_for("codex"), uca.adapter_for("claude")
        codex_codes = rc.load_codes(codex.root_cause_codes_path)
        claude_codes = rc.load_codes(claude.root_cause_codes_path)
        code = "campaign-run.action-failed"
        self.assertEqual(uca.root_cause_dimensions(codex, codex_codes, code, {"phase": "VC-1"}), {"phase": "VC-1"})
        self.assertEqual(
            uca.root_cause_dimensions(claude, claude_codes, code, {"phase": "VC-1"}), {"phase": "VC-1", "client": "claude"}
        )
        before = rc.structured_root_cause(
            component="supervisor", stable_error_code=code, failed_step="capture-official", stable_dimensions={"phase": "VC-1"},
        )
        after = rc.structured_root_cause(
            component="supervisor", stable_error_code=code, failed_step="capture-official",
            stable_dimensions=uca.root_cause_dimensions(codex, codex_codes, code, {"phase": "VC-1"}), codes=codex_codes,
        )
        self.assertEqual(after, before)
        with self.assertRaisesRegex(rc.RootCauseError, "Claude Campaign ID"):
            uca.root_cause_dimensions(
                claude, claude_codes, "claude.capture.relay-failed", {"phase": "VC-1", "scenario": "claude-code-2.1.286-formal"}
            )


class ReconcilerClientTests(unittest.TestCase):
    """对账器（B1-03）：入口按 adapter 参数或 Campaign 归属设定适配器，传错即拒绝；内部函数缺省 Codex；
    恢复接口用失败源的候选坐标。"""

    def test_internal_functions_default_to_codex(self) -> None:
        self.assertEqual(reconciler._adapter().client, "codex")

    def test_scope_follows_campaign_and_rejects_wrong_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_campaign = root / "codex"
            _write(codex_campaign / "campaign.json", {"schema_version": "codex-upgrade-campaign/v3"})
            claude_campaign = root / "claude"
            _write(claude_campaign / "campaign.json", {"schema_version": "official-client-upgrade-campaign/v1", "client": "claude"})
            bare = root / "bare"
            bare.mkdir()
            with reconciler._client_scope(codex_campaign, None) as adapter:
                self.assertEqual((adapter.client, reconciler._adapter().client), ("codex", "codex"))
            with reconciler._client_scope(claude_campaign, None):
                self.assertEqual(reconciler._adapter().client, "claude")
                with reconciler._client_scope(claude_campaign, None):
                    self.assertEqual(reconciler._adapter().client, "claude")
            self.assertEqual(reconciler._adapter().client, "codex")
            with reconciler._client_scope(bare, None) as adapter:
                self.assertEqual(adapter.client, "codex")
            for campaign, wrong in ((codex_campaign, "claude"), (claude_campaign, "codex"), (bare, "claude")):
                with self.subTest(campaign=campaign.name), self.assertRaisesRegex(reconciler.ReconcilerError, "拒绝对账"):
                    with reconciler._client_scope(campaign, uca.adapter_for(wrong)):
                        pass

    def test_entry_points_reject_wrong_adapter_before_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "codex"
            _write(campaign / "campaign.json", {"schema_version": "codex-upgrade-campaign/v3"})
            with self.assertRaisesRegex(reconciler.ReconcilerError, "拒绝对账"):
                reconciler.reconcile_attempt(campaign, "a1", adapter=uca.adapter_for("claude"))

    def test_recovery_reuse_check_uses_source_candidate(self) -> None:
        recorded: dict[str, object] = {}

        def fake(campaign_dir, **kwargs):
            recorded.update(kwargs)
            return {"status": "consistent"}

        preview = {
            "source_attempt_receipt_exists": True, "candidate_id": "cand-v7", "source_attempt_id": "a7",
            "reuse_job_ids": ["j1"], "execute_job_ids": ["j2"],
        }
        with mock.patch.object(codex_upgrade, "recovery_reuse_check", side_effect=fake):
            reconciler._resume_reuse_check(Path("/c"), phase="candidate", recovery_revision=None, preview=preview)
        self.assertEqual((recorded["candidate_id"], recorded["source_attempt_id"]), ("cand-v7", "a7"))


if __name__ == "__main__":
    unittest.main()
