"""ARM64 受监督部署的多根事务：写前意图日志、按内容恢复与按客户端签收据（Claude 升级 B5a-01）。

验收点（设计 B5a-01）逐个注入故障，每个都有对应用例：

① 第二根交换失败 → 第一根换回、文档换回、日志留证、收据不写：``test_1_second_root_exchange_failure_rolls_back``
   （另有文档切到一半失败的变体 ``test_1b_document_switch_failure_rolls_back_all_objects``）；
② 交换已完成、旧树还在候选区时中断 → 恢复按摘要在候选区找到旧树并换回：``test_2_interrupt_after_exchange_restores_from_candidate``；
③ 旧树已移入备份、``switch_done`` 未写时中断 → 按内容判已交换、从备份换回：``test_3_interrupt_after_backup_restores_from_backup``；
④ 首次新增的根切入后中断 → 新产物移回候选区、生产恢复为不存在：``test_4_interrupt_after_installing_new_root_moves_it_back``；
⑤ 回滚交换已完成、``rollback_done`` 未写时再次中断 → 再恢复判已回滚、不重复交换：``test_5_interrupted_rollback_is_idempotent``；
⑥ 收据正文已入日志、只写出一份收据时中断 → 补齐另一份后清理日志：``test_6_prepared_receipts_are_completed``；
⑦ 收据已全部写出、日志未删时中断 → 核验后只清理日志：``test_7_written_receipts_only_remove_journal``；
⑧ 生产既非 old 也非 new、或已交换但候选区与备份都找不到旧树 → 失败关闭：``test_8a_*``／``test_8b_*``（另有收据阶段的
   ``test_8c_*``／``test_8d_*``）；
⑨ 全部成功 → Claude 收据五摘要与本地复算一致：``ClaudeReceiptRealTreeTests``；Codex 收据形态不变：
   ``test_9_default_codex_receipt_shape_and_events``，与改造前脚本同一输入逐字节对照见 ``test_arm64_supervised_deploy_codex_parity``。

全部在开发机上替身化运行（见 ``arm64_deploy_fixture``），不碰 ARM64。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import arm64_supervised_deploy as deploy
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture.tests import arm64_deploy_fixture as fixture
from tools.official_client_capture.tests.arm64_deploy_fixture import DeployHarness

CAPTURE = deploy.CAPTURE_ROOT
CONTROL = deploy.CONTROL_ROOT
BOTH = ("--managed-roots", CAPTURE, CONTROL, "--client", "codex", "claude")
# 改造前 main 的事件序列（缺省 Codex 部署）；B5a-01 只在其中插入写意图、收据正文入日志与删除日志三步。
PRE_B5A01_EVENTS = [
    "enable:preflight",
    "enable:copy-candidate",
    "enable:normalize-candidate",
    "enable:verify-candidate",
    "enable:prepare-assertion-preparer",
    "enable:prepare-document-candidates",
    "enable:atomic-exchange",
    "enable:switch-assertion-preparer",
    "enable:switch-documents",
    "enable:post-switch-verify",
    "enable:read-project-ledger",
    "enable:write-receipt",
]
B5A01_EVENTS = {"enable:write-switch-journal", "enable:prepare-receipts", "enable:remove-switch-journal"}
# Codex 部署收据的字段闭集（codex-arm64-supervisor-enable/v1）在这里逐字写出，防止常量被一起改掉而测试不报。
CODEX_RECEIPT_FIELDS = {
    "schema_version", "status", "campaign_id", "created_at_utc", "architecture", "production_tool_root",
    "production_doc_root", "tool_files_sha256", "policy_version", "policy_sha256", "wire_producer_sha256",
    "evidence_semantics_sha256", "control_sha256", "supervisor_sha256", "assertion_preparer_sha256", "rollback_backup",
    "assertion_preparer_rollback_backup", "document_rollback_backup", "switched_archived_documents",
    "installed_runtime_documents", "supervisor_run_dir", "project_ledger",
}


class SimulatedCrash(BaseException):
    """模拟进程在某一步之后被杀：与 KeyboardInterrupt 同为 BaseException，部署器的任何 except 都会经过它。"""


class HarnessCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.harness = DeployHarness(Path(directory.name).resolve() / "site")

    def crash_run(self, *extra: str) -> None:
        """跑部署直到注入点抛出 SimulatedCrash；except 分支里的即时回滚被替成失败，现场保持进程被杀时的样子。"""

        with mock.patch.object(deploy, "restore_switch_journal", side_effect=RuntimeError("进程已被杀，不会即时回滚")):
            with self.assertRaises(SimulatedCrash):
                self.harness.run(*extra)

    def recover(self) -> dict:
        """下次启动：只执行残留日志的处置（与 main 开头的恢复同一函数）。"""

        with self.harness.patched():
            return deploy.recover_switch_journal(self.harness.control)

    def journal(self) -> deploy.SwitchJournal:
        journals = self.harness.journals()
        self.assertEqual(len(journals), 1, "应恰好留下一份活动日志")
        return deploy.SwitchJournal.load(journals[0])

    def entry(self, journal: deploy.SwitchJournal, root: str) -> dict:
        return next(entry for entry in journal.intent["roots"] if entry["root"] == root)


class FaultInjectionTests(HarnessCase):
    """设计验收 ①～⑧：故障逐个注入。"""

    def test_1_second_root_exchange_failure_rolls_back(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        before = harness.snapshot()

        def fail_second_root(_first: Path, second: Path) -> None:
            if Path(second) == harness.production(CONTROL):
                raise OSError("注入：第二根交换失败")

        harness.before_exchange = fail_second_root
        with self.assertRaisesRegex(OSError, "第二根交换失败"):
            harness.run(*BOTH)
        self.assertEqual(harness.snapshot(), before, "第一根、断言预处理器与文档必须全部回到部署前")
        self.assertEqual(harness.journals(), [], "回滚完成后不得留下活动日志")
        retired = harness.retired_journals()
        self.assertEqual(len(retired), 1, "日志改名留作失败证据")
        evidence = deploy.SwitchJournal.load(retired[0])
        self.assertEqual(evidence.markers, ["switch_begin", "rollback_begin", "rollback_done"])
        self.assertEqual((harness.receipts("codex"), harness.receipts("claude")), ([], []), "失败事务不写任何收据")
        capture = self.entry(evidence, CAPTURE)
        self.assertEqual(deploy.tree_content_sha256(Path(capture["backup_path"])), harness.staging_tree(CAPTURE),
                         "回滚后新树留在备份位置，不删除")
        self.assertIn(("end", "enable:rollback"), harness.events)

    def test_1b_document_switch_failure_rolls_back_all_objects(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        before = harness.snapshot()
        target = harness.docs / deploy.MANAGED_DOCUMENTS[1]

        def fail_second_document(_first: Path, second: Path) -> None:
            if Path(second) == target:
                raise OSError("注入：文档交换失败")

        harness.before_exchange = fail_second_document
        with self.assertRaisesRegex(OSError, "文档交换失败"):
            harness.run(*BOTH)
        self.assertEqual(harness.snapshot(), before, "两个根、断言预处理器与已切换的第一份文档都换回")
        self.assertEqual(harness.journals(), [])
        self.assertEqual(len(harness.retired_journals()), 1)

    def test_2_interrupt_after_exchange_restores_from_candidate(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        before = harness.snapshot()

        def crash_after_capture_exchange(_first: Path, second: Path) -> None:
            if Path(second) == harness.production(CAPTURE):
                raise SimulatedCrash()

        harness.after_exchange = crash_after_capture_exchange
        self.crash_run(*BOTH)
        harness.after_exchange = None
        journal = self.journal()
        capture = self.entry(journal, CAPTURE)
        # 现场：交换完成、旧树还在候选区、备份尚不存在。
        self.assertEqual(harness.tree(CAPTURE), capture["new_tree_sha256"])
        self.assertEqual(deploy.tree_content_sha256(Path(capture["candidate_path"])), capture["old_tree_sha256"])
        self.assertFalse(Path(capture["backup_path"]).exists())
        self.assertNotIn("switch_done", journal.markers)
        result = self.recover()
        self.assertEqual(result["outcome"], "rolled-back")
        self.assertEqual(result["restored"], [CAPTURE])
        self.assertEqual(harness.snapshot(), before)
        self.assertEqual(deploy.tree_content_sha256(Path(capture["candidate_path"])), capture["new_tree_sha256"],
                         "新树换回到候选区，不删除")
        self.assertEqual(harness.journals(), [])
        self.assertEqual(len(harness.retired_journals()), 1)

    def test_3_interrupt_after_backup_restores_from_backup(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        before = harness.snapshot()
        self._crash_before_control_exchange()
        journal = self.journal()
        capture = self.entry(journal, CAPTURE)
        self.assertEqual(journal.markers, ["switch_begin"], "switch_done 未写")
        self.assertEqual(harness.tree(CAPTURE), capture["new_tree_sha256"])
        self.assertFalse(Path(capture["candidate_path"]).exists())
        self.assertEqual(deploy.tree_content_sha256(Path(capture["backup_path"])), capture["old_tree_sha256"])
        result = self.recover()
        self.assertEqual(result["outcome"], "rolled-back")
        self.assertEqual(result["restored"], [CAPTURE], "只按内容判定，第二根未交换即无动作")
        self.assertEqual(harness.snapshot(), before)
        self.assertEqual(deploy.tree_content_sha256(Path(capture["backup_path"])), capture["new_tree_sha256"])

    def test_4_interrupt_after_installing_new_root_moves_it_back(self) -> None:
        harness = self.harness
        harness.build(control_root_old=None)
        before = harness.snapshot()
        self.assertIsNone(before[CONTROL])

        def crash_after_install(_source: Path, destination: Path) -> None:
            if Path(destination) == harness.production(CONTROL):
                raise SimulatedCrash()

        harness.after_rename_noreplace = crash_after_install
        self.crash_run(*BOTH)
        harness.after_rename_noreplace = None
        journal = self.journal()
        control = self.entry(journal, CONTROL)
        self.assertFalse(control["old_exists"])
        self.assertIsNone(control["old_tree_sha256"])
        self.assertEqual(harness.tree(CONTROL), control["new_tree_sha256"])
        result = self.recover()
        self.assertEqual(sorted(result["restored"]), sorted([CAPTURE, CONTROL]))
        self.assertFalse(harness.production(CONTROL).exists(), "生产恢复为不存在")
        self.assertEqual(deploy.tree_content_sha256(Path(control["candidate_path"])), harness.staging_tree(CONTROL),
                         "新产物移回候选区")
        self.assertEqual(harness.snapshot(), before)

    def test_5_interrupted_rollback_is_idempotent(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        before = harness.snapshot()
        self._crash_before_control_exchange()
        original_append = deploy.SwitchJournal.append

        def crash_before_rollback_done(journal: deploy.SwitchJournal, marker: str, **fields: object) -> None:
            if marker == "rollback_done":
                raise SimulatedCrash()
            original_append(journal, marker, **fields)

        with mock.patch.object(deploy.SwitchJournal, "append", crash_before_rollback_done):
            with self.assertRaises(SimulatedCrash):
                self.recover()
        self.assertEqual(harness.snapshot(), before, "回滚交换已完成")
        journal = self.journal()
        self.assertIn("rollback_begin", journal.markers)
        self.assertNotIn("rollback_done", journal.markers)
        exchanges = len(harness.exchange_calls)
        result = self.recover()
        self.assertEqual(len(harness.exchange_calls), exchanges, "再次恢复按内容判已回滚，不重复交换")
        self.assertEqual(result["restored"], [])
        self.assertEqual(harness.snapshot(), before)
        self.assertEqual(harness.journals(), [])

    def test_6_prepared_receipts_are_completed(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        self._crash_after_first_receipt()
        journal = self.journal()
        self.assertTrue(journal.receipts_prepared)
        self.assertEqual(len(harness.receipts("codex")), 1)
        self.assertEqual(harness.receipts("claude"), [])
        prepared = {item["client"]: item for item in journal.receipts or []}
        result = self.recover()
        self.assertEqual(result["outcome"], "receipts-completed")
        self.assertEqual(result["written_receipts"], [Path(prepared["claude"]["path"]).name])
        self.assertEqual(result["verified_receipts"], [Path(prepared["codex"]["path"]).name])
        self.assertEqual(harness.journals(), [])
        self.assertEqual(harness.retired_journals(), [], "收据正文已入日志的残留不回滚")
        claude = harness.receipts("claude")[0]
        self.assertEqual(deploy.file_sha256(claude), prepared["claude"]["sha256"])
        self.assertEqual(harness.tree(CAPTURE), harness.staging_tree(CAPTURE), "生产保持新版本")
        self.assertEqual(harness.tree(CONTROL), harness.staging_tree(CONTROL))

    def test_6b_completed_residue_lets_next_deploy_continue(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        self._crash_after_first_receipt()
        harness.stamp = "20261007T130000Z"
        self.assertEqual(harness.run(*BOTH), 0, "残留 ② 补齐收据、删日志后，本次部署照常进行")
        self.assertEqual(len(harness.receipts("codex")), 2)
        self.assertEqual(len(harness.receipts("claude")), 2)
        self.assertIn(("end", "enable:recover-switch-journal"), harness.events)
        self.assertEqual(harness.journals(), [])

    def test_7_written_receipts_only_remove_journal(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        with mock.patch.object(deploy.SwitchJournal, "remove", side_effect=SimulatedCrash()):
            self.crash_run(*BOTH)
        journal = self.journal()
        receipts = {path.name: deploy.file_sha256(path) for path in [*harness.receipts("codex"), *harness.receipts("claude")]}
        self.assertEqual(len(receipts), 2)
        result = self.recover()
        self.assertEqual(result["outcome"], "receipts-completed")
        self.assertEqual(result["written_receipts"], [])
        self.assertEqual(sorted(result["verified_receipts"]), sorted(receipts))
        self.assertEqual({path.name: deploy.file_sha256(path) for path in [*harness.receipts("codex"), *harness.receipts("claude")]},
                         receipts, "已写出的收据不改动")
        self.assertEqual(harness.journals(), [])
        self.assertEqual(journal.markers[-1], "receipts_prepared")

    def test_8a_neither_old_nor_new_fails_closed_without_touching_anything(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        self._crash_before_control_exchange()
        fixture.write(harness.production(CAPTURE) / "capture.py", "VERSION = 'hand-edited'\n")
        site = harness.snapshot()
        exchanges = len(harness.exchange_calls)
        with self.assertRaisesRegex(deploy.DeploymentError, "无法按内容判定.*undecidable"):
            self.recover()
        self.assertEqual(harness.snapshot(), site, "失败关闭时一个对象都不动")
        self.assertEqual(len(harness.exchange_calls), exchanges)
        self.assertEqual(len(harness.journals()), 1, "日志保持活动名，消费门禁继续拒绝派发")
        self.assertNotIn("rollback_begin", self.journal().markers)

    def test_8b_missing_old_copy_fails_closed(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        self._crash_before_control_exchange()
        capture = self.entry(self.journal(), CAPTURE)
        Path(capture["backup_path"]).rename(harness.root / "moved-away")
        site = harness.snapshot()
        with self.assertRaisesRegex(deploy.DeploymentError, "old-copy-missing"):
            self.recover()
        self.assertEqual(harness.snapshot(), site)
        self.assertEqual(len(harness.journals()), 1)

    def test_8c_prepared_receipts_with_drifted_production_fail_closed(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        self._crash_after_first_receipt()
        fixture.write(harness.production(CONTROL) / "store.py", "STORE = 'hand-edited'\n")
        with self.assertRaisesRegex(deploy.DeploymentError, "不是本次事务的新版本"):
            self.recover()
        self.assertEqual(harness.receipts("claude"), [], "生产不是新版本时不补写收据")
        self.assertEqual(len(harness.journals()), 1)

    def test_8d_tampered_written_receipt_fails_closed(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        with mock.patch.object(deploy.SwitchJournal, "remove", side_effect=SimulatedCrash()):
            self.crash_run(*BOTH)
        codex = harness.receipts("codex")[0]
        codex.write_bytes(codex.read_bytes().replace(b'"passed"', b'"failed"'))
        with self.assertRaisesRegex(deploy.DeploymentError, "与日志正文不一致"):
            self.recover()
        self.assertEqual(len(harness.journals()), 1)

    def test_startup_recovery_refuses_this_deploy_then_next_deploy_succeeds(self) -> None:
        """残留 ①：有日志、无收据正文 → 恢复到部署前、日志改名、拒绝本次部署；之后再部署照常。"""

        harness = self.harness
        harness.build(control_root_old=True)
        before = harness.snapshot()
        self._crash_before_control_exchange()
        harness.before_exchange = None
        harness.stamp = "20261007T130000Z"
        start = len(harness.events)
        with self.assertRaisesRegex(deploy.DeploymentError, "本次部署被拒绝"):
            harness.run(*BOTH)
        self.assertEqual(harness.snapshot(), before)
        self.assertEqual(harness.journals(), [])
        self.assertEqual(len(harness.retired_journals()), 1)
        second_run = [operation for kind, operation in harness.events[start:] if kind == "start"]
        self.assertEqual(second_run, ["enable:recover-switch-journal"], "恢复之后、预检之前即拒绝")
        harness.stamp = "20261007T140000Z"
        self.assertEqual(harness.run(*BOTH), 0)
        self.assertEqual(harness.tree(CAPTURE), harness.staging_tree(CAPTURE))
        self.assertEqual(harness.tree(CONTROL), harness.staging_tree(CONTROL))

    def test_9_default_codex_receipt_shape_and_events(self) -> None:
        """缺省部署（只交换 official_client_capture、只签 Codex）：Codex 收据的字段、文件名与摘要口径不变。"""

        harness = self.harness
        harness.build()
        old_capture = harness.tree(CAPTURE)
        self.assertEqual(harness.run(), 0)
        receipts = harness.receipts("codex")
        self.assertEqual([path.name for path in receipts], ["codex-01650-supervisor-enable-20261007t120000z.json"])
        raw = receipts[0].read_bytes()
        payload = json.loads(raw)
        self.assertEqual(set(payload), CODEX_RECEIPT_FIELDS)
        self.assertEqual(set(payload), set(deploy.CODEX_DEPLOY_RECEIPT_FIELDS))
        self.assertEqual(raw, deploy.canonical(payload) + b"\n", "规范不可变 JSON（VC-0 收口按字节核对）")
        self.assertEqual(payload["schema_version"], "codex-arm64-supervisor-enable/v1")
        self.assertEqual(payload["tool_files_sha256"], deploy.tool_digest(harness.production(CAPTURE))[0])
        self.assertEqual(payload["tool_files_sha256"], deploy.tool_digest(harness.staging / CAPTURE)[0])
        self.assertEqual({key: payload[key] for key in fixture.CODEX_IDENTITY_V2}, fixture.CODEX_IDENTITY_V2)
        self.assertEqual(payload["switched_archived_documents"], list(deploy.MANAGED_DOCUMENTS))
        self.assertEqual(payload["installed_runtime_documents"], list(deploy.MANAGED_RUNTIME_DOCUMENTS[:3]))
        self.assertEqual(deploy.tree_content_sha256(Path(payload["rollback_backup"])), old_capture)
        self.assertTrue(Path(payload["assertion_preparer_rollback_backup"]).is_file())
        self.assertTrue(Path(payload["document_rollback_backup"]).is_dir())
        self.assertEqual(harness.receipts("claude"), [], "缺省只签 Codex 收据")
        self.assertFalse(harness.production(CONTROL).exists(), "缺省不碰 official_client_control")
        for name in deploy.CLAUDE_MANAGED_DOCUMENTS:
            self.assertFalse((harness.docs / name).exists(), "缺省不安装 Claude 文档")
        self.assertEqual((harness.journals(), harness.retired_journals()), ([], []))
        operations = harness.operations()
        self.assertEqual([name for name in operations if name not in B5A01_EVENTS], PRE_B5A01_EVENTS)
        self.assertEqual(
            operations.index("enable:write-switch-journal"),
            operations.index("enable:prepare-document-candidates") + 1,
            "意图在交换任何对象之前写入",
        )
        self.assertEqual(operations[-1], "enable:remove-switch-journal")

    def test_both_clients_sign_one_receipt_each(self) -> None:
        harness = self.harness
        harness.build(control_root_old=None)
        self.assertEqual(harness.run(*BOTH), 0)
        codex_path, claude_path = harness.receipts("codex")[0], harness.receipts("claude")[0]
        self.assertEqual(claude_path.name, "claude-managed-deploy-20261007t120000z.json")
        codex = json.loads(codex_path.read_text(encoding="utf-8"))
        claude = json.loads(claude_path.read_text(encoding="utf-8"))
        self.assertEqual(set(codex), CODEX_RECEIPT_FIELDS)
        self.assertEqual(codex["switched_archived_documents"], list(deploy.MANAGED_DOCUMENTS),
                         "Codex 收据只列 Codex 文档（VC-0 收口逐项核对）")
        self.assertEqual(set(claude), set(deploy.CLAUDE_DEPLOY_RECEIPT_FIELDS))
        self.assertEqual(claude["schema_version"], "official-client-managed-deploy/v1")
        self.assertEqual(claude["client"], "claude")
        expected = fixture.fake_claude_identity(harness.staging)
        self.assertEqual(claude["identity"], {field: expected[field] for field in deploy.CLAUDE_IDENTITY_FIELDS})
        self.assertEqual([row["root"] for row in claude["managed_roots"]], [CAPTURE, CONTROL])
        roots = {row["root"]: row for row in claude["managed_roots"]}
        self.assertIsNone(roots[CONTROL]["rollback_backup"], "首次纳管的根没有回滚备份")
        self.assertTrue(Path(roots[CAPTURE]["rollback_backup"]).is_dir())
        self.assertEqual(claude["transaction_receipts"],
                         [{"client": "codex", "receipt": codex_path.name, "sha256": deploy.file_sha256(codex_path)}])
        self.assertEqual(claude["client_binaries"]["status"], "skipped", "没有目标版本时如实跳过")
        self.assertEqual(claude["pre_a3"]["status"], "not-registered")
        self.assertEqual(sorted(row["document"] for row in claude["documents"]),
                         sorted([*deploy.CLAUDE_MANAGED_DOCUMENTS,
                                 *(deploy.ARCHIVE_DOCUMENT_PREFIX + name for name in deploy.CLAUDE_MANAGED_DOCUMENTS)]))
        self.assertTrue(any("首次纳管" in note for note in claude["notes"]))
        for name in deploy.CLAUDE_MANAGED_DOCUMENTS:
            self.assertEqual((harness.docs / name).read_text(encoding="utf-8"), f"new:{name}\n")

    def test_claude_only_deploy_of_control_root(self) -> None:
        """只部署 official_client_control（B5a-02 的形态）：只签 Claude 收据，不碰 Codex 树、文档与断言预处理器。"""

        harness = self.harness
        harness.build(control_root_old=None)
        # 未交换的 official_client_capture 必须与暂存树逐项相同，Claude 收据才能如实描述生产身份。
        shutil.rmtree(harness.production(CAPTURE))
        shutil.copytree(harness.staging / CAPTURE, harness.production(CAPTURE))
        before = harness.snapshot()
        self.assertEqual(harness.run("--managed-roots", CONTROL, "--client", "claude"), 0)
        self.assertEqual(harness.receipts("codex"), [])
        claude = json.loads(harness.receipts("claude")[0].read_text(encoding="utf-8"))
        roots = {row["root"]: row for row in claude["managed_roots"]}
        self.assertIsNone(roots[CAPTURE]["rollback_backup"])
        self.assertTrue(any("未在本次事务交换" in note for note in claude["notes"]))
        after = harness.snapshot()
        for key in (CAPTURE, "preparer", *(f"docs/{name}" for name in deploy.MANAGED_DOCUMENTS)):
            self.assertEqual(after[key], before[key], f"只部署 Claude 根时不得改动 {key}")
        self.assertNotIn("enable:preflight", harness.operations(), "不签 Codex 收据时不跑 Codex 预检")

    def test_scope_is_rechecked_under_the_lock(self) -> None:
        """取锁之前判定 Claude 尚未纳管、取锁之后它已有收据（别的部署刚签出）：按当前状态重算，拒绝只签 Codex。"""

        harness = self.harness
        harness.build()
        calls: list[int] = []

        def flip(_control_root: Path) -> list[str]:
            calls.append(1)
            return [] if len(calls) == 1 else ["claude"]

        with mock.patch.object(deploy, "managed_clients", side_effect=flip):
            with self.assertRaisesRegex(deploy.DeploymentError, "已纳管的客户端 \\['claude'\\]"):
                harness.run()
        self.assertEqual(len(calls), 2)
        self.assertEqual((harness.journals(), harness.receipts("codex")), ([], []), "拒绝发生在写意图之前")

    def test_unswitched_claude_root_must_match_staging(self) -> None:
        harness = self.harness
        harness.build(control_root_old=True)
        with self.assertRaisesRegex(deploy.DeploymentError, "official_client_control 的生产现状与暂存树不一致"):
            harness.run("--client", "codex", "claude")
        self.assertEqual((harness.journals(), harness.retired_journals()), ([], []), "预检拒绝时不写意图")

    # ---- 共用的中断现场
    def _crash_before_control_exchange(self) -> None:
        harness = self.harness

        def crash(_first: Path, second: Path) -> None:
            if Path(second) == harness.production(CONTROL):
                raise SimulatedCrash()

        harness.before_exchange = crash
        self.crash_run(*BOTH)
        harness.before_exchange = None

    def _crash_after_first_receipt(self) -> None:
        original = deploy.write_json_once
        written: list[Path] = []

        def crash_on_second(path: Path, payload: dict) -> int:
            if written:
                raise SimulatedCrash()
            written.append(path)
            return original(path, payload)

        with mock.patch.object(deploy, "write_json_once", side_effect=crash_on_second):
            self.crash_run(*BOTH)


class TransactionScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.control = Path(directory.name)

    def test_default_scope_is_codex_capture_only(self) -> None:
        scope = deploy.transaction_scope(None, None, self.control)
        self.assertEqual(scope, {"roots": [CAPTURE], "clients": ["codex"], "affected_clients": ["codex", "claude"],
                                 "unmanaged_affected_clients": ["claude"]})

    def test_roots_are_ordered_and_unknown_roots_rejected(self) -> None:
        scope = deploy.transaction_scope([CONTROL, CAPTURE], ["claude", "codex"], self.control)
        self.assertEqual((scope["roots"], scope["clients"]), ([CAPTURE, CONTROL], ["codex", "claude"]))
        with self.assertRaisesRegex(deploy.DeploymentError, "不认识的受管根"):
            deploy.transaction_scope(["tools/other"], None, self.control)

    def test_cannot_sign_for_unaffected_client_or_leave_root_unsigned(self) -> None:
        with self.assertRaisesRegex(deploy.DeploymentError, "不影响客户端"):
            deploy.transaction_scope([CONTROL], ["codex"], self.control)
        with self.assertRaisesRegex(deploy.DeploymentError, "没有为受它影响的客户端签部署收据"):
            deploy.transaction_scope([CAPTURE, CONTROL], ["codex"], self.control)

    def test_managed_affected_client_must_sign(self) -> None:
        fixture.write(self.control / "claude-managed-deploy-20261001t000000z.json", "{}\n")
        with self.assertRaisesRegex(deploy.DeploymentError, r"--client codex claude"):
            deploy.transaction_scope(None, None, self.control)
        fixture.write(self.control / "codex-01600-supervisor-enable-20261001t000000z.json", "{}\n")
        with self.assertRaisesRegex(deploy.DeploymentError, "已纳管的客户端 \\['codex'\\]"):
            deploy.transaction_scope([CAPTURE, CONTROL], ["claude"], self.control)
        scope = deploy.transaction_scope([CONTROL], ["claude"], self.control)
        self.assertEqual(scope["unmanaged_affected_clients"], [])

    def test_document_sets_follow_signing_clients(self) -> None:
        self.assertEqual(deploy.transaction_documents(["codex"]), (deploy.MANAGED_DOCUMENTS, deploy.MANAGED_RUNTIME_DOCUMENTS))
        self.assertEqual(deploy.transaction_documents(["claude"]), (deploy.CLAUDE_MANAGED_DOCUMENTS, ()))
        documents, runtime = deploy.transaction_documents(["codex", "claude"])
        self.assertEqual(documents, (*deploy.MANAGED_DOCUMENTS, *deploy.CLAUDE_MANAGED_DOCUMENTS))
        self.assertEqual(runtime, deploy.MANAGED_RUNTIME_DOCUMENTS)
        self.assertFalse(set(deploy.MANAGED_DOCUMENTS) & set(deploy.CLAUDE_MANAGED_DOCUMENTS))

    def test_receipt_globs_match_client_adapters(self) -> None:
        """部署器的收据文件名模式与适配器 deploy_receipt_glob 同值（消费者经适配器取）。"""

        for client in deploy.DEPLOY_CLIENTS:
            self.assertEqual(upgrade_client_adapter.adapter_for(client).deploy_receipt_glob, deploy.DEPLOY_RECEIPT_GLOBS[client])
        stamp = "20261007t120000z"
        for client, template in deploy.RECEIPT_NAME_TEMPLATES.items():
            self.assertTrue(Path(template.format(stamp=stamp)).match(deploy.DEPLOY_RECEIPT_GLOBS[client]))

    def test_deploy_lock_is_exclusive(self) -> None:
        with deploy.deploy_lock(self.control):
            with self.assertRaisesRegex(deploy.DeploymentError, "拒绝并发执行"):
                with deploy.deploy_lock(self.control):
                    pass
        with deploy.deploy_lock(self.control):
            pass


class SwitchJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.control = self.root / "control"
        self.control.mkdir()
        production = fixture.write(self.root / "data" / "tools" / "x.sh", "old\n")
        candidate = fixture.write(self.control / "x-candidate", "new\n")
        self.entry = deploy.intent_entry("preparer", "x.sh", "file", production, candidate, self.control / "x-backup")
        self.path = self.control / "managed-deploy-20261007t120000z.journal"

    def intent(self, **changes: object) -> dict:
        intent = {
            "schema_version": deploy.SWITCH_JOURNAL_SCHEMA, "stamp": "20261007t120000z", "campaign_id": "c",
            "created_at_utc": "2026-10-07T12:00:00Z", "clients": ["codex"], "affected_clients": ["codex"],
            "unmanaged_affected_clients": [], "managed_roots": [CAPTURE], "control_root": str(self.control),
            "production_data_root": str(self.root / "data"), "production_doc_root": str(self.root / "data" / "docs"),
            "transaction_root": str(self.root / "data" / "docs" / ".t"),
            "receipt_paths": {"codex": str(self.control / "codex-01650-supervisor-enable-20261007t120000z.json")},
            "roots": [], "preparers": [self.entry], "documents": [],
        }
        intent.update(changes)
        return intent

    def test_round_trip_and_truncated_last_line(self) -> None:
        journal = deploy.SwitchJournal.create(self.path, self.intent())
        journal.append("switch_begin")
        with self.path.open("ab") as handle:
            handle.write(b'{"record":"marker","marker":"switch_do')  # 追加时进程中断：最后一行写到一半
        loaded = deploy.SwitchJournal.load(self.path)
        self.assertEqual((loaded.intent, loaded.markers, loaded.receipts_prepared), (self.intent(), ["switch_begin"], False))
        self.assertEqual(loaded.intent_sha256, deploy.sha256_bytes(deploy.canonical(self.intent())))

    def test_tampering_is_rejected(self) -> None:
        deploy.SwitchJournal.create(self.path, self.intent())
        raw = self.path.read_bytes()
        self.path.write_bytes(raw.replace(self.entry["new_tree_sha256"].encode(), b"0" * 64))
        with self.assertRaisesRegex(deploy.DeploymentError, "意图摘要不符"):
            deploy.SwitchJournal.load(self.path)
        self.path.unlink()
        deploy.SwitchJournal.create(self.path, self.intent())
        foreign = deploy.canonical({"record": "marker", "marker": "switch_begin", "intent_sha256": "f" * 64, "at_utc": "x"})
        with self.path.open("ab") as handle:
            handle.write(foreign + b"\n" + foreign + b"\n")
        with self.assertRaisesRegex(deploy.DeploymentError, "不属于这份意图"):
            deploy.SwitchJournal.load(self.path)

    def test_prepared_receipts_are_bound_to_intent(self) -> None:
        journal = deploy.SwitchJournal.create(self.path, self.intent())
        payload = {"schema_version": deploy.CODEX_DEPLOY_RECEIPT_SCHEMA, "status": "passed"}
        receipt_path = self.intent()["receipt_paths"]["codex"]
        good = {"client": "codex", "path": receipt_path, "sha256": deploy.sha256_bytes(deploy.receipt_bytes(payload)), "payload": payload}
        with self.assertRaisesRegex(deploy.DeploymentError, "正文被改动或坐标不符"):
            journal.append("receipts_prepared", receipts=[{**good, "sha256": "0" * 64}])
        journal.append("receipts_prepared", receipts=[good])
        self.assertTrue(deploy.SwitchJournal.load(self.path).receipts_prepared)

    def test_intent_paths_must_stay_in_bounds(self) -> None:
        escaped = {**self.entry, "candidate_path": str(self.root / "elsewhere")}
        with self.assertRaisesRegex(deploy.DeploymentError, "路径越界"):
            deploy.SwitchJournal.create(self.path, self.intent(preparers=[escaped]))
        with self.assertRaisesRegex(deploy.DeploymentError, "收据坐标非法"):
            deploy.SwitchJournal.create(self.path, self.intent(receipt_paths={"codex": str(self.control / "other.json")}))
        self.assertFalse(self.path.exists(), "校验失败时不写日志")

    def test_retired_name_no_longer_matches_active_pattern(self) -> None:
        journal = deploy.SwitchJournal.create(self.path, self.intent())
        retired = journal.retire()
        self.assertEqual(deploy.pending_switch_journals(self.control), [])
        self.assertTrue(retired.is_file())
        self.assertFalse(Path(retired.name).match(deploy.SWITCH_JOURNAL_GLOB))


class TreeDigestTests(unittest.TestCase):
    def test_tests_only_difference_is_visible_and_bytecode_is_ignored(self) -> None:
        """恢复判定不能用 tool_digest：两棵树只差测试文件时它给出同一个值。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            for tree, label in ((old, "a"), (new, "b")):
                fixture.write(tree / "tool.py", "X = 1\n")
                fixture.write(tree / "tests" / "test_tool.py", f"# {label}\n")
            self.assertEqual(deploy.tool_digest(old), deploy.tool_digest(new))
            self.assertNotEqual(deploy.tree_content_sha256(old), deploy.tree_content_sha256(new))
            digest = deploy.tree_content_sha256(old)
            fixture.write(old / "__pycache__" / "tool.cpython-312.pyc", "cache")
            fixture.write(old / "stray.pyc", "cache")
            self.assertEqual(deploy.tree_content_sha256(old), digest, "字节码缓存不是部署内容")
            self.assertEqual(deploy.object_sha256(old / "tool.py", "directory"), deploy.OBJECT_TYPE_MISMATCH)
            self.assertIsNone(deploy.object_sha256(root / "missing", "file"))


class ClaudeStructureTests(unittest.TestCase):
    """Claude 的 pre-A3 与二进制探针：现在没有登记就如实记录，登记之后按同一规则核对。"""

    def test_current_tree_claude_pre_a3_set_is_registered(self) -> None:
        """当前九条 Claude 入口在真实树上按 AST 核对；随树部署不等于逐链通过或当前认证已成立。"""

        tool_root = Path(deploy.__file__).resolve().parent / "official_client_capture"
        result = deploy.verify_claude_pre_a3_entries(tool_root)
        chains = ["vc-chain.claude-reconcile", "vc-chain.claude-deadline", "vc-chain.claude-root-cause",
                  "vc-chain.claude-tool-evolution", "vc-chain.claude-vc1-capture", "vc-chain.claude-full-validation-only",
                  "vc-chain.claude-recovery-chain", "vc-chain.claude-late-stage-faults",
                  "vc-chain.claude-revision-evaluator-recovery"]
        self.assertEqual((result["status"], result["scenario_count"], result["reason"]), ("verified", len(chains), None))
        self.assertEqual([row["id"] for row in result["real_chain_registration"]], chains)
        self.assertTrue(all(".tests.real_chains." in row["test"] for row in result["real_chain_registration"]),
                        "都是 tests/real_chains 下的真实链入口")

    def test_registered_claude_table_is_checked_like_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool_root = Path(directory) / "official_client_capture"
            fixture.write(tool_root / "tests" / "real_chains" / "test_claude_chain.py",
                          "import unittest\nclass ClaudeChain(unittest.TestCase):\n    def test_reconcile(self):\n        pass\n")
            fixture.write(tool_root / "tests" / "test_codex.py",
                          "import unittest\nclass Codex(unittest.TestCase):\n    def test_it(self):\n        pass\n")
            claude_row = ("vc-chain.claude-reconcile", "说明", "tools.official_client_capture.tests.real_chains.test_claude_chain",
                          "ClaudeChain", "test_reconcile")
            codex_row = ("codex-one", "说明", "tools.official_client_capture.tests.test_codex", "Codex", "test_it")
            source = (
                f"SCENARIOS = ({codex_row!r},)\nREAL_CHAIN_IDS = ('codex-one',)\n"
                f"CLAUDE_SCENARIOS = ({claude_row!r},)\n"
                "SCENARIOS_BY_CLIENT = {'codex': SCENARIOS, 'claude': CLAUDE_SCENARIOS}\n"
                "REAL_CHAIN_IDS_BY_CLIENT = {'codex': REAL_CHAIN_IDS, 'claude': ('vc-chain.claude-reconcile',)}\n"
            )
            fixture.write(tool_root / deploy.PRE_A3_CERTIFICATION_MODULE, source)
            result = deploy.verify_claude_pre_a3_entries(tool_root)
            self.assertEqual((result["status"], result["scenario_count"]), ("verified", 1))
            self.assertEqual([row["id"] for row in result["real_chain_registration"]], ["vc-chain.claude-reconcile"])
            self.assertEqual(deploy.verify_pre_a3_scenario_entries(tool_root)["scenario_count"], 1, "Codex 表照常读取")
            (tool_root / "tests" / "real_chains" / "test_claude_chain.py").unlink()
            with self.assertRaisesRegex(deploy.DeploymentError, "未随暂存树部署：vc-chain.claude-reconcile"):
                deploy.verify_claude_pre_a3_entries(tool_root)

    def test_claude_binary_probe_skips_without_target_and_checks_with_target(self) -> None:
        client = mock.Mock()
        with mock.patch.object(deploy, "run_checked") as run_checked:
            result = deploy.probe_claude_binaries(client, Path("/nonexistent"))
        run_checked.assert_not_called()
        self.assertEqual((result["status"], result["target_version"], result["binaries"]), ("skipped", None, []))
        with tempfile.TemporaryDirectory() as directory:
            tool_root = Path(directory)
            fixture.write(tool_root / "claude_upgrade_scenarios_2_1_285.json", json.dumps({"claude_version": "2.1.285"}))
            rows = [{"path": "/opt/claude-2.1.285/claude", "version": "2.1.285", "returncode": 0, "stdout": "2.1.285 (Claude Code)"},
                    {"path": "/opt/claude-2.1.245/claude", "version": "2.1.245", "returncode": 1, "stdout": ""}]
            with mock.patch.object(deploy, "CLAUDE_TARGET_SCENARIO_MANIFEST", "claude_upgrade_scenarios_2_1_285.json"), \
                    mock.patch.object(deploy, "reject_untrusted_file"), \
                    mock.patch.object(deploy, "run_checked", return_value=json.dumps(rows)) as run_checked:
                result = deploy.probe_claude_binaries(client, tool_root)
                self.assertEqual(run_checked.call_args.args[1], "enable:verify-claude-binaries")
                self.assertEqual(result["status"], "verified")
                self.assertEqual([row["verified"] for row in result["binaries"]], [True, False])
                run_checked.return_value = json.dumps(rows[1:])
                with self.assertRaisesRegex(deploy.DeploymentError, "缺少本轮目标版本 2.1.285 的 Claude"):
                    deploy.probe_claude_binaries(client, tool_root)


class ClaudeReceiptRealTreeTests(unittest.TestCase):
    """验收 ⑨（Claude）：在真实受管树副本上部署两根、签双收据，Claude 收据五摘要与本地 v3 引擎复算逐字相等。"""

    def test_claude_receipt_identity_matches_local_recomputation(self) -> None:
        from tools.official_client_capture import claude_tool_identity_policy as claude_policy
        from tools.official_client_capture import codex_upgrade_tool_identity_policy as codex_policy

        repo = fixture.REPO_ROOT
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        with tempfile.TemporaryDirectory() as directory:
            harness = DeployHarness(Path(directory).resolve() / "site")
            harness.build(control_root_old=None)
            for root in (CAPTURE, CONTROL):
                shutil.rmtree(harness.staging / root)
                shutil.copytree(repo / root, harness.staging / root, ignore=ignore)
            # 生产的 official_client_capture 是「上一版」：真实树副本里改动一个受管文件。
            shutil.rmtree(harness.production(CAPTURE))
            shutil.copytree(repo / CAPTURE, harness.production(CAPTURE), ignore=ignore)
            fixture.write(harness.production(CAPTURE) / "capture.py", "# 上一版\n")
            shutil.copyfile(repo / "tools" / deploy.MANAGED_ASSERTION_PREPARER, harness.staging / "tools" / deploy.MANAGED_ASSERTION_PREPARER)
            for name in (*deploy.MANAGED_DOCUMENTS, *deploy.CLAUDE_MANAGED_DOCUMENTS):
                shutil.copyfile(repo / "docs" / name, harness.staging / "docs" / name)
                shutil.copyfile(repo / "docs" / name, harness.staging / "docs" / "repository-docs" / name)
            for name in deploy.MANAGED_RUNTIME_DOCUMENTS:
                shutil.copyfile(repo / "docs" / name, harness.staging / "docs" / name)
            harness.claude_identity = None  # 真实 v3 引擎：暂存树预检一次、切换后的生产树复算一次
            harness.claude_pre_a3 = None
            harness.codex_identity_v2 = None  # 真实 Codex 暂存身份
            harness.pre_a3 = False  # 真实 pre-A3 入口核对
            supervisor_digest = deploy.file_sha256(repo / CAPTURE / "codex_upgrade_supervisor.py")
            preparer_digest = deploy.file_sha256(repo / "tools" / deploy.MANAGED_ASSERTION_PREPARER)
            argv = harness.argv(*BOTH)
            argv[argv.index("--expected-supervisor-digest") + 1] = supervisor_digest
            argv[argv.index("--expected-assertion-preparer-digest") + 1] = preparer_digest
            self.assertEqual(harness.run_argv(argv), 0)
            claude = json.loads(harness.receipts("claude")[0].read_text(encoding="utf-8"))
            local = claude_policy.compute_identity(claude_policy.load_policy(), repo)
            self.assertEqual(claude["identity"], {field: local[field] for field in deploy.CLAUDE_IDENTITY_FIELDS})
            digests = claude_policy.managed_tree_digests(repo, claude_policy.load_policy())
            for row in claude["managed_roots"]:
                prefix = deploy.root_name(row["root"]) + "/"
                entries = [{"path": key, "sha256": digests[key]} for key in sorted(digests) if key.startswith(prefix)]
                self.assertEqual(row["tool_files_sha256"], codex_policy._fingerprint({"entries": entries}), row["root"])
            self.assertEqual(claude["runtime_bindings"][0]["status"], "absent", "受管运行时尚未安装（B5a-03），如实记录")
            codex = json.loads(harness.receipts("codex")[0].read_text(encoding="utf-8"))
            tool_root = repo / CAPTURE
            entries = [{"path": path, "sha256": digest} for path, digest in sorted(codex_policy._managed_tree_digests(tool_root).items())]
            local_codex = codex_policy.compute_identity_v2(codex_policy.load_policy(), tool_root, entries)
            self.assertEqual(codex["tool_files_sha256"], deploy.tool_digest(tool_root)[0])
            for field in ("policy_version", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256"):
                self.assertEqual(codex[field], local_codex[field], field)


DRIVER_ROOT = fixture.REPO_ROOT / "tools" / "arm64_capture_driver"


def load_driver_module(name: str, path: Path):
    """按驱动在采集主机上的运行方式加载（脚本目录在 sys.path 首位），与驱动测试的 load_script 同法。"""

    spec = importlib.util.spec_from_file_location(f"b5a01_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with mock.patch.dict(os.environ, {"D": str(fixture.REPO_ROOT)}), mock.patch.object(sys, "path", [str(path.parent), *sys.path]):
        spec.loader.exec_module(module)
    return module


class DownstreamConsumerTests(unittest.TestCase):
    """下游同步：消费者按客户端选收据的文件名模式与合同，缺省 Codex 不受 Claude 收据影响；事务日志存在时驱动拒绝派发。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve() / "data"
        self.control = self.data / "control"
        self.codex_identity = {"policy_version": 8, "policy_sha256": "1" * 64, "wire_producer_sha256": "2" * 64,
                               "evidence_semantics_sha256": "3" * 64, "control_sha256": "4" * 64, "tool_files_sha256": "c" * 64}
        self.claude_identity = {"policy_version": 1, "policy_sha256": "5" * 64, "wire_producer_sha256": "6" * 64,
                                "evidence_semantics_sha256": "7" * 64, "control_sha256": "8" * 64, "tool_files_sha256": "d" * 64}
        common = {"status": "passed", "architecture": "aarch64", "supervisor_sha256": "9" * 64}
        self.codex = fixture.write(self.control / "codex-01600-supervisor-enable-20261007t120000z.json",
                                   json.dumps({**common, **self.codex_identity}))
        fixture.write(self.control / "claude-managed-deploy-20261007t110000z.json",
                      json.dumps({**common, "client": "claude", "identity": {**self.claude_identity, "tool_files_sha256": "e" * 64}}))
        self.claude = fixture.write(self.control / "claude-managed-deploy-20261007t130000z.json",
                                    json.dumps({**common, "client": "claude", "identity": self.claude_identity}))

    def test_driver_install_selects_receipt_by_client_and_refuses_open_journal(self) -> None:
        install = load_driver_module("install", DRIVER_ROOT / "install.py")
        self.assertEqual(install.latest_deploy_receipt(self.control)[0], self.codex, "缺省按 Codex 选收据")
        self.assertEqual(install.latest_deploy_receipt(self.control, "claude")[0], self.claude, "按部署顺序取 Claude 最新收据")
        self.assertEqual(install._deploy_binding(self.control, "claude")["tool_files_sha256"], "d" * 64)
        self.assertEqual(install._deploy_binding(self.control)["tool_files_sha256"], "c" * 64)
        self.assertEqual(install.DEPLOY_RECEIPT_GLOBS, deploy.DEPLOY_RECEIPT_GLOBS)
        self.assertEqual(install.SWITCH_JOURNAL_GLOB, deploy.SWITCH_JOURNAL_GLOB)
        journal = fixture.write(self.control / "managed-deploy-20261007t140000z.journal", "{}\n")
        for call in (lambda: install.verify_install(self.data / "target", self.data),
                     lambda: install.verify_install(self.data / "target", self.data, "claude")):
            with self.assertRaisesRegex(install.DriverError, "未完成的受监督部署事务日志"):
                call()
        journal.rename(journal.with_name(journal.name + deploy.SWITCH_JOURNAL_RETIRED_SUFFIX))
        install.require_no_switch_journal(self.control)

    def test_guard_refuses_open_journal_before_driver_verify(self) -> None:
        lines = (DRIVER_ROOT / "driver" / "guard.sh").read_text(encoding="utf-8").splitlines()
        start = next(index for index, line in enumerate(lines)
                     if line.startswith('if compgen -G "$D/control/managed-deploy-*.journal"'))
        end = next(index for index in range(start, len(lines)) if lines[index].strip() == "fi")
        verify = next(index for index, line in enumerate(lines) if 'install.py" verify' in line)
        self.assertLess(end, verify, "日志门禁排在驱动复验之前")
        snippet = "set -Eeuo pipefail\n" + "\n".join(lines[start:end + 1])

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(["bash", "-c", snippet], env={**os.environ, "D": str(self.data)},
                                  capture_output=True, text=True)

        self.assertEqual(run().returncode, 0)
        fixture.write(self.control / "managed-deploy-20261007t140000z.journal", "{}\n")
        result = run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("guard/deploy-journal: FAIL", result.stdout)

    def test_fix_and_continue_compares_claude_receipt_through_identity(self) -> None:
        helper = load_driver_module("fix_and_continue", DRIVER_ROOT / "driver" / "fix_and_continue.py")
        path, receipt = helper.latest_deploy_receipt({"D": str(self.data), "CLIENT": "claude"})
        self.assertEqual(path, self.claude)
        expected = {"status": "passed", "architecture": "aarch64", "supervisor_sha256": "9" * 64, **self.claude_identity}
        self.assertEqual(set(expected), set(helper.DEPLOY_RECEIPT_KEYS), "期望摘要的键两客户端相同")
        self.assertEqual(helper.deploy_receipt_problems(receipt, expected, client="claude"), [])
        self.assertTrue(helper.deploy_receipt_problems(receipt, expected), "按 Codex 口径读 Claude 收据必然不符")
        codex_path, codex_receipt = helper.latest_deploy_receipt({"D": str(self.data)})
        self.assertEqual(codex_path, self.codex)
        codex_expected = {"status": "passed", "architecture": "aarch64", "supervisor_sha256": "9" * 64, **self.codex_identity}
        self.assertEqual(helper.deploy_receipt_problems(codex_receipt, codex_expected), [])
        with self.assertRaisesRegex(helper.FixAndContinueError, "CLIENT 不认识"):
            helper.deploy_client({"CLIENT": "kilo"})

    def test_entry_steps_and_background_validation_follow_client(self) -> None:
        from tools.ci import background_validation as bv
        from tools.ci import entry_steps as es

        def context(**params: str) -> es.Context:
            return es.Context(params={"D": str(self.data), **params}, driver_dir=Path("."), steps_dir=Path("."))

        self.assertEqual(context().latest_deploy_receipt(), self.codex)
        self.assertEqual(context(CLIENT="claude").latest_deploy_receipt(), self.claude)
        for ctx, value in ((context(), "c" * 64), (context(CLIENT="claude"), "d" * 64)):
            entry = es.resolve(es.DEPLOY_TOOL_FILES, ctx)
            expected = es._value_entry(es.DEPLOY_TOOL_FILES, json.dumps(value, ensure_ascii=False, sort_keys=True))
            self.assertEqual(entry["sha256"], expected["sha256"])
        with self.assertRaisesRegex(es.EntryStepsError, "CLIENT 不认识"):
            context(CLIENT="kilo").latest_deploy_receipt()
        codex = bv.latest_deployment(self.data)
        self.assertEqual((codex["receipt"], codex["identity"]), (self.codex.name, self.codex_identity))
        claude = bv.latest_deployment(self.data, "claude")
        self.assertEqual((claude["receipt"], claude["identity"]), (self.claude.name, self.claude_identity))
        for module in (es, bv):
            self.assertEqual(module.DEPLOY_RECEIPT_GLOBS, deploy.DEPLOY_RECEIPT_GLOBS)

    def test_wire_transition_cli_takes_current_identity_through_adapter(self) -> None:
        from tools.official_client_capture import codex_upgrade
        from tools.official_client_capture import codex_upgrade_wire_transition as wire

        sentinel = {"wire_producer_sha256": "w" * 64, "files_sha256": "f" * 64}
        with mock.patch.object(codex_upgrade, "_tool_identity", return_value=sentinel) as identity, \
                mock.patch.object(wire, "verdict_official_attempt_identity",
                                  return_value={"verdict": "equal", "frozen_v2_identity": None}) as verdict, \
                mock.patch.object(wire, "_write_once"), \
                mock.patch.object(wire.tip, "load_policy", return_value={"policy_sha256": "p" * 64}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = wire.main(["verdict-official-attempt-identity", "--campaign-dir", "/c", "--attempt-id", "a",
                              "--control-root", "/x", "--output", "/o"])
        self.assertEqual(code, 0)
        identity.assert_called_once_with(include_git=False)
        self.assertIs(verdict.call_args.kwargs["current_identity"], sentinel, "经适配器取到的就是编排器当前的 _tool_identity")


if __name__ == "__main__":
    unittest.main()
