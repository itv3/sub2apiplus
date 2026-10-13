"""配置资格去重的轻量边界测试。

实际执行配置形状、来源绑定、文件字节及报告分派校验；完整材料导入和
两份资格重放用明确的异常/计数替身隔离。这里只证明调用不能被绕过或
吞掉，不替代第二根私有 reader、完整 Store 或真实守卫通道的集成验收。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control import qualification_bindings as qualification
from tools.official_client_control import qualification_material_import as materials
from tools.official_client_control import receipts
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256
from tools.official_client_control.errors import ControlError


class ConfigurationOriginalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-configuration-unit-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        base, context, _environment = fixture_world(self.root)
        self.campaign_id = context["campaign_id"]
        self.delivery_id = context["delivery_id"]
        self.configuration_ref = {"object_kind": "operational_evidence", "sha256": "c" * 64}
        self.campaign = {"schema_version": "official-client-control-campaign/v2", "client": "claude", "campaign_id": self.campaign_id,
                         # 材料后继解析读工具演进链：夹具没有登记，冻结身份只作链首占位。
                         "tool_identity": {"fixture": "no-tool-evolution"}}
        store_root = self.root / "control/store"
        (store_root / "campaigns").mkdir(parents=True)
        (store_root / "campaigns" / (self.campaign_id + ".json")).write_bytes(canonical_json_bytes(self.campaign))
        self.store = SimpleNamespace(root=store_root, load_object=self.load_object,
            load_campaign=lambda identifier: deepcopy(self.campaign),
            # 材料后继解析会读已登记的工具演进；本夹具没有任何登记，空列表即"无演进链"。
            list_object_refs=lambda kind: [],
            _verify_external_binding=lambda root, binding: guard.check_binding(root, binding))
        self.configuration = {**base, "schema_version": executor.MATERIAL_CONFIGURATION_SCHEMA,
                              "qualification_materials_binding": self.write_binding("source/material-import.json", {"fixture": "import"})}
        self.install_configuration(self.configuration)
        self.import_validation = mock.Mock(return_value=object())
        self.replay_validation = mock.Mock()
        self.current_tool = mock.Mock()
        for patch in (
            mock.patch.object(materials, "requires_qualification_materials", return_value=True),
            mock.patch.object(materials, "validate_qualification_material_import", self.import_validation),
            mock.patch.object(qualification, "validate_replay_report", self.replay_validation),
            mock.patch.object(receipts, "_require_effective_tool", self.current_tool),
            mock.patch.dict(execution.SOURCE_VERIFIERS, {"configuration-test-source": self.verify_source}, clear=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write_binding(self, relative: str, value: dict) -> dict:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(value))
        return guard.binding(self.root, path)

    def install_configuration(self, configuration: dict) -> None:
        """只为负例重建自洽输入，不能借旧 SHA 掩盖故意变更。"""
        self.configuration = deepcopy(configuration)
        self.configuration_binding = self.write_binding("source/configuration.json", self.configuration)
        self.origin = {"authority": "configuration-test-source", "source_scope": "regression_fixture",
                       "configuration_sha256": canonical_sha256(self.configuration),
                       "service_instance_id": "fixture-producer", "operations_index_binding": None}
        self.configuration_facts = {"contract_version": "claude-delivery-configuration/v1", "result": "pass",
            "campaign_id": self.campaign_id, "delivery_id": self.delivery_id, "origin": self.origin,
            "configuration": self.configuration, "configuration_sha256": canonical_sha256(self.configuration),
            "binding": self.configuration_binding}
        self.configuration_payload = {"schema_version": execution.OPERATIONAL_V2,
            "persona": {"provider": "anthropic", "official_product": "claude-code", "auth_family": "oauth", "upstream_route_family": "messages"},
            "manifest_id": "configuration-fixture", "evidence_type": "configuration",
            "entries": [{"id": "configuration", "facts": self.configuration_facts}]}
        self.report = {"schema_version": qualification.MATERIAL_DELIVERY_SCHEMA,
            "candidate": self.configuration["candidate"], "rollback": self.configuration["rollback"],
            "qualification_replays": [{"fixture": "candidate"}, {"fixture": "rollback"}],
            "candidate_compose": {}, "rollback_compose": {}, "normalized_compose": {},
            "allowed_changes": ["application_image", "application_stage_label", "active_runtime_profile"],
            "result": "pass", "configuration_binding": self.configuration_binding,
            "qualification_materials_binding": self.configuration.get("qualification_materials_binding", {"path": "source/material-import.json", "sha256": "d" * 64, "bytes": 1})}
        self.refresh_report()
        self.source = execution.VerifiedSource("e" * 64, "f" * 64, {}, canonical_sha256(self.configuration), "fixture-producer", self.root)

    def refresh_report(self) -> None:
        report_binding = self.write_binding("source/qualification.json", self.report)
        self.payload = {"evidence_type": "stage_execution", "entries": [{"facts": {
            "campaign_id": self.campaign_id, "delivery_id": self.delivery_id, "result": "pass",
            "configuration_ref": self.configuration_ref, "report_binding": report_binding}}]}

    def load_object(self, reference: dict) -> dict:
        if reference != self.configuration_ref:
            raise ControlError("轻量夹具没有该配置引用")
        return {"payload": deepcopy(self.configuration_payload)}

    def verify_source(self, store, content, origin):
        if store is not self.store:
            raise ControlError("来源跨 Store")
        return execution.VerifiedSource(canonical_sha256(content), "f" * 64, {},
            origin["configuration_sha256"], origin["service_instance_id"], self.root)

    def consume(self, current: bool) -> None:
        execution._validate_qualification_originals(self.store, self.payload, self.source,
            current_execution=current, frozen_configuration_ref=self.configuration_ref)

    def test_v3_import_runs_once_with_exact_purpose_and_both_replays(self) -> None:
        for current in (False, True):
            self.import_validation.reset_mock()
            self.replay_validation.reset_mock()
            self.current_tool.reset_mock()
            with self.subTest(current=current), mock.patch.object(materials, "projection_for_configuration", wraps=materials.projection_for_configuration) as projection:
                self.consume(current)
            projection.assert_called_once()
            self.import_validation.assert_called_once()
            self.assertEqual(projection.call_args.kwargs["purpose"], "current_delivery" if current else "formed_replay")
            self.assertEqual(self.import_validation.call_args.kwargs["purpose"], "current_delivery" if current else "formed_replay")
            self.assertEqual(self.replay_validation.call_count, 2)
            self.assertEqual(self.current_tool.call_count, int(current))

    def test_configuration_source_failure_is_not_skipped_or_swallowed(self) -> None:
        for current in (False, True):
            with self.subTest(current=current), mock.patch.dict(execution.SOURCE_VERIFIERS, {}, clear=True):
                with self.assertRaisesRegex(ControlError, "受管通道验收"):
                    self.consume(current)
        self.import_validation.assert_not_called()

    def test_changed_configuration_bytes_are_rejected_before_import(self) -> None:
        path = self.root / self.configuration_binding["path"]
        path.write_bytes(path.read_bytes() + b" ")
        for current in (False, True):
            with self.subTest(current=current), self.assertRaises(ControlError) as rejected:
                self.consume(current)
            # JSON 入口包装底层 ControlError；核实拒绝原因仍是原字节漂移。
            self.assertIn("漂移", str(rejected.exception.__cause__))
        self.import_validation.assert_not_called()

    def test_v3_cannot_use_v1_configuration_in_either_purpose(self) -> None:
        previous = deepcopy(self.configuration)
        previous["schema_version"] = executor.CONFIGURATION_SCHEMA
        del previous["qualification_materials_binding"]
        self.install_configuration(previous)
        for current, error in ((False, "资格 v3"), (True, "配置 v1 降级")):
            with self.subTest(current=current), self.assertRaisesRegex(ControlError, error):
                self.consume(current)
        self.import_validation.assert_not_called()

    def test_configuration_shape_validation_still_rejects_extra_fields(self) -> None:
        changed = deepcopy(self.configuration)
        changed["unregistered_execution"] = "forbidden"
        self.install_configuration(changed)
        with self.assertRaises(guard.GuardError):
            self.consume(False)
        self.import_validation.assert_not_called()

    def test_report_cannot_select_another_material_import(self) -> None:
        self.report["qualification_materials_binding"] = {"path": "source/other-import.json", "sha256": "d" * 64, "bytes": 1}
        self.refresh_report()
        for current in (False, True):
            with self.subTest(current=current), self.assertRaisesRegex(ControlError, "同一配置 v2"):
                self.consume(current)
        self.import_validation.assert_not_called()

    def test_material_subject_and_current_identity_errors_propagate(self) -> None:
        for current in (False, True):
            for reason in ("门禁完整 subject 漂移", "材料完整读集漂移"):
                self.import_validation.side_effect = ControlError(reason)
                with self.subTest(current=current, reason=reason), self.assertRaisesRegex(ControlError, reason):
                    self.consume(current)
        self.import_validation.side_effect = None
        self.current_tool.side_effect = ControlError("当前有效身份已失效")
        with self.assertRaisesRegex(ControlError, "当前有效身份已失效"):
            self.consume(True)
        # 形成期仍执行独立的完整导入调用，不借当前失败取得新写入权限。
        self.import_validation.reset_mock()
        self.consume(False)
        self.import_validation.assert_called_once()
        self.assertEqual(self.import_validation.call_args.kwargs["purpose"], "formed_replay")

    def test_qualification_replay_failure_still_propagates_after_projection(self) -> None:
        self.replay_validation.side_effect = ControlError("回退资格重放失效")
        with self.assertRaisesRegex(ControlError, "回退资格重放失效"):
            self.consume(False)
        self.import_validation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
