"""Codex 升级 ARM64 网络与磁盘硬门禁收据测试。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as receipt
from tools.official_client_capture.tests import runtime_egress_fixtures
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

    def _legacy_base_fixture(self, root: Path) -> tuple[Path, dict[str, object]]:
        """读取由原始 v7 源码生成的夹具，不用当前 producer 追认旧事实。"""
        source = Path(__file__).parent / "fixtures/arm64_environment_receipt_v7/p0/legacy-facts.json"
        facts = json.loads(source.read_text())
        path = root / "p0-facts.json"
        self._rewrite(path, facts)
        return path, facts

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
        facts_path, facts = self._legacy_base_fixture(root)
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

        facts_path, facts = self._legacy_base_fixture(root)
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

        facts_path, facts = self._legacy_base_fixture(root)
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

        facts_path, facts = self._legacy_base_fixture(root)
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
                path, facts = self._legacy_base_fixture(root)
                facts["wireguard"][field] = value
                self._rewrite(path, facts)
                with self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError,
                    "MTU",
                ):
                    receipt.validate_facts(facts, allow_legacy_replay=True)

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
                path, facts = self._legacy_base_fixture(root)
                facts["wireguard"][field] = value
                self._rewrite(path, facts)
                with self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError,
                    "Endpoint、MTU 或双向 TCPMSS",
                ):
                    receipt.validate_facts(facts, allow_legacy_replay=True)

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
        outputs = iter(
            f"{expected_status}\t192.0.2.1\t0.250000\n".encode()
            for _probe_name, _url, expected_status in receipt.TLS_READINESS_PROBES
            for _attempt in range(receipt.TLS_READINESS_ATTEMPTS)
        )
        expected_calls = len(receipt.TLS_READINESS_PROBES) * receipt.TLS_READINESS_ATTEMPTS

        # 第 64 项起探针经 _run_completed 取得退出码与原文，以便区分网络瞬态；无瞬态时逐次调用次数不变。
        def run(argv: list[str], _label: str, _timeout: int = 30, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(
                kwargs["allowed_returncodes"],
                frozenset({0}) | receipt.TLS_READINESS_TRANSIENT_CURL_EXIT_CODES,
            )
            return subprocess.CompletedProcess(argv, 0, next(outputs), b"")

        with mock.patch.object(receipt, "_run_completed", side_effect=run) as runner:
            observed = receipt._tls_readiness_observation("capture-cli")
        self.assertEqual(runner.call_count, expected_calls)
        self.assertEqual(
            [len(item["attempts"]) for item in observed],
            [receipt.TLS_READINESS_ATTEMPTS] * len(receipt.TLS_READINESS_PROBES),
        )

    def test_rust_tls_collector_uses_empty_home_and_accepts_only_no_auth_failure(
        self,
    ) -> None:
        observed_argv: list[str] = []
        # v8 探针目标来自采集参数：本轮目标版本 0.156.1 派生容器内二进制路径。
        target = receipt.rust_tls_probe_target("0.156.1")
        reported_version = "0.156.1"

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
                "codexVersion": reported_version,
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
                observed = receipt._rust_tls_readiness_observation(target)
            self.assertEqual(list(runtime_root.iterdir()), [])
            # 二进制自报版本与本轮目标不一致时拒绝，不能用旧客户端冒充目标版本的 TLS 就绪。
            reported_version = "0.154.0"
            with (
                mock.patch.object(receipt, "RUST_TLS_PROBE_HOST_RUNTIME_ROOT", runtime_root),
                mock.patch.object(receipt, "_run_completed", side_effect=doctor),
                mock.patch.object(receipt.time, "monotonic", side_effect=[10.0, 12.5]),
                self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "版本"),
            ):
                receipt._rust_tls_readiness_observation(target)

        self.assertIn("/opt/codex-0.156.1/bin/codex", observed_argv)
        self.assertEqual((observed["binary"], observed["codex_version"]), ("/opt/codex-0.156.1/bin/codex", "0.156.1"))

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

    def test_rust_tls_probe_target_comes_from_collect_argument(self) -> None:
        """v8 收据记录采集参数给出的目标版本；非法版本在采集前拒绝，历史常量只供 v6～v7 重放。"""

        self.assertEqual(
            receipt.rust_tls_probe_target("0.156.1"),
            {"container": "capture-cli", "binary": "/opt/codex-0.156.1/bin/codex", "codex_version": "0.156.1"},
        )
        for invalid in ("0.156", "v0.156.1", "0.156.1-alpha", "", None):
            with self.subTest(version=invalid), self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "Rust TLS"):
                receipt.rust_tls_probe_target(invalid)
        self.assertNotIn("0.154.0", json.dumps(receipt.contract_sha256()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = create_arm_receipt(root, phase="p0", subject_id="upgrade-x", prefix="p0",
                                      rust_tls_codex_version="0.156.1")
            built = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(built["rust_tls_probe"], {"binary": "/opt/codex-0.156.1/bin/codex", "codex_version": "0.156.1"})
            self.assertEqual(receipt.replay(root, path.name), built)

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
            ("root", "codex_version", "0.156"),
            ("root", "binary", "/opt/codex-0.156.1/bin/codex"),
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

    def test_current_runtime_policy_rejects_unapproved_legacy_egress(self) -> None:
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
                self.assertEqual(built["producer"]["version"], receipt.PRODUCER_VERSION)
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
            if version != "8":
                payload.pop("environment_equivalence", None)
                payload.pop("runtime_egress", None)
                payload.pop("rust_tls_probe", None)
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
        # v8 必须携带 Rust TLS 探针目标摘要；历史 v6～v7 收据不得出现该字段。
        missing_probe = json.loads(json.dumps(base))
        missing_probe.pop("rust_tls_probe")
        self.assertFalse(_schema_accepts(schema, missing_probe))
        self.assertFalse(_schema_accepts(schema, {**variant("7", "p0", passed_gate), "rust_tls_probe": base["rust_tls_probe"]}))
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
            facts["runtime_egress"]["runtime"]["services"]["sub2apiplus"]["container_id"] = "f" * 64
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
            facts_path, _ = self._legacy_base_fixture(root)
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

    def test_replays_v8_receipts_generated_before_maintenance_wait_fix(self) -> None:
        """修好接着跑第 36 项：维护等待口径的修复只改校验、不改事实合同，修复前 producer（9e10bd0f…，r17 树）生成的
        194249z P0／attempt 收据在当前 producer 下只读重放通过；旧身份不能再生成新收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            facts_path, facts = self._fixture(root)
            producer = dict(facts["collector"])
            producer["tool"] = (
                "/root/docker/capture-cli/data/tools/official_client_capture/"
                "codex_upgrade_arm64_environment_receipt.py"
            )
            producer["tool_sha256"] = "9e10bd0f91b588ee6aefd0ab06ac845c248faaa45b75c906bd5df91933845f98"
            self.assertEqual(producer["version"], "8")
            self.assertIn(producer["tool_sha256"], receipt.REGISTERED_REPLAY_PRODUCER_HASHES["8"])
            self.assertNotEqual(producer["tool_sha256"], receipt._current_producer()["tool_sha256"])
            facts["collector"] = producer
            self._rewrite(facts_path, facts)
            legacy_receipt = receipt._build_receipt(root, facts_path.name, replay_producer=producer)
            receipt_path = root / "p0-pre-item36-receipt.json"
            receipt._write_once(receipt_path, legacy_receipt)

            replayed = receipt.replay(root, receipt_path.name)
            self.assertEqual(replayed, legacy_receipt)
            self.assertEqual(replayed["producer"], producer)
            self.assertEqual(replayed["contract_sha256"], receipt.contract_sha256())
            self.assertIn("environment_equivalence", replayed)
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "身份漂移"):
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

    def test_r15_policy_switch_preserves_equivalence_and_history_without_runtime_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            _, facts = self._fixture(root)
            expected = receipt.validate_facts(facts)["equivalence_identity_sha256"]
            source = Path(__file__).parent / "fixtures/arm64_environment_receipt_v7/p0"
            legacy_root = root / "history"
            shutil.copytree(source, legacy_root)
            legacy_root.chmod(0o700)
            for path in legacy_root.iterdir():
                path.chmod(0o600)
            frozen = {path.name: path.read_bytes() for path in legacy_root.iterdir()}
            with mock.patch.object(receipt, "require_runtime_egress", side_effect=AssertionError("历史重放不得读取当前出口")):
                old = receipt.replay(legacy_root, "legacy-receipt.json")
                self.assertEqual(receipt.receipt_equivalence_sha256(legacy_root, old), expected)
                for address, mtu in (("144.34.230.210", 1420), ("69.63.195.102", 1360), ("1.0.0.1", 1380)):
                    changed = json.loads(json.dumps(facts))
                    runtime = changed["runtime_egress"]
                    policy = runtime["policy"]
                    policy["allowed_public_ipv4"] = [address]
                    policy["revision"] += 1
                    policy["nodes"]["exit"]["endpoint"]["ipv4"] = address
                    for node in policy["nodes"].values():
                        node["mtu"] = mtu
                    digest = receipt._sha256_bytes(receipt._canonical(policy))
                    runtime["policy_sha256"] = runtime["runtime"]["policy_sha256"] = digest
                    for service in runtime["runtime"]["services"].values():
                        for observation in service["observations"]:
                            observation["ip_address"] = address
                    for container in changed["containers"]:
                        container["public_egress"]["ip_address"] = address
                    self.assertEqual(receipt.validate_facts(changed)["equivalence_identity_sha256"], expected)
            self.assertEqual(frozen, {path.name: path.read_bytes() for path in legacy_root.iterdir()})

    def test_r15_registered_v7_after_phase_keeps_original_degraded_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "history"
            shutil.copytree(Path(__file__).parent / "fixtures/arm64_environment_receipt_v7/attempt_after", root)
            root.chmod(0o700)
            for path in root.iterdir():
                path.chmod(0o600)
            replayed = receipt.replay(root, "legacy-receipt.json")
            self.assertTrue(replayed["resource_gate"]["degraded"])
            facts = json.loads((root / "legacy-facts.json").read_text())
            facts["phase"] = "attempt_before"
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "根文件系统"):
                receipt.validate_facts(facts, allow_legacy_replay=True)

    def test_r15_probe_quorum_conflict_staleness_and_individual_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, facts = self._fixture(Path(directory))
            runtime = facts["runtime_egress"]
            policy, status = runtime["policy"], runtime["runtime"]
            now = status["observed_at_epoch"]
            first = status["services"]["capture-cli"]["observations"][0]
            first.update({"status": "failed", "ip_address": None, "response_sha256": None})
            receipt.validate_egress_status(policy, status, now_epoch=now)
            status["services"]["capture-cli"]["observations"][1]["ip_address"] = "8.8.8.8"
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "capture-cli"):
                receipt.validate_egress_status(policy, status, now_epoch=now)
            self.assertTrue(receipt.egress_observations_compliant(policy, status["services"]["sub2apiplus"]["observations"], now_epoch=now))
            for service in status["services"].values():
                for observation in service["observations"]:
                    observation.update({"status": "passed", "ip_address": policy["allowed_public_ipv4"][0], "response_sha256": "a" * 64})
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "过期"):
                receipt.validate_egress_status(policy, status, now_epoch=now + policy["lease_seconds"] + .01)
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "租期"):
                receipt.validate_egress_status(policy, status, now_epoch=now, now_monotonic_ns=status["valid_until_monotonic_ns"] + 1)
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "启动周期"):
                receipt.validate_egress_status(policy, status, now_epoch=now, boot_id="another-boot")
            status["shared_protection"]["checks"]["remote_guard"] = False
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "两个容器"):
                receipt.validate_egress_status(policy, status, now_epoch=now)

    def test_r15_equivalence_rejects_tampering_and_keeps_real_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, facts = self._fixture(root)
            current = receipt.replay(root, "p0-receipt.json")
            original = receipt.receipt_equivalence_sha256(root, current)
            changed = json.loads(json.dumps(current))
            changed["continuity_identity_sha256"] = "f" * 64
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "原 producer"):
                receipt.receipt_equivalence_sha256(root, changed)
            for key, value in (("image_id", "sha256:" + "a" * 64), ("image_id", "sha256:" + "b" * 64)):
                changed_facts = json.loads(json.dumps(facts))
                changed_facts["containers"][0][key] = value
                self.assertNotEqual(receipt.validate_facts(changed_facts)["equivalence_identity_sha256"], original)
            facts["containers"][0]["selected_network"]["network_id"] = "b" * 64
            facts["containers"][0]["network_bindings"][0]["network_id"] = "b" * 64
            self.assertNotEqual(receipt.validate_facts(facts)["equivalence_identity_sha256"], original)

    def test_equivalence_requires_complete_replayable_receipts(self) -> None:
        """缺 facts／producer 字段、证据根只剩收据、facts 被改动时，等价比较抛出而不返回真假。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts_path, facts = self._fixture(root)
            current = receipt.replay(root, "p0-receipt.json")
            self.assertTrue(receipt.receipts_equivalent(root, current, root, current))
            for field in ("facts", "producer"):
                partial = {key: value for key, value in current.items() if key != field}
                with self.subTest(missing=field), self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError, "缺少完整的 facts／producer 收据",
                ):
                    receipt.receipts_equivalent(root, current, root, partial)
            facts["host"]["hostname"] = "arm64-changed"
            self._rewrite(facts_path, facts)
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "未经原 producer 完整重放"):
                receipt.receipts_equivalent(root, current, root, current)
            facts_path.unlink()
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "facts不是可信普通文件"):
                receipt.receipts_equivalent(root, current, root, current)

    def test_runtime_egress_read_locations_are_resolved_at_call_time(self) -> None:
        """准入的三个读取位置在调用时解析：统一夹具重定向后，无参调用只读私有临时目录并失败关闭。"""

        defaults = (receipt.EGRESS_POLICY_PATH, receipt.EGRESS_STATUS_PATH, receipt.EGRESS_BOOT_ID_PATH)
        self.assertEqual(defaults, (
            Path("/etc/sub2api-egress/policy.json"),
            Path("/run/sub2api-egress/status.json"),
            Path("/proc/sys/kernel/random/boot_id"),
        ))
        original = receipt._read_egress_runtime_json
        with runtime_egress_fixtures.isolated_runtime_egress_paths() as absent:
            seen: list[Path] = []

            def spy(path: Path, *, private: bool) -> dict[str, object]:
                seen.append(Path(path))
                return original(path, private=private)

            with mock.patch.object(receipt, "_read_egress_runtime_json", side_effect=spy):
                with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "运行时出口准入拒绝"):
                    receipt.require_runtime_egress()
                with self.assertRaises(OSError):
                    receipt.load_egress_policy()
                with mock.patch.object(receipt, "load_egress_policy", return_value={}), \
                        self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "运行时出口准入拒绝"):
                    receipt.require_runtime_egress()
            self.assertEqual(seen, [absent / "policy.json", absent / "policy.json", absent / "status.json"])
            absent.mkdir()
            (absent / "boot_id").write_text("00000000-0000-0000-0000-000000000009\n", encoding="ascii")
            with mock.patch.object(receipt, "load_egress_policy", return_value={}), \
                    mock.patch.object(receipt, "_read_egress_runtime_json", return_value={"policy_sha256": "0" * 64}), \
                    mock.patch.object(receipt, "validate_egress_status") as validate:
                receipt.require_runtime_egress()
            self.assertEqual(validate.call_args.kwargs["boot_id"], "00000000-0000-0000-0000-000000000009")
        self.assertEqual(
            (receipt.EGRESS_POLICY_PATH, receipt.EGRESS_STATUS_PATH, receipt.EGRESS_BOOT_ID_PATH), defaults,
        )

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
            ["6", "7", receipt.PRODUCER_VERSION],
        )
        self.assertEqual(len(schema["allOf"]), 5)


# 假 docker：只模拟 ``docker exec <容器> /usr/bin/curl … <URL>``，按"容器＋探针"分别计数。
# ``$FAKE_DOCKER_STATE/plan-<容器>-<探针>`` 每行是一次调用的计划 ``退出码|内容``：退出码非 0 时把内容写到
# 标准错误并以该码退出（模拟 curl 失败原文）；退出码为 0 时按 printf %b 输出内容（模拟 HTTP 状态不符、响应
# 非法等）。计划用尽后的调用一律按成功返回冻结端点的 401 响应。每次调用都追加一行到 calls.log。
FAKE_DOCKER_SCRIPT = r"""#!/bin/sh
set -eu
state="${FAKE_DOCKER_STATE:?}"
if [ "$1" != "exec" ]; then
  echo "fake docker: unexpected subcommand $1" >&2
  exit 97
fi
container="$2"
url=""
for arg in "$@"; do url="$arg"; done
case "$url" in
  https://chatgpt.com/backend-api/wham/config/bundle) probe="chatgpt-cloud-config" ;;
  https://api.openai.com/v1/models) probe="openai-models" ;;
  *) echo "fake docker: unexpected url $url" >&2; exit 97 ;;
esac
counter="$state/count-$container-$probe"
count=0
if [ -f "$counter" ]; then count=$(cat "$counter"); fi
count=$((count + 1))
printf '%s\n' "$count" > "$counter"
printf '%s %s %s\n' "$container" "$probe" "$count" >> "$state/calls.log"
plan="$state/plan-$container-$probe"
line=""
if [ -f "$plan" ]; then line=$(sed -n "${count}p" "$plan"); fi
if [ -n "$line" ]; then
  code="${line%%|*}"
  body="${line#*|}"
  if [ "$code" = "0" ]; then
    printf '%b' "$body"
    exit 0
  fi
  printf '%s\n' "$body" >&2
  exit "$code"
fi
printf '401\t104.18.32.47\t0.250000\n'
"""


class TlsReadinessTransientRetryTests(unittest.TestCase):
    """修好接着跑第 64 项：TLS 就绪探针遇网络瞬态有界重试（假 docker 真实子进程驱动）。

    现场：2026-09-28 13:54Z，Campaign c01570-formal-vc1-r2-20260926t194249z 的 VC-5 批次 26 新 attempt
    20260928T135332Z-b23de48cb06bde03 在任何作业开始前的环境收据阶段失败，动作诊断原文为
    "capture-cli chatgpt-cloud-config TLS 就绪探针第 1 次失败：curl: (28) Resolving timed out after 6000
    milliseconds"；随后在容器内解析 chatgpt.com 即时成功，属网络瞬态。
    """

    DNS_TIMEOUT = "curl: (28) Resolving timed out after 6000 milliseconds"

    @staticmethod
    def _fake_docker(
        directory: Path, plans: dict[tuple[str, str], list[str]]
    ) -> tuple[Path, Path]:
        """在临时目录放置假 docker 与逐次调用计划，返回（可执行目录, 状态目录）。"""

        fake_bin = directory / "fake-bin"
        state = directory / "fake-docker-state"
        fake_bin.mkdir()
        state.mkdir()
        docker = fake_bin / "docker"
        docker.write_text(FAKE_DOCKER_SCRIPT, encoding="utf-8")
        docker.chmod(0o755)
        for (container, probe), lines in plans.items():
            (state / f"plan-{container}-{probe}").write_text(
                "".join(f"{line}\n" for line in lines), encoding="utf-8"
            )
        return fake_bin, state

    @staticmethod
    def _fake_environment(fake_bin: Path, state: Path) -> Any:
        """让 ``docker`` 解析到假脚本；只作用于本测试进程及其子进程。"""

        return mock.patch.dict(
            os.environ,
            {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "FAKE_DOCKER_STATE": str(state),
            },
        )

    @staticmethod
    def _calls(state: Path) -> list[tuple[str, str, int]]:
        """按调用顺序返回（容器, 探针, 该探针第几次调用）。"""

        path = state / "calls.log"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            container, probe, count = line.split()
            rows.append((container, probe, int(count)))
        return rows

    def test_first_curl_dns_timeout_retries_and_restarts_consecutive_successes(
        self,
    ) -> None:
        """复现现场：第 1 次 curl 以退出码 28 报 DNS 解析超时、之后成功。

        修复前第 1 次失败即抛"TLS 就绪探针第 1 次失败：curl: (28) …"，环境收据整批失败；修复后退避一次、
        清零连续计数重新验证，最终每个探针仍是连续 3 次成功。瞬态按最小方案只进 ARM64 环境收据日志：
        facts／收据字段与无瞬态时同形（编号 1..3），按 v8 合同 build_receipt 照常通过。
        """

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            fake_bin, state = self._fake_docker(
                workspace,
                {("capture-cli", "chatgpt-cloud-config"): [f"28|{self.DNS_TIMEOUT}"]},
            )
            log = io.StringIO()
            with (
                self._fake_environment(fake_bin, state),
                contextlib.redirect_stderr(log),
                # 退避秒数置零只为测试提速；默认退避序列由单独用例锁定。
                mock.patch.object(
                    receipt, "TLS_READINESS_TRANSIENT_BACKOFF_SECONDS", (0, 0, 0), create=True
                ),
            ):
                observed = receipt._tls_readiness_observation("capture-cli")
            calls = self._calls(state)

            # 1 次瞬态 + 连续 3 次成功；另一个端点不受影响。
            probes = [probe for _container, probe, _count in calls]
            self.assertEqual(probes.count("chatgpt-cloud-config"), 4)
            self.assertEqual(probes.count("openai-models"), receipt.TLS_READINESS_ATTEMPTS)
            self.assertEqual(
                [(item["name"], [attempt["attempt"] for attempt in item["attempts"]]) for item in observed],
                [
                    (name, list(range(1, receipt.TLS_READINESS_ATTEMPTS + 1)))
                    for name, _url, _status in receipt.TLS_READINESS_PROBES
                ],
            )
            self.assertTrue(
                all(
                    attempt["http_status"] == 401
                    for item in observed
                    for attempt in item["attempts"]
                )
            )

            # 瞬态进入 ARM64 环境收据日志：退出码与现场原文都在。
            text = log.getvalue()
            self.assertIn("capture-cli chatgpt-cloud-config TLS 就绪探针网络瞬态第 1 次", text)
            self.assertIn("退出码 28", text)
            self.assertIn(self.DNS_TIMEOUT, text)
            self.assertIn("在 1 次网络瞬态后连续 3 次通过", text)

            # 收据形态不变：把观测放进完整 v8 facts，仍按当前合同封存与重放。
            root = workspace / "evidence"
            root.mkdir(mode=0o700)
            create_arm_receipt(root, phase="attempt_before", subject_id="attempt-064", prefix="before")
            facts_path = root / "before-facts.json"
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            capture = next(item for item in facts["containers"] if item["name"] == "capture-cli")
            capture["tls_readiness"] = observed
            Arm64EnvironmentReceiptTests._rewrite(facts_path, facts)
            built = receipt.build_receipt(root, facts_path.name)
            self.assertEqual(built["producer"]["version"], "8")
            self.assertEqual(built["contract_sha256"], receipt.contract_sha256())

    def test_transient_failures_beyond_retry_limit_fail_with_each_exit_code(self) -> None:
        """第 4 次瞬态即失败；报错逐条带退出码与原文摘要，且整条能被动作失败诊断原样保留。"""

        from tools.official_client_capture import codex_upgrade_supervisor

        long_connect = (
            "curl: (7) Failed to connect to chatgpt.com port 443 after 3002 ms: "
            "Couldn't connect to server " + "x" * 200
        )
        plan = [
            f"28|{self.DNS_TIMEOUT}",
            "6|curl: (6) Could not resolve host: chatgpt.com",
            f"7|{long_connect}",
            "28|curl: (28) Connection timed out after 6001 milliseconds",
        ]
        with tempfile.TemporaryDirectory() as directory:
            fake_bin, state = self._fake_docker(
                Path(directory), {("capture-cli", "chatgpt-cloud-config"): plan}
            )
            log = io.StringIO()
            with (
                self._fake_environment(fake_bin, state),
                contextlib.redirect_stderr(log),
                mock.patch.object(receipt, "_transient_backoff") as backoff,
                self.assertRaises(receipt.Arm64EnvironmentReceiptError) as raised,
            ):
                receipt._tls_readiness_observation("capture-cli")
            calls = self._calls(state)

        message = str(raised.exception)
        self.assertTrue(
            message.startswith(
                "capture-cli chatgpt-cloud-config TLS 就绪探针网络瞬态超过重试上限 3 次：#1 退出码 28"
            ),
            message,
        )
        for index, code in enumerate((28, 6, 7, 28), 1):
            self.assertIn(f"#{index} 退出码 {code}「", message)
        self.assertIn(f"「{self.DNS_TIMEOUT}」", message)
        self.assertIn("Could not resolve host: chatgpt.com", message)
        self.assertIn("Connection timed out after 6001 milliseconds", message)
        self.assertNotIn("x" * 100, message)
        self.assertLessEqual(len(message), receipt.TLS_READINESS_TRANSIENT_MESSAGE_LIMIT)
        # 动作失败诊断超过 512 字或含敏感来源标签会整段改写；这里必须原样保留。
        self.assertEqual(codex_upgrade_supervisor._action_diagnostic_message(message), message)
        # 默认退避序列 2、4、8 秒各用一次；失败即停止，不再去探下一个端点。
        self.assertEqual([item.args[0] for item in backoff.call_args_list], [2, 4, 8])
        self.assertEqual(
            [item.kwargs["operation"] for item in backoff.call_args_list],
            ["arm64:tls-ready:capture-cli:chatgpt-cloud-config:backoff"] * 3,
        )
        self.assertEqual(
            [(probe, count) for _container, probe, count in calls],
            [("chatgpt-cloud-config", count) for count in range(1, 5)],
        )
        self.assertEqual(log.getvalue().count("网络瞬态第"), 3)

    def test_transient_restarts_consecutive_count_after_partial_successes(self) -> None:
        """成功、成功、瞬态之后必须重新连续成功 3 次；瞬态前的成功不计入最终连续次数。"""

        success = "0|401\\t104.18.32.47\\t0.250000\\n"
        with tempfile.TemporaryDirectory() as directory:
            fake_bin, state = self._fake_docker(
                Path(directory),
                {
                    ("capture-cli", "openai-models"): [
                        success,
                        success,
                        "7|curl: (7) Failed to connect to api.openai.com port 443: Connection refused",
                    ]
                },
            )
            log = io.StringIO()
            with (
                self._fake_environment(fake_bin, state),
                contextlib.redirect_stderr(log),
                mock.patch.object(receipt, "_transient_backoff") as backoff,
            ):
                observed = receipt._tls_readiness_observation("capture-cli")
            calls = self._calls(state)

        probes = [probe for _container, probe, _count in calls]
        self.assertEqual(probes.count("chatgpt-cloud-config"), 3)
        self.assertEqual(probes.count("openai-models"), 6)
        openai = next(item for item in observed if item["name"] == "openai-models")
        self.assertEqual([attempt["attempt"] for attempt in openai["attempts"]], [1, 2, 3])
        self.assertEqual([item.args[0] for item in backoff.call_args_list], [2])
        self.assertIn("本轮连续第 3 次尝试", log.getvalue())

    def test_non_transient_failures_fail_immediately_with_unchanged_message(self) -> None:
        """证书错误、TLS 握手出错、docker 自身错误、HTTP 状态不符、响应非法：不重试，报错与修复前逐字一致。"""

        cases = (
            (
                "60|curl: (60) SSL certificate problem: unable to get local issuer certificate",
                "capture-cli chatgpt-cloud-config TLS 就绪探针第 1 次失败：curl: (60) SSL certificate problem: "
                "unable to get local issuer certificate",
            ),
            (
                "35|curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to chatgpt.com:443",
                "capture-cli chatgpt-cloud-config TLS 就绪探针第 1 次失败：curl: (35) OpenSSL SSL_connect: "
                "SSL_ERROR_SYSCALL in connection to chatgpt.com:443",
            ),
            (
                "125|Error response from daemon: No such container: capture-cli",
                "capture-cli chatgpt-cloud-config TLS 就绪探针第 1 次失败：Error response from daemon: "
                "No such container: capture-cli",
            ),
            (
                "0|403\\t104.18.32.47\\t0.250000\\n",
                "capture-cli chatgpt-cloud-config TLS 就绪探针未通过",
            ),
            ("0|garbage\\n", "capture-cli chatgpt-cloud-config TLS 就绪探针响应非法"),
        )
        for line, expected in cases:
            with self.subTest(plan=line), tempfile.TemporaryDirectory() as directory:
                fake_bin, state = self._fake_docker(
                    Path(directory), {("capture-cli", "chatgpt-cloud-config"): [line]}
                )
                log = io.StringIO()
                with (
                    self._fake_environment(fake_bin, state),
                    contextlib.redirect_stderr(log),
                    mock.patch.object(receipt, "_transient_backoff") as backoff,
                    self.assertRaises(receipt.Arm64EnvironmentReceiptError) as raised,
                ):
                    receipt._tls_readiness_observation("capture-cli")
                self.assertEqual(str(raised.exception), expected)
                self.assertEqual(len(self._calls(state)), 1)
                backoff.assert_not_called()
                self.assertEqual(log.getvalue(), "")

    def test_non_transient_failure_after_transient_still_fails_immediately(self) -> None:
        """瞬态之后出现证书错误：证书错误照旧立即失败，不因为之前有过瞬态而继续重试。"""

        with tempfile.TemporaryDirectory() as directory:
            fake_bin, state = self._fake_docker(
                Path(directory),
                {
                    ("capture-cli", "chatgpt-cloud-config"): [
                        f"28|{self.DNS_TIMEOUT}",
                        "60|curl: (60) SSL certificate problem: certificate has expired",
                    ]
                },
            )
            with (
                self._fake_environment(fake_bin, state),
                contextlib.redirect_stderr(io.StringIO()),
                mock.patch.object(receipt, "_transient_backoff") as backoff,
                self.assertRaisesRegex(
                    receipt.Arm64EnvironmentReceiptError,
                    r"^capture-cli chatgpt-cloud-config TLS 就绪探针第 1 次失败：curl: \(60\) "
                    r"SSL certificate problem: certificate has expired$",
                ),
            ):
                receipt._tls_readiness_observation("capture-cli")
            self.assertEqual(len(self._calls(state)), 2)
            self.assertEqual(backoff.call_count, 1)

    def test_transient_retry_stops_at_total_window(self) -> None:
        """退避后会越过每个探针的瞬态总时限时不再重试，报错带已发生的瞬态摘要。"""

        def run(argv: list[str], _label: str, _timeout: int = 30, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 28, b"", f"{self.DNS_TIMEOUT}\n".encode())

        window = receipt.TLS_READINESS_TRANSIENT_WINDOW_SECONDS
        with (
            mock.patch.object(receipt, "_run_completed", side_effect=run) as runner,
            mock.patch.object(receipt, "_transient_backoff") as backoff,
            # 探针起点 0 秒；第 1 次瞬态判定时已过 window-1 秒，再退避 2 秒就越过总时限。
            mock.patch.object(receipt.time, "monotonic", side_effect=[0.0, float(window - 1)]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(receipt.Arm64EnvironmentReceiptError) as raised,
        ):
            receipt._tls_readiness_observation("capture-cli")
        self.assertEqual(
            str(raised.exception),
            f"capture-cli chatgpt-cloud-config TLS 就绪探针网络瞬态重试将超过总时限 {window} 秒："
            f"#1 退出码 28「{self.DNS_TIMEOUT}」",
        )
        self.assertEqual(runner.call_count, 1)
        backoff.assert_not_called()

    def test_backoff_runs_inside_shared_attempt_deadline(self) -> None:
        """有受管 deadline 时退避走 deadline.sleep；预算到期的 WallClockTimeoutError 原样上抛，不当作瞬态。"""

        deadline = mock.Mock()
        with mock.patch.object(receipt, "_ACTIVE_DEADLINE", deadline), mock.patch.object(receipt.time, "sleep") as sleep:
            receipt._transient_backoff(4, operation="arm64:tls-ready:capture-cli:openai-models:backoff")
        deadline.sleep.assert_called_once_with(4, operation="arm64:tls-ready:capture-cli:openai-models:backoff")
        sleep.assert_not_called()
        with mock.patch.object(receipt, "_ACTIVE_DEADLINE", None), mock.patch.object(receipt.time, "sleep") as sleep:
            receipt._transient_backoff(8, operation="arm64:tls-ready:capture-cli:openai-models:backoff")
        sleep.assert_called_once_with(8)

        expired = receipt.incremental_recovery.WallClockTimeoutError(
            "arm64:tls-ready:capture-cli:chatgpt-cloud-config:backoff",
            elapsed_seconds=10.0,
            budget_seconds=10.0,
        )
        deadline = mock.Mock()
        deadline.sleep.side_effect = expired

        def run(argv: list[str], _label: str, _timeout: int = 30, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 28, b"", f"{self.DNS_TIMEOUT}\n".encode())

        with (
            mock.patch.object(receipt, "_ACTIVE_DEADLINE", deadline),
            mock.patch.object(receipt, "_run_completed", side_effect=run),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(receipt.incremental_recovery.WallClockTimeoutError) as raised,
        ):
            receipt._tls_readiness_observation("capture-cli")
        self.assertIs(raised.exception, expired)

    def test_transient_retry_through_real_bounded_subprocess_with_deadline(self) -> None:
        """生产路径：collect_facts 把 attempt deadline 设为 _ACTIVE_DEADLINE 后，curl 改经 run_bounded_subprocess
        执行（这里直接设置同一作用域），瞬态同样有界重试，退避走真实 WallClockDeadline.sleep。"""

        with tempfile.TemporaryDirectory() as directory:
            fake_bin, state = self._fake_docker(
                Path(directory),
                {("sub2apiplus", "chatgpt-cloud-config"): [f"28|{self.DNS_TIMEOUT}"]},
            )
            deadline = receipt.incremental_recovery.WallClockDeadline(60, label="item64-test")
            with (
                self._fake_environment(fake_bin, state),
                contextlib.redirect_stderr(io.StringIO()),
                mock.patch.object(receipt, "_ACTIVE_DEADLINE", deadline),
                mock.patch.object(receipt, "TLS_READINESS_TRANSIENT_BACKOFF_SECONDS", (0, 0, 0)),
                mock.patch.object(
                    receipt.incremental_recovery,
                    "run_bounded_subprocess",
                    wraps=receipt.incremental_recovery.run_bounded_subprocess,
                ) as bounded,
            ):
                observed = receipt._tls_readiness_observation("sub2apiplus")
            probes = [probe for _container, probe, _count in self._calls(state)]
        self.assertEqual(probes.count("chatgpt-cloud-config"), 4)
        self.assertEqual(bounded.call_count, 7)
        self.assertTrue(all(call.kwargs["deadline"] is deadline for call in bounded.call_args_list))
        self.assertEqual(
            [len(item["attempts"]) for item in observed],
            [receipt.TLS_READINESS_ATTEMPTS] * len(receipt.TLS_READINESS_PROBES),
        )

    def test_retry_policy_keeps_v8_contract_version_and_fact_shape(self) -> None:
        """最小方案：PRODUCER_VERSION 仍为 8，合同摘要与第 64 项修改前逐字相同，重试参数不进入合同。"""

        self.assertEqual(receipt.PRODUCER_VERSION, "8")
        self.assertEqual(
            receipt.contract_sha256(),
            "e195b1cfa8c4d117ad1d51eb6e20aa36609c7ed03074ae8a69ade6774459b5cf",
        )
        with (
            mock.patch.object(receipt, "TLS_READINESS_TRANSIENT_CURL_EXIT_CODES", frozenset({28})),
            mock.patch.object(receipt, "TLS_READINESS_TRANSIENT_RETRIES", 9),
            mock.patch.object(receipt, "TLS_READINESS_TRANSIENT_BACKOFF_SECONDS", (1,)),
            mock.patch.object(receipt, "TLS_READINESS_TRANSIENT_WINDOW_SECONDS", 5),
        ):
            self.assertEqual(
                receipt.contract_sha256(),
                "e195b1cfa8c4d117ad1d51eb6e20aa36609c7ed03074ae8a69ade6774459b5cf",
            )
        self.assertEqual(receipt.TLS_READINESS_TRANSIENT_CURL_EXIT_CODES, frozenset({6, 7, 28}))
        self.assertEqual(receipt.TLS_READINESS_TRANSIENT_RETRIES, 3)
        self.assertEqual(receipt.TLS_READINESS_TRANSIENT_BACKOFF_SECONDS, (2, 4, 8))
        self.assertEqual(receipt.TLS_READINESS_TRANSIENT_WINDOW_SECONDS, 90)

    def test_replays_v8_receipts_generated_before_tls_transient_fix(self) -> None:
        """第 64 项修改前的受管 producer（1b62b096…，生成器重放登记门禁基线时已部署）生成的 v8 收据只读重放通过；
        旧身份不能再生成新收据。部署后同一 Campaign 的旧 attempt 收据与新 attempt 收据因此可以并存比较。"""

        legacy_sha = "1b62b096cc543350d0060c0eea5f97e2f833e47a95b22c346fdffaa27fe66968"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            create_arm_receipt(root, phase="attempt_after", subject_id="attempt-063", prefix="after")
            facts_path = root / "after-facts.json"
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            producer = dict(facts["collector"])
            producer["tool"] = (
                "/root/docker/capture-cli/data/tools/official_client_capture/"
                "codex_upgrade_arm64_environment_receipt.py"
            )
            producer["tool_sha256"] = legacy_sha
            self.assertEqual(producer["version"], "8")
            self.assertIn(legacy_sha, receipt.REGISTERED_REPLAY_PRODUCER_HASHES["8"])
            self.assertNotEqual(legacy_sha, receipt._current_producer()["tool_sha256"])
            facts["collector"] = producer
            Arm64EnvironmentReceiptTests._rewrite(facts_path, facts)
            legacy_receipt = receipt._build_receipt(root, facts_path.name, replay_producer=producer)
            legacy_path = root / "after-pre-item64-receipt.json"
            receipt._write_once(legacy_path, legacy_receipt)

            replayed = receipt.replay(root, legacy_path.name)
            self.assertEqual(replayed, legacy_receipt)
            self.assertEqual(replayed["contract_sha256"], receipt.contract_sha256())
            with self.assertRaisesRegex(receipt.Arm64EnvironmentReceiptError, "身份漂移"):
                receipt.build_receipt(root, facts_path.name)

            # 旧 producer 的 after 与当前 producer 的 before 做跨 attempt 等价比较（投影不含 TLS 就绪事实）。
            current_root = root / "current"
            current_path = create_arm_receipt(
                current_root, phase="attempt_before", subject_id="attempt-064", prefix="before"
            )
            current = receipt.replay(current_root, current_path.name)
            self.assertEqual(current["producer"]["tool_sha256"], receipt._current_producer()["tool_sha256"])
            self.assertTrue(receipt.receipts_equivalent(root, replayed, current_root, current))


if __name__ == "__main__":
    unittest.main()
