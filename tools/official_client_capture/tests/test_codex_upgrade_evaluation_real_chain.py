"""改造 5（评估失败局部恢复）M1 审核修正：真实评估链端到端（T5.10，副本受管树 + 子进程）。

每条链都在**副本受管树**上以正式派发入口（``compile-and-run-vc-batch``、staging 模型、正式监督器）执行，
动作是真实 CLI ``compare``／真实 builder＋真实 checker／真实 CLI ``accept``；父进程只准备非受管输入
（副本树、部署收据 fixture）并断言结果，不用原仓库创建或推进受管 Campaign（老板拍板 4.1）。

* 用例 1（checker 缺陷）：正式 Campaign 在缺陷副本 A（checker 对 SPEC-EP-006 误判）下建立并执行 b0 →
  父 run failed（post-run-tooling）→ reconcile → 修复副本 B（原 checker）：无 epoch 的 apply 被拒 →
  ``evaluation-epoch`` → apply（evaluator-defect，部署收据按 B 身份）→ b1 committed（两条规则全部重跑）→
  派发 b1（后继协议）→ compare 重跑、断言全部重跑全 pass、accept 真实 CLI 动作执行至候选生产合同
  （VC-4 构建收据）——候选证据全程不换；
* 用例 2（accept-reader 缺陷，副本 C）：b0 断言全 pass、accept 动作失败 → offline-accept-failed／anchored →
  apply（accept_reader 变化、无需 epoch）→ b1 断言全部复用（零 checker）；
* 用例 3（compare-reader 缺陷，副本 D）：b0 compare 动作失败 → offline-compare-failed／none → apply →
  b1 compare 重跑、断言全部 pending 重跑；
* R2／R2-b：父进程在 post-run-tooling 收据前／动作输出绑定前被 SIGKILL → monitor 封存 → reconciler 补写
  → 继续恢复到 b1；
* E1～E5：apply 在状态机五个写点逐点崩溃并续作收敛；
* 后继协议伪造负例（篡改 b1 COMMIT）。

本文件位于 tests/，不进受管摘要。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import managed_tree_copy as mtc

DRIVER = "tools.official_client_capture.tests.evaluation_chain_driver"
TREE_ROOT_ENV = "EVALUATION_CHAIN_TREE_ROOT"
CANDIDATE = "candidate-r1"
RULES = ("SPEC-H1-001", "SPEC-EP-006")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class _RealChainHarness:
    """副本树、驱动子进程与只读断言辅助。"""

    def __init__(self, case: unittest.TestCase, work: Path) -> None:
        self.case = case
        self.work = work
        self.root = work / "chain"
        self.root.mkdir(mode=0o700)
        self.trees: dict[str, Path] = {}

    # ---- 副本树 ---------------------------------------------------------

    def tree(self, name: str, *, mutate=None) -> Path:
        if name in self.trees:
            return self.trees[name]
        tree_root = mtc.copy_managed_tree(self.work / f"tree-{name}")
        if mutate is not None:
            mutate(tree_root)
        mtc.assert_tree_binding(tree_root)
        self.trees[name] = tree_root
        return tree_root

    def tree_a(self) -> Path:
        return self.tree("a", mutate=mtc.inject_checker_defect)

    def tree_b(self) -> Path:
        return self.tree("b")

    def tree_c(self) -> Path:
        return self.tree("c", mutate=mtc.inject_accept_defect)

    def tree_d(self) -> Path:
        return self.tree("d", mutate=mtc.inject_compare_defect)

    # ---- 驱动 ---------------------------------------------------------------

    def run(self, tree_root: Path, *arguments: str, expect_exit: int = 0, timeout: float = 900.0) -> dict:
        completed = mtc.run_python(
            tree_root,
            ["-m", DRIVER, "--root", str(self.root), *arguments],
            extra_env={TREE_ROOT_ENV: str(tree_root)},
            timeout=timeout,
        )
        self.case.assertEqual(
            completed.returncode, expect_exit,
            f"driver {' '.join(arguments)} rc={completed.returncode}\nSTDERR:\n{completed.stderr[-4000:]}\nSTDOUT:\n{completed.stdout[-2000:]}",
        )
        if expect_exit != 0:
            return {"returncode": completed.returncode, "stderr": completed.stderr}
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        return json.loads(lines[-1])

    def state(self) -> dict:
        return _read(self.root / "chain-state.json")

    def deployment_receipt(self, tree_root: Path, name: str) -> Path:
        """按副本树身份生成"修复已部署"的受监督部署收据 fixture（非受管输入）。"""

        identity = self.run(tree_root, "identity")
        receipt = Path(self.state()["control"]) / f"codex-0154-supervisor-enable-{name}.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": "codex-upgrade-arm64-supervised-deploy-receipt/v1",
                    "status": "passed",
                    "campaign_id": f"codex-0154-evaluator-fix-{name}",
                    "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "architecture": "aarch64",
                    "production_tool_root": "/root/docker/capture-cli/data/tools/official_client_capture",
                    "production_doc_root": "/root/docker/capture-cli/data/docs",
                    "tool_files_sha256": identity["files_sha256"],
                    "policy_version": identity["policy_version"],
                    "policy_sha256": identity["policy_sha256"],
                    "wire_producer_sha256": identity["wire_producer_sha256"],
                    "evidence_semantics_sha256": identity["evidence_semantics_sha256"],
                    "control_sha256": identity["control_sha256"],
                    "supervisor_sha256": "1" * 64,
                    "assertion_preparer_sha256": "2" * 64,
                    "rollback_backup": None,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        receipt.chmod(0o600)
        return receipt

    # ---- 只读断言辅助 ---------------------------------------------------------

    def campaign_dir(self) -> Path:
        return Path(self.state()["campaign_dir"])

    def summary(self) -> dict:
        return timing_ledger.inspect_ledger(Path(self.state()["timing_ledger"]))

    def events(self) -> list[tuple[str, str]]:
        return [(str(e["event_type"]), str(e["event_id"])) for e, _raw in timing_ledger._load_events(Path(self.state()["timing_ledger"]))]

    def index(self, baseline: int) -> dict:
        root = self.campaign_dir() / "assertions" / CANDIDATE
        if baseline:
            root = root / "revisions" / f"b{baseline}"
        return artifacts.validate_evaluation_run(_read(root / "evaluation-run.json"))

    def checkpoints(self, baseline: int) -> list[dict]:
        root = self.campaign_dir() / "assertions" / CANDIDATE
        if baseline:
            root = root / "revisions" / f"b{baseline}"
        directory = root / "checkpoints"
        return [_read(p) for p in sorted(directory.iterdir()) if p.suffix == ".json" and not p.name.endswith("-input.json")]

    def diagnostics(self, run_dir: str) -> dict[str, dict]:
        out = {}
        for path in sorted((Path(run_dir) / "action-diagnostics").glob("action-*-failure.json")):
            payload = _read(path)
            out[payload["action_id"]] = payload
        return out

    def wait_run_state(self, run_dir: str, expected: set[str], timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        state = {}
        while time.monotonic() < deadline:
            try:
                state = _read(Path(run_dir) / "state.json")
            except (OSError, ValueError):
                state = {}
            if state.get("state") in expected:
                return state
            time.sleep(0.1)
        raise AssertionError(f"父 run 未在预算内进入 {sorted(expected)}：{state}")

    def b0_index_path(self) -> Path:
        return self.campaign_dir() / "assertions" / CANDIDATE / "evaluation-run.json"

    # ---- 常用链段 ---------------------------------------------------------------

    def init(self, tree_root: Path, *, candidate_surface: str = "codex") -> dict:
        return self.run(tree_root, "init", "--candidate-surface", candidate_surface)

    def dispatch(self, tree_root: Path, sequence: int, tag: str, actions: list[str], *, baseline: int = 0, reuse_from: Path | None = None, authority: str = "none", reuse_items: list[str] | None = None, crash_at: str = "", expect_exit: int = 0) -> dict:
        arguments = ["dispatch", "--sequence", str(sequence), "--tag", tag, "--actions", *actions, "--baseline", str(baseline), "--authority", authority]
        if reuse_from is not None:
            arguments.extend(["--reuse-from", str(reuse_from)])
        if reuse_items:
            arguments.extend(["--reuse-items", *reuse_items])
        if crash_at:
            arguments.extend(["--crash-at", crash_at])
        return self.run(tree_root, *arguments, expect_exit=expect_exit)

    def apply_fix(self, tree_root: Path, receipt: Path, *, crash_at: str = "", expect_exit: int = 0) -> dict:
        preview = self.run(tree_root, "recover", "preview")
        self.case.assertEqual(preview["status"], "preview", preview)
        arguments = [
            "recover", "apply", "--root-cause-class", "evaluator-defect", "--approve-sha256", preview["review_sha256"],
            "--fix-commit", "a" * 40, "--deployment-receipt", str(receipt),
        ]
        if crash_at:
            arguments.extend(["--crash-at", crash_at])
        return self.run(tree_root, *arguments, expect_exit=expect_exit)


# 采集执行位置是合同常量（execution_contract.capture_root 必须等于 /root/oauth-capture）；有生产执行副本的
# 机器上，副本受管树建 Campaign 时 _verify_execution_tree 会拿该副本与副本树逐字比对而必然不一致。
# 真实链因此只能在没有执行副本的机器（开发机／CI）运行，真机上如实跳过，不得为此放宽执行树校验。
PRODUCTION_EXECUTION_TREE = Path("/root/oauth-capture/tools/official_client_capture")


class RealEvaluationChainTests(unittest.TestCase):
    def setUp(self) -> None:
        if PRODUCTION_EXECUTION_TREE.is_dir():
            self.skipTest(f"本机存在固定采集执行副本 {PRODUCTION_EXECUTION_TREE}，副本受管树 Campaign 的执行树校验必然不一致")
        self._temporary = tempfile.TemporaryDirectory(prefix="eval-real-chain-")
        self.addCleanup(self._temporary.cleanup)
        self.work = Path(self._temporary.name).resolve()
        self.harness = _RealChainHarness(self, self.work)

    # ------------------------------------------------------------------
    # 用例 1：checker 缺陷 → b0 fail → epoch → apply → b1 全部重跑全 pass → accept 真实动作
    # ------------------------------------------------------------------

    def test_checker_defect_chain_full_rerun_to_accept(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        digests_a = mtc.evaluator_digests(tree_a)
        digests_b = mtc.evaluator_digests(tree_b)
        self.assertNotEqual(digests_a["checker_sha256"], digests_b["checker_sha256"])
        self.assertEqual(digests_a["builder_sha256"], digests_b["builder_sha256"])

        # ---- 缺陷副本 A：建 Campaign（plan 冻结 A 的身份）、compare、b0 断言失败 ----
        h.init(tree_a)
        h.run(tree_a, "gate", "--tag", "a")
        compare0 = h.dispatch(tree_a, 5, "compare0", ["compare"])
        self.assertEqual((compare0["returncode"], compare0["campaign_run"]["reason"]), (0, "queue-complete"))
        b0 = h.dispatch(tree_a, 6, "b0", ["assert", "accept"], reuse_items=["compare"])
        self.assertEqual((b0["returncode"], b0["campaign_run"]["reason"]), (1, "action-failed:vc5-1-assert"), b0)
        self.assertEqual(b0["campaign_run"]["actions"][0]["effective_failure_class"], "post-run-tooling")
        self.assertEqual(len(b0["campaign_run"]["actions"]), 1)  # accept 未执行
        index0 = h.index(0)
        self.assertEqual({row["rule"]: row["status"] for row in index0["rules"]}, {"SPEC-EP-006": "fail", "SPEC-H1-001": "pass"})
        self.assertEqual(index0["evaluator"], digests_a)
        for checkpoint in h.checkpoints(0):
            self.assertEqual(checkpoint["checker_sha256"], digests_a["checker_sha256"])
            document = _read(h.campaign_dir() / checkpoint["document"]["path"])
            self.assertEqual(document["checker_sha256"], digests_a["checker_sha256"])  # 三者一致
        self.assertEqual(h.summary()["status"], "recovery_required")
        batch0 = _read(h.campaign_dir() / "control" / "vc" / "batches" / "0006-vc-5.json")
        self.assertEqual(batch0["evaluator_digests"], digests_a)
        self.assertIsNone(batch0["evaluation_baseline"])

        # ---- 对账 → active ----
        reconciled = h.run(tree_a, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])
        self.assertEqual(reconciled["status"], "recoverable", reconciled)
        self.assertEqual(h.summary()["status"], "active")

        # ---- 修复副本 B：preview 定位 failure-scope；无 epoch 的 apply 被拒（既有 A2 合同）----
        receipt_b = h.deployment_receipt(tree_b, "fix-b")
        preview = h.run(tree_b, "recover", "preview")
        self.assertEqual((preview["status"], preview["failure_source"], preview["reuse_authority"], preview["failed_step"]), ("preview", "assertion-failed", "anchored", "SPEC-EP-006"))
        self.assertEqual(preview["failure_scope"]["failed_rules"], ["SPEC-EP-006"])
        self.assertEqual(preview["failure_scope"]["jobs"], h.state()["job_ids"])
        rejected = h.apply_fix(tree_b, receipt_b)
        self.assertEqual(rejected["status"], "error", rejected)
        self.assertIn("evaluation-epoch", rejected["error"])
        # 在缺陷副本 A 上 apply：四项摘要相对失败批次没有变化 → 拒绝。
        receipt_a = h.deployment_receipt(tree_a, "no-fix-a")
        no_change = h.apply_fix(tree_a, receipt_a)
        self.assertEqual(no_change["status"], "error")
        self.assertIn("没有任何变化", no_change["error"])
        # 部署收据身份与当前树不一致（拿 B 的收据在 A 上 apply）→ 拒绝。
        stale_receipt = h.apply_fix(tree_a, receipt_b)
        self.assertEqual(stale_receipt["status"], "error")
        self.assertIn("修复尚未部署到当前树", stale_receipt["error"])

        # ---- epoch → apply → b1 committed ----
        epoch = h.run(tree_b, "epoch", "--attempt-id", h.state()["attempt_id"], "--reason", "evaluator checker fix")
        self.assertEqual((epoch["status"], epoch["epoch_index"]), ("epoch_appended", 1))
        self.assertEqual(epoch["evidence_semantics_sha256"], mtc.tool_identity(tree_b)["evidence_semantics_sha256"])
        applied = h.apply_fix(tree_b, receipt_b)
        self.assertEqual((applied["status"], applied["evaluation_baseline"], applied["kind"]), ("applied", 1, "evaluator-only"), applied)
        self.assertEqual(applied["execute_rules"], sorted(RULES))
        self.assertEqual(applied["reuse_rules"], [])
        self.assertEqual(applied["stage_sources"]["capture-candidate"]["source"], "reused")
        # checker 变化连带 compare／accept 读侧闭包（它们引用 checker 模块）变化：compare 也重跑。
        self.assertEqual(applied["stage_sources"]["compare"]["source"], "local")
        recovery = _read(h.campaign_dir() / "candidates" / CANDIDATE / "revisions" / "b1" / "recovery.json")
        self.assertEqual(recovery["current_evaluator_digests"], digests_b)
        self.assertEqual(recovery["failed_evaluator_digests"], digests_a)
        self.assertEqual(recovery["evaluation_epoch"]["index"], 1)
        self.assertEqual(recovery["evaluation_epoch"]["to_evidence_semantics_sha256"], epoch["evidence_semantics_sha256"])
        summary = h.summary()
        self.assertEqual((summary["status"], summary["current_evaluation_baseline"]["evaluation_baseline"]), ("active", 1))

        # ---- 派发 b1（评估基线后继协议）：compare 重跑 → 断言全部重跑全 pass → accept 真实 CLI 动作 ----
        compare1 = h.dispatch(tree_b, 7, "compare1", ["compare"], baseline=1)
        self.assertEqual((compare1["returncode"], compare1["campaign_run"]["reason"]), (0, "queue-complete"), compare1)
        batch7 = _read(h.campaign_dir() / "control" / "vc" / "batches" / "0007-vc-5.json")
        self.assertEqual((batch7["evaluation_baseline"], batch7["baseline_commit_sha256"], batch7["evaluator_digests"]), (1, applied["commit_sha256"], digests_b))
        self.assertTrue((h.campaign_dir() / "comparisons" / CANDIDATE / "revisions" / "b1" / "result.json").is_file())
        h.run(tree_b, "gate", "--tag", "b")
        b1 = h.dispatch(tree_b, 8, "b1", ["assert", "accept"], baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        actions = {action["action_id"]: action for action in b1["campaign_run"]["actions"]}
        self.assertEqual(actions["vc5-1-assert"]["status"], "passed", b1)
        index1 = h.index(1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in index1["rules"]))
        self.assertEqual(index1["evaluator"], digests_b)
        self.assertTrue((h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1" / "results.json").is_file())
        for checkpoint in h.checkpoints(1):
            self.assertEqual(checkpoint["checker_sha256"], digests_b["checker_sha256"])
        # accept 真实 CLI 动作在正式监督器内执行：断言读侧（基线授权身份、checkpoint 一致、epoch 回读、
        # 复用锚点）全部通过，止于候选身份的 VC-4 构建收据（0.154 生产合同，非 evaluator 侧；合成
        # Campaign 没有该收据——见汇报第五节）。
        self.assertEqual(actions["vc5-2-accept"]["status"], "failed", b1)
        diagnostic = h.diagnostics(b1["campaign_run"]["run_dir"])["vc5-2-accept"]
        self.assertIn("VC-4 构建收据", diagnostic["message"])

    # ------------------------------------------------------------------
    # 用例 2：accept-reader 缺陷 → offline-accept-failed／anchored → b1 全部复用、零 checker
    # ------------------------------------------------------------------

    def test_accept_reader_defect_chain_reuses_all_rules(self) -> None:
        h = self.harness
        tree_c, tree_b = h.tree_c(), h.tree_b()
        digests_c, digests_b = mtc.evaluator_digests(tree_c), mtc.evaluator_digests(tree_b)
        self.assertEqual(digests_c["checker_sha256"], digests_b["checker_sha256"])
        self.assertNotEqual(digests_c["accept_reader_sha256"], digests_b["accept_reader_sha256"])
        self.assertEqual(digests_c["compare_reader_sha256"], digests_b["compare_reader_sha256"])
        h.init(tree_c)
        h.run(tree_c, "gate", "--tag", "c")
        self.assertEqual(h.dispatch(tree_c, 5, "compare0", ["compare"])["returncode"], 0)
        b0 = h.dispatch(tree_c, 6, "b0", ["assert", "accept"], reuse_items=["compare"])
        actions = {action["action_id"]: action for action in b0["campaign_run"]["actions"]}
        self.assertEqual(actions["vc5-1-assert"]["status"], "passed", b0)
        self.assertEqual((actions["vc5-2-accept"]["status"], actions["vc5-2-accept"]["effective_failure_class"]), ("failed", "post-run-tooling"))
        self.assertIn("injected-accept-defect", h.diagnostics(b0["campaign_run"]["run_dir"])["vc5-2-accept"]["message"])
        self.assertTrue(all(row["status"] == "pass" for row in h.index(0)["rules"]))
        self.assertEqual(h.run(tree_c, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")
        preview = h.run(tree_b, "recover", "preview")
        self.assertEqual((preview["failure_source"], preview["reuse_authority"], preview["failed_step"]), ("offline-accept-failed", "anchored", "acceptance"))
        self.assertEqual(preview["failure_scope"]["failed_rules"], [])
        # accept 读侧变化属 control 层：evidence 未变，无需 epoch。
        receipt_b = h.deployment_receipt(tree_b, "fix-b")
        applied = h.apply_fix(tree_b, receipt_b)
        self.assertEqual((applied["status"], applied["evaluation_baseline"]), ("applied", 1), applied)
        self.assertEqual(applied["execute_rules"], [])
        self.assertEqual(applied["reuse_rules"], sorted(RULES))
        self.assertEqual(applied["stage_sources"]["compare"]["source"], "reused")
        self.assertIsNone(_read(h.campaign_dir() / "candidates" / CANDIDATE / "revisions" / "b1" / "recovery.json")["evaluation_epoch"])
        # b1：断言批次全部复用（reused checkpoint 指向 b0 链，checker 调用 0），accept 真实 CLI 动作。
        h.run(tree_b, "gate", "--tag", "b")
        b1 = h.dispatch(tree_b, 7, "b1", ["assert", "accept"], baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        actions = {action["action_id"]: action for action in b1["campaign_run"]["actions"]}
        self.assertEqual(actions["vc5-1-assert"]["status"], "passed", b1)
        index1 = h.index(1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is not None and row["reused_from"]["baseline"] == 0 for row in index1["rules"]))
        b1_machine = h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1" / "machine"
        self.assertFalse((b1_machine / "candidate" / "SPEC-H1-001.json").exists())  # 零 checker：没有新文档
        self.assertTrue(all(checkpoint["reused_from"] is not None for checkpoint in h.checkpoints(1)))
        self.assertEqual(actions["vc5-2-accept"]["status"], "failed")
        self.assertIn("VC-4 构建收据", h.diagnostics(b1["campaign_run"]["run_dir"])["vc5-2-accept"]["message"])

    # ------------------------------------------------------------------
    # 用例 3：compare-reader 缺陷 → offline-compare-failed／none → b1 全部 pending 重跑
    # ------------------------------------------------------------------

    def test_compare_reader_defect_chain_reruns_pending(self) -> None:
        h = self.harness
        tree_d, tree_b = h.tree_d(), h.tree_b()
        h.init(tree_d)
        compare0 = h.dispatch(tree_d, 5, "compare0", ["compare"])
        self.assertEqual((compare0["returncode"], compare0["campaign_run"]["reason"]), (1, "action-failed:vc5-1-compare"), compare0)
        self.assertEqual(compare0["campaign_run"]["actions"][0]["effective_failure_class"], "post-run-tooling")
        self.assertIn("injected-compare-defect", h.diagnostics(compare0["campaign_run"]["run_dir"])["vc5-1-compare"]["message"])
        self.assertEqual(h.run(tree_d, "reconcile", "--run-dir", compare0["campaign_run"]["run_dir"])["status"], "recoverable")
        preview = h.run(tree_b, "recover", "preview")
        self.assertEqual((preview["failure_source"], preview["reuse_authority"], preview["failed_step"]), ("offline-compare-failed", "none", "compare"))
        receipt_b = h.deployment_receipt(tree_b, "fix-b")
        applied = h.apply_fix(tree_b, receipt_b)
        self.assertEqual((applied["status"], applied["evaluation_baseline"]), ("applied", 1), applied)
        self.assertEqual(applied["execute_rules"], sorted(RULES))
        self.assertEqual(applied["reuse_rules"], [])
        self.assertEqual(applied["stage_sources"]["compare"]["source"], "local")
        compare1 = h.dispatch(tree_b, 6, "compare1", ["compare"], baseline=1)
        self.assertEqual((compare1["returncode"], compare1["campaign_run"]["reason"]), (0, "queue-complete"), compare1)
        b1 = h.dispatch(tree_b, 7, "b1", ["assert"], baseline=1, authority="none", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        index1 = h.index(1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in index1["rules"]))
        self.assertTrue((h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1" / "results.json").is_file())

    # ------------------------------------------------------------------
    # R2：父进程在 post-run-tooling 收据前 SIGKILL → monitor 封存 → reconciler 补写 → 恢复到 b1
    # ------------------------------------------------------------------

    def test_r2_owner_loss_after_binding_is_sealed_backfilled_and_recovered(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, 5, "compare0", ["compare"])["returncode"], 0)
        crashed = h.dispatch(tree_a, 6, "b0", ["assert"], reuse_items=["compare"], crash_at="post-run-tooling", expect_exit=137)
        self.assertEqual(crashed["returncode"], 137)
        run_dirs = sorted(p for p in Path(h.state()["state_dir"]).iterdir() if p.name.startswith("run-"))
        run_dir = max(run_dirs, key=lambda p: p.stat().st_mtime)
        state = h.wait_run_state(str(run_dir), {"failed", "watchdog-aborted"})
        self.assertEqual(state["state"], "failed", state)
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        receipt = supervisor.read_stop_receipt(run_dir)
        self.assertEqual(receipt["reason"], "action-failed:vc5-1-assert")
        self.assertIsNotNone(receipt["action_outputs_sha256"])
        self.assertFalse((run_dir / "action-diagnostics" / "action-vc5-1-assert-post-run-tooling.json").exists())
        reconciled = h.run(tree_a, "reconcile", "--run-dir", str(run_dir))
        self.assertEqual(reconciled["status"], "recoverable", reconciled)
        self.assertTrue((run_dir / "action-diagnostics" / "action-vc5-1-assert-post-run-tooling.json").is_file())
        receipt_payload = _read(h.campaign_dir() / reconciled["reconciliation_receipt"]["path"])
        self.assertEqual(receipt_payload["failure_class"], "post-run-tooling")
        self.assertTrue(receipt_payload["run"]["action_diagnostic"]["post_run_tooling"]["backfilled"])
        preview = h.run(tree_b, "recover", "preview")
        self.assertEqual((preview["failure_source"], preview["reuse_authority"]), ("assertion-failed", "anchored"))
        h.run(tree_b, "epoch", "--attempt-id", h.state()["attempt_id"], "--reason", "evaluator checker fix")
        applied = h.apply_fix(tree_b, h.deployment_receipt(tree_b, "fix-b"))
        self.assertEqual((applied["status"], applied["evaluation_baseline"]), ("applied", 1), applied)
        self.assertEqual(h.dispatch(tree_b, 7, "compare1", ["compare"], baseline=1)["returncode"], 0)
        b1 = h.dispatch(tree_b, 8, "b1", ["assert"], baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in h.index(1)["rules"]))

    def test_r2b_owner_loss_before_binding_yields_none_authority(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, 5, "compare0", ["compare"])["returncode"], 0)
        h.dispatch(tree_a, 6, "b0", ["assert"], reuse_items=["compare"], crash_at="before-binding", expect_exit=137)
        run_dir = max((p for p in Path(h.state()["state_dir"]).iterdir() if p.name.startswith("run-")), key=lambda p: p.stat().st_mtime)
        state = h.wait_run_state(str(run_dir), {"failed", "watchdog-aborted"})
        self.assertEqual(state["state"], "failed", state)
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        self.assertIsNone(supervisor.read_stop_receipt(run_dir)["action_outputs_sha256"])
        self.assertEqual(h.run(tree_a, "reconcile", "--run-dir", str(run_dir))["status"], "recoverable")
        preview = h.run(tree_b, "recover", "preview")
        self.assertEqual((preview["failure_source"], preview["reuse_authority"]), ("assertion-failed", "none"))
        h.run(tree_b, "epoch", "--attempt-id", h.state()["attempt_id"], "--reason", "evaluator checker fix")
        applied = h.apply_fix(tree_b, h.deployment_receipt(tree_b, "fix-b"))
        self.assertEqual((applied["status"], applied["reuse_rules"]), ("applied", []), applied)
        recovery = _read(h.campaign_dir() / "candidates" / CANDIDATE / "revisions" / "b1" / "recovery.json")
        self.assertEqual(recovery["reuse_authority"], "none")
        self.assertEqual(h.dispatch(tree_b, 7, "compare1", ["compare"], baseline=1)["returncode"], 0)
        b1 = h.dispatch(tree_b, 8, "b1", ["assert"], baseline=1, authority="none", reuse_items=["compare"])
        self.assertEqual(b1["returncode"], 0, b1)
        self.assertTrue(all(row["reused_from"] is None for row in h.index(1)["rules"]))

    # ------------------------------------------------------------------
    # E1～E5：apply 在五个写点逐点崩溃，续作收敛（一条链上依次推进）
    # ------------------------------------------------------------------

    def test_apply_crash_points_e1_to_e5_resume(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, 5, "compare0", ["compare"])["returncode"], 0)
        b0 = h.dispatch(tree_a, 6, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual(b0["returncode"], 1)
        self.assertEqual(h.run(tree_a, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")
        h.run(tree_b, "epoch", "--attempt-id", h.state()["attempt_id"], "--reason", "evaluator checker fix")
        receipt_b = h.deployment_receipt(tree_b, "fix-b")
        baseline_dir = h.campaign_dir() / "candidates" / CANDIDATE / "revisions" / "b1"
        outbox = Path(h.state()["campaign_dir"]) / "ledger" / "outbox"

        def present(*names: str) -> list[bool]:
            return [(baseline_dir / name).is_file() for name in names]

        # E1：PREPARED 已写、outbox 未写。
        h.apply_fix(tree_b, receipt_b, crash_at="e1", expect_exit=137)
        self.assertEqual(present("diagnosis.json", "recovery.json", "PREPARED", "AUTHORIZATION", "COMMIT"), [True, True, True, False, False])
        recovery_bytes = (baseline_dir / "recovery.json").read_bytes()
        outbox_before = sorted(p.name for p in outbox.iterdir()) if outbox.is_dir() else []
        self.assertEqual(h.summary()["current_evaluation_baseline"], None)
        # b1 不是当前基线：按 b1 编译被拒（冻结基线仍 b0，reuse-from 视角下 builder 失败关闭）。
        # E2：outbox 已提交、未推送。
        h.apply_fix(tree_b, receipt_b, crash_at="e2", expect_exit=137)
        self.assertEqual(present("AUTHORIZATION", "COMMIT"), [False, False])
        self.assertEqual((baseline_dir / "recovery.json").read_bytes(), recovery_bytes)
        self.assertGreater(len(sorted(p.name for p in outbox.iterdir())), len(outbox_before))
        # E3：判定过、AUTHORIZATION 未写（outbox 续作 reused／duplicate）。
        h.apply_fix(tree_b, receipt_b, crash_at="e3", expect_exit=137)
        self.assertEqual(present("AUTHORIZATION", "COMMIT"), [False, False])
        # E4：AUTHORIZATION 已写、COMMIT 未写。
        h.apply_fix(tree_b, receipt_b, crash_at="e4", expect_exit=137)
        self.assertEqual(present("AUTHORIZATION", "COMMIT"), [True, False])
        # E5：COMMIT 已写、账本未写 → b1 仍不是当前基线。
        h.apply_fix(tree_b, receipt_b, crash_at="e5", expect_exit=137)
        self.assertEqual(present("AUTHORIZATION", "COMMIT"), [True, True])
        self.assertIsNone(h.summary()["current_evaluation_baseline"])
        # 续作：只补账本事件，recovery.json 字节不变。
        applied = h.apply_fix(tree_b, receipt_b)
        self.assertEqual((applied["status"], applied["evaluation_baseline"]), ("applied", 1), applied)
        self.assertTrue(applied["ledger_event"]["appended"])
        self.assertEqual((baseline_dir / "recovery.json").read_bytes(), recovery_bytes)
        self.assertEqual(h.summary()["current_evaluation_baseline"]["evaluation_baseline"], 1)
        # 基线已激活：preview／apply 都没有新的失败 run；abandon：没有未 COMMIT 的基线。
        again = h.run(tree_b, "recover", "preview")
        self.assertEqual(again["status"], "error")
        self.assertIn("没有评估动作失败的父 run", again["error"])
        abandon = h.run(tree_b, "recover", "abandon")
        self.assertEqual(abandon["status"], "error")
        self.assertIn("没有未 COMMIT", abandon["error"])

    # ------------------------------------------------------------------
    # 后继协议伪造负例：篡改 b1 COMMIT 后派发被拒
    # ------------------------------------------------------------------

    def test_successor_protocol_rejects_tampered_commit(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, 5, "compare0", ["compare"])["returncode"], 0)
        b0 = h.dispatch(tree_a, 6, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual(h.run(tree_a, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")
        h.run(tree_b, "epoch", "--attempt-id", h.state()["attempt_id"], "--reason", "evaluator checker fix")
        applied = h.apply_fix(tree_b, h.deployment_receipt(tree_b, "fix-b"))
        self.assertEqual(applied["status"], "applied", applied)
        commit_path = h.campaign_dir() / "candidates" / CANDIDATE / "revisions" / "b1" / "COMMIT"
        original = commit_path.read_bytes()
        forged = json.loads(original)
        forged["stage_sources"]["compare"] = {"source": "reused", "baseline": 0, "path": f"comparisons/{CANDIDATE}/result.json", "sha256": "0" * 64}
        forged["commit_sha256"] = artifacts.digest({k: v for k, v in forged.items() if k != "commit_sha256"})
        commit_path.chmod(0o600)
        commit_path.write_bytes(json.dumps(forged, ensure_ascii=False).encode("utf-8"))
        rejected = h.dispatch(tree_b, 7, "forged", ["compare"], baseline=1, expect_exit=1)
        self.assertIn("COMMIT", rejected["stderr"])
        commit_path.write_bytes(original)
        compare1 = h.dispatch(tree_b, 7, "compare1", ["compare"], baseline=1)
        self.assertEqual(compare1["returncode"], 0, compare1)


if __name__ == "__main__":
    unittest.main()
