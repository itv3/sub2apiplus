"""R15：运行时出口准入、逐容器闭锁、不可逆暂停和安装中断的离线回归。"""

from __future__ import annotations

import contextlib
import copy
import argparse
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import arm64_supervised_deploy as deploy
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture.tests import control_receipt_fixtures


def policy_fixture():
    return json.loads((Path(__file__).parent / "fixtures/runtime_egress_policy.json").read_text())


def observations(policy):
    return [{"url": url, "status": "passed", "ip_address": policy["allowed_public_ipv4"][0],
             "observed_at_epoch": time.time(), "response_sha256": "e" * 64} for url in policy["probe_urls"]]


def runtime_fixture():
    containers = [{"name": name, "container_id": str(index) * 64, "public_egress": {},
                   "network_bindings": [{"ipv4_address": f"172.20.0.{index}"}]} for index, name in enumerate(("sub2apiplus", "capture-cli"), 1)]
    snapshot = control_receipt_fixtures.create_runtime_egress_fact(containers, datetime.now(timezone.utc).isoformat())
    snapshot["runtime"].update(observed_at_monotonic_ns=time.monotonic_ns(), valid_until_monotonic_ns=time.monotonic_ns() + 2500000000)
    return snapshot


class EgressGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        guard = deploy.EgressGuard.__new__(deploy.EgressGuard)
        self.guard = guard
        guard.contract, guard.policy = arm, policy_fixture()
        guard.policy_path, guard.runtime_root = self.root / "policy.json", self.root
        guard.policy_sha256 = arm.egress_policy_sha256(guard.policy)
        guard.role, guard.parents = "origin", {"sub2api_egress.slice": "/fixture/cgroup"}
        guard.maps = {"sub2api_egress.slice": mock.Mock()}
        guard.manifest = {"firewall_sha256": "f" * 64}
        guard.probes = {url: "1.1.1.1" for url in guard.policy["probe_urls"]}
        guard.inventory = {"services": {
            name: {"container_id": str(index) * 64, "valid": True, "reason": "",
                   "parent": "sub2api_egress.slice", "cgroup_id": index, "dependencies": [],
                   "bindings": [{"ifindex": 2, "host_ifindex": index + 10, "source_ipv4": f"172.31.1.{index}"}]}
            for index, name in enumerate(guard.policy["services"], 1)}, "bypass_ifindices": [100]}
        guard.observations = {name: observations(guard.policy) for name in guard.policy["services"]}
        guard.pending, guard.blocked_since, guard.last_compliant = {}, {}, {}
        guard.next_probe = {name: time.time() + 60 for name in guard.policy["services"]}
        guard.lease_states = {name: "compliant" for name in guard.policy["services"]}
        guard.boot_id = "00000000-0000-0000-0000-000000000001"
        guard.status_path = self.root / "status.json"
        guard.previous_state, guard.health, guard.health_expiry = None, {}, 0
        guard.pool = mock.Mock()
        guard.pool.submit.side_effect = lambda *args: Future()
        guard.resolver_pool, guard.resolver_pending, guard.next_resolution = mock.Mock(), None, time.time() + 60
        self.enterContext(mock.patch.object(arm, "load_egress_policy", return_value=guard.policy))
        self.enterContext(mock.patch.object(deploy, "egress_firewall_identity", return_value="f" * 64))
        self.enterContext(mock.patch.object(deploy, "egress_wireguard_observation"))
        self.enterContext(mock.patch.object(deploy, "egress_verify_routes"))
        self.enterContext(mock.patch.object(deploy, "egress_remote_status"))
        self.inventory = self.enterContext(mock.patch.object(deploy, "egress_inventory", side_effect=lambda *args: copy.deepcopy(guard.inventory)))
        self.command = self.enterContext(mock.patch.object(deploy, "egress_command", return_value=""))

    def test_individual_fault_revokes_only_affected_container(self):
        self.guard.observations["capture-cli"][0]["ip_address"] = "8.8.8.8"
        status = self.guard.step()
        self.assertEqual(status["shared_protection"]["status"], "compliant")
        self.assertEqual(status["services"]["sub2apiplus"]["status"], "compliant")
        self.assertEqual(status["services"]["capture-cli"]["status"], "blocked")
        self.guard.maps["sub2api_egress.slice"].revoke.assert_called_once_with(2, 2, "172.31.1.2")
        leases = self.guard.maps["sub2api_egress.slice"].lease.call_args_list
        self.assertFalse(leases[0].kwargs["probe_only"])
        self.assertTrue(leases[1].kwargs["probe_only"])
        with self.assertRaisesRegex(arm.Arm64EnvironmentReceiptError, "升级必须暂停"):
            arm.validate_egress_status(self.guard.policy, status, now_epoch=time.time())

    def test_shared_fault_revokes_both_and_cannot_reuse_cached_health(self):
        for function in ("egress_remote_status", "egress_wireguard_observation", "egress_verify_routes", "egress_firewall_identity"):
            with self.subTest(function=function), mock.patch.object(deploy, function, side_effect=deploy.DeploymentError("隔离故障")):
                maps = self.guard.maps["sub2api_egress.slice"]
                maps.reset_mock()
                status = self.guard.step()
                self.assertEqual(status["shared_protection"]["status"], "blocked")
                self.assertEqual({item["status"] for item in status["services"].values()}, {"blocked"})
                maps.lease.assert_not_called()
                self.assertEqual({call.args[0] for call in maps.revoke.call_args_list}, {1, 2})
                self.assertEqual(self.guard.health_expiry, 0)
        # 路径修好不能复用故障前的观测放行，须重新从每个容器完成独立探针。
        self.assertEqual({item["status"] for item in self.guard.step()["services"].values()}, {"blocked"})

    def test_step_reconciles_probe_table_and_clears_it_on_shared_fault(self):
        maps = self.guard.maps["sub2api_egress.slice"]
        self.guard.step()
        maps.sync_probes.assert_called_with({"1.1.1.1"})
        maps.probe.assert_not_called()
        with mock.patch.object(deploy, "egress_remote_status", side_effect=deploy.DeploymentError("隔离故障")):
            self.guard.step()
        maps.sync_probes.assert_called_with(set())

    def test_probe_resolution_failure_is_retried_without_blocking_lease_loop(self):
        self.guard.next_resolution = 0
        future = Future()
        self.guard.resolver_pool.submit.return_value = future
        self.guard.step()
        self.guard.resolver_pool.submit.assert_called_once()
        future.set_result({url: "9.9.9.9" for url in self.guard.policy["probe_urls"]})
        self.guard.step()
        self.assertEqual(set(self.guard.probes.values()), {"9.9.9.9"})

    def test_missing_or_changed_policy_blocks_shared_protection(self):
        for result in (FileNotFoundError("缺失策略"), PermissionError("权限错误"), {**self.guard.policy, "revision": 2}):
            with self.subTest(result=type(result).__name__), mock.patch.object(arm, "load_egress_policy") as loader:
                if isinstance(result, Exception):
                    loader.side_effect = result
                else:
                    loader.return_value = result
                self.assertEqual(self.guard.step()["shared_protection"]["status"], "blocked")

    def test_quorum_substitutes_one_failure_but_not_expired_observations(self):
        failed = self.guard.observations["capture-cli"][0]
        failed.update(status="failed", ip_address=None, response_sha256=None)
        self.assertEqual(self.guard.step()["services"]["capture-cli"]["status"], "compliant")
        for item in self.guard.observations["capture-cli"]:
            item["observed_at_epoch"] -= 61
        self.assertEqual(self.guard.step()["services"]["capture-cli"]["status"], "blocked")
        self.assertEqual(len([json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]), 2)

    def test_guard_status_passes_independent_receipt_contract(self):
        status = self.guard.step()
        arm.validate_egress_status(self.guard.policy, status, now_epoch=time.time(),
                                  now_monotonic_ns=time.monotonic_ns(), boot_id=self.guard.boot_id)

    def test_rebuild_and_network_reconnect_require_new_independent_probes(self):
        for field in ("container_id", "bindings", "cgroup_id", "pid", "started_at_utc"):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.guard.inventory)
                service = candidate["services"]["capture-cli"]
                service[field] = {"container_id": "a" * 64, "bindings": [{"ifindex": 3, "host_ifindex": 13, "source_ipv4": "172.31.1.3"}],
                                  "cgroup_id": 9, "pid": 42, "started_at_utc": "2026-09-23T00:00:00Z"}[field]
                old = self.guard.inventory["services"]["capture-cli"]
                identity = deploy.egress_service_identity(old)
                result = Future()
                result.set_result(observations(self.guard.policy))
                self.guard.pending["capture-cli"] = (identity, result)
                self.inventory.side_effect = lambda *args: candidate
                status = self.guard.step()
                self.assertEqual(status["services"]["capture-cli"]["status"], "blocked")
                self.assertEqual(status["services"]["sub2apiplus"]["status"], "compliant")
                self.assertEqual(status["services"]["capture-cli"]["observations"], [])

    def test_failed_atomic_transaction_never_grants_new_bpf_lease(self):
        self.command.side_effect = deploy.DeploymentError("隔离事务失败")
        with self.assertRaises(deploy.DeploymentError):
            self.guard.step()
        self.guard.maps["sub2api_egress.slice"].lease.assert_not_called()
        self.assertFalse(self.guard.status_path.exists())


class _FakeLibbpf:
    """模拟 libbpf 对已固定 HASH 表的读写、删除与迭代；fd 使用真实 /dev/null 句柄以便正常关闭。"""

    def __init__(self, keys):
        import ctypes

        self._ctypes = ctypes
        self.table = {key: (1).to_bytes(4, "little") for key in keys}

        def obj_get(_path):
            return os.open(os.devnull, os.O_RDONLY)

        def get_next_key(_fd, current, following):
            ordered = sorted(self.table)
            if current is None:
                candidates = ordered
            else:
                key = bytes(current)[:4]
                candidates = [item for item in ordered if item > key]
            if not candidates:
                ctypes.set_errno(2)
                return -2
            ctypes.memmove(following, candidates[0], 4)
            return 0

        def delete(_fd, key):
            self.table.pop(bytes(key)[:4], None)
            return 0

        def update(_fd, key, value, _flags):
            self.table[bytes(key)[:4]] = bytes(value)[:4]
            return 0

        self.bpf_obj_get = obj_get
        self.bpf_map_get_next_key = get_next_key
        self.bpf_map_delete_elem = delete
        self.bpf_map_update_elem = update


class RuntimeEgressReviewFixTests(unittest.TestCase):
    """阶段 1 审核修正：探针表对账、probing 依赖、专用接口 Peer 与部署核验不写死。"""

    def _maps(self, keys):
        import socket

        maps = deploy.EgressKernelMaps.__new__(deploy.EgressKernelMaps)
        maps.pin_root = Path("/fixture/pin")
        maps.library = _FakeLibbpf([socket.inet_aton(item) for item in keys])
        return maps

    def test_probe_table_is_reconciled_against_actual_kernel_keys(self):
        import socket

        maps = self._maps(["203.0.113.1", "203.0.113.2", "203.0.113.3"])
        result = maps.sync_probes({"203.0.113.2", "198.51.100.9"})
        self.assertEqual(result, {"removed": 2, "current": 2})
        self.assertEqual(sorted(maps.library.table), sorted(socket.inet_aton(item) for item in ("203.0.113.2", "198.51.100.9")))
        # 守护重启后进程内没有旧记忆，仍以内核实际键为准；空集合清空全部探针目的。
        self.assertEqual(maps.sync_probes(set()), {"removed": 2, "current": 0})
        self.assertEqual(maps.library.table, {})

    def test_probe_rotation_never_accumulates_toward_table_limit(self):
        maps = self._maps([])
        for index in range(600):
            maps.sync_probes({f"198.51.{index // 250}.{index % 250 + 1}"})
            self.assertLessEqual(len(maps.library.table), 1)

    def test_probing_keeps_dependencies_closed_until_admission(self):
        """重建后 probing 期间只发探针租期：内网依赖与入站业务均闭锁，完成准入后才放行（方案"先闭锁"）。"""

        policy = policy_fixture()
        inventory = {"services": {
            "sub2apiplus": {"bindings": [{"ifindex": 2, "host_ifindex": 11, "source_ipv4": "172.20.0.2"}],
                            "dependencies": [{"ipv4": "172.20.0.4", "protocol": "tcp", "port": 5432}]},
            "capture-cli": {"bindings": [{"ifindex": 2, "host_ifindex": 12, "source_ipv4": "172.20.0.6"}],
                            "dependencies": [{"ipv4": "172.20.0.2", "protocol": "tcp", "port": 8080}]},
        }, "bypass_ifindices": []}
        marks = deploy.egress_service_marks(policy)
        text = deploy.egress_lease_transaction(policy, "origin", inventory,
                                               {"sub2apiplus": "probing", "capture-cli": "compliant"}, [], shared=True)
        self.assertNotIn("172.20.0.4 . tcp . 5432", text)
        self.assertNotIn(f"{marks['sub2apiplus'] | 0x10000} . 8080", text)
        self.assertIn(f"{marks['capture-cli']} . 172.20.0.2 . tcp . 8080", text)

    def test_wireguard_observation_ignores_monitor_peers_and_rejects_extra_business_peer(self):
        policy = policy_fixture()
        node, peer = policy["nodes"]["origin"], policy["nodes"]["exit"]
        endpoint = f"{peer['endpoint']['ipv4']}:{peer['endpoint']['port']}"
        extra = {"peers": ""}
        def command(argv, **_kwargs):
            if argv[:2] == ["wg", "show"]:
                # 既有 wg1 等监控接口可以有任意多个 Peer，但守护只允许查询策略的专用接口。
                self.assertEqual(argv[2], node["interface"])
                values = {"public-key": node["public_key"], "peers": peer["public_key"] + extra["peers"],
                          "endpoints": f"{peer['public_key']}\t{endpoint}", "allowed-ips": f"{peer['public_key']}\t0.0.0.0/0",
                          "listen-port": str(node["listen_port"]), "fwmark": hex(deploy.EGRESS_WG_MARK)}
                return values[argv[3]]
            if argv[:4] == ["ip", "-j", "addr", "show"]:
                return json.dumps([{"mtu": node["mtu"], "flags": ["UP"], "ifindex": 7, "addr_info": [
                    {"family": "inet", "local": node["tunnel_ipv4"].split("/")[0], "prefixlen": int(node["tunnel_ipv4"].split("/")[1])}]}])
            raise AssertionError(argv)
        with mock.patch.object(deploy, "egress_command", side_effect=command):
            observed = deploy.egress_wireguard_observation(policy, "origin")
            self.assertEqual(observed["peer_public_key"], peer["public_key"])
            extra["peers"] = "\nmonitor-peer-public-key="
            with self.assertRaisesRegex(deploy.DeploymentError, "专用 WireGuard"):
                deploy.egress_wireguard_observation(policy, "origin")

    def test_post_switch_requires_target_client_and_only_records_others(self):
        """R15 复审修正：本轮目标版本的客户端必须存在、可执行且自报版本一致；其余目录只记录不判定。

        修改前只要求"至少一个客户端且每个都可用"：目标版本缺失照样通过，非 x.y.z 命名或执行失败的
        其他目录反而拒掉整个部署。
        """

        target = {"path": "/opt/codex-0.156.1/bin/codex", "version": "0.156.1", "returncode": 0, "stdout": "codex-cli 0.156.1"}
        other = {"path": "/opt/codex-0.154.0/bin/codex", "version": "0.154.0", "returncode": 0, "stdout": "codex-cli 0.154.0"}
        nightly = {"path": "/opt/codex-nightly/bin/codex", "version": None, "returncode": 0, "stdout": "codex-cli 0.157.0-alpha"}
        broken_other = {"path": "/opt/codex-0.155.0/bin/codex", "version": "0.155.0", "returncode": 1, "stdout": ""}
        unexecutable = {"path": "/opt/codex-0.149.1/bin/codex", "version": "0.149.1", "returncode": None, "stdout": "",
                        "error": "PermissionError"}
        rows = deploy.verify_container_codex_binaries(
            json.dumps([unexecutable, other, broken_other, target, nightly]), "0.156.1")
        self.assertEqual([(row["path"], row["verified"]) for row in rows],
                         [(unexecutable["path"], False), (other["path"], False), (broken_other["path"], False),
                          (target["path"], True), (nightly["path"], False)])
        for label, broken in (("目标缺失", [other]), ("空清单", []), ("目标不可执行", [{**target, "returncode": 1}]),
                              ("目标自报不符", [{**target, "stdout": "codex-cli 0.154.0"}]),
                              ("目标执行异常", [{**target, "returncode": None, "stdout": "", "error": "TimeoutExpired"}])):
            with self.subTest(case=label), self.assertRaisesRegex(deploy.DeploymentError, "0[.]156[.]1"):
                deploy.verify_container_codex_binaries(json.dumps(broken), "0.156.1")
        for malformed in ("{}", json.dumps([target, "not-a-row"]), json.dumps([{**target, "path": None}]), "not-json"):
            with self.subTest(malformed=malformed), self.assertRaises(deploy.DeploymentError):
                deploy.verify_container_codex_binaries(malformed, "0.156.1")

    def test_target_codex_version_comes_from_deployed_scenario_manifest(self):
        """R15 复审修正：目标版本取自已部署工具树的目标场景清单，部署脚本里不写死版本。"""

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(deploy, "reject_untrusted_file") as trusted:
            tool_root = Path(directory).resolve()
            manifest = tool_root / deploy.TARGET_SCENARIO_MANIFEST
            for value in ("0.156.1", "0.154.0"):
                manifest.write_text(json.dumps({"codex_version": value}), encoding="utf-8")
                self.assertEqual(deploy.read_target_codex_version(tool_root), value)
            trusted.assert_called_with(manifest, label="目标场景清单")
            for bad in ({}, {"codex_version": "0.156"}, {"codex_version": "v0.156.1"}, {"codex_version": 156}, []):
                manifest.write_text(json.dumps(bad), encoding="utf-8")
                with self.subTest(bad=bad), self.assertRaises(deploy.DeploymentError):
                    deploy.read_target_codex_version(tool_root)
            repository_tool_root = Path(deploy.__file__).resolve().parent / "official_client_capture"
            expected = json.loads((repository_tool_root / deploy.TARGET_SCENARIO_MANIFEST).read_text(encoding="utf-8"))
            self.assertEqual(deploy.read_target_codex_version(repository_tool_root), expected["codex_version"])

    def test_binary_probe_records_failures_instead_of_aborting(self):
        """R15 复审修正：容器内探针对任一二进制的执行异常只记录，是否放行由目标版本判定决定。

        修改前一个不可执行的旧版本目录就让整个探针脚本异常退出，部署在核验目标版本之前失败。
        """

        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory).resolve()

            def install(name: str, body: str, mode: int) -> None:
                binary = prefix / f"codex-{name}" / "bin" / "codex"
                binary.parent.mkdir(parents=True)
                binary.write_text(body, encoding="utf-8")
                binary.chmod(mode)

            install("0.156.1", "#!/bin/sh\necho 'codex-cli 0.156.1'\n", 0o755)
            install("0.149.1", "#!/bin/sh\necho 'codex-cli 0.149.1'\n", 0o644)
            probe = deploy.CODEX_BINARY_PROBE.replace("/opt/codex-", f"{prefix}/codex-")
            done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=True)
            rows = json.loads(done.stdout.replace(str(prefix), "/opt"))
        self.assertEqual([row["path"] for row in rows], ["/opt/codex-0.149.1/bin/codex", "/opt/codex-0.156.1/bin/codex"])
        self.assertIsNone(rows[0]["returncode"])
        verified = deploy.verify_container_codex_binaries(json.dumps(rows), "0.156.1")
        self.assertEqual([row["verified"] for row in verified], [False, True])

    def test_deploy_checks_do_not_pin_provider_routes_or_client_version(self):
        import ast

        source = Path(deploy.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        forbidden = ("wg1", "51830", "fixed-bwg", "0.154.0", "144.34.230.210", "69.63.195.102", "BWG", "DMIT")
        for name in ("_preflight", "_post_switch_verify", "egress_wireguard_observation", "verify_runtime_egress"):
            literals = [node.value for node in ast.walk(functions[name]) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
            with self.subTest(function=name):
                self.assertFalse([item for item in literals for word in forbidden if word in item])


class EgressSupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        # 父 run 状态包含 R8 预算执行所需的时间锚：截止与单调时钟起点对应同一启动时刻。
        self.state = {"campaign_id": "egress-fixture", "owner_nonce": "a" * 64,
                      "started_at_epoch": time.time() - 10, "deadline_at_epoch": time.time() + 20,
                      "started_monotonic_ns": time.monotonic_ns() - 10 * 10**9,
                      "egress_guard": {"required": True, "campaign_dir": str(self.root)}}

    def client(self):
        client = supervisor.SupervisorClient(self.root, campaign_id="egress-fixture", phase="official",
                                             deadline_at_epoch=time.time() + 20, heartbeat_seconds=0.05,
                                             watchdog_timeout_seconds=1, ledger_interval_seconds=1, terminate_owner=False)
        client.owner_nonce = self.state["owner_nonce"]
        client._deadline_monotonic_ns = time.monotonic_ns() + 20 * 10**9
        for name in ("_ensure_monitor_alive", "event_start", "event_end", "event_fail", "heartbeat"):
            setattr(client, name, mock.Mock())
        client._require_started = lambda: self.root
        self.enterContext(mock.patch.object(supervisor, "_read_state", return_value=self.state))
        return client

    def test_pause_is_immutable_and_repair_does_not_resume_same_run(self):
        with mock.patch.object(arm, "require_runtime_egress", side_effect=ValueError("出口不符")):
            with self.assertRaises(supervisor.RuntimeEgressPaused):
                supervisor._check_runtime_egress(self.root, self.state)
        before = (self.root / "egress-pause.json").read_bytes()
        with mock.patch.object(arm, "require_runtime_egress", return_value={}) as reader:
            with self.assertRaises(supervisor.RuntimeEgressPaused):
                supervisor._check_runtime_egress(self.root, self.state, monitor=True)
            reader.assert_not_called()
        self.assertEqual(before, (self.root / "egress-pause.json").read_bytes())

    def test_failure_on_command_return_is_not_recorded_as_success(self):
        client = self.client()
        with mock.patch.object(arm, "require_runtime_egress", side_effect=[{}, {}, ValueError("退出时出口失效")]):
            with self.assertRaises(supervisor.RuntimeEgressPaused):
                client.run_command([sys.executable, "-c", "pass"], operation="fixture:return", timeout_seconds=3)
        client.event_end.assert_not_called()
        client.event_fail.assert_called_once()
        self.assertTrue(client._command_failed)

    def test_active_capture_cleans_up_without_waiting_for_original_deadline(self):
        client = self.client()
        ready, cleaned = self.root / "ready", self.root / "cleaned"
        source = ("import signal,time,sys\nfrom pathlib import Path\n"
                  f"def cleanup(*args):\n Path({str(cleaned)!r}).touch()\n sys.exit(0)\n"
                  f"signal.signal(signal.SIGUSR1,cleanup)\nPath({str(ready)!r}).touch()\ntime.sleep(30)\n")
        def check():
            if ready.exists():
                raise ValueError("运行中出口失效")
            return {}
        start = time.monotonic()
        with mock.patch.object(arm, "require_runtime_egress", side_effect=check), mock.patch.object(supervisor, "_process_start_ticks", side_effect=lambda pid: str(pid)):
            with self.assertRaises(supervisor.RuntimeEgressPaused):
                client.run_command([sys.executable, "-c", source], operation="fixture:capture", timeout_seconds=15, cleanup_grace_seconds=2)
        self.assertTrue(cleaned.exists())
        self.assertLess(time.monotonic() - start, 3)
        self.assertFalse(list((self.root / "egress-commands").glob("[0-9]*.json")) and
                         [path for path in (self.root / "egress-commands").glob("*.json") if not path.name.endswith(".signalled.json")])

    def test_owner_and_monitor_signal_once_and_reused_pid_is_ignored(self):
        path = self.root / "123.json"
        supervisor._write_json(path, {"pid": 123, "owner_nonce": self.state["owner_nonce"], "start_ticks": "11", "cleanup_grace_seconds": 2}, replace=False)
        with mock.patch.object(supervisor, "_process_start_ticks", return_value="11"), mock.patch.object(os, "getpgid", return_value=123), mock.patch.object(os, "kill") as kill:
            supervisor._request_egress_cleanup(self.root, self.state, path)
            supervisor._request_egress_cleanup(self.root, self.state, path)
            kill.assert_called_once_with(123, signal.SIGUSR1)
        (self.root / "123.signalled.json").unlink()
        with mock.patch.object(supervisor, "_process_start_ticks", return_value="12"), mock.patch.object(os, "kill") as kill:
            supervisor._request_egress_cleanup(self.root, self.state, path)
            kill.assert_not_called()

    def test_formal_campaign_without_trusted_fixture_ledger_requires_guard(self):
        (self.root / "campaign.json").write_text(json.dumps({"campaign_mode": "formal"}))
        (self.root / "campaign.json").chmod(0o600)
        with mock.patch.object(ledger, "find_project_ledger", return_value=None):
            self.assertTrue(arm.campaign_requires_runtime_egress(self.root))
        with mock.patch.object(ledger, "find_project_ledger", return_value=self.root), mock.patch.object(ledger, "replay_head", side_effect=ledger.ProjectLedgerError("伪造夹具")):
            with self.assertRaises(ValueError):
                arm.campaign_requires_runtime_egress(self.root)

    def test_declared_restart_waits_for_fresh_admission_and_records_both_boundaries(self):
        for shared_fault in (False, True):
            with self.subTest(shared_fault=shared_fault):
                if (self.root / "egress-pause.json").exists():
                    (self.root / "egress-pause.json").unlink()
                snapshot = runtime_fixture()
                waiting = copy.deepcopy(snapshot["runtime"])
                waiting["services"]["sub2apiplus"].update(status="blocked", admission_state="missing", container_id="", network_bindings=[], observations=[])
                if shared_fault:
                    waiting["shared_protection"]["checks"]["wireguard"] = False
                self.state.update(state="running", owner_pid=os.getppid(), monitor_pid=os.getpid(), deadline_monotonic_ns=time.monotonic_ns() + 20 * 10**9)
                environment = {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1", supervisor.CAMPAIGN_RUN_DIR_ENV: str(self.root),
                               supervisor.CAMPAIGN_RUN_OWNER_PID_ENV: str(self.state["owner_pid"]),
                               supervisor.CAMPAIGN_RUN_OWNER_NONCE_ENV: self.state["owner_nonce"],
                               supervisor.CAMPAIGN_RUN_ID_ENV: self.state["campaign_id"]}
                process = mock.Mock(returncode=0)
                process.poll.return_value = 0
                arguments = argparse.Namespace(container="sub2apiplus", compose_service=None, timeout_seconds=10, cleanup=False,
                                               command_argv=["docker", "restart", "sub2apiplus"])
                read_text = Path.read_text
                with (mock.patch.dict(os.environ, environment),
                      mock.patch.object(supervisor, "_read_state", return_value=self.state),
                      mock.patch.object(supervisor, "_process_start_ticks", return_value="11"),
                      mock.patch.object(supervisor, "_process_descends_from", return_value=True),
                      mock.patch.object(supervisor, "_owner_alive", return_value=True),
                      mock.patch.object(arm, "load_egress_policy", return_value=snapshot["policy"]),
                      mock.patch.object(arm, "_read_egress_runtime_json", return_value=waiting),
                      mock.patch.object(Path, "read_text", autospec=True, side_effect=lambda path, *args, **kwargs:
                                        waiting["boot_id"] if str(path) == "/proc/sys/kernel/random/boot_id" else read_text(path, *args, **kwargs)),
                      mock.patch.object(arm, "require_runtime_egress", side_effect=[snapshot, snapshot, ValueError("正在重建"), snapshot]),
                      mock.patch.object(supervisor.subprocess, "Popen", return_value=process) as launched):
                    if shared_fault:
                        with self.assertRaises(supervisor.RuntimeEgressPaused):
                            supervisor._egress_transition_command(arguments)
                    else:
                        self.assertEqual(supervisor._egress_transition_command(arguments), 0)
                    launched.assert_called_once()
                self.assertFalse((self.root / "egress-transition.json").exists())
                finishes = [json.loads(path.read_bytes()) for path in (self.root / "egress-transitions").glob("*.finish.json")]
                selected = [item for item in finishes if item["status"] == ("failed" if shared_fault else "passed")]
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["after_sha256"] is None, shared_fault)
                self.assertEqual((self.root / "egress-pause.json").exists(), shared_fault)

    def test_transition_scope_rejects_other_container_conflict_and_configuration_error(self):
        snapshot = runtime_fixture()
        for fault in ("other-container", "conflict", "invalid"):
            with self.subTest(fault=fault):
                status = copy.deepcopy(snapshot["runtime"])
                selected = status["services"]["sub2apiplus"]
                selected.update(status="blocked", admission_state="probing")
                if fault == "other-container":
                    status["services"]["capture-cli"].update(status="blocked", admission_state="probing")
                elif fault == "conflict":
                    selected["observations"][0]["ip_address"] = "8.8.8.8"
                else:
                    selected["admission_state"] = "invalid"
                with self.assertRaises(arm.Arm64EnvironmentReceiptError):
                    arm.validate_egress_status(snapshot["policy"], status, now_epoch=time.time(), _transitioning_service="sub2apiplus")

    def test_maintenance_does_not_authorize_new_dispatch_or_arbitrary_command(self):
        (self.root / "egress-transition.json").write_text("{}")
        with mock.patch.object(arm, "require_runtime_egress") as current:
            with self.assertRaisesRegex(supervisor.RuntimeEgressPaused, "禁止派发"):
                supervisor._check_runtime_egress(self.root, self.state)
            current.assert_not_called()
        for command in (["sh", "-c", "true"], ["docker", "exec", "sub2apiplus", "curl", "https://example.com"],
                        ["docker", "restart", "other"], ["docker", "compose", "down"]):
            with self.subTest(command=command), self.assertRaises(supervisor.SupervisorError):
                supervisor._egress_local_command(argparse.Namespace(container="sub2apiplus", compose_service="sub2api", command_argv=command))

    def test_expired_dead_tampered_or_finished_transition_pauses_even_if_network_is_ready(self):
        self.state.update(owner_pid=os.getppid(), deadline_monotonic_ns=time.monotonic_ns() + 90 * 10**9)
        now = time.monotonic_ns()
        record = {"schema_version": "codex-upgrade-egress-transition/v1", "transition_id": "1" * 32,
                  "campaign_id": self.state["campaign_id"], "owner_nonce": self.state["owner_nonce"],
                  "actor_pid": os.getpid(), "start_ticks": "11", "started_at_epoch": time.time(),
                  "started_at_monotonic_ns": now, "deadline_monotonic_ns": now + 10 * 10**9,
                  "container": "sub2apiplus", "command_sha256": "b" * 64, "policy_sha256": "c" * 64, "before_sha256": "d" * 64}
        archive = self.root / "egress-transitions" / f"{record['transition_id']}.json"
        archive.parent.mkdir(mode=0o700)
        for fault in ("dead", "expired", "too-long", "boolean-pid", "foreign-owner", "archive", "finished", "extra-field"):
            with self.subTest(fault=fault):
                changed = copy.deepcopy(record)
                if fault == "expired":
                    changed["deadline_monotonic_ns"] = now - 1
                if fault == "too-long":
                    changed["deadline_monotonic_ns"] = now + 61 * 10**9
                if fault == "boolean-pid":
                    changed["actor_pid"] = True
                if fault == "foreign-owner":
                    changed["owner_nonce"] = "e" * 64
                if fault == "extra-field":
                    changed["skip"] = True
                supervisor._write_json(archive, record if fault == "archive" else changed, replace=True)
                if fault == "archive":
                    changed["before_sha256"] = "e" * 64
                supervisor._write_json(self.root / "egress-transition.json", changed, replace=True)
                finish = archive.with_suffix(".finish.json")
                if fault == "finished":
                    finish.touch()
                with (mock.patch.object(supervisor, "_process_start_ticks", return_value=None if fault == "dead" else "11"),
                      mock.patch.object(supervisor, "_process_descends_from", return_value=True),
                      mock.patch.object(supervisor, "_egress_transition_status"),
                      mock.patch.object(arm, "require_runtime_egress", return_value=runtime_fixture()) as ordinary):
                    with self.assertRaises(supervisor.RuntimeEgressPaused):
                        supervisor._check_runtime_egress(self.root, self.state, monitor=True)
                    ordinary.assert_not_called()
                self.assertTrue((self.root / "egress-pause.json").exists())
                (self.root / "egress-pause.json").unlink()
                finish.unlink(missing_ok=True)

    def test_successful_local_restart_cannot_silently_change_policy(self):
        before = runtime_fixture()
        after = copy.deepcopy(before)
        after["policy_sha256"] = "f" * 64
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        args = argparse.Namespace(container="sub2apiplus", compose_service=None, timeout_seconds=10, cleanup=False,
                                  command_argv=["docker", "restart", "sub2apiplus"])
        with (mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "0"}),
              mock.patch.object(arm, "require_runtime_egress", side_effect=[before, after]),
              mock.patch.object(supervisor.subprocess, "Popen", return_value=process)):
            with self.assertRaisesRegex(supervisor.RuntimeEgressPaused, "策略发生变化"):
                supervisor._egress_transition_command(args)

    def _transition_context(self, readiness):
        """正式父 run 上下文中的维护入口；出口准入按给定序列返回，其余身份核验为已绑定。"""

        self.state.update(state="running", owner_pid=os.getppid(), monitor_pid=os.getpid(),
                          deadline_monotonic_ns=time.monotonic_ns() + 20 * 10**9)
        environment = {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1", supervisor.CAMPAIGN_RUN_DIR_ENV: str(self.root),
                       supervisor.CAMPAIGN_RUN_OWNER_PID_ENV: str(self.state["owner_pid"]),
                       supervisor.CAMPAIGN_RUN_OWNER_NONCE_ENV: self.state["owner_nonce"],
                       supervisor.CAMPAIGN_RUN_ID_ENV: self.state["campaign_id"]}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, environment))
        stack.enter_context(mock.patch.object(supervisor, "_read_state", return_value=self.state))
        stack.enter_context(mock.patch.object(supervisor, "_process_start_ticks", return_value="11"))
        stack.enter_context(mock.patch.object(supervisor, "_process_descends_from", return_value=True))
        stack.enter_context(mock.patch.object(supervisor, "_owner_alive", return_value=True))
        stack.enter_context(mock.patch.object(arm, "require_runtime_egress", side_effect=readiness))
        # 维护进行中执行端核验声明绑定时读取守护状态：按首个准入快照提供合规状态与本机启动身份。
        snapshot = readiness[0]
        read_text = Path.read_text
        stack.enter_context(mock.patch.object(arm, "load_egress_policy", return_value=snapshot["policy"]))
        stack.enter_context(mock.patch.object(arm, "_read_egress_runtime_json", return_value=snapshot["runtime"]))
        stack.enter_context(mock.patch.object(Path, "read_text", autospec=True, side_effect=lambda path, *args, **kwargs:
                                              snapshot["runtime"]["boot_id"] if str(path) == "/proc/sys/kernel/random/boot_id"
                                              else read_text(path, *args, **kwargs)))
        return stack

    @staticmethod
    def _restart_arguments():
        return argparse.Namespace(container="sub2apiplus", compose_service=None, timeout_seconds=10, cleanup=False,
                                  command_argv=["docker", "restart", "sub2apiplus"])

    def test_cleanup_signal_handler_is_installed_before_transition_declaration(self):
        snapshot = runtime_fixture()
        observed = []
        real_write = supervisor._write_json
        def write(path, *args, **kwargs):
            if Path(path).name == "egress-transition.json":
                observed.append(signal.getsignal(signal.SIGUSR1))
            return real_write(path, *args, **kwargs)
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        with self._transition_context([snapshot] * 4), mock.patch.object(supervisor, "_write_json", side_effect=write), \
                mock.patch.object(supervisor.subprocess, "Popen", return_value=process):
            self.assertEqual(supervisor._egress_transition_command(self._restart_arguments()), 0)
        self.assertEqual(len(observed), 1)
        self.assertNotIn(observed[0], (signal.SIG_DFL, signal.SIG_IGN, None))
        self.assertFalse((self.root / "egress-transition.json").exists())

    def test_failed_maintenance_command_output_is_logged_and_reported(self):
        snapshot = runtime_fixture()
        def launch(command, *, stdin, stdout, stderr, shell):
            stdout.write(b"Error response from daemon: fixture restart failure\n")
            stdout.flush()
            self.assertEqual(stderr, subprocess.STDOUT)
            process = mock.Mock(returncode=1)
            process.poll.return_value = 1
            return process
        with self._transition_context([snapshot] * 2), mock.patch.object(supervisor.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(supervisor.SupervisorError, "fixture restart failure"):
                supervisor._egress_transition_command(self._restart_arguments())
        logs = list((self.root / "egress-transitions").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        # 声明之后的失败仍按原合同写暂停并收口声明。
        self.assertTrue((self.root / "egress-pause.json").exists())
        self.assertFalse((self.root / "egress-transition.json").exists())

    def test_parallel_maintenance_rejection_keeps_other_declaration_and_does_not_pause(self):
        snapshot = runtime_fixture()
        supervisor._write_json(self.root / "egress-transition.json", {"transition_id": "f" * 32, "marker": "other"}, replace=False)
        before = (self.root / "egress-transition.json").read_bytes()
        with self._transition_context([snapshot] * 2), mock.patch.object(supervisor.subprocess, "Popen") as launched:
            with self.assertRaises(supervisor.RuntimeEgressPaused):
                supervisor._egress_transition_command(self._restart_arguments())
            launched.assert_not_called()
        self.assertEqual((self.root / "egress-transition.json").read_bytes(), before)
        self.assertFalse((self.root / "egress-pause.json").exists())
        self.assertFalse(list((self.root / "egress-transitions").glob("*.finish.json")) if (self.root / "egress-transitions").exists() else [])

    def test_unverifiable_job_egress_binding_fails_closed_with_explicit_reason(self):
        binding = {"schema_version": "codex-upgrade-job-egress/v1", "run_dir": str(self.root / "missing-parent-run"),
                   "campaign_id": self.state["campaign_id"], "owner_nonce": self.state["owner_nonce"],
                   "started_at_epoch": time.time() - 5, "finished_at_epoch": time.time() - 1}
        with self.assertRaisesRegex(reconciler.ReconcilerError, "出口时段绑定无法核验"):
            reconciler._job_egress_trusted({"id": "job", "status": "complete", "runtime_egress": binding})
        # 没有出口绑定的历史结果按原合同读取，不因父 run 归档规则改判。
        self.assertTrue(reconciler._job_egress_trusted({"id": "legacy", "status": "complete"}))

    def test_cleanup_signal_preserves_egress_failure_class(self):
        supervisor._egress_pause(self.root, self.state, "父监督器已确认出口异常")
        environment = {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1", supervisor.CAMPAIGN_RUN_CLEANUP_SIGNAL_ENV: "SIGUSR1",
                       supervisor.CAMPAIGN_RUN_DIR_ENV: str(self.root), supervisor.CAMPAIGN_RUN_OWNER_NONCE_ENV: self.state["owner_nonce"],
                       supervisor.CAMPAIGN_RUN_ID_ENV: self.state["campaign_id"]}
        with mock.patch.dict(os.environ, environment), mock.patch.object(supervisor, "_read_state", return_value=self.state):
            with upgrade._campaign_cleanup_signal_guard(), self.assertRaises(supervisor.RuntimeEgressPaused) as caught:
                os.kill(os.getpid(), signal.SIGUSR1)
            self.assertEqual(caught.exception.failure_class, "environment-prerequisite")

    def test_reconciliation_keeps_only_completed_jobs_before_uncertain_window(self):
        now = time.time()
        self.state.update(started_at_epoch=now - 20, deadline_at_epoch=now + 60)
        last = runtime_fixture()
        for service in last["runtime"]["services"].values():
            for item in service["observations"]:
                item["observed_at_epoch"] = now - 5
        supervisor._write_json(self.root / "egress-last-valid.json", last, replace=False)
        supervisor._egress_pause(self.root, self.state, "隔离出口中断")
        results = []
        for name, finished in (("trusted", now - 8), ("uncertain", now - 2)):
            results.append({"id": name, "execution_sha256": "e" * 64, "status": "complete",
                            "runtime_egress": {"schema_version": "codex-upgrade-job-egress/v1", "run_dir": str(self.root),
                                               "campaign_id": self.state["campaign_id"], "owner_nonce": self.state["owner_nonce"],
                                               "started_at_epoch": finished - 1, "finished_at_epoch": finished}})
        reservation = {"planned_jobs": [{"id": item["id"], "execution_sha256": item["execution_sha256"]} for item in results]}
        with mock.patch.object(supervisor, "_read_state", return_value=self.state):
            for _ in range(2):
                jobs = reconciler._classify_jobs(self.root, {}, reservation, {"results": results},
                                                 {item["id"]: {"status": "complete"} for item in results}, self.root)
                self.assertEqual(jobs["groups"]["complete"], ["trusted"])
                self.assertEqual(jobs["groups"]["indeterminate"], ["uncertain"])
            preview_dir = self.root / "preview"
            preview_dir.mkdir(mode=0o700)
            preview = reconciler._recovery_preview(
                self.root, preview_dir, manifest={"campaign_id": self.state["campaign_id"]}, attempt_id="a1",
                phase="official", candidate_id=None, attempt_exists=True, jobs=jobs, environment_status="restored",
                provenance_copy={"jobs": []}, current={}, reconciliation_receipt_sha256="e" * 64,
                campaign_ledger_head={}, project_ledger_head={}, now=datetime.now(timezone.utc).isoformat())
            self.assertEqual(preview["reuse_job_ids"], ["trusted"])
            self.assertEqual(preview["execute_job_ids"], ["uncertain"])
            self.assertEqual(preview["live_request_count"], 0)
            changed = json.loads((self.root / "egress-pause.json").read_bytes())
            changed["uncertain_window_start_epoch"] = now
            supervisor._write_json(self.root / "egress-pause.json", changed, replace=True)
            with self.assertRaisesRegex(supervisor.SupervisorError, "被修改"):
                supervisor.job_egress_trusted(results[0])


class EgressInstallTests(unittest.TestCase):
    def test_partial_pin_update_attaches_closed_replacement_before_removing_old_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group, bpffs, runtime = root / "cgroup/sub2api_egress.slice", root / "bpf/sub2api-egress", root / "runtime"
            group.mkdir(parents=True)
            bpffs.mkdir(parents=True)
            runtime.mkdir()
            identity = deploy.sha256_bytes((deploy.EGRESS_FILTER_SOURCE + deploy.EGRESS_FILTER_LOADER).encode())
            old = bpffs / ("sub2api_egress.slice-" + identity[:16])
            old.mkdir()
            (old / "link").touch()
            path_class = Path
            def redirect(value):
                return {"/sys/fs/cgroup": root / "cgroup", "/sys/fs/bpf/sub2api-egress": bpffs}.get(str(value), path_class(value))
            def command(argv, **kwargs):
                if argv[:2] == ["systemctl", "show"]:
                    return "/sub2api_egress.slice"
                if argv[0].endswith("egress-filter-loader"):
                    self.assertTrue((old / "link").exists(), "挂载替代默认拒绝程序前不能删除旧保护")
                    (Path(argv[-1]) / "link").touch()
                return ""
            def kernel(pin):
                result = mock.Mock()
                if pin == old:
                    result.verify_attachment.side_effect = deploy.DeploymentError("隔离半成品 pin")
                return result
            with (mock.patch.object(deploy, "Path", side_effect=redirect),
                  mock.patch.object(arm, "load_egress_policy", return_value=policy_fixture()),
                  mock.patch.object(deploy, "egress_apply_firewall", return_value="f" * 64),
                  mock.patch.object(deploy, "build_egress_filter", return_value={}),
                  mock.patch.object(deploy, "egress_command", side_effect=command),
                  mock.patch.object(deploy, "EgressKernelMaps", side_effect=kernel)):
                result = deploy.egress_bootstrap(root / "policy.json", "origin", runtime)
            self.assertFalse(old.exists())
            self.assertTrue(Path(result["pins"]["sub2api_egress.slice"]).is_dir())
            self.assertEqual(result["policy_sha256"], arm.egress_policy_sha256(policy_fixture()))

    def test_bootstrap_does_not_contact_docker_before_service_start(self):
        for active in (False, True):
            with self.subTest(active=active):
                with (mock.patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0 if active else 3)),
                      mock.patch.object(deploy, "egress_inventory", return_value={"bypass_ifindices": [17]}) as inventory,
                      mock.patch.object(deploy, "egress_command", side_effect=[json.dumps({"nftables": []}), ""]) as command,
                      mock.patch.object(deploy, "egress_firewall_identity", return_value="f" * 64)):
                    deploy.egress_apply_firewall(policy_fixture(), "origin")
                self.assertEqual(inventory.call_count, int(active))
                transaction = command.call_args.kwargs["input_text"]
                self.assertEqual("add element bridge sub2api_egress bypass { 17 }" in transaction, active)
                self.assertNotIn("add element inet sub2api_egress shared", transaction)

    def test_compose_preserves_writable_hosts_and_only_names_required_dependencies(self):
        policy = policy_fixture()
        items = [{"Name": "/sub2apiplus", "NetworkSettings": {"Networks": {"db": {}}}},
                 {"Name": "/postgres", "NetworkSettings": {"Networks": {"db": {"IPAddress": "172.20.0.4", "Aliases": ["postgres", "sub2api-postgres"]}}}}]
        settings = deploy.egress_compose_settings(policy, "sub2apiplus", items, Path("/etc/fixture/resolv.conf"))
        self.assertEqual(settings["cgroup_parent"], policy["services"]["sub2apiplus"]["cgroup_parent"])
        self.assertEqual(settings["extra_hosts"], {"postgres": "172.20.0.4", "sub2api-postgres": "172.20.0.4"})
        self.assertEqual([item["target"] for item in settings["volumes"]], ["/etc/resolv.conf"])
        self.assertTrue(settings["volumes"][0]["read_only"])

    def test_install_never_restarts_docker_dependency_and_closes_before_config_update(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                units, wireguard, runtime = root / "units", root / "wireguard", root / "runtime"
                units.mkdir()
                runtime.mkdir()
                if existing:
                    (runtime / "installed.json").write_text("{}")
                policy = policy_fixture()
                contract, key = mock.Mock(), mock.Mock()
                contract.load_egress_policy.return_value = policy
                contract._read_egress_runtime_json.return_value = {"status": "installed"}
                key.is_symlink.return_value, key.is_file.return_value = False, True
                key.stat.return_value = mock.Mock(st_uid=0, st_gid=0, st_mode=0o600)
                key.read_text.return_value = "isolated-private-key"
                commands, order = [], []
                def command(argv, **kwargs):
                    commands.append(argv)
                    if argv[:2] == ["wg", "pubkey"]:
                        return policy["nodes"]["origin"]["public_key"]
                    if argv[:3] == ["ip", "-j", "link"]:
                        return "[]"
                    order.append(tuple(argv))
                    return ""
                original_path = Path
                def redirect(value):
                    return {"/etc/systemd/system": units, "/etc/wireguard": wireguard}.get(str(value), original_path(value))
                with (mock.patch.object(deploy, "Path", side_effect=redirect),
                      mock.patch.object(deploy, "egress_contract", return_value=contract),
                      mock.patch.object(deploy, "egress_runtime_bundle", return_value=root / "bundle/tools/arm64_supervised_deploy.py"),
                      mock.patch.object(deploy, "egress_command", side_effect=command),
                      mock.patch.object(deploy, "egress_apply_firewall", side_effect=lambda *args: order.append("closed")),
                      mock.patch.object(deploy, "egress_bootstrap") as bootstrap,
                      mock.patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0 if existing else 3))):
                    deploy.egress_install(root / "policy.json", "origin", runtime, key)
                self.assertNotIn(["systemctl", "restart", "sub2api-egress-bootstrap.service"], commands)
                self.assertNotIn(["systemctl", "restart", "docker.service"], commands)
                self.assertEqual(bootstrap.call_count, int(existing))
                self.assertLess(order.index("closed"), order.index(("systemctl", "daemon-reload")))
                source = (units / "sub2api-egress-bootstrap.service").read_text()
                self.assertIn(str(root / "bundle/tools/arm64_supervised_deploy.py"), source)
                self.assertIn("Before=docker.service", source)
                self.assertEqual((wireguard / "wg-egress.conf").stat().st_mode & 0o777, 0o600)

    def test_runtime_bundle_survives_source_staging_removal_and_detects_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = deploy.egress_runtime_bundle(root)
            self.assertTrue(script.is_relative_to(root / "bundles"))
            self.assertEqual(script, deploy.egress_runtime_bundle(root))
            result = subprocess.run([sys.executable, str(script), "egress-check", "--help"], cwd=root,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, "-c", "from tools import arm64_supervised_deploy as d; d.egress_contract().validate_egress_policy(__import__('json').load(open(__import__('sys').argv[1])))", str(Path(__file__).parent / "fixtures/runtime_egress_policy.json")],
                                    cwd=script.parent.parent, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            script.chmod(0o600)
            script.write_text("破坏已发布包")
            with self.assertRaisesRegex(deploy.DeploymentError, "被修改"):
                deploy.egress_runtime_bundle(root)

    def test_atomic_config_interruption_keeps_old_bytes_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wireguard.conf"
            deploy.egress_write_atomic(path, b"old")
            with mock.patch.object(deploy.os, "replace", side_effect=OSError("隔离中断")):
                with self.assertRaises(OSError):
                    deploy.egress_write_atomic(path, b"new")
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_firewall_identity_keeps_static_private_network_set(self):
        def document(address):
            return {"nftables": [{"set": {"name": "private4", "elem": [address]}},
                                 {"set": {"name": "shared", "elem": [0]}}]}
        with mock.patch.object(deploy, "egress_command", return_value=json.dumps(document("10.0.0.0/8"))):
            before = deploy.egress_firewall_identity("exit")
        with mock.patch.object(deploy, "egress_command", return_value=json.dumps(document("192.168.0.0/16"))):
            self.assertNotEqual(before, deploy.egress_firewall_identity("exit"))




if __name__ == "__main__":
    unittest.main()
