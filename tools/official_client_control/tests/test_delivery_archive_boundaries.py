"""交付归档 Profile 父字节边界：真实合同消费及未声明原件的严格负例。

只在局部作用域登记文件原件来源，保留正式空登记表。没有守卫服务、
Docker 或账号调用；冻结配置仍经过原来源、原件与配置合同校验。
"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError
from tools.official_client_control.store import ControlStore


class DeliveryArchiveBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="delivery-archive-boundary-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.configuration, self.context, _environment = fixture_world(self.root)
        self.history = {"path": "history/other-root/original.json", "sha256": "e" * 64, "bytes": 7}
        for name in ("candidate", "rollback"):
            binding = self.write(self.configuration[name]["profile_binding"]["path"], {
                "client": "claude", "mode": name, "historical_source": self.history,
            })
            self.configuration[name].update(profile_binding=binding, runtime_profile_sha256=binding["sha256"])
        self.configuration_binding = self.write("inputs/configuration.json", self.configuration)
        self.configuration_sha256 = canonical_sha256(self.configuration)
        self.proof = self.write("channel/proof.json", {"fixture_only": True})
        self.source = execution.VerifiedSource(
            content_sha256="0" * 64,
            deployment_sha256=self.configuration["guard"]["deployment_receipt_binding"]["sha256"],
            bytes_by_path={}, configuration_sha256=self.configuration_sha256,
            service_instance_id="archive-boundary-fixture", trusted_data_root=self.root,
        )
        self.authority = "archive-boundary-fixture-channel"

        def verifier(_store, core, _origin):
            return replace(self.source, content_sha256=canonical_sha256(core))

        registration = mock.patch.dict(execution.SOURCE_VERIFIERS, {self.authority: verifier}, clear=True)
        registration.start()
        self.addCleanup(registration.stop)
        self.store = ControlStore.initialize(self.root / "store", "2026-10-08T00:00:00Z")
        facts = {
            "contract_version": "claude-delivery-configuration/v1", "result": "pass",
            "campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"],
            "origin": {
                "authority": self.authority, "source_scope": "regression_fixture",
                "configuration_sha256": self.configuration_sha256,
                "service_instance_id": self.source.service_instance_id,
                "operations_index_binding": self.proof,
            },
            "configuration": self.configuration, "configuration_sha256": self.configuration_sha256,
            "binding": self.configuration_binding,
        }
        self.payload = {
            "schema_version": execution.OPERATIONAL_V2,
            "persona": {"provider": "synthetic-provider", "official_product": "synthetic-cli",
                        "auth_family": "oauth", "upstream_route_family": "synthetic-responses"},
            "manifest_id": "archive-boundary-configuration", "evidence_type": "configuration",
            "entries": [{"id": "configuration", "facts": facts}],
        }
        reference = self.store.seal_object("operational_evidence", self.payload)
        self.plan = {
            "schema_version": execution.PLAN_V2,
            "campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"],
            "executor_configuration_ref": reference, "configuration_sha256": self.configuration_sha256,
            "runtime_profile_sha256": self.configuration["candidate"]["runtime_profile_sha256"],
            "rollback_runtime_profile_sha256": self.configuration["rollback"]["runtime_profile_sha256"],
        }

    def write(self, relative: str, value: object) -> dict:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
        path.write_bytes(raw)
        return {"path": relative, "sha256": sha256_bytes(raw), "bytes": len(raw)}

    def archive(self, initial: object = None, *, plan: dict | None = None,
                source: execution.VerifiedSource | None = None) -> list[dict]:
        return execution.complete_delivery_raw_bindings(
            self.store, self.plan if plan is None else plan, self.source if source is None else source,
            self.payload if initial is None else initial,
        )

    def test_frozen_profiles_archive_complete_parent_bytes_without_rebasing_history(self) -> None:
        entries = self.archive()
        for name in ("candidate", "rollback"):
            self.assertIn(self.configuration[name]["profile_binding"], entries)
        self.assertIn(self.configuration_binding, entries)
        self.assertNotIn(self.history, entries)
        self.assertEqual(entries, sorted(entries, key=lambda row: row["path"]))
        # 生产端可先提供配置证据；消费端即使原材料中未列配置，也推导同一两份精确父绑定。
        consumption = self.archive(initial=[self.configuration["candidate"]["profile_binding"]])
        self.assertIn(self.configuration["rollback"]["profile_binding"], consumption)

    def test_unqualified_complete_raw_bindings_keeps_strict_recursive_contract(self) -> None:
        with self.assertRaisesRegex(ControlError, "交付原件缺失"):
            execution.complete_raw_bindings(self.source, self.payload)

    def test_same_bytes_or_profile_name_at_another_path_do_not_grant_a_boundary(self) -> None:
        raw = (self.root / self.configuration["candidate"]["profile_binding"]["path"]).read_bytes()
        for path in ("other/candidate-profile.json", "unknown.json", "journal/000001.json",
                     "publication/op-fixture.json"):
            with self.subTest(path=path):
                binding = self.write(path, raw)
                with self.assertRaisesRegex(ControlError, "交付原件缺失"):
                    self.archive([self.payload, binding])

    def test_unknown_json_still_archives_every_declared_descendant(self) -> None:
        leaf = self.write("unknown/leaf.bin", b"original descendant")
        child = self.write("unknown/child.json", {"original": leaf})
        parent = self.write("unknown/parent.json", {"original": child})
        entries = self.archive([self.payload, parent])
        for binding in (parent, child, leaf):
            self.assertIn(binding, entries)
        (self.root / leaf["path"]).unlink()
        with self.assertRaisesRegex(ControlError, "交付原件缺失"):
            self.archive([self.payload, parent])

    def test_profile_parent_missing_or_byte_drift_is_rejected(self) -> None:
        for name in ("candidate", "rollback"):
            path = self.root / self.configuration[name]["profile_binding"]["path"]
            original = path.read_bytes()
            for failure in ("missing", "drift"):
                with self.subTest(profile=name, failure=failure):
                    if failure == "missing":
                        path.unlink()
                    else:
                        path.write_bytes(original + b" ")
                    try:
                        with self.assertRaisesRegex(ControlError, "原件缺失|字节漂移"):
                            self.archive()
                    finally:
                        path.write_bytes(original)

    def test_profile_parent_symlink_is_rejected(self) -> None:
        path = self.root / self.configuration["candidate"]["profile_binding"]["path"]
        original = path.read_bytes()
        copied = self.root / "copied-profile.json"
        copied.write_bytes(original)
        path.unlink()
        path.symlink_to(copied)
        with self.assertRaisesRegex(ControlError, "符号链接"):
            self.archive()

    def test_plan_coordinates_and_both_profile_digests_must_match_frozen_source(self) -> None:
        changes = {
            "campaign_id": "other-campaign", "delivery_id": "other-delivery",
            "configuration_sha256": "f" * 64, "runtime_profile_sha256": "f" * 64,
            "rollback_runtime_profile_sha256": "f" * 64, "schema_version": "other-plan/v1",
        }
        for key, value in changes.items():
            with self.subTest(field=key):
                plan = {**self.plan, key: value}
                with self.assertRaises(ControlError):
                    self.archive(plan=plan)

    def test_source_instance_deployment_and_data_root_cannot_be_substituted(self) -> None:
        alternatives = {
            "configuration": replace(self.source, configuration_sha256="f" * 64),
            "instance": replace(self.source, service_instance_id="other-instance"),
            "deployment": replace(self.source, deployment_sha256="f" * 64),
            "root": replace(self.source, trusted_data_root=self.root / "other-root"),
        }
        for name, source in alternatives.items():
            with self.subTest(source=name):
                with self.assertRaisesRegex(ControlError, "来源实例不一致"):
                    self.archive(source=source)

    def test_same_profile_path_with_changed_binding_remains_a_conflict(self) -> None:
        binding = {**self.configuration["candidate"]["profile_binding"], "sha256": "f" * 64}
        with self.assertRaisesRegex(ControlError, "不同字节"):
            self.archive([self.payload, binding])

    def test_configuration_reference_still_requires_registered_source_and_raw_bytes(self) -> None:
        with mock.patch.dict(execution.SOURCE_VERIFIERS, {}, clear=True):
            with self.assertRaisesRegex(ControlError, "尚未完成受管通道验收"):
                self.archive()
        (self.root / self.configuration_binding["path"]).write_bytes(
            canonical_json_bytes({**deepcopy(self.configuration), "instance_id": "other-fixture"})
        )
        with self.assertRaisesRegex(ControlError, "字节漂移"):
            self.archive()


if __name__ == "__main__":
    unittest.main()
