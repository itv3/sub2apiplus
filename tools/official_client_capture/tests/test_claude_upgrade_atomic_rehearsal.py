"""Claude 的原子双跑（Claude 升级控制面设计 B5a-09 续）：共享收据模块按 ``--client claude`` 经适配器取 Claude 画像。

不起 Docker、不联网：本机按 ``require_arm64=False`` 在临时 ``staging`` 树里真跑两次 VC-0→VC-1——Claude 编排器真实的原子入口
（共享控制面以 Claude 适配器转调）、真实的 campaign-run 父监督器与 worker 子进程、真实的计时账本（v2 计划）与测试用项目总账
（``upgrade-project-ledger-claude``）。覆盖：

* 画像：Codex 的画像就是共享模块（取值是改造前写死的原值），Claude 的画像是 ``claude_upgrade_atomic_rehearsal``；
* 双跑：收据是 ``official-client-atomic-vc0-vc1-rehearsal/v1`` 且带 ``client``，两个实例的父 run 记录 Claude 新合同清单，
  合成 Campaign 声明 ``client: claude``，失败父 run 用 Claude 新合同清单、计时账本进阶段审核、第二个失败父 run 显式报出
  闭合失败，四个负例都失败关闭；工具身份另绑共享控制面、Claude VC 制品与画像；逐字重放；
* 客户端隔离：Claude 收据按 Codex 合同重放即拒绝，归属字段被改并重算自摘要也拒绝；作用域不得嵌套另一客户端；
* 命令行：原子双跑与 worker 接受 ``--client``，分批演练不接受；报错前缀按客户端。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade_atomic_rehearsal as claude_profile
from tools.official_client_capture import codex_upgrade_campaign_run_rehearsal_receipt as rehearsal
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture import upgrade_client_adapter as uca

OCC = Path(rehearsal.__file__).resolve().parent


def _staging_root(directory: str) -> Path:
    """演练实例是 staging 树内的 formal Campaign（测试用项目总账只能建在 staging 段下）。"""

    root = Path(directory).resolve() / "staging" / "rehearsal"
    root.mkdir(parents=True, mode=0o700)
    root.parent.chmod(0o700)
    evidence = root / "atomic-double"
    evidence.mkdir(mode=0o700)
    return evidence


class ProfileTests(unittest.TestCase):
    def test_adapters_bind_the_two_profiles(self) -> None:
        self.assertIs(uca.adapter_for("codex").atomic_rehearsal_profile, rehearsal)
        self.assertIs(uca.adapter_for("claude").atomic_rehearsal_profile, claude_profile)
        self.assertIs(rehearsal.atomic_profile("claude"), claude_profile)
        # Codex 画像的取值就是改造前写死在流程里的原值。
        self.assertEqual(
            (rehearsal.ATOMIC_RECEIPT_SCHEMA, rehearsal.ACTION_RESULT_SCHEMA, rehearsal.ATOMIC_VC0_STAGE_SCHEMA),
            ("codex-atomic-vc0-vc1-rehearsal/v2", "codex-p0-campaign-run-action-result/v1", "codex-atomic-vc0-stage/v1"),
        )
        self.assertEqual((rehearsal.ATOMIC_BASELINE_VERSION, rehearsal.ATOMIC_TARGET_VERSION), ("0.151.0", "0.154.0"))
        self.assertEqual(rehearsal.ATOMIC_RUN_SCHEMA, codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA)
        self.assertEqual((dict(rehearsal.ATOMIC_CLIENT_FIELDS), rehearsal.ATOMIC_CLIENT_ARGV, dict(rehearsal.ATOMIC_BATCHED_MANIFEST_EXTRA)),
                         ({}, (), {}))
        self.assertEqual(rehearsal.atomic_extra_tool_files(), ())
        self.assertEqual(rehearsal.CLI_LABEL, "Codex")
        # Claude 画像：合同名都在 official-client-* 下，清单带 client 与 null 的候选级绑定。
        for name in (claude_profile.ATOMIC_RECEIPT_SCHEMA, claude_profile.ACTION_RESULT_SCHEMA, claude_profile.ATOMIC_VC0_STAGE_SCHEMA):
            self.assertTrue(name.startswith("official-client-atomic-"), name)
        self.assertEqual(claude_profile.ATOMIC_RUN_SCHEMA, codex_upgrade_supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA)
        self.assertEqual(dict(claude_profile.ATOMIC_CLIENT_FIELDS), {"client": "claude"})
        self.assertEqual(claude_profile.ATOMIC_CLIENT_ARGV, ("--client", "claude"))
        self.assertEqual(dict(claude_profile.ATOMIC_BATCHED_MANIFEST_EXTRA),
                         {"client": "claude", "candidate_revision": None, "candidate_id": None})

    def test_scope_cannot_switch_clients(self) -> None:
        with rehearsal.client_scope("claude") as adapter:
            self.assertEqual(adapter.client, "claude")
            with rehearsal.client_scope(None) as same:
                self.assertIs(same, adapter)
            with self.assertRaisesRegex(rehearsal.CampaignRunRehearsalError, "不得在 claude 的作用域里切换到 codex"):
                with rehearsal.client_scope("codex"):
                    pass
        with rehearsal.client_scope(None) as default:
            self.assertEqual(default.client, "codex", "没有进入作用域时是 Codex")
        with self.assertRaises(uca.ClientAdapterError):
            with rehearsal.client_scope("kilo"):
                pass

    def test_tool_identity_follows_the_client(self) -> None:
        codex = {item["path"] for item in rehearsal._atomic_tool_identity("codex")["files"]}
        claude = {item["path"] for item in rehearsal._atomic_tool_identity("claude")["files"]}
        shared = {
            "codex_upgrade_campaign_run_rehearsal_receipt.py", "codex_upgrade_evidence_permissions.py",
            "codex_upgrade_predispatch_stop.py", "codex_upgrade_supervisor.py", "codex_upgrade_timing_ledger.py",
            "codex_upgrade_vc_artifacts.py",
        }
        self.assertEqual(codex, shared | {"codex_upgrade.py"}, "Codex 绑定的文件与改造前相同")
        self.assertEqual(claude, shared | {"claude_upgrade.py", "upgrade_control_plane.py", "claude_upgrade_vc.py",
                                           "claude_upgrade_atomic_rehearsal.py"})
        self.assertEqual(rehearsal._atomic_tool_identity(), rehearsal._atomic_tool_identity("codex"))


class ClaudeAtomicDoubleTests(unittest.TestCase):
    """真实原子入口、真实父监督器与 worker 子进程：两个独立新根各完成一次零请求 VC-0→VC-1。"""

    def test_collect_and_replay_sign_the_claude_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _staging_root(directory)
            receipt = rehearsal.collect_atomic_double(evidence, "receipt.json", require_arm64=False, client="claude")
            self.assertEqual((receipt["schema_version"], receipt["client"], receipt["status"]),
                             (claude_profile.ATOMIC_RECEIPT_SCHEMA, "claude", "passed"))
            self.assertEqual((receipt["live_request_count"], receipt["scanned_bytes"], receipt["network_used"]), (0, 0, False))
            self.assertEqual(receipt["tool_identity"], rehearsal._atomic_tool_identity("claude"))
            self.assertEqual([item["campaign_id"] for item in receipt["instances"]], ["atomic-vc0-vc1-1", "atomic-vc0-vc1-2"])
            for item in receipt["instances"]:
                with self.subTest(instance=item["index"]):
                    root = evidence / f"instance-{item['index']}"
                    self.assertEqual((item["parent_run"]["schema_version"], item["parent_run"]["state"]),
                                     (codex_upgrade_supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "stopped"))
                    campaign = json.loads((root / "campaign" / "campaign.json").read_text(encoding="utf-8"))
                    self.assertEqual((campaign["client"], campaign["target_version"]), ("claude", claude_profile.ATOMIC_TARGET_VERSION))
                    plan = json.loads((root / "timing-ledger" / "ledger.json").read_text(encoding="utf-8"))
                    self.assertEqual((plan["schema_version"], plan["client"]),
                                     (codex_upgrade_timing_ledger.CLIENT_PLAN_SCHEMA, "claude"), "计时账本是 Claude 的 v2 计划")
                    self.assertTrue((root / "upgrade-project-ledger-claude" / "plan.json").is_file(), "测试用项目总账按 Claude 建")
                    self.assertFalse((root / "upgrade-project-ledger").exists())
                    marker = json.loads((root / "state" / "atomic-action.json").read_text(encoding="utf-8"))
                    self.assertEqual(marker["schema_version"], claude_profile.ACTION_RESULT_SCHEMA, "worker 按 --client claude 写动作事实")
                    stage = json.loads((root / "campaign" / "control" / "vc" / "vc0-stage.json").read_text(encoding="utf-8"))
                    self.assertEqual(stage["schema_version"], claude_profile.ATOMIC_VC0_STAGE_SCHEMA)
                    batch_manifest = json.loads((root / "campaign" / "control" / "vc" / "run-manifests" / "0001-vc-1.json").read_text(
                        encoding="utf-8"))
                    self.assertEqual((batch_manifest["schema_version"], batch_manifest["client"]),
                                     (codex_upgrade_supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "claude"))
                    self.assertEqual(batch_manifest["actions"][0]["command"][-2:], ["--client", "claude"])
                    # 失败父 run：Claude 新合同清单，计时账本进阶段审核；第二个失败父 run 换了根因，闭合显式失败。
                    timing = item["timing_failure_closeout"]
                    self.assertEqual((timing["status"], timing["event_types"][-2:]),
                                     ("stage_review_required", ["stage_abandoned", "stage_review_required"]))
                    self.assertEqual(timing["failure_parent"]["timing_closeout"]["status"], "passed")
                    self.assertEqual(timing["closeout_failure_parent"]["timing_closeout"]["status"], "failed")
                    self.assertIn("已由其他根因进入 stage_review_required", timing["closeout_failure_parent"]["timing_closeout"]["message"])
                    failing = json.loads((root / "campaign" / "control" / "vc" / "run-manifests" / "atomic-offline-failure.json").read_text(
                        encoding="utf-8"))
                    self.assertEqual((failing["schema_version"], failing["client"], failing["batch_sequence"], failing["candidate_id"]),
                                     (codex_upgrade_supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "claude", 1, None))
                    self.assertEqual(failing["predecessor_checkpoint"]["path"], "control/vc/vc-0-checkpoint.json")
                    self.assertEqual(item["negative_fixtures"], {
                        "canonical_tamper_rejected": True,
                        "existing_parent_run_rejected": True,
                        "deadline_drift_rejected": True,
                        "extra_file_detected_by_inventory": True,
                    })
                    self.assertEqual(item["permission_closeout"]["changed_entry_count"], 8)
            self.assertEqual(rehearsal.replay_atomic_double(evidence, "receipt.json", require_arm64=False, client="claude"), receipt)

            # Claude 收据不能按 Codex 合同重放（作用域缺省 Codex）；归属字段被改并重算自摘要同样拒绝。
            with self.assertRaisesRegex(rehearsal.CampaignRunRehearsalError, "身份、自摘要或零请求边界漂移"):
                rehearsal.replay_atomic_double(evidence, "receipt.json", require_arm64=False)
            path = evidence / "receipt.json"
            forged = dict(receipt, client="codex")
            forged.pop("receipt_sha256")
            forged["receipt_sha256"] = codex_upgrade_vc_artifacts.digest(forged)
            path.chmod(0o600)
            path.write_text(json.dumps(forged, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(rehearsal.CampaignRunRehearsalError, "身份、自摘要或零请求边界漂移"):
                rehearsal.replay_atomic_double(evidence, "receipt.json", require_arm64=False, client="claude")

    def test_failing_parent_manifest_is_rebuilt_from_the_frozen_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _staging_root(directory) / "instance"
            root.mkdir(mode=0o700)
            with rehearsal.client_scope("claude"):
                campaign_dir, _state, plan, checkpoint_path, checkpoint = rehearsal._write_atomic_campaign(root, campaign_id="atomic-x")
                manifest = claude_profile.atomic_failing_parent_manifest(campaign_dir, "atomic-x", "atomic-offline-failure")
                self.assertEqual(manifest, claude_profile.atomic_failing_parent_manifest(campaign_dir, "atomic-x", "atomic-offline-failure"))
                self.assertEqual(
                    (manifest["campaign_plan_sha256"], manifest["original_deadline_at_utc"], manifest["phase"]),
                    (plan["plan_sha256"], plan["original_deadline_at_utc"], "VC-1"),
                )
                self.assertEqual(manifest["predecessor_checkpoint"]["checkpoint_sha256"], checkpoint["checkpoint_sha256"])
                self.assertEqual(manifest["actions"][0]["item_ids"], ["atomic-offline-failure"])
                with self.assertRaisesRegex(rehearsal.CampaignRunRehearsalError, "不一致"):
                    claude_profile.atomic_failing_parent_manifest(campaign_dir, "atomic-other", "atomic-offline-failure")
                checkpoint_path.chmod(0o600)
                checkpoint_path.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(rehearsal.CampaignRunRehearsalError, "非法"):
                    claude_profile.atomic_failing_parent_manifest(campaign_dir, "atomic-x", "atomic-offline-failure")


class CommandLineTests(unittest.TestCase):
    def test_client_flag_is_only_on_the_atomic_commands(self) -> None:
        parser = rehearsal.build_parser()
        for argv in (
            ["atomic-double-collect", "--evidence-root", "/x", "--output", "receipt.json", "--client", "claude"],
            ["atomic-double-replay", "--evidence-root", "/x", "--receipt", "receipt.json", "--client", "claude"],
            ["action-worker", "--campaign-id", "c", "--phase", "VC-1", "--action-id", "a", "--output", "/x/o.json", "--client", "claude"],
        ):
            with self.subTest(command=argv[0]):
                self.assertEqual(parser.parse_args(argv).client, "claude")
                self.assertEqual(parser.parse_args(argv[:-2]).client, "codex", "缺省 codex")
        for argv in (
            ["collect", "--campaign-dir", "/c", "--evidence-root", "/x", "--output", "r.json", "--client", "claude"],
            ["replay", "--campaign-dir", "/c", "--evidence-root", "/x", "--receipt", "r.json", "--client", "claude"],
        ):
            with self.subTest(command=argv[0]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_errors_are_labelled_by_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing")
            for client, label in (("claude", "Claude campaign-run 演练失败"), ("codex", "Codex campaign-run 演练失败")):
                # main 按命令行进程的口径设 umask 0077：测试进程里替掉，免得影响同一进程里的其他用例。
                with self.subTest(client=client), mock.patch.object(rehearsal.os, "umask"), \
                        contextlib.redirect_stderr(io.StringIO()) as stderr:
                    code = rehearsal.main(["atomic-double-replay", "--evidence-root", missing, "--receipt", "receipt.json",
                                           "--client", client])
                self.assertEqual(code, 1)
                self.assertIn(label, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
