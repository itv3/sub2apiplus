"""R4：真实子进程分类失败、原 Campaign 对账与 N+1 重派；上游证据使用零请求夹具。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture.tests import evaluation_chain_driver as driver
from tools.official_client_capture.tests import managed_tree_copy as trees
from tools.official_client_capture.tests import project_ledger_fixture


ANCHOR = '        return _write_classification_draft(\n            campaign_dir, manifest, source_diff, official_diff\n        )'
INJECTION = '        draft = _write_classification_draft(campaign_dir, manifest, source_diff, official_diff)\n        raise ConfigurationError("R4 隔离副本：草案已写后非零退出")'


class StageRecoveryChainTests(unittest.TestCase):
    def test_classify_commit_failure_reconciles_and_redispatches(self):
        with tempfile.TemporaryDirectory(prefix="codex-r4-staging-") as directory:
            tree = trees.copy_managed_tree(Path(directory) / "tree")
            trees.replace_once(tree, "codex_upgrade.py", ANCHOR, INJECTION)
            result = trees.run_python(tree, ["-m", "unittest", "-v", __name__ + ".StageRecoveryChainTests._exercise"],
                                      extra_env={project_ledger_fixture.FIXTURE_ONLY_ENV: "1"}, timeout=240)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("skipped=", result.stderr)
            print(result.stdout, end="", flush=True)

    def _exercise(self):
        case = driver.new_real_chain_case(full_chain=True)
        self.addCleanup(case.doCleanups)
        with tempfile.TemporaryDirectory(prefix="r4-staging-") as directory:
            root = Path(directory).resolve() / "staging"
            root.mkdir(mode=0o700)
            fixture = case._b0_fixture(root, campaign_id="r4-source")
            source, manifest = fixture["campaign_dir"], fixture["manifest"]
            case._write_capture_stage(source, source / "official-evidence", phase="official",
                                      identity=manifest["official_identity"],
                                      prepare_evidence=driver._prepare_side_evidence(None),
                                      extra_artifacts=driver._extra_artifacts(False))
            campaign = fixture["data"] / "evidence" / "campaigns" / "r4-upgrade"
            code, stdout, stderr = case._run_main([
                "reuse-official-evidence", "--predecessor-campaign-dir", str(source),
                "--campaign-dir", str(campaign), "--campaign-id", "r4-upgrade",
                "--codex-account-id", str(manifest["configuration"]["codex_account_id"]),
            ])
            self.assertEqual(code, 0, stderr)
            state_dir = root / "supervisor"
            state_dir.mkdir(mode=0o700)
            fixture = {**fixture, "campaign_dir": campaign, "state_dir": state_dir}
            plan = root / "classify.json"
            driver._write(plan, {
                "schema_version": upgrade.codex_upgrade_vc_artifacts.VC_ACTION_PLAN_SCHEMA,
                "execute_item_ids": ["classify-draft"], "reuse_item_ids": [], "actions": [{
                    "action_id": "classify-draft", "operation": "VC-2:classify-draft", "timeout_seconds": 90,
                    "item_ids": ["classify-draft"], "command": [sys.executable, str(Path(upgrade.__file__).resolve()),
                        "classify", "--campaign-dir", str(campaign)],
                }],
            })
            first, code = upgrade.compile_and_run_vc_batch(case._vc_chain_arguments(fixture, "VC-2", 2, plan))
            self.assertEqual(code, 1, first)
            ledger = fixture["timing_ledger"]
            self.assertEqual(timing.inspect_ledger(ledger)["status"], "stage_review_required")
            self.assertTrue((campaign / "control/vc/commits/0002-vc-2.json").is_file())
            self.assertFalse((campaign / "control/vc/vc-2-checkpoint.json").exists())
            draft_paths = list((campaign / "classification/draft").glob("*/draft.json"))
            self.assertEqual(len(draft_paths), 1, first)
            original_files = {p: p.read_bytes() for base in (campaign / "official", campaign / "classification")
                              for p in base.rglob("*") if p.is_file()}
            run_dir = Path(first["campaign_run"]["run_dir"])
            # 真实修改隔离副本中的 control 函数；wire、evidence 和 Campaign 不换身份。
            trees.replace_once(Path(upgrade.__file__).resolve().parents[2], "codex_upgrade.py", INJECTION, ANCHOR)
            self._register_control_fix(case, fixture, campaign, plan)
            reconciled = reconciler.reconcile_supervisor_run(run_dir, campaign)
            self.assertEqual(reconciled["status"], "recoverable", reconciled)
            self.assertTrue(reconciled["stage_replay"]["allowed"])
            self.assertEqual(reconciled["stage_replay"]["next_action"], "redispatch-same-batch")
            # 实际产出的证明样本按 schema 做结构校验（字段闭合、常量与枚举），不再只打印供离线核对。
            schema = json.loads(Path(upgrade.__file__).with_name("codex_upgrade_stage_replay.schema.json").read_text(encoding="utf-8"))
            sample = reconciled["stage_replay"]
            self.assertEqual(set(sample), set(schema["required"]))
            for key, rule in schema["properties"].items():
                if "const" in rule:
                    self.assertEqual(sample[key], rule["const"], key)
                if "enum" in rule:
                    self.assertIn(sample[key], rule["enum"], key)
            for action in sample["actions"]:
                self.assertEqual(set(action), set(schema["properties"]["actions"]["items"]["required"]))
            inner = supervisor._read_json(run_dir / "campaign-run-manifest.json")["manifest"]
            changed = draft_paths[0].parent / "profile.json"
            original = changed.read_bytes()
            changed.write_bytes(original + b"\n")
            with self.assertRaisesRegex(supervisor.SupervisorError, "漂移"):
                supervisor._validate_batched_stage_review_successor(
                    supervisor._read_state(run_dir), inner, run_dir, inner, campaign_dir=campaign,
                )
            changed.write_bytes(original)
            count = timing.inspect_ledger(ledger)["head_sequence"]
            reconciler.reconcile_supervisor_run(run_dir, campaign)
            self.assertEqual(timing.inspect_ledger(ledger)["head_sequence"], count)
            with self.assertRaises(upgrade.ConfigurationError) as refused:
                upgrade.compile_and_run_vc_batch(case._vc_chain_arguments(fixture, "VC-3", 3, plan))
            # VC-2 仍在审核中，VC-3 必须因阶段次序被拒；演进已登记，拒因不能是工具身份门禁。
            self.assertNotIsInstance(refused.exception, upgrade.ToolEvolutionRequired)
            final, code = upgrade.compile_and_run_vc_batch(case._vc_chain_arguments(fixture, "VC-2", 3, plan))
            self.assertEqual(code, 0, final)
            self.assertEqual(timing.inspect_ledger(ledger)["status"], "active")
            self.assertEqual(original_files, {p: p.read_bytes() for p in original_files})
            self.assertEqual(len(list((campaign / "classification/draft").glob("*/draft.json"))), 1)
            head = project.replay_head(fixture["ledger"])
            self.assertNotIn("r4-upgrade", head["terminal_campaigns"])
            self.assertEqual((head["precise_total"], head["estimated_total"]), (0, 0))
            print(json.dumps({"r4_schema_samples": {"stage_replay": reconciled["stage_replay"],
                                                    "timing_checkpoint": timing.build_checkpoint(ledger)}}), flush=True)
            print(json.dumps({"fixture": "R4 原 Campaign 分类恢复", "execute_actions": 2,
                              "reused_drafts": 1, "reused_official_jobs": 1,
                              "live_request_count": 0, "unchanged_source_bytes": True}), flush=True)

    def _register_control_fix(self, case, fixture, campaign: Path, plan: Path) -> None:
        """修复部署后按修好接着跑的正式顺序登记工具演进（修好接着跑第 52 项）。

        工具演进登记（2026-09-27 起）的不变式：Campaign 继续执行时当前受管树必须恰是有效工具身份的树，control 层单独
        变化也要登记（影响为空，只是一次预览＋批准）。本链真实改了隔离副本的 control 函数，所以：① 未登记前派发入口以
        ToolEvolutionRequired 零写入拒绝；② 写一份五摘要按当前受管树现算的受监督部署收据（字段沿用夹具首份收据，不写死
        任何摘要，受管树以后怎么变都跟得上）；③ tool-evolution 预览→批准，影响为空；④ 登记后当前树即有效身份。
        """

        staging_root = campaign / "control" / "vc" / "staging"

        def staging_snapshot() -> list[str]:
            return sorted(str(path.relative_to(campaign)) for path in staging_root.rglob("*")) if staging_root.exists() else []

        before = staging_snapshot()
        with self.assertRaisesRegex(upgrade.ToolEvolutionRequired, "control"):
            upgrade.compile_and_run_vc_batch(case._vc_chain_arguments(fixture, "VC-2", 3, plan))
        self.assertEqual(staging_snapshot(), before)
        current = upgrade._tool_identity(include_git=False)
        # 场景前提直接核对：Campaign 与当前受管树出自同一份副本，唯一差别是撤回注入，只动 control 层。
        effective = upgrade._campaign_effective_tool_identity(campaign, upgrade._require_formal_campaign(campaign))["identity"]
        for field in ("policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256"):
            self.assertEqual(current[field], effective[field], field)
        self.assertNotEqual(current["control_sha256"], effective["control_sha256"])
        receipt = json.loads(Path(fixture["deployment"]).read_text(encoding="utf-8"))
        receipt.update(
            {
                "created_at_utc": upgrade._utc_now(),
                "tool_files_sha256": current["files_sha256"],
                "policy_version": current["policy_version"],
                "policy_sha256": current["policy_sha256"],
                "wire_producer_sha256": current["wire_producer_sha256"],
                "evidence_semantics_sha256": current["evidence_semantics_sha256"],
                "control_sha256": current["control_sha256"],
            }
        )
        control = Path(fixture["control"])
        case._write_import_receipt(control / "codex-r4-fix-supervisor-enable-20260928t000000z.json", receipt)
        arguments = argparse.Namespace(
            campaign_dir=campaign,
            fix_commit="4" * 40,
            reason="R4 隔离副本：修复分类草案写后非零退出（control 层）",
            control_root=control,
            approve_sha256=None,
            approved_by=None,
            policy_compatibility_receipt=None,
            policy_activation_certification=None,
        )
        preview = upgrade._tool_evolution_command(arguments)
        self.assertEqual(preview["status"], "approval_required", preview)
        self.assertEqual(preview["changes"]["impact_paths"], [])
        self.assertEqual(preview["impact"]["official"]["affected_job_ids"], [])
        arguments.approve_sha256 = preview["review_sha256"]
        arguments.approved_by = "r4-fixture"
        applied = upgrade._tool_evolution_command(arguments)
        self.assertEqual(applied["status"], "evolution_applied", applied)
        effective = upgrade._require_tool_evolution_registered(campaign, upgrade._require_formal_campaign(campaign), action="t")
        self.assertEqual(effective["index"], 1)


if __name__ == "__main__":
    unittest.main()
