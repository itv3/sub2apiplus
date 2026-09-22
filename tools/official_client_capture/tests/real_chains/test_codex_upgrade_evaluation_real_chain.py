"""改造 5（评估失败局部恢复）M1 审核修正：真实评估链端到端（T5.10，副本受管树 + 子进程）。

每条链都在**副本受管树**上以正式派发入口（``compile-and-run-vc-batch``、staging 模型、正式监督器）执行，
动作是真实 CLI ``compare``／真实 builder＋真实 checker／真实 CLI ``accept``；父进程只准备非受管输入
（副本树、部署收据 fixture）并断言结果，不用原仓库创建或推进受管 Campaign（老板拍板 4.1）。

候选身份（老板 2026-09-20 二次拍板 A 受限版）：docker 与 go 可用（linux/arm64）时，驱动 ``init`` 以真实
``plan-candidate-gates``／``record-candidate-build`` 建立 0.154 候选身份（真实源码树、二进制、镜像与
VC-4 构建收据），accept 因而真实运行到 AcceptanceFact 与 VC-5 completion；不可用时（开发机无 docker）
用例 1～3 只验证到 b1 断言，随后如实标记 skip（accept／completion 段未执行，不把失败当预期）。
有固定采集执行副本（``/root/oauth-capture``）的机器上，每个受管子进程在独立 mount namespace 内把该固定
路径绑定到当前副本树，执行树一致性校验原样生效，不放宽（``managed_tree_copy.python_command``）。

* 用例 1（checker 缺陷）：正式 Campaign 在缺陷副本 A（checker 对 SPEC-EP-006 误判）下建立并执行 b0 →
  父 run failed（post-run-tooling）→ reconcile → 修复副本 B（原 checker）：无 epoch 的 apply 被拒 →
  ``evaluation-epoch`` → apply（evaluator-defect，部署收据按 B 身份）→ b1 committed（两条规则全部重跑）→
  派发 b1（后继协议）→ compare 重跑、断言全部重跑全 pass、accept 真实 CLI 动作通过 → VC-5 completion
  绑定 b1 的 AcceptanceFact——候选证据全程不换；
* 用例 2（accept-reader 缺陷，副本 C）：b0 断言全 pass、accept 动作失败 → offline-accept-failed／anchored →
  apply（accept_reader 变化、无需 epoch）→ b1 断言全部复用（零 checker）、accept 通过 → completion；
* 用例 3（compare-reader 缺陷，副本 D）：b0 compare 动作失败 → offline-compare-failed／none → apply →
  b1 compare 重跑、断言全部 pending 重跑、accept 通过 → completion；
* R2／R2-b：父进程在 post-run-tooling 收据前／动作输出绑定前被 SIGKILL → monitor 封存 → reconciler 补写
  → 继续恢复到 b1；
* E1～E5：apply 在状态机五个写点逐点崩溃并续作收敛；
* C1：正式监督器批次内的 accept 动作（真实 CLI）在 VC-5 completion 写出前崩溃（子进程内按开关文件 patch
  ``_complete_vc_with_receipt``）→ 父 run failed／stop-receipt → reconcile → 环境恢复重派（与原批次逐字
  相同）→ accept 整份重放一致即复用既有 AcceptanceFact（字节不变）只补 completion；b0 变体带三条漂移负例，
  b1 变体在 evaluator 恢复链之后以 ``[assert, accept]`` 整批崩溃／整批重派（assert 零 checker）；
* 后继协议伪造负例（篡改 b1 COMMIT）。

本文件位于 tests/，不进受管摘要。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import candidate_identity_fixture as cif
from tools.official_client_capture.tests import managed_tree_copy as mtc

DRIVER = "tools.official_client_capture.tests.evaluation_chain_driver"
TREE_ROOT_ENV = "EVALUATION_CHAIN_TREE_ROOT"
CANDIDATE = "candidate-r1"
RULES = ("SPEC-H1-001", "SPEC-EP-006")


# 候选身份夹具（docker + go，linux/arm64）可用时 accept／VC-5 completion 段真实执行；模块导入时判定一次。
CANDIDATE_IDENTITY_AVAILABLE = cif.available()
ACCEPT_SKIP_REASON = "accept／VC-5 completion 段需要候选身份夹具（docker 与 go，linux/arm64），本机不可用"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage_fingerprint(document: dict) -> str:
    """与受管 ``_fingerprint`` 同口径（紧凑 JSON、无尾换行），供负例重算封存 package_digest。"""

    return hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


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

    def batch(self, dispatched: dict) -> dict:
        return _read(self.campaign_dir() / "control" / "vc" / "batches" / f"{int(dispatched['batch_sequence']):04d}-vc-5.json")

    def acceptance_path(self, baseline: int) -> Path:
        base = self.campaign_dir() / "acceptance" / CANDIDATE
        return base / "result.json" if baseline == 0 else base / "revisions" / f"b{baseline}" / "result.json"

    def vc5_checkpoint_path(self) -> Path:
        return self.campaign_dir() / "control" / "vc" / "vc-5-checkpoint.json"

    def vc5_completion_path(self) -> Path:
        return self.campaign_dir() / "control" / "vc" / "receipts" / CANDIDATE / "vc5-completion.json"

    def assert_accepted_to_completion(self, baseline: int) -> dict:
        """AcceptanceFact 通过、VC-5 checkpoint 与 completion 收据存在且逐字节绑定该基线的验收结果。"""

        acceptance_path = self.acceptance_path(baseline)
        self.case.assertTrue(acceptance_path.is_file(), acceptance_path)
        acceptance = _read(acceptance_path)
        self.case.assertEqual((acceptance["accepted"], acceptance["status"], acceptance["failed_gates"]), (True, "complete", []), acceptance.get("gates"))
        self.case.assertTrue(all(acceptance["gates"].values()), acceptance["gates"])
        self.case.assertEqual(acceptance["candidate_identity"]["build_receipt_digest"], self.state()["identity"]["build_receipt_digest"])
        self.case.assertTrue(acceptance_path.with_name("evidence-seal.json").is_file())
        checkpoint = _read(self.vc5_checkpoint_path())
        self.case.assertEqual((checkpoint["phase"], checkpoint["status"]), ("VC-5", "complete"))
        self.case.assertEqual(checkpoint["stage_receipt"]["path"], self.vc5_completion_path().relative_to(self.campaign_dir()).as_posix())
        self.case.assertEqual(checkpoint["stage_receipt"]["sha256"], _sha256(self.vc5_completion_path()))
        completion = _read(self.vc5_completion_path())
        self.case.assertEqual((completion["kind"], completion["status"], completion["assertions"]["acceptance_passed"]), ("vc5_completion", "complete", True))
        self.case.assertEqual(
            [(row["role"], row["path"], row["sha256"]) for row in completion["evidence"]],
            [("acceptance_fact", acceptance_path.relative_to(self.campaign_dir()).as_posix(), _sha256(acceptance_path))],
        )
        self.case.assertEqual(completion["subject"]["candidate_id"], CANDIDATE)
        return acceptance

    def accept_direct(self, tree_root: Path, *, crash_at: str = "", expect_exit: int = 0) -> dict:
        arguments = ["accept-direct"]
        if crash_at:
            arguments.extend(["--crash-at", crash_at])
        return self.run(tree_root, *arguments, expect_exit=expect_exit)

    def rewrite_sealed_acceptance(self, baseline: int, mutate, *, reseal: bool = True) -> None:
        """父进程篡改已封存验收结果（负例夹具）：``mutate(document)`` 后按封存算法重算 package_digest
        （``reseal=False`` 时故意不重算），保持 0o600。"""

        path = self.acceptance_path(baseline)
        document = _read(path)
        mutate(document)
        if reseal:
            document.pop("package_digest", None)
            document["package_digest"] = _stage_fingerprint(document)
        path.chmod(0o600)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # ---- 常用链段 ---------------------------------------------------------------

    def init(self, tree_root: Path, *, candidate_surface: str = "codex") -> dict:
        arguments = ["init", "--candidate-surface", candidate_surface]
        if not CANDIDATE_IDENTITY_AVAILABLE:
            arguments.append("--no-candidate-identity")
        state = self.run(tree_root, *arguments)
        identity = state.get("candidate_identity")
        if identity:
            # 一次性 registry 容器与候选镜像属非受管夹具，测试结束时由父进程清理。
            self.case.addCleanup(cif.cleanup_identity, identity)
        return state

    def dispatch(self, tree_root: Path, tag: str, actions: list[str], *, baseline: int = 0, reuse_from: Path | None = None, authority: str = "none", reuse_items: list[str] | None = None, crash_at: str = "", accept_wrapper: bool = False, expect_exit: int = 0) -> dict:
        # 全局批次序号由驱动按既有 COMMIT 推算（真实 record-candidate-build 不占序号）。
        arguments = ["dispatch", "--tag", tag, "--actions", *actions, "--baseline", str(baseline), "--authority", authority]
        if accept_wrapper:
            arguments.append("--accept-wrapper")
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


class RealEvaluationChainTests(unittest.TestCase):
    def setUp(self) -> None:
        # 采集执行位置是合同常量（execution_contract.capture_root＝/root/oauth-capture）。有固定执行副本的机器上
        # 受管子进程在独立 mount namespace 内把该路径绑定到当前副本树（需要 root 与 unshare）；建不起来才跳过。
        if mtc.execution_tree_binding_required() and not mtc.execution_tree_binding_available():
            self.skipTest(f"本机存在固定采集执行副本 {mtc.PRODUCTION_EXECUTION_TREE}，但无法建立 mount namespace 绑定（需要 root 与 unshare）")
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
        compare0 = h.dispatch(tree_a, "compare0", ["compare"])
        self.assertEqual((compare0["returncode"], compare0["campaign_run"]["reason"]), (0, "queue-complete"))
        b0 = h.dispatch(tree_a, "b0", ["assert", "accept"], reuse_items=["compare"])
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
        batch0 = h.batch(b0)
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

        # ---- 派发 b1（评估基线后继协议）：compare 重跑 → 断言全部重跑全 pass → accept 真实 CLI 动作 → completion ----
        compare1 = h.dispatch(tree_b, "compare1", ["compare"], baseline=1)
        self.assertEqual((compare1["returncode"], compare1["campaign_run"]["reason"]), (0, "queue-complete"), compare1)
        batch1 = h.batch(compare1)
        self.assertEqual((batch1["evaluation_baseline"], batch1["baseline_commit_sha256"], batch1["evaluator_digests"]), (1, applied["commit_sha256"], digests_b))
        self.assertTrue((h.campaign_dir() / "comparisons" / CANDIDATE / "revisions" / "b1" / "result.json").is_file())
        h.run(tree_b, "gate", "--tag", "b")
        b1_actions = ["assert", "accept"] if CANDIDATE_IDENTITY_AVAILABLE else ["assert"]
        b1 = h.dispatch(tree_b, "b1", b1_actions, baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        actions = {action["action_id"]: action for action in b1["campaign_run"]["actions"]}
        self.assertEqual(actions["vc5-1-assert"]["status"], "passed", b1)
        index1 = h.index(1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in index1["rules"]))
        self.assertEqual(index1["evaluator"], digests_b)
        self.assertTrue((h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1" / "results.json").is_file())
        for checkpoint in h.checkpoints(1):
            self.assertEqual(checkpoint["checker_sha256"], digests_b["checker_sha256"])
        if not CANDIDATE_IDENTITY_AVAILABLE:
            self.skipTest(ACCEPT_SKIP_REASON)
        # accept 真实 CLI 动作在正式监督器内执行：断言读侧（基线授权身份、checkpoint 一致、epoch 回读、
        # 复用锚点）与候选身份（VC-4 构建收据真实重放）全部通过 → AcceptanceFact → VC-5 completion 绑定 b1。
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"], actions["vc5-2-accept"]["status"]), (0, "queue-complete", "passed"), b1)
        acceptance = h.assert_accepted_to_completion(1)
        self.assertEqual(acceptance["assertions"], {"complete": True, "failed_rules": [], "not_applicable_count": 0, "pass_count": len(RULES), "rule_count": len(RULES)})
        self.assertFalse(h.acceptance_path(0).exists())  # b0 从未到达 accept

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
        self.assertEqual(h.dispatch(tree_c, "compare0", ["compare"])["returncode"], 0)
        b0 = h.dispatch(tree_c, "b0", ["assert", "accept"], reuse_items=["compare"])
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
        # b1：断言批次全部复用（reused checkpoint 指向 b0 链，checker 调用 0），accept 真实 CLI 动作 → completion。
        h.run(tree_b, "gate", "--tag", "b")
        b1_actions = ["assert", "accept"] if CANDIDATE_IDENTITY_AVAILABLE else ["assert"]
        b1 = h.dispatch(tree_b, "b1", b1_actions, baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        actions = {action["action_id"]: action for action in b1["campaign_run"]["actions"]}
        self.assertEqual(actions["vc5-1-assert"]["status"], "passed", b1)
        index1 = h.index(1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is not None and row["reused_from"]["baseline"] == 0 for row in index1["rules"]))
        b1_machine = h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1" / "machine"
        self.assertFalse((b1_machine / "candidate" / "SPEC-H1-001.json").exists())  # 零 checker：没有新文档
        self.assertTrue(all(checkpoint["reused_from"] is not None for checkpoint in h.checkpoints(1)))
        if not CANDIDATE_IDENTITY_AVAILABLE:
            self.skipTest(ACCEPT_SKIP_REASON)
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"], actions["vc5-2-accept"]["status"]), (0, "queue-complete", "passed"), b1)
        h.assert_accepted_to_completion(1)
        # b0 的 accept 动作在注入缺陷处失败关闭，没有留下（阻断）验收结果。
        self.assertFalse(h.acceptance_path(0).exists())

    # ------------------------------------------------------------------
    # 用例 3：compare-reader 缺陷 → offline-compare-failed／none → b1 全部 pending 重跑
    # ------------------------------------------------------------------

    def test_compare_reader_defect_chain_reruns_pending(self) -> None:
        h = self.harness
        tree_d, tree_b = h.tree_d(), h.tree_b()
        h.init(tree_d)
        compare0 = h.dispatch(tree_d, "compare0", ["compare"])
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
        compare1 = h.dispatch(tree_b, "compare1", ["compare"], baseline=1)
        self.assertEqual((compare1["returncode"], compare1["campaign_run"]["reason"]), (0, "queue-complete"), compare1)
        h.run(tree_b, "gate", "--tag", "b")
        b1_actions = ["assert", "accept"] if CANDIDATE_IDENTITY_AVAILABLE else ["assert"]
        b1 = h.dispatch(tree_b, "b1", b1_actions, baseline=1, authority="none", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        index1 = h.index(1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in index1["rules"]))
        self.assertTrue((h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1" / "results.json").is_file())
        if not CANDIDATE_IDENTITY_AVAILABLE:
            self.skipTest(ACCEPT_SKIP_REASON)
        self.assertEqual({action["action_id"]: action["status"] for action in b1["campaign_run"]["actions"]}, {"vc5-1-assert": "passed", "vc5-2-accept": "passed"})
        h.assert_accepted_to_completion(1)

    # ------------------------------------------------------------------
    # R2：父进程在 post-run-tooling 收据前 SIGKILL → monitor 封存 → reconciler 补写 → 恢复到 b1
    # ------------------------------------------------------------------

    def test_r2_owner_loss_after_binding_is_sealed_backfilled_and_recovered(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, "compare0", ["compare"])["returncode"], 0)
        crashed = h.dispatch(tree_a, "b0", ["assert"], reuse_items=["compare"], crash_at="post-run-tooling", expect_exit=137)
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
        self.assertEqual(h.dispatch(tree_b, "compare1", ["compare"], baseline=1)["returncode"], 0)
        h.run(tree_b, "gate", "--tag", "b")
        b1_actions = ["assert", "accept"] if CANDIDATE_IDENTITY_AVAILABLE else ["assert"]
        b1 = h.dispatch(tree_b, "b1", b1_actions, baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in h.index(1)["rules"]))
        if not CANDIDATE_IDENTITY_AVAILABLE:
            self.skipTest(ACCEPT_SKIP_REASON)
        # R2 主链继续：accept 真实 CLI 动作通过 → VC-5 completion 绑定 b1。
        self.assertEqual({action["action_id"]: action["status"] for action in b1["campaign_run"]["actions"]}, {"vc5-1-assert": "passed", "vc5-2-accept": "passed"})
        h.assert_accepted_to_completion(1)

    def test_r2b_owner_loss_before_binding_yields_none_authority(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, "compare0", ["compare"])["returncode"], 0)
        h.dispatch(tree_a, "b0", ["assert"], reuse_items=["compare"], crash_at="before-binding", expect_exit=137)
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
        self.assertEqual(h.dispatch(tree_b, "compare1", ["compare"], baseline=1)["returncode"], 0)
        h.run(tree_b, "gate", "--tag", "b")
        b1_actions = ["assert", "accept"] if CANDIDATE_IDENTITY_AVAILABLE else ["assert"]
        b1 = h.dispatch(tree_b, "b1", b1_actions, baseline=1, authority="none", reuse_items=["compare"])
        self.assertEqual((b1["returncode"], b1["campaign_run"]["reason"]), (0, "queue-complete"), b1)
        self.assertTrue(all(row["reused_from"] is None for row in h.index(1)["rules"]))
        if not CANDIDATE_IDENTITY_AVAILABLE:
            self.skipTest(ACCEPT_SKIP_REASON)
        self.assertEqual({action["action_id"]: action["status"] for action in b1["campaign_run"]["actions"]}, {"vc5-1-assert": "passed", "vc5-2-accept": "passed"})
        h.assert_accepted_to_completion(1)

    # ------------------------------------------------------------------
    # E1～E5：apply 在五个写点逐点崩溃，续作收敛（一条链上依次推进）
    # ------------------------------------------------------------------

    def test_apply_crash_points_e1_to_e5_resume(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, "compare0", ["compare"])["returncode"], 0)
        b0 = h.dispatch(tree_a, "b0", ["assert"], reuse_items=["compare"])
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
    # C1：accept 结果已封存、VC-5 completion 写出前崩溃 → 续跑只补 completion，AcceptanceFact 字节不变
    # ------------------------------------------------------------------

    def test_c1_accept_sealed_completion_crash_resumes_without_rewriting_acceptance(self) -> None:
        if not CANDIDATE_IDENTITY_AVAILABLE:
            self.skipTest(ACCEPT_SKIP_REASON)
        h = self.harness
        tree_b = h.tree_b()
        h.init(tree_b)
        self.assertEqual(h.dispatch(tree_b, "compare0", ["compare"])["returncode"], 0)
        h.run(tree_b, "gate", "--tag", "b")
        b0 = h.dispatch(tree_b, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual((b0["returncode"], b0["campaign_run"]["reason"]), (0, "queue-complete"), b0)
        # ---- 正式监督器批次内的 accept 动作（真实 CLI，同一 argv）在 VC-5 completion 写出前 SIGKILL 语义退出 ----
        crashed = h.dispatch(tree_b, "accept", ["accept"], reuse_items=["compare", "assert-rules"], accept_wrapper=True, crash_at="accept-completion")
        self.assertEqual((crashed["returncode"], crashed["campaign_run"]["status"], crashed["campaign_run"]["reason"]), (1, "failed", "action-failed:vc5-1-accept"), crashed)
        self.assertEqual(crashed["campaign_run"]["actions"][0]["effective_failure_class"], "post-run-tooling")
        run_dir = Path(crashed["campaign_run"]["run_dir"])
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        self.assertEqual(supervisor.read_stop_receipt(run_dir)["reason"], "action-failed:vc5-1-accept")
        acceptance_path = h.acceptance_path(0)
        seal_path = acceptance_path.with_name("evidence-seal.json")
        self.assertTrue(acceptance_path.is_file() and seal_path.is_file())
        self.assertFalse(h.vc5_checkpoint_path().exists())
        self.assertFalse(h.vc5_completion_path().exists())
        sealed_bytes, seal_bytes = acceptance_path.read_bytes(), seal_path.read_bytes()
        self.assertTrue(_read(acceptance_path)["accepted"])
        # ---- 对账：失败父 run → recoverable（第一次同根因）----
        reconciled = h.run(tree_b, "reconcile", "--run-dir", str(run_dir))
        self.assertEqual((reconciled["status"], reconciled["decision"]["decision"]), ("recoverable", "recoverable"), reconciled)
        self.assertEqual(h.summary()["status"], "active")
        # ---- 受管恢复入口：环境恢复重派（与原批次逐字相同的 actions／reuse_items，开关文件已删）----
        resumed = h.dispatch(tree_b, "accept", ["accept"], reuse_items=["compare", "assert-rules"], accept_wrapper=True)
        self.assertEqual((resumed["returncode"], resumed["campaign_run"]["reason"]), (0, "queue-complete"), resumed)
        self.assertEqual(resumed["campaign_run"]["actions"][0]["status"], "passed")
        # 既有 AcceptanceFact 与证据封印字节不变，只补 completion／VC-5 checkpoint。
        self.assertEqual(acceptance_path.read_bytes(), sealed_bytes)
        self.assertEqual(seal_path.read_bytes(), seal_bytes)
        h.assert_accepted_to_completion(0)
        # VC-5 已终态：时间账本已登记本 revision 完成（原子入口先于 checkpoint 门拒绝再开批次）。
        rejected = h.dispatch(tree_b, "after-completion", ["accept"], reuse_items=["compare", "assert-rules"], accept_wrapper=True, expect_exit=1)
        self.assertRegex(rejected["stderr"], "已登记 VC-5 在当前 revision 完成，禁止重开|VC-5 已有 checkpoint")
        # ---- 漂移负例（副本树子进程内真实 accept_campaign 读侧）：整份相等才允许续跑 ----
        # ① campaign_manifest_sha256 被改（封存摘要同步重算，自洽）→ 拒绝。
        h.rewrite_sealed_acceptance(0, lambda document: document.__setitem__("campaign_manifest_sha256", "0" * 64))
        rejected = h.accept_direct(tree_b)
        self.assertEqual(rejected["status"], "error", rejected)
        self.assertIn("不完全一致", rejected["error"])
        self.assertIn("campaign_manifest_sha256", rejected["error"])
        acceptance_path.write_bytes(sealed_bytes)
        # ② 多出一个字段（封存摘要同步重算）→ 拒绝。
        h.rewrite_sealed_acceptance(0, lambda document: document.__setitem__("extra_field", True))
        rejected = h.accept_direct(tree_b)
        self.assertEqual(rejected["status"], "error", rejected)
        self.assertIn("多余=['extra_field']", rejected["error"])
        acceptance_path.write_bytes(sealed_bytes)
        # ③ 内容改了但封存摘要没重算 → 封存不自洽，拒绝。
        h.rewrite_sealed_acceptance(0, lambda document: document.__setitem__("status", "blocked"), reseal=False)
        rejected = h.accept_direct(tree_b)
        self.assertEqual(rejected["status"], "error", rejected)
        self.assertIn("package digest 与内容不符", rejected["error"])
        acceptance_path.write_bytes(sealed_bytes)
        # 复原后再次 accept：整份一致 → 幂等（completion 既有幂等），文件字节仍不变。
        again = h.accept_direct(tree_b)
        self.assertEqual((again["status"], again["accepted"]), ("accepted", True), again)
        self.assertEqual(acceptance_path.read_bytes(), sealed_bytes)
        self.assertEqual(_read(h.vc5_checkpoint_path())["checkpoint_sha256"], again["vc5_checkpoint_sha256"])

    # ------------------------------------------------------------------
    # C1（b1 同批次）：b0 evaluator 失败 → 对账／epoch／apply 进入 b1 → b1 正式批次 [assert, accept] 在 accept
    # completion 前崩溃 → 对账 recoverable（VC-5:accept 与前次 VC-5:assert 是不同根因）→ 逐字重派同一批次：
    # assert 零 checker、checkpoint／索引不重写，AcceptanceFact 字节不变，只补 VC-5 completion（绑定 b1）
    # ------------------------------------------------------------------

    def test_c1_on_b1_batch_accept_crash_redispatches_same_batch_with_zero_checker(self) -> None:
        if not CANDIDATE_IDENTITY_AVAILABLE:
            self.skipTest(ACCEPT_SKIP_REASON)
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        digests_b = mtc.evaluator_digests(tree_b)
        # ---- b0：缺陷副本 A 断言失败 → 对账 → epoch → apply → b1 ----
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, "compare0", ["compare"])["returncode"], 0)
        b0 = h.dispatch(tree_a, "b0", ["assert"], reuse_items=["compare"])
        self.assertEqual((b0["returncode"], b0["campaign_run"]["reason"]), (1, "action-failed:vc5-1-assert"), b0)
        reconciled_b0 = h.run(tree_a, "reconcile", "--run-dir", b0["campaign_run"]["run_dir"])
        self.assertEqual(reconciled_b0["status"], "recoverable")
        self.assertEqual(reconciled_b0["root_cause"]["failed_step"], "VC-5-assert")
        h.run(tree_b, "epoch", "--attempt-id", h.state()["attempt_id"], "--reason", "evaluator checker fix")
        applied = h.apply_fix(tree_b, h.deployment_receipt(tree_b, "fix-b"))
        self.assertEqual((applied["status"], applied["evaluation_baseline"]), ("applied", 1), applied)
        self.assertEqual(h.dispatch(tree_b, "compare1", ["compare"], baseline=1)["returncode"], 0)
        h.run(tree_b, "gate", "--tag", "b")
        # ---- b1 正式批次 [assert, accept]：assert 全部重跑全 pass，accept 动作在 completion 写出前崩溃 ----
        batch_arguments = dict(baseline=1, reuse_from=h.b0_index_path(), authority="anchored", reuse_items=["compare"], accept_wrapper=True)
        crashed = h.dispatch(tree_b, "b1", ["assert", "accept"], crash_at="accept-completion", **batch_arguments)
        self.assertEqual((crashed["returncode"], crashed["campaign_run"]["status"], crashed["campaign_run"]["reason"]), (1, "failed", "action-failed:vc5-2-accept"), crashed)
        actions = {action["action_id"]: action for action in crashed["campaign_run"]["actions"]}
        self.assertEqual((actions["vc5-1-assert"]["status"], actions["vc5-2-accept"]["status"], actions["vc5-2-accept"]["effective_failure_class"]), ("passed", "failed", "post-run-tooling"))
        index_path = h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1" / "evaluation-run.json"
        index1 = h.index(1)
        self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in index1["rules"]))
        self.assertEqual(index1["evaluator"], digests_b)
        acceptance_path = h.acceptance_path(1)
        seal_path = acceptance_path.with_name("evidence-seal.json")
        self.assertTrue(acceptance_path.is_file() and seal_path.is_file())
        self.assertFalse(h.vc5_checkpoint_path().exists())
        self.assertFalse(h.vc5_completion_path().exists())
        # 快照：b1 索引／checkpoint 链／checker 机器文档／AcceptanceFact 的字节与 mtime。
        b1_root = h.campaign_dir() / "assertions" / CANDIDATE / "revisions" / "b1"
        index_bytes = index_path.read_bytes()
        checkpoint_stamps = {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in (b1_root / "checkpoints").iterdir()}
        machine_stamps = {path.relative_to(b1_root).as_posix(): path.stat().st_mtime_ns for path in (b1_root / "machine").rglob("*.json")}
        self.assertTrue(machine_stamps)  # b1 确实跑过 checker（全部重跑）
        sealed_bytes, seal_bytes = acceptance_path.read_bytes(), seal_path.read_bytes()
        # ---- 对账：本 revision 第二次失败对账，但失败动作是 VC-5:accept，与 b0 的 VC-5:assert 是不同根因
        # （无枚举观测时 failed_step 取失败动作的 operation，不再取父 run 最后事件）→ recoverable ----
        reconciled = h.run(tree_b, "reconcile", "--run-dir", crashed["campaign_run"]["run_dir"])
        self.assertEqual((reconciled["status"], reconciled["decision"]["decision"]), ("recoverable", "recoverable"), reconciled)
        self.assertEqual(reconciled["root_cause"]["failed_step"], "VC-5-accept")
        self.assertNotEqual(reconciled["root_cause"]["root_cause_id"], reconciled_b0["root_cause"]["root_cause_id"])
        self.assertEqual(reconciled["decision"]["root_cause_counts"].get(reconciled["root_cause"]["root_cause_id"]), 1, reconciled["decision"])
        self.assertEqual(h.summary()["status"], "active")
        # ---- 逐字重派同一 [assert, accept] 批次（同 tag → 同 builder config／同命令；开关文件已删）----
        resumed = h.dispatch(tree_b, "b1", ["assert", "accept"], **batch_arguments)
        self.assertEqual((resumed["returncode"], resumed["campaign_run"]["reason"]), (0, "queue-complete"), resumed)
        self.assertEqual({action["action_id"]: action["status"] for action in resumed["campaign_run"]["actions"]}, {"vc5-1-assert": "passed", "vc5-2-accept": "passed"})
        # assert 同基线重入：索引与 checkpoint 链字节／mtime 不变，机器文档 mtime 不变（checker 调用为零）。
        self.assertEqual(index_path.read_bytes(), index_bytes)
        self.assertEqual({path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in (b1_root / "checkpoints").iterdir()}, checkpoint_stamps)
        self.assertEqual({path.relative_to(b1_root).as_posix(): path.stat().st_mtime_ns for path in (b1_root / "machine").rglob("*.json")}, machine_stamps)
        # accept 整份重放一致：AcceptanceFact 与证据封印字节不变，只补 completion（绑定 b1）。
        self.assertEqual(acceptance_path.read_bytes(), sealed_bytes)
        self.assertEqual(seal_path.read_bytes(), seal_bytes)
        h.assert_accepted_to_completion(1)

    # ------------------------------------------------------------------
    # 后继协议伪造负例：篡改 b1 COMMIT 后派发被拒
    # ------------------------------------------------------------------

    def test_successor_protocol_rejects_tampered_commit(self) -> None:
        h = self.harness
        tree_a, tree_b = h.tree_a(), h.tree_b()
        h.init(tree_a)
        self.assertEqual(h.dispatch(tree_a, "compare0", ["compare"])["returncode"], 0)
        b0 = h.dispatch(tree_a, "b0", ["assert"], reuse_items=["compare"])
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
        rejected = h.dispatch(tree_b, "forged", ["compare"], baseline=1, expect_exit=1)
        self.assertIn("COMMIT", rejected["stderr"])
        commit_path.write_bytes(original)
        compare1 = h.dispatch(tree_b, "compare1", ["compare"], baseline=1)
        self.assertEqual(compare1["returncode"], 0, compare1)


if __name__ == "__main__":
    unittest.main()
