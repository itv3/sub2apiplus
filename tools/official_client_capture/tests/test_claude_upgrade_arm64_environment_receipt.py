"""Claude 升级 ARM64 环境收据（B1-10）测试：Claude 探针、收据生成与重放。

全部外部依赖都用替身，不联网、不访问 docker：

* docker inspect、容器路由表、curl、sha256sum 与 ``claude --version`` 经替身 ``subprocess.run`` 按 argv 分派——Codex 模块
  的 ``_run_completed`` 照常执行，退出码闭集与报错文案都是真实逻辑；
* 出口守护两次实时准入替身返回合成的合规状态（``control_receipt_fixtures.create_runtime_egress_fact``），出口策略、守护
  状态与启动身份三处读取位置同时隔离到不存在的私有目录（``runtime_egress_fixtures``）；
* 宿主架构、根盘 statvfs、主机名与单调时钟固定；隔离 HOME 的宿主运行根重定向到私有 0700 临时目录。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import ipaddress
import json
import os
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_environment_receipt as claude_arm
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as codex_arm
from tools.official_client_capture.tests import runtime_egress_fixtures
from tools.official_client_capture.tests.control_receipt_fixtures import (
    create_arm_receipt,
    create_runtime_egress_fact,
)


CLAUDE_VERSION = "2.1.226"
CLAUDE_BINARY = f"/opt/claude-{CLAUDE_VERSION}/claude"
BINARY_SHA256 = "ab" * 32
OBSERVED_AT = datetime(2026, 10, 7, 3, 0, 0, tzinfo=timezone.utc)
API_HELLO = "https://api.anthropic.com/api/hello"
DNS_TIMEOUT = b"curl: (28) Resolving timed out after 6000 milliseconds\n"
TLS_SUCCESS = b"200\t160.79.104.10\t0.120000\n"
GIB = 1024**3
# 根盘 100 GiB、已用 40 GiB、可用 60 GiB：使用率 40%，通过水位。
BLOCK_SIZE = 4096
FAKE_STATVFS = types.SimpleNamespace(
    f_frsize=BLOCK_SIZE,
    f_bsize=BLOCK_SIZE,
    f_blocks=100 * GIB // BLOCK_SIZE,
    f_bfree=60 * GIB // BLOCK_SIZE,
    f_bavail=60 * GIB // BLOCK_SIZE,
)
# 当前合同摘要。共用常量、Claude 探针合同或合同结构的任何变化都会改变它，必须同时升 PRODUCER_VERSION 并为旧版本冻结合同。
FROZEN_CONTRACT_SHA256 = "9714de189e028860b1a85fc25ae2dd75cad5d73552fbcdde08c30a26ad04d102"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _rewrite(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _rewrite_canonical(path: Path, value: object) -> None:
    """按收据的规范字节重写：未变异的重写仍能逐字重放，失败只可能来自变异本身。"""

    path.write_bytes(codex_arm._canonical(value))
    path.chmod(0o600)


@contextlib.contextmanager
def _preserve_umask() -> Iterator[None]:
    """main() 会把进程 umask 设为 0077；测试结束后恢复，避免影响同一进程里的其它测试。"""

    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _route_table(gateway: str) -> bytes:
    """合成容器 /proc/net/route：一条启用网关的 IPv4 默认路由（网关按小端十六进制写）。"""

    little_endian = "".join(f"{octet:02X}" for octet in reversed(ipaddress.IPv4Address(gateway).packed))
    return (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        f"eth0\t00000000\t{little_endian}\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    ).encode("ascii")


class FakeClock:
    """确定性单调时钟：每次读取前进 0.25 秒，版本探针的耗时因此恒为 0.25 秒。"""

    def __init__(self, start: float = 100.0, step: float = 0.25) -> None:
        self.value = start - step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class FakeHost:
    """替身 ARM64 宿主：按 argv 分派全部外部命令并逐条记录；未登记的命令一律报错。"""

    def __init__(
        self,
        *,
        curl_plans: dict[tuple[str, str], list[tuple[int, bytes, bytes]]] | None = None,
        version_stdout: bytes | None = None,
        version_exit: int = 0,
        digests: list[str] | None = None,
        digest_path: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        # (容器, URL) → 依次返回的 (退出码, 标准输出, 标准错误)；计划用尽后按 HTTP 200 成功返回。
        self.curl_plans = {key: list(value) for key, value in (curl_plans or {}).items()}
        self.version_stdout = (
            f"{CLAUDE_VERSION} (Claude Code)\n".encode() if version_stdout is None else version_stdout
        )
        self.version_exit = version_exit
        # 依次返回的二进制摘要；None 表示恒为 BINARY_SHA256。digest_path 覆盖 sha256sum 输出里的文件名。
        self.digests = None if digests is None else list(digests)
        self.digest_path = digest_path

    @staticmethod
    def container_id(name: str) -> str:
        return str(sorted(codex_arm.CONTAINER_CONTRACTS).index(name) + 1) * 64

    @classmethod
    def inspect(cls, name: str) -> bytes:
        """与 control_receipt_fixtures.create_arm_receipt 同一组身份（network_id 取 continuity_seed "a"）。"""

        expected = codex_arm.CONTAINER_CONTRACTS[name]
        index = sorted(codex_arm.CONTAINER_CONTRACTS).index(name) + 1
        payload = [
            {
                "Id": cls.container_id(name),
                "Image": f"sha256:{str(index + 2) * 64}",
                "State": {"Running": True},
                "NetworkSettings": {
                    "Networks": {
                        expected["network"]: {
                            "NetworkID": "a" * 64,
                            "EndpointID": str(index) * 64,
                            "IPAddress": expected["ipv4_address"],
                            "Gateway": expected["gateway"],
                        }
                    }
                },
            }
        ]
        return json.dumps(payload).encode()

    def run(self, argv: list[str], *, check: bool, stdout: Any, stderr: Any, timeout: float) -> Any:
        argv = list(argv)
        self.calls.append(argv)
        if check is not False or timeout <= 0:
            raise AssertionError("探针必须自行判定退出码且带超时")
        if argv[:2] == ["docker", "inspect"] and len(argv) == 3:
            return subprocess.CompletedProcess(argv, 0, self.inspect(argv[2]), b"")
        if argv[:2] == ["docker", "exec"] and argv[3:] == ["cat", "/proc/net/route"]:
            gateway = codex_arm.CONTAINER_CONTRACTS[argv[2]]["gateway"]
            return subprocess.CompletedProcess(argv, 0, _route_table(gateway), b"")
        if argv[:2] == ["docker", "exec"] and argv[3] == "/usr/bin/curl":
            plan = self.curl_plans.get((argv[2], argv[-1]))
            if plan:
                code, out, err = plan.pop(0)
                return subprocess.CompletedProcess(argv, code, out, err)
            return subprocess.CompletedProcess(argv, 0, TLS_SUCCESS, b"")
        if argv[:4] == ["docker", "exec", "capture-cli", "/usr/bin/sha256sum"] and len(argv) == 5:
            digest = BINARY_SHA256 if self.digests is None else self.digests.pop(0)
            path = self.digest_path or argv[4]
            return subprocess.CompletedProcess(argv, 0, f"{digest}  {path}\n".encode(), b"")
        if argv[:5] == ["docker", "exec", "capture-cli", "/usr/bin/env", "-i"]:
            if self.version_exit != 0:
                return subprocess.CompletedProcess(argv, self.version_exit, b"", b"error: boom\n")
            return subprocess.CompletedProcess(argv, 0, self.version_stdout, b"")
        raise AssertionError(f"替身宿主收到未登记的命令：{argv}")

    def curl_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[:2] == ["docker", "exec"] and argv[3] == "/usr/bin/curl"]

    def version_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[3:5] == ["/usr/bin/env", "-i"]]

    def digest_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[3:4] == ["/usr/bin/sha256sum"]]


def runtime_egress_for(host: FakeHost) -> dict[str, Any]:
    """为替身宿主的两个容器生成合规的出口守护准入值（与 Codex 夹具同一合成策略）。"""

    containers = []
    for name in sorted(codex_arm.CONTAINER_CONTRACTS):
        containers.append(
            {
                "name": name,
                "container_id": host.container_id(name),
                "network_bindings": [{"ipv4_address": codex_arm.CONTAINER_CONTRACTS[name]["ipv4_address"]}],
                "public_egress": {},
            }
        )
    return create_runtime_egress_fact(containers, OBSERVED_AT.isoformat())


@contextlib.contextmanager
def fake_arm64(
    host: FakeHost,
    runtime_root: Path,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    machine: str = "aarch64",
) -> Iterator[dict[str, mock.Mock]]:
    """进入替身 ARM64 宿主：外部命令、出口准入、宿主读取与时钟全部替身化。"""

    runtime = runtime_egress_for(host)
    with contextlib.ExitStack() as stack:
        stack.enter_context(runtime_egress_fixtures.isolated_runtime_egress_paths())
        stack.enter_context(mock.patch.object(codex_arm.subprocess, "run", side_effect=host.run))
        patches = {
            "machine": stack.enter_context(mock.patch.object(claude_arm.platform, "machine", return_value=machine)),
            "statvfs": stack.enter_context(mock.patch.object(claude_arm.os, "statvfs", return_value=FAKE_STATVFS)),
            "admission": stack.enter_context(
                mock.patch.object(codex_arm, "require_runtime_egress", return_value=before or runtime)
            ),
            "checked": stack.enter_context(
                mock.patch.object(codex_arm, "_checked_runtime_egress", return_value=(after or runtime, OBSERVED_AT))
            ),
            "backoff": stack.enter_context(mock.patch.object(codex_arm, "_transient_backoff")),
        }
        stack.enter_context(mock.patch.object(claude_arm.socket, "gethostname", return_value="arm64-test"))
        stack.enter_context(mock.patch.object(claude_arm, "CLIENT_PROBE_HOST_RUNTIME_ROOT", runtime_root))
        stack.enter_context(mock.patch.object(claude_arm.time, "monotonic", side_effect=FakeClock()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        yield patches


class ClaudeEnvironmentTestCase(unittest.TestCase):
    """公共夹具：私有证据根与探针运行根，以及在替身宿主上跑一次真实 collect。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-arm64-receipt-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.root = _private_dir(self.workspace / "evidence")
        self.runtime_root = _private_dir(self.workspace / "runtime")

    def collect(
        self,
        *,
        host: FakeHost | None = None,
        phase: str = "p0",
        subject_id: str = "upgrade-claude-2-1-226",
        output: str = "facts.json",
        root: Path | None = None,
    ) -> tuple[FakeHost, dict[str, Any]]:
        host = host or FakeHost()
        with fake_arm64(host, self.runtime_root):
            facts = claude_arm.collect(
                root or self.root,
                output,
                phase=phase,
                subject_id=subject_id,
                claude_version=CLAUDE_VERSION,
            )
        return host, facts

    def collect_and_finalize(self, **options: Any) -> tuple[FakeHost, dict[str, Any], dict[str, Any]]:
        host, facts = self.collect(**options)
        root = options.get("root") or self.root
        receipt = claude_arm.finalize(root, options.get("output", "facts.json"), "receipt.json")
        return host, facts, receipt


class ClaudeEnvironmentCollectTests(ClaudeEnvironmentTestCase):
    def test_collect_finalize_replay_round_trip(self) -> None:
        """真实 collect（替身宿主）→ finalize → replay 逐字往返；收据带 client 与 client_binary。"""

        host, facts, receipt = self.collect_and_finalize(phase="attempt_before", subject_id="attempt-001")
        # 重放只读证据根：不得读取当前出口策略或执行任何外部命令。
        with (
            runtime_egress_fixtures.isolated_runtime_egress_paths(),
            mock.patch.object(codex_arm, "require_runtime_egress", side_effect=AssertionError("重放不得读取当前出口")),
            mock.patch.object(codex_arm.subprocess, "run", side_effect=AssertionError("重放不得执行外部命令")),
        ):
            replayed = claude_arm.replay(self.root, "receipt.json")
        self.assertEqual(replayed, receipt)
        self.assertEqual(json.loads((self.root / "receipt.json").read_text(encoding="utf-8")), receipt)

        self.assertEqual(
            set(receipt),
            {
                "schema_version", "client", "status", "phase", "subject_id", "observed_at_utc", "contract_sha256",
                "continuity_identity_sha256", "resource_gate", "facts", "producer", "environment_equivalence",
                "runtime_egress", "client_binary",
            },
        )
        self.assertEqual(receipt["schema_version"], "official-client-arm64-environment-receipt/v1")
        self.assertEqual(receipt["client"], "claude")
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual((receipt["phase"], receipt["subject_id"]), ("attempt_before", "attempt-001"))
        self.assertEqual(
            receipt["client_binary"],
            {"path": CLAUDE_BINARY, "sha256": BINARY_SHA256, "version": CLAUDE_VERSION},
        )
        self.assertEqual(receipt["producer"], claude_arm._current_producer())
        self.assertEqual(
            (receipt["producer"]["schema_version"], receipt["producer"]["version"]),
            ("official-client-arm64-environment-producer/v1", "1"),
        )
        self.assertEqual(receipt["contract_sha256"], claude_arm.contract_sha256())
        self.assertEqual(receipt["resource_gate"], {"used_percent": 40, "available_bytes": 60 * GIB, "passed": True})
        self.assertEqual(receipt["environment_equivalence"]["sha256"], receipt["continuity_identity_sha256"])
        self.assertEqual(claude_arm.receipt_equivalence_sha256(self.root, receipt), receipt["continuity_identity_sha256"])
        self.assertEqual(receipt["runtime_egress"]["policy_sha256"], facts["runtime_egress"]["policy_sha256"])

        self.assertEqual(set(facts), set(claude_arm.FACT_FIELDS))
        self.assertEqual(facts["schema_version"], "official-client-arm64-environment-facts/v1")
        self.assertEqual(facts["client"], "claude")
        self.assertNotIn("rust_tls_readiness", facts)
        self.assertEqual(facts["client_binary"], receipt["client_binary"])
        self.assertEqual(
            facts["client_binary_probe"],
            {
                "container": "capture-cli",
                "isolated_empty_home": True,
                "process_exit_code": 0,
                "version_stdout": f"{CLAUDE_VERSION} (Claude Code)\n",
                "duration_seconds": 0.25,
            },
        )
        self.assertEqual(facts["observed_at_utc"], OBSERVED_AT.isoformat(timespec="microseconds"))
        for container in facts["containers"]:
            with self.subTest(container=container["name"]):
                self.assertEqual(
                    [(item["name"], item["url"], item["expected_http_status"], item["required_successes"])
                     for item in container["tls_readiness"]],
                    [("anthropic-api-hello", API_HELLO, 200, codex_arm.TLS_READINESS_ATTEMPTS)],
                )
                self.assertEqual(
                    [attempt["attempt"] for attempt in container["tls_readiness"][0]["attempts"]],
                    list(range(1, codex_arm.TLS_READINESS_ATTEMPTS + 1)),
                )
                self.assertEqual(container["public_egress"]["ip_address"], "69.63.195.102")

        # 外部命令：只调用 docker；curl 每容器连续 3 次、无凭据 HEAD；摘要前后各读一次；版本探针清空环境、隔离 HOME。
        self.assertTrue(all(argv[0] == "docker" for argv in host.calls))
        curl = host.curl_calls()
        self.assertEqual(len(curl), len(codex_arm.CONTAINER_CONTRACTS) * codex_arm.TLS_READINESS_ATTEMPTS)
        for argv in curl:
            self.assertIn("--head", argv)
            self.assertIn("--disable", argv)
            self.assertEqual(argv[-1], API_HELLO)
            self.assertFalse({"-H", "--header", "-u", "--user", "-d", "--data", "-b", "--cookie"} & set(argv))
        self.assertEqual(
            [argv[2:] for argv in host.digest_calls()],
            [["capture-cli", "/usr/bin/sha256sum", CLAUDE_BINARY]] * 2,
        )
        (version_call,) = host.version_calls()
        home = version_call[5]
        self.assertTrue(home.startswith(f"HOME={codex_arm.RUST_TLS_PROBE_CONTAINER_RUNTIME_ROOT}/claude-version-"), home)
        self.assertEqual(
            version_call[:5] + version_call[6:],
            ["docker", "exec", "capture-cli", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", CLAUDE_BINARY, "--version"],
        )
        joined = " ".join(" ".join(argv) for argv in host.calls).upper()
        for secret_marker in ("TOKEN", "API_KEY", "AUTHORIZATION", "X-API-KEY", "ANTHROPIC_"):
            self.assertNotIn(secret_marker, joined)
        # 隔离 HOME 探针结束即删除，运行根恢复为空。
        self.assertEqual(list(self.runtime_root.iterdir()), [])

    def test_cli_round_trip_and_shared_egress_check(self) -> None:
        """命令行：collect／finalize／replay 输出带 client；egress-check 直接走两客户端共用的出口守护准入。"""

        host = FakeHost()
        stdout = io.StringIO()
        self.enterContext(_preserve_umask())
        with fake_arm64(host, self.runtime_root), contextlib.redirect_stdout(stdout):
            self.assertEqual(
                claude_arm.main([
                    "collect", "--evidence-root", str(self.root), "--output", "facts.json", "--phase", "p0",
                    "--subject-id", "upgrade-cli", "--claude-version", CLAUDE_VERSION,
                ]),
                0,
            )
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(
                claude_arm.main(["finalize", "--evidence-root", str(self.root), "--facts", "facts.json",
                                 "--output", "receipt.json"]),
                0,
            )
            self.assertEqual(claude_arm.main(["replay", "--evidence-root", str(self.root), "--receipt", "receipt.json"]), 0)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            lines,
            [
                {"client": "claude", "phase": "p0", "status": "collected", "subject_id": "upgrade-cli"},
                {"client": "claude", "phase": "p0", "status": "passed", "subject_id": "upgrade-cli"},
                {"client": "claude", "phase": "p0", "status": "passed", "subject_id": "upgrade-cli"},
            ],
        )

        output = io.StringIO()
        with (
            runtime_egress_fixtures.stubbed_runtime_egress_admission({"policy_sha256": "a" * 64}) as admission,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(claude_arm.main(["egress-check"]), 0)
        admission.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), {"policy_sha256": "a" * 64, "status": "passed"})

        errors = io.StringIO()
        with (
            runtime_egress_fixtures.isolated_runtime_egress_paths(),
            contextlib.redirect_stderr(errors),
        ):
            # 不替身准入：三处读取位置都被隔离到不存在的目录，共用准入失败关闭。
            self.assertEqual(claude_arm.main(["egress-check"]), 1)
        self.assertIn("Claude ARM64 环境收据失败：运行时出口准入拒绝", errors.getvalue())

    def test_collect_shares_attempt_deadline_and_restores_scope(self) -> None:
        """受管 deadline 与心跳经 Codex 作用域变量传给全部命令；结束（含异常）后原样恢复。"""

        deadline, heartbeat = mock.Mock(name="deadline"), mock.Mock(name="heartbeat")
        seen: list[tuple[str, Any, Any]] = []

        def bounded(argv: list[str], **kwargs: Any) -> Any:
            self.assertTrue(codex_arm.HEARTBEAT_OPERATION_RE.fullmatch(kwargs["operation"]))
            seen.append((kwargs["operation"], kwargs["deadline"], kwargs["heartbeat"]))
            return host.run(argv, check=kwargs["check"], stdout=None, stderr=None, timeout=kwargs["timeout"])

        for version_stdout, failure in ((None, None), (b"2.1.225 (Claude Code)\n", "自报版本")):
            with self.subTest(failure=failure):
                seen.clear()
                host = FakeHost(version_stdout=version_stdout)
                with (
                    fake_arm64(host, self.runtime_root),
                    mock.patch.object(codex_arm.incremental_recovery, "run_bounded_subprocess", side_effect=bounded),
                ):
                    context = (
                        self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, failure)
                        if failure else contextlib.nullcontext()
                    )
                    with context:
                        claude_arm.collect_facts(
                            phase="attempt_after", subject_id="attempt-002", claude_version=CLAUDE_VERSION,
                            deadline=deadline, heartbeat=heartbeat,
                        )
                self.assertEqual((codex_arm._ACTIVE_DEADLINE, codex_arm._ACTIVE_HEARTBEAT), (None, None))
                self.assertTrue(seen)
                self.assertTrue(all(item[1] is deadline and item[2] is heartbeat for item in seen))
                operations = [item[0] for item in seen]
                self.assertIn("arm64:tls-ready:capture-cli:anthropic-api-hello:1", operations)
                self.assertIn("arm64:client-binary:version", operations)
                self.assertEqual(
                    [operation for operation in operations if operation.startswith("arm64:client-binary:sha256:")],
                    ["arm64:client-binary:sha256:before", "arm64:client-binary:sha256:after"],
                )

    def test_collect_rejects_invalid_target_or_host_before_reading(self) -> None:
        """目标版本非法或宿主不是 ARM64：在任何 docker、根盘或出口读取之前拒绝。"""

        for version, machine, message in (
            ("2.1", "aarch64", "x.y.z"),
            ("v2.1.226", "aarch64", "x.y.z"),
            ("2.1.226-beta", "aarch64", "x.y.z"),
            (CLAUDE_VERSION, "x86_64", "ARM64"),
        ):
            with self.subTest(version=version, machine=machine):
                host = FakeHost()
                with fake_arm64(host, self.runtime_root, machine=machine) as patches:
                    with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, message):
                        claude_arm.collect_facts(phase="p0", subject_id="upgrade-x", claude_version=version)
                self.assertEqual(host.calls, [])
                patches["statvfs"].assert_not_called()
                patches["admission"].assert_not_called()
        with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "phase"):
            claude_arm.collect_facts(phase="attempt_middle", subject_id="x", claude_version=CLAUDE_VERSION)

    def test_collect_fails_closed_on_admission_drift(self) -> None:
        """准入与采集之间容器重建、采集期间策略变化、策略未保护双容器：一律拒绝。"""

        host = FakeHost()
        rebuilt = runtime_egress_for(host)
        rebuilt["runtime"]["services"]["sub2apiplus"]["container_id"] = "f" * 64
        changed = runtime_egress_for(host)
        changed["policy_sha256"] = "0" * 64
        unprotected = runtime_egress_for(host)
        unprotected["policy"] = {"services": {"capture-cli": {}}}
        for name, options, message in (
            ("rebuilt", {"before": rebuilt}, "重建"),
            ("policy-changed", {"after": changed}, "须重新准入"),
            ("unprotected", {"before": unprotected}, "未保护"),
        ):
            with self.subTest(case=name), fake_arm64(FakeHost(), self.runtime_root, **options):
                with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, message):
                    claude_arm.collect_facts(phase="p0", subject_id="upgrade-x", claude_version=CLAUDE_VERSION)

    def test_equivalence_projection_is_shared_with_codex_receipts(self) -> None:
        """同一宿主与容器：Claude 收据与 Codex v8 收据的等价摘要相同；但 Claude 入口不接受 Codex 收据。"""

        _host, _facts, receipt = self.collect_and_finalize(phase="attempt_before", subject_id="attempt-x")
        codex_root = self.workspace / "codex"
        codex_path = create_arm_receipt(codex_root, phase="attempt_before", subject_id="attempt-x", prefix="codex")
        codex_receipt = codex_arm.replay(codex_root, codex_path.name)
        self.assertEqual(
            claude_arm.receipt_equivalence_sha256(self.root, receipt),
            codex_arm.receipt_equivalence_sha256(codex_root, codex_receipt),
        )
        self.assertTrue(claude_arm.receipts_equivalent(self.root, receipt, self.root, receipt))
        with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "缺少完整的 facts／producer 收据"):
            claude_arm.receipts_equivalent(self.root, receipt, codex_root, codex_receipt)
        # 镜像变化仍然打破等价（等价投影与 Codex 一样保留真实依赖）。
        changed_root = _private_dir(self.workspace / "changed")
        host = FakeHost()
        original_inspect = host.inspect

        def inspect(name: str) -> bytes:
            payload = json.loads(original_inspect(name))
            if name == "sub2apiplus":
                payload[0]["Image"] = "sha256:" + "9" * 64
            return json.dumps(payload).encode()

        host.inspect = inspect  # type: ignore[method-assign]
        self.collect(host=host, root=changed_root)
        changed = claude_arm.finalize(changed_root, "facts.json", "receipt.json")
        self.assertFalse(claude_arm.receipts_equivalent(self.root, receipt, changed_root, changed))


class ClaudeTlsReadinessProbeTests(unittest.TestCase):
    """Claude TLS 就绪探针：连续成功、网络瞬态有界重试、非瞬态立即失败（策略与 Codex 第 64 项同构）。"""

    def _observe(
        self, plan: list[tuple[int, bytes, bytes]], container: str = "capture-cli"
    ) -> tuple[FakeHost, mock.Mock, str, Any]:
        host = FakeHost(curl_plans={(container, API_HELLO): plan})
        log = io.StringIO()
        outcome: Any
        with (
            mock.patch.object(codex_arm.subprocess, "run", side_effect=host.run),
            mock.patch.object(codex_arm, "_transient_backoff") as backoff,
            contextlib.redirect_stderr(log),
        ):
            try:
                outcome = claude_arm._tls_readiness_observation(container)
            except claude_arm.Arm64EnvironmentReceiptError as error:
                outcome = error
        return host, backoff, log.getvalue(), outcome

    def test_success_requires_consecutive_head_requests(self) -> None:
        host, backoff, log, observed = self._observe([])
        self.assertEqual(len(host.curl_calls()), codex_arm.TLS_READINESS_ATTEMPTS)
        backoff.assert_not_called()
        self.assertEqual(log, "")
        (probe,) = observed
        self.assertEqual(probe["url"], API_HELLO)
        self.assertEqual(
            probe["attempts"][0],
            {
                "attempt": 1,
                "http_status": 200,
                "remote_ip": "160.79.104.10",
                "tls_seconds": 0.12,
                "response_sha256": hashlib.sha256(TLS_SUCCESS).hexdigest(),
            },
        )

    def test_dns_timeout_then_success_restarts_consecutive_count(self) -> None:
        host, backoff, log, observed = self._observe([(28, b"", DNS_TIMEOUT)])
        self.assertEqual(len(host.curl_calls()), 1 + codex_arm.TLS_READINESS_ATTEMPTS)
        self.assertEqual([attempt["attempt"] for attempt in observed[0]["attempts"]], [1, 2, 3])
        self.assertEqual([call.args[0] for call in backoff.call_args_list], [2])
        self.assertEqual(
            backoff.call_args.kwargs["operation"], "arm64:tls-ready:capture-cli:anthropic-api-hello:backoff"
        )
        self.assertIn("capture-cli anthropic-api-hello TLS 就绪探针网络瞬态第 1 次", log)
        self.assertIn("退出码 28", log)
        self.assertIn("在 1 次网络瞬态后连续 3 次通过", log)

    def test_transient_after_partial_successes_discards_them(self) -> None:
        success = (0, TLS_SUCCESS, b"")
        refused = (7, b"", b"curl: (7) Failed to connect to api.anthropic.com port 443: Connection refused\n")
        host, backoff, log, observed = self._observe([success, success, refused], container="sub2apiplus")
        self.assertEqual(len(host.curl_calls()), 6)
        self.assertEqual([attempt["attempt"] for attempt in observed[0]["attempts"]], [1, 2, 3])
        self.assertEqual(backoff.call_count, 1)
        self.assertIn("本轮连续第 3 次尝试", log)

    def test_transients_beyond_retry_limit_fail_with_each_exit_code(self) -> None:
        plan = [
            (28, b"", DNS_TIMEOUT),
            (6, b"", b"curl: (6) Could not resolve host: api.anthropic.com\n"),
            (7, b"", b"curl: (7) Failed to connect to api.anthropic.com port 443\n"),
            (28, b"", b"curl: (28) Connection timed out after 6001 milliseconds\n"),
        ]
        host, backoff, _log, error = self._observe(plan)
        self.assertIsInstance(error, claude_arm.Arm64EnvironmentReceiptError)
        message = str(error)
        self.assertTrue(
            message.startswith("capture-cli anthropic-api-hello TLS 就绪探针网络瞬态超过重试上限 3 次：#1 退出码 28"),
            message,
        )
        for index, code in enumerate((28, 6, 7, 28), 1):
            self.assertIn(f"#{index} 退出码 {code}「", message)
        self.assertLessEqual(len(message), codex_arm.TLS_READINESS_TRANSIENT_MESSAGE_LIMIT)
        self.assertEqual([call.args[0] for call in backoff.call_args_list], [2, 4, 8])
        self.assertEqual(len(host.curl_calls()), 4)

    def test_non_transient_failures_fail_immediately(self) -> None:
        cases = (
            (
                (60, b"", b"curl: (60) SSL certificate problem: unable to get local issuer certificate\n"),
                "capture-cli anthropic-api-hello TLS 就绪探针第 1 次失败：curl: (60) SSL certificate problem: "
                "unable to get local issuer certificate",
            ),
            (
                (125, b"", b"Error response from daemon: No such container: capture-cli\n"),
                "capture-cli anthropic-api-hello TLS 就绪探针第 1 次失败：Error response from daemon: "
                "No such container: capture-cli",
            ),
            ((0, b"403\t160.79.104.10\t0.120000\n", b""), "capture-cli anthropic-api-hello TLS 就绪探针未通过"),
            ((0, b"200\t160.79.104.10\t0\n", b""), "capture-cli anthropic-api-hello TLS 就绪探针未通过"),
            ((0, b"garbage\n", b""), "capture-cli anthropic-api-hello TLS 就绪探针响应非法"),
        )
        for line, expected in cases:
            with self.subTest(expected=expected):
                host, backoff, log, error = self._observe([line])
                self.assertIsInstance(error, claude_arm.Arm64EnvironmentReceiptError)
                self.assertEqual(str(error), expected)
                self.assertEqual(len(host.curl_calls()), 1)
                backoff.assert_not_called()
                self.assertEqual(log, "")

    def test_non_transient_failure_after_transient_still_fails_immediately(self) -> None:
        host, backoff, _log, error = self._observe(
            [(28, b"", DNS_TIMEOUT), (60, b"", b"curl: (60) SSL certificate problem: certificate has expired\n")]
        )
        self.assertRegex(
            str(error),
            r"^capture-cli anthropic-api-hello TLS 就绪探针第 1 次失败：curl: \(60\) SSL certificate problem",
        )
        self.assertEqual(len(host.curl_calls()), 2)
        self.assertEqual(backoff.call_count, 1)


class ClaudeClientBinaryProbeTests(ClaudeEnvironmentTestCase):
    """客户端二进制探针：版本自报恰为冻结输出、探针前后摘要一致、隔离 HOME 与运行根约束。"""

    def _probe(self, host: FakeHost) -> Any:
        target = claude_arm.client_binary_target(CLAUDE_VERSION)
        with (
            mock.patch.object(codex_arm.subprocess, "run", side_effect=host.run),
            mock.patch.object(claude_arm, "CLIENT_PROBE_HOST_RUNTIME_ROOT", self.runtime_root),
            mock.patch.object(claude_arm.time, "monotonic", side_effect=FakeClock()),
        ):
            try:
                return claude_arm._client_binary_observation(target)
            except claude_arm.Arm64EnvironmentReceiptError as error:
                return error

    def test_target_derives_path_from_target_version(self) -> None:
        self.assertEqual(
            claude_arm.client_binary_target("2.1.226"),
            {"container": "capture-cli", "path": "/opt/claude-2.1.226/claude", "version": "2.1.226"},
        )
        for invalid in ("2.1", "v2.1.226", "2.1.226-beta", "02.1.226", "", None, 2):
            with self.subTest(version=invalid), self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "x.y.z"):
                claude_arm.client_binary_target(invalid)  # type: ignore[arg-type]

    def test_success_records_identity_and_cleans_isolated_home(self) -> None:
        host = FakeHost()
        binary, probe = self._probe(host)
        self.assertEqual(binary, {"path": CLAUDE_BINARY, "sha256": BINARY_SHA256, "version": CLAUDE_VERSION})
        self.assertEqual(probe["version_stdout"], "2.1.226 (Claude Code)\n")
        self.assertEqual(probe["duration_seconds"], 0.25)
        self.assertEqual(len(host.digest_calls()), 2)
        self.assertEqual(list(self.runtime_root.iterdir()), [])

    def test_version_mismatch_or_extra_output_is_rejected(self) -> None:
        for stdout in (
            b"2.1.225 (Claude Code)\n",
            b"2.1.226 (Claude Code)",
            b"2.1.226 (Claude Code)\nupdate available\n",
            b"2.1.226\n",
            b"",
        ):
            with self.subTest(stdout=stdout):
                error = self._probe(FakeHost(version_stdout=stdout))
                self.assertIsInstance(error, claude_arm.Arm64EnvironmentReceiptError)
                self.assertIn("自报版本与本轮目标 2.1.226 不一致", str(error))
                self.assertEqual(list(self.runtime_root.iterdir()), [])

    def test_nonzero_exit_is_rejected_with_stderr(self) -> None:
        error = self._probe(FakeHost(version_exit=1))
        self.assertIsInstance(error, claude_arm.Arm64EnvironmentReceiptError)
        self.assertEqual(str(error), "capture-cli Claude 2.1.226 客户端二进制版本探针失败：error: boom")

    def test_binary_digest_must_be_stable_and_well_formed(self) -> None:
        changed = self._probe(FakeHost(digests=[BINARY_SHA256, "cd" * 32]))
        self.assertIsInstance(changed, claude_arm.Arm64EnvironmentReceiptError)
        self.assertIn("探针期间 Claude 客户端二进制发生变化", str(changed))
        for host in (FakeHost(digest_path="/opt/claude-2.1.225/claude"), FakeHost(digests=["XYZ", "XYZ"])):
            with self.subTest(host=host.digest_path or "bad-digest"):
                error = self._probe(host)
                self.assertIsInstance(error, claude_arm.Arm64EnvironmentReceiptError)
                self.assertIn("格式非法或路径不符", str(error))

    def test_runtime_root_must_be_private(self) -> None:
        self.runtime_root.chmod(0o755)
        error = self._probe(FakeHost())
        self.assertIsInstance(error, claude_arm.Arm64EnvironmentReceiptError)
        self.assertIn("0700", str(error))
        self.runtime_root.chmod(0o700)
        link = self.workspace / "runtime-link"
        link.symlink_to(self.runtime_root, target_is_directory=True)
        with mock.patch.object(claude_arm, "CLIENT_PROBE_HOST_RUNTIME_ROOT", link):
            with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "不可信"):
                claude_arm._probe_runtime_root()


class ClaudeEnvironmentValidationTests(ClaudeEnvironmentTestCase):
    """封存前校验：Claude 专属字段、共用容器合同、出口守护登记与资源门禁。"""

    def _mutated(self, mutate: Any, phase: str = "p0") -> str:
        """采集一份真实 facts、施加变异后重写，返回 build_receipt 的报错原文（不报错时返回空串）。"""

        root = _private_dir(self.workspace / f"case-{len(list(self.workspace.iterdir()))}")
        _host, facts = self.collect(root=root, phase=phase)
        mutate(facts)
        _rewrite(root / "facts.json", facts)
        try:
            claude_arm.build_receipt(root, "facts.json")
        except claude_arm.Arm64EnvironmentReceiptError as error:
            return str(error)
        return ""

    def test_tampered_facts_fail_closed(self) -> None:
        codex_probes = [
            {"name": name, "url": url, "expected_http_status": status,
             "required_successes": codex_arm.TLS_READINESS_ATTEMPTS, "attempts": []}
            for name, url, status in codex_arm.TLS_READINESS_PROBES
        ]

        def set_path(path: tuple[Any, ...], value: Any) -> Any:
            def mutate(facts: dict[str, Any]) -> None:
                target = facts
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
            return mutate

        cases = (
            ("client", set_path(("client",), "codex"), "facts.client"),
            ("schema", set_path(("schema_version",), codex_arm.FACTS_SCHEMA), "schema_version"),
            ("codex-contract", set_path(("contract_sha256",), codex_arm.contract_sha256()), "合同摘要漂移"),
            ("rust-tls-field", set_path(("rust_tls_readiness",), {}), "字段不闭合"),
            ("binary-version", set_path(("client_binary", "version"), "2.1.225"), "路径模板"),
            ("binary-path", set_path(("client_binary", "path"), "/opt/claude-2.1.226/bin/claude"), "路径模板"),
            ("binary-bad-version", set_path(("client_binary", "version"), "latest"), "x.y.z"),
            ("binary-sha256", set_path(("client_binary", "sha256"), "XYZ"), "client_binary.sha256"),
            ("probe-stdout", set_path(("client_binary_probe", "version_stdout"), "2.1.225 (Claude Code)\n"), "探针事实"),
            ("probe-exit-false", set_path(("client_binary_probe", "process_exit_code"), False), "探针事实"),
            ("probe-exit-1", set_path(("client_binary_probe", "process_exit_code"), 1), "探针事实"),
            ("probe-home", set_path(("client_binary_probe", "isolated_empty_home"), False), "探针事实"),
            ("probe-container", set_path(("client_binary_probe", "container"), "sub2apiplus"), "探针事实"),
            ("probe-duration", set_path(("client_binary_probe", "duration_seconds"), 0), "探针事实"),
            ("codex-tls-probes", set_path(("containers", 0, "tls_readiness"), codex_probes), "没有完整覆盖冻结端点"),
            ("tls-url", set_path(("containers", 0, "tls_readiness", 0, "url"), codex_arm.TLS_READINESS_PROBES[0][1]),
             "连续成功次数不足"),
            ("tls-status", set_path(("containers", 1, "tls_readiness", 0, "attempts", 0, "http_status"), 401),
             "TLS 第 1 次未通过"),
            ("tls-missing", lambda facts: facts["containers"][0].pop("tls_readiness"), "缺少 Claude TLS 就绪事实"),
            ("tls-short", lambda facts: facts["containers"][0]["tls_readiness"][0]["attempts"].pop(),
             "连续成功次数不足"),
            ("fixed-ip", set_path(("containers", 1, "selected_network", "ipv4_address"), "172.25.0.99"), "固定网络坐标"),
            ("public-egress", set_path(("containers", 1, "public_egress", "ip_address"), "203.0.113.1"), "公网出口"),
            ("container-id", set_path(("containers", 0, "container_id"), "f" * 64), "出口守护登记不一致"),
            ("stale-status", set_path(("observed_at_utc",), "2026-10-07T03:10:00.000000+00:00"), "过期"),
            ("containers-shape", set_path(("containers",), [None, None]), "固定双容器"),
            ("collector", set_path(("collector", "tool_sha256"), "0" * 64), "身份漂移"),
        )
        for name, mutate, message in cases:
            with self.subTest(case=name):
                self.assertIn(message, self._mutated(mutate))

    def test_admission_phase_watermark_fails_but_after_phase_degrades(self) -> None:
        def low_disk(facts: dict[str, Any]) -> None:
            facts["root_filesystem"]["available_bytes"] = codex_arm.ROOT_MIN_AVAILABLE_BYTES - 1

        for phase in ("p0", "attempt_before", "deployment_before"):
            with self.subTest(phase=phase):
                self.assertIn("停线水位", self._mutated(low_disk, phase=phase))

        root = _private_dir(self.workspace / "after")
        _host, facts = self.collect(root=root, phase="attempt_after")
        facts["root_filesystem"]["used_percent"] = 72
        _rewrite(root / "facts.json", facts)
        receipt = claude_arm.finalize(root, "facts.json", "receipt.json")
        self.assertEqual(
            receipt["resource_gate"],
            {"used_percent": 72, "available_bytes": 60 * GIB, "passed": False, "degraded": True},
        )
        self.assertEqual(claude_arm.replay(root, "receipt.json"), receipt)


class ClaudeEnvironmentReplayTests(ClaudeEnvironmentTestCase):
    """重放：逐字比较、拒绝篡改、producer 身份与只读重放登记。"""

    def test_replay_rejects_tampered_receipt_or_facts(self) -> None:
        _host, facts, receipt = self.collect_and_finalize()
        receipt_path = self.root / "receipt.json"
        original = receipt_path.read_bytes()
        # 对照：按规范字节原样重写仍能重放，下面每个失败都只来自对应的变异。
        _rewrite_canonical(receipt_path, json.loads(original))
        self.assertEqual(claude_arm.replay(self.root, "receipt.json"), receipt)
        for name, mutate, message in (
            ("client-binary", lambda value: value["client_binary"].update(sha256="cd" * 32), "重放结果不一致"),
            ("client", lambda value: value.update(client="codex"), "receipt.client"),
            ("schema", lambda value: value.update(schema_version=codex_arm.RECEIPT_SCHEMA), "schema_version"),
            ("continuity", lambda value: value.update(continuity_identity_sha256="f" * 64), "重放结果不一致"),
            ("unregistered-producer", lambda value: value["producer"].update(tool_sha256="0" * 64), "身份漂移"),
            ("wrong-coordinate", lambda value: value["producer"].update(
                tool="/tmp/claude_upgrade_arm64_environment_receipt.py"), "身份漂移"),
            ("codex-producer-schema", lambda value: value["producer"].update(schema_version=codex_arm.PRODUCER_SCHEMA),
             "身份漂移"),
        ):
            with self.subTest(case=name):
                payload = json.loads(original)
                mutate(payload)
                _rewrite_canonical(receipt_path, payload)
                with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, message):
                    claude_arm.replay(self.root, "receipt.json")
        receipt_path.write_bytes(original)
        self.assertEqual(claude_arm.replay(self.root, "receipt.json"), receipt)

        # facts 在封存后被改动（即使改动后的 facts 本身仍合法）：重放必须失败。
        facts["client_binary"]["sha256"] = "cd" * 32
        _rewrite(self.root / "facts.json", facts)
        with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "重放结果不一致"):
            claude_arm.replay(self.root, "receipt.json")
        with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "未经原 producer 完整重放"):
            claude_arm.receipt_equivalence_sha256(self.root, receipt)

    def test_replay_accepts_relocated_worktree_root(self) -> None:
        """工作树根迁移只改变绝对坐标：同一字节 producer 在别的根生成的收据照常重放。"""

        _host, facts = self.collect()
        relocated = dict(facts["collector"])
        relocated["tool"] = (
            "/root/docker/capture-cli/data/tools/official_client_capture/claude_upgrade_arm64_environment_receipt.py"
        )
        facts["collector"] = relocated
        _rewrite(self.root / "facts.json", facts)
        receipt = claude_arm._build_receipt(self.root, "facts.json", replay_producer=relocated)
        codex_arm._write_once(self.root / "receipt.json", receipt)
        self.assertEqual(claude_arm.replay(self.root, "receipt.json")["producer"], relocated)

    def test_registered_legacy_producer_replays_but_cannot_produce_new_receipts(self) -> None:
        """历史登记表初始为空；登记后旧摘要只允许只读重放，不允许生成新收据，登记在错误版本下无效。"""

        self.assertEqual(claude_arm.REGISTERED_REPLAY_PRODUCER_HASHES, {})
        legacy = "e" * 64
        _host, facts = self.collect()
        producer = dict(facts["collector"], tool_sha256=legacy)
        facts["collector"] = producer
        _rewrite(self.root / "facts.json", facts)
        for registry in ({}, {"2": frozenset({legacy})}):
            with self.subTest(registry=registry), mock.patch.dict(claude_arm.REGISTERED_REPLAY_PRODUCER_HASHES, registry):
                with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "身份漂移"):
                    claude_arm._build_receipt(self.root, "facts.json", replay_producer=producer)
        with mock.patch.dict(claude_arm.REGISTERED_REPLAY_PRODUCER_HASHES, {"1": frozenset({legacy})}):
            receipt = claude_arm._build_receipt(self.root, "facts.json", replay_producer=producer)
            codex_arm._write_once(self.root / "receipt.json", receipt)
            self.assertEqual(claude_arm.replay(self.root, "receipt.json"), receipt)
            self.assertEqual(receipt["producer"]["tool_sha256"], legacy)
            with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "身份漂移"):
                claude_arm.build_receipt(self.root, "facts.json")
        with self.assertRaisesRegex(claude_arm.Arm64EnvironmentReceiptError, "身份漂移"):
            claude_arm.replay(self.root, "receipt.json")


class ClaudeEnvironmentContractTests(unittest.TestCase):
    """合同摘要、与 Codex 共用校验的边界与命令行参数。"""

    def test_contract_binds_shared_codex_constants_and_claude_probes(self) -> None:
        self.assertEqual(claude_arm.contract_sha256(), FROZEN_CONTRACT_SHA256)
        self.assertNotEqual(claude_arm.contract_sha256(), codex_arm.contract_sha256())
        # 共用常量：Codex 改动时 Claude 合同随之变化。
        for name, value in (
            ("ROOT_MAX_USED_PERCENT", codex_arm.ROOT_MAX_USED_PERCENT - 1),
            ("ROOT_MIN_AVAILABLE_BYTES", codex_arm.ROOT_MIN_AVAILABLE_BYTES + 1),
            ("TLS_READINESS_ATTEMPTS", codex_arm.TLS_READINESS_ATTEMPTS + 1),
            ("EGRESS_POLICY_SCHEMA", "codex-runtime-egress-policy/v2"),
            ("CONTAINER_CONTRACTS", {**codex_arm.CONTAINER_CONTRACTS, "extra": {"network": "x"}}),
        ):
            with self.subTest(shared=name), mock.patch.object(codex_arm, name, value):
                self.assertNotEqual(claude_arm.contract_sha256(), FROZEN_CONTRACT_SHA256)
        # Codex 专属探针与重试参数：不影响 Claude 合同。
        for name, value in (
            ("TLS_READINESS_PROBES", (("x", "https://example.invalid/", 401),)),
            ("RUST_TLS_PROBE_BINARY_TEMPLATE", "/opt/codex/{codex_version}"),
            ("TLS_READINESS_TRANSIENT_RETRIES", 9),
            ("TLS_READINESS_TRANSIENT_BACKOFF_SECONDS", (1,)),
        ):
            with self.subTest(codex_only=name), mock.patch.object(codex_arm, name, value):
                self.assertEqual(claude_arm.contract_sha256(), FROZEN_CONTRACT_SHA256)
        # Claude 专属探针合同。
        for name, value in (
            ("TLS_READINESS_PROBES", (("anthropic-api-hello", API_HELLO, 204),)),
            ("TLS_READINESS_METHOD", "GET"),
            ("CLIENT_BINARY_TEMPLATE", "/opt/claude-{version}/bin/claude"),
            ("CLIENT_VERSION_STDOUT_TEMPLATE", "{version}\n"),
        ):
            with self.subTest(claude=name), mock.patch.object(claude_arm, name, value):
                self.assertNotEqual(claude_arm.contract_sha256(), FROZEN_CONTRACT_SHA256)

    def test_shared_container_validation_excludes_codex_tls_rules(self) -> None:
        """Codex _validate_container 只校验共用字段：Claude 版本标记不落入 Codex TLS 版本集合，剥离 tls_readiness 是必要的。"""

        self.assertNotIn(
            claude_arm.CODEX_SHARED_CONTAINER_VALIDATION_VERSION,
            {"5", *codex_arm.RUST_TLS_READINESS_PRODUCER_VERSIONS},
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = _private_dir(Path(directory) / "runtime")
            host = FakeHost()
            with fake_arm64(host, runtime_root):
                facts = claude_arm.collect_facts(phase="p0", subject_id="upgrade-x", claude_version=CLAUDE_VERSION)
        policy = facts["runtime_egress"]["policy"]
        container = facts["containers"][0]
        shared = {key: value for key, value in container.items() if key != "tls_readiness"}
        options = {"expected_public_egress": "运行时授权策略", "runtime_policy": policy}
        codex_arm._validate_container(
            shared, container["name"], producer_version=claude_arm.CODEX_SHARED_CONTAINER_VALIDATION_VERSION, **options
        )
        with self.assertRaisesRegex(codex_arm.Arm64EnvironmentReceiptError, "字段不闭合"):
            codex_arm._validate_container(
                container, container["name"],
                producer_version=claude_arm.CODEX_SHARED_CONTAINER_VALIDATION_VERSION, **options,
            )
        # 若按 Codex v8 校验，Claude 的 TLS 探针集合会被当成 Codex 端点而拒绝。
        with self.assertRaisesRegex(codex_arm.Arm64EnvironmentReceiptError, "TLS"):
            codex_arm._validate_container(
                container, container["name"], producer_version=codex_arm.PRODUCER_VERSION, **options
            )

    def test_cli_has_no_network_override_arguments(self) -> None:
        parser = claude_arm.build_parser()
        destinations = {
            action.dest
            for subparser in parser._actions
            if getattr(subparser, "choices", None)
            for choice in subparser.choices.values()
            for action in choice._actions
        }
        self.assertIn("claude_version", destinations)
        for forbidden in ("public_egress_ip", "gateway", "ipv4_address", "url", "binary", "client_binary"):
            self.assertNotIn(forbidden, destinations)
        self.assertEqual(
            parser.parse_args(["collect", "--evidence-root", "/x", "--output", "f.json", "--phase", "p0",
                               "--subject-id", "s", "--claude-version", CLAUDE_VERSION]).claude_version,
            CLAUDE_VERSION,
        )

    def test_schema_names_and_shared_error_class(self) -> None:
        self.assertEqual(
            (claude_arm.FACTS_SCHEMA, claude_arm.RECEIPT_SCHEMA, claude_arm.PRODUCER_SCHEMA, claude_arm.PRODUCER_VERSION),
            (
                "official-client-arm64-environment-facts/v1",
                "official-client-arm64-environment-receipt/v1",
                "official-client-arm64-environment-producer/v1",
                "1",
            ),
        )
        self.assertIs(claude_arm.Arm64EnvironmentReceiptError, codex_arm.Arm64EnvironmentReceiptError)
        self.assertIs(claude_arm.PHASES, codex_arm.PHASES)


if __name__ == "__main__":
    unittest.main()
