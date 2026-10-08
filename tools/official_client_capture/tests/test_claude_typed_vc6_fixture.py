"""共用 typed 夹具的输入拒绝和 armed 后强杀；重型整链另行排队。

轻量组核原 Store 原件保留、缺配置先于写入拒绝和正式登记不泄漏。
恢复组走既有组合世界的真实 Store、独立守卫与发布流程，不下调策略版本。
"""

from __future__ import annotations

import argparse
import copy
import multiprocessing
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import claude_typed_vc6_fixture as fixture
from tools.official_client_capture.tests import test_claude_real_delivery_integration as integration_fixture
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_json_bytes
from tools.official_client_control.tests.fixtures import SyntheticCampaign


class TypedFixtureInputTests(unittest.TestCase):
    """不启动守卫、Docker、账号或整链，只核允许接入的测试输入。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="typed-input-", dir="/private/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        os.chown(self.root, -1, os.getegid())

    def test_same_store_constructor_preserves_original_campaign_and_external_bytes(self) -> None:
        original = SyntheticCampaign(self.root, schema="v2")
        original.bootstrap_and_campaign()
        before = {path: path.read_bytes() for path in original.external_root.rglob("*") if path.is_file()}
        original_campaign = canonical_json_bytes(original.store.load_campaign(original.campaign_id))
        rollback_root = self.root / "rollback"
        rollback_root.mkdir(mode=0o700)
        rollback = SyntheticCampaign(rollback_root, schema="v2", store=original.store,
                                     external_root=original.external_root, external_prefix="rollback-source")
        rollback.campaign_id = "independent-rollback-campaign"
        rollback.bootstrap_and_campaign(platforms=["linux/arm64"], campaign_purpose="validation_only")
        self.assertIs(rollback.store, original.store)
        self.assertEqual(rollback.external_root, original.external_root)
        self.assertEqual(canonical_json_bytes(original.store.load_campaign(original.campaign_id)), original_campaign)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertTrue(all(binding["path"].startswith("rollback-source/") for binding in
                            (rollback.contract_binding, rollback.official_binding, rollback.private_binding)))
        self.assertGreaterEqual(rollback.tool_identity["policy_version"], 4)

    def test_same_store_requires_nonempty_unique_source_prefix(self) -> None:
        original = SyntheticCampaign(self.root, schema="v2")
        for options in ({"store": original.store}, {"store": original.store, "external_root": original.external_root},
                        {"external_root": original.external_root, "external_prefix": "rollback"}):
            with self.assertRaises(ValueError):
                SyntheticCampaign(self.root / "unused", schema="v2", **options)
        rollback = SyntheticCampaign(self.root / "unused", schema="v2", store=original.store,
                                     external_root=original.external_root, external_prefix="unique-source")
        with self.assertRaises(FileExistsError):
            rollback._write_external("official/synthetic-cli.bin", "不能覆盖原件".encode("utf-8"))

    def test_current_policy_missing_configuration_rejects_before_any_write(self) -> None:
        campaign = SyntheticCampaign(self.root, schema="v2")
        campaign.bootstrap_and_campaign()
        context = SimpleNamespace(store=campaign.store, campaign=campaign.campaign_document)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        arguments = argparse.Namespace(campaign_dir=self.root, candidate_id="current-candidate",
                                       executor_configuration=None, executor_configuration_sha256=None)
        with mock.patch.object(delivery, "_require_parent_batch"), mock.patch.object(delivery, "_resolve", return_value=context), \
                mock.patch.object(delivery, "_delivery_lock") as write_lock, \
                mock.patch.object(delivery, "_environment_executor") as factory:
            with self.assertRaisesRegex(delivery.ConfigurationError, "缺配置零变更拒绝"):
                delivery._run(arguments)
            write_lock.assert_not_called()
            factory.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertGreaterEqual(campaign.tool_identity["policy_version"], 4)

    def test_formal_source_rejected_before_test_registry_changes(self) -> None:
        configuration, context, _environment = fixture_world(self.root)
        configuration = copy.deepcopy(configuration)
        configuration["source_scope"] = "formal"
        raw_path = self.root / "inputs/forbidden-formal.json"
        guard.write_json_once(raw_path, configuration)
        context["configuration_ref"] = guard.binding(self.root, raw_path)
        control_path = self.root / "control.json"
        guard.write_json_once(control_path, {"schema_version": fixture.CONTROL_SCHEMA, "fixture_only": True,
            "formal_version_semantics_certified": False, "context": context})
        before_factories = dict(delivery.ENVIRONMENT_EXECUTORS)
        before_sources = dict(execution.SOURCE_VERIFIERS)
        with mock.patch.object(executor, "create_executor") as create:
            with self.assertRaisesRegex(guard.GuardError, "不能安装正式执行器"):
                with fixture.source_scope(control_path):
                    self.fail("正式来源不能进入测试登记作用域")
            create.assert_not_called()
        self.assertEqual(delivery.ENVIRONMENT_EXECUTORS, before_factories)
        self.assertEqual(execution.SOURCE_VERIFIERS, before_sources)

    def test_configuration_bound_action_keeps_exact_batch_and_postrun_classification(self) -> None:
        configuration, _context, _environment = fixture_world(self.root)
        campaign = self.root / "evidence/campaigns/typed-current-campaign"
        guard.private_directory(campaign)
        path = campaign / "configuration.json"
        guard.write_json_once(path, configuration)
        binding = guard.binding(self.root, path)
        action = delivery.run_action(campaign, "candidate-current", environment_executor=fixture.EXECUTOR_NAME,
            observation_window_seconds=1, executor_configuration=path, executor_configuration_sha256=binding["sha256"])
        manifest = {"schema_version": supervisor.OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA, "client": "claude", "phase": "VC-6",
            "campaign_id": campaign.name, "candidate_id": "candidate-current", "actions": [action],
            "execute_items": [delivery.RUN_ACTION_ID]}
        self.assertEqual(delivery.batch_action_kind(action, manifest), "run")
        self.assertEqual(supervisor.candidate_post_run_recovery_action(manifest, delivery.RUN_ACTION_ID), "post-run")
        raw = path.read_bytes()
        path.write_bytes(raw + b" ")
        self.assertIsNone(delivery.batch_action_kind(action, manifest))
        self.assertIsNone(supervisor.candidate_post_run_recovery_action(manifest, delivery.RUN_ACTION_ID))


def armed_worker(context: delivery.DeliveryContext, configuration: dict, execution_context: dict, plan: dict) -> None:
    """实际 action owner 单独进程组；强杀时不终止持有日志的独立守卫。"""

    os.setsid()
    instance = executor.create_executor("arm64-isolated", configuration, execution_context)
    delivery._execute_typed_stage(context, instance, plan, "candidate_active", None, None)


class TypedArmedRecoveryTests(unittest.TestCase):
    """通过模块属性复用构造方法，避免 unittest 将导入的 TestCase 再次加载。"""

    setUpClass = classmethod(integration_fixture.RealDeliveryIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration_fixture.RealDeliveryIntegrationTests.tearDownClass.__func__)
    setUp = integration_fixture.RealDeliveryIntegrationTests.setUp
    _stop_guard = integration_fixture.RealDeliveryIntegrationTests._stop_guard
    _seal_review = integration_fixture.RealDeliveryIntegrationTests._seal_review

    def test_armed_worker_kill_restores_then_publishes_original_failure_and_resumes_new_segment(self) -> None:
        guard.atomic_json(self.root / "fault.json", {"execute": "wait", "restore": "normal", "freeze": "normal"})
        worker = multiprocessing.get_context("fork").Process(target=armed_worker,
            args=(self.context, self.configuration, self.executor.context, self.delivery))
        worker.start()
        self.addCleanup(self.stop_worker, worker)
        deadline = time.monotonic() + 8
        while guard.read_json(self.root / "state.json")["switch_count"] != 1:
            if not worker.is_alive() or time.monotonic() >= deadline:
                self.fail("动作没有在独立守卫 armed 后开始物理操作")
            time.sleep(0.01)
        index = guard.read_json(self.root / "guard/index.json")
        self.assertEqual(len(index["operations"]), 1)
        operation_id = index["operations"][0]["operation_id"]
        operation = self.root / "guard/operations" / operation_id
        self.assertTrue((operation / "prepared.json").is_file())
        self.assertTrue((operation / "armed.json").is_file())
        self.assertFalse((operation / "physical-intent.json").exists())
        os.kill(worker.pid, signal.SIGKILL)
        worker.join(timeout=5)
        self.assertEqual(worker.exitcode, -signal.SIGKILL)
        summary = self.executor.recover_pending({"reason": "entry"})
        self.assertFalse(summary["blocked"])
        core = summary["operations"][0]["execution"]
        self.assertEqual((core["result"], core["restoration"]["state"]), ("failed", "verified"))
        state = guard.read_json(self.root / "state.json")
        self.assertEqual((state["switch_count"], state["restore_count"]), (1, 1))
        self.assertEqual(state["image_digest"], self.configuration["rollback"]["image_digest"])
        self.assertEqual(state["runtime_profile_sha256"], self.configuration["rollback"]["runtime_profile_sha256"])
        original_core = canonical_json_bytes(core)
        # 物理恢复后只能补原发布预登记，不能为失败事实生成新的形成期或 producer。
        first = delivery._publish_execution(self.context, self.delivery, core)
        publication_path = delivery._publication_root(self.context, self.delivery["delivery_id"]) / (operation_id + ".json")
        original_publication = publication_path.read_bytes()
        second = delivery._publish_execution(self.context, self.delivery, core)
        self.assertEqual(first["fact_ref"], second["fact_ref"])
        publication = guard.read_json(publication_path)
        fact = self.store.load_fact(first["fact_ref"])
        for field in ("issued_at_utc", "producer_run", "formed_under", "idempotency_key"):
            self.assertEqual(fact[field], publication[field])
        self.assertEqual(publication_path.read_bytes(), original_publication)
        repeated = self.executor.recover_pending({"reason": "entry"})
        self.assertEqual(canonical_json_bytes(repeated["operations"][0]["execution"]), original_core)
        self.assertEqual(guard.read_json(self.root / "state.json")["restore_count"], 1)
        guard.atomic_json(self.root / "fault.json", {"execute": "normal", "restore": "normal", "freeze": "normal"})
        snapshot = delivery._freeze(self.context, self.executor, "arm64-isolated", self.delivery["delivery_id"], 1)
        chain = delivery._chain(self.context, self.delivery["delivery_id"])
        reservation = delivery._reserve(self.context, self.delivery, chain, snapshot, None)
        self.assertEqual((reservation["recovery_revision"], reservation["rerun_from_stage"]), ("dr1", "candidate_active"))
        recovery_ref = reservation["recovery_ref"]
        recovery = ("dr1", recovery_ref, self.store.load_object(recovery_ref)["payload"])
        previous = None
        for stage in delivery.STAGES:
            result = delivery._execute_typed_stage(self.context, self.executor, self.delivery, stage, previous, recovery)
            self.assertEqual(result["result"], "pass")
            previous = result["fact_ref"]
        self.assertEqual((guard.read_json(self.root / "state.json")["switch_count"],
                          guard.read_json(self.root / "state.json")["restore_count"]), (5, 1))
        self.assertTrue(delivery._chain(self.context, self.delivery["delivery_id"])["complete"])
        self.assertEqual(publication_path.read_bytes(), original_publication)
        self.assertEqual(canonical_json_bytes(self.store.load_fact(first["fact_ref"])), canonical_json_bytes(fact))

    @staticmethod
    def stop_worker(worker: multiprocessing.Process) -> None:
        if worker.is_alive():
            worker.kill()
        worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
