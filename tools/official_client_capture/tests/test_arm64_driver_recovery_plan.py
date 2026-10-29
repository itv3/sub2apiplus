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
        self.assertEqual(states["恢复预览 #1"], "valid")  # 已部署工具身份与预览冻结的逐项一致
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


READINESS_PLACEHOLDER_ATTEMPT = "no-attempt-readiness-failed-batch12"


class ReadinessFailureWithoutAttemptTests(_WorldCase):
    """OPT-23：候选就绪静态检查失败、零请求退出、没有创建 attempt 目录。

    此前恢复计划在这里只会停在“reconcile：找不到目标 attempt”，人工改走“对账 → 同批次重派”；现在认出这个情形，
    把没有 attempt 的步骤标成不适用，先对账入账，再停在人工并打印同批次重派命令（本计划不代办，也不会派发）。
    现场形状取自上一次升级的真实失败：失败的静态收据（``model-mapping`` 一项）、占位 ATTEMPT 名、RUNROOT 里的候选批次后台脚本。
    """

    def readiness_world(self, *, status: str = "failed", script: bool = True, **kwargs) -> rw.World:
        world = self.make_world(**kwargs)
        world.write_params(ATTEMPT=READINESS_PLACEHOLDER_ATTEMPT)
        self.write_receipt(world, "static-20261027T212230110000Z-985698fb82a5.json", status)
        if script:
            rw.write(world.runroot / "vc5-run-batch.sh", "#!/bin/bash\necho ran\n", 0o700)
        return world

    @staticmethod
    def write_receipt(world: rw.World, name: str, status: str, **override) -> Path:
        failed = status == "failed"
        payload = {
            "schema_version": "codex-upgrade-candidate-readiness-static/v1", "campaign_id": world.campaign, "candidate_id": world.candidate,
            "status": status, "observed_at_utc": "2026-10-27T21:22:30.110Z", "live_request_count": 0,
            "failure_observations": ([{"check_id": "candidate-readiness.model-mapping", "failure_code": "model-mapping-not-clean"}] if failed else []),
        }
        payload.update(override)
        directory = world.campaign_dir / "control" / "candidate-readiness" / world.candidate / "static-receipts"
        return rw.write_json(directory / name, payload)

    def test_the_failed_readiness_scene_is_recognized_and_the_redispatch_command_is_printed(self) -> None:
        world = self.readiness_world()
        plan = world.plan()
        self.assertEqual(self.statuses(plan), {
            "resume": "done", "evolution": "done", "reconcile-runs": "done", "reconcile": "skipped",
            "approve": "skipped", "authorize": "skipped", "continue": "blocked"})
        self.assertEqual(plan["overall"], "blocked")
        self.assertEqual(plan["next"]["step"], "continue")
        self.assertIn("candidate-readiness.model-mapping=model-mapping-not-clean", self.step(plan, "reconcile")["detail"])
        command = self.step(plan, "continue")["command"]
        self.assertIn(f"cd {world.runroot}", command)
        self.assertIn(f"setsid -f bash {world.runroot}/vc5-run-batch.sh", command)
        self.assertIn("vc5-run-batch.out", command)
        self.assertIn(".failed-", command, "旧日志与 pid 要改名留档，不能直接覆盖")
        self.assertEqual(plan["readiness"]["failed_checks"], [{"check_id": "candidate-readiness.model-mapping", "failure_code": "model-mapping-not-clean"}])
        self.assertTrue(any("没有创建 attempt" in warning for warning in plan["warnings"]))
        text = world.run("--plan").stdout
        self.assertIn("命令：cd ", text, "阻塞步骤的重派命令要在预览里打印出来")
        self.assertNotIn("尚无预览，执行 reconcile 步骤后产出", text)

    def test_readiness_scene_preview_is_read_only_and_repeatable(self) -> None:
        world = self.readiness_world()
        first = world.run("--plan")
        before_data, before_run = rw.tree_digest(world.data, skip=(".stub/calls.jsonl",)), rw.tree_digest(world.runroot)
        second = world.run("--plan")
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(before_data, rw.tree_digest(world.data, skip=(".stub/calls.jsonl",)))
        self.assertEqual(before_run, rw.tree_digest(world.runroot))

    def test_apply_reconciles_the_failed_parent_run_first_then_stops_at_the_command_and_never_dispatches(self) -> None:
        world = self.readiness_world()
        world.add_tail_run("run-b", "failed", 200.0)
        before = self.statuses(world.plan())
        self.assertEqual(before["reconcile-runs"], "pending")
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertEqual(world.write_calls().count("reconcile-supervisor-run"), 1, "失败的父 run 先对账入账")
        self.assertEqual(world.dispatches(), [], "同批次重派由人执行，本计划不派发")
        self.assertIn("RECOVERY_PLAN_STOPPED step=continue status=needs-operator", result.stderr)
        self.assertIn("setsid -f bash", result.stderr, "停下时下一步直接给出重派命令")
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR)
        self.assertEqual(world.write_calls().count("reconcile-supervisor-run"), 1, "再执行不重复对账")

    def test_a_newer_passed_receipt_means_this_is_not_the_scene_and_the_original_block_stays(self) -> None:
        """最新一份静态收据已通过（批次已重派过，或 ATTEMPT 只是填错了）：不能悄悄当成已处理。"""
        world = self.readiness_world()
        self.write_receipt(world, "static-20261027T213356154000Z-3edef637dc4e.json", "passed")
        plan = world.plan()
        self.assertEqual(self.step(plan, "reconcile")["status"], "blocked")
        self.assertIn("找不到目标 attempt", self.step(plan, "reconcile")["detail"])
        self.assertNotIn("readiness", plan)

    def test_no_static_receipt_keeps_the_original_block(self) -> None:
        world = self.make_world()
        world.write_params(ATTEMPT=READINESS_PLACEHOLDER_ATTEMPT)
        plan = world.plan()
        self.assertEqual(self.step(plan, "reconcile")["status"], "blocked")
        self.assertNotIn("readiness", plan)

    def test_an_unreadable_or_foreign_latest_receipt_fails_closed(self) -> None:
        for label, damage in (("坏 JSON", lambda path: path.write_text("{坏", encoding="utf-8")),
                              ("别的候选", lambda path: rw.write_json(path, {**json.loads(path.read_text(encoding="utf-8")), "candidate_id": "别的候选"}))):
            with self.subTest(label):
                world = self.readiness_world()
                damage(next((world.campaign_dir / "control" / "candidate-readiness").rglob("static-*.json")))
                plan = world.plan()
                self.assertEqual(self.step(plan, "reconcile")["status"], "blocked", label)
                self.assertIn("找不到目标 attempt", self.step(plan, "reconcile")["detail"])
                self.assertNotIn("readiness", plan)

    def test_a_running_batch_process_blocks_without_a_redispatch_command(self) -> None:
        world = self.readiness_world()
        rw.write(world.runroot / "vc5-run-batch.pid", f"{os.getpid()}\n")
        step = self.step(world.plan(), "continue")
        self.assertEqual(step["status"], "blocked")
        self.assertIn("仍在运行", step["detail"])
        self.assertEqual(step["command"], "", "批次还在跑时不能给重派命令")

    def test_a_missing_batch_script_points_to_vc5_start(self) -> None:
        world = self.readiness_world(script=False)
        step = self.step(world.plan(), "continue")
        self.assertEqual(step["status"], "blocked")
        self.assertIn("vc5-start.sh", step["detail"])
        self.assertEqual(step["command"], "")

    def test_other_attempts_in_the_candidate_directory_add_a_wrong_attempt_warning(self) -> None:
        world = self.readiness_world()
        world.add_attempt("20261021T192333Z-beaeb421943592c6")  # 夹具本来就有一个默认 attempt，加上这个共 2 个
        warnings = "\n".join(world.plan()["warnings"])
        self.assertIn("另有 2 个 attempt", warnings)
        self.assertIn("ATTEMPT 填错", warnings)

    def test_the_printed_command_is_runnable_and_archives_the_old_files(self) -> None:
        """命令本身可执行：旧的批次日志、pid、vc5-all 日志改名留档，再用后台脚本重派（本机没有 setsid，用同名替身顶）。"""
        world = self.readiness_world()
        rw.write(world.runroot / "vc5-run-batch.sh", "#!/bin/bash\necho redispatched > \"$(dirname \"$0\")/redispatched.txt\"\n", 0o700)
        for name in ("vc5-run-batch.out", "vc5-run-batch.pid", "vc5-all.out"):
            rw.write(world.runroot / name, f"旧 {name}\n")
        fake_bin = world.root / "fake-bin"
        rw.write(fake_bin / "setsid", "#!/bin/sh\nshift\nexec \"$@\"\n", 0o700)
        command = self.step(world.plan(), "continue")["command"]
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((world.runroot / "redispatched.txt").read_text(encoding="utf-8").strip(), "redispatched")
        for name in ("vc5-run-batch.out", "vc5-run-batch.pid", "vc5-all.out"):
            archived = sorted(path for path in world.runroot.glob(name + ".failed-*"))
            self.assertEqual(len(archived), 1, name)
            self.assertEqual(archived[0].read_text(encoding="utf-8"), f"旧 {name}\n", "留档的是原内容，不是截断后的")


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
        # 停在 reconcile 时直接打印补账命令模板：参数名与已知坐标（数据根、Campaign 目录、编排器、未决 operation）已填好。
        self.assertIn("命令模板：补账预览（零写入）", result.stderr)
        self.assertIn(f"accounting-resolve --campaign-dir {world.data}/evidence/campaigns/{world.campaign}", result.stderr)
        self.assertIn(f"--operation-id reconcile-attempt:{world.attempt}", result.stderr)
        self.assertIn("--estimated-count <请求数批准上界> --evidence <来源审计文件绝对路径>", result.stderr)
        self.assertIn("--approve-sha256 <预览输出的 review_sha256> --approved-by '<批准人>'", result.stderr)
        self.assertNotIn("environment-isolate --campaign-dir", result.stderr, "只有账务暂停时不打印环境隔离命令")
        self.assertEqual(world.write_calls(), [])
        self.assertEqual(world.dispatches(), [])
        world.set_state(accounting_resolved=True)  # 人执行了 accounting-resolve
        world.pre_sign_recovery(seq=10, effective=7)
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OK, again.stderr)
        self.assertEqual(len(world.dispatches()), 1)

    def test_deadline_pause_hint_explains_the_batch_mutex_and_how_to_retry(self) -> None:
        """批次执行期间 deadline-extend 被设计内的互斥拒绝，实战里靠手写重试脚本才在间隙抢到；提示里要说清楚怎么重试。"""

        world = self.make_world(scenario={"pauses": {"20261021T135838Z-70aa3c8b45478aec": ["deadline"]}})
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        for fragment in ("deadline-extend", "设计内的互斥", "30～60 秒重试同一条命令", "批次间隙", "重新 preview 再 apply"):
            self.assertIn(fragment, result.stderr)
        self.assertEqual(world.write_calls(), [])

    def test_environment_pause_names_isolate_command(self) -> None:
        world = self.make_world(scenario={"pauses": {"20261021T135838Z-70aa3c8b45478aec": ["environment", "request_budget"]}})
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR)
        self.assertIn("environment-isolate", result.stderr)
        self.assertIn("request-budget-extend", result.stderr)
        # 环境暂停：干净环境收据（collect／finalize）与环境隔离两条命令模板；批准权限不变，仍由人给出批准摘要与批准人。
        self.assertIn("codex_upgrade_arm64_environment_receipt collect --evidence-root", result.stderr)
        self.assertIn("finalize --evidence-root <证据根> --facts <facts.json> --output <receipt.json>", result.stderr)
        self.assertIn(f"environment-isolate --campaign-dir {world.data}/evidence/campaigns/{world.campaign}", result.stderr)
        self.assertIn("--environment-repair-receipt <环境修复记录绝对路径> --clean-environment-receipt", result.stderr)
        self.assertNotIn("accounting-resolve --campaign-dir", result.stderr, "没有账务暂停时不打印补账命令")
        self.assertEqual(world.write_calls(), [], "只是打印模板，不执行任何写动作")

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

    def _wait_blocked_continue(self, world: rw.World) -> dict:
        import time

        plan = world.plan()
        for _ in range(60):
            plan = world.plan()
            if self.step(plan, "continue")["status"] == "blocked":
                break
            time.sleep(0.2)
        return plan

    def test_zero_request_exit_prints_a_runnable_new_round_redispatch_command_but_never_redispatches(self) -> None:
        """0.165 实战：续跑在派发真实批次之前就因准入失败退出（零请求），计划只说“禁止自动重派，人工核对后换新轮次”，
        操作员手工拼新轮次命令。现在在“占位之后没有新的监督器 run、没有后继 attempt”时给出可直接执行的新轮次命令；仍不自动重派。"""

        world = self._ready()
        (world.root / "dispatch.mode").write_text("fail", encoding="utf-8")
        self.assertIn(world.run("--apply").returncode, (EXIT_OK, EXIT_OPERATOR))
        plan = self._wait_blocked_continue(world)
        step = self.step(plan, "continue")
        self.assertEqual(step["status"], "blocked")
        self.assertIn("判为零请求退出", step["detail"])
        self.assertTrue(step["evidence"]["zero_request"])
        new_params = world.params_path.with_name(f"{world.params_path.stem}-r2{world.params_path.suffix}")
        self.assertEqual(step["evidence"]["new_params_path"], str(new_params))
        self.assertIn(f"s/^ROUND=.*/ROUND=r2/", step["command"])
        self.assertIn(f"recovery-plan.sh {new_params} --apply", step["command"])
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR, again.stderr)
        self.assertIn("修好退出原因后人工执行（本计划不代办）", again.stderr)
        self.assertIn(step["command"], again.stderr)
        self.assertEqual(len(world.dispatches()), 1, "有了命令也不自动重派")
        # 命令是可运行的：照它建出新轮次参数文件，新轮次不继承占位，同一预览已批准，直接重派一次（这回成功）。
        (world.root / "dispatch.mode").write_text("ok", encoding="utf-8")
        shell = subprocess.run(["bash", "-c", step["command"].replace(f"bash {SCRIPTS / 'recovery-plan.sh'}", "true")],
                               capture_output=True, text=True)
        self.assertEqual(shell.returncode, 0, shell.stderr)
        self.assertIn("ROUND=r2", new_params.read_text(encoding="utf-8"))
        redo = subprocess.run([sys.executable, "-B", str(SCRIPTS / "recovery_plan.py"), str(new_params), "--apply"],
                              capture_output=True, text=True, env=world.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(redo.returncode, EXIT_OK, redo.stderr)
        self.assertEqual(len(world.dispatches()), 2)

    def test_exit_after_new_supervisor_run_is_not_called_zero_request_and_prints_no_redispatch_command(self) -> None:
        """续跑启动之后监督器状态目录里出现了新的 run（已经开始过真实采集）：不能当零请求，保持原来的禁止自动重派，不给重派命令。"""

        world = self._ready()
        (world.root / "dispatch.mode").write_text("fail", encoding="utf-8")
        self.assertIn(world.run("--apply").returncode, (EXIT_OK, EXIT_OPERATOR))
        world.add_tail_run("run-after-dispatch", "failed", 0.0)   # 占位之后才出现的 run 目录
        plan = self._wait_blocked_continue(world)
        step = self.step(plan, "continue")
        self.assertEqual(step["status"], "blocked")
        self.assertIn("可能已发出真实请求", step["detail"])
        self.assertIn("run-after-dispatch", step["detail"])
        self.assertEqual(step["command"], "")
        self.assertNotIn("zero_request", step["evidence"])

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


class ZeroRequestEvidenceTests(_WorldCase):
    """零请求判定（``zero_request_evidence``）的每个条件单独成立才放行，任何一条不成立都不能判零。"""

    def _evidence(self, directory: str, *, successors: list[str], inflight: bool = True, setup=None) -> dict:
        helper = self.make_world().helper()
        root = Path(directory)
        out, state = root / "out", root / "state"
        (out / "continue").mkdir(parents=True)
        state.mkdir()
        params = {"OUT": str(out), "VC_STATE_DIR": str(state)}
        if inflight:
            marker = out / "continue" / "REV.inflight.json"
            marker.write_text("{}", encoding="utf-8")
            os.utime(marker, (1000, 1000))
        if setup is not None:
            setup(state)
        return helper.zero_request_evidence(params, "REV", successors)

    def test_nothing_new_and_no_successor_is_zero(self) -> None:
        def old_run(state: Path) -> None:
            (state / "run-old").mkdir()
            os.utime(state / "run-old", (500, 500))

        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(self._evidence(directory, successors=[], setup=old_run)["zero"])

    def test_a_successor_attempt_alone_defeats_zero(self) -> None:
        """后继 attempt 已存在（续跑已预约新 attempt）：即使监督器状态目录里没有新 run 也不能判零请求。"""

        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(directory, successors=["20990101T000000Z-successor0000000000"])
            self.assertFalse(evidence["zero"])
            self.assertEqual(evidence["successors"], ["20990101T000000Z-successor0000000000"])

    def test_a_new_run_directory_alone_defeats_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(directory, successors=[], setup=lambda state: (state / "run-new").mkdir())
            self.assertFalse(evidence["zero"])
            self.assertEqual(evidence["new_runs"], ["run-new"])

    def test_an_old_run_whose_json_was_written_after_dispatch_defeats_zero(self) -> None:
        def touched(state: Path) -> None:
            (state / "run-old").mkdir()
            os.utime(state / "run-old", (500, 500))
            (state / "run-old" / "state.json").write_text("{}", encoding="utf-8")

        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(self._evidence(directory, successors=[], setup=touched)["zero"])

    def test_unprovable_inputs_never_claim_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_marker = self._evidence(directory, successors=[], inflight=False)
            self.assertFalse(missing_marker["zero"])
            self.assertTrue(missing_marker["reason"])


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
                killed = world.run("--apply", **world.crash(crash))
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
            self.assertEqual(world.run("--apply", **world.crash(crash)).returncode, 137)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        plan = world.wait_continue_finished()
        self.assertEqual(world.write_calls(), reference_writes)
        self.assertEqual(world.state(), reference_state)
        self.assertEqual(plan["plan_sha256"], reference_plan)

    def test_interrupt_inside_dispatch_window_never_redispatches(self) -> None:
        world = self._chain()
        # 占位已写、进程还没起：执行结果不确定，保守停下，不自动重派。
        self.assertEqual(world.run("--apply", **world.crash("continue-inflight")).returncode, 137)
        self.assertEqual(world.dispatches(), [])
        plan = world.plan()
        self.assertEqual(self.step(plan, "continue")["status"], "blocked")
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR, again.stderr)
        self.assertIn("禁止自动重派", again.stderr)
        self.assertEqual(world.dispatches(), [])

    def test_interrupt_after_spawn_before_started_record_does_not_double_dispatch(self) -> None:
        world = self._chain()
        self.assertEqual(world.run("--apply", **world.crash("continue-spawned")).returncode, 137)
        import time

        for _ in range(50):  # 等被派生出的续跑替身自己跑完
            if world.dispatches():
                break
            time.sleep(0.1)
        self.assertEqual(len(world.dispatches()), 1)
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR, again.stderr)
        self.assertEqual(len(world.dispatches()), 1, "占位存在而启动记录缺失：不重派")

    def test_interrupted_step_does_not_leave_a_stale_needs_operator_record_behind(self) -> None:
        """0.165 实战：approve 先因缺批准停在 needs-operator，补批准后动作做完、记录写入之前被打断；重入时现场判该步已完成、
        不再执行，``steps/approve.json`` 一直残留旧的 needs-operator。重入后必须补写成 passed（原因写明来源），summary 不丢。"""

        world = self.make_world()
        stopped = world.run("--apply")
        self.assertEqual(stopped.returncode, EXIT_OPERATOR, stopped.stderr)
        record = world.out_dir() / "steps" / "approve.json"
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "needs-operator")
        self.assertEqual(len(world.approve_from_intents()), 2)
        self.assertEqual(world.run("--apply", **world.crash("approve")).returncode, 137)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "needs-operator", "打断时动作已做完但记录还是旧的")
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OK, again.stderr)
        healed = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(healed["status"], "passed")
        self.assertIn("现场判定该步骤已完成", healed["reason"])
        self.assertIn("needs-operator", healed["reason"], "写明补写前的旧状态")
        world.wait_continue_finished()
        for name in ("reconcile", "approve", "authorize", "continue"):
            data = json.loads((world.out_dir() / "steps" / f"{name}.json").read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "passed", name)
        # 只读预览与计划摘要不受影响；再执行一次不再补写（已是 passed）。
        before = (world.out_dir() / "history.ndjson").read_text(encoding="utf-8")
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        after = (world.out_dir() / "history.ndjson").read_text(encoding="utf-8")
        self.assertEqual(before.count('"step": "approve"'), after.count('"step": "approve"'))

    def test_interrupt_leaves_no_partial_state_files(self) -> None:
        world = self._chain()
        self.assertEqual(world.run("--apply", **world.crash("approve")).returncode, 137)
        for path in world.out_dir().rglob("*"):
            self.assertFalse(path.name.startswith(".") and ".tmp-" in path.name, path)


class HalfDoneResumeTests(_WorldCase):
    """campaign-resume 四段写入（收据→总账根因修复→计时账本恢复→总账 campaign_resumed）在第三段之后失败：停线已清、总账未撤终态。"""

    def _world(self, **kwargs) -> rw.World:
        world = self.make_world(ledger_status="stop_required", scenario={"resume_fail_before_project": True}, **kwargs)
        world.pre_approve("campaign-resume", rw.RESUME_SHA, extra_files=("REGRESSION_RECEIPT",))
        world.pre_sign_recovery(seq=11, effective=7)
        return world

    def test_half_done_resume_is_pending_not_done_and_completed_with_the_same_approval(self) -> None:
        world = self._world()
        first = world.run("--apply")
        self.assertEqual(first.returncode, EXIT_FAILED, first.stderr)
        self.assertIn("campaign-resume 写入失败", first.stderr)
        # 现场：计时账本已离开停线，收据在，总账没有 campaign_resumed。
        state = world.state()
        self.assertEqual((state["ledger_status"], state.get("project_resumed", [])), ("recovery_required", []))
        receipts = list((world.campaign_dir / "control/resume").glob("resume-*.json"))
        self.assertEqual(len(receipts), 1)
        plan = world.plan()
        resume = self.step(plan, "resume")
        self.assertEqual(resume["status"], "pending", "只看计时账本会把它误判成已恢复")
        self.assertIn("只做了一半", resume["detail"])
        self.assertEqual(len(resume["evidence"]["unresolved_receipts"]), 1)
        self.assertEqual(plan["overall"], "pending")
        # 续接用收据里的批准，不再要新的批准文件：把人提交的批准删掉也照样补完。
        for approval in (world.runroot / "recovery-plan-approvals").rglob("*.json"):
            data = json.loads(approval.read_text(encoding="utf-8"))
            if data["binding"]["operation"] == "campaign-resume":
                approval.unlink()
        second = world.run("--apply")
        self.assertEqual(second.returncode, EXIT_OK, second.stderr)
        self.assertEqual(world.write_calls(), ["campaign-resume:apply", "campaign-resume:apply", "reconcile-attempt:approve",
                                               "reconcile-attempt:authorize"])
        resume_calls = [call for call in world.calls() if call[0] == "campaign-resume"]
        applies = [call for call in resume_calls if "--approve-sha256" in call]
        self.assertEqual(len(applies), 2)
        self.assertEqual(applies[1][applies[1].index("--approve-sha256") + 1], rw.RESUME_SHA, "续接用的是收据里的批准摘要")
        self.assertEqual(len(resume_calls) - len(applies), 1, "续接不重新预览：只有第一次执行预览过一次")
        self.assertEqual(len(list((world.campaign_dir / "control/resume").glob("resume-*.json"))), 1, "没有第二份收据")
        self.assertEqual(len(world.state()["project_resumed"]), 1)
        self.assertEqual(len(world.dispatches()), 1)
        done = world.wait_continue_finished()
        resume = self.step(done, "resume")
        self.assertEqual(resume["status"], "done")
        self.assertIn("都已在项目总账落账", resume["detail"])

    def test_interrupt_during_completion_still_converges(self) -> None:
        world = self._world()
        self.assertEqual(world.run("--apply").returncode, EXIT_FAILED)
        self.assertEqual(world.run("--apply", **world.crash("resume")).returncode, 137)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        self.assertEqual(len(world.state()["project_resumed"]), 1)
        self.assertEqual(len(world.dispatches()), 1)

    def test_unreadable_project_ledger_blocks_instead_of_guessing(self) -> None:
        world = self._world()
        self.assertEqual(world.run("--apply").returncode, EXIT_FAILED)
        world.set_state(project_ledger_error="总账事件目录不可信")
        plan = world.plan()
        resume = self.step(plan, "resume")
        self.assertEqual(resume["status"], "blocked")
        self.assertIn("总账事件目录不可信", resume["detail"])
        self.assertEqual(world.run("--apply").returncode, EXIT_OPERATOR)
        self.assertEqual(world.dispatches(), [])

    def test_fully_resumed_receipts_do_not_trigger_completion(self) -> None:
        world = self.make_world(ledger_status="stop_required")
        world.pre_approve("campaign-resume", rw.RESUME_SHA, extra_files=("REGRESSION_RECEIPT",))
        world.pre_sign_recovery(seq=11, effective=7)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        world.wait_continue_finished()
        self.assertEqual(world.write_calls().count("campaign-resume:apply"), 1)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        self.assertEqual(world.write_calls().count("campaign-resume:apply"), 1)


class StalePreviewTests(_WorldCase):
    """磁盘上的旧预览不能因为进程在记录写入前被打断就被拿去派发。"""

    def test_pure_staleness_reasons(self) -> None:
        helper = self.make_world().helper()
        preview = {"tool_files_sha256": "f1" * 32, "tool_wire_sha256": "a" * 64, "tool_policy_sha256": "b" * 64,
                   "evolution_indexes": [3, 4], "created_at_utc": "2026-10-21T10:00:00Z"}
        same = {"current_files_sha256": "f1" * 32, "current_wire_sha256": "a" * 64, "current_policy_sha256": "b" * 64, "effective_index": 4}
        self.assertEqual(helper.preview_stale_reasons(preview, same, []), ([], 4))
        reasons, _ = helper.preview_stale_reasons(preview, {**same, "current_files_sha256": "0" * 64}, [])
        self.assertTrue(any("工具文件摘要" in item for item in reasons), reasons)
        reasons, _ = helper.preview_stale_reasons(preview, {**same, "effective_index": 5}, [])
        self.assertTrue(any("预览基于演进 #4，当前有效演进 #5" in item for item in reasons), reasons)
        reasons, _ = helper.preview_stale_reasons(preview, same, [{"approved_sha256": "c" * 64, "approved_at_utc": "2026-10-21T10:00:01Z"}])
        self.assertTrue(any("早于 campaign-resume 批准" in item for item in reasons), reasons)
        self.assertEqual(helper.preview_stale_reasons(preview, same, [{"approved_sha256": "c" * 64, "approved_at_utc": "2026-10-21T09:59:59Z"}])[0], [])
        self.assertEqual(helper.preview_stale_reasons({"evolution_indexes": [], "created_at_utc": None}, {"error": "x"}, []), ([], 0))

    def test_p1_authorized_then_new_drift_registered_then_interrupted_must_not_dispatch(self) -> None:
        """验收者描述的场景：P1 已批准授权；新漂移（新部署）登记完、记录写入前被打断；重入后必须停下要求新预览，不能派发。"""

        world = self.make_world()
        world.pre_sign_recovery(seq=10, effective=7, index=1)
        self.assertEqual(world.run("--apply", **world.crash("authorize")).returncode, 137)
        self.assertEqual(world.state()["ledger_status"], "active", "P1 已批准并授权，只差派发")
        self.assertEqual(world.dispatches(), [])
        world.new_drift()  # 部署了新的受管工具，演进未登记
        world.pre_approve("tool-evolution", rw.EVOLUTION_SHA)
        self.assertEqual(world.run("--apply", **world.crash("evolution")).returncode, 137)
        self.assertEqual(world.state()["effective_index"], 8, "演进登记已落账，只是步骤记录没写")
        # 重入前的只读预览就应当看出 P1 已过期（不靠任何内存状态）。
        plan = world.plan()
        reconcile = self.step(plan, "reconcile")
        self.assertEqual(reconcile["status"], "pending", plan)
        self.assertIn("已过期", reconcile["detail"])
        self.assertIn("工具文件摘要", reconcile["detail"])
        states = {row["object"]: row["state"] for row in plan["objects"]["reverify"]}
        self.assertEqual(states["恢复预览 #1"], "stale")
        again = world.run("--apply")
        self.assertEqual(again.returncode, EXIT_OPERATOR, again.stderr)
        self.assertIn("缺少 recovery-approve 专项人工批准", again.stderr, "必须停下要求新预览的批准")
        self.assertEqual(world.dispatches(), [], "旧预览绝不派发")
        directory = world.campaign_dir / "control/reconciliation" / f"attempt-{world.attempt}"
        self.assertEqual(len(list(directory.glob("recovery-preview-*.json"))), 2, "重新对账出了新预览")
        # 人审阅新预览后批准，才继续并派发。
        world.pre_sign_recovery(seq=10, effective=8, index=2)
        final = world.run("--apply")
        self.assertEqual(final.returncode, EXIT_OK, final.stderr)
        self.assertEqual(len(world.dispatches()), 1)
        self.assertIn(directory.joinpath("recovery-preview-02.json").as_posix(), world.dispatches()[0])

    def test_acceptance_check_catches_ledger_head_advance_the_disk_cannot_show(self) -> None:
        """派发前的接受检查是最后一道：账本 head 在授权之后又前进（磁盘上看不出来），旧预览同样不能派发。"""

        world = self.make_world()
        world.pre_sign_recovery(seq=10, effective=7, index=1)
        self.assertEqual(world.run("--apply", **world.crash("authorize")).returncode, 137)
        self.assertEqual(self.step(world.plan(), "continue")["status"], "pending", "磁盘判定看不出过期")
        world.set_state(ledger_seq=11)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("缺少 recovery-approve 专项人工批准", result.stderr)
        self.assertEqual(world.dispatches(), [])
        directory = world.campaign_dir / "control/reconciliation" / f"attempt-{world.attempt}"
        self.assertEqual(len(list(directory.glob("recovery-preview-*.json"))), 2)
        history = (world.out_dir() / "history.ndjson").read_text(encoding="utf-8")
        self.assertIn("派发前复核发现预览不再有效", history)

    def test_official_phase_readback_catches_a_changed_preview_before_dispatch(self) -> None:
        world = self.make_world(phase="official", ledger_status="stage_review_required", reuse_complete=True)
        world.pre_sign_recovery(seq=10, effective=7, index=1)
        self.assertEqual(world.run("--apply", **world.crash("reconcile")).returncode, 137)
        world.set_state(ledger_seq=11)  # 对账之后账本前进：回读对账会得到新预览
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("缺少 recovery-continue 专项人工批准", result.stderr)
        self.assertEqual(world.dispatches(), [])

    def test_dispatch_is_not_blocked_by_a_consumed_preview_once_dispatched(self) -> None:
        world = self.make_world()
        world.pre_sign_recovery(seq=10, effective=7)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        world.wait_continue_finished()
        world.new_drift()  # 派发之后受管工具又换了：预览已被消费，不再按过期处理
        plan = world.plan()
        self.assertEqual(self.step(plan, "reconcile")["status"], "done")
        self.assertEqual(self.step(plan, "continue")["status"], "done")


class SuccessorTests(_WorldCase):
    def _dispatched(self) -> rw.World:
        world = self.make_world()
        world.pre_sign_recovery(seq=10, effective=7)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        world.wait_continue_finished()
        return world

    def _set_successor_status(self, world: rw.World, status: str | None) -> str:
        name = world.successor_name
        path = world.attempts_dir() / name / "attempt.json"
        if status is None:
            path.unlink()
        else:
            rw.write_json(path, {"attempt_id": name, "phase": world.phase, "status": status, "results": []})
        return name

    def test_successful_successor_keeps_overall_complete_and_shows_its_status(self) -> None:
        world = self._dispatched()
        plan = world.plan()
        self.assertEqual(plan["overall"], "complete")
        rows = self.step(plan, "continue")["evidence"]["successors"]
        self.assertEqual([(row["id"], row["status"], row["class"]) for row in rows], [(world.successor_name, "awaiting_receipts", "ok")])
        self.assertEqual(plan["warnings"], [])
        self.assertNotIn("followed_from", json.loads(world.run("--plan", "--json", "--follow-latest").stdout))

    def test_failed_successor_is_not_reported_complete_and_points_to_the_newest_failure(self) -> None:
        world = self._dispatched()
        name = self._set_successor_status(world, "environment_contaminated")
        plan = world.plan()
        self.assertEqual(plan["overall"], "superseded-failed")
        self.assertEqual(self.step(plan, "continue")["status"], "done")
        self.assertTrue(any(name in item and "--follow-latest" in item and "已失败" in item for item in plan["warnings"]), plan["warnings"])
        self.assertIn("environment_contaminated", self.step(plan, "continue")["detail"])
        text = world.run("--plan").stdout
        self.assertIn("总体 superseded-failed", text)
        self.assertIn("警告：", text)
        applied = world.run("--apply")
        self.assertEqual(applied.returncode, EXIT_OPERATOR, applied.stderr)
        self.assertIn("改指最新失败的 attempt", applied.stderr)
        self.assertEqual(len(world.dispatches()), 1, "对已被取代的目标不再派发")

    def test_follow_latest_retargets_plan_and_apply_to_the_failed_successor(self) -> None:
        world = self._dispatched()
        name = self._set_successor_status(world, "failed")
        followed = json.loads(world.run("--plan", "--json", "--follow-latest").stdout)
        self.assertEqual((followed["attempt"], followed["followed_from"], followed["attempt_status"]), (name, world.attempt, "failed"))
        self.assertEqual(self.step(followed, "reconcile")["status"], "pending")
        self.assertEqual(followed["warnings"], [])
        applied = world.run("--apply", "--follow-latest")
        self.assertEqual(applied.returncode, EXIT_OPERATOR, applied.stderr)
        self.assertIn("--follow-latest：目标 attempt 由", applied.stderr)
        self.assertIn("缺少 recovery-approve 专项人工批准", applied.stderr, "新目标要重新批准，批准按新 ATTEMPT 绑定")
        self.assertEqual(len(world.dispatches()), 1)
        # 参数文件没有被改写。
        self.assertIn(world.attempt, world.params_path.read_text(encoding="utf-8"))

    def test_unknown_successor_warns_but_is_not_followed(self) -> None:
        world = self._dispatched()
        self._set_successor_status(world, None)  # 只剩目录，没有 attempt.json
        plan = world.plan()
        self.assertEqual(plan["overall"], "complete")
        self.assertTrue(any("状态未知" in item for item in plan["warnings"]), plan["warnings"])
        self.assertNotIn("followed_from", json.loads(world.run("--plan", "--json", "--follow-latest").stdout))

    def test_unreadable_successor_attempt_json_does_not_fail_the_plan(self) -> None:
        world = self._dispatched()
        path = world.attempts_dir() / world.successor_name / "attempt.json"
        path.write_text("", encoding="utf-8")  # 正在被写的半截文件
        plan = world.plan()
        self.assertTrue(any("状态未知" in item for item in plan["warnings"]), plan["warnings"])


class AcceptanceGateAndLogTests(_WorldCase):
    def test_crash_variable_alone_does_nothing(self) -> None:
        world = self.make_world()
        result = world.run("--apply", RECOVERY_PLAN_CRASH_AFTER="reconcile")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)  # 正常走到缺批准，没有被打断
        self.assertIn("忽略 RECOVERY_PLAN_CRASH_AFTER", result.stderr)
        wrong = world.run("--apply", RECOVERY_PLAN_CRASH_AFTER="reconcile", RECOVERY_PLAN_ACCEPTANCE="yes")
        self.assertEqual(wrong.returncode, EXIT_OPERATOR, wrong.stderr)
        fresh = self.make_world()  # 前两次已经对账过，这里换一个干净现场，让"对账之后"这个打断点真的会到达
        both = fresh.run("--apply", **fresh.crash("reconcile"))
        self.assertEqual(both.returncode, 137)

    def _fake_python_script(self, world: rw.World) -> tuple[dict[str, str], Path]:
        """PATH 垫片：python3 只打印验收两个变量就退出；id 报 root。用来验证 recovery-plan.sh 自己对环境变量的处理。"""

        shims = world.root / "shims"
        shims.mkdir()
        rw.write(shims / "python3", '#!/bin/bash\necho "CRASH=[${RECOVERY_PLAN_CRASH_AFTER:-}] ACCEPT=[${RECOVERY_PLAN_ACCEPTANCE:-}] ARGS=$*"\n', 0o700)
        rw.write(shims / "id", '#!/bin/bash\nif [ "${1:-}" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi\n', 0o700)
        env = {**world.env(), "PATH": f"{shims}:{os.environ['PATH']}", "RECOVERY_PLAN_CRASH_AFTER": "approve", "RECOVERY_PLAN_ACCEPTANCE": "1"}
        return env, shims

    def test_wrapper_script_clears_the_injection_variables_by_default(self) -> None:
        world = self.make_world()
        env, _ = self._fake_python_script(world)
        for arguments in ([], ["--apply"], ["--plan"]):
            done = subprocess.run(["bash", str(rw.SCRIPT), str(world.params_path), *arguments], capture_output=True, text=True, env=env,
                                  stdin=subprocess.DEVNULL)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("CRASH=[] ACCEPT=[]", done.stdout, arguments)

    def test_wrapper_script_inject_option_sets_both_only_for_apply(self) -> None:
        world = self.make_world()
        env, _ = self._fake_python_script(world)
        done = subprocess.run(["bash", str(rw.SCRIPT), str(world.params_path), "--apply", "--inject-crash-after", "resume"],
                              capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("CRASH=[resume] ACCEPT=[1]", done.stdout)
        self.assertIn("验收打断注入已开启", done.stderr)
        refused = subprocess.run(["bash", str(rw.SCRIPT), str(world.params_path), "--inject-crash-after", "resume"],
                                 capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(refused.returncode, EXIT_USAGE)
        self.assertIn("只对 --apply 有效", refused.stderr)

    def test_official_entry_log_is_per_round_and_old_log_is_kept(self) -> None:
        world = self.make_world(phase="official", ledger_status="stage_review_required")
        world.pre_sign_recovery(seq=10, effective=7)
        old = world.runroot / "recovery-plan-entry-r1.out"
        old.write_text("上一次同轮次的旧日志\n", encoding="utf-8")
        self.assertIn(world.run("--apply").returncode, (EXIT_OK, EXIT_OPERATOR))
        world.wait_continue_finished()
        kept = sorted(world.runroot.glob("recovery-plan-entry-r1.out.pre-r1-*"))
        self.assertEqual(len(kept), 1, kept)
        self.assertEqual(kept[0].read_text(encoding="utf-8"), "上一次同轮次的旧日志\n")
        self.assertIn("entry.sh done", old.read_text(encoding="utf-8"))
        started = json.loads(next((world.out_dir() / "continue").glob("*.started.json")).read_text(encoding="utf-8"))
        self.assertTrue(started["log"].endswith("recovery-plan-entry-r1.out"), started)


class PauseWarningTests(_WorldCase):
    def test_paused_scopes_are_listed_as_a_warning(self) -> None:
        world = self.make_world()
        self.assertEqual(world.plan()["warnings"], [])
        world.set_state(paused_scopes=["stage"])
        plan = world.plan()
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("预算暂停范围 ['stage']", plan["warnings"][0])
        self.assertIn("deadline-extend", plan["warnings"][0])
        self.assertIn("警告：", world.run("--plan").stdout)

    def test_deadline_pause_stops_with_deadline_extend_hint_then_continues(self) -> None:
        world = self.make_world(scenario={"pauses": {"20261021T135838Z-70aa3c8b45478aec": ["deadline"]}})
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("deadline-extend", result.stderr)
        self.assertNotIn("--from", result.stderr)
        self.assertNotIn("EXTEND_DEADLINE", result.stderr, "本计划不代办延期，也没有 pre-extend 步骤")
        self.assertIn("重新执行同一条命令", result.stderr)
        self.assertEqual(world.write_calls(), [])
        world.set_state(deadline_resolved=True)
        world.pre_sign_recovery(seq=10, effective=7)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        self.assertEqual(len(world.dispatches()), 1)

    def test_request_budget_pause_stops_with_request_budget_extend_hint_then_continues(self) -> None:
        world = self.make_world(scenario={"pauses": {"20261021T135838Z-70aa3c8b45478aec": ["request_budget"]}})
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR, result.stderr)
        self.assertIn("request-budget-extend", result.stderr)
        self.assertIn("重新执行同一条命令", result.stderr)
        self.assertEqual(world.write_calls(), [])
        self.assertEqual(world.dispatches(), [])
        world.set_state(request_budget_resolved=True)
        world.pre_sign_recovery(seq=10, effective=7)
        self.assertEqual(world.run("--apply").returncode, EXIT_OK)
        self.assertEqual(len(world.dispatches()), 1)

    def test_reconcile_writes_without_a_human_approval(self) -> None:
        """对账（reconcile-runs、reconcile）会写项目总账、不要人工批准——与 fix-and-continue 一致；第一次执行没有任何批准文件时它们照样执行。"""

        world = self.make_world()
        world.add_tail_run("run-b", "failed", 200.0)
        result = world.run("--apply")
        self.assertEqual(result.returncode, EXIT_OPERATOR)
        names = [call[0] for call in world.calls()]
        self.assertIn("reconcile-supervisor-run", names)
        self.assertIn("reconcile-attempt", names)
        self.assertFalse((world.runroot / "recovery-plan-approvals").exists())


if __name__ == "__main__":
    unittest.main()
