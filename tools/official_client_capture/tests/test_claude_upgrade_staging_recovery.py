"""Claude 提交中崩溃恢复（Claude 升级控制面设计 B3-06）：批次派发事务 P1～P4、动作结果 WAL 的幂等键判定与过期租约死角。

全部零请求、离线。批次夹具复用 B3-01 的真实工作树夹具（``_RealTreeBatchCase``）：动作在父监督器子进程里真实执行，崩溃点
在入口进程内抛错、在入口子进程里 ``os._exit``（驱动脚本，模拟 SIGKILL），或经 ``usercustomize`` 只在指定动作的子进程里
注入；不替换编译器与制品构建器。

* P1：prepare 之后、父 run 之前失败——命令行路径与直接调用编排器薄壳的路径各一例（后者在 B3-06 之前会因对账器停在 Codex
  作用域而按两套码表算出不同根因、失败关闭）：ABORT 入账一次根因，序号不占，同序号重派成功。
* P2：父 run 已 prepared、owner 在 commit 之前被 SIGKILL：monitor 封存 ``aborted_prepared``，下次入口对账、ABORT、同序号重派。
* P3：commit 发布步失败（账本事件已写、COMMIT 未写）：对账、ABORT、归档已发布的半产物，同序号重派。
* P4：COMMIT 已写、父 run 激活失败：账本 ``recovery_required``，对账后 N+1 重派。
* 中间态两个边界：P1 链 ABORT 已写、总账未推送；P3 链对账收据已写、outbox 未提交——下次入口闭合，没有重复入账。
* VC-3 的两个 Store 写入点（准备批准的 EvidenceApprovalFact、暂存批准的 ProfileApprovalFact），各在写入前与写入后硬杀：
  写入前崩溃，N+1 重派写下唯一一条事实（producer_run 是重派的 run）；写入后崩溃，N+1 按幂等键复用原事实（producer_run
  仍是写下它的那次 run），事实数不增。
* 过期租约死角：run 写完 attempt.json 之后、释放租约之前被强杀，seal 先把过期租约固化为停线收据再继续；封存之后
  classify、VC-3 与前序导入按「绑定的 attempt 已封存即了结」放行；执行者仍持锁或租约仍在有效期内照旧拒绝。
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import site
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_batch as batch
from tools.official_client_capture import claude_upgrade_profile as profile
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_profile as profile_tests
from tools.official_client_capture.tests import test_claude_upgrade_recovery as recovery_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests

REPOSITORY_ROOT = Path(claude_upgrade.__file__).resolve().parents[2]
CAMPAIGN_ID = batch_tests.CAMPAIGN_ID
run_main = batch_tests.run_main

# 入口子进程驱动：按配置替换编排器、监督器或对账器的函数（exit_before／exit_after 硬退出、raise_after 抛错、fail_second 第二次
# 调用抛 ConfigurationError），再跑真实的 ``claude_upgrade.py`` 主入口。与 Codex staging 派发测试的驱动同一做法。
DRIVER = r"""
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
config = json.loads(sys.argv[2])
# 崩溃点用 os._exit 直接结束进程，来不及删的临时目录落在用例临时根下，随用例一起清理。
tempfile.tempdir = config["tmpdir"]
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import runtime_egress_fixtures
modules = {"claude_upgrade": claude_upgrade, "supervisor": supervisor, "reconciler": reconciler}
for item in config["patches"]:
    module = modules[item["module"]]
    original = getattr(module, item["name"])

    def make(original, mode):
        calls = {"count": 0}

        def wrapper(*args, **kwargs):
            calls["count"] += 1
            if mode == "exit_before":
                os._exit(9)
            if mode == "fail_second" and calls["count"] == 2:
                raise claude_upgrade.ConfigurationError("injected-second-call-" + original.__name__)
            result = original(*args, **kwargs)
            if mode == "exit_after":
                os._exit(9)
            if mode == "raise_after":
                raise RuntimeError("injected-after-" + original.__name__)
            return result
        return wrapper

    setattr(module, item["name"], make(original, item["mode"]))
with runtime_egress_fixtures.offline_campaign_egress():
    code = claude_upgrade.main(config["argv"])
print(json.dumps({"returncode": code}))
"""

CRASH_ACTION_ENV = "CLAUDE_B306_TEST_CRASH_ACTION"
CRASH_MARKER_ENV = "CLAUDE_B306_TEST_CRASH_MARKER"
CRASH_HOOK_ENV = "CLAUDE_B306_TEST_CRASH_HOOK"
# 动作子进程里的崩溃注入（不进受管树）：``rename-suffix:<后缀>`` 在改名发布到该目标那一刻、``open-create:<片段>`` 在新建路径含该
# 片段的文件那一刻硬退出（不执行任何 finally），每个标记文件只触发一次。Store 事实文件新建于 ``control-store/facts/`` 下，
# 用它模拟 WAL 第 ① 步之前的崩溃；目录改名发布在事实写入之后，用它模拟第 ① 步之后的崩溃。
USERCUSTOMIZE = f'''"""B3-06 测试注入（不进受管树）：父监督器指定动作的子进程在给定的文件系统操作处硬退出一次。"""
import os

_ACTION = os.environ.get("{CRASH_ACTION_ENV}")
if _ACTION and os.environ.get("{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV}") == _ACTION:
    _MARKER = os.environ["{CRASH_MARKER_ENV}"]
    _KIND, _, _PATTERN = os.environ["{CRASH_HOOK_ENV}"].partition(":")

    def _crash():
        if os.path.exists(_MARKER):
            return
        with open(_MARKER, "w", encoding="utf-8") as handle:
            handle.write("crashed\\n")
        os._exit(17)

    if _KIND == "rename-suffix":
        _RENAME = os.rename

        def _rename(source, destination, *arguments, **options):
            if str(destination).endswith(_PATTERN):
                _crash()
            return _RENAME(source, destination, *arguments, **options)

        os.rename = _rename
    elif _KIND == "open-create":
        _OPEN = os.open

        def _open(path, flags, *arguments, **options):
            if flags & os.O_CREAT and _PATTERN in os.fspath(path):
                _crash()
            return _OPEN(path, flags, *arguments, **options)

        os.open = _open
'''


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _claude_scope() -> Any:
    return upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude"))


class _StagingCase(batch_tests._RealTreeBatchCase):
    """B3-01 的批次夹具加上 P1～P4 需要的读取与驱动。"""

    def draft_actions(self, campaign: Path) -> list[dict[str, Any]]:
        argv = ["classify", "--campaign-dir", campaign, "--candidates", self.candidates_path(campaign)]
        return [batch.managed_action("vc2-classify-draft", "VC-2:vc2-classify-draft", argv, item_ids=["vc2-classify-draft"])]

    def head(self) -> dict[str, Any]:
        return project_ledger.replay_head(self.project_root)

    def staging_dir(self, sequence: int, phase: str = "VC-2") -> Path:
        return self.campaign / "control" / "vc" / "staging" / f"{sequence:04d}-{phase.lower()}"

    def commit_path(self, sequence: int, phase: str = "VC-2") -> Path:
        return self.campaign / "control" / "vc" / "commits" / f"{sequence:04d}-{phase.lower()}.json"

    def abort(self, sequence: int, attempt: int = 1) -> dict[str, Any]:
        return artifacts.validate_staging_abort(_read(self.staging_dir(sequence) / f"attempt-{attempt}" / "ABORT"))

    def staging_runs(self) -> list[Path]:
        """prepared 起动的父 run（排除 no-op 首批引导 run）。"""

        return [path for path in sorted(self.state_dir.glob("run-*")) if supervisor._read_state(path).get("staging_binding") is not None]

    def wait_terminal(self, run_dir: Path, expected: set[str], *, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = supervisor._read_state(run_dir)
            if state.get("state") in expected:
                return state
            time.sleep(0.1)
        self.fail(f"父 run 未进入 {sorted(expected)}：{supervisor._read_state(run_dir).get('state')}")

    def ledger_events(self) -> list[tuple[str, str]]:
        return [(event["event_type"], event["event_id"]) for event, _raw in timing._load_events(self.ledger_dir)]

    def cli_argv(self, phase: str, sequence: int, plan_path: Path) -> list[str]:
        previous = artifacts.VC_PHASES[artifacts.VC_PHASES.index(phase) - 1]
        return [
            "compile-and-run-vc-batch", "--campaign-dir", str(self.campaign), "--phase", phase, "--sequence", str(sequence),
            "--predecessor-checkpoint", str(claude_upgrade_vc.vc_checkpoint_path(self.campaign, previous)),
            "--action-plan", str(plan_path), "--state-dir", str(self.state_dir),
        ]

    def plan_file(self, phase: str, actions: list[dict[str, Any]]) -> Path:
        path = self.root / "action-plans" / f"{phase.lower()}-{actions[0]['action_id']}.json"
        if not path.exists():
            batch_tests._write_json(path, batch.action_plan(actions, phase=phase))
        return path

    def run_driver(self, phase: str, sequence: int, actions: list[dict[str, Any]], patches: list[dict[str, str]], *, expect_exit: int = 9) -> None:
        """在入口子进程里派发，按补丁在指定位置硬退出（模拟 SIGKILL）。"""

        driver_tmp = self.root / "driver-tmp"
        driver_tmp.mkdir(mode=0o700, exist_ok=True)
        config = {"tmpdir": str(driver_tmp), "argv": self.cli_argv(phase, sequence, self.plan_file(phase, actions)), "patches": patches}
        completed = subprocess.run(
            [sys.executable, "-c", DRIVER, str(REPOSITORY_ROOT), json.dumps(config)],
            capture_output=True, text=True, timeout=300, check=False,
        )
        self.assertEqual(completed.returncode, expect_exit, completed.stderr[-2000:])

    def assert_unique_accounting(self) -> None:
        """总账 outbox 的 operation_id 与计时账本事件都不重复（中间态崩溃后重放不得重复入账）。"""

        outbox = self.campaign / project_ledger.CAMPAIGN_LEDGER_DIR_NAME / "outbox"
        operations = [project_ledger._read_batch(path)["commit"]["operation_id"] for _number, path in project_ledger._batch_dirs(outbox)]
        self.assertEqual(len(operations), len(set(operations)), operations)
        events = self.ledger_events()
        self.assertEqual(len(events), len(set(events)), events)


class StagingCommitRecoveryTests(_StagingCase):
    """批次派发事务 P1～P4 与两个中间态边界：每种崩溃之后同序号（P4 为 N+1）重派收敛，根因只入账一次。"""

    def _assert_p1_closed(self, campaign: Path) -> None:
        abort = self.abort(2)
        self.assertEqual((abort["stage"], abort["failure_kind"], abort["error_type"]), ("prepare", "prepare-failed", "RuntimeError"))
        self.assertIsNone(abort["parent_run_dir"])
        with _claude_scope():
            expected = reconciler.staging_abort_root_cause("VC-2", "prepare", error_type="RuntimeError", error_signature=abort["error_signature"])
        # 根因按 Claude 码表编码（稳定维度带 client），对账复算与 ABORT 记录的是同一个 ID。
        self.assertEqual(expected["stable_dimensions"]["client"], "claude")
        self.assertEqual(abort["root_cause_id"], expected["root_cause_id"])
        head = self.head()
        self.assertIn("staging-abort:0002:1", head["operations"])
        self.assertEqual(head["root_cause_counts"], {abort["root_cause_id"]: 1})
        # 序号未占：没有 COMMIT、正式文件与父 run；账本没有新事件（阶段没有真正开始）。
        self.assertFalse(self.commit_path(2).exists())
        self.assertFalse((campaign / "control" / "vc" / "batches" / "0002-vc-2.json").exists())
        self.assertEqual(self.staging_runs(), [])
        # 同序号重派：attempt-2 提交，attempt-1 的 ABORT 不再重复入账。
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, self.draft_actions(campaign)), 2, "VC-2")
        self.assertEqual((result["staging_attempt"], result["orphans"]), (2, []))
        self.assertEqual(result["commit"]["staging_attempt"], 2)
        self.assertEqual(self.head()["root_cause_counts"], {abort["root_cause_id"]: 1})
        self.assert_zero_requests()

    def _crashing_compile(self):
        original = claude_upgrade.compile_vc_batch

        def crashing(arguments, **options):
            original(arguments, **options)
            raise RuntimeError("crash-after-prepare")

        return mock.patch.object(claude_upgrade, "compile_vc_batch", side_effect=crashing)

    def test_p1_prepare_failure_via_cli_converges(self) -> None:
        campaign = self.campaign = self.build_batch_campaign()
        events_before = self.ledger_events()
        with self._crashing_compile():
            code, result, stderr = self.dispatch(campaign, "VC-2", 2, self.draft_actions(campaign))
        self.assertEqual((code, result), (1, None))
        self.assertIn("crash-after-prepare", stderr)
        self.assertEqual(self.ledger_events(), events_before)
        self._assert_p1_closed(campaign)

    def test_p1_prepare_failure_via_direct_shell_converges(self) -> None:
        """直接调用 Claude 编排器薄壳（atomic-double 等程序化调用方的路径）：对账器随 adapter_scope 进入 Claude 作用域。"""

        campaign = self.campaign = self.build_batch_campaign()
        arguments = claude_upgrade._build_parser().parse_args(
            self.cli_argv("VC-2", 2, self.plan_file("VC-2", self.draft_actions(campaign)))
        )
        with self._crashing_compile(), self.assertRaisesRegex(RuntimeError, "crash-after-prepare"):
            claude_upgrade.compile_and_run_vc_batch(arguments)
        self._assert_p1_closed(campaign)

    def test_p2_prepared_orphan_after_owner_sigkill_converges(self) -> None:
        campaign = self.campaign = self.build_batch_campaign()
        events_before = self.ledger_events()
        self.run_driver("VC-2", 2, self.draft_actions(campaign), [{"module": "supervisor", "name": "_commit_prepared_run", "mode": "exit_before"}])
        (run_dir,) = self.staging_runs()
        # monitor 独立进程把 prepared 孤儿封存为 aborted_prepared(prepared-abandoned)；commit 未开始，账本没有事件。
        self.wait_terminal(run_dir, {"aborted_prepared"})
        self.assertEqual(_read(run_dir / "stop-receipt.json")["reason"], "prepared-abandoned")
        self.assertEqual(self.ledger_events(), events_before)
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, self.draft_actions(campaign)), 2, "VC-2")
        self.assertEqual(result["staging_attempt"], 2)
        (handled,) = result["orphans"]
        self.assertEqual((handled["kind"], handled["decision"]), ("parent-run", "recoverable"))
        abort = self.abort(2)
        self.assertEqual((abort["stage"], abort["failure_kind"], abort["parent_run_state"]), ("parent-run", "abandoned", "aborted_prepared"))
        self.assertEqual(abort["root_cause_id"], handled["root_cause_id"])
        head = self.head()
        self.assertIn(f"reconcile-supervisor-run:{run_dir.name}", head["operations"])
        self.assertEqual(head["root_cause_counts"], {abort["root_cause_id"]: 1})
        self.assert_zero_requests()

    def test_p3_commit_publish_failure_converges(self) -> None:
        campaign = self.campaign = self.build_batch_campaign()
        original = claude_upgrade._publish_staging_file
        calls = {"count": 0}

        def failing(source: Path, target: Path, campaign_dir: Path) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise ConfigurationError("simulated publish failure")
            original(source, target, campaign_dir)

        with mock.patch.object(claude_upgrade, "_publish_staging_file", side_effect=failing):
            code, result, stderr = self.dispatch(campaign, "VC-2", 2, self.draft_actions(campaign))
        self.assertEqual((code, result), (1, None))
        self.assertRegex(stderr, "commit-publish 步失败.*可按同序号重新派发")
        (run_dir,) = self.staging_runs()
        self.assertEqual(supervisor._read_state(run_dir)["state"], "aborted_prepared")
        self.assertEqual(_read(run_dir / "stop-receipt.json")["reason"], "staging-commit-failed:commit-publish")
        receipt = _read(campaign / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json")
        self.assertEqual((receipt["failure_class"], receipt["reservation_exists"], receipt["live_request_count"]), ("parent-prepare-abandoned", False, 0))
        self.assertEqual(receipt["root_cause"]["stable_dimensions"]["client"], "claude")
        summary = timing.inspect_ledger(self.ledger_dir)
        self.assertEqual((summary["status"], summary["active_phase"], summary["next_action"]), ("active", "VC-2", "redispatch-same-sequence"))
        abort = self.abort(2)
        self.assertEqual((abort["stage"], abort["failure_kind"], abort["parent_run_dir"]), ("commit-publish", "commit-failed", str(run_dir)))
        self.assertEqual(abort["root_cause_id"], receipt["root_cause"]["root_cause_id"])
        archive = campaign / "control" / "vc" / "staging-aborts" / "0002-vc-2" / "attempt-1"
        self.assertTrue((archive / "batch.json").is_file())
        self.assertFalse((campaign / "control" / "vc" / "batches" / "0002-vc-2.json").exists())
        self.assertFalse(self.commit_path(2).exists())
        # 同序号重派：阶段已 active，不再补登记事件；attempt-2 提交。
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, self.draft_actions(campaign)), 2, "VC-2")
        self.assertEqual((result["staging_attempt"], result["timing_ledger"]["events"], result["orphans"]), (2, [], []))
        self.assertEqual(result["commit"]["ledger_event_ids"], [])
        self.assertEqual(self.head()["root_cause_counts"], {abort["root_cause_id"]: 1})
        self.assert_zero_requests()

    def test_p4_activation_failure_reconciles_and_n_plus_one_redispatches(self) -> None:
        campaign = self.campaign = self.build_batch_campaign()

        def broken_activate(client: Any, commit: Any) -> None:
            del client, commit
            raise supervisor.SupervisorError("simulated state write failure")

        with mock.patch.object(supervisor.SupervisorClient, "activate_committed", broken_activate):
            code, result, stderr = self.dispatch(campaign, "VC-2", 2, self.draft_actions(campaign))
        self.assertEqual((code, result["status"], result["campaign_run"]["reason"]), (1, "failed", "parent-start-failed"), stderr)
        run_dir = Path(result["campaign_run"]["run_dir"])
        self.assertEqual(supervisor.classify_prepared_run(run_dir), "committed")
        self.assertTrue(self.commit_path(2).is_file())
        self.assertEqual(result["campaign_run"]["timing_closeout"]["ledger_status"], "recovery_required")
        # 未对账即派发被拒，零写入。
        code, _result, stderr = self.dispatch(campaign, "VC-2", 3, self.draft_actions(campaign))
        self.assertEqual(code, 1)
        self.assertIn("recovery_required", stderr)
        self.assertFalse(self.staging_dir(3).exists())
        code, reconciled, stderr = run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", run_dir])
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        cause = reconciled["root_cause"]
        self.assertEqual((cause["stable_error_code"], cause["stable_dimensions"]["client"]), ("parent-start.failed", "claude"))
        summary = timing.inspect_ledger(self.ledger_dir)
        self.assertEqual((summary["status"], summary["active_phase"], summary["next_action"]), ("active", "VC-2", "redispatch-same-batch"))
        # 幂等：再对账不重复入账。
        run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", run_dir])
        self.assertEqual(self.head()["root_cause_counts"], {cause["root_cause_id"]: 1})
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 3, self.draft_actions(campaign)), 3, "VC-2")
        self.assertEqual(result["orphans"], [])
        self.assertEqual(sorted(int(path.name[:4]) for path in (campaign / "control" / "vc" / "commits").iterdir()), [2, 3])
        self.assert_zero_requests()

    def test_p1_boundary_abort_written_before_ledger_push_closes_on_next_entry(self) -> None:
        """P1 链：ABORT 已写、总账 outbox 未写时硬杀；下次入口补 outbox → 推送 → 判定，同序号重派，根因只计一次。"""

        campaign = self.campaign = self.build_batch_campaign()
        self.run_driver(
            "VC-2", 2, self.draft_actions(campaign),
            [
                {"module": "claude_upgrade", "name": "compile_vc_batch", "mode": "raise_after"},
                {"module": "claude_upgrade", "name": "_write_staging_abort", "mode": "exit_after"},
            ],
        )
        self.assertTrue((self.staging_dir(2) / "attempt-1" / "ABORT").is_file())
        self.assertNotIn("staging-abort:0002:1", self.head()["operations"])
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, self.draft_actions(campaign)), 2, "VC-2")
        self.assertEqual(result["staging_attempt"], 2)
        self.assertEqual([item["kind"] for item in result["orphans"]], ["staging-attempt"])
        head = self.head()
        self.assertIn("staging-abort:0002:1", head["operations"])
        self.assertEqual(sum(head["root_cause_counts"].values()), 1)
        self.assert_unique_accounting()
        # 再派发一批：闭合态不再被重复处理。
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 3, self.draft_actions(campaign)), 3, "VC-2")
        self.assertEqual(result["orphans"], [])

    def test_p3_boundary_receipt_written_before_outbox_closes_on_next_entry(self) -> None:
        """P3 链：父 run 对账收据已写、总账 outbox 未提交时硬杀；下次入口续完对账、ABORT 与归档，同序号重派，许可事件只一条。"""

        campaign = self.campaign = self.build_batch_campaign()
        self.run_driver(
            "VC-2", 2, self.draft_actions(campaign),
            [
                {"module": "claude_upgrade", "name": "_publish_staging_file", "mode": "fail_second"},
                {"module": "reconciler", "name": "_commit_batch", "mode": "exit_before"},
            ],
        )
        (run_dir,) = self.staging_runs()
        self.wait_terminal(run_dir, {"aborted_prepared"})
        self.assertTrue((campaign / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json").is_file())
        self.assertNotIn(f"reconcile-supervisor-run:{run_dir.name}", self.head()["operations"])
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, self.draft_actions(campaign)), 2, "VC-2")
        self.assertEqual(result["staging_attempt"], 2)
        head = self.head()
        self.assertIn(f"reconcile-supervisor-run:{run_dir.name}", head["operations"])
        self.assertEqual(sum(head["root_cause_counts"].values()), 1)
        self.assertEqual(self.abort(2)["stage"], "commit-publish")
        self.assertTrue((campaign / "control" / "vc" / "staging-aborts" / "0002-vc-2" / "attempt-1" / "batch.json").is_file())
        self.assertEqual(self.ledger_events().count(("receipt_passed", f"reconcile-run-passed-{run_dir.name}")), 1)
        self.assert_unique_accounting()
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 3, self.draft_actions(campaign)), 3, "VC-2")
        self.assertEqual(result["orphans"], [])


@unittest.skipUnless(site.ENABLE_USER_SITE, "崩溃注入经 usercustomize 进入动作子进程，要求启用用户 site")
class Vc3StoreWriteIdempotencyTests(_StagingCase):
    """动作结果 WAL 的幂等键判定（Claude 专属）：VC-3 两个 Store 写入点各在写入前、写入后硬杀，N+1 重派收敛。

    两个用例互换崩溃位置，各自覆盖两个写入点（同一 Campaign 里同一动作失败两次会累计同一根因、触发根因上限暂停，
    所以前、后两种崩溃分在两个 Campaign）。准备批准不是 VC-3 的最后一批，它重派成功后同一阶段还要派发暂存预览与批准，
    顺带验证后继协议对已执行过的历史后继不再复算实物。
    """

    FACTS_HOOK = "open-create:/control-store/facts/"

    def _vc3_ready(self) -> Path:
        campaign = self.campaign = self.build_batch_campaign()
        self.approve_vc2_directly(campaign, change=(profile_tests.CHANGE,), delete=(profile_tests.DELETE,))
        for source, name in ((profile_tests.ACTIVE_CATALOG, "active-release-catalog.json"), (profile_tests.ACTIVE_PROFILE, "active-profile.json")):
            (campaign / "analysis" / name).write_bytes(source.read_bytes())
            (campaign / "analysis" / name).chmod(0o600)
        return campaign

    def _action(self, action_id: str, argv: list[Any]) -> dict[str, Any]:
        return batch.managed_action(action_id, f"VC-3:{action_id}", argv, item_ids=[action_id])

    def _facts(self, kind: str) -> list[dict[str, Any]]:
        return [fact for fact in self.store_facts() if fact["fact_kind"] == kind]

    def _crash_dispatch(self, sequence: int, action: dict[str, Any], hook: str) -> Path:
        """注入崩溃派发一批：动作子进程按 ``hook`` 硬退出，批次失败、账本进入阶段审核；返回失败的父 run。"""

        inject = self.root / "inject"
        inject.mkdir(mode=0o700, exist_ok=True)
        (inject / "usercustomize.py").write_text(USERCUSTOMIZE, encoding="utf-8")
        marker = self.root / f"crashed-{sequence}.marker"
        environment = {
            "PYTHONPATH": str(inject), CRASH_ACTION_ENV: str(action["action_id"]), CRASH_MARKER_ENV: str(marker), CRASH_HOOK_ENV: hook,
        }
        with mock.patch.dict(os.environ, environment):
            code, failed, stderr = self.dispatch(self.campaign, "VC-3", sequence, [action])
        self.assertTrue(marker.is_file(), f"崩溃注入没有生效：{hook}")
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, f"action-failed:{action['action_id']}"), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "stage_review_required")
        return Path(failed["campaign_run"]["run_dir"])

    def _reconcile_and_redispatch(self, failed_run: Path, sequence: int, action: dict[str, Any]) -> Path:
        code, reconciled, stderr = run_main(["reconcile-supervisor-run", "--campaign-dir", self.campaign, "--run-dir", failed_run])
        self.assertIsNotNone(reconciled, stderr)
        self.assertEqual((code, reconciled["status"], reconciled["stage_replay"]["allowed"]), (0, "recoverable", True), stderr)
        result = self.assert_batch_passed(self.dispatch(self.campaign, "VC-3", sequence, [action]), sequence, "VC-3")
        return Path(result["campaign_run"]["run_dir"])

    def _action_after_prepare_revision(self, campaign: Path) -> dict[str, Any]:
        self.assert_batch_passed(self.dispatch(campaign, "VC-3", 2, [self._action("vc3-prepare-draft", self.prepare_draft_argv(campaign))]), 2, "VC-3")
        directory = self.write_prepare(self.resolved_prepare(campaign, campaign / profile.PREPARE_DRAFT_ROOT / "rev-0001"), self.root / "prepare-resolved")
        revise = ["prepare-profile", "--campaign-dir", campaign, "--from-revision", "rev-0001", "--revision-input", directory]
        self.assert_batch_passed(self.dispatch(campaign, "VC-3", 3, [self._action("vc3-prepare-revise", revise)]), 3, "VC-3")
        code, revised, stderr = self.revise_prepare(campaign, "rev-0001", directory)
        self.assertEqual((code, revised["revision"], revised["reused"]), (2, "rev-0002", True), stderr)
        return self._action("vc3-prepare-approve", self.approve_prepare_argv(campaign, "rev-0002", revised["review_sha256"]))

    def _stage_approval(self, campaign: Path, sequence: int) -> dict[str, Any]:
        self.assert_batch_passed(self.dispatch(campaign, "VC-3", sequence, [self._action("vc3-stage-preview", ["stage-profile", "--campaign-dir", campaign])]), sequence, "VC-3")
        (joint_manifest,) = sorted((campaign / profile.STAGE_ROOT).glob(f"*/{profile.JOINT_MANIFEST_FILENAME}"))
        joint = _read(joint_manifest)["joint_sha256"]
        return self._action("vc3-stage-approve", ["stage-profile", "--campaign-dir", campaign, "--approve-joint-sha256", joint, "--approved-by", "reviewer-a"])

    def _assert_before_write(self, kind: str, failed_run: Path, redispatched_run: Path) -> None:
        (fact,) = self._facts(kind)
        self.assertEqual(fact["producer_run"]["run_dir"], str(redispatched_run))
        self.assertNotEqual(str(failed_run), str(redispatched_run))

    def _assert_after_write(self, kind: str, failed_run: Path) -> None:
        (fact,) = self._facts(kind)
        self.assertEqual(fact["producer_run"]["run_dir"], str(failed_run))

    def _exercise(self, *, prepare_before_write: bool) -> None:
        """准备批准与暂存批准各硬杀一次：``prepare_before_write`` 为真时准备批准在写事实之前、暂存批准在写事实之后，反之互换。"""

        campaign = self._vc3_ready()
        approve = self._action_after_prepare_revision(campaign)
        prepare_hook = self.FACTS_HOOK if prepare_before_write else "rename-suffix:/profile/prepare/approved/rev-0002"
        failed = self._crash_dispatch(4, approve, prepare_hook)
        if prepare_before_write:
            self.assertEqual(self._facts("evidence_approved"), [])
        else:
            self._assert_after_write("evidence_approved", failed)
            self.assertFalse((campaign / profile.PREPARED_ROOT / "rev-0002").exists())
        redispatched = self._reconcile_and_redispatch(failed, 5, approve)
        if prepare_before_write:
            self._assert_before_write("evidence_approved", failed, redispatched)
        else:
            self._assert_after_write("evidence_approved", failed)
        self.assertEqual(profile.approved_prepare_revisions(campaign), ["rev-0002"])

        # 阶段内的后续批次：后继协议对已执行过的历史后继只重放冻结许可，不因后继自己的产物误判漂移。
        stage = self._stage_approval(campaign, 6)
        stage_hook = "rename-suffix:/profile/release" if prepare_before_write else self.FACTS_HOOK
        failed = self._crash_dispatch(7, stage, stage_hook)
        if prepare_before_write:
            self._assert_after_write("profile_approved", failed)
            self.assertFalse((campaign / profile.STAGE_RESULT_RELATIVE).exists())
        else:
            self.assertEqual(self._facts("profile_approved"), [])
        redispatched = self._reconcile_and_redispatch(failed, 8, stage)
        if prepare_before_write:
            self._assert_after_write("profile_approved", failed)
        else:
            self._assert_before_write("profile_approved", failed, redispatched)
        self.assertEqual(seal_tests.store_status(self.store_root, CAMPAIGN_ID), "profile_approved")
        self.assertEqual(claude_upgrade._load_stage_result(campaign, "stage-profile")["prepared_revision"], "rev-0002")
        self.assertIn("VC-3", timing.phase_ledger_state(self.ledger_dir)["completed_phases"])
        self.assertEqual(sorted(int(path.name[:4]) for path in (campaign / "control" / "vc" / "commits").iterdir()), [2, 3, 4, 5, 6, 7, 8])
        self.assert_zero_requests()

    def test_prepare_approval_before_write_and_stage_approval_after_write_converge(self) -> None:
        self._exercise(prepare_before_write=True)

    def test_prepare_approval_after_write_and_stage_approval_before_write_converge(self) -> None:
        self._exercise(prepare_before_write=False)


class ExpiredLeaseDeadCornerTests(recovery_tests.RecoveryChainCase):
    """过期租约死角：run 写完 attempt.json、释放租约之前被强杀，seal 先固化停线收据再继续，封存后的步骤不再被它挡住。"""

    def _kill_before_lease_release(self, campaign: Path) -> int:
        """子进程跑完官方取证（attempt.json 已写），在租约会话退出（释放租约）那一刻 SIGKILL 自己。"""

        pid = os.fork()
        if pid == 0:  # pragma: no cover - 子进程
            try:
                def killing(lease: Any, *arguments: Any) -> None:
                    del lease, arguments
                    os.kill(os.getpid(), signal.SIGKILL)

                claude_upgrade.CampaignLease.__exit__ = killing  # type: ignore[method-assign]
                with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    claude_upgrade.main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
            finally:
                os._exit(99)
        _pid, status = os.waitpid(pid, 0)
        return status

    def _hold_lease_lock(self, campaign: Path) -> subprocess.Popen[str]:
        """另起一个进程持有租约锁（模拟仍在运行的执行者），持锁后在标准输出报告。"""

        script = (
            "import fcntl, os, sys, time\n"
            "descriptor = os.open(sys.argv[1], os.O_RDWR)\n"
            "fcntl.flock(descriptor, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(120)\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", script, str(campaign / claude_upgrade.CAMPAIGN_LEASE_LOCK_FILENAME)], stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        return holder

    def test_expired_lease_is_solidified_and_sealed_attempt_settles_it(self) -> None:
        campaign = self.build_recovery_campaign()
        with self.chain_patched():
            status = self._kill_before_lease_release(campaign)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        (attempt_root,) = sorted((campaign / "official" / "attempts").iterdir())
        attempt_id = attempt_root.name
        self.assertEqual(_read(attempt_root / "attempt.json")["status"], "awaiting_receipts")
        lease = claude_upgrade._read_campaign_lease(campaign)
        self.assertEqual((lease["state"], lease["attempt_id"]), ("active", attempt_id))
        stop_receipt = campaign / claude_upgrade.CAMPAIGN_LEASE_STOP_DIRECTORY / f"lease-{lease['owner_nonce']}.json"
        self.assertFalse(stop_receipt.exists())

        # 执行者仍持锁：拒绝，不写停线收据。
        holder = self._hold_lease_lock(campaign)
        with claude_upgrade._campaign_lock(campaign), self.assertRaisesRegex(ConfigurationError, "执行者仍持有租约锁"):
            claude_upgrade._require_quiescent_campaign_lease(campaign, action="seal", attempt_id=attempt_id)
        self.assertFalse(stop_receipt.exists())
        holder.kill()
        holder.wait()
        # 锁空着但租约仍在有效期内（PID 存活、心跳新鲜）：拒绝，不写停线收据。
        lease_path = campaign / claude_upgrade.CAMPAIGN_LEASE_FILENAME
        original = lease_path.read_bytes()
        fresh = dict(lease, owner_pid=os.getpid(), last_heartbeat_at_utc=claude_upgrade._utc_now())
        lease_path.write_text(json.dumps(fresh), encoding="utf-8")
        lease_path.chmod(0o600)
        with claude_upgrade._campaign_lock(campaign), self.assertRaisesRegex(ConfigurationError, "仍在有效期内"):
            claude_upgrade._require_quiescent_campaign_lease(campaign, action="seal", attempt_id=attempt_id)
        self.assertFalse(stop_receipt.exists())
        lease_path.write_bytes(original)
        lease_path.chmod(0o600)

        # 过期且锁空：别的对象（前序导入）先固化停线收据，再因绑定 attempt 尚未封存而拒绝。
        with claude_upgrade._campaign_lock(campaign), self.assertRaisesRegex(ConfigurationError, f"绑定 attempt {attempt_id}"):
            claude_upgrade._require_quiescent_campaign_lease(campaign, action="前序证据导入")
        receipt = _read(stop_receipt)
        self.assertEqual((receipt["attempt_id"], receipt["reason"]), (attempt_id, "owner_not_alive"))
        # seal 该 attempt：预览与批准都放行（停线收据重复发现只重放原文件），官方阶段封存。
        with self.chain_patched():
            code, preview, stderr = seal_tests.run_main(self.seal_argv(campaign, attempt_id))
            self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
            code, sealed, stderr = seal_tests.run_main(self.seal_argv(campaign, attempt_id, "--approve-seal-sha256", preview["review_sha256"]))
            self.assertEqual((code, sealed["status"]), (0, "complete"), stderr)
        self.assertEqual(_read(stop_receipt), receipt)
        self.assertEqual(claude_upgrade._read_campaign_lease(campaign)["state"], "active")
        # 绑定的 attempt 已封存即了结：classify、VC-3 与前序导入的静止判据放行。
        with claude_upgrade._campaign_lock(campaign):
            for action in ("classify", "VC-3", "前序证据导入"):
                claude_upgrade._require_quiescent_campaign_lease(campaign, action=action)
        self.assertEqual(sorted(path.name for path in (campaign / claude_upgrade.CAMPAIGN_LEASE_STOP_DIRECTORY).iterdir()), [stop_receipt.name])


if __name__ == "__main__":
    unittest.main()
