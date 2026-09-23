"""改造 4（staging/WAL）派发入口集成回归：正常路径、P1～P4、中间态逐点崩溃、完整性异常、根因累计。

全部用例复用 ``test_codex_upgrade.CodexUpgradeTest._vc_chain_fixture``（真实 Campaign、项目总账、
时间账本、零请求合成动作），只走真实 ``compile_and_run_vc_batch``；崩溃点用子进程 ``os._exit``
模拟 SIGKILL，故障注入只 patch 入口内部函数，不替换编译器与制品构建器。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_root_cause as root_cause
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import codex_upgrade_predispatch_stop as predispatch_stop
from tools.official_client_capture.tests import test_codex_upgrade

REPO_ROOT = Path(__file__).resolve().parents[3]
ORDER = artifacts.VC_PHASES

# 子进程驱动：按配置 patch 入口内部函数（raise／exit_before／exit_after），再跑真实入口。
DRIVER = r"""
import argparse, json, os, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
config = json.loads(sys.argv[2])
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as rehearsal
modules = {"codex_upgrade": codex_upgrade, "supervisor": supervisor, "reconciler": reconciler}
for item in config["patches"]:
    module = modules[item["module"]]
    original = getattr(module, item["name"])
    mode = item["mode"]

    def make(original, mode):
        calls = {"count": 0}

        def wrapper(*args, **kwargs):
            calls["count"] += 1
            if mode == "exit_before":
                os._exit(9)
            if mode == "fail_second" and calls["count"] == 2:
                raise codex_upgrade.ConfigurationError("injected-second-call-" + original.__name__)
            result = original(*args, **kwargs)
            if mode == "exit_after":
                os._exit(9)
            if mode == "raise_after":
                raise RuntimeError("injected-after-" + original.__name__)
            return result
        return wrapper

    setattr(module, item["name"], make(original, mode))
path_keys = {"campaign_dir", "state_dir", "predecessor_checkpoint", "action_plan"}
args = argparse.Namespace(**{k: (Path(v) if k in path_keys else v) for k, v in config["arguments"].items()})
with mock.patch.object(rehearsal, "_target_evidence_label_declaration_sha256", return_value="d" * 64), \
     mock.patch.object(supervisor.arm64_environment, "campaign_requires_runtime_egress", return_value=False):
    # 与父测试 _vc_chain_fixture 一致：纯零请求故障夹具不读取生产出口策略。
    try:
        result, returncode = codex_upgrade.compile_and_run_vc_batch(args)
        print(json.dumps({"returncode": returncode, "status": result["status"], "run_dir": result["campaign_run"]["run_dir"]}))
    except BaseException as error:  # noqa: BLE001
        print(json.dumps({"error": type(error).__name__, "message": str(error)[:800]}))
"""


class StagingDispatchTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------

    def setUp(self) -> None:
        super().setUp()
        # 复用 test_codex_upgrade 的 Campaign 夹具方法与其 setUp 的 rehearsal 声明 mock。
        self.helper = test_codex_upgrade.CodexUpgradeTest(
            "test_bound_evidence_path_accepts_legacy_attempt_relative_binding"
        )
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def _fixture(self, root: Path) -> dict[str, object]:
        return self.helper._vc_chain_fixture(root)

    def _arguments(self, fixture: dict[str, object], phase: str, sequence: int, action_plan: Path):
        return self.helper._vc_chain_arguments(fixture, phase, sequence, action_plan)

    def _plan(self, root: Path, campaign_dir: Path, phase: str, *, tag: str = "") -> Path:
        return self.helper._vc_chain_action_plan(root / f"plans{tag}", campaign_dir, phase)

    def _open_r1(self, fixture: dict[str, object]) -> None:
        """改造 2：候选级首批（VC-4）派发前先激活 r1；幂等，可重复调用。"""

        opened = codex_upgrade.open_candidate_revision(
            argparse.Namespace(campaign_dir=fixture["campaign_dir"], candidate_id="candidate-r1", initial=True, supersedes=None)
        )
        self.assertEqual(opened["revision"], 1)

    def _dispatch(self, fixture: dict[str, object], root: Path, phase: str, sequence: int, *, tag: str = ""):
        campaign_dir = fixture["campaign_dir"]
        if phase == "VC-4":
            self._open_r1(fixture)
        action_plan = self._plan(root, campaign_dir, phase, tag=tag)
        return codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, phase, sequence, action_plan))

    def _run_driver(
        self,
        fixture: dict[str, object],
        root: Path,
        phase: str,
        sequence: int,
        *,
        patches: list[dict[str, str]],
        tag: str = "driver",
        expect_exit: int | None = 9,
    ) -> dict[str, object]:
        campaign_dir = fixture["campaign_dir"]
        action_plan = self._plan(root, campaign_dir, phase, tag=tag)
        arguments = self._arguments(fixture, phase, sequence, action_plan)
        config = {
            "patches": patches,
            "arguments": {key: (str(value) if isinstance(value, Path) else value) for key, value in vars(arguments).items()},
        }
        completed = subprocess.run(
            [sys.executable, "-c", DRIVER, str(REPO_ROOT), json.dumps(config)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if expect_exit is not None:
            self.assertEqual(completed.returncode, expect_exit, completed.stderr[-2000:])
            return {}
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        return json.loads(completed.stdout.strip().splitlines()[-1])

    @staticmethod
    def _head(fixture: dict[str, object]) -> dict[str, object]:
        return project_ledger.replay_head(Path(str(fixture["ledger"])))

    @staticmethod
    def _events(fixture: dict[str, object]) -> list[tuple[str, str]]:
        return test_codex_upgrade.CodexUpgradeTest._b0_ledger_events(Path(str(fixture["timing_ledger"])))

    @staticmethod
    def _staging_dir(campaign_dir: Path, sequence: int, phase: str) -> Path:
        return campaign_dir / "control" / "vc" / "staging" / f"{sequence:04d}-{phase.lower()}"

    @staticmethod
    def _commit_path(campaign_dir: Path, sequence: int, phase: str) -> Path:
        return campaign_dir / "control" / "vc" / "commits" / f"{sequence:04d}-{phase.lower()}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _staging_runs(fixture: dict[str, object]) -> list[Path]:
        """同 Campaign 下 prepared 起动的父 run（排除 no-op 引导首批）。"""

        return [
            path
            for path in sorted(fixture["state_dir"].glob("run-*"))
            if supervisor._read_state(path).get("staging_binding") is not None
        ]

    def _wait_terminal(self, run_dir: Path, expected: set[str], *, timeout: float = 5.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._read(run_dir / "state.json")
            if state.get("state") in expected:
                return state
            time.sleep(0.05)
        self.fail(f"父 run 未进入 {sorted(expected)}：{self._read(run_dir / 'state.json').get('state')}")

    def _finish_chain(self, fixture: dict[str, object], root: Path, *, start_phase: str, start_sequence: int) -> int:
        """从给定阶段继续派发到 VC-6，返回最后一个序号。"""

        sequence = start_sequence
        for phase in ORDER[ORDER.index(start_phase):]:
            result, returncode = self._dispatch(fixture, root, phase, sequence, tag=f"-finish-{phase}")
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["status"], "stopped")
            sequence += 1
        self.assertTrue((fixture["campaign_dir"] / "control" / "vc" / "vc-6-checkpoint.json").is_file())
        return sequence - 1

    # ------------------------------------------------------------------
    # 正常路径
    # ------------------------------------------------------------------

    def test_normal_dispatch_commits_through_staging_and_never_writes_predispatch_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            head_before = self._head(fixture)["sequence"]
            result, returncode = self._dispatch(fixture, root, "VC-2", 2)
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["batch_model"], "staging")
            self.assertEqual(result["staging_attempt"], 1)
            self.assertEqual(result["orphans"], [])
            attempt_dir = self._staging_dir(campaign_dir, 2, "VC-2") / "attempt-1"
            marker = artifacts.validate_staging_prepared_marker(self._read(attempt_dir / "PREPARED"))
            commit = artifacts.validate_vc_commit(self._read(self._commit_path(campaign_dir, 2, "VC-2")))
            self.assertEqual(result["commit"], commit)
            self.assertEqual(commit["staging_attempt"], 1)
            self.assertEqual(commit["batch_sha256"], marker["batch_sha256"])
            self.assertEqual(commit["owner_nonce"], marker["owner_nonce"])
            self.assertEqual(
                commit["ledger_event_ids"],
                [
                    "vc-batch-0002-vc-2-vc-0-completed",
                    "vc-batch-0002-vc-2-vc-1-started",
                    "vc-batch-0002-vc-2-vc-1-completed",
                    "vc-batch-0002-vc-2-vc-2-started",
                ],
            )
            # 正式产物与 staging 逐字节一致；COMMIT 的 manifest_sha256 对正式清单文件。
            for name, formal in (("batch.json", "batches"), ("run-manifest.json", "run-manifests")):
                staged = attempt_dir / name
                published = campaign_dir / "control" / "vc" / formal / "0002-vc-2.json"
                self.assertEqual(staged.read_bytes(), published.read_bytes())
            self.assertEqual(
                commit["manifest_sha256"],
                supervisor._sha256(supervisor._canonical(self._read(campaign_dir / "control" / "vc" / "run-manifests" / "0002-vc-2.json"))),
            )
            run_dir = Path(result["campaign_run"]["run_dir"])
            state = supervisor._read_state(run_dir)
            self.assertEqual(state["state"], "stopped")
            self.assertEqual(state["commit_sha256"], commit["commit_sha256"])
            self.assertEqual(state["staging_binding"]["commit_path"], str(self._commit_path(campaign_dir, 2, "VC-2").resolve()))
            self.assertEqual(commit["parent_run_dir"], str(run_dir))
            self.assertFalse((campaign_dir / "control" / "vc" / "predispatch-stops").exists())
            self.assertFalse((attempt_dir / "ABORT").exists())
            self.assertEqual(self._head(fixture)["sequence"], head_before)
            # 同序号重派被拒（改造 2 起账本门先拒绝重开当前 revision 已完成阶段；
            # 更早的实现由"已有 COMMIT"拒绝），并且不会留下新的 staging attempt。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已有 COMMIT|禁止重开"):
                self._dispatch(fixture, root, "VC-2", 2, tag="-again")
            self.assertEqual([p.name for p in sorted(self._staging_dir(campaign_dir, 2, "VC-2").iterdir())], ["attempt-1"])
            # predispatch_stop.record 对 staging 模型 Campaign 拒绝（T4.7）。
            with self.assertRaisesRegex(predispatch_stop.PredispatchStopError, "staging 模型"):
                predispatch_stop.record(
                    campaign_dir=campaign_dir,
                    state_dir=fixture["state_dir"],
                    batch_path=campaign_dir / "control" / "vc" / "batches" / "0002-vc-2.json",
                    manifest_path=campaign_dir / "control" / "vc" / "run-manifests" / "0002-vc-2.json",
                    failure_kind="dispatch-before-parent-run",
                    error_type="RuntimeError",
                )
            self._finish_chain(fixture, root, start_phase="VC-3", start_sequence=3)

    # ------------------------------------------------------------------
    # P1：prepare 之后、父 run 之前失败
    # ------------------------------------------------------------------

    def _inject_prepare_crash(self):
        original = codex_upgrade.compile_vc_batch

        def crashing(arguments, **kwargs):
            original(arguments, **kwargs)
            raise RuntimeError("crash-after-prepare")

        return mock.patch.object(codex_upgrade, "compile_vc_batch", side_effect=crashing)

    def test_prepare_failure_writes_abort_accounts_root_cause_and_same_sequence_redispatch_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            head_before = self._head(fixture)["sequence"]
            events_before = self._events(fixture)
            with self._inject_prepare_crash():
                with self.assertRaisesRegex(RuntimeError, "crash-after-prepare"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-p1")
            attempt_dir = self._staging_dir(campaign_dir, 2, "VC-2") / "attempt-1"
            abort = artifacts.validate_staging_abort(self._read(attempt_dir / "ABORT"))
            self.assertEqual((abort["stage"], abort["failure_kind"], abort["error_type"]), ("prepare", "prepare-failed", "RuntimeError"))
            self.assertIsNone(abort["parent_run_dir"])
            expected_cause = root_cause.structured_root_cause(
                component="orchestrator",
                stable_error_code="staging.abandoned",
                failed_step="prepare",
                stable_dimensions={"phase": "VC-2", "stage": "prepare"},
            )
            self.assertEqual(abort["root_cause_id"], expected_cause)
            head = self._head(fixture)
            self.assertEqual(head["sequence"], head_before + 1)
            self.assertIn("staging-abort:0002:1", head["operations"])
            self.assertEqual(head["root_cause_counts"][expected_cause], 1)
            self.assertEqual(head["precise_total"], 0)
            # 序号未占：无 COMMIT、无正式文件、无父 run；账本没有新事件（阶段没有真正开始）。
            self.assertFalse(self._commit_path(campaign_dir, 2, "VC-2").exists())
            self.assertFalse((campaign_dir / "control" / "vc" / "batches" / "0002-vc-2.json").exists())
            self.assertEqual(self._staging_runs(fixture), [])
            self.assertEqual(self._events(fixture), events_before)
            # 同序号重派成功：attempt-2 提交 COMMIT；attempt-1 的 ABORT 不再重复入账。
            result, returncode = self._dispatch(fixture, root, "VC-2", 2, tag="-p1-retry")
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["staging_attempt"], 2)
            self.assertEqual(result["orphans"], [])
            commit = artifacts.validate_vc_commit(self._read(self._commit_path(campaign_dir, 2, "VC-2")))
            self.assertEqual(commit["staging_attempt"], 2)
            self.assertEqual(self._head(fixture)["root_cause_counts"][expected_cause], 1)
            self._finish_chain(fixture, root, start_phase="VC-3", start_sequence=3)

    def test_two_prepare_failures_hit_root_cause_limit_and_stop_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            with self._inject_prepare_crash():
                with self.assertRaisesRegex(RuntimeError, "crash-after-prepare"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-p1a")
                with self.assertRaisesRegex(codex_upgrade.StagingStopTheLine, "root_cause_limit"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-p1b")
            head = self._head(fixture)
            campaign_id = str(fixture["manifest"]["campaign_id"])
            self.assertEqual(head["terminal_campaigns"][campaign_id]["terminal_reason"], "root_cause_limit")
            self.assertEqual(timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))["status"], "stopped")
            self.assertTrue((self._staging_dir(campaign_dir, 2, "VC-2") / "attempt-2" / "ABORT").is_file())
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "拒绝派发|已终态"):
                self._dispatch(fixture, root, "VC-2", 2, tag="-p1c")

    # ------------------------------------------------------------------
    # P3：commit 发布步失败（账本事件已写、COMMIT 未写）
    # ------------------------------------------------------------------

    def _inject_publish_crash(self):
        original = codex_upgrade._publish_staging_file
        calls = {"count": 0}

        def crashing(source, target, campaign_dir):
            calls["count"] += 1
            if calls["count"] == 2:
                raise codex_upgrade.ConfigurationError("simulated publish failure")
            return original(source, target, campaign_dir)

        return mock.patch.object(codex_upgrade, "_publish_staging_file", side_effect=crashing)

    def test_commit_publish_failure_is_reconciled_archived_and_same_sequence_redispatch_reaches_vc6(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = Path(str(fixture["timing_ledger"]))
            head_before = self._head(fixture)["sequence"]
            with self._inject_publish_crash():
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "commit-publish 步失败.*可按同序号重新派发") as context:
                    self._dispatch(fixture, root, "VC-2", 2, tag="-p3")
            self.assertNotIsInstance(context.exception, codex_upgrade.StagingStopTheLine)
            runs = self._staging_runs(fixture)
            self.assertEqual(len(runs), 1)
            run_dir = runs[0]
            self.assertEqual(supervisor._read_state(run_dir)["state"], "aborted_prepared")
            stop = self._read(run_dir / "stop-receipt.json")
            self.assertEqual(stop["reason"], "staging-commit-failed:commit-publish")
            # ① 正式对账收据：parent-prepare-abandoned，根因 staging.commit-failed(stage=commit-publish)。
            receipt = self._read(campaign_dir / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json")
            self.assertEqual(receipt["failure_class"], "parent-prepare-abandoned")
            expected_cause = root_cause.structured_root_cause(
                component="orchestrator",
                stable_error_code="staging.commit-failed",
                failed_step="commit-publish",
                stable_dimensions={"phase": "VC-2", "stage": "commit-publish"},
            )
            self.assertEqual(receipt["root_cause"]["root_cause_id"], expected_cause)
            self.assertFalse(receipt["reservation_exists"])
            self.assertEqual(receipt["live_request_count"], 0)
            head = self._head(fixture)
            self.assertEqual(head["sequence"], head_before + 1)
            self.assertIn(f"reconcile-supervisor-run:{run_dir.name}", head["operations"])
            self.assertEqual(head["root_cause_counts"][expected_cause], 1)
            # 账本：VC-2 已 stage_started（步骤 1 已写）→ receipt_passed(redispatch-same-sequence)。
            events = self._events(fixture)
            self.assertEqual(events[-1], ("receipt_passed", f"reconcile-run-passed-{run_dir.name}"))
            summary = timing_ledger.inspect_ledger(ledger_dir)
            self.assertEqual((summary["status"], summary["active_phase"], summary["next_action"]), ("active", "VC-2", "redispatch-same-sequence"))
            # ② ABORT 绑定对账收据；③ 正式半产物（已发布的 batch）归档，序号未占。
            attempt_dir = self._staging_dir(campaign_dir, 2, "VC-2") / "attempt-1"
            abort = artifacts.validate_staging_abort(self._read(attempt_dir / "ABORT"))
            self.assertEqual((abort["stage"], abort["failure_kind"]), ("commit-publish", "commit-failed"))
            self.assertEqual(abort["parent_run_dir"], str(run_dir))
            self.assertEqual(abort["reconciliation_receipt"]["path"], f"control/reconciliation/run-{run_dir.name}/supervisor-run-reconciliation.json")
            self.assertEqual(abort["root_cause_id"], expected_cause)
            archive = campaign_dir / "control" / "vc" / "staging-aborts" / "0002-vc-2" / "attempt-1"
            self.assertTrue((archive / "batch.json").is_file())
            self.assertFalse((archive / "run-manifest.json").exists())
            self.assertFalse((campaign_dir / "control" / "vc" / "batches" / "0002-vc-2.json").exists())
            self.assertFalse(self._commit_path(campaign_dir, 2, "VC-2").exists())
            # 同序号重派：阶段已 active，不再写 started；attempt-2 提交；链继续到 VC-6。
            result, returncode = self._dispatch(fixture, root, "VC-2", 2, tag="-p3-retry")
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["staging_attempt"], 2)
            self.assertEqual(result["timing_ledger"]["events"], [])
            self.assertEqual(result["orphans"], [])
            commit = artifacts.validate_vc_commit(self._read(self._commit_path(campaign_dir, 2, "VC-2")))
            self.assertEqual(commit["ledger_event_ids"], [])
            last = self._finish_chain(fixture, root, start_phase="VC-3", start_sequence=3)
            self.assertEqual(last, 6)
            # 序号链：no-op 首批 + 5 个已提交批次；aborted_prepared 不计。
            committed = codex_upgrade._committed_vc_sequences(campaign_dir, self._read(campaign_dir / "campaign.json"))
            self.assertEqual(committed, [1, 2, 3, 4, 5, 6])
            self.assertEqual(self._events(fixture)[-1], ("stage_completed", "vc-batch-0006-vc-6-completed"))
            self.assertFalse((campaign_dir / "control" / "vc" / "predispatch-stops").exists())

    def test_two_commit_failures_at_same_step_hit_root_cause_limit(self) -> None:
        """同 step 两次 staging.commit-failed 累计达上限：第二次入口内对账即永久停线。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            with self._inject_publish_crash():
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "可按同序号重新派发"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-cf-a")
            with self._inject_publish_crash():
                with self.assertRaisesRegex(codex_upgrade.StagingStopTheLine, "root_cause_limit"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-cf-b")
            campaign_id = str(fixture["manifest"]["campaign_id"])
            head = self._head(fixture)
            self.assertEqual(head["terminal_campaigns"][campaign_id]["terminal_reason"], "root_cause_limit")
            self.assertEqual(sorted(head["root_cause_counts"].values()), [2])
            self.assertEqual(timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))["status"], "stopped")
            # 两个 attempt 都有 ABORT，且都绑定各自父 run 的对账收据。
            for attempt in (1, 2):
                abort = artifacts.validate_staging_abort(self._read(self._staging_dir(fixture["campaign_dir"], 2, "VC-2") / f"attempt-{attempt}" / "ABORT"))
                self.assertEqual(abort["stage"], "commit-publish")
                self.assertIsNotNone(abort["reconciliation_receipt"])

    def test_sequence_gap_is_rejected_before_any_staging_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            result, returncode = self._dispatch(fixture, root, "VC-2", 2)
            self.assertEqual(returncode, 0, result)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "必须连续"):
                self._dispatch(fixture, root, "VC-3", 4, tag="-gap")
            self.assertFalse(self._staging_dir(campaign_dir, 4, "VC-3").exists())
            self.assertEqual(codex_upgrade._committed_vc_sequences(campaign_dir, self._read(campaign_dir / "campaign.json")), [1, 2])

    def test_abandoned_and_commit_failed_root_causes_do_not_accumulate_together(self) -> None:
        """staging.abandoned 与 staging.commit-failed 是不同根因：各一次不触发上限。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            with self._inject_publish_crash():
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "commit-publish"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-mix-a")
            with self._inject_prepare_crash():
                with self.assertRaisesRegex(RuntimeError, "crash-after-prepare"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-mix-b")
            counts = self._head(fixture)["root_cause_counts"]
            self.assertEqual(sorted(counts.values()), [1, 1])
            result, returncode = self._dispatch(fixture, root, "VC-2", 2, tag="-mix-c")
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["staging_attempt"], 3)

    # ------------------------------------------------------------------
    # P4：COMMIT 已写、父 run 未转 running → N+1 逐字重派
    # ------------------------------------------------------------------

    def test_activation_failure_pauses_ledger_then_reconciler_allows_n_plus_one_verbatim_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = Path(str(fixture["timing_ledger"]))

            def broken_activate(self_client, commit):
                raise supervisor.SupervisorError("simulated state write failure")

            with mock.patch.object(supervisor.SupervisorClient, "activate_committed", broken_activate):
                result, returncode = self._dispatch(fixture, root, "VC-2", 2, tag="-p4")
            self.assertEqual(returncode, 1)
            self.assertEqual((result["status"], result["campaign_run"]["reason"]), ("failed", "parent-start-failed"))
            self.assertEqual(result["campaign_run"]["commit_failure"]["classification"], "committed")
            run_dir = Path(result["campaign_run"]["run_dir"])
            self.assertEqual(supervisor.classify_prepared_run(run_dir), "committed")
            commit = artifacts.validate_vc_commit(self._read(self._commit_path(campaign_dir, 2, "VC-2")))
            self.assertEqual(result["commit"], commit)
            diagnostic = supervisor.read_parent_start_failure(run_dir, supervisor._read_state(run_dir))
            self.assertEqual(diagnostic["failure_kind"], "state-write-failed")
            self.assertEqual(diagnostic["commit_sha256"], commit["commit_sha256"])
            # 账本暂停（recovery_required，根因 parent-start.failed），未对账即派发被拒。
            self.assertEqual(result["campaign_run"]["timing_closeout"]["ledger_status"], "recovery_required")
            self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "recovery_required")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "recovery_required"):
                self._dispatch(fixture, root, "VC-2", 3, tag="-p4-early")
            # 同序号重派：序号已占。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "recovery_required|已有 COMMIT"):
                self._dispatch(fixture, root, "VC-2", 2, tag="-p4-same")
            # reconciler：parent-start-failed 分支，根因 parent-start.failed，可恢复 → receipt_passed(redispatch-same-batch)。
            outcome = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(outcome["status"], "recoverable")
            expected_cause = root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="parent-start.failed",
                failed_step="commit-activate",
                stable_dimensions={"phase": "VC-2"},
            )
            self.assertEqual(outcome["root_cause"]["root_cause_id"], expected_cause)
            receipt = self._read(campaign_dir / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json")
            self.assertEqual(receipt["failure_class"], "parent-start-failed")
            self.assertEqual(receipt["run"]["staging"]["parent_start_failure"]["failure_kind"], "state-write-failed")
            summary = timing_ledger.inspect_ledger(ledger_dir)
            self.assertEqual((summary["status"], summary["active_phase"], summary["next_action"]), ("active", "VC-2", "redispatch-same-batch"))
            self.assertEqual(self._head(fixture)["root_cause_counts"][expected_cause], 1)
            # 幂等：再对账不重复入账。
            reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(self._head(fixture)["root_cause_counts"][expected_cause], 1)
            # N+1 逐字重派（同一 action plan → 九个不可变字段逐字相等）→ 成功，链继续到 VC-6。
            result, returncode = self._dispatch(fixture, root, "VC-2", 3, tag="-p4")
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["orphans"], [])
            self.assertTrue(self._commit_path(campaign_dir, 3, "VC-2").is_file())
            last = self._finish_chain(fixture, root, start_phase="VC-3", start_sequence=4)
            self.assertEqual(last, 7)
            committed = codex_upgrade._committed_vc_sequences(campaign_dir, self._read(campaign_dir / "campaign.json"))
            self.assertEqual(committed, [1, 2, 3, 4, 5, 6, 7])

    def test_n_plus_one_after_parent_start_failure_rejects_drifted_batch_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]

            def broken_activate(self_client, commit):
                raise supervisor.SupervisorError("simulated state write failure")

            with mock.patch.object(supervisor.SupervisorClient, "activate_committed", broken_activate):
                result, _returncode = self._dispatch(fixture, root, "VC-2", 2, tag="-p4")
            run_dir = Path(result["campaign_run"]["run_dir"])
            reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            # 用失败动作计划（不同 command）派发 N+1：不可变字段漂移 → 拒绝，且不产生 COMMIT。
            drifted_plan = self.helper._vc_chain_action_plan(root / "drift", campaign_dir, "VC-2", fail=True)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "漂移字段"):
                codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, "VC-2", 3, drifted_plan))
            self.assertFalse(self._commit_path(campaign_dir, 3, "VC-2").exists())
            # 父 run 创建前被拒：入口当场把 staging attempt 以 parent-run-create 中止并入账（P1 语义）。
            abort = artifacts.validate_staging_abort(self._read(self._staging_dir(campaign_dir, 3, "VC-2") / "attempt-1" / "ABORT"))
            self.assertEqual((abort["stage"], abort["failure_kind"], abort["error_type"]), ("parent-run-create", "prepare-failed", "SupervisorError"))
            self.assertIn("staging-abort:0003:1", self._head(fixture)["operations"])
            # 随后逐字重派成功，且没有遗留孤儿。
            result, returncode = self._dispatch(fixture, root, "VC-2", 3, tag="-p4-ok")
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["orphans"], [])
            self.assertEqual(result["staging_attempt"], 2)

    # ------------------------------------------------------------------
    # P2：prepared 父 run，commit 未开始，owner 崩溃（子进程 SIGKILL）
    # ------------------------------------------------------------------

    def test_prepared_orphan_is_finalized_reconciled_and_same_sequence_redispatched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            events_before = self._events(fixture)
            self._run_driver(
                fixture,
                root,
                "VC-2",
                2,
                patches=[{"module": "supervisor", "name": "_commit_prepared_run", "mode": "exit_before"}],
            )
            runs = self._staging_runs(fixture)
            self.assertEqual(len(runs), 1)
            run_dir = runs[0]
            # monitor 独立进程把 prepared 孤儿封存为 aborted_prepared(prepared-abandoned)。
            self._wait_terminal(run_dir, {"aborted_prepared"})
            self.assertEqual(self._read(run_dir / "stop-receipt.json")["reason"], "prepared-abandoned")
            self.assertEqual(self._events(fixture), events_before)  # commit 未开始：账本无事件
            result, returncode = self._dispatch(fixture, root, "VC-2", 2, tag="-p2-retry")
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["staging_attempt"], 2)
            handled = result["orphans"]
            self.assertEqual([item["kind"] for item in handled], ["parent-run"])
            self.assertEqual(handled[0]["stop_reason"], "prepared-abandoned")
            self.assertEqual(handled[0]["decision"], "recoverable")
            expected_cause = root_cause.structured_root_cause(
                component="orchestrator",
                stable_error_code="staging.abandoned",
                failed_step="parent-run",
                stable_dimensions={"phase": "VC-2", "stage": "parent-run"},
            )
            self.assertEqual(handled[0]["root_cause_id"], expected_cause)
            abort = artifacts.validate_staging_abort(self._read(self._staging_dir(campaign_dir, 2, "VC-2") / "attempt-1" / "ABORT"))
            self.assertEqual((abort["stage"], abort["failure_kind"], abort["parent_run_state"]), ("parent-run", "abandoned", "aborted_prepared"))
            self.assertIn(f"reconcile-supervisor-run:{run_dir.name}", self._head(fixture)["operations"])
            self._finish_chain(fixture, root, start_phase="VC-3", start_sequence=3)

    # ------------------------------------------------------------------
    # 中间态逐点 SIGKILL：每个边界崩溃后再跑一次入口即闭合，且无重复事件
    # ------------------------------------------------------------------

    def _assert_unique_operations(self, fixture: dict[str, object]) -> None:
        campaign_dir = fixture["campaign_dir"]
        outbox = campaign_dir / project_ledger.CAMPAIGN_LEDGER_DIR_NAME / "outbox"
        operations = [project_ledger._read_batch(path)["commit"]["operation_id"] for _n, path in project_ledger._batch_dirs(outbox)]
        self.assertEqual(len(operations), len(set(operations)), operations)
        events = self._events(fixture)
        self.assertEqual(len(events), len(set(events)), events)

    def test_intermediate_crash_points_after_prepare_failure_close_idempotently(self) -> None:
        """P1 链 A→B→C→H 的每个边界：ABORT 已写／outbox 已写／已推送 后崩溃，下次入口闭合并同序号重派。"""

        boundaries = [
            ("A->B", [{"module": "codex_upgrade", "name": "_write_staging_abort", "mode": "exit_after"}]),
            ("B->C", [{"module": "reconciler", "name": "_push_and_replay", "mode": "exit_before"}]),
            ("C->H", [{"module": "reconciler", "name": "_decide", "mode": "exit_before"}]),
        ]
        for label, exit_patches in boundaries:
            with self.subTest(boundary=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                fixture = self._fixture(root)
                campaign_dir = fixture["campaign_dir"]
                patches = [{"module": "codex_upgrade", "name": "compile_vc_batch", "mode": "raise_after"}, *exit_patches]
                self._run_driver(fixture, root, "VC-2", 2, patches=patches, tag=f"-crash-{label}")
                attempt_dir = self._staging_dir(campaign_dir, 2, "VC-2") / "attempt-1"
                self.assertTrue((attempt_dir / "ABORT").is_file())
                self.assertEqual(self._staging_runs(fixture), [])
                result, returncode = self._dispatch(fixture, root, "VC-2", 2, tag=f"-after-{label}")
                self.assertEqual(returncode, 0, result)
                self.assertEqual(result["staging_attempt"], 2)
                kinds = [item["kind"] for item in result["orphans"]]
                # A→B：outbox 未写，入口补 outbox → 推送 → 判定；B→C／C→H：outbox 已 COMMIT，
                # 治理预检的补齐器（assert_campaign_admitted）先行推送，扫描时已闭合。
                self.assertEqual(kinds, ["staging-attempt"] if label == "A->B" else [], label)
                head = self._head(fixture)
                self.assertIn("staging-abort:0002:1", head["operations"])
                self.assertEqual(sum(head["root_cause_counts"].values()), 1)
                self._assert_unique_operations(fixture)
                # 再跑一次入口（序号 3）确认闭合态不再被重复处理。
                result, returncode = self._dispatch(fixture, root, "VC-3", 3, tag=f"-next-{label}")
                self.assertEqual(returncode, 0, result)
                self.assertEqual(result["orphans"], [])

    def test_intermediate_crash_points_after_commit_failure_close_idempotently(self) -> None:
        """P3 链 D→E→F→G→H 的每个边界（含 E 内部三处）崩溃后，下次入口闭合并同序号重派。"""

        boundaries = [
            ("D->E", [{"module": "reconciler", "name": "reconcile_supervisor_run", "mode": "exit_before"}]),
            ("E:receipt-written", [{"module": "reconciler", "name": "_commit_batch", "mode": "exit_before"}]),
            ("E:outbox-written", [{"module": "reconciler", "name": "_push_and_replay", "mode": "exit_before"}]),
            ("E:pushed-no-ledger", [{"module": "reconciler", "name": "_append_ledger_event", "mode": "exit_before"}]),
            ("E->F", [{"module": "codex_upgrade", "name": "_write_staging_abort", "mode": "exit_before"}]),
            ("F->G", [{"module": "codex_upgrade", "name": "_archive_uncommitted_formal_artifacts", "mode": "exit_before"}]),
            ("G->H", [{"module": "codex_upgrade", "name": "_archive_uncommitted_formal_artifacts", "mode": "exit_after"}]),
        ]
        for label, exit_patches in boundaries:
            with self.subTest(boundary=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                fixture = self._fixture(root)
                campaign_dir = fixture["campaign_dir"]
                # 让第二次发布（run-manifest）失败：账本事件已写、batch 已发布、COMMIT 未写。
                patches = [{"module": "codex_upgrade", "name": "_publish_staging_file", "mode": "fail_second"}, *exit_patches]
                self._run_driver(fixture, root, "VC-2", 2, patches=patches, tag=f"-crash-{label}")
                runs = self._staging_runs(fixture)
                self.assertEqual(len(runs), 1, label)
                run_dir = runs[0]
                self._wait_terminal(run_dir, {"aborted_prepared"})
                result, returncode = self._dispatch(fixture, root, "VC-2", 2, tag=f"-after-{label}")
                self.assertEqual(returncode, 0, result)
                self.assertEqual(result["staging_attempt"], 2, label)
                head = self._head(fixture)
                self.assertIn(f"reconcile-supervisor-run:{run_dir.name}", head["operations"])
                self.assertEqual(sum(head["root_cause_counts"].values()), 1, label)
                abort = artifacts.validate_staging_abort(self._read(self._staging_dir(campaign_dir, 2, "VC-2") / "attempt-1" / "ABORT"))
                self.assertEqual(abort["stage"], "commit-publish", label)
                self.assertTrue((campaign_dir / "control" / "vc" / "staging-aborts" / "0002-vc-2" / "attempt-1" / "batch.json").is_file(), label)
                self._assert_unique_operations(fixture)
                events = self._events(fixture)
                self.assertEqual(events.count(("receipt_passed", f"reconcile-run-passed-{run_dir.name}")), 1, label)
                result, returncode = self._dispatch(fixture, root, "VC-3", 3, tag=f"-next-{label}")
                self.assertEqual(returncode, 0, result)
                self.assertEqual(result["orphans"], [], label)

    # ------------------------------------------------------------------
    # 完整性异常：COMMIT 存在但 nonce 不匹配 → 永久停线、序号不可重派
    # ------------------------------------------------------------------

    def test_commit_integrity_mismatch_is_permanently_stopped_and_sequence_not_redispatchable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            original = artifacts.build_vc_commit

            def tampered(**kwargs):
                kwargs["owner_nonce"] = "f" * 64
                return original(**kwargs)

            with mock.patch.object(artifacts, "build_vc_commit", side_effect=tampered):
                with self.assertRaisesRegex(codex_upgrade.StagingStopTheLine, "完整性异常"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-integrity")
            run_dir = self._staging_runs(fixture)[0]
            state = supervisor._read_state(run_dir)
            self.assertEqual(state["state"], "audit-incomplete")
            self.assertEqual(self._read(run_dir / "stop-receipt.json")["reason"], "commit-integrity-mismatch")
            self.assertEqual(supervisor.classify_prepared_run(run_dir), "integrity_mismatch")
            campaign_id = str(fixture["manifest"]["campaign_id"])
            head = self._head(fixture)
            self.assertEqual(head["terminal_campaigns"][campaign_id]["terminal_reason"], "integrity_mismatch")
            receipt = self._read(campaign_dir / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json")
            self.assertEqual(receipt["failure_class"], "commit-integrity-mismatch")
            expected_cause = root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="commit.integrity-mismatch",
                failed_step="commit-verify",
                stable_dimensions={"phase": "VC-2"},
            )
            self.assertEqual(receipt["root_cause"]["root_cause_id"], expected_cause)
            self.assertEqual(timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))["status"], "stopped")
            # 序号视为已占且不可重派；COMMIT 永不移动。
            self.assertTrue(self._commit_path(campaign_dir, 2, "VC-2").is_file())
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已终态|拒绝派发|stopped"):
                self._dispatch(fixture, root, "VC-2", 2, tag="-integrity-again")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已终态|拒绝派发|stopped"):
                self._dispatch(fixture, root, "VC-2", 3, tag="-integrity-next")

    # ------------------------------------------------------------------
    # ABORT write-once + 内容核对
    # ------------------------------------------------------------------

    def test_staging_abort_write_once_verifies_full_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt_dir = Path(directory).resolve() / "attempt-1"
            attempt_dir.mkdir(mode=0o700)
            facts = {
                "campaign_id": "abort-campaign",
                "campaign_plan_sha256": "1" * 64,
                "phase": "VC-2",
                "sequence": 2,
                "attempt": 1,
                "stage": "prepare",
                "failure_kind": "abandoned",
                "error_type": "StagingAttemptAbandoned",
                "root_cause_id": "rc1-" + "a" * 20,
                "batch_sha256": "2" * 64,
                "manifest_sha256": "3" * 64,
                "parent_run_dir": None,
                "parent_run_state": None,
                "reconciliation_receipt": None,
            }
            first = codex_upgrade._write_staging_abort(attempt_dir, **facts)
            raw_before = (attempt_dir / "ABORT").read_bytes()
            # 相同事实（只有时间不同）→ 返回既有收据，文件字节不变。
            again = codex_upgrade._write_staging_abort(attempt_dir, **facts)
            self.assertEqual(again, first)
            self.assertEqual((attempt_dir / "ABORT").read_bytes(), raw_before)
            # 任一非易变字段不同 → 失败关闭并点名漂移字段，文件仍不变。
            drifts = [
                ({"campaign_id": "other-campaign"}, "campaign_id"),
                ({"campaign_plan_sha256": "9" * 64}, "campaign_plan_sha256"),
                ({"stage": "parent-run", "failure_kind": "interrupted"}, "failure_kind、stage"),
                ({"root_cause_id": "rc1-" + "b" * 20}, "root_cause_id"),
                ({"reconciliation_receipt": {"path": "control/reconciliation/run-x/supervisor-run-reconciliation.json", "sha256": "4" * 64}}, "reconciliation_receipt"),
                ({"parent_run_dir": "/srv/state/run-x", "parent_run_state": "aborted_prepared"}, "parent_run_dir、parent_run_state"),
                ({"batch_sha256": None}, "batch_sha256"),
                ({"error_type": "RuntimeError"}, "error_type"),
            ]
            for patch, expected in drifts:
                with self.subTest(patch=patch):
                    with self.assertRaisesRegex(codex_upgrade.ConfigurationError, f"漂移字段：{expected}$"):
                        codex_upgrade._write_staging_abort(attempt_dir, **{**facts, **patch})
                    self.assertEqual((attempt_dir / "ABORT").read_bytes(), raw_before)
            # attempt 身份不同也是漂移（sequence／attempt／phase 参与核对）。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "漂移字段：attempt|漂移字段：staging_attempt"):
                codex_upgrade._write_staging_abort(attempt_dir, **{**facts, "attempt": 2})

    # ------------------------------------------------------------------
    # T4.8：账本对同序号重派的幂等
    # ------------------------------------------------------------------

    def test_batch_events_are_empty_after_same_sequence_receipt_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = Path(str(fixture["timing_ledger"]))
            with self._inject_publish_crash():
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "commit-publish"):
                    self._dispatch(fixture, root, "VC-2", 2, tag="-t48")
            summary = timing_ledger.inspect_ledger(ledger_dir)
            self.assertEqual(summary["next_action"], "redispatch-same-sequence")
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            plan = codex_upgrade._vc_campaign_plan(campaign_dir, manifest)
            head, events = codex_upgrade._timing_ledger_batch_events(campaign_dir, manifest, plan, phase="VC-2", ledger_dir=ledger_dir)
            self.assertEqual(events, [])
            self.assertEqual(head, summary["head_sequence"])


if __name__ == "__main__":
    unittest.main()
