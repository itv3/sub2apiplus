"""R18：validation_only 连续链的后段父失败注入（VC-2 分类、VC-4 构建登记、VC-5 验收）与恢复到交付。

在 R13-1 连续链上，同一 Campaign 内依次注入三次父批次失败，每次都走正式对账与恢复，最后交付到 VC-6：

* VC-2：副本树 ``classify`` 在分类草案已写后非零退出（与 R4 同一注入点）→ 父批次失败 →
  撤回注入（修复 control 函数）→ ``reconcile-supervisor-run`` 给出同批重派 → N+1 逐字重派成功，草案字节不变；
* VC-4：与生产 vc4.sh 同形，经 VC-4 批次派发 ``record-candidate-build``；副本树在构建收据已写、
  VC-4 checkpoint 未写时非零退出 → 候选审核 → 撤回注入、对账证明动作可幂等 → 同一 revision 重开 VC-4 并
  N+1 逐字重派，构建收据与 revision seal 字节不变（R18 补上的“工具缺陷修好接着跑”合同）；
* VC-5：``[assert, accept]`` 批次的 accept 动作在 AcceptanceFact 封存后、VC-5 completion 写出前崩溃（C1）→
  对账 recoverable → 同一批次逐字重派（开关文件已删）→ 只补 completion，AcceptanceFact 字节不变。

三次注入都只改 control 层函数（wire、evidence 身份不变）；全程零真实请求，每次重复派发都实测为零增量。
Linux ARM64 上实际构建夹具 Go 二进制及 Docker 镜像；缺少环境时如实 skip（发布认证把 skip 视为未认证）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import candidate_identity_fixture as cif
from tools.official_client_capture.tests import evaluation_chain_driver as driver
from tools.official_client_capture.tests import managed_tree_copy as trees
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests.real_chains.test_codex_upgrade_evaluation_real_chain import _RealChainHarness
from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import (
    LATE_STAGE_FAULTS_ENV, ledger_request_counts,
)

VC2_ANCHOR = '        return _write_classification_draft(\n            campaign_dir, manifest, source_diff, official_diff\n        )'
VC2_INJECTION = (
    '        draft = _write_classification_draft(campaign_dir, manifest, source_diff, official_diff)\n'
    '        raise ConfigurationError("R18 后段注入：分类草案已写后非零退出")'
)
VC4_ANCHOR = '    receipt = _read_json(output, "Candidate 构建收据")\n    _complete_vc_phase(\n'
VC4_INJECTION = (
    '    receipt = _read_json(output, "Candidate 构建收据")\n'
    '    raise ConfigurationError("R18 后段注入：构建收据已写、VC-4 checkpoint 未写时非零退出")\n'
    '    _complete_vc_phase(\n'
)
# 两个注入批次都与生产计划生成器同形：直接调用受管 codex_upgrade.py（阶段幂等重派证明只认直接调用）。
# 失败后账本分别进入 Campaign 级阶段审核与候选审核。
EXPECTED_REVIEW = {"classify-draft": "stage_review_required", "record-candidate-build": "candidate_review_required"}
FAULTS = {
    "classify-draft": {
        "file": "codex_upgrade.py", "anchor": VC2_ANCHOR, "injection": VC2_INJECTION,
        "preserve": ["classification/draft/*/draft.json"],
    },
    "record-candidate-build": {
        "file": "codex_upgrade.py", "anchor": VC4_ANCHOR, "injection": VC4_INJECTION,
        "preserve": ["candidates/*/build-receipt.json", "control/vc/revisions/r1/seal.json"],
    },
}


class LateStageFaultChainTests(unittest.TestCase):
    def test_late_stage_parent_failures_recover_to_delivery(self):
        if not cif.available():
            self.skipTest("后段注入链必须在有 Docker 和 Go 的 Linux ARM64 环境运行")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="codex-late-stage-") as directory, \
             mock.patch.dict(os.environ, {project_ledger_fixture.FIXTURE_ONLY_ENV: "1"}):
            staging = Path(directory).resolve() / "staging"
            staging.mkdir(mode=0o700)
            harness = _RealChainHarness(self, staging)
            tree = harness.tree("late")
            # 注入在建 Campaign 之前写入副本树：Campaign 冻结的是含注入的工具身份，撤回注入即 control 修复。
            trees.replace_once(tree, "codex_upgrade.py", VC2_ANCHOR, VC2_INJECTION)
            trees.replace_once(tree, "codex_upgrade.py", VC4_ANCHOR, VC4_INJECTION)
            faults = staging / "late-stage-faults.json"
            driver._write(faults, FAULTS)
            with mock.patch.dict(os.environ, {LATE_STAGE_FAULTS_ENV: str(faults)}):
                initialized = harness.run(tree, "init", "--full-chain")
            self.addCleanup(cif.cleanup_identity, initialized["candidate_identity"])
            source = (trees.tool_root(tree) / "codex_upgrade.py").read_text(encoding="utf-8")
            self.assertNotIn("R18 后段注入", source, "两处注入都应已在恢复中撤回")
            for tag in FAULTS:
                record = driver._read(harness.root / f"fault-{tag}.json")
                with self.subTest(tag=tag):
                    self.assertEqual(record["failed_reason"], f"action-failed:{tag}")
                    self.assertTrue(any("R18 后段注入" in str(message) for message in record["failed_diagnostics"]), record)
                    self.assertEqual(record["ledger_status_after_failure"], EXPECTED_REVIEW[tag])
                    self.assertEqual((record["reconcile_status"], record["next_action"]), ("recoverable", "redispatch-same-batch"))
                    self.assertEqual(record["recovered_sequence"], record["failed_sequence"] + 1)
                    self.assertEqual(record["ledger_status_after_recovery"], "active")
                    self.assertTrue(record["preserved_files"])
            self.assertEqual(len(list((harness.campaign_dir() / "classification" / "draft").glob("*/draft.json"))), 1)

            harness.run(tree, "gate", "--tag", "late")
            compared = harness.run(tree, "dispatch", "--tag", "late-compare", "--actions", "compare")
            self.assertEqual(compared["returncode"], 0, compared)
            # ---- VC-5：[assert, accept] 批次在 completion 写出前崩溃（C1），对账后逐字重派同一批次 ----
            crashed = harness.dispatch(tree, "late-accept", ["assert", "accept"], accept_wrapper=True, crash_at="accept-completion")
            self.assertEqual(
                (crashed["returncode"], crashed["campaign_run"]["status"], crashed["campaign_run"]["reason"]),
                (1, "failed", "action-failed:vc5-2-accept"), crashed,
            )
            acceptance_path = harness.acceptance_path(0)
            seal_path = acceptance_path.with_name("evidence-seal.json")
            self.assertTrue(acceptance_path.is_file() and seal_path.is_file())
            self.assertFalse(harness.vc5_completion_path().exists())
            sealed_bytes, seal_bytes = acceptance_path.read_bytes(), seal_path.read_bytes()
            reconciled = harness.run(tree, "reconcile", "--run-dir", crashed["campaign_run"]["run_dir"])
            self.assertEqual((reconciled["status"], reconciled["decision"]["decision"]), ("recoverable", "recoverable"), reconciled)
            self.assertEqual(reconciled["root_cause"]["failed_step"], "VC-5-accept")
            resumed = harness.dispatch(tree, "late-accept", ["assert", "accept"], accept_wrapper=True)
            self.assertEqual((resumed["returncode"], resumed["campaign_run"]["reason"]), (0, "queue-complete"), resumed)
            self.assertEqual(acceptance_path.read_bytes(), sealed_bytes)
            self.assertEqual(seal_path.read_bytes(), seal_bytes)
            harness.assert_accepted_to_completion(0)

            delivered = harness.run(tree, "deliver-full")
            self.assertEqual(delivered["completed_phases"], [f"VC-{index}" for index in range(7)])
            self.assertEqual(delivered["live_request_count"], 0)
            self.assertEqual(delivered["duplicate_dispatch_requests"], 0)
            per_batch, ledger_total, project_total = ledger_request_counts(harness.campaign_dir())
            self.assertEqual((ledger_total, project_total), (0, 0))
            duplicate_checks = {driver._read(path)["tag"]: driver._read(path)["duplicate_dispatch_requests"]
                                for path in sorted(harness.root.glob("duplicate-*.json"))}
            self.assertEqual(sorted(duplicate_checks), sorted([
                "vc1-bootstrap", "classify-draft", "classify-preview", "classify-approve", "stage-profile",
                "record-candidate-build", "late-compare", "late-accept", "deliver"]))
            self.real_chain_metrics = {
                "live_request_count": project_total,
                "ledger_live_request_count": ledger_total,
                "duplicate_dispatch_requests": sum(duplicate_checks.values()),
                "duplicate_dispatch_checks": len(duplicate_checks),
                "injected_parent_failures": 3,
                "seconds": round(time.monotonic() - started, 3),
                "batches": [{"phase": batch["phase"], "sequence": batch["sequence"],
                             "execute": batch["execute_item_ids"], "reuse": batch["reuse_item_ids"],
                             "live_request_count": per_batch.get(batch["sequence"], 0)}
                            for path in sorted((harness.campaign_dir() / "control/vc/batches").glob("*.json"))
                            for batch in [driver._read(path)]],
            }
            self.assertEqual(self.real_chain_metrics["duplicate_dispatch_requests"], 0)
            core = ["candidate-frozen-core"]
            expected = [
                ("VC-1", [], ["official-core"]),
                ("VC-2", ["classify-draft"], []),              # 注入失败
                ("VC-2", ["classify-draft"], []),              # 同批重派
                ("VC-2", ["classify-preview"], []),
                ("VC-2", ["classify-approve"], []),
                ("VC-3", ["stage-profile"], []),
                ("VC-4", ["record-candidate-build"], []),      # 注入失败
                ("VC-4", ["record-candidate-build"], []),      # 同批重派
                ("VC-5", ["compare"], core),
                ("VC-5", ["acceptance", "assert-rules"], core),  # accept 崩溃
                ("VC-5", ["acceptance", "assert-rules"], core),  # 同批重派
                ("VC-6", ["deliver"], []),
            ]
            self.assertEqual(self.real_chain_metrics["batches"], [
                {"phase": phase, "sequence": index, "execute": execute, "reuse": reuse, "live_request_count": 0}
                for index, (phase, execute, reuse) in enumerate(expected, 1)
            ])
            self.assertLess(self.real_chain_metrics["seconds"], 900, "ARM64 后段注入链超过 15 分钟验收上限")
            print(json.dumps({"chain": "vc-chain.late-stage-faults", **self.real_chain_metrics}, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
