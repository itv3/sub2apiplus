"""Claude 原子批次入口（Claude 升级控制面设计 B3-01）：VC-2～VC-6 经 ``compile-and-run-vc-batch`` 派发。

全部零请求、离线。验收链（``AtomicBatchChainTests``）不打替身：批次动作在父监督器的子进程里真实执行，测试进程里的受管树
替身到不了子进程，所以这一组的 Campaign、Store 与部署收据一律按真实工作树的工具身份建立（与生产一致）。

* VC-2：首批零请求派发（no-op 首批由共享流程代跑成父 run 历史）、COMMIT 与账本阶段事件齐全 → 修订批次 → 批准批次在
  Store 分类事实写入之后、批准目录发布之前被硬杀（``usercustomize`` 只在该动作的子进程里注入）→ 账本进入阶段审核、
  对账前 N+1 被拒 → ``reconcile-supervisor-run`` 按阶段幂等重派合同写证明 → N+1 逐字重派收敛：VC-2 checkpoint 与阶段结果
  写出、``stage_completed``、Store 分类事实仍只有一条且保留第一次的 producer_run（动作结果 WAL 第 ① 步）。之后再派发 VC-2
  被拒。
* VC-3：准备草案 → 修订 → 准备批准 → 暂存预览 → 暂存批准五个批次，Store 到 ``profile_approved``、VC-3 checkpoint、
  ``stage-profile`` 阶段结果可读、阶段重派证明按批准状态判定。
* VC-4（B3-04）：没有候选 revision 时派发前拒绝、不留 staging；``revision-open --initial`` 之后两个批次——
  ``plan-candidate-gates``（在候选源码副本上重新生成计划，与提交内计划逐字一致）与 ``record-candidate-build``（假 docker／go
  在 PATH 最前，子进程里的构建复算落到它们）——Store 到 ``candidate_sealed``、VC-4 checkpoint 写出、阶段完成登记；VC-4 的
  阶段幂等重派证明：构建动作可逐字重派并绑定 checkpoint，计划动作输出已存在即拒绝。
* 接口与对照：checkpoint 坐标与重放（含 r≥2 与 VC-5 重开）与 Codex 逐项相同；队列清单除合同名与 ``client`` 外与 Codex 的
  staging 清单相同、重派身份（12 字段＋动作三元组）相等；``vc_control`` 语义；阶段结果读取；阶段幂等重派证明的形态闭合与
  schema；父监督器上下文的暂停点退出码与控制面命令拒绝。
"""

from __future__ import annotations

import collections
import contextlib
import io
import json
import os
import site
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_batch as batch
from tools.official_client_capture import claude_upgrade_classify as classify
from tools.official_client_capture import claude_upgrade_profile as profile
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_candidate_build_fixture as build_fixture
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests import runtime_egress_fixtures
from tools.official_client_capture.tests import test_claude_upgrade_classify as classify_tests
from tools.official_client_capture.tests import test_claude_upgrade_profile as profile_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
from tools.official_client_control.store import ControlStore

CAMPAIGN_ID = classify_tests.CAMPAIGN_ID
BASELINE = classify_tests.BASELINE
TARGET = classify_tests.TARGET
FIRST_BATCH_COMPILED_AT = "2026-10-07T00:10:00Z"
FIRST_BATCH_START_BY = "2026-10-07T00:11:00Z"
CRASH_ACTION_ENV = "CLAUDE_B301_TEST_CRASH_ACTION"
CRASH_MARKER_ENV = "CLAUDE_B301_TEST_CRASH_MARKER"
CRASH_SUFFIX_ENV = "CLAUDE_B301_TEST_CRASH_RENAME_SUFFIX"
# 只在指定动作的子进程里生效的崩溃注入：目标目录改名发布那一刻硬退出（不执行任何 finally），只触发一次。
USERCUSTOMIZE = f'''"""B3-01 测试注入（不进受管树）：父监督器指定动作的子进程在发布目标目录前硬退出一次。"""
import os

_ACTION = os.environ.get("{CRASH_ACTION_ENV}")
if _ACTION and os.environ.get("{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV}") == _ACTION:
    _MARKER = os.environ["{CRASH_MARKER_ENV}"]
    _SUFFIX = os.environ["{CRASH_SUFFIX_ENV}"]
    _RENAME = os.rename

    def _crash_before_publish(source, destination, *arguments, **options):
        if str(destination).endswith(_SUFFIX) and not os.path.exists(_MARKER):
            with open(_MARKER, "w", encoding="utf-8") as handle:
                handle.write("crashed\\n")
            os._exit(17)
        return _RENAME(source, destination, *arguments, **options)

    os.rename = _crash_before_publish
'''


def run_main(argv: list[str]) -> tuple[int, dict | None, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main([str(item) for item in argv])
    text = stdout.getvalue()
    return code, (json.loads(text) if text.strip() else None), stderr.getvalue()


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _plan(campaign_id: str = "claude-code-2_1_285-parity") -> dict[str, Any]:
    return vc_artifacts.build_campaign_plan(
        campaign_id=campaign_id,
        campaign_mode="formal",
        campaign_purpose="validation_only",
        baseline_version=BASELINE,
        target_version=TARGET,
        created_at_utc="2026-10-07T00:00:00Z",
        original_deadline_at_utc="2026-12-31T00:00:00Z",
        timing_checkpoint_sha256="1" * 64,
        arm64_environment_sha256="2" * 64,
        job_rehearsal_sha256="3" * 64,
        p0_gate_sha256="4" * 64,
    )


# ---------------------------------------------------------------------------
# 接口与对照（不起子进程）
# ---------------------------------------------------------------------------


class VcCheckpointParityTests(unittest.TestCase):
    """checkpoint 坐标与逐级重放：同一目录布局上 Claude 与 Codex 逐项相同（r1、r≥2、VC-5 重开）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.campaign = Path(directory.name).resolve() / "campaign"
        self.campaign.mkdir(mode=0o700)
        self.plan = _plan()
        _write_json(self.campaign / "control" / "vc" / "campaign-plan.json", self.plan)
        previous: tuple[Path, dict[str, Any]] | None = None
        for phase in vc_artifacts.VC_PHASES:
            previous = self.write_checkpoint(phase, None, previous)
        vc3 = (claude_upgrade_vc.vc_checkpoint_path(self.campaign, "VC-3"), _read(claude_upgrade_vc.vc_checkpoint_path(self.campaign, "VC-3")))
        previous = vc3
        for phase in ("VC-4", "VC-5", "VC-6"):
            previous = self.write_checkpoint(phase, 2, previous)

    def write_checkpoint(self, phase: str, revision: int | None, previous: tuple[Path, dict[str, Any]] | None) -> tuple[Path, dict[str, Any]]:
        stage = _write_json(self.campaign / "stages" / f"{phase.lower()}-r{revision or 1}.json", {"phase": phase, "revision": revision})
        checkpoint = vc_artifacts.build_vc_checkpoint(
            campaign_plan=self.plan,
            phase=phase,
            status="complete",
            predecessor_checkpoint=(
                None if previous is None else claude_upgrade_vc.vc_checkpoint_reference(self.campaign, previous[0], previous[1])
            ),
            stage_receipt={"path": stage.relative_to(self.campaign).as_posix(), "sha256": claude_upgrade.file_sha256(stage)},
            completed_at_utc="2026-10-07T01:00:00Z",
            execute_item_ids=[],
            reuse_item_ids=[],
            live_request_count=0,
            scanned_bytes=0,
        )
        path = claude_upgrade_vc.vc_checkpoint_path(self.campaign, phase, revision=revision)
        _write_json(path, checkpoint)
        return path, checkpoint

    def test_paths_and_replay_match_codex(self) -> None:
        for phase in vc_artifacts.VC_PHASES:
            for revision in (None, 1, 2):
                with self.subTest(phase=phase, revision=revision):
                    claude_path = claude_upgrade._vc_checkpoint_path(self.campaign, phase, revision=revision)
                    self.assertEqual(claude_path, codex_upgrade._vc_checkpoint_path(self.campaign, phase, revision=revision))
                    self.assertEqual(
                        claude_upgrade._replay_vc_checkpoint(self.campaign, self.plan, phase, revision=revision),
                        codex_upgrade._replay_vc_checkpoint(self.campaign, self.plan, phase, revision=revision),
                    )
        self.assertEqual(
            claude_upgrade._vc_checkpoint_path(self.campaign, "VC-6", revision=2),
            self.campaign / "control" / "vc" / "revisions" / "r2" / "vc-6-checkpoint.json",
        )
        path, checkpoint = claude_upgrade._replay_vc_checkpoint(self.campaign, self.plan, "VC-5", revision=2)
        self.assertEqual(
            claude_upgrade._vc_checkpoint_reference(self.campaign, path, checkpoint),
            codex_upgrade._vc_checkpoint_reference(self.campaign, path, checkpoint),
        )
        with self.assertRaisesRegex(ConfigurationError, "候选 revision 必须是正整数"):
            claude_upgrade._vc_checkpoint_path(self.campaign, "VC-4", revision=0)
        with self.assertRaisesRegex(ConfigurationError, "未知 VC 阶段"):
            claude_upgrade._vc_checkpoint_path(self.campaign, "VC-7")

    def test_replay_fails_closed_like_codex(self) -> None:
        stage = self.campaign / "stages" / "vc-2-r1.json"
        stage.write_text('{"phase": "VC-2", "tampered": true}\n', encoding="utf-8")
        for orchestrator in (claude_upgrade, codex_upgrade):
            with self.subTest(orchestrator=orchestrator.__name__), self.assertRaisesRegex(ConfigurationError, "VC-2 checkpoint 的阶段收据摘要漂移"):
                orchestrator._replay_vc_checkpoint(self.campaign, self.plan, "VC-4", revision=2)
        claude_upgrade_vc.vc_checkpoint_path(self.campaign, "VC-5", revision=2).unlink()
        for orchestrator in (claude_upgrade, codex_upgrade):
            with self.subTest(orchestrator=orchestrator.__name__), self.assertRaisesRegex(ConfigurationError, "VC-5 checkpoint 缺失"):
                orchestrator._replay_vc_checkpoint(self.campaign, self.plan, "VC-6", revision=2)

    def test_vc5_reopen_follows_ledger_summary(self) -> None:
        with mock.patch.object(claude_upgrade_vc, "vc5_reopened_baseline", return_value=3), mock.patch.object(
            codex_upgrade, "_vc5_reopened_baseline", return_value=3
        ):
            for revision in (1, 2):
                with self.subTest(revision=revision):
                    path = claude_upgrade._vc_checkpoint_path(self.campaign, "VC-5", revision=revision)
                    self.assertEqual(path, codex_upgrade._vc_checkpoint_path(self.campaign, "VC-5", revision=revision))
                    self.assertEqual(path.parent.name, "reopen-b3")
        summary = {"current_revision": 2, "vc5_reopened_baseline": 3}
        with mock.patch.object(claude_upgrade_vc, "campaign_ledger_summary", return_value=summary):
            self.assertEqual(claude_upgrade_vc.vc5_reopened_baseline(self.campaign, 2), 3)
            self.assertIsNone(claude_upgrade_vc.vc5_reopened_baseline(self.campaign, 1))
            self.assertEqual(claude_upgrade_vc.vc5_reopened_baseline(self.campaign), 3)
        with mock.patch.object(claude_upgrade_vc, "campaign_ledger_summary", return_value={"vc5_reopened_baseline": True}):
            with self.assertRaisesRegex(ConfigurationError, "vc5_reopened_baseline 非法"):
                claude_upgrade_vc.vc5_reopened_baseline(self.campaign)
        # 没有总计划（因而没有账本绑定）时不读账本。
        self.assertIsNone(claude_upgrade_vc.campaign_ledger_summary(self.campaign))


class RunManifestTests(unittest.TestCase):
    """批次 → 父监督器队列清单：合同 official-client-campaign-run/v1、client=claude，其余字段与 Codex staging 清单相同。"""

    def _reference(self, phase: str) -> dict[str, str]:
        return {"path": f"control/vc/{phase.lower()}-checkpoint.json", "sha256": "5" * 64, "phase": phase, "checkpoint_sha256": "6" * 64}

    def _batch(self, phase: str, sequence: int, **binding: Any) -> dict[str, Any]:
        item = f"{phase.lower()}-item"
        return vc_artifacts.build_vc_batch(
            campaign_plan=_plan(),
            phase=phase,
            sequence=sequence,
            predecessor_checkpoint=self._reference(vc_artifacts.VC_PHASES[vc_artifacts.VC_PHASES.index(phase) - 1]),
            execute_item_ids=[item],
            reuse_item_ids=["reused-item"],
            actions=[batch.managed_action(item, f"{phase}:synthetic", ["status", "--campaign-dir", "/tmp/c"], item_ids=[item])],
            compiled_at_utc="2026-10-07T02:00:00Z",
            must_start_by_utc="2026-10-07T02:01:00Z",
            **binding,
        )

    def test_campaign_and_candidate_phases_match_codex_except_contract(self) -> None:
        digests = {field: "7" * 64 for field in vc_artifacts.EVALUATOR_DIGEST_FIELDS}
        cases = {
            "VC-2": self._batch("VC-2", 2),
            "VC-4": self._batch("VC-4", 5, candidate_revision=2, candidate_id="cand-a"),
            "VC-5": self._batch("VC-5", 6, candidate_revision=2, candidate_id="cand-a", evaluator_digests=digests),
        }
        for phase, item in cases.items():
            with self.subTest(phase=phase):
                claude = claude_upgrade._vc_run_manifest_from_batch(item, batch_model="staging")
                codex = codex_upgrade._vc_run_manifest_from_batch(item, batch_model="staging")
                self.assertEqual((claude["schema_version"], claude["client"]), (supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "claude"))
                self.assertEqual(supervisor.run_manifest_client(claude), "claude")
                self.assertEqual(
                    {key: value for key, value in claude.items() if key not in {"schema_version", "client"}},
                    {key: value for key, value in codex.items() if key != "schema_version"},
                )
                # 批次身份 12 字段与动作三元组：两客户端逐字相同（重派协议同一判据）。
                identity = supervisor._redispatch_identity(claude)
                self.assertEqual(identity, supervisor._redispatch_identity(codex))
                self.assertEqual(set(supervisor.REDISPATCH_IDENTITY_FIELDS) <= set(identity), True)
        self.assertEqual((cases["VC-4"]["candidate_revision"], claude_upgrade._vc_run_manifest_from_batch(cases["VC-4"], batch_model="staging")["candidate_id"]), (2, "cand-a"))
        vc5 = claude_upgrade._vc_run_manifest_from_batch(cases["VC-5"], batch_model="staging")
        self.assertEqual((vc5["evaluation_baseline"], vc5["evaluator_digests"]), (None, digests))
        with self.assertRaisesRegex(ConfigurationError, "只使用 staging 批次模型"):
            claude_upgrade._vc_run_manifest_from_batch(cases["VC-2"])


class CampaignRunContextTests(unittest.TestCase):
    """父监督器上下文：草案、修订与预览的暂停点退出 0；批次控制面命令拒绝。"""

    def test_pause_statuses_and_control_plane_guard(self) -> None:
        self.assertEqual(batch.CONTROL_PLANE_COMMANDS, frozenset(claude_upgrade.SHARED_SUBCOMMANDS) | {"tool-evolution"})
        for status in sorted(batch.CAMPAIGN_RUN_PAUSE_STATUSES):
            with self.subTest(status=status):
                self.assertEqual(batch.campaign_run_aware_exit_code({"status": status}, 2), 2)
                with mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}):
                    self.assertEqual(batch.campaign_run_aware_exit_code({"status": status}, 2), 0)
        with mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}):
            self.assertEqual(batch.campaign_run_aware_exit_code({"status": "complete"}, 0), 0)
            self.assertEqual(batch.campaign_run_aware_exit_code({"status": "failed"}, 2), 2)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                code, result, stderr = run_main(
                    [
                        "compile-and-run-vc-batch", "--campaign-dir", root / "c", "--phase", "VC-2", "--sequence", "2",
                        "--predecessor-checkpoint", root / "p.json", "--action-plan", root / "a.json", "--state-dir", root / "s",
                    ]
                )
                self.assertEqual((code, result), (1, None))
                self.assertIn("禁止由 campaign-run 动作派发", stderr)
                self.assertEqual(sorted(path.name for path in root.iterdir()), [])
        self.assertIsNone(batch.reject_control_plane_in_campaign_run("compile-and-run-vc-batch"))

    def test_managed_action_plan_is_sorted_and_validated(self) -> None:
        later = batch.managed_action("vc3-02-stage-preview", "VC-3:stage-preview", ["stage-profile", "--campaign-dir", "/c"], item_ids=["b"])
        earlier = batch.managed_action("vc3-01-prepare-approve", "VC-3:prepare-approve", ["prepare-profile", "--campaign-dir", "/c"], item_ids=["a"])
        plan = batch.action_plan([later, earlier], phase="VC-3", reuse_item_ids=["r"])
        self.assertEqual([action["action_id"] for action in plan["actions"]], ["vc3-01-prepare-approve", "vc3-02-stage-preview"])
        self.assertEqual((plan["execute_item_ids"], plan["reuse_item_ids"]), (["a", "b"], ["r"]))
        self.assertEqual(earlier["command"][:2], [batch.sys.executable, str(batch.ORCHESTRATOR_SCRIPT)])
        with self.assertRaisesRegex(ConfigurationError, "动作计划不合法"):
            batch.action_plan([earlier], phase="VC-3", reuse_item_ids=["a"])


# ---------------------------------------------------------------------------
# 进程内夹具上的读侧与证明（受管树替身，不起子进程）
# ---------------------------------------------------------------------------


class _VC2Mixin:
    """在分类夹具上走完 VC-2 批准（直接 CLI），返回 (Campaign, 修订输出)。"""

    def approve_vc2_directly(self, campaign: Path, **resolution: Any) -> dict[str, Any]:
        draft = self.draft(campaign)
        docs = self.resolved_documents(Path(draft["draft_dir"]), **resolution)
        if resolution:
            for row in docs["profile_patches"]["rule_patches"]:
                row["profile_paths"] = list(profile_tests.PLANNED[row["spec_id"]])
        directory = self.write_documents(docs, self.root / "vc2-resolved")
        code, revised, stderr = self.revise(campaign, draft["revision"], directory)
        self.assertEqual((code, revised["gate"]["reasons"]), (2, []), stderr)
        code, approved, stderr = run_main(self.approve_argv(campaign, revised["revision"], revised["review_sha256"]))
        self.assertEqual((code, approved["status"]), (0, "complete"), stderr)
        return revised


class VcControlTests(classify_tests._ClassifyCase):
    """``vc_control`` 语义：字段闭集、总计划身份与 staging、VC-0 checkpoint 坐标、首批两件的确定性编译关系。"""

    def campaign_with_first_batch(self) -> tuple[Path, dict[str, Any]]:
        campaign = self.build_campaign()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        plan = claude_upgrade_vc.vc_campaign_plan(campaign, manifest)
        first = claude_upgrade_vc.write_first_formal_batch(
            campaign, plan=plan, execute_item_ids=[], reuse_item_ids=["official-a", "official-b"], actions=[],
            compiled_at_utc=FIRST_BATCH_COMPILED_AT, must_start_by_utc=FIRST_BATCH_START_BY,
        )
        vc0 = claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-0")
        control = {**manifest["vc_control"], "vc0_checkpoint": claude_upgrade_vc.vc_control_binding(campaign, vc0), **first}
        return campaign, seal_tests.rewrite_manifest(campaign, vc_control=control)

    def test_first_batch_chain_and_refusals(self) -> None:
        campaign, manifest = self.campaign_with_first_batch()
        loaded = claude_upgrade.load_campaign_manifest(campaign)
        self.assertEqual(set(loaded["vc_control"]), set(claude_upgrade_vc.VC_CONTROL_FIELDS))
        run = _read(campaign / loaded["vc_control"]["first_campaign_run_manifest"]["path"])
        self.assertEqual((run["schema_version"], run["client"], run["batch_sequence"], run["no_op"]), (supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "claude", 1, True))
        with self.assertRaisesRegex(ConfigurationError, "不可变文件已经存在"):
            claude_upgrade_vc.write_first_formal_batch(
                campaign, plan=claude_upgrade._vc_campaign_plan(campaign, loaded), execute_item_ids=[], reuse_item_ids=["official-a"],
                actions=[], compiled_at_utc=FIRST_BATCH_COMPILED_AT, must_start_by_utc=FIRST_BATCH_START_BY,
            )

        def refused(pattern: str, **control: Any) -> None:
            seal_tests.rewrite_manifest(campaign, vc_control=control)
            try:
                with self.assertRaisesRegex(ConfigurationError, pattern):
                    claude_upgrade.load_campaign_manifest(campaign)
            finally:
                seal_tests.rewrite_manifest(campaign, vc_control=manifest["vc_control"])

        good = manifest["vc_control"]
        refused("未登记字段", **good, extra=None)
        refused("必须同时绑定", **{key: value for key, value in good.items() if key != "first_campaign_run_manifest"})
        refused("没有指向规范的 VC-0 checkpoint", **{**good, "vc0_checkpoint": good["first_formal_batch"]})
        # 队列清单不是首批的确定性编译（改了一个字段、绑定同步更新）：拒绝。
        tampered = dict(run, reuse_items=["official-a"])
        forged = _write_json(campaign / "control" / "vc" / "forged-run-manifest.json", tampered)
        refused("不是由首批确定性编译", **{**good, "first_campaign_run_manifest": claude_upgrade_vc.vc_control_binding(campaign, forged)})
        # legacy 批次模型的总计划：拒绝。
        legacy = vc_artifacts.build_campaign_plan(
            campaign_id=CAMPAIGN_ID, campaign_mode="formal", campaign_purpose="validation_only", baseline_version=BASELINE,
            target_version=TARGET, created_at_utc="2026-10-07T00:00:00Z", original_deadline_at_utc="2026-12-31T00:00:00Z",
            timing_checkpoint_sha256="1" * 64, arm64_environment_sha256="2" * 64, job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64, batch_model=None,
        )
        legacy_path = _write_json(campaign / "control" / "vc" / "legacy-plan.json", legacy)
        refused("必须声明 staging 批次模型", campaign_plan=claude_upgrade_vc.vc_control_binding(campaign, legacy_path))
        self.assertEqual(claude_upgrade.load_campaign_manifest(campaign)["vc_control"], good)


class StageResultAndReplayTests(_VC2Mixin, classify_tests._ClassifyCase):
    """``_load_stage_result`` 的 classify 分支与 VC-2 阶段幂等重派证明（只读判定，不执行命令）。"""

    def action(self, action_id: str, argv: list[Any]) -> dict[str, Any]:
        return batch.managed_action(action_id, f"VC-2:{action_id}", argv, item_ids=[action_id])

    def facts(self, campaign: Path, phase: str, *actions: dict[str, Any]) -> dict[str, Any]:
        return claude_upgrade._campaign_stage_replay_facts(campaign, {"phase": phase, "actions": list(actions)})

    def test_classify_stage_result_requires_bound_checkpoint(self) -> None:
        campaign = self.build_campaign()
        with self.assertRaisesRegex(ConfigurationError, "阶段尚未封存：classify"):
            claude_upgrade._load_stage_result(campaign, "classify")
        revised = self.approve_vc2_directly(campaign)
        stage = claude_upgrade._load_stage_result(campaign, "classification")
        self.assertEqual((stage["revision"], stage["review_sha256"]), (revised["revision"], revised["review_sha256"]))
        with self.assertRaisesRegex(ConfigurationError, "不接受 candidate-id"):
            claude_upgrade._load_stage_result(campaign, "classify", "c1")
        checkpoint = claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-2")
        parked = checkpoint.with_name("parked.json")
        checkpoint.rename(parked)
        with self.assertRaisesRegex(ConfigurationError, "尚未封存完整"):
            claude_upgrade._load_stage_result(campaign, "classify")
        parked.rename(checkpoint)
        with self.assertRaisesRegex(ConfigurationError, "阶段尚未封存：stage-profile"):
            claude_upgrade._load_stage_result(campaign, "stage-profile")

    def test_vc2_replay_contract(self) -> None:
        campaign = self.build_campaign()
        candidates = ["classify", "--campaign-dir", campaign, "--candidates", self.candidates_path(campaign)]
        for phase, reason in (("VC-1", batch.VC1_REPLAY_REASON), ("VC-4", "VC-4 的动作 classify 没有阶段幂等合同"), ("VC-5", "VC-5 没有阶段幂等重派合同")):
            with self.subTest(phase=phase):
                result = self.facts(campaign, phase, self.action("x", candidates))
                self.assertEqual(result, {"phase": phase, "allowed": False, "actions": [], "reasons": [reason]})
        draft = self.facts(campaign, "VC-2", self.action("vc2-classify-draft", candidates))
        self.assertTrue(draft["allowed"], draft)
        self.assertEqual(set(draft), {"phase", "allowed", "actions", "reasons"})
        (only,) = draft["actions"]
        self.assertEqual(set(only), {"action_id", "command", "inputs", "outputs"})
        self.assertEqual(only["inputs"], {"--candidates": claude_upgrade.file_sha256(self.candidates_path(campaign))})
        self.assertNotIn("checkpoint", only["outputs"])
        # 形态不闭合：别的解释器、包装脚本、未登记旗标、重复旗标、别的 Campaign、未知子命令。
        other_python = self.root / "python3"
        other_python.symlink_to("/bin/sh")
        shapes = {
            "别的解释器": ([str(other_python), str(batch.ORCHESTRATOR_SCRIPT), *map(str, candidates)], "不是当前受管 Python"),
            "包装脚本": ([batch.sys.executable, "-c", "pass", *map(str, candidates)], "不是受管 claude_upgrade 的直接调用"),
            "经 env 转调": (["/usr/bin/env", "python3", str(batch.ORCHESTRATOR_SCRIPT), *map(str, candidates)], "不是受管 claude_upgrade"),
        }
        for label, (command, pattern) in shapes.items():
            with self.subTest(shape=label):
                result = self.facts(campaign, "VC-2", {"action_id": "a", "command": command})
                self.assertFalse(result["allowed"])
                self.assertRegex(result["reasons"][0], pattern)
        module_form = self.facts(
            campaign, "VC-2", {"action_id": "m", "command": [batch.sys.executable, "-m", batch.ORCHESTRATOR_MODULE, *map(str, candidates)]}
        )
        self.assertTrue(module_form["allowed"], module_form)
        for label, argv, pattern in (
            ("未登记旗标", [*candidates, "--approved-by", "x"], "没有精确闭合"),
            ("重复旗标", [*candidates, "--candidates", self.candidates_path(campaign)], "重复"),
            ("别的 Campaign", ["classify", "--campaign-dir", self.root, "--candidates", self.candidates_path(campaign)], "不是本 Campaign"),
            ("未知子命令", ["stage-profile", "--campaign-dir", campaign], "没有阶段幂等合同"),
            ("参数不成对", [*candidates, "--dangling"], "不是成对"),
        ):
            with self.subTest(argv=label):
                result = self.facts(campaign, "VC-2", self.action("a", argv))
                self.assertFalse(result["allowed"])
                self.assertRegex(result["reasons"][0], pattern)

        revised = self.approve_vc2_directly(campaign)
        approve = self.action("vc2-classify-approve", self.approve_argv(campaign, revised["revision"], revised["review_sha256"]))
        replay = self.facts(campaign, "VC-2", approve)
        self.assertTrue(replay["allowed"], replay)
        checkpoint = claude_upgrade.file_sha256(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-2"))
        outputs = replay["actions"][0]["outputs"]
        self.assertEqual(outputs["checkpoint"], checkpoint)
        self.assertEqual(outputs["classification/result.json"], claude_upgrade.file_sha256(campaign / classify.STAGE_RESULT_RELATIVE))
        self.assertIn("control-store/rule_classification_recorded", outputs)
        self.assertEqual(set(replay["actions"][0]["inputs"]), {f"revision:{revised['revision']}"})
        # 证明按共享对账器的构造器落盘时满足 codex-upgrade-stage-replay/v1 schema。
        proof = reconciler.build_stage_replay_proof(
            campaign_id=CAMPAIGN_ID, run_id="run-x", review_event_id="review", review_root_cause_id="rc",
            reconciliation_receipt_sha256="8" * 64, commit_sha256="9" * 64, next_action="redispatch-same-batch", replay=replay,
        )
        schema = json.loads(Path(codex_upgrade.__file__).with_name("codex_upgrade_stage_replay.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(proof), set(schema["required"]))
        for action in proof["actions"]:
            self.assertEqual(set(action), set(schema["properties"]["actions"]["items"]["required"]))
            self.assertTrue(all(len(value) == 64 for value in [*action["inputs"].values(), *action["outputs"].values()]))
        # 批准之后：草案、修订与批准别的 revision 必然被拒绝，留在审核。
        for label, argv, pattern in (
            ("草案", candidates, "分类已批准"),
            ("别的 revision", self.approve_argv(campaign, "rev-0001", revised["review_sha256"]), "分类已批准 revision rev-0002"),
            ("别的摘要", self.approve_argv(campaign, revised["revision"], "0" * 64), "分类已批准 revision"),
        ):
            with self.subTest(after=label):
                result = self.facts(campaign, "VC-2", self.action("a", argv))
                self.assertFalse(result["allowed"])
                self.assertRegex(result["reasons"][0], pattern)
        # 批准目录被改动：重派的批准会被拒绝覆盖。
        approved = campaign / classify.APPROVED_ROOT / "scenarios.json"
        approved.chmod(0o600)
        approved.write_text("{}\n", encoding="utf-8")
        result = self.facts(campaign, "VC-2", approve)
        self.assertFalse(result["allowed"])
        self.assertIn("不一致", result["reasons"][0])


# ---------------------------------------------------------------------------
# 验收链：真实子进程动作（真实工作树的工具身份）
# ---------------------------------------------------------------------------


class _RealTreeBatchCase(_VC2Mixin, profile_tests._ProfileCase):
    """批次动作在父监督器子进程里执行：不启用受管树替身，Store、总计划与部署收据都按真实工作树的工具身份建立。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool_identity = claude_upgrade._tool_identity(include_git=False)

    @classmethod
    def tearDownClass(cls) -> None:
        pass

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        (self.data / "evidence" / "campaigns").mkdir(parents=True, mode=0o700)
        # 零请求离线夹具：父 run 不要求运行时出口准入（动作本身不发请求）。
        egress = runtime_egress_fixtures.offline_campaign_egress()
        egress.__enter__()
        self.addCleanup(egress.__exit__, None, None, None)

    def build_batch_campaign(self) -> Path:
        """分类夹具（VC-0／VC-1 已封存，VC-1 为只读复用前序证据的形态：本 Campaign 没有官方 attempt）＋ VC-0 交接链首批（零请求
        no-op，复用全部官方作业）＋ Claude 计时账本（VC-0 完成）＋ 项目总账注册 ＋ 当前工具身份的受管部署收据 ＋ 规范坐标的
        监督器状态目录。"""

        campaign = self.build_campaign(official_reuse=True)
        # 只读复用形态：VC-1 证据（中继清单与 Wire 证据）都在前序 Campaign 里，本 Campaign 没有官方 attempt 目录；
        # 对账器会按正式 attempt 读这里每个目录的原子预约收据，夹具不得留下半个 attempt。
        self.assertFalse((campaign / "official" / "attempts").exists(), "只读复用形态的 Campaign 不得出现官方 attempt 目录")
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        plan = claude_upgrade_vc.vc_campaign_plan(campaign, manifest)
        official = sorted(job["id"] for job in classify_tests._scenarios(TARGET)["capture_jobs"])
        first = claude_upgrade_vc.write_first_formal_batch(
            campaign, plan=plan, execute_item_ids=[], reuse_item_ids=official, actions=[],
            compiled_at_utc=FIRST_BATCH_COMPILED_AT, must_start_by_utc=FIRST_BATCH_START_BY,
        )
        ledger = self.root / "timing-ledger"
        timing.create_ledger(
            ledger, upgrade_id=f"{CAMPAIGN_ID}-ledger", baseline_version=BASELINE, target_version=TARGET,
            campaign_purpose="validation_only", evidence_decision="recapture", client="claude",
        )
        timing.append_event(ledger, event_id="fixture-vc0-completed", phase="VC-0", event_type="stage_completed", next_action="VC-0 收口完成", receipts=[])
        vc0 = claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-0")
        seal_tests.rewrite_manifest(
            campaign,
            vc_control={**manifest["vc_control"], "vc0_checkpoint": claude_upgrade_vc.vc_control_binding(campaign, vc0), **first},
            # 计时控制绑定按 Codex 收口写出的形状带 upgrade_id 与证据决定（监督器失败收口按 upgrade_id 核对账本归属）。
            control_receipts={
                "upgrade_timing": {
                    "ledger_dir": str(ledger.resolve()),
                    "ledger_plan_sha256": claude_upgrade.file_sha256(ledger / "ledger.json"),
                    "upgrade_id": f"{CAMPAIGN_ID}-ledger",
                    "evidence_decision": "recapture",
                }
            },
        )
        for directory in (campaign / "control", campaign / "control" / "vc"):
            directory.chmod(0o700)
        self.project_root = ledger_fixture.register_claude_campaign(campaign)
        ledger_fixture.write_claude_deploy_receipt(self.data, self.tool_identity, stamp="20261007t000000z")
        self.state_dir = self.data / "control" / f"{CAMPAIGN_ID}-supervisor"
        self.state_dir.mkdir(mode=0o700)
        self.ledger_dir = ledger.resolve()
        self.store_root = claude_upgrade_store.campaign_store_root(campaign)
        return campaign

    def dispatch(self, campaign: Path, phase: str, sequence: int, actions: list[dict[str, Any]]) -> tuple[int, dict | None, str]:
        plan_path = self.root / "action-plans" / f"{phase.lower()}-{actions[0]['action_id']}.json"
        if not plan_path.exists():
            _write_json(plan_path, batch.action_plan(actions, phase=phase))
        previous = vc_artifacts.VC_PHASES[vc_artifacts.VC_PHASES.index(phase) - 1]
        return run_main(
            [
                "compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", phase, "--sequence", sequence,
                "--predecessor-checkpoint", claude_upgrade_vc.vc_checkpoint_path(campaign, previous),
                "--action-plan", plan_path, "--state-dir", self.state_dir,
            ]
        )

    def assert_batch_passed(self, outcome: tuple[int, dict | None, str], sequence: int, phase: str) -> dict[str, Any]:
        code, result, stderr = outcome
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"], result["campaign_run"]["reason"]), (0, "stopped", "queue-complete"), stderr)
        name = f"{sequence:04d}-{phase.lower()}.json"
        commit = vc_artifacts.validate_vc_commit(_read(self.campaign / "control" / "vc" / "commits" / name))
        published = _read(self.campaign / "control" / "vc" / "run-manifests" / name)
        self.assertEqual((commit["sequence"], commit["phase"]), (sequence, phase))
        self.assertEqual((published["schema_version"], published["client"]), (supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "claude"))
        self.assertEqual(result["commit"]["commit_sha256"], commit["commit_sha256"])
        return result

    def store_facts(self) -> list[dict[str, Any]]:
        return ControlStore(self.store_root).list_facts(CAMPAIGN_ID)

    def assert_zero_requests(self) -> None:
        head = project_ledger.replay_head(self.project_root)
        self.assertEqual((head["precise_total"], head["estimated_total"]), (0, 0))


@unittest.skipUnless(site.ENABLE_USER_SITE, "崩溃注入经 usercustomize 进入动作子进程，要求启用用户 site")
class AtomicBatchChainTests(_RealTreeBatchCase):
    def classify_action(self, action_id: str, argv: list[Any]) -> dict[str, Any]:
        return batch.managed_action(action_id, f"VC-2:{action_id}", argv, item_ids=[action_id])

    def test_vc2_first_batch_commits_and_n_plus_one_redispatch_converges(self) -> None:
        campaign = self.campaign = self.build_batch_campaign()
        facts_before = len(self.store_facts())

        # 批次 2：VC-2 首批（机器草案）零请求派发；no-op 首批先被代跑成父 run 历史。
        draft_argv = ["classify", "--campaign-dir", campaign, "--candidates", self.candidates_path(campaign)]
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, [self.classify_action("vc2-classify-draft", draft_argv)]), 2, "VC-2")
        self.assertEqual((result["bootstrap_noop_run"]["reason"], result["bootstrap_noop_run"]["batch_sequence"]), ("incremental-noop", 1))
        events = [(item["event_type"], item["phase"]) for item in result["timing_ledger"]["events"]]
        self.assertEqual(events, [("stage_started", "VC-1"), ("stage_completed", "VC-1"), ("stage_started", "VC-2")])
        self.assertEqual(result["commit"]["ledger_event_ids"], [item["event_id"] for item in result["timing_ledger"]["events"]])
        self.assertIsNone(result["timing_ledger"]["completion"])
        state = timing.inspect_ledger(self.ledger_dir)
        self.assertEqual((state["status"], state["active_phase"]), ("active", "VC-2"))
        self.assertTrue((campaign / classify.DRAFT_ROOT / "rev-0001" / classify.DRAFT_RECEIPT_FILENAME).is_file())
        self.assertEqual(len(self.store_facts()), facts_before)
        staged = campaign / "control" / "vc" / "staging" / "0002-vc-2" / "attempt-1"
        self.assertEqual(sorted(path.name for path in staged.iterdir()), sorted(["batch.json", "run-manifest.json", "PREPARED"]))

        # 审核人处置草案；批次 3：修订。审核人在批次之外复跑同一修订命令读取 review_sha256（同内容复用，不写文件）。
        directory = self.write_documents(self.resolved_documents(campaign / classify.DRAFT_ROOT / "rev-0001"), self.root / "vc2-resolved")
        revise_argv = ["classify", "--campaign-dir", campaign, "--from-revision", "rev-0001", "--revision-input", directory]
        self.assert_batch_passed(self.dispatch(campaign, "VC-2", 3, [self.classify_action("vc2-classify-revise", revise_argv)]), 3, "VC-2")
        code, revised, stderr = self.revise(campaign, "rev-0001", directory)
        self.assertEqual((code, revised["revision"], revised["reused"], revised["gate"]["reasons"]), (2, "rev-0002", True, []), stderr)

        # 批次 4：批准；子进程在 Store 分类事实写入之后、批准目录发布之前被硬杀。
        approve = [self.classify_action("vc2-classify-approve", self.approve_argv(campaign, "rev-0002", revised["review_sha256"]))]
        inject = self.root / "inject"
        inject.mkdir(mode=0o700)
        (inject / "usercustomize.py").write_text(USERCUSTOMIZE, encoding="utf-8")
        marker = self.root / "crashed.marker"
        crash = {
            "PYTHONPATH": str(inject), CRASH_ACTION_ENV: "vc2-classify-approve", CRASH_MARKER_ENV: str(marker),
            CRASH_SUFFIX_ENV: "/classification/approved",
        }
        with mock.patch.dict(os.environ, crash):
            code, failed, stderr = self.dispatch(campaign, "VC-2", 4, approve)
        self.assertTrue(marker.is_file(), "崩溃注入没有生效（usercustomize 未被动作子进程加载）")
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:vc2-classify-approve"), stderr)
        failed_run = Path(failed["campaign_run"]["run_dir"])
        classifications = [fact for fact in self.store_facts() if fact["fact_kind"] == "rule_classification_recorded"]
        self.assertEqual(len(classifications), 1)
        self.assertEqual(classifications[0]["producer_run"], {"run_dir": str(failed_run), "owner_nonce": supervisor._read_state(failed_run)["owner_nonce"], "action_id": "vc2-classify-approve"})
        self.assertFalse((campaign / classify.APPROVED_ROOT).exists())
        self.assertTrue(any(path.name.startswith(classify.APPROVED_TEMPORARY_PREFIX) for path in (campaign / "classification").iterdir()))
        self.assertFalse(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-2").exists())
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "stage_review_required")
        self.assertTrue((campaign / "control" / "vc" / "commits" / "0004-vc-2.json").is_file())

        # 对账之前 N+1 被拒，零写入（没有 0005 的 staging）。
        code, _result, stderr = self.dispatch(campaign, "VC-2", 5, approve)
        self.assertEqual(code, 1)
        self.assertIn("stage_review_required", stderr)
        self.assertFalse((campaign / "control" / "vc" / "staging" / "0005-vc-2").exists())

        # 对账：阶段幂等重派合同成立，写证明，账本重开 VC-2、下一动作逐字重派。
        code, reconciled, stderr = run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", failed_run])
        self.assertIsNotNone(reconciled, stderr)
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        proof = reconciled["stage_replay"]
        self.assertEqual((proof["allowed"], proof["phase"], proof["next_action"]), (True, "VC-2", "redispatch-same-batch"))
        self.assertEqual([item["action_id"] for item in proof["actions"]], ["vc2-classify-approve"])
        self.assertTrue(any(key.startswith("classification/" + classify.APPROVED_TEMPORARY_PREFIX) for key in proof["actions"][0]["outputs"]))
        state = timing.inspect_ledger(self.ledger_dir)
        self.assertEqual((state["status"], state["active_phase"], state["next_action"]), ("active", "VC-2", "redispatch-same-batch"))
        self.assertEqual(_read(campaign / "control" / "reconciliation" / f"run-{failed_run.name}" / "stage-replay.json"), proof)
        # 重复对账幂等：证明逐字相同，账本不再追加。
        code, again, stderr = run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", failed_run])
        self.assertEqual((code, again["stage_replay"]), (0, proof), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["head_sequence"], state["head_sequence"])

        # N+1 逐字重派收敛：Store 事实复用（仍是第一次的 producer_run），批准目录、阶段结果与 VC-2 checkpoint 写出，阶段完成登记。
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 5, approve), 5, "VC-2")
        self.assertEqual(result["timing_ledger"]["events"], [])
        self.assertEqual(result["timing_ledger"]["completion"]["idempotent"], False)
        classifications = [fact for fact in self.store_facts() if fact["fact_kind"] == "rule_classification_recorded"]
        self.assertEqual(len(classifications), 1)
        self.assertEqual(classifications[0]["producer_run"]["run_dir"], str(failed_run))
        self.assertEqual(collections.Counter(fact["fact_kind"] for fact in self.store_facts())["rule_classification_recorded"], 1)
        stage = claude_upgrade._load_stage_result(campaign, "classify")
        self.assertEqual(stage["revision"], "rev-0002")
        self.assertFalse(any(path.name.startswith(classify.APPROVED_TEMPORARY_PREFIX) for path in (campaign / "classification").iterdir()))
        plan = claude_upgrade._vc_campaign_plan(campaign, claude_upgrade.load_campaign_manifest(campaign))
        _path, checkpoint = claude_upgrade._replay_vc_checkpoint(campaign, plan, "VC-2")
        self.assertEqual(checkpoint["metrics"], {"live_request_count": 0, "scanned_bytes": 0})
        self.assertIn("VC-2", timing.phase_ledger_state(self.ledger_dir)["completed_phases"])
        sequences = sorted(int(path.name[:4]) for path in (campaign / "control" / "vc" / "commits").iterdir())
        self.assertEqual(sequences, [2, 3, 4, 5])

        # VC-2 已封存：再编译 VC-2 批次被拒（不是阶段审核许可的逐字重派）。
        code, _result, stderr = self.dispatch(campaign, "VC-2", 6, approve)
        self.assertEqual(code, 1)
        self.assertIn("VC-2", stderr)
        self.assertFalse((campaign / "control" / "vc" / "commits" / "0006-vc-2.json").exists())
        self.assert_zero_requests()

    def test_vc3_and_vc4_batches_reach_candidate_sealed(self) -> None:
        campaign = self.campaign = self.build_batch_campaign()
        self.approve_vc2_directly(campaign, change=(profile_tests.CHANGE,), delete=(profile_tests.DELETE,))
        for source, name in ((profile_tests.ACTIVE_CATALOG, "active-release-catalog.json"), (profile_tests.ACTIVE_PROFILE, "active-profile.json")):
            (campaign / "analysis" / name).write_bytes(source.read_bytes())
            (campaign / "analysis" / name).chmod(0o600)

        def action(action_id: str, argv: list[Any]) -> dict[str, Any]:
            return batch.managed_action(action_id, f"VC-3:{action_id}", argv, item_ids=[action_id])

        # 批次 2：准备草案（VC-1、VC-2 由批次入口补登记完成）。
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-3", 2, [action("vc3-prepare-draft", self.prepare_draft_argv(campaign))]), 2, "VC-3")
        events = [(item["event_type"], item["phase"]) for item in result["timing_ledger"]["events"]]
        self.assertEqual(events, [("stage_started", "VC-1"), ("stage_completed", "VC-1"), ("stage_started", "VC-2"), ("stage_completed", "VC-2"), ("stage_started", "VC-3")])
        draft_dir = campaign / profile.PREPARE_DRAFT_ROOT / "rev-0001"
        directory = self.write_prepare(self.resolved_prepare(campaign, draft_dir), self.root / "prepare-resolved")
        # 批次 3：准备修订；审核人复跑同一修订命令读取 review_sha256。
        revise_argv = ["prepare-profile", "--campaign-dir", campaign, "--from-revision", "rev-0001", "--revision-input", directory]
        self.assert_batch_passed(self.dispatch(campaign, "VC-3", 3, [action("vc3-prepare-revise", revise_argv)]), 3, "VC-3")
        code, revised, stderr = self.revise_prepare(campaign, "rev-0001", directory)
        self.assertEqual((code, revised["revision"], revised["reused"], revised["gate"]["reasons"]), (2, "rev-0002", True, []), stderr)
        # 批次 4：准备批准（EvidenceApprovalFact 带本动作的 producer_run）。
        approve_prepare = action("vc3-prepare-approve", self.approve_prepare_argv(campaign, "rev-0002", revised["review_sha256"]))
        prepare_run = self.assert_batch_passed(self.dispatch(campaign, "VC-3", 4, [approve_prepare]), 4, "VC-3")["campaign_run"]["run_dir"]
        (evidence_approval,) = [fact for fact in self.store_facts() if fact["fact_kind"] == "evidence_approved"]
        self.assertEqual((evidence_approval["producer_run"]["run_dir"], evidence_approval["producer_run"]["action_id"]), (prepare_run, "vc3-prepare-approve"))
        # 批次 5：暂存预览；审核人读冻结的联合清单取 joint_sha256。批次 6：暂存批准。
        self.assert_batch_passed(self.dispatch(campaign, "VC-3", 5, [action("vc3-stage-preview", ["stage-profile", "--campaign-dir", campaign])]), 5, "VC-3")
        (joint_manifest,) = sorted((campaign / profile.STAGE_ROOT).glob(f"*/{profile.JOINT_MANIFEST_FILENAME}"))
        joint = _read(joint_manifest)["joint_sha256"]
        stage_approve = action("vc3-stage-approve", ["stage-profile", "--campaign-dir", campaign, "--approve-joint-sha256", joint, "--approved-by", "reviewer-a"])
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-3", 6, [stage_approve]), 6, "VC-3")
        self.assertEqual(result["timing_ledger"]["completion"]["idempotent"], False)
        self.assertIn("VC-3", timing.phase_ledger_state(self.ledger_dir)["completed_phases"])
        self.assertEqual(seal_tests.store_status(self.store_root, CAMPAIGN_ID), "profile_approved")
        stage = claude_upgrade._load_stage_result(campaign, "stage-profile")
        self.assertEqual((stage["joint_sha256"], stage["prepared_revision"]), (joint, "rev-0002"))

        # VC-3 的阶段幂等重派证明：已批准后暂存批准可逐字重派（绑定 checkpoint），预览与准备草案必然被拒。
        replay = claude_upgrade._campaign_stage_replay_facts(campaign, {"phase": "VC-3", "actions": [approve_prepare, stage_approve]})
        self.assertTrue(replay["allowed"], replay)
        checkpoint = claude_upgrade.file_sha256(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-3"))
        self.assertTrue(all(item["outputs"]["checkpoint"] == checkpoint for item in replay["actions"]))
        self.assertEqual(set(replay["actions"][1]["inputs"]), {joint_manifest.relative_to(campaign).as_posix()})
        for refused, pattern in (
            (action("p", ["stage-profile", "--campaign-dir", campaign]), "画像已批准"),
            (action("d", self.prepare_draft_argv(campaign)), "画像已批准"),
            (action("o", ["stage-profile", "--campaign-dir", campaign, "--approve-joint-sha256", joint, "--approved-by", "reviewer-b"]), "批准人 reviewer-a"),
        ):
            result = claude_upgrade._campaign_stage_replay_facts(campaign, {"phase": "VC-3", "actions": [refused]})
            self.assertFalse(result["allowed"])
            self.assertRegex(result["reasons"][0], pattern)

        # VC-4（B3-04）：没有候选 revision 时派发前拒绝，不留 staging、账本不动。
        head = timing.inspect_ledger(self.ledger_dir)["head_sequence"]
        plan = self.root / "action-plans" / "vc-4-synthetic.json"
        _write_json(plan, batch.action_plan([batch.managed_action("vc4-plan", "VC-4:plan", ["status", "--campaign-dir", campaign], item_ids=["vc4-plan"])], phase="VC-4"))
        code, _result, stderr = run_main(
            [
                "compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", "VC-4", "--sequence", 7,
                "--predecessor-checkpoint", claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-3"), "--action-plan", plan,
                "--state-dir", self.state_dir,
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("revision-open --initial", stderr)
        self.assertFalse((campaign / "control" / "vc" / "staging" / "0007-vc-4").exists())
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["head_sequence"], head)

        # revision-open --initial：编排器侧（revision 目录＋账本 stage_revision）与 Store 侧两份登记。
        candidate_id = "claude-cand-r1"
        code, opened, stderr = run_main(["revision-open", "--campaign-dir", campaign, "--candidate-id", candidate_id, "--initial"])
        self.assertEqual((code, opened["revision"], opened["store"]["payload"]["revision"]), (0, 1, 1), stderr)
        # 候选源码链（基线 → 纳入暂存 Release 与门禁映射 → 门禁计划）、构建实物、source transition 与实现测试收据。
        code, preview, stderr = run_main(["plan-candidate-gates", "--campaign-dir", campaign, "--candidate-id", candidate_id])
        self.assertEqual(code, 2, stderr)
        fixture = build_fixture.CandidateBuildFixture(self.root / "build", campaign, candidate_id, run_main=run_main)
        fixture.build_source(preview=preview)
        fixture.build_artifacts()
        fixture.build_transition()
        fixture.build_implementation_receipt(preview["requirements"], f"{CAMPAIGN_ID}-ledger")

        def vc4_action(action_id: str, argv: list[Any]) -> dict[str, Any]:
            return batch.managed_action(action_id, f"VC-4:{action_id}", argv, item_ids=[action_id])

        # 批次 7：plan-candidate-gates 在候选源码副本上重新生成计划（与驱动同一做法），与提交内计划逐字一致；VC-4 进行中。
        plan_source = self.root / "plan-source"
        subprocess.run(["git", "clone", "-q", str(fixture.source), str(plan_source)], check=True, capture_output=True, timeout=60)
        lifecycle = fixture.lifecycle_dir.relative_to(fixture.source)
        dispatched_plan = plan_source / lifecycle / "gate-plan-dispatch.json"
        plan_action = vc4_action(
            "vc4-plan-gates",
            ["plan-candidate-gates", "--campaign-dir", campaign, "--candidate-id", candidate_id, "--candidate-source", plan_source,
             "--mapping", plan_source / lifecycle / "gate-mapping.json", "--output", dispatched_plan],
        )
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-4", 7, [plan_action]), 7, "VC-4")
        self.assertEqual([(item["event_type"], item["phase"]) for item in result["timing_ledger"]["events"]], [("stage_started", "VC-4")])
        self.assertIsNone(result["timing_ledger"]["completion"])
        self.assertEqual(dispatched_plan.read_bytes(), fixture.plan_path.read_bytes())
        commit = vc_artifacts.validate_vc_commit(_read(campaign / "control" / "vc" / "commits" / "0007-vc-4.json"))
        self.assertEqual(commit["phase"], "VC-4")
        published = _read(campaign / "control" / "vc" / "run-manifests" / "0007-vc-4.json")
        self.assertEqual((published["candidate_id"], published["candidate_revision"]), (candidate_id, 1))
        # 批次 8：record-candidate-build（子进程里的 docker／go 落到假命令）：Store 到 candidate_sealed、VC-4 checkpoint、阶段完成。
        record_action = vc4_action("vc4-record-build", fixture.argv())
        bin_dir = fixture.install_fake_commands(self.root / "fake-bin")
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
            result = self.assert_batch_passed(self.dispatch(campaign, "VC-4", 8, [record_action]), 8, "VC-4")
        self.assertEqual(result["timing_ledger"]["events"], [])
        self.assertEqual(result["timing_ledger"]["completion"]["idempotent"], False)
        self.assertIn("VC-4", timing.phase_ledger_state(self.ledger_dir)["completed_phases"])
        self.assertEqual(seal_tests.store_status(self.store_root, CAMPAIGN_ID), "candidate_sealed")
        (frozen,) = [fact for fact in self.store_facts() if fact["fact_kind"] == "candidate_frozen"]
        self.assertEqual(
            (frozen["producer_run"]["run_dir"], frozen["producer_run"]["action_id"]), (result["campaign_run"]["run_dir"], "vc4-record-build")
        )
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        checkpoint_path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(campaign, claude_upgrade_vc.vc_campaign_plan(campaign, manifest), "VC-4", revision=1)
        self.assertEqual(checkpoint["stage_receipt"]["path"], f"candidates/{candidate_id}/build-receipt.json")
        self.assertTrue((claude_upgrade._candidate_revision_dir(campaign, 1) / "seal.json").is_file())
        # VC-4 的阶段幂等重派证明：构建动作逐字重派绑定 checkpoint 与 Store 侧事实；计划动作输出已存在即拒绝。
        run = {"phase": "VC-4", "candidate_id": candidate_id, "candidate_revision": 1}
        replay = claude_upgrade._campaign_stage_replay_facts(campaign, {**run, "actions": [record_action]})
        self.assertTrue(replay["allowed"], replay)
        outputs = replay["actions"][0]["outputs"]
        self.assertEqual(outputs["checkpoint"], claude_upgrade.file_sha256(checkpoint_path))
        self.assertIn("control-store/candidate_frozen", outputs)
        refused = claude_upgrade._campaign_stage_replay_facts(campaign, {**run, "actions": [plan_action]})
        self.assertFalse(refused["allowed"])
        self.assertIn("已存在", refused["reasons"][0])
        self.assert_zero_requests()

if __name__ == "__main__":
    unittest.main()
