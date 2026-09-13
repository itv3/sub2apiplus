"""Codex 升级 ARM64 网络与磁盘硬门禁收据测试。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as receipt
from tools.official_client_capture.tests.control_receipt_fixtures import (
    create_arm_receipt,
)


class Arm64EnvironmentReceiptTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, dict[str, object]]:
        create_arm_receipt(
            root,
            phase="p0",
            subject_id="upgrade-0151",
            prefix="p0",
        )
        facts_path = root / "p0-facts.json"
        return facts_path, json.loads(facts_path.read_text(encoding="utf-8"))

    @staticmethod
    def _rewrite(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _legacy_v1_fixture(
        self, root: Path
    ) -> tuple[Path, Path, dict[str, object]]:
        facts_path, facts = self._fixture(root)
        producer = {
            "schema_version": receipt.PRODUCER_SCHEMA,
            "tool": str(Path(receipt.__file__).resolve()),
            "tool_sha256": receipt.LEGACY_REPLAY_PRODUCERS["1"],
            "version": "1",
        }
        facts["collector"] = producer
        facts.pop("wireguard", None)
        facts["contract_sha256"] = receipt.LEGACY_NETWORK_CONTRACT_SHA256
        for container in facts["containers"]:
            container["public_egress"][
                "ip_address"
            ] = receipt.LEGACY_DMIT_PUBLIC_EGRESS
        self._rewrite(facts_path, facts)
        legacy_receipt = receipt._build_receipt(
            root,
            facts_path.name,
            replay_producer=producer,
        )
        receipt_path = root / "p0-v1-receipt.json"
        receipt._write_once(receipt_path, legacy_receipt)
        return facts_path, receipt_path, facts

    def _legacy_v3_fixture(
        self, root: Path
    ) -> tuple[Path, Path, dict[str, object]]:
        """构造切换 BWG 前由 v3 生成的 DMIT 环境收据。"""

        facts_path, facts = self._fixture(root)
        producer = {
            "schema_version": receipt.PRODUCER_SCHEMA,
            "tool": str(Path(receipt.__file__).resolve()),
            "tool_sha256": next(
                iter(receipt.REGISTERED_REPLAY_PRODUCER_HASHES["3"])
            ),
            "version": "3",
        }
        facts["collector"] = producer
        facts["contract_sha256"] = receipt.LEGACY_V3_NETWORK_CONTRACT_SHA256
        for container in facts["containers"]:
            container["public_egress"][
                "ip_address"
            ] = receipt.LEGACY_DMIT_PUBLIC_EGRESS
        wireguard = facts["wireguard"]
        wireguard.pop("egress_provider")
        wireguard["expected_dmit_mtu"] = wireguard.pop("expected_mtu")
        self._rewrite(facts_path, facts)
        legacy_receipt = receipt._build_receipt(
            root,
            facts_path.name,
            replay_producer=producer,
        )
        receipt_path = root / "p0-v3-receipt.json"
        receipt._write_once(receipt_path, legacy_receipt)
        return facts_path, receipt_path, facts

    def test_finalize_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = create_arm_receipt(
                root,
                phase="attempt_before",
                subject_id="attempt-001",
                prefix="before",
            )
            replayed = receipt.replay(root, path.name)
            self.assertEqual(replayed["status"], "passed")
            self.assertEqual(replayed["phase"], "attempt_before")
            self.assertEqual(replayed["resource_gate"]["passed"], True)

    def test_bounded_probe_separates_diagnostic_label_from_heartbeat_operation(
        self,
    ) -> None:
        observed: list[str] = []

        def heartbeat(operation: str) -> None:
            operation.encode("ascii")
            observed.append(operation)

        def bounded(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            callback = kwargs["heartbeat"]
            assert callable(callback)
            callback(kwargs["operation"])
            return subprocess.CompletedProcess(argv, 0, b"ok", b"")

        with mock.patch.object(
            receipt.incremental_recovery,
            "run_bounded_subprocess",
            side_effect=bounded,
        ):
            output = receipt._run(
                ["probe"],
                "面向人的中文错误标签",
                operation="arm64:docker-inspect:capture-cli",
                deadline=mock.Mock(),
                heartbeat=heartbeat,
            )

        self.assertEqual(output, b"ok")
        self.assertEqual(observed, ["arm64:docker-inspect:capture-cli"])

    def test_probe_rejects_non_ascii_heartbeat_operation(self) -> None:
        with self.assertRaisesRegex(
            receipt.Arm64EnvironmentReceiptError,
            "heartbeat operation",
        ):
            receipt._run(
                ["probe"],
                "诊断标签",
                operation="ARM64 公网出口查询",
            )

    def test_wrong_fixed_ip_gateway_or_public_egress_fails_closed(self) -> None:
        mutations = (
            ("selected_network", "ipv4_address", "172.25.0.99", "固定网络坐标"),
            ("default_route", "gateway", "172.25.0.99", "默认路由"),
            ("public_egress", "ip_address", "203.0.113.1", "公网出口"),
        )
        for index, (group, field, value, message) in enumerate(mutations):
            with self.subTest(group=group), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                path, facts = self._fixture(root)
                container = next(
                    item for item in facts["containers"] if item["name"] == "sub2apiplus"
                )
                container[group][field] = value
                self._rewrite(path, facts)
                with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, message):
                    receipt.build_receipt(root, "p0-facts.json")

    def test_wg1_mtu_must_match_frozen_bwg_value(self) -> None:
        for field, value in (
            ("configured_mtu", 8920),
            ("runtime_mtu", 8920),
            ("expected_mtu", 8920),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                path, facts = self._fixture(root)
                facts["wireguard"][field] = value
                self._rewrite(path, facts)
                with self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError,
                    "MTU",
                ):
                    receipt.build_receipt(root, "p0-facts.json")

    def test_current_bwg_contract_rejects_previous_dmit_egress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path, facts = self._fixture(root)
            for container in facts["containers"]:
                container["public_egress"][
                    "ip_address"
                ] = receipt.LEGACY_DMIT_PUBLIC_EGRESS
            self._rewrite(path, facts)
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "公网出口",
            ):
                receipt.build_receipt(root, "p0-facts.json")

    def test_disk_watermarks_fail_closed(self) -> None:
        for field, value in (("used_percent", 70), ("available_bytes", 30 * 1024**3 - 1)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                path, facts = self._fixture(root)
                facts["root_filesystem"][field] = value
                self._rewrite(path, facts)
                with self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError, "停线水位"
                ):
                    receipt.build_receipt(root, "p0-facts.json")

    def test_continuity_ignores_docker_restart_ephemeral_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path, facts = self._fixture(root)
            before = receipt.validate_facts(facts)["continuity_identity_sha256"]

            container = next(
                item for item in facts["containers"] if item["name"] == "sub2apiplus"
            )
            container["container_id"] = "f" * 64
            container["default_route"]["interface"] = "eth9"
            container["selected_network"]["endpoint_id"] = "e" * 64
            for binding in container["network_bindings"]:
                binding["endpoint_id"] = (
                    "e" * 64 if binding["name"] == "proxy-network" else "d" * 64
                )
            self._rewrite(path, facts)

            after = receipt.validate_facts(facts)["continuity_identity_sha256"]
            self.assertEqual(before, after)

    def test_continuity_still_binds_network_membership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            _, facts = self._fixture(root)
            before = receipt.validate_facts(facts)["continuity_identity_sha256"]

            container = next(
                item for item in facts["containers"] if item["name"] == "sub2apiplus"
            )
            container["selected_network"]["network_id"] = "c" * 64
            container["network_bindings"][0]["network_id"] = "c" * 64

            after = receipt.validate_facts(facts)["continuity_identity_sha256"]
            self.assertNotEqual(before, after)

    def test_replay_rejects_tampered_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path, facts = self._fixture(root)
            facts["observed_at_utc"] = "2026-08-30T00:00:00Z"
            self._rewrite(path, facts)
            with self.assertRaises(receipt.Arm64EnvironmentReceiptError):
                receipt.replay(root, "p0-receipt.json")

    def test_replay_accepts_registered_v1_producer_without_rewriting_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            facts_path, receipt_path, facts = self._legacy_v1_fixture(root)

            replayed = receipt.replay(root, receipt_path.name)
            self.assertEqual(replayed["producer"]["version"], "1")
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "身份漂移",
            ):
                receipt.build_receipt(root, facts_path.name)

            before = receipt.validate_facts(
                facts,
                allow_legacy_replay=True,
            )["continuity_identity_sha256"]
            container = next(
                item for item in facts["containers"] if item["name"] == "sub2apiplus"
            )
            container["selected_network"]["endpoint_id"] = "e" * 64
            binding = next(
                item
                for item in container["network_bindings"]
                if item["name"] == container["selected_network"]["name"]
            )
            binding["endpoint_id"] = "e" * 64
            after = receipt.validate_facts(
                facts,
                allow_legacy_replay=True,
            )["continuity_identity_sha256"]
            self.assertNotEqual(before, after)

    def test_replay_accepts_registered_v3_dmit_contract(self) -> None:
        """BWG producer 只读承接已登记的 v3 DMIT 收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            facts_path, receipt_path, facts = self._legacy_v3_fixture(root)

            replayed = receipt.replay(root, receipt_path.name)
            self.assertEqual(replayed["producer"]["version"], "3")
            self.assertEqual(
                facts["containers"][0]["public_egress"]["ip_address"],
                receipt.LEGACY_DMIT_PUBLIC_EGRESS,
            )
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "身份漂移",
            ):
                receipt.build_receipt(root, facts_path.name)

    def test_replay_rejects_unregistered_historical_producer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            _, receipt_path, _ = self._legacy_v1_fixture(root)
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload["producer"]["tool_sha256"] = "0" * 64
            self._rewrite(receipt_path, payload)
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "身份漂移",
            ):
                receipt.replay(root, receipt_path.name)

    def test_replay_accepts_relocated_registered_v2_producer(self) -> None:
        """工作树根迁移只改变坐标，不应使历史 ARM64 P0 失效。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            facts_path, _ = self._fixture(root)
            producer = {
                "schema_version": receipt.PRODUCER_SCHEMA,
                "tool": (
                    "/root/retired-codex-worktree/tools/official_client_capture/"
                    "codex_upgrade_arm64_environment_receipt.py"
                ),
                "tool_sha256": (
                    "a62a269e5e4cb0e64aac21e5223ddbde8b884ecbe383b405c560e3c6ebcea527"
                ),
                "version": "2",
            }
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            facts["collector"] = producer
            facts.pop("wireguard", None)
            facts["contract_sha256"] = receipt.LEGACY_NETWORK_CONTRACT_SHA256
            for container in facts["containers"]:
                container["public_egress"][
                    "ip_address"
                ] = receipt.LEGACY_DMIT_PUBLIC_EGRESS
            self._rewrite(facts_path, facts)
            legacy_receipt = receipt._build_receipt(
                root,
                facts_path.name,
                replay_producer=producer,
            )
            receipt_path = root / "p0-relocated-v2-receipt.json"
            receipt._write_once(receipt_path, legacy_receipt)

            replayed = receipt.replay(root, receipt_path.name)
            self.assertEqual(replayed["producer"], producer)
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "身份漂移",
            ):
                receipt.build_receipt(root, facts_path.name)

    def test_replay_rejects_registered_hash_at_wrong_coordinate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            facts_path, _ = self._fixture(root)
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            facts["collector"]["tool"] = "/tmp/codex_upgrade_arm64_environment_receipt.py"
            facts["collector"]["tool_sha256"] = next(
                iter(receipt.REGISTERED_REPLAY_PRODUCER_HASHES["2"])
            )
            self._rewrite(facts_path, facts)
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "身份漂移",
            ):
                receipt.validate_facts(facts, allow_legacy_replay=True)

    def test_contract_has_no_network_override_arguments(self) -> None:
        parser = receipt.build_parser()
        destinations = {
            action.dest
            for subparser in parser._actions
            if getattr(subparser, "choices", None)
            for choice in subparser.choices.values()
            for action in choice._actions
        }
        self.assertNotIn("public_egress_ip", destinations)
        self.assertNotIn("gateway", destinations)
        self.assertNotIn("ipv4_address", destinations)

    def test_schema_matches_runtime_version(self) -> None:
        schema = json.loads(
            Path(receipt.__file__)
            .with_name("codex_upgrade_arm64_environment_receipt.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            receipt.RECEIPT_SCHEMA,
        )
        self.assertEqual(
            schema["properties"]["producer"]["properties"]["version"]["const"],
            receipt.PRODUCER_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
