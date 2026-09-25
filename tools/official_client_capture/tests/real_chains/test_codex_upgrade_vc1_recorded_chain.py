"""R18 ②③：以 0.156.1 录制官方证据零请求回放 VC-1 取证链与恢复链。

隔离副本受管树＋私有挂载与网络命名空间（副本根→/root/oauth-capture，副本 runs→宿主 runs，宿主数据根只读，
无外网），Formal Campaign 沿用录制 campaign_id；Job 步骤由录制证据回放，其余全部真跑。链前后比对生产录制
目录的 stat 哨兵，确认只读。缺少录制数据、root 或 unshare 时如实 skip（发布认证视为未认证）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.official_client_capture.tests import managed_tree_copy as trees
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests import vc1_recorded_replay as replay

DRIVER_MODULE = "tools.official_client_capture.tests.vc1_recorded_chain_driver"
REPO_ROOT = Path(__file__).resolve().parents[4]


class _RecordedChainHarness:
    """一条录制回放链的隔离环境：副本树、录制快照、回放状态与命名空间内的驱动阶段。"""

    def __init__(self, case: unittest.TestCase, staging: Path) -> None:
        self.case = case
        self.staging = staging
        self.tree = trees.copy_managed_tree(staging / "tree")
        shutil.copy2(REPO_ROOT / "tools" / "prepare_assertion_bundle.sh", self.tree / "tools" / "prepare_assertion_bundle.sh")
        (self.tree / "runs").mkdir(mode=0o700)
        replay.install_sitecustomize(self.tree)
        self.index = replay.build_snapshot(staging)
        self.sentinel = replay.production_sentinel(self.index)
        self.state = replay.write_state(staging / "replay-state.json", self.index, counter_dir=staging / "replay-counters")
        self.last_stdout = ""
        self.last_stderr = ""

    def run(self, stage: str, *extra: str, timeout: float = 1800.0) -> dict:
        argv = [sys.executable, "-m", DRIVER_MODULE, "--tree", str(self.tree), *extra, stage]
        completed = subprocess.run(
            replay.namespace_argv(self.tree, argv), capture_output=True, text=True, cwd=str(self.tree), timeout=timeout,
            env=trees.subprocess_env(self.tree, {replay.REPLAY_STATE_ENV: str(self.state),
                                                 project_ledger_fixture.FIXTURE_ONLY_ENV: "1",
                                                 "TMPDIR": str(self.staging)}),
        )
        self.last_stdout, self.last_stderr = completed.stdout, completed.stderr
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        if not lines:
            self.case.fail(f"{stage} 阶段没有结果：rc={completed.returncode}\n{completed.stderr[-6000:]}")
        return json.loads(lines[-1])

    def assert_production_untouched(self) -> None:
        self.case.assertEqual(replay.production_sentinel(self.index), self.sentinel, "生产录制目录在回放链中被改动")


class VC1RecordedCaptureChainTests(unittest.TestCase):
    def test_vc1_capture_from_recorded_evidence(self):
        if not replay.available():
            self.skipTest("录制回放链必须在具备 0.156.1 录制数据、root 与 unshare 的 Linux ARM64 上运行")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="codex-vc1-replay-", dir=replay.scratch_root()) as directory:
            staging = Path(directory).resolve() / "staging"
            staging.mkdir(mode=0o700)
            harness = _RecordedChainHarness(self, staging)
            initialized = harness.run("init")
            self.assertEqual(initialized["status"], "passed", f"{initialized}\n{harness.last_stderr[-6000:]}")
            attempts = initialized["attempts"]
            self.assertEqual(len(attempts), 1, attempts)
            self.assertEqual((attempts[0]["status"], attempts[0]["complete"], attempts[0]["executed"]),
                             ("awaiting_receipts", 31, 31), attempts)
            sealed = harness.run("seal")
            self.assertEqual(sealed["status"], "passed", f"{json.dumps(sealed, ensure_ascii=False)[:6000]}\n{harness.last_stderr[-4000:]}")
            accounted = harness.run("account")
            self.assertEqual(accounted["status"], "passed", accounted)
            classified = harness.run("classify")
            self.assertEqual(classified["status"], "passed", f"{json.dumps(classified, ensure_ascii=False)[:6000]}")
            harness.assert_production_untouched()
            # 真实 0.156.1 官方断言包过 seal 门禁（R21 验收）：25 条规则、57 个 check、917 条观测，延后 10 项＝弃用标签 3
            # （基线有、目标已删的标签取值）＋目标版本删除的 legacy compact 端点 7；其余零命中仍会当场失败。
            gate = sealed["assertion_gate"]
            self.assertEqual((sealed["stage_status"], sealed["vc1_checkpoint"]), ("complete", True), sealed)
            self.assertEqual((gate["checked_rule_count"], gate["checked_check_count"], gate["observation_count"]), (25, 57, 917), gate)
            deferred = gate["deferred_unreachable_checks"]
            self.assertEqual((len(deferred), sum("retired_labels" in item for item in deferred),
                              sum("absent_paths" in item for item in deferred)), (10, 3, 7), deferred)
            batches = classified["batches"]
            self.assertEqual([(row["phase"], row["sequence"], row["execute"], row["reuse"]) for row in batches],
                             [("VC-1", 1, 31, 0), ("VC-1", 2, 2, 0), ("VC-1", 3, 1, 0), ("VC-2", 4, 1, 0)], batches)
            duplicates = [sealed["preview"]["duplicate_dispatch_requests"], sealed["approve"]["duplicate_dispatch_requests"],
                          classified["batch"]["duplicate_dispatch_requests"]]
            self.assertEqual(duplicates, [0, 0, 0])
            seconds = round(time.monotonic() - started, 3)
            self.assertLess(seconds, 900, "ARM64 VC-1 取证链超过 15 分钟验收上限")
            self.real_chain_metrics = {
                "network_isolated": True, "live_request_count": 0, "duplicate_dispatch_requests": sum(duplicates),
                "duplicate_dispatch_checks": len(duplicates), "seconds": seconds, "batches": batches,
                "accounted_recorded_requests": accounted.get("output", "")[-600:],
            }
            print(json.dumps({"chain": "vc-chain.vc1-capture", **self.real_chain_metrics,
                              "attempts": attempts, "deferred": deferred, "classify": classified.get("draft_summary")},
                             ensure_ascii=False), file=sys.stderr)


# ③ 恢复预览批次的控制面缺陷注入：只在 resume --preview-recovery 路径经过的失效摘要函数入口抛错
# （本机按策略 v2 核算只改 control 层；in-process 建 Campaign 与 reconcile-attempt 的 R17 复算都不经过它）。
PREVIEW_DEFECT_ANCHOR = '''    """按控制／环境／数据三层输出恢复失效集合。

    该摘要只使用小型身份和冻结 recovery scope，不读取证据，也不创建
    reservation。调用方必须显式传入已经闭合的 Job 集，禁止在此重新推导。
    """
'''
PREVIEW_DEFECT_INJECTION = PREVIEW_DEFECT_ANCHOR + '    raise ConfigurationError("R18 恢复链注入：恢复预览批次失败")\n'
GUARDIAN = "official-relay-guardian-review"


class VC1RecordedRecoveryChainTests(unittest.TestCase):
    """按 0.156.1 真实恢复顺序连续复演 D1～D7：首批超时 → 对账批准预览 → 预览批次失败（注入）→ 修复、对账、
    N+1 逐字重派 → 补跑失败（guardian 三连败）→ 控制面修复部署后对账、新预览 → 补跑成功（承接链）→ 证据语义修复
    与 evaluation epoch → 封存 → 入账 → 分类，全程无外网。"""

    def test_vc1_recovery_chain_from_recorded_evidence(self):
        if not replay.available():
            self.skipTest("录制回放链必须在具备 0.156.1 录制数据、root 与 unshare 的 Linux ARM64 上运行")
        started = time.monotonic()
        steps: list[dict] = []

        def step(name: str, result: dict) -> dict:
            steps.append({"step": name, "status": result.get("status"), "seconds": result.get("seconds")})
            return result

        with tempfile.TemporaryDirectory(prefix="codex-vc1-recovery-", dir=replay.scratch_root()) as directory:
            staging = Path(directory).resolve() / "staging"
            staging.mkdir(mode=0o700)
            harness = _RecordedChainHarness(self, staging)
            trees.replace_once(harness.tree, "codex_upgrade.py", PREVIEW_DEFECT_ANCHOR, PREVIEW_DEFECT_INJECTION)
            replay.update_state(harness.state, stall_job=GUARDIAN, job_retry_delay_seconds=1,
                                guardian_plan=["fail", "fail", "fail", "success"])
            # D1：首批动作超时（guardian 在进入步骤前阻塞），子进程自行封口，closeout 按失败收口。
            initialized = step("closeout-first-batch-timeout", harness.run("init", "--first-batch-timeout", "150"))
            self.assertEqual(initialized["status"], "failed", initialized)
            first = initialized["attempts"]
            self.assertEqual([(row["status"], row["complete"]) for row in first], [("failed", 30)], first)
            replay.update_state(harness.state, stall_job=None)
            # 对账 A1 → 零请求预览（R17 复算一致）→ 批准 → 授权。
            reconciled = step("reconcile-attempt-1", harness.run("reconcile-attempt"))
            self.assertEqual(reconciled["status"], "passed", reconciled)
            self.assertEqual((reconciled["execute_job_ids"], len(reconciled["reuse_job_ids"])), ([GUARDIAN], 30), reconciled)
            self.assertEqual((reconciled["resume_reuse_check"] or {}).get("status"), "consistent", reconciled)
            # D4：预览批次因控制面缺陷失败 → 修复（撤回注入）→ 父 run 对账（VC-1 只能走 attempt 恢复）→ 再对账授权 →
            # N+1 逐字重派预览。
            failed_preview = step("preview-injected-failure", harness.run("preview"))
            self.assertEqual(failed_preview["status"], "failed", failed_preview)
            self.assertTrue(any("R18 恢复链注入" in str(item) for item in failed_preview["diagnostics"]), failed_preview)
            trees.replace_once(harness.tree, "codex_upgrade.py", PREVIEW_DEFECT_INJECTION, PREVIEW_DEFECT_ANCHOR)
            run_reconciled = step("reconcile-run-preview", harness.run("reconcile-run", "--run-dir", failed_preview["run_dir"]))
            self.assertEqual((run_reconciled["reconcile_status"], run_reconciled["decision"]),
                             ("stage_review_required", "review_required"), run_reconciled)
            reauthorized = step("reconcile-attempt-2", harness.run("reconcile-attempt"))
            self.assertEqual(reauthorized["status"], "passed", reauthorized)
            redispatched = step("preview-redispatch", harness.run("preview"))
            self.assertEqual((redispatched["status"], redispatched["sequence"]), ("passed", failed_preview["sequence"] + 1), redispatched)
            # D6：按预览真实补跑，guardian 三次尝试均失败 → 新 attempt failed（30 承接 + 1 失败）。
            rerun_failed = step("rerun-guardian-fails", harness.run("rerun", "--preview", reauthorized["preview_path"]))
            self.assertEqual(rerun_failed["status"], "failed", rerun_failed)
            second = rerun_failed["attempts"][-1]
            self.assertEqual((second["status"], second["complete"], second["failed"]), ("failed", 30, [GUARDIAN]), rerun_failed["attempts"])
            # D3／D7：控制面修复部署（编排器 control 层变化）后，二次承接的结果沿承接链回溯原始执行仍可复用。
            trees.append_comment(harness.tree, "codex_upgrade.py", "R18 恢复链：控制面修复部署")
            third_reconciled = step("reconcile-attempt-3", harness.run("reconcile-attempt", "--attempt", second["attempt_id"]))
            self.assertEqual(third_reconciled["status"], "passed", third_reconciled)
            self.assertEqual(len(third_reconciled["reuse_job_ids"]), 30, third_reconciled)
            self.assertEqual((third_reconciled["resume_reuse_check"] or {}).get("status"), "consistent", third_reconciled)
            fresh_preview = step("preview-after-rerun-failure", harness.run("preview"))
            self.assertEqual(fresh_preview["status"], "passed", fresh_preview)
            rerun = step("rerun-guardian-succeeds", harness.run("rerun", "--preview", third_reconciled["preview_path"]))
            self.assertEqual(rerun["status"], "passed", rerun)
            final = rerun["attempts"][-1]
            self.assertEqual((final["status"], final["complete"], final["executed"], final["reused"]),
                             ("awaiting_receipts", 31, 1, 30), rerun["attempts"])
            # D5：封存前证据语义修复部署（evidence 层变化）→ evaluation epoch → 封存 → 入账 → 分类（按 epoch 链判定）。
            trees.append_comment(harness.tree, "build_evidence_catalog.py", "R18 恢复链：证据语义修复部署")
            epoch = step("evaluation-epoch", harness.run("epoch"))
            self.assertEqual(epoch["status"], "passed", epoch)
            sealed = step("seal", harness.run("seal"))
            self.assertEqual(sealed["status"], "passed", f"{json.dumps(sealed, ensure_ascii=False)[:6000]}")
            accounted = step("account", harness.run("account"))
            self.assertEqual(accounted["status"], "passed", accounted)
            classified = step("classify", harness.run("classify"))
            self.assertEqual(classified["status"], "passed", f"{json.dumps(classified, ensure_ascii=False)[:6000]}")
            harness.assert_production_untouched()
            seconds = round(time.monotonic() - started, 3)
            self.real_chain_metrics = {"network_isolated": True, "live_request_count": 0, "seconds": seconds,
                                       "steps": steps, "batches": classified["batches"]}
            print(json.dumps({"chain": "vc-chain.vc1-recovery-chain", **self.real_chain_metrics}, ensure_ascii=False), file=sys.stderr)
            self.assertLess(seconds, 900, "ARM64 VC-1 恢复链超过 15 分钟验收上限")


if __name__ == "__main__":
    unittest.main()
