"""Codex 升级 ARM64 网络与磁盘硬门禁收据测试。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as receipt
from tools.official_client_capture.tests.control_receipt_fixtures import (
    create_arm_receipt,
)


V6_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "arm64_environment_receipt_v6"
ROOT_MIN_AVAILABLE_BYTES = 30 * 1024**3


def _schema_accepts(schema: dict, value: object, node: object | None = None) -> bool:
    """最小 JSON Schema 求值器：只覆盖本收据 schema 用到的关键字，供离线双分支测试。"""

    node = schema if node is None else node
    if node is True:
        return True
    if node is False:
        return False
    assert isinstance(node, dict), node
    if "$ref" in node:
        target = schema
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        if not _schema_accepts(schema, value, target):
            return False
    if "const" in node and value != node["const"]:
        return False
    if "enum" in node and value not in node["enum"]:
        return False
    if "type" in node:
        expected = node["type"]
        checks = {
            "object": lambda item: isinstance(item, dict),
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "boolean": lambda item: isinstance(item, bool),
        }
        if not checks[expected](value):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            return False
        if "maximum" in node and value > node["maximum"]:
            return False
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            return False
        if "pattern" in node and not re.search(node["pattern"], value):
            return False
    if isinstance(value, dict):
        for name in node.get("required", []):
            if name not in value:
                return False
        properties = node.get("properties", {})
        for name, child in properties.items():
            if name in value and not _schema_accepts(schema, value[name], child):
                return False
        if node.get("additionalProperties") is False and set(value) - set(properties):
            return False
    for child in node.get("allOf", []):
        if not _schema_accepts(schema, value, child):
            return False
    if "anyOf" in node and not any(_schema_accepts(schema, value, c) for c in node["anyOf"]):
        return False
    if "oneOf" in node and sum(_schema_accepts(schema, value, c) for c in node["oneOf"]) != 1:
        return False
    if "not" in node and _schema_accepts(schema, value, node["not"]):
        return False
    if "if" in node and _schema_accepts(schema, value, node["if"]):
        if "then" in node and not _schema_accepts(schema, value, node["then"]):
            return False
    return True


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
        facts.pop("rust_tls_readiness", None)
        facts["contract_sha256"] = receipt.LEGACY_NETWORK_CONTRACT_SHA256
        for container in facts["containers"]:
            container.pop("tls_readiness")
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
        facts.pop("rust_tls_readiness", None)
        for container in facts["containers"]:
            container.pop("tls_readiness")
            container["public_egress"][
                "ip_address"
            ] = receipt.LEGACY_DMIT_PUBLIC_EGRESS
        current_wireguard = facts["wireguard"]
        facts["wireguard"] = {
            "interface": current_wireguard["interface"],
            "configured_mtu": current_wireguard["configured_mtu"],
            "runtime_mtu": current_wireguard["runtime_mtu"],
            "expected_dmit_mtu": current_wireguard["expected_mtu"],
            "config_path": current_wireguard["config_path"],
            "config_sha256": current_wireguard["config_sha256"],
        }
        self._rewrite(facts_path, facts)
        legacy_receipt = receipt._build_receipt(
            root,
            facts_path.name,
            replay_producer=producer,
        )
        receipt_path = root / "p0-v3-receipt.json"
        receipt._write_once(receipt_path, legacy_receipt)
        return facts_path, receipt_path, facts

    def _legacy_v4_fixture(
        self, root: Path
    ) -> tuple[Path, Path, dict[str, object]]:
        """构造增强 Endpoint／TLS 门禁前由 v4 生成的 BWG 收据。"""

        facts_path, facts = self._fixture(root)
        producer = {
            "schema_version": receipt.PRODUCER_SCHEMA,
            "tool": str(Path(receipt.__file__).resolve()),
            "tool_sha256": next(
                iter(receipt.REGISTERED_REPLAY_PRODUCER_HASHES["4"])
            ),
            "version": "4",
        }
        facts["collector"] = producer
        facts["contract_sha256"] = receipt.LEGACY_V4_NETWORK_CONTRACT_SHA256
        facts.pop("rust_tls_readiness", None)
        for container in facts["containers"]:
            container.pop("tls_readiness")
        current_wireguard = facts["wireguard"]
        facts["wireguard"] = {
            key: current_wireguard[key]
            for key in (
                "interface",
                "egress_provider",
                "configured_mtu",
                "runtime_mtu",
                "expected_mtu",
                "config_path",
                "config_sha256",
            )
        }
        self._rewrite(facts_path, facts)
        legacy_receipt = receipt._build_receipt(
            root,
            facts_path.name,
            replay_producer=producer,
        )
        receipt_path = root / "p0-v4-receipt.json"
        receipt._write_once(receipt_path, legacy_receipt)
        return facts_path, receipt_path, facts

    def _legacy_v5_fixture(
        self, root: Path
    ) -> tuple[Path, Path, dict[str, object]]:
        """构造仅校验出站 MSS 与 curl TLS 的 v5 BWG 收据。"""

        facts_path, facts = self._fixture(root)
        producer = {
            "schema_version": receipt.PRODUCER_SCHEMA,
            "tool": str(Path(receipt.__file__).resolve()),
            "tool_sha256": next(
                iter(receipt.REGISTERED_REPLAY_PRODUCER_HASHES["5"])
            ),
            "version": "5",
        }
        facts["collector"] = producer
        facts["contract_sha256"] = receipt.LEGACY_V5_NETWORK_CONTRACT_SHA256
        facts.pop("rust_tls_readiness", None)
        for field in (
            "configured_tcpmss_destinations",
            "runtime_tcpmss_destinations",
            "expected_tcpmss_destinations",
        ):
            facts["wireguard"].pop(field)
        self._rewrite(facts_path, facts)
        legacy_receipt = receipt._build_receipt(
            root,
            facts_path.name,
            replay_producer=producer,
        )
        receipt_path = root / "p0-v5-receipt.json"
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

    def test_wg1_endpoint_and_tcpmss_must_match_frozen_bwg_value(self) -> None:
        mutations = (
            ("configured_endpoint", "[2607:8700:5500:d44::2]:51830"),
            ("runtime_endpoint", "[2607:8700:5500:d44::2]:51830"),
            ("expected_endpoint", "bwg.3ab.in:51830"),
            ("configured_tcpmss_sources", ["172.30.0.0/16"]),
            ("runtime_tcpmss_sources", ["172.25.0.3/32"]),
            ("configured_tcpmss_destinations", ["172.30.0.0/16"]),
            ("runtime_tcpmss_destinations", ["172.25.0.3/32"]),
            ("expected_tcp_mss", 1460),
        )
        for field, value in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                path, facts = self._fixture(root)
                facts["wireguard"][field] = value
                self._rewrite(path, facts)
                with self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError,
                    "Endpoint、MTU 或双向 TCPMSS",
                ):
                    receipt.build_receipt(root, "p0-facts.json")

    def test_tls_readiness_requires_three_successes_per_container_and_endpoint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path, facts = self._fixture(root)
            facts["containers"][0]["tls_readiness"][0]["attempts"].pop()
            self._rewrite(path, facts)
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "TLS 连续成功次数不足",
            ):
                receipt.build_receipt(root, "p0-facts.json")

    def test_runtime_tcpmss_parser_rejects_missing_or_duplicate_rule(self) -> None:
        outbound = [
            (
                f"-A FORWARD -s {source} -o wg1 -p tcp -m tcp "
                "--tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"
            )
            for source in receipt.EXPECTED_TCPMSS_SOURCES
        ]
        inbound = [
            (
                f"-A FORWARD -d {destination} -i wg1 -p tcp -m tcp "
                "--tcp-flags SYN,RST SYN -m tcpmss "
                f"--mss {receipt.EXPECTED_TCPMSS_MATCH_RANGE} "
                f"-j TCPMSS --set-mss {receipt.EXPECTED_TCP_MSS}"
            )
            for destination in receipt.EXPECTED_TCPMSS_DESTINATIONS
        ]
        lines = [*outbound, *inbound]
        self.assertEqual(
            receipt._runtime_tcpmss_rules(("\n".join(lines) + "\n").encode()),
            {
                "sources": sorted(receipt.EXPECTED_TCPMSS_SOURCES),
                "destinations": sorted(receipt.EXPECTED_TCPMSS_DESTINATIONS),
            },
        )
        for payload in (outbound, lines[:-1], [*lines, inbound[0]]):
            with self.subTest(line_count=len(payload)), self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "缺失、重复|目标漂移",
            ):
                receipt._runtime_tcpmss_rules(
                    ("\n".join(payload) + "\n").encode()
                )

    def test_expected_tcpmss_config_commands_cover_bidirectional_lifecycle(
        self,
    ) -> None:
        commands = receipt._expected_tcpmss_config_commands()
        self.assertEqual(len(commands), 8)
        self.assertEqual(
            [kind for kind, _command in commands].count("postup"),
            4,
        )
        self.assertEqual(
            [kind for kind, _command in commands].count("postdown"),
            4,
        )
        self.assertEqual(
            sum("--clamp-mss-to-pmtu" in command for _kind, command in commands),
            4,
        )
        self.assertEqual(
            sum("--set-mss 1380" in command for _kind, command in commands),
            4,
        )

    def test_tls_readiness_collector_repeats_both_endpoints(self) -> None:
        outputs = [
            f"{expected_status}\t192.0.2.1\t0.250000\n".encode()
            for _probe_name, _url, expected_status in receipt.TLS_READINESS_PROBES
            for _attempt in range(receipt.TLS_READINESS_ATTEMPTS)
        ]
        with mock.patch.object(receipt, "_run", side_effect=outputs) as runner:
            observed = receipt._tls_readiness_observation("capture-cli")
        self.assertEqual(runner.call_count, len(outputs))
        self.assertEqual(
            [len(item["attempts"]) for item in observed],
            [receipt.TLS_READINESS_ATTEMPTS] * len(receipt.TLS_READINESS_PROBES),
        )

    def test_rust_tls_collector_uses_empty_home_and_accepts_only_no_auth_failure(
        self,
    ) -> None:
        observed_argv: list[str] = []

        def doctor(
            argv: list[str],
            _label: str,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            observed_argv.extend(argv)
            home = next(
                item.split("=", 1)[1]
                for item in argv
                if item.startswith("CODEX_HOME=")
            )
            report = {
                "schemaVersion": 1,
                "codexVersion": receipt.RUST_TLS_PROBE_CODEX_VERSION,
                "overallStatus": "fail",
                "checks": {
                    "auth.credentials": {
                        "status": "fail",
                        "summary": "no Codex credentials were found",
                    },
                    "config.load": {
                        "status": "ok",
                        "details": {"CODEX_HOME": home},
                    },
                    "network.provider_reachability": {"status": "ok"},
                },
            }
            self.assertEqual(kwargs["allowed_returncodes"], frozenset({1}))
            return subprocess.CompletedProcess(
                argv,
                1,
                (json.dumps(report, sort_keys=True) + "\n").encode(),
                b"",
            )

        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory)
            runtime_root.chmod(0o700)
            with (
                mock.patch.object(
                    receipt,
                    "RUST_TLS_PROBE_HOST_RUNTIME_ROOT",
                    runtime_root,
                ),
                mock.patch.object(receipt, "_run_completed", side_effect=doctor),
                mock.patch.object(receipt.time, "monotonic", side_effect=[10.0, 12.5]),
            ):
                observed = receipt._rust_tls_readiness_observation()
            self.assertEqual(list(runtime_root.iterdir()), [])

        self.assertEqual(observed["process_exit_code"], 1)
        self.assertEqual(observed["duration_seconds"], 2.5)
        self.assertEqual(
            observed["checks"]["network.provider_reachability"],
            "ok",
        )
        self.assertIn("/usr/bin/env", observed_argv)
        self.assertIn("-i", observed_argv)
        self.assertFalse(
            any(
                "TOKEN=" in item or "API_KEY=" in item or "AUTH=" in item
                for item in observed_argv
            )
        )

    def test_rust_tls_readiness_rejects_missing_network_or_present_credentials(
        self,
    ) -> None:
        mutations = (
            ("checks", "network.provider_reachability", "fail"),
            ("checks", "auth.credentials", "ok"),
            ("root", "process_exit_code", 0),
            (
                "root",
                "failed_check_ids",
                ["auth.credentials", "network.provider_reachability"],
            ),
            ("root", "codex_version", "0.151.0"),
        )
        for group, field, value in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                path, facts = self._fixture(root)
                if group == "checks":
                    facts["rust_tls_readiness"]["checks"][field] = value
                else:
                    facts["rust_tls_readiness"][field] = value
                self._rewrite(path, facts)
                with self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError,
                    "Rust TLS",
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

    def test_after_phase_below_watermark_records_degraded_receipt(self) -> None:
        """v7：收尾阶段低于水位只记 degraded，收据通过、可重放、连续性身份不变。"""

        for field, value in (
            ("used_percent", 72),
            ("available_bytes", ROOT_MIN_AVAILABLE_BYTES - 2_500_000),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                create_arm_receipt(
                    root, phase="attempt_before", subject_id="attempt-x", prefix="before"
                )
                before = json.loads((root / "before-receipt.json").read_text("utf-8"))
                create_arm_receipt(
                    root, phase="attempt_after", subject_id="attempt-x", prefix="after"
                )
                after_facts_path = root / "after-facts.json"
                facts = json.loads(after_facts_path.read_text(encoding="utf-8"))
                facts["root_filesystem"][field] = value
                self._rewrite(after_facts_path, facts)
                (root / "after-receipt.json").unlink()

                built = receipt.finalize(root, "after-facts.json", "after-receipt.json")
                self.assertEqual(built["status"], "passed")
                self.assertEqual(built["producer"]["version"], "7")
                self.assertEqual(
                    built["resource_gate"],
                    {
                        "used_percent": facts["root_filesystem"]["used_percent"],
                        "available_bytes": facts["root_filesystem"]["available_bytes"],
                        "passed": False,
                        "degraded": True,
                    },
                )
                self.assertEqual(
                    built["continuity_identity_sha256"],
                    before["continuity_identity_sha256"],
                )
                self.assertEqual(receipt.replay(root, "after-receipt.json"), built)

    def test_before_phases_below_watermark_still_fail_closed(self) -> None:
        """准入阶段（p0／*_before）任何版本都不允许降级。"""

        for phase in ("p0", "attempt_before", "kilo_before", "gate_before", "deployment_before"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                create_arm_receipt(root, phase=phase, subject_id="subject-x", prefix="x")
                facts_path = root / "x-facts.json"
                facts = json.loads(facts_path.read_text(encoding="utf-8"))
                facts["root_filesystem"]["available_bytes"] = ROOT_MIN_AVAILABLE_BYTES - 1
                self._rewrite(facts_path, facts)
                with self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError, "停线水位"
                ):
                    receipt.build_receipt(root, "x-facts.json")

    def test_schema_branches_by_producer_version_and_phase(self) -> None:
        """schema 按 producer.version × phase 二维分支：v6 全阶段硬门禁，v7 只有 *_after 可降级。"""

        schema = json.loads(
            Path(receipt.__file__)
            .with_name("codex_upgrade_arm64_environment_receipt.schema.json")
            .read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            create_arm_receipt(root, phase="attempt_after", subject_id="s", prefix="s")
            base = json.loads((root / "s-receipt.json").read_text(encoding="utf-8"))
        passed_gate = {"used_percent": 55, "available_bytes": 47 * 1024**3, "passed": True}
        degraded_gate = {
            "used_percent": 72,
            "available_bytes": 28 * 1024**3,
            "passed": False,
            "degraded": True,
        }

        def variant(version: str, phase: str, gate: dict) -> dict:
            payload = json.loads(json.dumps(base))
            payload["producer"]["version"] = version
            payload["phase"] = phase
            payload["resource_gate"] = gate
            return payload

        self.assertTrue(_schema_accepts(schema, base))
        self.assertTrue(_schema_accepts(schema, variant("6", "attempt_after", passed_gate)))
        self.assertTrue(_schema_accepts(schema, variant("7", "p0", passed_gate)))
        self.assertTrue(_schema_accepts(schema, variant("7", "attempt_after", degraded_gate)))
        self.assertTrue(_schema_accepts(schema, variant("7", "deployment_after", degraded_gate)))
        self.assertFalse(_schema_accepts(schema, variant("6", "attempt_after", degraded_gate)))
        self.assertFalse(_schema_accepts(schema, variant("6", "p0", degraded_gate)))
        self.assertFalse(_schema_accepts(schema, variant("7", "p0", degraded_gate)))
        self.assertFalse(_schema_accepts(schema, variant("7", "attempt_before", degraded_gate)))
        self.assertFalse(_schema_accepts(schema, variant("5", "p0", passed_gate)))
        # degraded 必须与真实低水位一致：水位正常却声称 degraded，或低水位却声称通过，都不合法。
        self.assertFalse(
            _schema_accepts(schema, variant("7", "attempt_after", {**passed_gate, "passed": False, "degraded": True}))
        )
        self.assertFalse(
            _schema_accepts(schema, variant("7", "attempt_after", {**degraded_gate, "passed": True}))
        )
        self.assertFalse(
            _schema_accepts(schema, variant("7", "attempt_after", {"used_percent": 72, "available_bytes": 28 * 1024**3, "passed": True}))
        )

    def test_replays_real_v6_receipts_after_producer_upgrade(self) -> None:
        """冻结夹具：v14 P0 与 v13 attempt_before 的真实 v6 收据在 v7 下只读重放通过。"""

        for name in ("p0", "attempt_before"):
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                for file_name in ("facts.json", "receipt.json"):
                    shutil.copyfile(V6_FIXTURE_ROOT / name / file_name, root / file_name)
                    (root / file_name).chmod(0o600)
                original = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
                self.assertEqual(original["producer"]["version"], "6")
                self.assertIn(
                    original["producer"]["tool_sha256"],
                    receipt.REGISTERED_REPLAY_PRODUCER_HASHES["6"],
                )
                self.assertEqual(
                    original["contract_sha256"],
                    receipt.LEGACY_V6_NETWORK_CONTRACT_SHA256,
                )
                self.assertNotEqual(original["contract_sha256"], receipt.contract_sha256())

                replayed = receipt.replay(root, "receipt.json")
                self.assertEqual(replayed, original)
                self.assertEqual(replayed["phase"], name)
                self.assertEqual(replayed["resource_gate"]["passed"], True)
                # v6 事实不能由 v7 生成新收据：采集器身份已漂移。
                with self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError, "身份漂移"
                ):
                    receipt.build_receipt(root, "facts.json")

    def test_v6_after_phase_keeps_hard_watermark_gate(self) -> None:
        """降级只对 v7 生效：v6 收尾阶段低于水位仍按生成时的硬门禁失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            shutil.copyfile(V6_FIXTURE_ROOT / "attempt_before" / "facts.json", root / "facts.json")
            (root / "facts.json").chmod(0o600)
            facts = json.loads((root / "facts.json").read_text(encoding="utf-8"))
            facts["phase"] = "attempt_after"
            facts["root_filesystem"]["available_bytes"] = ROOT_MIN_AVAILABLE_BYTES - 1
            self._rewrite(root / "facts.json", facts)
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError, "停线水位"
            ):
                receipt._build_receipt(
                    root, "facts.json", replay_producer=facts["collector"]
                )

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

    def test_replay_accepts_registered_v4_bwg_contract(self) -> None:
        """v6 producer 只读承接尚无 Endpoint／TLS 字段的 v4 BWG 收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            facts_path, receipt_path, _ = self._legacy_v4_fixture(root)

            replayed = receipt.replay(root, receipt_path.name)
            self.assertEqual(replayed["producer"]["version"], "4")
            with self.assertRaisesRegex(
                receipt.Arm64EnvironmentReceiptError,
                "身份漂移",
            ):
                receipt.build_receipt(root, facts_path.name)

    def test_replay_accepts_registered_v5_outbound_mss_contract(self) -> None:
        """v6 只读承接缺少回程 MSS 与 Rust TLS 的 v5 BWG 收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            facts_path, receipt_path, facts = self._legacy_v5_fixture(root)

            replayed = receipt.replay(root, receipt_path.name)
            self.assertEqual(replayed["producer"]["version"], "5")
            self.assertNotIn("rust_tls_readiness", facts)
            self.assertNotIn(
                "runtime_tcpmss_destinations",
                facts["wireguard"],
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
            facts.pop("rust_tls_readiness", None)
            facts["contract_sha256"] = receipt.LEGACY_NETWORK_CONTRACT_SHA256
            for container in facts["containers"]:
                container.pop("tls_readiness")
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
        # v7 生成、v6 只读重放：schema 同时接受两个版本，且当前版本必须在其中。
        self.assertEqual(
            schema["properties"]["producer"]["properties"]["version"]["enum"],
            ["6", receipt.PRODUCER_VERSION],
        )
        self.assertEqual(len(schema["allOf"]), 3)


if __name__ == "__main__":
    unittest.main()
