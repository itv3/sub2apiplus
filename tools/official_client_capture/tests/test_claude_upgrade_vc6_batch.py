"""Claude VC-6 批次接入（Claude 升级控制面设计 B4-07 收尾）。

一、监督器候选级零请求后处理判定的 Codex 对照（``CandidatePostRunCodexParityTests``，不起子进程）
====================================================================================
B4-07 收尾只给 ``codex_upgrade_supervisor.candidate_post_run_recovery_action`` 加了一条按清单客户端经适配器
``candidate_post_run_extra_action`` 放行的分支。Codex 清单（v1～v3 不带 client，隐含 codex）在一组覆盖各阶段、各类项名
（共享闭集的 compare／acceptance／candidate-seal／assert-*、canonical 各步与退休项、Codex 的 deliver、Claude 的两个交付
项名、空表、非字符串、不可哈希）与各种 execute 项的清单上，改后判定与改前逐字相同——改前判定以 ``_baseline_decision``
（改前函数体的逐字副本，基线为 B4-07 收尾开工时的主线头 aa0415a49）为准，适配器的 Codex 成员恒为假。Claude 清单只多认
形态精确闭合的两个交付动作，其余判定与 Codex 同。

二、真实 VC-5 链上的 VC-6 批次（``Vc6BatchChainTests``）
=======================================================
世界是 B4-06b 的真实批次链（``test_claude_upgrade_compare_accept`` 的 VC-3／VC-4 真实批次 → VC-5 compare、assert-rules、
accept 三批到 VC-5 checkpoint），之后全部以 ``compile-and-run-vc-batch --phase VC-6`` 在父监督器的动作子进程里派发交付
动作；typed 文件现场经 ``claude_typed_vc6_fixture`` 给出回归观测，独立守卫与 Store 都走真实链。
来源只在测试生命周期与指定动作子进程的 usercustomize 中临时登记，退出即恢复，注入点读控制 JSON。
一条用例走完整条恢复链（世界构建约 4 分钟，只建一次）：

1. 直接 CLI 的交付写入被拒（零写入）；
2. 首轮第三阶段失败：``stage_failed`` 在父批次里退出 0（失败已写 Store、由交付侧记账），账本保持进行中；
3. 下一个 run 批次自动预约 dr1，第四阶段 prepare 前动作进程被强杀 → 父批次失败 → 账本 ``recovery_required``（候选级零请求后处理，
   阶段保持进行中，不进候选审核）→ ``reconcile-supervisor-run`` 可恢复 → 逐字重派 N+1 续跑到 ``awaiting_finalize``（退出 0）；
   原替身在阶段入口记调用后即退出，本点仍核未开始的操作同段重派；已 armed 后的真实恢复见 ``TypedArmedRecoveryTests``；
4. 两批之间直接执行 ``delivery-stage-invalidate preview／apply``（控制面命令），下一个 run 批次以 dr2（approved_invalidation）
   从被失效的阶段重跑；status 给出 ``delivery_awaiting_finalize`` 与审核摘要；
5. finalize 批次：交付收据、交付阶段结果与 VC-6 checkpoint，批次层补写 ``vc-batch-NNNN-vc-6-completed``；status 为
   ``ready_for_operator_release``；VC-6 已在当前 revision 完成，再派发 VC-6 批次被拒；全程零请求。

交付门禁未过（``delivery_gates_failed``）在父批次里保持退出 3、交父批次失败收口，由 ``test_claude_upgrade_batch`` 的暂停点
闭集与 ``test_claude_upgrade_delivery`` 的零写入用例覆盖；不放进这条链：Claude 主入口不写动作诊断，同一动作先后两次没有
拒因签名的失败会编成同一根因（``supervisor-run.interrupted``），第二次即按同根因上限暂停，这条链只放一次批次失败。
"""

from __future__ import annotations

import copy
import os
import site
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_recovery
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture.tests import claude_vc5_chain_fixture as chain
from tools.official_client_capture.tests import claude_typed_vc6_fixture as vc6_fixture
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_compare_accept as compare_tests

CANDIDATE = compare_tests.CANDIDATE
run_main = batch_tests.run_main


def _baseline_decision(manifest: Mapping[str, Any], action_id: str) -> str | None:
    """B4-07 收尾改动之前 ``candidate_post_run_recovery_action`` 的函数体逐字副本（基线 aa0415a49）。"""

    if manifest.get("phase") not in {"VC-5", "VC-6"}:
        return None
    actions = manifest.get("actions")
    if not isinstance(actions, list):
        return None
    for action in actions:
        if not isinstance(action, Mapping) or action.get("action_id") != action_id:
            continue
        item_ids = action.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            return None
        execute_items = manifest.get("execute_items")
        if not isinstance(execute_items, list) or not execute_items:
            return None
        if all(supervisor._post_run_tooling_item_allowed(item) for item in item_ids) and all(
            supervisor._post_run_tooling_item_allowed(item) for item in execute_items
        ):
            return "post-run"
        return None
    return None


class CandidatePostRunCodexParityTests(unittest.TestCase):
    """模块说明第一节。"""

    CAMPAIGN_ID = "claude-code-2_1_285-vc6-parity"

    def delivery_actions(self) -> list[dict[str, Any]]:
        campaign = Path("/data/evidence/campaigns") / self.CAMPAIGN_ID
        return [
            delivery.run_action(campaign, "claude-cand-r1", environment_executor="fixture"),
            delivery.finalize_action(campaign, "claude-cand-r1", "a" * 64),
        ]

    def item_matrix(self) -> list[list[Any] | None]:
        return [
            ["compare"], ["acceptance"], ["candidate-seal"], ["assert-rules"], ["canonical-import"], ["canonical-accept"],
            ["production-activation"], ["rollback-verification"], ["retire-0.160.0"], ["deliver"],
            [delivery.RUN_ACTION_ID], [delivery.FINALIZE_ACTION_ID], ["compare", "deliver"], ["compare", delivery.RUN_ACTION_ID],
            [], None, [1], [["unhashable"]], [""],
        ]

    def manifests(self, *, claude: bool) -> list[tuple[dict[str, Any], str]]:
        """（清单, 失败动作 ID）组合：每个阶段 × 每组项名（动作项与 execute 项分别取）× 几种动作形态。"""

        header = (
            {"schema_version": supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "client": "claude"}
            if claude
            else {"schema_version": "codex-upgrade-campaign-run/v3"}
        )
        cases: list[tuple[dict[str, Any], str]] = []
        delivery_actions = self.delivery_actions()
        for phase in ("VC-4", "VC-5", "VC-6", None):
            base = {**header, "phase": phase, "campaign_id": self.CAMPAIGN_ID, "candidate_id": "claude-cand-r1"}
            for items in self.item_matrix():
                for execute in (items, ["compare"], [delivery.RUN_ACTION_ID], ["deliver"], [], None):
                    generic = {"action_id": "a1", "item_ids": items, "command": ["/usr/bin/true"]}
                    cases.append(({**base, "actions": [generic], "execute_items": execute}, "a1"))
                    cases.append(({**base, "actions": [generic], "execute_items": execute}, "other"))
            for action in delivery_actions:
                for execute in (action["item_ids"], ["compare", *action["item_ids"]], ["deliver"]):
                    cases.append(({**base, "actions": [action], "execute_items": execute}, action["action_id"]))
                    wrong_command = dict(action, command=["/usr/bin/true"])
                    cases.append(({**base, "actions": [wrong_command], "execute_items": execute}, action["action_id"]))
            cases.append(({**base, "actions": "not-a-list", "execute_items": ["compare"]}, "a1"))
            cases.append(({**base, "actions": ["not-a-mapping"], "execute_items": ["compare"]}, "a1"))
        return cases

    def test_codex_manifests_decide_exactly_as_before(self) -> None:
        adapter = upgrade_client_adapter.adapter_for("codex")
        cases = self.manifests(claude=False)
        self.assertGreater(len(cases), 400)
        for manifest, action_id in cases:
            self.assertEqual(supervisor.run_manifest_client(manifest), "codex")
            for action in manifest["actions"] if isinstance(manifest["actions"], list) else []:
                self.assertFalse(adapter.candidate_post_run_extra_action(manifest, action))
            self.assertEqual(
                supervisor.candidate_post_run_recovery_action(manifest, action_id),
                _baseline_decision(manifest, action_id),
                (manifest.get("phase"), manifest["actions"], manifest["execute_items"], action_id),
            )

    def test_claude_manifests_add_only_exact_delivery_actions(self) -> None:
        changed: list[tuple[Any, str, Any]] = []
        for manifest, action_id in self.manifests(claude=True):
            before, after = _baseline_decision(manifest, action_id), supervisor.candidate_post_run_recovery_action(manifest, action_id)
            if before != after:
                changed.append((manifest["phase"], action_id, manifest["execute_items"]))
                self.assertEqual((before, after), (None, "post-run"))
                (action,) = manifest["actions"]
                self.assertIsNotNone(delivery.batch_action_kind(action, manifest))
        # 只有 VC-6 清单里形态精确的两个交付动作（execute 项为它本身，或再加共享闭集里的 compare）多出可恢复判定。
        self.assertEqual(
            sorted(changed, key=repr),
            sorted(
                [
                    ("VC-6", action_id, execute)
                    for action_id in (delivery.RUN_ACTION_ID, delivery.FINALIZE_ACTION_ID)
                    for execute in ([action_id], ["compare", action_id])
                ],
                key=repr,
            ),
        )

    def test_adapter_members(self) -> None:
        codex, claude = upgrade_client_adapter.adapter_for("codex"), upgrade_client_adapter.adapter_for("claude")
        run, finalize = self.delivery_actions()
        manifest = {"phase": "VC-6", "campaign_id": self.CAMPAIGN_ID, "candidate_id": "claude-cand-r1"}
        self.assertEqual((codex.candidate_post_run_extra_action(manifest, run), codex.candidate_post_run_extra_action(manifest, finalize)), (False, False))
        self.assertEqual((claude.candidate_post_run_extra_action(manifest, run), claude.candidate_post_run_extra_action(manifest, finalize)), (True, True))
        self.assertFalse(claude.candidate_post_run_extra_action(manifest, {"action_id": delivery.RUN_ACTION_ID}))
        self.assertFalse(claude.candidate_post_run_extra_action(None, run))
        # 交付动作项名不进 post-run-tooling 五判据的共享闭集（只走 execution-failure 的残余恢复路径）。
        self.assertFalse(supervisor._post_run_tooling_item_allowed(delivery.RUN_ACTION_ID))


class TypedMacFixtureAuthorityTests(unittest.TestCase):
    """不建 VC-5 整链；完整 Mac v2 reader 核临时 authority 的准确边界。"""

    def setUp(self) -> None:
        from tools.official_client_capture import claude_client_receipt_contract as contract
        from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
        from tools.official_client_capture.tests import claude_client_receipt_fixture as client_fixture
        from tools.official_client_capture.tests import claude_mac_v2_fixture as mac_fixture
        from tools.official_client_capture.tests.project_ledger_fixture import FIXTURE_ONLY_ENV
        from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world

        directory = tempfile.TemporaryDirectory(prefix="typed-mac-scope-")
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve() / "staging/data"
        self.contract, self.guard = contract, guard
        self.configuration, self.context, _environment = fixture_world(self.data)
        q = self.configuration["candidate"]["qualification"]
        self.campaign = self.data / "evidence/campaigns" / q["campaign_id"]
        client_fixture.write(self.campaign / "campaign.json", {"campaign_id": q["campaign_id"], "client": "claude"})
        self.attempt = self.campaign / "candidates" / q["candidate_id"] / "attempts/first"
        values = {"campaign_id": q["campaign_id"], "candidate_id": q["candidate_id"],
            "attempt_id": "first", "run_nonce": "mac-fixture-run"}
        self.receipts = {}
        with mock.patch.dict(os.environ, {FIXTURE_ONLY_ENV: "1"}), mac_fixture.FixturePatch():
            for client_id in sorted(contract.CLIENTS):
                self.receipts[client_id] = mac_fixture.write_receipt(self.attempt, client_id, values)
        self.round_path = self.campaign / "candidates" / q["candidate_id"] / "result.json"
        client_fixture.write(self.round_path, {"campaign_id": q["campaign_id"], "candidate_id": q["candidate_id"],
            "attempt": {key: contract.binding(self.attempt / "attempt.json", self.campaign)[key] for key in ("path", "sha256")},
            "client_evidence": {"client_bindings": {client_id: {key: contract.binding(path, self.campaign)[key] for key in ("path", "sha256")}
                for client_id, path in self.receipts.items()}}})
        parent = client_fixture.write(self.campaign / "acceptance" / q["candidate_id"] / "result.json",
            {"campaign_id": q["campaign_id"], "candidate_id": q["candidate_id"], "external_gate": {}})
        q["sealed_result_binding"] = guard.binding(self.data, parent)
        self.recorder = client_fixture.RECORDER
        first = contract.read_json(self.receipts["claude-desktop"])
        self.task = contract.validate_task(contract.read_json(contract.bound_file(self.attempt, first["task"])))
        self.capture_root = Path(self.task["execution"]["capture_root"])
        self.bundle_root = contract.bound_file(self.attempt, first["bundle"]).parent
        self.before_adapters = dict(contract.SOURCE_ADAPTERS)
        self.before_wrapper = claude_upgrade.verify_mac_source_v2

    def assert_restored(self) -> None:
        self.assertEqual(self.contract.SOURCE_ADAPTERS, self.before_adapters)
        self.assertIs(claude_upgrade.verify_mac_source_v2, self.before_wrapper)

    def test_original_reader_replays_both_clients_after_initial_fixture_scope_ended(self) -> None:
        self.assert_restored()
        with vc6_fixture.fixture_mac_source_scope(self.configuration, self.data):
            for client_id, path in self.receipts.items():
                verified = self.contract.verify_client_receipt(self.attempt, client_id, path)
                self.assertEqual(verified["status"], "verified")
                self.assertEqual(verified, self.contract.read_json(path)["verification"])
            adapter = self.contract.SOURCE_ADAPTERS[self.recorder]
            self.assertFalse(adapter.owns_services(type("Service", (), {"fixture_only": True})()))
            self.assertFalse(adapter.owns_recorder(type("Recorder", (), {"fixture_only": True})()))
        self.assert_restored()

    def test_changed_task_client_candidate_or_nonce_has_no_authority(self) -> None:
        with vc6_fixture.fixture_mac_source_scope(self.configuration, self.data):
            adapter = self.contract.SOURCE_ADAPTERS[self.recorder]
            for field, value in (("client_id", "claude-vscode"), ("candidate_id", "another-candidate"),
                    ("campaign_id", "another-campaign"), ("task_nonce", "another-task")):
                with self.subTest(field=field):
                    foreign = copy.deepcopy(self.task)
                    foreign[field] = value
                    foreign["task_sha256"] = self.contract.digest({key: item for key, item in foreign.items() if key != "task_sha256"})
                    with self.assertRaisesRegex(self.guard.GuardError, "准确 task"):
                        adapter.verify_capture_origin(foreign, self.capture_root)
            with self.assertRaisesRegex(self.guard.GuardError, "准确 task"):
                adapter.verify_capture_origin(self.task, self.capture_root.parent)
            with self.assertRaisesRegex(self.guard.GuardError, "冻结原包"):
                adapter.verify_origin(self.task, self.bundle_root.parent, self.capture_root)
        self.assert_restored()

    def test_task_original_byte_drift_is_rejected_during_registered_scope(self) -> None:
        receipt = self.contract.read_json(self.receipts["claude-desktop"])
        task_path = self.contract.bound_file(self.attempt, receipt["task"])
        with vc6_fixture.fixture_mac_source_scope(self.configuration, self.data):
            task_path.write_bytes(task_path.read_bytes() + b" ")
            with self.assertRaisesRegex(self.contract.ConfigurationError, "摘要或字节数漂移"):
                self.contract.SOURCE_ADAPTERS[self.recorder].verify_capture_origin(self.task, self.capture_root)
        self.assert_restored()

    def test_source_origin_drift_is_rejected_by_original_adapter(self) -> None:
        with vc6_fixture.fixture_mac_source_scope(self.configuration, self.data):
            origin = self.contract.read_json(self.bundle_root / "source-origin.json")
            proof = self.contract.bound_file(self.bundle_root, origin["attestation"])
            proof.write_bytes(b'{"fixture_only": false}\n')
            with self.assertRaises(self.contract.ConfigurationError):
                self.contract.verify_client_receipt(self.attempt, "claude-desktop", self.receipts["claude-desktop"])
        self.assert_restored()

    def test_nonfixture_ledger_cannot_acquire_mac_authority(self) -> None:
        from tools.official_client_capture import codex_upgrade_project_ledger as ledger
        root = Path(self.task["execution"]["project_ledger"])
        plan, _raw = ledger._load_plan(root)
        plan["fixture_only"] = False
        with mock.patch.object(ledger, "_load_plan", return_value=(plan, b"")):
            with self.assertRaisesRegex(self.contract.ConfigurationError, "fixture_only"):
                with vc6_fixture.fixture_mac_source_scope(self.configuration, self.data):
                    self.fail("真实账本不能获得测试 authority")
        self.assert_restored()

    def test_exception_restores_preexisting_registry_and_wrapper(self) -> None:
        sentinel = object()
        with mock.patch.dict(self.contract.SOURCE_ADAPTERS, {self.recorder: sentinel, "other-recorder": sentinel}):
            before = dict(self.contract.SOURCE_ADAPTERS)
            with self.assertRaisesRegex(RuntimeError, "测试中断"):
                with vc6_fixture.fixture_mac_source_scope(self.configuration, self.data):
                    raise RuntimeError("测试中断")
            self.assertEqual(self.contract.SOURCE_ADAPTERS, before)
        self.assert_restored()

    def test_formal_configuration_and_unrelated_action_refuse_before_registration(self) -> None:
        formal = dict(self.configuration, source_scope="formal")
        with self.assertRaisesRegex(self.guard.GuardError, "回归配置"):
            with vc6_fixture.fixture_mac_source_scope(formal, self.data):
                self.fail("正式配置不能获得 Mac 测试 authority")
        with mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_ACTION_ID_ENV: "mac-client-run"}):
            with self.assertRaisesRegex(self.guard.GuardError, "两个明确"):
                vc6_fixture.install(str(self.data / "unused-control.json"))
        with self.assertRaisesRegex(ValueError, "两个明确"):
            vc6_fixture.subprocess_environment(self.data / "unused-inject", self.data / "unused-control.json", ["mac-client-run"])
        self.assertFalse((self.data / "unused-inject").exists())
        self.assert_restored()

    def test_fresh_action_process_replays_original_mac_reader_and_restores_scope(self) -> None:
        payload = self.data / "mac-scope-input.json"
        self.guard.write_json_once(payload, {"configuration": self.configuration, "data_root": str(self.data),
            "attempt_root": str(self.attempt), "receipts": {key: str(path) for key, path in self.receipts.items()}})
        script = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.official_client_capture.tests import claude_typed_vc6_fixture as fixture
from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import claude_upgrade
value = json.loads(Path(sys.argv[2]).read_bytes())
before = dict(contract.SOURCE_ADAPTERS)
wrapper = claude_upgrade.verify_mac_source_v2
with fixture.fixture_mac_source_scope(value["configuration"], Path(value["data_root"])):
    for client_id, path in value["receipts"].items():
        verified = contract.verify_client_receipt(Path(value["attempt_root"]), client_id, Path(path))
        assert verified == contract.read_json(Path(path))["verification"]
assert contract.SOURCE_ADAPTERS == before
assert claude_upgrade.verify_mac_source_v2 is wrapper
print("原 Mac v2 读取器在新进程中重放两客户端，退出恢复登记")
'''
        environment = dict(os.environ, **{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV: delivery.RUN_ACTION_ID})
        result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", script,
            str(vc6_fixture.REPOSITORY_ROOT), str(payload)], env=environment,
            capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("退出恢复登记", result.stdout)
        self.assert_restored()


@unittest.skipUnless(site.ENABLE_USER_SITE, "VC-5／VC-6 动作子进程里的接缝经 usercustomize 注入，要求启用用户 site")
class Vc6BatchChainTests(batch_tests._RealTreeBatchCase):
    """模块说明第二节。世界与 VC-5 批次照搬 B4-06b 的真实链（函数原样借用，不继承它的用例）。"""

    vc5_world = compare_tests.CompareAcceptChainTests.vc5_world
    dispatch_vc5 = compare_tests.CompareAcceptChainTests.dispatch_vc5
    compare_action = compare_tests.CompareAcceptChainTests.compare_action
    assert_action = compare_tests.CompareAcceptChainTests.assert_action
    accept_action = compare_tests.CompareAcceptChainTests.accept_action
    gate_receipt = compare_tests.CompareAcceptChainTests.gate_receipt
    run_compare_and_assert = compare_tests.CompareAcceptChainTests.run_compare_and_assert
    assert_recorded_client_requests = compare_tests.CompareAcceptChainTests.assert_recorded_client_requests

    # -- 世界与派发 -------------------------------------------------------------------------------------------

    def setUp(self) -> None:
        with vc6_fixture.retain_setup_world():
            super().setUp()

    def vc5_complete_world(self) -> dict[str, Any]:
        world = self.vc5_world(typed_official_source=True)
        # 只对本次新建世界开启完整环境合同；VC-5 动作与父门禁共读同一配置。
        config = compare_tests._read(world["config_path"])
        config["gate_environment_mode"] = chain.REAL_GATE_ENVIRONMENT
        chain._write_json(world["config_path"], config)
        campaign = world["campaign"]
        comparison = self.run_compare_and_assert(world)
        self.assert_batch_passed(self.dispatch_vc5(world, 10, self.assert_action(campaign, comparison)), 10, "VC-5")
        receipt = self.gate_receipt(world, "candidate-receipt.json")
        self.assert_batch_passed(self.dispatch_vc5(world, 11, self.accept_action(campaign, receipt, world["gate_root"])), 11, "VC-5")
        self.assertTrue(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-5", revision=1).is_file())
        fixture = vc6_fixture.TypedDeliveryFixture(campaign, CANDIDATE)
        fixture.__enter__()
        self.addCleanup(fixture.close)
        world["typed_delivery"] = fixture
        self.control = fixture.control_path
        self.ledger_dir = claude_upgrade._campaign_timing_ledger_dir(campaign, world["manifest"])
        return world

    def set_control(self, world: dict[str, Any], **fields: Any) -> None:
        vc6_fixture.write_control(self.control, **fields)

    def run_action(self, world: dict[str, Any]) -> dict[str, Any]:
        return world["typed_delivery"].run_action()

    def dispatch_vc6(self, world: dict[str, Any], sequence: int, action: dict[str, Any]) -> tuple[int, dict | None, str]:
        campaign = world["campaign"]
        plan = chain._write_json(self.root / "action-plans" / f"vc-6-{sequence:04d}.json", delivery.vc6_action_plan(action))
        environment = vc6_fixture.subprocess_environment(self.root / "inject-vc6", self.control, [str(action["action_id"])])
        with mock.patch.dict(os.environ, environment), \
                vc6_fixture.action_diagnostics(sequence, action, environment, self.control) as output_path:
            outcome = run_main(
                [
                    "compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", "VC-6", "--sequence", sequence,
                    "--predecessor-checkpoint", claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-5", revision=1),
                    "--action-plan", plan, "--state-dir", self.state_dir,
                ]
            )
        return vc6_fixture.diagnostic_outcome(outcome, output_path)

    def assert_batch_failed_and_reconcile(self, outcome: tuple[int, dict | None, str], action_id: str) -> None:
        code, result, stderr = outcome
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["campaign_run"]["reason"]), (1, f"action-failed:{action_id}"), stderr)
        ledger = timing.inspect_ledger(self.ledger_dir)
        # 候选级零请求后处理（B4-07 收尾经适配器认定）：阶段保持进行中，不进候选审核。
        self.assertEqual((ledger["status"], ledger["active_phase"]), ("recovery_required", "VC-6"))
        code, reconciled, stderr = run_main(
            ["reconcile-supervisor-run", "--campaign-dir", self.campaign, "--run-dir", Path(result["campaign_run"]["run_dir"])]
        )
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        self.assertIn("逐字重派同一批次", reconciled["next_command"])

    def candidate_status(self, world: dict[str, Any]) -> dict[str, Any]:
        with chain.seams(world["config_path"]):
            return claude_upgrade_recovery.campaign_status(world["campaign"], CANDIDATE)

    def assert_status(self, world: dict[str, Any], expected: str) -> dict[str, Any]:
        status = self.candidate_status(world)
        self.assertEqual(status["status"], expected, status.get("next_command"))
        return status

    def delivery_calls(self) -> list[tuple[str, str]]:
        return vc6_fixture.read_calls(self.control)

    # -- 用例 ------------------------------------------------------------------------------------------------

    def test_vc6_batches_deliver_recover_and_complete(self) -> None:
        world = self.vc5_complete_world()
        campaign = world["campaign"]
        self.assertEqual(campaign.name, world["manifest"]["campaign_id"])
        self.assert_status(world, "ready")
        # 1. 直接 CLI 的交付写入被拒。
        code, _result, stderr = run_main(
            ["deliver-candidate", "run", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--environment-executor", "fixture"]
        )
        self.assertEqual(code, 1)
        self.assertIn("是 VC-6 批次动作", stderr)
        run = self.run_action(world)
        # 2. 首轮第三阶段失败：stage_failed 在父批次里是合法停点（退出 0），失败由交付侧记账。
        self.set_control(world, failures=[["dr0", "candidate_restored", None]])
        self.assert_batch_passed(self.dispatch_vc6(world, 12, run), 12, "VC-6")
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")
        self.assert_status(world, "delivery_stage_failed")
        # 3. dr1 阶段四 prepare 前强杀（原替身也未变更现场）；父 run 对账后同段重派。
        # armed 后强杀另由 TypedArmedRecoveryTests 覆盖，不能据此把已 armed 事实当成未发生。
        self.set_control(world, kill_at=["dr1", "stable_observed"])
        physical_before = len(vc6_fixture.read_calls(self.control, physical=True))
        self.assert_batch_failed_and_reconcile(self.dispatch_vc6(world, 13, run), delivery.RUN_ACTION_ID)
        self.assert_status(world, "delivery_in_progress")
        self.assertEqual(vc6_fixture.read_calls(self.control, physical=True)[physical_before:], [("dr1", "candidate_restored")])
        self.set_control(world)
        self.assert_batch_passed(self.dispatch_vc6(world, 14, run), 14, "VC-6")
        self.assertEqual(
            self.delivery_calls(),
            [("dr0", "candidate_active"), ("dr0", "rollback_verified"), ("dr0", "candidate_restored"),
             ("dr1", "candidate_restored"), ("dr1", "stable_observed"), ("dr1", "stable_observed")],
        )
        self.assertEqual(vc6_fixture.read_calls(self.control, physical=True),
            [("dr0", "candidate_active"), ("dr0", "rollback_verified"), ("dr0", "candidate_restored"),
             ("dr1", "candidate_restored"), ("dr1", "stable_observed")])
        # 4. 两批之间批准第二阶段失效（控制面命令，直接执行），下一个 run 批次以 dr2 从它起重跑。
        code, preview, stderr = run_main(
            ["delivery-stage-invalidate", "preview", "--campaign-dir", campaign, "--candidate-id", CANDIDATE,
             "--stage", "rollback_verified", "--reason", "回退验证需要在新的回退镜像上重做"]
        )
        self.assertEqual((code, preview["status"]), (2, "preview"), stderr)
        code, applied, stderr = run_main(
            ["delivery-stage-invalidate", "apply", "--campaign-dir", campaign, "--candidate-id", CANDIDATE,
             "--stage", "rollback_verified", "--reason", "回退验证需要在新的回退镜像上重做",
             "--approve-sha256", preview["review_sha256"], "--approved-by", "reviewer-a"]
        )
        self.assertEqual((code, applied["status"]), (0, "applied"), stderr)
        self.assert_status(world, "delivery_invalidation_pending")
        self.assert_batch_passed(self.dispatch_vc6(world, 15, run), 15, "VC-6")
        self.assertEqual(self.delivery_calls()[6:], [("dr2", stage) for stage in delivery.STAGES[1:]])
        status = self.assert_status(world, "delivery_awaiting_finalize")
        review = status["delivery"]["review_sha256"]
        # 5. finalize 批次：收据、交付阶段结果与 VC-6 checkpoint，批次层补写 stage_completed。
        finalized = self.assert_batch_passed(self.dispatch_vc6(world, 16, delivery.finalize_action(campaign, CANDIDATE, review)), 16, "VC-6")
        completion = finalized["timing_ledger"]["completion"]
        self.assertEqual((completion["idempotent"], completion["event_id"]), (False, "vc-batch-0016-vc-6-completed"))
        state = timing.phase_ledger_state(self.ledger_dir)
        self.assertEqual(state["revision_phase_state"]["1"]["VC-6"], "completed")
        self.assertIsNone(state["active_phase"])
        result = delivery.load_stage_result(campaign, CANDIDATE)
        self.assertEqual((result["vc6_status"], result["release_state"]), ("complete", "ready_for_operator_release"))
        _path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(
            campaign, claude_upgrade_vc.vc_campaign_plan(campaign, world["manifest"]), "VC-6", revision=1
        )
        self.assertEqual(checkpoint["stage_receipt"]["path"], f"delivery/{CANDIDATE}/result.json")
        self.assert_status(world, "ready_for_operator_release")
        receipts = world["store"].list_receipt_refs("candidate_delivery")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(result["receipt_ref"], receipts[0])
        # VC-6 已在当前 revision 完成：共享入口拒绝再派发 VC-6 批次。
        code, _result, stderr = self.dispatch_vc6(world, 17, run)
        self.assertEqual(code, 1)
        self.assertIn("已登记 VC-6 在当前 revision 完成", stderr)
        self.assert_recorded_client_requests(world)


if __name__ == "__main__":
    unittest.main()
