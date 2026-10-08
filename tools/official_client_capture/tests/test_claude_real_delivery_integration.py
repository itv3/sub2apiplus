"""合同 A 的本机组合验收：真实 Store、私有文件现场与独立恢复守卫。

只在测试作用域登记 regression_fixture 来源，不启动 Docker、产品或账号。
两份候选走完整 VC-5 事实链，Profile 摘要取真实原件。物理动作使用文件
现场，核对提交后中断只补原事实，以及直接发布与归档闭集的拒绝行为。
"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import FileRuntime, fixture_world
from tools.official_client_control import contracts, delivery_execution as execution, receipts
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign


def fixture_projection() -> dict:
    """测试现场的稳定下层设置，采用正式投影闭集。"""

    return executor.stable_environment_projection({"services": {"app": {"image": "fixture-candidate"}, "db": {"image": "fixed-db"}, "redis": {"image": "fixed-redis"}}}, {"app": {"Config": {"Image": "fixture-candidate"}}, "db": {"Config": {"Image": "fixed-db"}}, "redis": {"Config": {"Image": "fixed-redis"}}}, "app")


class DeliveryCampaign(SyntheticCampaign):
    """沿用既有事实生产器，构建输入绑定文件现场实际材料。"""

    materials: dict

    def select_profile(self, name: str) -> None:
        """分别封存两份真实 Profile；历史候选仍引用原批准和原 Release。"""

        digest = self.materials[name]["runtime_profile_sha256"]
        for kind, key in (("snapshot", "snapshot"), ("release_bundle", "release-bundle"), ("release_artifact", "release")):
            payload = deepcopy(self.store.load_object(self.references[key])["payload"])
            payload["profile_digest"] = digest
            if kind != "snapshot":
                payload["snapshot_ref"] = self.references["snapshot"]
            if kind == "release_artifact":
                payload["release_bundle_ref"] = self.references["release-bundle"]
            self.references[key] = self.store.seal_object(kind, payload)

    def candidate_build_input(self, tag: str = "strict") -> dict:
        value = super().candidate_build_input(tag)
        material = self.materials["candidate" if tag == "strict" else "rollback"]
        value["profile_identity"]["profile_digest"] = material["runtime_profile_sha256"]
        value["build_identity"]["target_architecture"] = "linux/arm64"
        parameters = {"offline": True, "target": "linux/arm64"}
        value["build_identity"].update(parameters=parameters, parameters_sha256=canonical_sha256(parameters))
        value["image"].update(reference="registry.local/sub2api@" + material["image_digest"], image_digest=material["image_digest"], manifest_digest=material["image_digest"])
        return value


class QualifiedFileRuntime(FileRuntime):
    """文件现场仍走真实验收子图；不把假引用当成通过证据。"""

    def check_qualification(self) -> None:
        super().check_qualification()
        for name in ("candidate", "rollback"):
            qualification = self.configuration[name]["qualification"]
            store = ControlStore.open_read_only(self.root / qualification["store_relative"])
            executor.replay_qualification_graph(store, qualification, self.root / qualification["external_root_relative"], self.root)

    def observation(self, stage: str, target: dict, directory: Path) -> dict:
        value = super().observation(stage, target, directory)
        request_path, response_path = directory / "fixture-models-request.json", directory / "fixture-models-response.json"
        guard.write_json_once(request_path, {"method": "GET", "path": "/v1/models", "authentication": "candidate_private_ref", "scope": "regression_fixture"})
        guard.write_json_once(response_path, {"status": 200, "models": ["claude-fixture-model"], "scope": "regression_fixture"})
        value["request_records"] = [{"method": "GET", "path": "/v1/models", "request_binding": guard.binding(self.root, request_path), "response_binding": guard.binding(self.root, response_path)}]
        value["after_bindings"] += [guard.binding(self.root, request_path), guard.binding(self.root, response_path)]
        for row in value["checks"]:
            row["facts"]["bindings"] = deepcopy(value["during_bindings"])
        return value

    def freeze(self, request: dict, directory: Path) -> dict:
        value = super().freeze(request, directory)
        projection = fixture_projection()
        projection_path = directory / "qualified-projection.json"
        guard.write_json_once(projection_path, projection)
        value["cores"]["stable_projection"].update(projection=projection, projection_sha256=canonical_sha256(projection), binding=guard.binding(self.root, projection_path))
        qualification = self.configuration["rollback"]["qualification"]
        store = ControlStore.open_read_only(self.root / qualification["store_relative"])
        acceptance = store.load_fact(qualification["acceptance_ref"])["payload"]
        value["cores"]["rollback_state"]["evidence_package_ref"] = acceptance["candidate_evidence_package_ref"]
        value["cores"]["rollback_state"]["bindings"] += [self.configuration["rollback"]["profile_binding"], self.configuration["rollback"]["override_binding"]]
        recipe = {"schema_version": "claude-upgrade-zero-model-probe-recipe/v1", "allowed_requests": [{"method": "GET", "path": "/health", "authentication": "none"}, {"method": "GET", "path": "/v1/models", "authentication": "candidate_private_ref"}], "scope": "regression_fixture"}
        recipe_path = directory / "qualified-recipe.json"
        guard.write_json_once(recipe_path, recipe)
        value["cores"]["zero_model_probes"].update(recipe=recipe, bindings=[guard.binding(self.root, recipe_path)])
        return value


def serve_qualified_guard(configuration: dict, context: dict) -> None:
    """独立服务持有物理日志；子进程继承的身份树仅限测试副本。"""

    os.setsid()
    identity = executor.guard_identity(configuration, context, formal=False)
    engine = guard.GuardEngine(configuration, context, QualifiedFileRuntime(configuration, context), identity)
    root = Path(context["data_root"])
    key = guard.read_bytes(guard.check_binding(root, configuration["guard"]["channel_key_binding"]))
    guard.serve(engine, root / configuration["guard"]["socket_relative"], key)


class RealDeliveryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree_directory = tempfile.TemporaryDirectory(prefix="delivery-managed-", dir="/private/tmp")
        cls.tree = ManagedToolTree(Path(cls.tree_directory.name) / "repo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tree_directory.cleanup()

    def setUp(self) -> None:
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory(prefix="delivery-combined-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        # macOS 的 /private/tmp 可能继承 wheel 组；受管账本要求本人有效组，夹具从根开始按正式身份创建。
        os.chown(self.root, -1, os.getegid())
        configuration, _, environment = fixture_world(self.root)
        guard.private_directory(self.root / "qualification")
        fixture = DeliveryCampaign(self.root / "qualification", schema="v2")
        fixture.materials = configuration
        fixture.bootstrap_and_campaign(platforms=["linux/arm64"], campaign_purpose="validation_only")
        fixture.discovery_and_evidence()
        fixture.profile_objects()
        fixture.select_profile("rollback")
        fixture.profile_approve("validation_only")
        fixture.strict_validate(tag="baseline")
        fixture.select_profile("candidate")
        fixture.profile_approve("validation_only")
        fixture.strict_validate()
        self.fixture = fixture
        self.store = fixture.store
        for name, tag in (("candidate", "strict"), ("rollback", "baseline")):
            key = lambda reference: reference if tag == "strict" else reference + "-" + tag
            qualification = {"store_relative": "qualification/store", "external_root_relative": "qualification/external", "campaign_id": fixture.campaign_id, "candidate_id": "synthetic-candidate-" + tag, "candidate_ref": fixture.references["candidate-" + tag], "build_receipt_ref": fixture.references[key("candidate-build-receipt")], "validation_ref": fixture.references[key("validation-completed")], "acceptance_ref": fixture.references["acceptance-" + tag]}
            sealed = {"schema_version": "claude-upgrade-acceptance/v1", "candidate_id": qualification["candidate_id"], "store": {"acceptance_ref": qualification["acceptance_ref"], "validation_completed_ref": qualification["validation_ref"]}, "production_state": "not_activated"}
            path = self.root / "inputs" / (name + "-qualification-sealed.json")
            guard.write_json_once(path, sealed)
            qualification["sealed_result_binding"] = guard.binding(self.root, path)
            configuration[name]["qualification"] = qualification
        executor.validate_configuration(configuration)
        self.configuration = configuration
        campaign_dir = self.root / "evidence" / "campaigns" / fixture.campaign_id
        guard.private_directory(campaign_dir)
        configuration_path = campaign_dir / "configuration.json"
        guard.write_json_once(configuration_path, configuration)
        binding = guard.binding(self.root, configuration_path)
        candidate_id = "synthetic-candidate-strict"
        context = {"campaign_id": fixture.campaign_id, "candidate_id": candidate_id, "delivery_id": candidate_id + "-d1", "segment": "dr0", "run_nonce": candidate_id + "-d1-dr0", "data_root": str(self.root), "configuration_ref": binding}
        projection = fixture_projection()
        environment.update(architecture="linux/arm64", configuration_sha256=canonical_sha256(configuration), environment_sha256=canonical_sha256(projection))
        state = guard.read_json(self.root / "state.json")
        state["environment"] = environment
        guard.atomic_json(self.root / "state.json", state)
        self.guard_process = multiprocessing.get_context("fork").Process(target=serve_qualified_guard, args=(configuration, context))
        self.guard_process.start()
        self.addCleanup(self._stop_guard)
        deadline = time.monotonic() + 8
        while not (self.root / configuration["guard"]["socket_relative"]).exists():
            if not self.guard_process.is_alive() or time.monotonic() >= deadline:
                self.fail("组合守卫未成功启动")
            time.sleep(0.01)
        self.executor = executor.create_executor("arm64-isolated", configuration, context)
        source = mock.patch.dict(execution.SOURCE_VERIFIERS, {"claude-arm64-recovery-guard": lambda store, content, origin: execution.VerifiedSource(**self.executor.verify_origin_evidence(content, origin))}, clear=True)
        source.start()
        self.addCleanup(source.stop)
        factory = mock.patch.dict(delivery.ENVIRONMENT_EXECUTORS, {"arm64-isolated": executor.create_executor}, clear=True)
        factory.start()
        self.addCleanup(factory.stop)
        self.context = delivery.DeliveryContext(campaign_dir=campaign_dir, manifest={}, candidate_id=candidate_id, revision=1, store=self.store, campaign=fixture.campaign_document, ledger_dir=self.root / "ledger", data_root=self.root, handoff=delivery._vc5_handoff(self.store, fixture.campaign_document, candidate_id), entry={})
        timing.create_ledger(self.context.ledger_dir, upgrade_id=fixture.campaign_id, baseline_version="0.9.0", target_version="1.0.0", campaign_purpose="validation_only", evidence_decision="recapture", client="claude")
        def event(event_id: str, phase: str, kind: str, **fields) -> None:
            timing.append_event(self.context.ledger_dir, event_id=event_id, phase=phase, event_type=kind, next_action="组合验收夹具", **fields)
        event("vc0-done", "VC-0", "stage_completed")
        for phase in ("VC-1", "VC-2", "VC-3"):
            event(phase + "-started", phase, "stage_started")
            event(phase + "-done", phase, "stage_completed")
        event("revision-r1", "VC-4", "stage_revision", revision=1, candidate_id=candidate_id, revision_commit_sha256="e" * 64)
        for phase in ("VC-4", "VC-5"):
            event(phase + "-started", phase, "stage_started")
            event(phase + "-done", phase, "stage_completed")
        event("vc6-started", "VC-6", "stage_started")
        snapshot = delivery._freeze(self.context, self.executor, "arm64-isolated", context["delivery_id"], 1)
        self.delivery = delivery._open_typed_delivery(self.context, 1, snapshot, 1)

    def _stop_guard(self) -> None:
        self.guard_process.terminate()
        self.guard_process.join(timeout=5)
        if self.guard_process.is_alive():
            self.guard_process.kill()
            self.guard_process.join(timeout=5)

    def _complete(self) -> None:
        previous = None
        for stage in delivery.STAGES:
            value = delivery._execute_typed_stage(self.context, self.executor, self.delivery, stage, previous, None)
            self.assertEqual(value["result"], "pass")
            previous = value["fact_ref"]

    def _seal_review(self) -> dict:
        chain = delivery._chain(self.context, self.delivery["delivery_id"])
        review = delivery._typed_package_review(self.context, self.delivery, chain)
        for manifest in review["operational_evidence"]:
            self.context.session.seal_object("operational_evidence", manifest)
        self.context.session.seal_object("private_archive_manifest", review["private_archive_manifest"])
        return self.context.session.seal_object("candidate_delivery_package", review["package"])

    def test_complete_chain_and_direct_publish_revalidate_all_receipt_fields(self) -> None:
        self._complete()
        package_ref = self._seal_review()
        receipt = receipts.build_candidate_delivery_receipt(self.store, self.fixture.campaign_id, package_ref)
        self.assertEqual(receipt["schema_version"], execution.RECEIPT_V4)
        reference = receipts.finalize_candidate_delivery(self.store, self.fixture.campaign_id, package_ref)
        self.assertEqual(receipts.replay_receipt(self.store, reference), receipt)
        self.assertTrue(self.store.write_receipt_outcome("candidate_delivery", receipt, allow_existing=True)["reused"])
        for field in ("candidate_image_digest", "release_state", "delivery_fact_refs"):
            value = deepcopy(receipt)
            if field == "candidate_image_digest":
                value[field] = "sha256:" + "f" * 64
            elif field == "release_state":
                value[field] = "ready_for_operator_release" if value[field] != "ready_for_operator_release" else "not_activated"
            else:
                value[field] = list(reversed(value[field]))
            value["input_facts_sha256"] = canonical_sha256([*contracts._candidate_delivery_receipt_inputs(value), *[value[key] for key in sorted(execution.RECEIPT_EXTRA_FIELDS)]])
            value["identity_sha256"] = canonical_sha256({key: item for key, item in value.items() if key != "identity_sha256"})
            with self.subTest(field=field), self.assertRaises(ControlError):
                self.store.write_receipt_outcome("candidate_delivery", value, allow_existing=True)
        self.assertEqual(self.store.list_receipt_refs("candidate_delivery"), [reference])

    def test_store_failure_after_physical_commit_only_publishes_original_coordinates(self) -> None:
        with mock.patch.object(delivery, "_record_stage", side_effect=OSError("模拟 Store 写入中断")):
            with self.assertRaises(OSError):
                delivery._execute_typed_stage(self.context, self.executor, self.delivery, "candidate_active", None, None)
        state = guard.read_json(self.root / "state.json")
        self.assertEqual((state["switch_count"], state["restore_count"]), (1, 0))
        summary = self.executor.recover_pending({"reason": "entry"})
        self.assertFalse(summary["blocked"])
        core = summary["operations"][0]["execution"]
        publication_path = delivery._publication_root(self.context, self.delivery["delivery_id"]) / (core["operation_id"] + ".json")
        original = publication_path.read_bytes()
        first = delivery._publish_execution(self.context, self.delivery, core)
        second = delivery._publish_execution(self.context, self.delivery, core)
        self.assertEqual(first["fact_ref"], second["fact_ref"])
        self.assertEqual(publication_path.read_bytes(), original)
        fact = self.store.load_fact(first["fact_ref"])
        publication = guard.read_json(publication_path)
        for field in ("issued_at_utc", "producer_run", "formed_under", "idempotency_key"):
            self.assertEqual(fact[field], publication[field])
        state = guard.read_json(self.root / "state.json")
        self.assertEqual((state["switch_count"], state["restore_count"]), (1, 0))

    def test_missing_publication_and_index_omission_cannot_finalize(self) -> None:
        self._complete()
        inventory = self.executor.inventory()
        core = inventory["executions"][0]
        path = delivery._publication_root(self.context, self.delivery["delivery_id"]) / (core["operation_id"] + ".json")
        original = path.read_bytes()
        path.unlink()
        chain = delivery._chain(self.context, self.delivery["delivery_id"])
        with self.assertRaises(delivery.ConfigurationError):
            delivery._typed_package_review(self.context, self.delivery, chain)
        guard.write_once(path, original)
        index_path = self.root / "guard" / "index.json"
        original_index = guard.read_json(index_path)
        changed = deepcopy(original_index)
        changed["operations"].pop()
        guard.atomic_json(index_path, changed)
        with self.assertRaises(guard.GuardError):
            self.executor.inventory()
        guard.atomic_json(index_path, original_index)
        self.assertEqual(len(self.executor.inventory()["executions"]), 4)

    def test_failed_third_stage_restores_and_dr1_only_runs_remaining_stages(self) -> None:
        previous = None
        for stage in delivery.STAGES[:2]:
            value = delivery._execute_typed_stage(self.context, self.executor, self.delivery, stage, previous, None)
            previous = value["fact_ref"]
        guard.atomic_json(self.root / "fault.json", {"execute": "exception", "restore": "normal", "freeze": "normal"})
        failure = delivery._execute_typed_stage(self.context, self.executor, self.delivery, "candidate_restored", previous, None)
        self.assertEqual(failure["result"], "failed")
        failed_core = execution.evidence_facts(self.store, failure["evidence_ref"], "stage_execution")
        self.assertIsNone(failed_core["environment"])
        self.assertEqual(failed_core["restoration"]["state"], "verified")
        guard.atomic_json(self.root / "fault.json", {"execute": "normal", "restore": "normal", "freeze": "normal"})
        snapshot = delivery._freeze(self.context, self.executor, "arm64-isolated", self.delivery["delivery_id"], 1)
        chain = delivery._chain(self.context, self.delivery["delivery_id"])
        reservation = delivery._reserve(self.context, self.delivery, chain, snapshot, None)
        recovery_ref = reservation["recovery_ref"]
        recovery_payload = self.store.load_object(recovery_ref)["payload"]
        recovery = ("dr1", recovery_ref, recovery_payload)
        for stage in delivery.STAGES[2:]:
            value = delivery._execute_typed_stage(self.context, self.executor, self.delivery, stage, previous, recovery)
            self.assertEqual(value["result"], "pass")
            previous = value["fact_ref"]
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 5)
        package_ref = self._seal_review()
        receipt_ref = receipts.finalize_candidate_delivery(self.store, self.fixture.campaign_id, package_ref)
        receipt = self.store.load_receipt(receipt_ref)
        self.assertEqual(receipt["delivery_failure_refs"], [failure["fact_ref"]])
        self.assertEqual(len(receipt["execution_evidence_refs"]), 5)
        self.assertEqual(len(receipt["restoration_evidence_refs"]), 1)


class StoreDimensionLockTests(unittest.TestCase):
    def test_same_thread_reentry_does_not_release_other_thread_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ControlStore.initialize(Path(temporary) / "store", "2026-10-08T00:00:00Z")
            attempted, acquired = threading.Event(), threading.Event()
            def contender() -> None:
                attempted.set()
                with store._dimension_locks("fixture", ("deployment",)):
                    acquired.set()
            with store._dimension_locks("fixture", ("deployment",)):
                with store._dimension_locks("fixture", ("deployment",)):
                    thread = threading.Thread(target=contender)
                    thread.start()
                    self.assertTrue(attempted.wait(1))
                    self.assertFalse(acquired.wait(0.05))
                self.assertFalse(acquired.is_set())
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(acquired.is_set())


if __name__ == "__main__":
    unittest.main()
