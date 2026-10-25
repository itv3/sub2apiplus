"""第 18 项：恢复链一条命令（``driver/recovery-plan.sh``，``driver/recovery_plan.py``）的离线测试。

覆盖：

* 驱动清单闭合：新脚本、Python 实现与参数模板被清单登记，bash 语法可解析；
* 参数文件经 ``parse_env.py`` 同一词法层解析（命令替换、未知键等一律拒绝），与驱动参数文件交叉核对；
* **只读计划预览**：各现场（刚失败、账本停线、演进未登记、预览已出未批准、已批准未授权、已授权未续跑、续跑在跑、
  后继已存在）的逐步状态；保留／重验／重采清单；同一现场两次预览逐字相同；预览不改动数据根与运行根下任何文件；
* **执行**（受管工具替身，CLI 级）：按序走完并派发续跑；缺批准只写意向并停下（批准与授权的意向一次写全）；批准人不符、
  已过期被拒；暂停（账务、环境）停下不越权、处置后同一条命令续上；预览过期自动回到对账；
* **重复执行与打断**：全部做完后再执行不再调用任何写命令、不重派；在每一步动作之后、记录之前强制结束进程（kill -9 等价），
  再执行一次得到与不打断相同的终态与相同的写调用序列；派发窗口内被打断时保守停下、绝不重派；
* 并发锁。

受管工具替身与现场文件形状见 ``recovery_plan_world.py``（形状取自 0.163 真实 Campaign 记录）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests import recovery_plan_world as rw
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests

SCRIPTS = driver_tests.SCRIPTS
DRIVER_ROOT = driver_tests.DRIVER_ROOT
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED, EXIT_OPERATOR = 0, 1, 2, 3, 4


class _WorldCase(unittest.TestCase):
    """每个用例一个临时假采集主机；用例结束时杀掉遗留的续跑替身进程。"""

    def make_world(self, **kwargs) -> rw.World:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        root.chmod(0o700)
        world = rw.World(root, **kwargs)
        self.addCleanup(self._kill_launched, world)
        return world

    @staticmethod
    def _kill_launched(world: rw.World) -> None:
        for started in (world.out_dir() / "continue").glob("*.started.json") if (world.out_dir() / "continue").is_dir() else []:
            try:
                pid = json.loads(started.read_text(encoding="utf-8"))["pid"]
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ValueError, KeyError):
                pass

    def step(self, plan: dict, name: str) -> dict:
        return next(item for item in plan["steps"] if item["id"] == name)

    def statuses(self, plan: dict) -> dict[str, str]:
        return {item["id"]: item["status"] for item in plan["steps"]}


class ManifestTests(unittest.TestCase):
    def test_new_driver_files_are_in_manifest_and_parse(self) -> None:
        manifest = driver_tests.driver.load_manifest(DRIVER_ROOT)
        self.assertEqual(manifest, driver_tests.driver.build_manifest(DRIVER_ROOT))
        paths = {row["path"]: row for row in manifest["files"]}
        for relative, mode in (("driver/recovery-plan.sh", "0700"), ("driver/recovery_plan.py", "0700"),
                               ("driver/recovery-plan.example.params", "0600")):
            self.assertIn(relative, paths)
            self.assertEqual(paths[relative]["mode"], mode)
        result = subprocess.run(["bash", "-n", str(rw.SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_example_params_are_rejected_until_placeholders_are_replaced(self) -> None:
        # 模板里的 REPLACE_* 占位符不是合法坐标：直接拿模板执行必须被参数校验拒绝，而不是跑到别的 Campaign。
        template = SCRIPTS / "recovery-plan.example.params"
        result = subprocess.run([sys.executable, "-B", str(rw.HELPER), str(template)], capture_output=True, text=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, EXIT_USAGE, result.stdout + result.stderr)


class ParamsTests(_WorldCase):
    def test_valid_params_load_and_derive_paths(self) -> None:
        world = self.make_world()
        params = world.helper().load_params(world.params_path)
        self.assertEqual(params["OUT"], f"{world.runroot}/recovery-plan/r1")
        self.assertEqual(params["C"], str(world.campaign_dir))

    def test_shared_safe_lexer_is_used(self) -> None:
        world = self.make_world()
        helper = world.helper()
        self.assertEqual(Path(helper.parse_env.__file__).resolve(), (SCRIPTS / "parse_env.py").resolve())

    def test_command_substitution_and_unknown_keys_are_rejected_without_executing(self) -> None:
        world = self.make_world()
        marker = world.root / "pwned"
        for line in (f'APPROVER="$(touch {marker})"', f"UNKNOWN_KEY=1", f'APPROVER="`touch {marker}`"', "ROUND=r1; touch x"):
            text = world.params_path.read_text(encoding="utf-8") + line + "\n"
            bad = rw.write(world.root / "bad.env", text)
            result = subprocess.run([sys.executable, "-B", str(rw.HELPER), str(bad)], capture_output=True, text=True,
                                    env=world.env(), stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, EXIT_USAGE, (line, result.stdout + result.stderr))
            self.assertFalse(marker.exists())

    def test_missing_required_key_and_env_mismatch_are_rejected(self) -> None:
        world = self.make_world()
        for override, expect in (({"CAMPAIGN": "other-campaign"}, "与驱动参数文件不一致"), ({"APPROVER": None}, "缺少键")):
            world.write_params(**override)
            result = world.run("--plan")
            self.assertEqual(result.returncode, EXIT_USAGE, result.stdout + result.stderr)
            self.assertIn(expect, result.stderr)

    def test_claude_client_is_not_supported_yet(self) -> None:
        from tools.official_client_capture.tests.test_driver_client_table import claude_env_text

        world = self.make_world()
        world.fixture.env_file.write_text(claude_env_text(world.fixture), encoding="utf-8")
        result = world.run("--plan")
        self.assertEqual(result.returncode, EXIT_USAGE, result.stdout + result.stderr)
        self.assertIn("只支持 Codex", result.stderr)

    def test_bad_fix_commit_is_rejected(self) -> None:
        world = self.make_world()
        world.write_params(FIX_COMMIT="abc")
        result = world.run("--plan")
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertIn("FIX_COMMIT", result.stderr)


class PlanPreviewTests(_WorldCase):
    def test_fresh_failure_lists_pending_chain_in_order(self) -> None:
        world = self.make_world(evolution_registered=False)
        plan = world.plan()
        self.assertEqual(plan["phase"], "candidate")
        self.assertEqual(plan["ledger_status"], "recovery_required")
        self.assertEqual(self.statuses(plan), {
            "resume": "done", "evolution": "pending", "reconcile-runs": "done", "reconcile": "waiting",
            "approve": "waiting", "authorize": "waiting", "continue": "waiting"})
        self.assertEqual(plan["next"]["step"], "evolution")
        self.assertEqual(plan["overall"], "pending")

    def test_warns_when_fix_is_deployed_before_the_failure_was_reconciled(self) -> None:
        world = self.make_world(evolution_registered=False)
        plan = world.plan()
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("部署收据早于停线", plan["warnings"][0])
        self.assertIn("警告：", world.run("--plan").stdout)
        # 已对账（收据存在）后不再提示。
        rw.write_json(world.campaign_dir / "control/reconciliation" / f"attempt-{world.attempt}" / "attempt-reconciliation.json",
                      {"attempt_id": world.attempt, "campaign_ledger": {"status": "recovery_required"}})
        self.assertEqual(world.plan()["warnings"], [])
        self.assertEqual(self.make_world().plan()["warnings"], [], "没有未登记漂移时没有提示")

    def test_stopped_ledger_needs_resume_and_missing_inputs_block(self) -> None:
        world = self.make_world(ledger_status="stop_required")
        plan = world.plan()
        self.assertEqual(self.step(plan, "resume")["status"], "pending")
        self.assertEqual(plan["next"]["step"], "resume")
        world.write_params(REGRESSION_RECEIPT=None, RESUME_REASON=None)
        plan = world.plan()
        self.assertEqual(self.step(plan, "resume")["status"], "blocked")
        self.assertIn("REGRESSION_RECEIPT", self.step(plan, "resume")["detail"])
        self.assertEqual(plan["overall"], "blocked")

    def test_ledger_read_failure_blocks_instead_of_guessing(self) -> None:
        world = self.make_world()
        world.set_state(ledger_error="计时账本不可读")
        plan = world.plan()
        self.assertEqual(self.step(plan, "resume")["status"], "blocked")
        self.assertIn("计时账本不可读", self.step(plan, "resume")["detail"])

    def test_preview_exists_but_not_approved(self) -> None:
        world = self.make_world()
        self.assertEqual(world.run("--apply").returncode, EXIT_OPERATOR)
        plan = world.plan()
        self.assertEqual(self.step(plan, "reconcile")["status"], "done")
        self.assertEqual(self.step(plan, "approve")["status"], "pending")
        self.assertEqual(self.step(plan, "authorize")["status"], "waiting")
        self.assertEqual(self.step(plan, "continue")["status"], "waiting")

    def test_objects_list_keep_reverify_recollect_from_preview(self) -> None:
        world = self.make_world(reuse_complete=True, jobs={"complete": ["a", "b"], "failed": ["c"], "indeterminate": ["d"], "pending": ["e"]})
        self.assertEqual(world.run("--apply").returncode, EXIT_OPERATOR)
        plan = world.plan()
        objects = plan["objects"]
        self.assertEqual(objects["source"], "preview")
        self.assertEqual(objects["keep"], ["a", "b"])
        self.assertEqual({row["job"]: row["reason"] for row in objects["recollect"]}, {"c": "已失败", "d": "结果不确定", "e": "尚未执行"})
        states = {row["object"]: row["state"] for row in objects["reverify"]}
        self.assertEqual(states["恢复预览 #1"], "unknown")  # 没有演进信息，由受管对账确认
        self.assertEqual(states["恢复批准"], "missing")
        self.assertEqual(states["工具演进登记"], "valid")

    def test_objects_without_preview_use_attempt_state(self) -> None:
        world = self.make_world()
        objects = world.plan()["objects"]
        self.assertEqual(objects["source"], "attempt")
        self.assertEqual(objects["keep"], ["job-done"])
        self.assertEqual([row["job"] for row in objects["recollect"]], ["job-failed"])
        self.assertEqual(objects["reverify"][0], {"object": "恢复预览", "state": "missing", "why": "尚无预览，执行 reconcile 步骤后产出"})

    def test_two_previews_are_identical_and_read_only(self) -> None:
        world = self.make_world(evolution_registered=False)
        first = world.run("--plan")
        before_data, before_run = rw.tree_digest(world.data, skip=(".stub/calls.jsonl",)), rw.tree_digest(world.runroot)
        second = world.run("--plan")
        third = world.run("--plan", "--json")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(third.stdout)["plan_sha256"], first.stdout.strip().rsplit("plan_sha256=", 1)[1])
        self.assertEqual(before_data, rw.tree_digest(world.data, skip=(".stub/calls.jsonl",)), "预览不得改动数据根")
        self.assertEqual(before_run, rw.tree_digest(world.runroot), "预览不得在运行根写任何文件")
        # 预览只调只读命令（tool-evolution-status）。
        self.assertEqual({call[0] for call in world.calls()}, {"tool-evolution-status"})

    def test_tail_failed_run_is_listed_as_pending_reconcile(self) -> None:
        world = self.make_world()
        world.add_tail_run("run-a", "stopped", 100.0)
        world.add_tail_run("run-b", "failed", 200.0)
        plan = world.plan()
        self.assertEqual(self.step(plan, "reconcile-runs")["status"], "pending")
        self.assertEqual(self.step(plan, "reconcile-runs")["evidence"]["pending"], ["run-b"])

    def test_active_parent_run_blocks(self) -> None:
        world = self.make_world()
        world.add_tail_run("run-a", "running", 100.0)
        plan = world.plan()
        self.assertEqual(self.step(plan, "reconcile-runs")["status"], "blocked")

    def test_successor_attempt_means_continuation_already_dispatched(self) -> None:
        world = self.make_world()
        world.add_attempt("20261021T192333Z-beaeb421943592c6")
        plan = world.plan()
        self.assertEqual(self.step(plan, "continue")["status"], "done")
        self.assertIn("后继 attempt", self.step(plan, "continue")["detail"])

    def test_unknown_attempt_is_blocked(self) -> None:
        world = self.make_world()
        world.write_params(ATTEMPT="20200101T000000Z-0000000000000000")
        plan = world.plan()
        self.assertEqual(self.step(plan, "reconcile")["status"], "blocked")
        self.assertIn("找不到目标 attempt", self.step(plan, "reconcile")["detail"])

    def test_complete_attempt_needs_no_recovery(self) -> None:
        world = self.make_world(attempt_status="complete")
        plan = world.plan()
        self.assertEqual(plan["overall"], "complete")
        self.assertEqual(self.step(plan, "reconcile")["status"], "skipped")

    def test_official_phase_folds_approve_and_authorize_into_entry(self) -> None:
        world = self.make_world(phase="official", ledger_status="stage_review_required")
        plan = world.plan()
        self.assertEqual(plan["phase"], "official")
        self.assertEqual(self.step(plan, "approve")["status"], "skipped")
        self.assertEqual(self.step(plan, "authorize")["status"], "skipped")

    def test_same_attempt_in_both_dirs_fails_closed(self) -> None:
        world = self.make_world()
        other = world.campaign_dir / "official" / "attempts" / world.attempt
        rw.write_json(other / "attempt.json", {"status": "failed"})
        result = world.run("--plan")
        self.assertEqual(result.returncode, EXIT_FAILED)
        self.assertIn("同时出现在官方与候选目录", result.stderr)


class ApplyTests(_WorldCase):
    def _full_chain_world(self, **kwargs) -> rw.World:
        """账本停线＋演进未登记：完整链 resume → evolution → 对账 → 批准 → 授权 → 续跑；全部批准预先由"人"提交。"""

        world = self.make_world(ledger_status="stop_required", evolution_registered=False, **kwargs)
        world.pre_approve("campaign-resume", rw.RESUME_SHA, extra_files=("REGRESSION_RECEIPT",))
        world.pre_approve("tool-evolution", rw.EVOLUTION_SHA)
        # resume 把账本序号 +1，演进登记把有效演进序号 +1，之后第一份预览的键是 (11, 8)。
        world.pre_sign_recovery(seq=11, effective=8)
        return world

    def test_one_command_walks_the_whole_chain_to_continuation(self) -> None:
        world = self._full_chain_world()
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(world.write_calls(), ["campaign-resume:apply", "tool-evolution:apply", "reconcile-attempt:approve",
                                               "reconcile-attempt:authorize"])
        self.assertEqual(len(world.dispatches()), 1)
        self.assertIn("vc5-recover.sh", world.dispatches()[0])
        self.assertIn("RECOVERY_PLAN_DONE", result.stderr)
        plan = world.wait_continue_finished()
        self.assertEqual(plan["overall"], "complete")
        self.assertEqual(self.statuses(plan), {name: "done" for name in
                                               ("resume", "evolution", "reconcile-runs", "reconcile", "approve", "authorize", "continue")})
        self.assertEqual(world.state()["ledger_status"], "active")

    def test_apply_records_plan_steps_and_raw_outputs(self) -> None:
        world = self._full_chain_world()
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        out = world.out_dir()
        self.assertTrue(list((out / "plans").glob("*.json")))
        for name in ("resume", "evolution", "reconcile", "approve", "authorize", "continue", "done"):
            record = json.loads((out / "steps" / f"{name}.json").read_text(encoding="utf-8"))
            self.assertEqual((record["status"], record["step"]), ("passed", name))
        self.assertTrue((out / "history.ndjson").is_file())
        raw = sorted(path.name for path in (out / "raw").iterdir())
        self.assertTrue(any(name.endswith("-resume-apply.rc") for name in raw))
        # 只有"续跑派发"产生真实采集：占位与启动记录各一份。
        self.assertEqual(len(list((out / "continue").glob("*.inflight.json"))), 1)
        self.assertEqual(len(list((out / "continue").glob("*.started.json"))), 1)
        for path in (out / "steps" / "resume.json", out / "history.ndjson"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_second_apply_after_completion_changes_nothing_and_never_redispatches(self) -> None:
        world = self._full_chain_world()
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        world.wait_continue_finished()
        writes, dispatches = world.write_calls(), world.dispatches()
        digest = rw.tree_digest(world.data, skip=(".stub/calls.jsonl",))
        for _ in range(2):
            again = world.run("--apply")
            self.assertEqual(again.returncode, EXIT_OK, again.stderr)
            self.assertEqual(world.write_calls(), writes)
            self.assertEqual(world.dispatches(), dispatches)
        self.assertEqual(digest, rw.tree_digest(world.data, skip=(".stub/calls.jsonl",)), "已完成的计划再执行不得改动现场")

    def test_missing_approvals_stop_after_writing_both_recovery_intents(self) -> None:
        world = self.make_world()
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("缺少 recovery-approve 专项人工批准", result.stderr)
        intents = sorted(json.loads(path.read_text(encoding="utf-8"))["binding"]["operation"]
                         for path in (world.out_dir() / "approval-intents").glob("*.json"))
        self.assertEqual(intents, ["recovery-approve", "recovery-authorize"], "批准与授权共用同一预览摘要，意向一次写全")
        self.assertEqual(world.write_calls(), [], "没有批准就不得写账本")
        self.assertEqual(world.dispatches(), [])
        # 人审阅意向并提交批准后，同一条命令续上并走到续跑。
        self.assertEqual(len(world.approve_from_intents()), 2)
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OK, again.stderr)
        self.assertEqual(world.write_calls(), ["reconcile-attempt:approve", "reconcile-attempt:authorize"])
        self.assertEqual(len(world.dispatches()), 1)

    def test_approval_by_someone_else_or_for_other_subject_is_rejected(self) -> None:
        world = self.make_world()
        sha = world.expected_preview_sha(1, 10, 7)
        world.pre_approve("recovery-approve", sha, approver="另一个人")
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("人工批准的身份、范围、预览摘要或凭证来源不一致", result.stderr)
        self.assertEqual(world.write_calls(), [])
        # 批准的是别的预览摘要：根本不会被当成本次的批准。
        world2 = self.make_world()
        world2.pre_approve("recovery-approve", "9" * 64)
        result = world2.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR)
        self.assertIn("缺少 recovery-approve 专项人工批准", result.stderr)

    def test_expired_approval_is_rejected(self) -> None:
        world = self.make_world()
        path = world.pre_approve("recovery-approve", world.expected_preview_sha(1, 10, 7))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["approved_at_utc"], data["expires_at_utc"] = "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z"
        rw.write_json(path, data)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR)
        self.assertIn("人工批准尚未生效或已过期", result.stderr)

    def test_accounting_pause_stops_without_overreach_then_same_command_continues(self) -> None:
        world = self.make_world(scenario={"pauses": {"20261021T135838Z-70aa3c8b45478aec": ["accounting"]}})
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("accounting-resolve", result.stderr)
        self.assertIn("重新执行同一条命令", result.stderr)
        self.assertNotIn("--from", result.stderr)
        self.assertEqual(world.write_calls(), [])
        self.assertEqual(world.dispatches(), [])
        world.set_state(accounting_resolved=True)  # 人执行了 accounting-resolve
        world.pre_sign_recovery(seq=10, effective=7)
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OK, again.stderr)
        self.assertEqual(len(world.dispatches()), 1)

    def test_environment_pause_names_isolate_command(self) -> None:
        world = self.make_world(scenario={"pauses": {"20261021T135838Z-70aa3c8b45478aec": ["environment", "request_budget"]}})
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR)
        self.assertIn("environment-isolate", result.stderr)
        self.assertIn("request-budget-extend", result.stderr)

    def test_stale_preview_at_authorize_rewinds_to_reconcile_once(self) -> None:
        world = self.make_world(scenario={"stale_authorize_once": True})
        world.pre_sign_recovery(seq=10, effective=7, index=1)
        world.pre_sign_recovery(seq=11, effective=7, index=2)  # 账本推进后第二份预览的键
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(world.write_calls(), ["reconcile-attempt:approve", "reconcile-attempt:authorize",
                                               "reconcile-attempt:approve", "reconcile-attempt:authorize"])
        self.assertEqual(len(world.dispatches()), 1)
        directory = world.campaign_dir / "control/reconciliation" / f"attempt-{world.attempt}"
        self.assertEqual(len(list(directory.glob("recovery-preview-*.json"))), 2)

    def test_tail_failed_run_is_reconciled_once(self) -> None:
        world = self.make_world()
        world.add_tail_run("run-a", "stopped", 100.0)
        world.add_tail_run("run-b", "failed", 200.0)
        world.pre_sign_recovery(seq=10, effective=7)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        self.assertEqual(world.write_calls().count("reconcile-supervisor-run"), 1)
        world.wait_continue_finished()
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        self.assertEqual(world.write_calls().count("reconcile-supervisor-run"), 1, "已对账的链尾 run 不重复对账")

    def test_tail_run_pause_is_revisited_after_resolution(self) -> None:
        world = self.make_world(scenario={"pauses": {"run-b": ["accounting"]}})
        world.add_tail_run("run-b", "failed", 200.0)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("accounting-resolve", result.stderr)
        # 收据虽已写下，判定是暂停：下次扫描必须重新对账，而不是当作已对账跳过。
        self.assertEqual(self.step(world.plan(), "reconcile-runs")["status"], "pending")
        world.set_state(accounting_resolved=True)
        world.pre_sign_recovery(seq=10, effective=7)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        self.assertEqual(world.write_calls().count("reconcile-supervisor-run"), 2)

    def test_wire_closure_change_is_not_registered_automatically(self) -> None:
        world = self.make_world(evolution_registered=False, scenario={"wire_changed": True})
        world.pre_approve("tool-evolution", rw.EVOLUTION_SHA)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("wire_closure_changed", result.stderr)
        self.assertEqual(world.write_calls(), [])

    def test_active_parent_run_blocks_everything(self) -> None:
        world = self.make_world()
        world.add_tail_run("run-a", "running", 100.0)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR)
        self.assertIn("有运行中的父 run", result.stderr)
        self.assertEqual(world.write_calls(), [])
        self.assertEqual(world.dispatches(), [])

    def test_missing_resume_inputs_block_instead_of_running(self) -> None:
        world = self.make_world(ledger_status="stopped")
        world.write_params(RESUME_REASON=None)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR)
        self.assertIn("RESUME_REASON", result.stderr)
        self.assertEqual(world.write_calls(), [])

    def test_official_phase_dispatches_entry_with_approved_digest(self) -> None:
        world = self.make_world(phase="official", ledger_status="stage_review_required", reuse_complete=True)
        world.pre_sign_recovery(seq=10, effective=7)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(world.write_calls(), [], "官方首批的批准与授权由 entry.sh 的收口模块一次完成，计划不另调")
        self.assertEqual(len(world.dispatches()), 1)
        sha = world.expected_preview_sha(1, 10, 7)
        self.assertEqual(world.dispatches()[0], f"entry.sh --approve-sha256 {sha} --approved-by {rw.APPROVER}")
        self.assertEqual(world.wait_continue_finished()["overall"], "complete")

    def test_official_phase_without_approval_stops_with_one_intent(self) -> None:
        world = self.make_world(phase="official", ledger_status="stage_review_required")
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("recovery-continue", result.stderr)
        self.assertEqual(world.dispatches(), [])

    def test_concurrent_apply_is_refused(self) -> None:
        import fcntl

        world = self.make_world()
        out = world.out_dir()
        out.mkdir(parents=True, mode=0o700)
        handle = os.open(out / ".round.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, handle)
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_LOCKED, result.stderr)
        self.assertIn("已有恢复计划实例在运行", result.stderr)
        self.assertEqual(world.calls(), [])

    def test_wrapper_script_preview_and_non_root_apply_refusal(self) -> None:
        world = self.make_world()
        preview = subprocess.run(["bash", str(rw.SCRIPT), str(world.params_path)], capture_output=True, text=True, env=world.env(),
                                 stdin=subprocess.DEVNULL)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("恢复计划（只读预览）", preview.stdout)
        if os.geteuid() != 0:
            refused = subprocess.run(["bash", str(rw.SCRIPT), str(world.params_path), "--apply"], capture_output=True, text=True,
                                     env=world.env(), stdin=subprocess.DEVNULL)
            self.assertEqual(refused.returncode, EXIT_USAGE)
            self.assertIn("必须以 root 执行", refused.stderr)
        usage = subprocess.run(["bash", str(rw.SCRIPT)], capture_output=True, text=True, env=world.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(usage.returncode, EXIT_USAGE)


class ContinueGuardTests(_WorldCase):
    """续跑派发是唯一产生真实采集请求的动作：占位在先、启动记录在后、永不自动重派。"""

    def _ready(self, **kwargs) -> rw.World:
        world = self.make_world(**kwargs)
        world.pre_sign_recovery(seq=10, effective=7)
        return world

    def test_running_dispatch_is_reported_running_and_not_repeated(self) -> None:
        world = self._ready()
        (world.root / "dispatch.mode").write_text("sleep", encoding="utf-8")
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        plan = world.plan()
        self.assertIn("仍在运行", self.step(plan, "continue")["detail"])
        self.assertEqual(plan["overall"], "complete")
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OK, again.stderr)
        self.assertEqual(len(world.dispatches()), 1, "进程还在跑时再执行不得重派")

    def test_failed_dispatch_is_blocked_not_retried(self) -> None:
        world = self._ready()
        (world.root / "dispatch.mode").write_text("fail", encoding="utf-8")
        # 替身派发后立刻以 7 结束：主命令重新判定时可能已看到结束标记（退出 4），也可能还没（退出 0），两种都对。
        self.assertIn(world.run("--apply").returncode, (EXIT_OK, EXIT_OPERATOR))
        import time

        plan = world.plan()
        for _ in range(60):
            plan = world.plan()
            if self.step(plan, "continue")["status"] == "blocked":
                break
            time.sleep(0.2)
        self.assertEqual(self.step(plan, "continue")["status"], "blocked")
        self.assertIn("以 7 结束", self.step(plan, "continue")["detail"])
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR)
        self.assertEqual(len(world.dispatches()), 1, "失败的续跑不自动重派")

    def test_dispatch_refused_while_driver_process_is_alive(self) -> None:
        world = self._ready()
        sleeper = subprocess.Popen(["bash", "-c", "exec -a vc5-run-batch sleep 30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        (world.runroot / "vc5-run-batch.pid").write_text(f"{sleeper.pid}\n", encoding="utf-8")
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("驱动进程", result.stderr)
        self.assertEqual(world.dispatches(), [])

    def test_dispatch_counts_even_after_a_newer_preview_appears(self) -> None:
        world = self._ready()
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        world.wait_continue_finished()
        # 派发之后又有人对账出了新预览（账本／演进序号变了）：这个 attempt 已经派发过，不能因为摘要变了再派一次。
        world.set_state(effective_index=8, ledger_seq=12)
        reconcile = subprocess.run([sys.executable, "-B", "-m", "tools.official_client_capture.codex_upgrade", "reconcile-attempt",
                                    "--campaign-dir", str(world.campaign_dir), "--attempt-id", world.attempt],
                                   cwd=world.data, env={**world.env(), "PYTHONPATH": str(world.data)}, capture_output=True, text=True)
        self.assertEqual(reconcile.returncode, 0, reconcile.stderr)
        directory = world.campaign_dir / "control/reconciliation" / f"attempt-{world.attempt}"
        self.assertEqual(len(list(directory.glob("recovery-preview-*.json"))), 2)
        plan = world.plan()
        self.assertEqual(self.step(plan, "continue")["status"], "done")
        self.assertEqual(len(world.dispatches()), 1)

    def test_process_ending_between_log_read_and_liveness_check_is_not_lost(self) -> None:
        """先读日志后查进程：进程恰在两次之间结束（结束标记已落盘）不能被误判成"进程消失"。"""

        from unittest import mock

        world = self.make_world()
        helper = world.helper()
        params = helper.load_params(world.params_path)
        launch = Path(params["OUT"]) / "continue"
        launch.mkdir(parents=True, mode=0o700)
        log = world.root / "launch.log"
        log.write_text("续跑输出\n", encoding="utf-8")
        rw.write_json(launch / ("a" * 64 + ".inflight.json"), {"attempt": params["ATTEMPT"], "status": "dispatch_pending"})
        rw.write_json(launch / ("a" * 64 + ".started.json"), {"pid": 4242424, "log": str(log)})

        def ends_meanwhile(_pid):
            with log.open("a", encoding="utf-8") as handle:
                handle.write("RECOVERY_PLAN_EXIT=0\n")
            return False

        with mock.patch.object(helper, "_process_alive", side_effect=ends_meanwhile):
            state = helper.continuation_state(params, [])
        self.assertEqual((state["state"], state["exit_code"]), ("finished-ok", "0"))
        # 日志没有标记、进程也不在：才是真的消失。
        log.write_text("续跑输出\n", encoding="utf-8")
        with mock.patch.object(helper, "_process_alive", return_value=False):
            self.assertEqual(helper.continuation_state(params, [])["state"], "lost")

    def test_old_driver_logs_are_kept_not_overwritten(self) -> None:
        world = self._ready()
        (world.runroot / "vc5-recover.out").write_text("旧日志\n", encoding="utf-8")
        (world.runroot / "vc5-run-batch.out").write_text("旧批次日志\n", encoding="utf-8")
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        kept = sorted(path.name for path in world.runroot.glob("vc5-*.pre-r1-*"))
        self.assertEqual(len(kept), 2, kept)


class InterruptionTests(_WorldCase):
    """中途被打断（kill -9 等价）后再执行同一条命令，终态与不打断相同；绝不因打断而重复写账本或重复采集。"""

    def _chain(self) -> rw.World:
        world = self.make_world(ledger_status="stop_required", evolution_registered=False)
        world.pre_approve("campaign-resume", rw.RESUME_SHA, extra_files=("REGRESSION_RECEIPT",))
        world.pre_approve("tool-evolution", rw.EVOLUTION_SHA)
        world.pre_sign_recovery(seq=11, effective=8)
        return world

    def _reference(self) -> tuple[list[str], dict, str]:
        world = self._chain()
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        plan = world.wait_continue_finished()
        return world.write_calls(), world.state(), plan["plan_sha256"]

    def test_interrupt_after_every_step_then_rerun_gives_same_result(self) -> None:
        reference_writes, reference_state, reference_plan = self._reference()
        for crash in ("resume", "evolution", "reconcile", "approve", "authorize"):
            with self.subTest(crash_after=crash):
                world = self._chain()
                killed = world.run("--apply", RECOVERY_PLAN_CRASH_AFTER=crash)
                self.assertEqual(killed.returncode, 137, (crash, killed.stderr))
                self.assertNotIn("RECOVERY_PLAN_DONE", killed.stderr)
                # 被打断时现场处在"动作做完、记录没写"的状态：预览能看出已推进。
                self.assertNotEqual(world.plan()["overall"], "complete")
                again = world.run("--apply")
                self.assertEqual(again.returncode, EXIT_OK, (crash, again.stderr))
                plan = world.wait_continue_finished()
                self.assertEqual(world.write_calls(), reference_writes, "每个写动作只做一次，打断不造成重复或遗漏")
                self.assertEqual(world.state(), reference_state)
                self.assertEqual(plan["plan_sha256"], reference_plan, "终态计划摘要与不打断时逐字相同")
                self.assertEqual(len(world.dispatches()), 1)

    def test_interrupt_three_times_in_a_row_still_converges(self) -> None:
        reference_writes, reference_state, reference_plan = self._reference()
        world = self._chain()
        for crash in ("resume", "reconcile", "approve"):
            self.assertEqual(world.run("--apply", RECOVERY_PLAN_CRASH_AFTER=crash).returncode, 137)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        plan = world.wait_continue_finished()
        self.assertEqual(world.write_calls(), reference_writes)
        self.assertEqual(world.state(), reference_state)
        self.assertEqual(plan["plan_sha256"], reference_plan)

    def test_interrupt_inside_dispatch_window_never_redispatches(self) -> None:
        world = self._chain()
        # 占位已写、进程还没起：执行结果不确定，保守停下，不自动重派。
        self.assertEqual(world.run("--apply", RECOVERY_PLAN_CRASH_AFTER="continue-inflight").returncode, 137)
        self.assertEqual(world.dispatches(), [])
        plan = world.plan()
        self.assertEqual(self.step(plan, "continue")["status"], "blocked")
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR, again.stderr)
        self.assertIn("禁止自动重派", again.stderr)
        self.assertEqual(world.dispatches(), [])

    def test_interrupt_after_spawn_before_started_record_does_not_double_dispatch(self) -> None:
        world = self._chain()
        self.assertEqual(world.run("--apply", RECOVERY_PLAN_CRASH_AFTER="continue-spawned").returncode, 137)
        import time

        for _ in range(50):  # 等被派生出的续跑替身自己跑完
            if world.dispatches():
                break
            time.sleep(0.1)
        self.assertEqual(len(world.dispatches()), 1)
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR, again.stderr)
        self.assertEqual(len(world.dispatches()), 1, "占位存在而启动记录缺失：不重派")

    def test_interrupt_leaves_no_partial_state_files(self) -> None:
        world = self._chain()
        self.assertEqual(world.run("--apply", RECOVERY_PLAN_CRASH_AFTER="approve").returncode, 137)
        for path in world.out_dir().rglob("*"):
            self.assertFalse(path.name.startswith(".") and ".tmp-" in path.name, path)


if __name__ == "__main__":
    unittest.main()
