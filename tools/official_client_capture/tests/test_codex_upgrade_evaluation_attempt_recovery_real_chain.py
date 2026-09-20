"""改造 5 M2（attempt-recovery：临时环境故障后同 attempt 只补跑部分 Job）：真实评估链端到端（T5.18）。

每条链在**副本受管树**上以正式派发入口（``compile-and-run-vc-batch``、staging 模型、正式监督器）执行；
M1 的评估动作（真实 CLI ``compare``／真实 builder＋checker／真实 CLI ``accept``）不变，M2 新增的恢复段动作
同样是正式批次内的真实 CLI：``capture-candidate run --attempt-recovery ar<k>``（段 run）、
``capture-candidate seal --attempt-recovery ar<k>``（段增量封存，先经 ``rehearse-candidate-seal`` 隔离预演）
与 ``account-sealed-candidate --attempt-recovery ar<k>``（段级入账）。

端到端口径（M2 开工回顾第四节裁定 2）：b0 attempt 用合成 seal（两候选 Job：Job-A＝candidate-frozen-core／A04／
SPEC-EP-006，Job-B＝candidate-frozen-aux／A03／SPEC-H1-001），ar 段 run 只 mock **外部环境依赖**（容器身份、
ARM64／容器探针、恢复 finalizer 输入、候选凭据、Kilo 后探针、权限收口的 runs 别名、Job 执行本身），provenance、
段预约／账本、投影＋delta 合并、增量 seal、builder（"一条复用、一条重跑"）、accept、completion 全部真实；
seal 断言门禁按 Campaign 批准画像（e2e 两场景简化画像）执行，门禁逻辑不变。

* 用例 1（主链）：b0 断言 SPEC-EP-006 fail（Job-A surface 错）→ reconcile → transient-environment apply
  （``J*={Job-A}``，b1 committed）→ ar1 段 run 只执行 Job-A（证据根重定位 ``…-recovery-ar1``）→ 段增量 seal
  （effective-results：Job-A recovered／Job-B reused；投影 dropped＝{Job-A 原根, 旧 evidence, 旧 logs}）→
  段级入账 → b1 ``[assert, accept]``：SPEC-EP-006 重跑 pass、SPEC-H1-001 复用 → VC-5 completion 绑定 b1；
* 用例 2（两 Job 同在 failure-scope）：b0 两条规则都 fail → ``J*={Job-A, Job-B}`` → ar1 补跑两 Job → b1 两条重跑；
* 用例 3（多基线）：compare-reader 缺陷恢复 b0→b1（evaluator-only）之后断言 fail（真实候选证据问题）→ transient
  恢复 b1→b2（前序清单取 b1 实际 manifest）→ b2 一条复用一条重跑 → completion 绑定 b2；
* A1／E1～E5 变体：transient apply 五个写点崩溃续作；段 run 父 run 在 post-run-tooling 前 SIGKILL（R2 变体）
  → 段失败对账 → 新段。

候选身份夹具（docker＋go，linux/arm64）不可用的机器上，段 seal 需要 VC-4 构建收据、seal 预演需要 Linux root 与
OverlayFS：用例只验证到 ar 段 run 与段目录事实，随后如实 skip。本文件位于 tests/，不进受管摘要。
"""

from __future__ import annotations

import json
import platform
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests import candidate_identity_fixture as cif
from tools.official_client_capture.tests import managed_tree_copy as mtc
from tools.official_client_capture.tests.test_codex_upgrade_evaluation_real_chain import (
    CANDIDATE,
    CANDIDATE_IDENTITY_AVAILABLE,
    RULES,
    _read,
    _RealChainHarness,
)

JOB_A = "candidate-frozen-core"
JOB_B = "candidate-frozen-aux"
RULE_A = "SPEC-EP-006"
RULE_B = "SPEC-H1-001"
SEGMENT_SKIP_REASON = (
    "段增量 seal 需要 VC-4 构建收据（候选身份夹具：docker 与 go，linux/arm64）与 Linux root 上的 OverlayFS seal 预演，本机不可用"
)


def _linux_root() -> bool:
    import os

    return platform.system() == "Linux" and os.geteuid() == 0


class _AttemptRecoveryHarness(_RealChainHarness):
    """M2 链段：两 Job 候选初始化、transient 裁定、段 run／seal／入账与只读断言。"""

    # ---- 链段 ---------------------------------------------------------------

    def init_ar(self, tree_root: Path, *, candidate_surface: str = "other", h1_method: str = "POST") -> dict:
        arguments = ["init-ar", "--candidate-surface", candidate_surface, "--candidate-h1-method", h1_method]
        if not CANDIDATE_IDENTITY_AVAILABLE:
            arguments.append("--no-candidate-identity")
        state = self.run(tree_root, *arguments)
        identity = state.get("candidate_identity")
        if identity:
            self.case.addCleanup(cif.cleanup_identity, identity)
        return state

    def preview(self, tree_root: Path) -> dict:
        preview = self.run(tree_root, "recover", "preview")
        self.case.assertEqual(preview["status"], "preview", preview)
        return preview

    def apply_transient(self, tree_root: Path, *, crash_at: str = "", expect_exit: int = 0) -> dict:
        preview = self.preview(tree_root)
        self.case.assertEqual(preview["failure_source"], "assertion-failed", preview)
        self.case.assertIn("transient-environment", preview["admissible_classes"], preview)
        arguments = ["recover", "apply", "--root-cause-class", "transient-environment", "--approve-sha256", preview["review_sha256"]]
        if crash_at:
            arguments.extend(["--crash-at", crash_at])
        return self.run(tree_root, *arguments, expect_exit=expect_exit)

    def ar_run(self, tree_root: Path, tag: str, applied: dict, *, crash_at: str = "", segment_fail: bool = False, recovery_preview: str = "", expect_exit: int = 0) -> dict:
        arguments = [
            "dispatch", "--tag", tag, "--actions", "ar-run", "--baseline", str(applied["evaluation_baseline"]),
            "--recovery-revision", str(applied["recovery_revision"]), "--execute-jobs", *applied["execute_jobs"],
            "--reuse-items", *applied["reuse_jobs"],
        ]
        if crash_at:
            arguments.extend(["--crash-at", crash_at])
        if segment_fail:
            arguments.append("--segment-fail")
        if recovery_preview:
            arguments.extend(["--rerun-failed", "--recovery-preview", recovery_preview])
        return self.run(tree_root, *arguments, expect_exit=expect_exit)

    def ar_prepare(self, tree_root: Path, applied: dict) -> dict:
        prepared = self.run(tree_root, "ar-prepare", "--recovery-revision", str(applied["recovery_revision"]), "--baseline", str(applied["evaluation_baseline"]))
        self.case.assertEqual(prepared["status"], "ar_prepared", prepared)
        return prepared

    def ar_seal(self, tree_root: Path, tag: str, applied: dict) -> dict:
        """seal 预演（Linux root，真实 OverlayFS）通过后派发段 seal 批次；预演不可用的机器由调用方先 skip。"""

        arguments = ["--tag", tag, "--actions", "ar-seal", "--baseline", str(applied["evaluation_baseline"]), "--reuse-items", JOB_A, JOB_B]
        rehearsal = self.run(tree_root, "ar-rehearse", *arguments, timeout=1500.0)
        self.case.assertEqual((rehearsal["status"], rehearsal["lower_unchanged"]), ("passed", True), rehearsal)
        self.case.assertTrue(Path(rehearsal["receipt_path"]).is_file(), rehearsal)
        return self.run(tree_root, "dispatch", *arguments, timeout=1500.0)

    def ar_account(self, tree_root: Path) -> dict:
        accounted = self.run(tree_root, "ar-account")
        self.case.assertEqual(accounted["status"], "accounted", accounted)
        return accounted

    # ---- 只读断言辅助 ---------------------------------------------------------

    def attempt_root(self) -> Path:
        return self.campaign_dir() / "candidates" / CANDIDATE / "attempts" / str(self.state()["attempt_id"])

    def segment_root(self, recovery_revision: str) -> Path:
        return self.attempt_root() / "recovery" / recovery_revision

    def segment_summary(self, recovery_revision: str) -> dict:
        return _read(self.segment_root(recovery_revision) / "attempt-recovery.json")

    def baseline_dir(self, baseline: int) -> Path:
        return self.campaign_dir() / "candidates" / CANDIDATE / "revisions" / f"b{baseline}"

    def effective_results(self, baseline: int) -> dict:
        return _read(self.baseline_dir(baseline) / "effective-results.json")

    def projection_receipt(self, baseline: int) -> dict:
        return _read(self.baseline_dir(baseline) / "manifest-projection.json")

    def evidence_manifest_roots(self, baseline: int) -> list[str]:
        manifest = _read(self.baseline_dir(baseline) / "evidence-manifest.json")
        return sorted(str(item.get("root") or item.get("prefix") or "") for item in manifest.get("roots", []))

    def ledger_event_types(self) -> list[str]:
        return [event_type for event_type, _event_id in self.events()]

    def assert_segment_awaiting_receipts(self, recovery_revision: str, *, execute_jobs: list[str]) -> dict:
        summary = self.segment_summary(recovery_revision)
        self.case.assertEqual(summary["status"], "awaiting_receipts", summary)
        self.case.assertEqual(sorted(summary["execute_jobs"]), sorted(execute_jobs), summary)
        results = {str(row["id"]): row for row in summary["results"]}
        self.case.assertEqual(sorted(results), sorted(execute_jobs))
        for job_id, row in results.items():
            self.case.assertEqual(row["status"], "complete", row)
            self.case.assertTrue(all(root.endswith(f"-recovery-{recovery_revision}") for root in row["evidence_roots"]), row["evidence_roots"])
            self.case.assertTrue((self.segment_root(recovery_revision) / f"job-{job_id}.json").is_file())
        self.case.assertIsNone(summary.get("evidence_permission_error"))
        self.case.assertIsInstance(summary.get("evidence_permission_closeout"), dict)
        return summary

    def assert_b1_one_reused_one_rerun(self, baseline: int, *, rerun: set[str], reused: set[str]) -> dict:
        index = self.index(baseline)
        rows = {str(row["rule"]): row for row in index["rules"]}
        self.case.assertEqual(set(rows), set(RULES))
        for rule in rerun:
            self.case.assertEqual((rows[rule]["status"], rows[rule]["reused_from"]), ("pass", None), rows[rule])
        for rule in reused:
            self.case.assertEqual(rows[rule]["status"], "pass", rows[rule])
            self.case.assertIsNotNone(rows[rule]["reused_from"], rows[rule])
        return index


class RealAttemptRecoveryChainTests(unittest.TestCase):
    def setUp(self) -> None:
        if mtc.execution_tree_binding_required() and not mtc.execution_tree_binding_available():
            self.skipTest(f"本机存在固定采集执行副本 {mtc.PRODUCTION_EXECUTION_TREE}，但无法建立 mount namespace 绑定（需要 root 与 unshare）")
        self._temporary = tempfile.TemporaryDirectory(prefix="eval-ar-chain-")
        self.addCleanup(self._temporary.cleanup)
        self.work = Path(self._temporary.name).resolve()
        self.harness = _AttemptRecoveryHarness(self, self.work)

    def _segment_stage_available(self) -> bool:
        return CANDIDATE_IDENTITY_AVAILABLE and _linux_root()

    # ------------------------------------------------------------------
    # 用例 1：主链——J*={Job-A}，一条复用一条重跑，completion 绑定 b1
    # ------------------------------------------------------------------

    def test_transient_recovery_reseizes_job_a_then_b1_reuses_one_reruns_one(self) -> None:
        h = self.harness
        tree = h.tree_b()
        state = h.init_ar(tree, candidate_surface="other")
        self.assertEqual(sorted(state["job_ids"]), sorted([JOB_A, JOB_B]))
        compare0 = h.dispatch(tree, "compare0", ["compare"])
        self.assertEqual((compare0["returncode"], compare0["campaign_run"]["reason"]), (0, "queue-complete"), compare0)
        h.run(tree, "gate", "--tag", "b")
        b0 = h.dispatch(tree, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual((b0["returncode"], b0["campaign_run"]["reason"]), (1, "action-failed:vc5-1-assert"), b0)
        index0 = h.index(0)
        self.assertEqual({row["rule"]: row["status"] for row in index0["rules"]}, {RULE_A: "fail", RULE_B: "pass"})
        self.assertEqual(h.run(tree, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")

        preview = h.preview(tree)
        self.assertEqual(preview["reuse_authority"], "anchored", preview)
        self.assertEqual(preview["failure_scope"]["jobs"], [JOB_A], preview["failure_scope"])
        applied = h.apply_transient(tree)
        self.assertEqual((applied["status"], applied["kind"], applied["evaluation_baseline"], applied["recovery_revision"]), ("applied", "attempt-recovery", 1, "ar1"), applied)
        self.assertEqual((applied["execute_jobs"], applied["reuse_jobs"]), ([JOB_A], [JOB_B]))
        self.assertEqual((applied["execute_rules"], applied["reuse_rules"]), ([RULE_A], [RULE_B]))
        self.assertEqual(applied["attempt_id"], state["attempt_id"])
        recovery = _read(h.baseline_dir(1) / "recovery.json")
        self.assertEqual((recovery["kind"], recovery["recovery_revision"], recovery["execute_jobs"]), ("attempt-recovery", "ar1", [JOB_A]))
        # COMMIT 冻结的读来源：候选阶段与 compare 为 local（段 seal 后写出），其余按基线协议。
        self.assertEqual(applied["stage_sources"]["capture-candidate"]["source"], "local", applied["stage_sources"])
        self.assertEqual(_read(h.baseline_dir(1) / "COMMIT")["stage_sources"]["capture-candidate"]["source"], "local")

        ar_run = h.ar_run(tree, "ar-run", applied)
        self.assertEqual((ar_run["returncode"], ar_run["campaign_run"]["reason"]), (0, "queue-complete"), ar_run)
        summary = h.assert_segment_awaiting_receipts("ar1", execute_jobs=[JOB_A])
        self.assertEqual(summary["attempt_id"], state["attempt_id"])
        self.assertEqual(summary["evaluation_baseline"], 1)
        # Job-B 原证据根未被触碰；Job-A 的新证据在重定位根内。
        self.assertTrue((Path(state["job_roots"][JOB_B]) / "relay" / "conn001.client_to_upstream.bin").is_file())
        self.assertTrue((Path(state["job_roots"][JOB_A] + "-recovery-ar1") / "traces" / "surface.observation.jsonl").is_file())
        events = h.ledger_event_types()
        self.assertIn("attempt_recovery_started", events)
        self.assertIn("attempt_recovery_completed", events)
        if not self._segment_stage_available():
            self.skipTest(SEGMENT_SKIP_REASON)

        h.ar_prepare(tree, applied)
        sealed = h.ar_seal(tree, "ar-seal", applied)
        self.assertEqual((sealed["returncode"], sealed["campaign_run"]["reason"]), (0, "queue-complete"), sealed)
        effective = h.effective_results(1)
        by_job = {str(row["job_id"]): row for row in effective["entries"]}
        self.assertEqual(set(by_job), {JOB_A, JOB_B})
        self.assertEqual((by_job[JOB_A]["source"], by_job[JOB_B]["source"]), ("recovered", "reused"), by_job)
        self.assertEqual(by_job[JOB_A]["recovery_revision"], "ar1")
        self.assertEqual(by_job[JOB_B]["baseline"], 0)
        # 投影：只丢 Job-A 原根、旧 evidence、旧 logs 三个根（收据记根路径），Job-B 根逐字保留。
        projection = h.projection_receipt(1)
        self.assertEqual(sorted(Path(root).name for root in projection["dropped_roots"]), sorted([Path(state["job_roots"][JOB_A]).name, "evidence", "logs"]), projection)
        self.assertIn(Path(state["job_roots"][JOB_B]).name, [Path(root).name for root in projection["kept_roots"]], projection)
        self.assertTrue((h.baseline_dir(1) / "result.json").is_file())
        accounted = h.ar_account(tree)
        self.assertEqual(accounted["recovery_revision"], "ar1", accounted)
        # 候选证据变化 → b1 的 compare 为 local：先重跑 compare（读增量封存结果），再断言／验收。
        compare1 = h.dispatch(tree, "compare1", ["compare"], baseline=1)
        self.assertEqual((compare1["returncode"], compare1["campaign_run"]["reason"]), (0, "queue-complete"), compare1)
        h.run(tree, "gate", "--tag", "b1")
        b1 = h.dispatch(tree, "b1", ["assert", "accept"], baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        self.assertEqual({action["action_id"]: action["status"] for action in b1["campaign_run"]["actions"]}, {"vc5-1-assert": "passed", "vc5-2-accept": "passed"})
        h.assert_b1_one_reused_one_rerun(1, rerun={RULE_A}, reused={RULE_B})
        h.assert_accepted_to_completion(1)
        # b0 只读：原基线评估索引与验收目录未被改写。
        self.assertEqual({row["rule"]: row["status"] for row in h.index(0)["rules"]}, {RULE_A: "fail", RULE_B: "pass"})

    # ------------------------------------------------------------------
    # 用例 2：两 Job 同在 failure-scope → J* 为全集 → 两 Job 补跑、两条规则重跑
    # ------------------------------------------------------------------

    def test_transient_recovery_with_both_jobs_in_failure_scope(self) -> None:
        h = self.harness
        tree = h.tree_b()
        state = h.init_ar(tree, candidate_surface="other", h1_method="GET")
        compare0 = h.dispatch(tree, "compare0", ["compare"])
        self.assertEqual((compare0["returncode"], compare0["campaign_run"]["reason"]), (0, "queue-complete"), compare0)
        h.run(tree, "gate", "--tag", "b")
        b0 = h.dispatch(tree, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual((b0["returncode"], b0["campaign_run"]["reason"]), (1, "action-failed:vc5-1-assert"), b0)
        self.assertEqual({row["rule"]: row["status"] for row in h.index(0)["rules"]}, {RULE_A: "fail", RULE_B: "fail"})
        self.assertEqual(h.run(tree, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")
        preview = h.preview(tree)
        self.assertEqual(sorted(preview["failure_scope"]["jobs"]), sorted([JOB_A, JOB_B]), preview["failure_scope"])
        applied = h.apply_transient(tree)
        self.assertEqual((applied["status"], applied["kind"], applied["recovery_revision"]), ("applied", "attempt-recovery", "ar1"), applied)
        self.assertEqual((sorted(applied["execute_jobs"]), applied["reuse_jobs"]), (sorted([JOB_A, JOB_B]), []))
        self.assertEqual((sorted(applied["execute_rules"]), applied["reuse_rules"]), (sorted(RULES), []))
        ar_run = h.ar_run(tree, "ar-run", applied)
        self.assertEqual((ar_run["returncode"], ar_run["campaign_run"]["reason"]), (0, "queue-complete"), ar_run)
        h.assert_segment_awaiting_receipts("ar1", execute_jobs=[JOB_A, JOB_B])
        if not self._segment_stage_available():
            self.skipTest(SEGMENT_SKIP_REASON)
        h.ar_prepare(tree, applied)
        sealed = h.ar_seal(tree, "ar-seal", applied)
        self.assertEqual((sealed["returncode"], sealed["campaign_run"]["reason"]), (0, "queue-complete"), sealed)
        by_job = {str(row["job_id"]): row["source"] for row in h.effective_results(1)["entries"]}
        self.assertEqual(by_job, {JOB_A: "recovered", JOB_B: "recovered"})
        h.ar_account(tree)
        self.assertEqual(h.dispatch(tree, "compare1", ["compare"], baseline=1)["returncode"], 0)
        h.run(tree, "gate", "--tag", "b1")
        b1 = h.dispatch(tree, "b1", ["assert", "accept"], baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        rows = {row["rule"]: row for row in h.index(1)["rules"]}
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in rows.values()), rows)
        h.assert_accepted_to_completion(1)

    # ------------------------------------------------------------------
    # 用例 3：多基线——compare-reader 缺陷恢复 b0→b1（evaluator-only，断言 pending 全部重跑）后断言 fail
    # （真实候选证据；失败动作与 b0 的 compare 失败根因不同，不触发同根因上限）→ transient 恢复 b1→b2
    # （前序清单取 b1 实际 manifest，即 b1 复用的 b0 封存清单）→ b2 一条复用一条重跑 → completion 绑定 b2
    # ------------------------------------------------------------------

    def test_evaluator_defect_then_transient_recovery_reaches_b2(self) -> None:
        h = self.harness
        tree_d, tree_b = h.tree_d(), h.tree_b()
        state = h.init_ar(tree_d, candidate_surface="other")
        compare0 = h.dispatch(tree_d, "compare0", ["compare"])
        self.assertEqual((compare0["returncode"], compare0["campaign_run"]["reason"]), (1, "action-failed:vc5-1-compare"), compare0)
        self.assertEqual(h.run(tree_d, "reconcile", "--run-dir", compare0["campaign_run"]["run_dir"])["status"], "recoverable")
        preview = h.preview(tree_b)
        self.assertEqual((preview["failure_source"], preview["reuse_authority"]), ("offline-compare-failed", "none"), preview)
        applied1 = h.apply_fix(tree_b, h.deployment_receipt(tree_b, "fix-b"))
        self.assertEqual((applied1["status"], applied1["evaluation_baseline"], applied1["kind"]), ("applied", 1, "evaluator-only"), applied1)
        self.assertEqual(h.dispatch(tree_b, "compare1", ["compare"], baseline=1)["returncode"], 0)
        h.run(tree_b, "gate", "--tag", "b")
        b1 = h.dispatch(tree_b, "b1", ["assert"], baseline=1, authority="none", reuse_items=["compare"])
        # 修复副本下的真实判定：SPEC-EP-006 fail（候选 surface 错），SPEC-H1-001 pass。
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (1, "action-failed:vc5-1-assert"), b1)
        self.assertEqual({row["rule"]: row["status"] for row in h.index(1)["rules"]}, {RULE_A: "fail", RULE_B: "pass"})
        self.assertEqual(h.run(tree_b, "reconcile", "--run-dir", b1["campaign_run"]["run_dir"])["status"], "recoverable")
        # transient：b1→b2（J*={Job-A}，ar1）；前序基线 b1 的候选阶段是 reuse→b0。
        applied2 = h.apply_transient(tree_b)
        self.assertEqual((applied2["status"], applied2["kind"], applied2["evaluation_baseline"], applied2["recovery_revision"]), ("applied", "attempt-recovery", 2, "ar1"), applied2)
        self.assertEqual((applied2["execute_jobs"], applied2["execute_rules"], applied2["reuse_rules"]), ([JOB_A], [RULE_A], [RULE_B]))
        recovery2 = _read(h.baseline_dir(2) / "recovery.json")
        self.assertEqual(recovery2["previous_baseline"], 1)
        ar_run = h.ar_run(tree_b, "ar-run", applied2)
        self.assertEqual((ar_run["returncode"], ar_run["campaign_run"]["reason"]), (0, "queue-complete"), ar_run)
        summary = h.assert_segment_awaiting_receipts("ar1", execute_jobs=[JOB_A])
        self.assertEqual(summary["evaluation_baseline"], 2)
        if not self._segment_stage_available():
            self.skipTest(SEGMENT_SKIP_REASON)
        h.ar_prepare(tree_b, applied2)
        sealed = h.ar_seal(tree_b, "ar-seal", applied2)
        self.assertEqual((sealed["returncode"], sealed["campaign_run"]["reason"]), (0, "queue-complete"), sealed)
        effective = h.effective_results(2)
        by_job = {str(row["job_id"]): row for row in effective["entries"]}
        self.assertEqual((by_job[JOB_A]["source"], by_job[JOB_B]["source"], by_job[JOB_B]["baseline"]), ("recovered", "reused", 1))
        h.ar_account(tree_b)
        self.assertEqual(h.dispatch(tree_b, "compare2", ["compare"], baseline=2)["returncode"], 0)
        h.run(tree_b, "gate", "--tag", "b2")
        b1_index = h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1" / "evaluation-run.json"
        b2 = h.dispatch(tree_b, "b2", ["assert", "accept"], baseline=2, reuse_from=b1_index, authority="anchored", reuse_items=["compare"])
        self.assertEqual((b2["returncode"], b2["campaign_run"]["reason"]), (0, "queue-complete"), b2)
        h.assert_b1_one_reused_one_rerun(2, rerun={RULE_A}, reused={RULE_B})
        h.assert_accepted_to_completion(2)

    # ------------------------------------------------------------------
    # E1～E5 的 attempt-recovery 变体：transient apply 在五个写点逐点崩溃并续作收敛
    # ------------------------------------------------------------------

    def test_transient_apply_crash_points_e1_to_e5_resume(self) -> None:
        h = self.harness
        tree = h.tree_b()
        h.init_ar(tree, candidate_surface="other")
        self.assertEqual(h.dispatch(tree, "compare0", ["compare"])["returncode"], 0)
        h.run(tree, "gate", "--tag", "b")
        b0 = h.dispatch(tree, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual(b0["returncode"], 1)
        self.assertEqual(h.run(tree, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")
        baseline_dir = h.baseline_dir(1)

        def present(*names: str) -> list[bool]:
            return [(baseline_dir / name).is_file() for name in names]

        h.apply_transient(tree, crash_at="e1", expect_exit=137)
        self.assertEqual(present("diagnosis.json", "recovery.json", "PREPARED", "AUTHORIZATION", "COMMIT"), [True, True, True, False, False])
        recovery_bytes = (baseline_dir / "recovery.json").read_bytes()
        self.assertEqual(_read(baseline_dir / "recovery.json")["kind"], "attempt-recovery")
        h.apply_transient(tree, crash_at="e2", expect_exit=137)
        self.assertEqual(present("AUTHORIZATION", "COMMIT"), [False, False])
        h.apply_transient(tree, crash_at="e3", expect_exit=137)
        self.assertEqual(present("AUTHORIZATION", "COMMIT"), [False, False])
        h.apply_transient(tree, crash_at="e4", expect_exit=137)
        self.assertEqual(present("AUTHORIZATION", "COMMIT"), [True, False])
        h.apply_transient(tree, crash_at="e5", expect_exit=137)
        self.assertEqual(present("AUTHORIZATION", "COMMIT"), [True, True])
        self.assertIsNone(h.summary()["current_evaluation_baseline"])
        applied = h.apply_transient(tree)
        self.assertEqual((applied["status"], applied["evaluation_baseline"], applied["recovery_revision"]), ("applied", 1, "ar1"), applied)
        self.assertEqual((baseline_dir / "recovery.json").read_bytes(), recovery_bytes)
        current = h.summary()["current_evaluation_baseline"]
        self.assertEqual((current["evaluation_baseline"], current["baseline_kind"], current["recovery_revision"]), (1, "attempt-recovery", "ar1"))
        # 基线已激活后段 run 正常开段（预约绑定同一 recovery_sha256）。
        ar_run = h.ar_run(tree, "ar-run", applied)
        self.assertEqual((ar_run["returncode"], ar_run["campaign_run"]["reason"]), (0, "queue-complete"), ar_run)
        h.assert_segment_awaiting_receipts("ar1", execute_jobs=[JOB_A])

    # ------------------------------------------------------------------
    # 段 Job 真实失败（段 status=failed，父 run 正常终态 action-failed；恢复段失败归可恢复→recovery_required）
    # → 父 run 对账分流到段对账 → 段对账／批准／授权 → 后继段 ar2 成功 → seal／accept 用 ar2。
    # 注：M1 的 R2（父 run 在 post-run-tooling 收据前崩溃）对段 run 动作没有对应物——post-run-tooling
    # 收据只属于零请求后处理动作；父 run 在段动作成功后崩溃属既有 watchdog-aborted 合同（人工停线）。
    # ------------------------------------------------------------------

    def test_segment_job_failure_reconciles_segment_and_opens_successor(self) -> None:
        h = self.harness
        tree = h.tree_b()
        state = h.init_ar(tree, candidate_surface="other")
        self.assertEqual(h.dispatch(tree, "compare0", ["compare"])["returncode"], 0)
        h.run(tree, "gate", "--tag", "b")
        b0 = h.dispatch(tree, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual(b0["returncode"], 1)
        self.assertEqual(h.run(tree, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")
        applied = h.apply_transient(tree)
        self.assertEqual(applied["status"], "applied", applied)
        failed = h.ar_run(tree, "ar-run", applied, segment_fail=True)
        self.assertEqual((failed["returncode"], failed["campaign_run"]["reason"]), (1, "action-failed:vc5-1-ar-run"), failed)
        self.assertEqual(failed["campaign_run"]["timing_closeout"]["ledger_status"], "recovery_required", failed["campaign_run"])
        segment_summary = h.segment_summary("ar1")
        self.assertEqual(segment_summary["status"], "failed", segment_summary)
        # 父 run 对账被分流到段对账（段未成功收口）。
        rejected = h.run(tree, "reconcile", "--run-dir", failed["campaign_run"]["run_dir"], expect_exit=1)
        self.assertIn("reconcile-attempt", rejected["stderr"])
        reconciled = h.run(tree, "reconcile-ar", "--recovery-revision", "ar1", "--approve")
        self.assertEqual((reconciled["status"], reconciled["jobs"]["failed"]), ("recoverable", [JOB_A]), reconciled)
        self.assertEqual(reconciled["authorized"], "authorized")
        ledger = h.summary()
        self.assertEqual((ledger["status"], ledger["attempt_recoveries"][f"{state['attempt_id']}:ar1"]["status"]), ("active", "failed"), ledger)
        successor = dict(applied, recovery_revision="ar2")
        ar2 = h.ar_run(tree, "ar-run-2", successor, recovery_preview=reconciled["recovery_preview_path"])
        self.assertEqual((ar2["returncode"], ar2["campaign_run"]["reason"]), (0, "queue-complete"), ar2)
        h.assert_segment_awaiting_receipts("ar2", execute_jobs=[JOB_A])
        if not self._segment_stage_available():
            self.skipTest(SEGMENT_SKIP_REASON)
        h.ar_prepare(tree, successor)
        sealed = h.ar_seal(tree, "ar-seal", successor)
        self.assertEqual((sealed["returncode"], sealed["campaign_run"]["reason"]), (0, "queue-complete"), sealed)
        h.ar_account(tree)
        self.assertEqual(h.dispatch(tree, "compare1", ["compare"], baseline=1)["returncode"], 0)
        h.run(tree, "gate", "--tag", "b1")
        b1 = h.dispatch(tree, "b1", ["assert", "accept"], baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        h.assert_accepted_to_completion(1)

    # ------------------------------------------------------------------
    # 同段幂等重派：段成功收口后（父 run 正常终态）以逐字相同的批次内容重派 → 段 run 幂等返回（零请求）
    # ------------------------------------------------------------------

    def test_sealed_segment_redispatch_is_idempotent(self) -> None:
        h = self.harness
        tree = h.tree_b()
        h.init_ar(tree, candidate_surface="other")
        self.assertEqual(h.dispatch(tree, "compare0", ["compare"])["returncode"], 0)
        h.run(tree, "gate", "--tag", "b")
        b0 = h.dispatch(tree, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual(b0["returncode"], 1)
        self.assertEqual(h.run(tree, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")
        applied = h.apply_transient(tree)
        first = h.ar_run(tree, "ar-run", applied)
        self.assertEqual((first["returncode"], first["campaign_run"]["reason"]), (0, "queue-complete"), first)
        digest_before = h.assert_segment_awaiting_receipts("ar1", execute_jobs=[JOB_A])["attempt_recovery_digest"]
        again = h.ar_run(tree, "ar-run-again", applied)
        self.assertEqual((again["returncode"], again["campaign_run"]["reason"]), (0, "queue-complete"), again)
        self.assertEqual(h.segment_summary("ar1")["attempt_recovery_digest"], digest_before)
        self.assertEqual(h.ledger_event_types().count("attempt_recovery_started"), 1)
        self.assertEqual(h.ledger_event_types().count("attempt_recovery_completed"), 1)

    # ------------------------------------------------------------------
    # A1：段 run 动作在 Job 证据落盘后、段摘要写出前 SIGKILL → 父 run failed（恢复段失败归可恢复，
    # 账本 recovery_required）→ 父 run 对账分流到段对账 → reconcile-attempt --recovery-revision ar1
    # （attempt_recovery_failed，根因 attempt.interrupted）→ 批准恢复预览 → 后继段 ar2 全量补跑
    # （recovery_authorized → active）→ 段 seal／入账用 ar2 → b1 一条复用一条重跑 → completion
    # ------------------------------------------------------------------

    def test_a1_segment_interrupted_then_successor_segment_recovers(self) -> None:
        h = self.harness
        tree = h.tree_b()
        state = h.init_ar(tree, candidate_surface="other")
        self.assertEqual(h.dispatch(tree, "compare0", ["compare"])["returncode"], 0)
        h.run(tree, "gate", "--tag", "b")
        b0 = h.dispatch(tree, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual(b0["returncode"], 1)
        self.assertEqual(h.run(tree, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])["status"], "recoverable")
        applied = h.apply_transient(tree)
        self.assertEqual(applied["status"], "applied", applied)
        crashed = h.ar_run(tree, "ar-run-crash", applied, crash_at="segment-job")
        self.assertEqual((crashed["returncode"], crashed["campaign_run"]["reason"]), (1, "action-failed:vc5-1-ar-run"), crashed)
        self.assertEqual(crashed["campaign_run"]["actions"][0]["effective_failure_class"], "execution-failure")
        segment = h.segment_root("ar1")
        self.assertTrue((segment / "recovery-reservation.json").is_file())
        self.assertFalse((segment / "attempt-recovery.json").exists())
        self.assertFalse((segment / f"job-{JOB_A}.json").exists())
        self.assertTrue((Path(state["job_roots"][JOB_A] + "-recovery-ar1") / "traces" / "surface.observation.jsonl").is_file())
        # 恢复段动作失败不是候选级失败：阶段保持 active、账本 recovery_required（不进 candidate_review_required）。
        ledger = h.summary()
        self.assertEqual((ledger["status"], ledger["active_phase"]), ("recovery_required", "VC-5"), ledger)
        # 父 run 对账被分流到段对账。
        rejected = h.run(tree, "reconcile", "--run-dir", crashed["campaign_run"]["run_dir"], expect_exit=1)
        self.assertIn("reconcile-attempt", rejected["stderr"])
        self.assertIn("ar1", rejected["stderr"])
        reconciled = h.run(tree, "reconcile-ar", "--recovery-revision", "ar1", "--approve")
        self.assertEqual((reconciled["status"], reconciled["recovery_revision"]), ("recoverable", "ar1"), reconciled)
        self.assertEqual(reconciled["root_cause"]["stable_error_code"], "attempt.interrupted")
        self.assertEqual(reconciled["approval_sha256"], reconciled["review_sha256"])
        self.assertEqual(h.summary()["attempt_recoveries"][f"{state['attempt_id']}:ar1"]["status"], "failed")
        # 后继段 ar2：--rerun-failed + 已批准预览；recovery_authorized 让账本回到 active 后再开段。
        successor = dict(applied, recovery_revision="ar2")
        ar2 = h.ar_run(tree, "ar-run-2", successor, recovery_preview=reconciled["recovery_preview_path"])
        self.assertEqual((ar2["returncode"], ar2["campaign_run"]["reason"]), (0, "queue-complete"), ar2)
        h.assert_segment_awaiting_receipts("ar2", execute_jobs=[JOB_A])
        ledger = h.summary()
        self.assertEqual(ledger["status"], "active", ledger)
        self.assertEqual(ledger["attempt_recoveries"][f"{state['attempt_id']}:ar1"]["status"], "failed")
        self.assertEqual(ledger["attempt_recoveries"][f"{state['attempt_id']}:ar2"]["status"], "completed")
        self.assertEqual(ledger["current_evaluation_baseline"]["recovery_revision"], "ar1")
        self.assertIn("recovery_authorized", h.ledger_event_types())
        if not self._segment_stage_available():
            self.skipTest(SEGMENT_SKIP_REASON)
        h.ar_prepare(tree, successor)
        sealed = h.ar_seal(tree, "ar-seal", successor)
        self.assertEqual((sealed["returncode"], sealed["campaign_run"]["reason"]), (0, "queue-complete"), sealed)
        by_job = {str(row["job_id"]): row for row in h.effective_results(1)["entries"]}
        self.assertEqual((by_job[JOB_A]["source"], by_job[JOB_A]["recovery_revision"], by_job[JOB_B]["source"]), ("recovered", "ar2", "reused"))
        accounted = h.ar_account(tree)
        self.assertEqual(accounted["recovery_revision"], "ar2", accounted)
        self.assertEqual(h.dispatch(tree, "compare1", ["compare"], baseline=1)["returncode"], 0)
        h.run(tree, "gate", "--tag", "b1")
        b1 = h.dispatch(tree, "b1", ["assert", "accept"], baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        h.assert_b1_one_reused_one_rerun(1, rerun={RULE_A}, reused={RULE_B})
        h.assert_accepted_to_completion(1)


if __name__ == "__main__":
    unittest.main()
