"""Claude 官方取证 Job 动作体与环境闭环（Claude 升级控制面设计 B2-06）。

不起 Docker、不联网、不读凭据：
* relay 用夹具替身（``claude_fw_e_relay.py`` 同名脚本，dry-run 回显计划、execute 写 relay 清单），步骤仍经真实
  Campaign 租约的独立监督器 ``SupervisorClient.run_command`` 执行；
* 宿主命令边界（宿主运行凭据签发、容器内可见核对、凭据与 CA 文件存在性）由 ``FakeHost`` 替身应答；
* 环境探针的采集函数换成读一份可被替身 relay 改写的环境文件，用来制造「执行后漂移」。

场景清单取 B2-05 的真实 2.1.285 清单，裁剪到少数 Job 并去掉 ``docker exec <容器>`` 前缀（B2-05 的步骤校验不要求它），
测试里的「容器坐标」与宿主坐标相同（``capture_root`` 指向临时数据根）。
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_evidence_permissions as permissions
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture.capturelib.model import ConfigurationError

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
TARGET = "2.1.285"
BASELINE = "2.1.226"
TARGET_SHA = "a" * 64
IMAGE = "oauth-egress-capture-capture-cli@sha256:" + "b" * 64
SMALL_SYNTHETIC_JOB = "official-synthetic-oauth-refresh"  # 1 个合成 probe
SMALL_LIVE_JOB = "official-transport-baseline"  # 4 个真实上游 probe（零请求演练改写成 dry-run）

FAKE_RELAY = r'''#!/usr/bin/env python3
"""claude_fw_e_relay.py 的离线替身：dry-run 回显计划；execute 核对宿主凭据摘要后写 relay 清单与一份非空产物。"""
import hashlib, json, os, sys
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
control = json.load(open(os.environ["FAKE_RELAY_CONTROL"], encoding="utf-8"))
probe = value("--probe-id")
dry = "--dry-run" in args
with open(os.environ["FAKE_RELAY_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"probe": probe, "dry_run": dry, "execute": "--execute" in args,
                             "acknowledgements": [item for item in args if item.startswith("--acknowledge")]}) + "\n")
output = value("--output-root")
if dry:
    if os.path.exists(output):
        print("output exists", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"schema_version": "claude-code-fw-e-relay-plan/v1", "run_id": value("--run-id"), "probe_id": probe,
                      "version": value("--expected-version"), "live_requests": value("--upstream-ip") is not None,
                      "upstream_ip": value("--upstream-ip"), "production_changes": False}, indent=2, sort_keys=True))
    sys.exit(0)
receipt = value("--host-runtime-receipt")
digest = hashlib.sha256(open(receipt, "rb").read()).hexdigest()
if digest != value("--host-runtime-receipt-sha256"):
    print("receipt mismatch", file=sys.stderr)
    sys.exit(2)
counts_path = os.environ["FAKE_RELAY_CONTROL"] + ".counts"
counts = json.load(open(counts_path, encoding="utf-8")) if os.path.exists(counts_path) else {}
counts[probe] = counts.get(probe, 0) + 1
json.dump(counts, open(counts_path, "w", encoding="utf-8"))
os.makedirs(output, mode=0o700)
with open(os.path.join(output, "relay-raw.bin"), "wb") as handle:
    handle.write(b"POST /v1/messages HTTP/1.1\r\n\r\n")
if control.get("contaminate") == probe:
    env_path = os.environ["FAKE_RELAY_ENV"]
    environment = json.load(open(env_path, encoding="utf-8"))
    environment["configuration"]["hosts_sha256"] = "f" * 64
    json.dump(environment, open(env_path, "w", encoding="utf-8"))
status = "failed" if counts[probe] <= int(control.get("fail", {}).get(probe, 0)) else "complete"
manifest = {"schema_version": "claude-code-fw-e-relay-run/v1", "probe_id": probe, "status": status,
            "m_binding": {"complete": status == "complete"}, "secret_scan": {"passed": True},
            "cleanup": {"relay_stopped": True, "hosts_restored": True},
            "client": {"version": value("--expected-version") + " (Claude Code)", "sha256": value("--expected-sha256")},
            "runtime": {"host_runtime_receipt": {"sha256": digest, "run_nonce": value("--run-nonce")}}}
with open(os.path.join(output, "relay-manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle)
sys.exit(0 if status == "complete" else 2)
'''

BASE_ENVIRONMENT = {
    "containers": {"probe_kind": "containers", "containers": [{"role": "capture", "name": "capture-cli", "id": "c" * 64, "started_at": "t0"}]},
    "configuration": {"probe_kind": "configuration", "hosts_sha256": "1" * 64},
    "client": {"probe_kind": "client", "global_state": {"exists": True, "mtime_ns": 1}},
    "processes": {"probe_kind": "processes", "capture_processes": []},
    "listeners": {"probe_kind": "listeners", "tcp_listen_ports": [{"family": "tcp", "port": 22}]},
}


def _baseline_rules() -> tuple[str, ...]:
    return plan.load_baseline_rules(plan.baseline_rule_manifest_path(BASELINE), BASELINE)


def _reduced_payload(job_ids: set[str], *, strip_docker: bool = True) -> dict:
    """真实 2.1.285 场景清单裁剪到指定 Job（场景只保留被执行的 probe），可去掉 ``docker exec <容器>`` 前缀。"""

    payload = json.loads(plan.scenario_manifest_path(TARGET).read_text(encoding="utf-8"))
    jobs = [job for job in payload["capture_jobs"] if job["id"] in job_ids]
    executed = {step["probe_id"] for job in jobs for step in job["steps"]}
    scenarios = []
    for scenario in payload["evidence_scenarios"]:
        probes = [probe for probe in scenario["probe_ids"] if probe in executed]
        if probes:
            scenarios.append({**scenario, "probe_ids": probes})
    if strip_docker:
        for job in jobs:
            for step in job["steps"]:
                assert step["argv"][:3] == ["docker", "exec", "{capture_container}"], step["argv"][:3]
                step["argv"] = step["argv"][3:]
    payload["capture_jobs"] = jobs
    payload["evidence_scenarios"] = scenarios
    payload["probe_catalog"] = {**payload["probe_catalog"], "probe_count": len(executed)}
    return payload


class FakeHost:
    """宿主命令边界替身：签发宿主运行凭据、容器内 sha256sum、凭据与 CA 文件存在性（测试里容器坐标即宿主坐标）。"""

    def __init__(self, *, files_present: bool = True) -> None:
        self.files_present = files_present
        self.calls: list[list[str]] = []

    def run(self, argv, *, operation, timeout_seconds=30, deadline=None, heartbeat=None, check=True):
        del operation, timeout_seconds, deadline, heartbeat
        argv = [str(item) for item in argv]
        self.calls.append(argv)
        if argv[1].endswith("runtime_host_receipt.py"):
            value = lambda flag: argv[argv.index(flag) + 1]  # noqa: E731
            output = Path(value("--output"))
            output.write_text(
                json.dumps(
                    {
                        "schema_version": "official-client-runtime-host-receipt/v1",
                        "issued_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "run_nonce": value("--run-nonce"),
                        "container": {"name": value("--container"), "id": "c" * 64, "hostname": "capture"},
                        "runtime_image_reference": value("--runtime-image"),
                        "runtime_image_id": "sha256:" + "d" * 64,
                        "repo_digest_verified": True,
                        "capture_source_bundle": {"sha256": "e" * 64, "files": []},
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            output.chmod(0o600)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["docker", "exec"] and argv[3:5] == ["test", "-f"]:
            code = 0 if self.files_present else 1
            if check and code:
                raise ConfigurationError("missing")
            return subprocess.CompletedProcess(argv, code, "", "")
        if argv[:2] == ["docker", "exec"] and argv[3] == "/usr/bin/sha256sum":
            digest = hashlib.sha256(Path(argv[4]).read_bytes()).hexdigest()
            return subprocess.CompletedProcess(argv, 0, f"{digest}  {argv[4]}\n", "")
        raise AssertionError(f"FakeHost 收到未登记的命令：{argv}")


class CaptureFixture(unittest.TestCase):
    """临时数据根 + 替身 relay + Campaign 写入器；所有 Docker／宿主边界都在 ``patched()`` 里替换。"""

    tool_identity: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool_identity = claude_upgrade._tool_identity(include_git=False)

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        (self.data / "runs").mkdir(parents=True, mode=0o700)
        (self.data / "evidence" / "campaigns").mkdir(parents=True, mode=0o700)
        self.tool_copy = self.data / "tools" / "official_client_capture"
        self.tool_copy.mkdir(parents=True, mode=0o700)
        relay = self.tool_copy / "claude_fw_e_relay.py"
        relay.write_text(FAKE_RELAY, encoding="utf-8")
        relay.chmod(0o700)
        self.control_file = self.root / "relay-control.json"
        self.env_file = self.root / "environment.json"
        self.calls_file = self.root / "relay-calls.jsonl"
        self.set_control({})
        self.env_file.write_text(json.dumps(BASE_ENVIRONMENT), encoding="utf-8")
        self.host = FakeHost()
        self.execution_tree_calls: list[Path | None] = []

    def set_control(self, control: dict) -> None:
        self.control_file.write_text(json.dumps(control), encoding="utf-8")

    def write_campaign(
        self,
        payload: dict,
        *,
        mode: str = "formal",
        campaign_id: str = "claude-code-2_1_285-t1",
        configuration: dict | None = None,
        jobs: list | None = None,
    ) -> Path:
        campaign = self.data / "evidence" / "campaigns" / campaign_id
        (campaign / "inputs").mkdir(parents=True, mode=0o700)
        scenario = campaign / "inputs" / "target-scenarios.json"
        scenario.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        scenario.chmod(0o600)
        settings = {
            "platform": "linux/arm64",
            "runtime_image": IMAGE,
            "capture_root": str(self.data),
            "capture_tool_root": str(self.tool_copy),
            "capture_container": "capture-cli",
            "claude_bin": f"/opt/claude-{TARGET}/claude",
            "model": "claude-sonnet-5",
            "upstream_ip": "160.79.104.10",
            "credentials_file": "/root/.claude/.credentials.json",
            "ca_signing_pem": "/opt/mitm/mitmproxy-ca.pem",
            "ca_cert": "/opt/mitm/mitmproxy-ca-cert.pem",
            **(configuration or {}),
        }
        manifest = {
            "schema_version": "official-client-upgrade-campaign/v1", "client": "claude", "campaign_id": campaign_id,
            "created_at_utc": "2026-10-07T00:00:00Z", "campaign_mode": mode, "campaign_purpose": "validation_only",
            "baseline_version": BASELINE, "target_version": TARGET, "target_sha256": TARGET_SHA,
            "official_identity": {"npm": {}, "gcs": {}, "binary_sha256": TARGET_SHA, "binary_equivalence": "npm=gcs"},
            "baseline_identity": {}, "tool_identity": self.tool_identity, "control_receipts": {},
            "inputs": {"target_scenarios": {"path": "inputs/target-scenarios.json", "sha256": codex_upgrade.file_sha256(scenario)}},
            "configuration": settings, "required_rules": list(_baseline_rules()), "coverage_plan": {},
            "jobs": jobs or [], "vc_control": {}, "predecessor": None,
        }
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        (campaign / "campaign.json").write_bytes(raw)
        (campaign / "campaign.json").chmod(0o600)
        (campaign / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
        return campaign

    def fake_states(self, manifest, *, deadline=None, heartbeat=None):
        del manifest, deadline, heartbeat
        return json.loads(self.env_file.read_text(encoding="utf-8"))

    def fake_binaries(self, manifest, *, deadline=None, heartbeat=None):
        del deadline, heartbeat
        return {"schema_version": capture.BINARY_VERIFICATION_SCHEMA, "target_sha256": manifest["target_sha256"], "fake": True}

    @contextlib.contextmanager
    def patched(self):
        runs = self.data / "runs"
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(claude_upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", runs))
            stack.enter_context(
                mock.patch.object(
                    claude_upgrade,
                    "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS",
                    (PurePosixPath(str(runs)), PurePosixPath("/root/oauth-capture/runs")),
                )
            )
            stack.enter_context(mock.patch.object(claude_upgrade, "JOB_RETRY_DELAY_SECONDS", 0))
            stack.enter_context(mock.patch.object(capture, "_collect_probe_states", self.fake_states))
            stack.enter_context(mock.patch.object(capture, "_run_probe_command", self.host.run))
            stack.enter_context(mock.patch.object(claude_upgrade, "_verify_official_binaries", self.fake_binaries))
            stack.enter_context(mock.patch.object(claude_upgrade, "_verify_execution_tree", self.execution_tree_calls.append))
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {
                        "FAKE_RELAY_CONTROL": str(self.control_file),
                        "FAKE_RELAY_ENV": str(self.env_file),
                        "FAKE_RELAY_CALLS": str(self.calls_file),
                    },
                )
            )
            yield

    def run_main(self, argv: list[str]) -> tuple[int, dict | None, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = claude_upgrade.main(argv)
        text = stdout.getvalue()
        return code, (json.loads(text) if text.strip() else None), stderr.getvalue()

    def relay_calls(self) -> list[dict]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()]


class CampaignJobsTests(CaptureFixture):
    """服务面 ``_campaign_jobs``：从 Campaign 冻结副本展开，wire 根 ``_campaign_arguments`` 只读传入的总计划。"""

    def test_official_jobs_expand_from_frozen_copy(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB, SMALL_LIVE_JOB}))
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        jobs = claude_upgrade._campaign_jobs(campaign, manifest, "official")
        self.assertEqual({job.job_id for job in jobs}, {SMALL_SYNTHETIC_JOB, SMALL_LIVE_JOB})
        for job in jobs:
            self.assertEqual(job.evidence_roots, (f"{self.data}/runs/claude-official/claude-code-2_1_285-t1/{job.job_id}",))
            for step in job.steps:
                self.assertEqual(set(step["runtime_variables"]), set(capture.claude_upgrade_plan.runtime_variable_types(_reduced_payload({job.job_id}))))
                placeholders = {field for item in step["argv"] for field in plan.TEMPLATE_FIELD_RE.findall(item)}
                self.assertEqual(placeholders, {"host_runtime_receipt", "host_runtime_receipt_sha256", "run_nonce"})
        again = claude_upgrade._campaign_jobs(campaign, manifest, "official")
        self.assertEqual([claude_upgrade._job_execution_sha256(job) for job in jobs], [capture._job_execution_sha256(job) for job in again])

    def test_frozen_rows_and_bindings_fail_closed(self) -> None:
        payload = _reduced_payload({SMALL_SYNTHETIC_JOB})
        rows = [{"id": SMALL_SYNTHETIC_JOB, "phase": "official", "execution_sha256": "0" * 64}]
        campaign = self.write_campaign(payload, jobs=rows)
        with self.assertRaisesRegex(ConfigurationError, "执行摘要漂移"):
            claude_upgrade._campaign_jobs(campaign, claude_upgrade.load_campaign_manifest(campaign), "official")
        other = self.write_campaign(payload, campaign_id="claude-code-2_1_285-t2", configuration={"capture_tool_root": ""})
        with self.assertRaisesRegex(ConfigurationError, "capture_tool_root"):
            claude_upgrade._campaign_jobs(other, claude_upgrade.load_campaign_manifest(other), "official")
        with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, "B4-01"):
            claude_upgrade._campaign_jobs(campaign, claude_upgrade.load_campaign_manifest(campaign), "candidate", candidate_id="c1")

    def test_campaign_arguments_reads_only_the_given_manifest(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB}))
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with mock.patch.object(claude_upgrade, "load_campaign_manifest", side_effect=AssertionError("wire 根不得读总计划")):
            arguments = claude_upgrade._campaign_arguments(campaign, manifest)
            context = claude_upgrade._job_context(arguments)
        self.assertEqual(set(context), set(plan.CAMPAIGN_VARIABLES))
        self.assertEqual(context["capture_tool_root"], str(self.tool_copy))
        with self.assertRaisesRegex(ConfigurationError, "越过|非法"):
            capture._campaign_relative_file(campaign, "../escape.json")

    def test_tool_dependencies_map_policy_ignored_files(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_LIVE_JOB}, strip_docker=False))
        job = claude_upgrade._campaign_jobs(campaign, claude_upgrade.load_campaign_manifest(campaign), "official")[0]
        files = claude_upgrade._job_tool_dependency_files(job, self.tool_identity)
        self.assertEqual(len(files), 38)
        self.assertIn("official_client_capture/drive_codex_model_catalog.py", files)
        self.assertIn("official_client_capture/claude_fw_e_relay.py", files)
        policy = claude_identity.load_policy()
        self.assertIsNone(claude_identity.classify_path(policy, "official_client_capture/drive_codex_model_catalog.py"))
        broken = dict(self.tool_identity)
        broken["entries"] = [entry for entry in self.tool_identity["entries"] if not entry["path"].endswith("drive_codex_model_catalog.py")]
        with self.assertRaisesRegex(ConfigurationError, "不在受管工具清单"):
            claude_upgrade._job_tool_dependency_files(job, broken)


class WireClosureTests(unittest.TestCase):
    """wire 根闭包保持小：只到 claude_upgrade、claude_upgrade_plan、claude_upgrade_capture 与两个整文件 wire 模块。"""

    def test_wire_closure_module_set_is_locked(self) -> None:
        policy = claude_identity.load_policy()
        digests = claude_identity.managed_tree_digests(REPOSITORY_ROOT, policy)
        index = claude_identity._module_index(policy, digests)
        roots = claude_identity._orchestrator_roots(policy, index, policy["orchestrator"]["wire_roots"])
        wire = claude_identity.closure(policy, REPOSITORY_ROOT, digests, roots, layer="wire_producer")
        by_module: dict[str, set[str]] = collections.defaultdict(set)
        for item in wire["symbols"]:
            by_module[item["module"]].add(item["name"])
        package = "tools.official_client_capture."
        self.assertEqual(
            set(by_module),
            {package + "claude_upgrade", package + "claude_upgrade_plan", package + "claude_upgrade_capture"},
        )
        self.assertEqual(
            {item["module"] for item in wire["modules"]},
            {package + "capturelib.model", package + "capturelib.security"},
        )
        self.assertEqual(
            by_module[package + "claude_upgrade"],
            {"Job", "_campaign_arguments", "_job_context", "_load_plan_jobs", "_run_job_impl", "_scenario_job_execution_contract"},
        )
        capture_symbols = by_module[package + "claude_upgrade_capture"]
        self.assertTrue({"_run_job_impl", "_campaign_arguments", "_rehearsal_argv"} <= capture_symbols)
        # control 层的执行与收口不得进入 wire 闭包（改它们只是 control 变化，不要求重采）
        for name in (
            "ScenarioReceiptContext", "_issue_host_runtime_receipt", "_run_step_command", "_run_probe_command",
            "_probe_capture_environment", "_finalize_job_result", "_run_job_with_retry", "_close_job", "run_capture_attempt",
            "_job_incremental_metadata", "_relay_manifest_check",
        ):
            self.assertNotIn(name, capture_symbols)
        self.assertEqual(claude_identity.compute_identity(policy, REPOSITORY_ROOT)["defaulted_paths"], [])
        self.assertEqual(claude_identity.classify_path(policy, "official_client_capture/claude_upgrade_capture.py"), "control")


class ProbeTests(unittest.TestCase):
    """Claude 只读探针：一次 inspect 加一次容器内只读脚本，五类快照逐字比较，残留取证进程即漂移。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.hosts = ["127.0.0.1 localhost", "172.30.0.10 " + ("c" * 12)]
        self.processes: list[str] = []
        self.manifest = {
            "configuration": {
                "capture_container": "capture-cli", "ca_cert": "/opt/mitm/mitmproxy-ca-cert.pem", "ca_signing_pem": "/opt/mitm/mitmproxy-ca.pem",
                "claude_bin": f"/opt/claude-{TARGET}/claude", "credentials_file": "/root/.claude/.credentials.json",
            }
        }

    def fake_command(self, argv, *, operation, timeout_seconds=30, deadline=None, heartbeat=None, check=True):
        del operation, timeout_seconds, deadline, heartbeat, check
        if argv[:2] == ["docker", "inspect"]:
            item = {
                "Id": "c" * 64, "Image": "sha256:" + "d" * 64, "Config": {"Image": "capture"}, "RestartCount": 0,
                "State": {"Status": "running", "Running": True, "StartedAt": "2026-10-07T00:00:00Z"},
                "HostConfig": {"Privileged": True, "NetworkMode": "capture-network"},
                "NetworkSettings": {"Networks": {"capture-network": {"IPAddress": "172.30.0.10"}}},
                "Mounts": [{"Destination": "/capture", "Source": "/data", "RW": False, "Type": "bind"}],
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([item]), "")
        self.assertEqual(argv[:5], ["docker", "exec", "capture-cli", "python3", "-c"])
        arguments = json.loads(argv[6])
        self.assertEqual(arguments["state_dir"], "/root/.claude")
        observed = {
            "hosts_lines": self.hosts, "resolv_conf": {"exists": True, "sha256": "2" * 64},
            "ca_bundle": {"exists": True, "sha256": "3" * 64}, "mitm_ca_cert": {"exists": True, "sha256": "4" * 64},
            "mitm_ca_signing_pem": {"exists": True, "size": 10}, "claude_bin": {"exists": True, "size": 5},
            "global_state": {"exists": True, "mtime_ns": 1}, "credentials_file": {"exists": False},
            "state_dir": {"path": "/root/.claude", "entries": [], "truncated": False},
            "capture_processes": list(self.processes), "tcp_listen_ports": [],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(observed), "")

    def probe(self, name: str, phase: str) -> dict:
        output = self.root / name
        with mock.patch.object(capture, "_run_probe_command", self.fake_command):
            return claude_upgrade._probe_capture_environment(self.manifest, output, phase)

    def test_probe_writes_closed_snapshots(self) -> None:
        probe = self.probe("before", "before")
        self.assertEqual(probe["schema_version"], capture.PROBE_MANIFEST_SCHEMA)
        self.assertEqual([item["kind"] for item in probe["snapshots"]], list(capture.PROBE_KINDS))
        digests = claude_upgrade._probe_snapshot_digests(probe)
        for kind, filename in capture.PROBE_STATE_FILES.items():
            path = self.root / "before" / filename
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digests[kind])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        written = json.loads((self.root / "before" / capture.PROBE_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(written["snapshots"], probe["snapshots"])
        with self.assertRaisesRegex(ConfigurationError, "拒绝覆盖"):
            self.probe("before", "before")
        self.assertEqual(claude_upgrade.CONTINUITY_PROBE_KINDS, ("containers", "configuration", "client"))
        credentials = json.loads((self.root / "before" / "client-state.json").read_text(encoding="utf-8"))["credentials_file"]
        self.assertNotIn("sha256", credentials)

    def test_drift_rules(self) -> None:
        before = self.probe("a", "before")
        self.hosts = list(reversed(self.hosts)) + ["172.30.0.11 " + ("c" * 12)]
        self.assertEqual(capture._probe_drift(before, self.probe("b", "after")), [])
        self.hosts.append("127.0.0.1 api.anthropic.com")
        self.assertEqual(capture._probe_drift(before, self.probe("c", "after")), ["configuration"])
        self.hosts.pop()
        self.processes = ["relay"]
        self.assertEqual(capture._probe_drift(before, self.probe("d", "after")), ["processes"])


class BinaryVerificationTests(unittest.TestCase):
    """容器内二进制（复用 B1-10 版本探针）、宿主实体安装与运行镜像 RepoDigests 一起核对。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        binary = self.root / "opt" / f"claude-{TARGET}" / "claude"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"claude-binary")
        self.sha = hashlib.sha256(b"claude-binary").hexdigest()
        self.digests = [IMAGE]
        self.manifest = {
            "target_version": TARGET, "target_sha256": self.sha, "official_identity": {"binary_sha256": self.sha},
            "configuration": {"capture_container": "capture-cli", "claude_bin": f"/opt/claude-{TARGET}/claude", "runtime_image": IMAGE},
        }

    def fake_command(self, argv, *, operation, timeout_seconds=30, deadline=None, heartbeat=None, check=True):
        del operation, timeout_seconds, deadline, heartbeat, check
        if argv[:3] == ["docker", "inspect", "--format"]:
            return subprocess.CompletedProcess(argv, 0, "sha256:" + "d" * 64 + "\n", "")
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.digests), "")
        raise AssertionError(argv)

    def verify(self, container_sha: str) -> dict:
        observation = ({"path": f"/opt/claude-{TARGET}/claude", "sha256": container_sha, "version": TARGET}, {"isolated_empty_home": True})
        with mock.patch.object(capture, "_run_probe_command", self.fake_command), mock.patch.object(
            capture.claude_arm64, "_client_binary_observation", return_value=observation
        ), mock.patch.object(capture, "HOST_CLIENT_BINARY_TEMPLATE", str(self.root / "opt" / "claude-{version}" / "claude")):
            return claude_upgrade._verify_official_binaries(self.manifest)

    def test_success_and_rejections(self) -> None:
        result = self.verify(self.sha)
        self.assertEqual((result["host_binary"]["sha256"], result["runtime_image"]["reference"]), (self.sha, IMAGE))
        with self.assertRaisesRegex(ConfigurationError, "容器内目标客户端摘要"):
            self.verify("f" * 64)
        self.digests = ["other@sha256:" + "9" * 64]
        with self.assertRaisesRegex(ConfigurationError, "RepoDigests"):
            self.verify(self.sha)
        self.digests = [IMAGE]
        self.manifest["configuration"]["capture_container"] = "other"
        with self.assertRaisesRegex(ConfigurationError, "B5a-07"):
            self.verify(self.sha)


class RehearsalRunTests(CaptureFixture):
    """验收路径：零请求单 Job 演练走正式 run 同一条代码路径，relay 只以 dry-run 执行。"""

    def test_zero_request_single_job_rehearsal(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB, SMALL_LIVE_JOB}), mode="preflight_only")
        with self.patched():
            code, result, stderr = self.run_main(
                ["capture-official", "run", "--campaign-dir", str(campaign), "--zero-request-rehearsal", "--job-id", SMALL_LIVE_JOB]
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual((result["status"], result["mode"]), ("rehearsal_passed", "zero_request_rehearsal"))
            verdict = capture.verify_rehearsal_attempt(campaign, result["attempt_id"])
        self.assertEqual(verdict["verdict"], "passed", [item for item in verdict["checks"] if not item["passed"]])
        calls = self.relay_calls()
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call["dry_run"] and not call["execute"] and not call["acknowledgements"] for call in calls))
        self.assertFalse((self.data / "runs" / "claude-official").exists())
        self.assertFalse((campaign / "official").exists())
        attempt_root = campaign / "rehearsal" / "official" / "attempts" / result["attempt_id"]
        attempt = json.loads((attempt_root / "attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(attempt["schema_version"], capture.CAPTURE_ATTEMPT_SCHEMA)
        self.assertEqual(attempt["live_request_count_planned"], 0)
        job = attempt["results"][0]
        self.assertEqual([check["status"] for check in job["relay_checks"]], ["passed"] * 4)
        nonces = {step["host_runtime_receipt"]["run_nonce"] for step in job["steps"]}
        self.assertEqual(len(nonces), 4)
        closeout = json.loads((attempt_root / "evidence-permission-closeout.json").read_text(encoding="utf-8"))
        self.assertEqual(closeout["schema_version"], "codex-upgrade-evidence-permission-closeout/v2")
        self.assertTrue((attempt_root / "evidence" / "jobs" / SMALL_LIVE_JOB / "evidence-permission-closeout.json").is_file())
        records = incremental_recovery.CheckpointStore(attempt_root / "checkpoints", create=False).records()
        self.assertEqual([(record["item_id"], record["status"]) for record in records], [(SMALL_LIVE_JOB, "complete")])
        self.assertEqual(self.execution_tree_calls, [self.data])
        # 每一步都经 Campaign 租约的独立监督器执行：监督器事件账本里有逐步的命令事件。
        ledgers = list((campaign / ".supervisor").glob("run-*/events.ndjson"))
        self.assertEqual(len(ledgers), 1)
        ledger = ledgers[0].read_text(encoding="utf-8")
        for index in range(1, 5):
            self.assertIn(f"job:{SMALL_LIVE_JOB}:step-{index}:attempt-1", ledger)
        # 判定方法的反例：证据根一旦出现（有输出落盘）或收据被改，复核即判失败。
        (self.data / "runs" / "claude-official" / "claude-code-2_1_285-t1" / SMALL_LIVE_JOB).mkdir(parents=True)
        with self.patched():
            tampered = capture.verify_rehearsal_attempt(campaign, result["attempt_id"])
        self.assertEqual(tampered["verdict"], "failed")
        self.assertIn(f"job:{SMALL_LIVE_JOB}:evidence-roots-absent", [item["check"] for item in tampered["checks"] if not item["passed"]])

    def test_gates_refuse_before_any_write(self) -> None:
        payload = _reduced_payload({SMALL_SYNTHETIC_JOB})
        formal = self.write_campaign(payload)
        preflight = self.write_campaign(payload, mode="preflight_only", campaign_id="claude-code-2_1_285-t3")
        cases = {
            "Formal 上的演练": (["--campaign-dir", str(formal), "--zero-request-rehearsal", "--job-id", SMALL_SYNTHETIC_JOB], "preflight_only"),
            "演练缺 Job": (["--campaign-dir", str(preflight), "--zero-request-rehearsal"], "--job-id"),
            "演练与真实确认同时给": (["--campaign-dir", str(preflight), "--zero-request-rehearsal", "--acknowledge-live-requests"], "互斥"),
            "正式缺确认": (["--campaign-dir", str(formal)], "--acknowledge-live-requests"),
            "正式带 job-id": (["--campaign-dir", str(formal), "--acknowledge-live-requests", "--job-id", SMALL_SYNTHETIC_JOB], "只用于零请求演练"),
            "preflight 上的正式 run": (["--campaign-dir", str(preflight), "--acknowledge-live-requests"], "preflight_only"),
        }
        with self.patched():
            for label, (argv, message) in cases.items():
                with self.subTest(label=label):
                    code, _result, stderr = self.run_main(["capture-official", "run", *argv])
                    self.assertEqual(code, 1)
                    self.assertIn(message, stderr)
        for campaign in (formal, preflight):
            self.assertFalse((campaign / "official").exists())
            self.assertFalse((campaign / "rehearsal").exists())
        self.assertEqual(self.relay_calls(), [])

    def test_rehearsal_campaign_builder_freezes_plan(self) -> None:
        campaign = self.data / "evidence" / "campaigns" / "claude-code-2_1_285-r1"
        arguments = capture._build_parser().parse_args(
            ["rehearsal-campaign", "--campaign-dir", str(campaign), "--target-version", TARGET, "--target-sha256", TARGET_SHA,
             "--runtime-image", IMAGE, "--model", "claude-sonnet-5", "--upstream-ip", "160.79.104.10", "--campaign-id", "claude-code-2_1_285-r1"]
        )
        summary = capture.build_rehearsal_campaign(arguments)
        self.assertEqual((summary["campaign_mode"], len(summary["jobs"])), ("preflight_only", 12))
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.assertTrue(manifest["rehearsal_only"])
        self.assertEqual(manifest["configuration"]["capture_root"], "/capture")
        self.assertEqual(manifest["configuration"]["capture_tool_root"], "/root/oauth-capture/tools/official_client_capture")
        self.assertEqual(sum(job["steps"] for job in summary["jobs"]), 79)
        copied = campaign / manifest["inputs"]["target_scenarios"]["path"]
        self.assertEqual(copied.read_bytes(), plan.scenario_manifest_path(TARGET).read_bytes())
        with self.assertRaisesRegex(ConfigurationError, "不存在"):
            capture.build_rehearsal_campaign(arguments)
        with self.assertRaisesRegex(ConfigurationError, "evidence/campaigns"):
            arguments.campaign_dir = self.root / "elsewhere" / "c"
            capture.build_rehearsal_campaign(arguments)


class FormalRunTests(CaptureFixture):
    """正式 run：每 Job 闭环（探针、凭据、relay 清单、Job 级收口、checkpoint），run 末 v2 收口，补跑、归档与污染。"""

    def run_formal(self, campaign: Path) -> tuple[int, dict, str]:
        with self.patched():
            code, result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
        return code, result or {}, stderr

    def attempt(self, campaign: Path, attempt_id: str) -> tuple[Path, dict]:
        with self.patched():
            return claude_upgrade._load_capture_attempt(campaign, "official", None, attempt_id, _replay_evidence_permissions=False)

    def test_formal_run_closes_every_job_and_attempt(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB, SMALL_LIVE_JOB}))
        code, result, stderr = self.run_formal(campaign)
        self.assertEqual(code, 2, stderr)  # 直接 CLI 的 awaiting_receipts 记诊断码 2（与 Codex 同口径）
        self.assertEqual(result["status"], "awaiting_receipts")
        attempt_root, attempt = self.attempt(campaign, result["attempt_id"])
        self.assertEqual({item["id"]: item["status"] for item in attempt["results"]}, {SMALL_LIVE_JOB: "complete", SMALL_SYNTHETIC_JOB: "complete"})
        calls = self.relay_calls()
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call["execute"] and not call["dry_run"] for call in calls))
        layout = attempt["evidence_permission_layout"]
        permissions.replay_evidence_permission_closeout(
            attempt_root,
            [Path(item) for item in attempt["evidence_roots"]],
            attempt["evidence_permission_closeout"],
            managed_data_root=Path(layout["managed_data_root"]),
            logical_runs_roots=[Path(item) for item in layout["logical_runs_roots"]],
        )
        for job in attempt["results"]:
            closure = json.loads((attempt_root / job["job_closure"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(closure["schema_version"], capture.JOB_CLOSURE_SCHEMA)
            self.assertEqual(closure["evidence_roots"], [f"{self.data}/runs/claude-official/claude-code-2_1_285-t1/{job['id']}"])
            self.assertTrue(all(not run["drifted_kinds"] for run in closure["environment_runs"]))
            for check, step in zip(job["relay_checks"], job["steps"], strict=True):
                self.assertEqual(check["status"], "passed")
                self.assertTrue((attempt_root / step["host_runtime_receipt"]["path"]).is_file())
        records = incremental_recovery.CheckpointStore(attempt_root / "checkpoints", create=False).records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["checkpoint_schema_version"], capture.JOB_CHECKPOINT_SCHEMA)
        self.assertTrue((attempt_root / "evidence" / "environment" / "after" / capture.PROBE_MANIFEST_NAME).is_file())
        with self.patched():
            self.assertEqual(claude_upgrade._active_unsealed_attempts(campaign, "official"), [f"official:{result['attempt_id']}"])
            self.assertEqual(len(claude_upgrade._ordered_capture_attempts(campaign, "official", None)), 1)
            self.assertEqual([item[2].name for item in claude_upgrade._campaign_attempt_roots(campaign)], [result["attempt_id"]])
            # 默认重放 run 末权限收口（B2-07 落实 _replay_attempt_evidence_permissions），与只读核对状态得到同一份收据。
            replayed_root, replayed = claude_upgrade._load_capture_attempt(campaign, "official", None, result["attempt_id"])
            self.assertEqual((replayed_root, replayed["attempt_digest"]), (attempt_root, attempt["attempt_digest"]))
        code, _again, stderr = self.run_formal(campaign)
        self.assertEqual(code, 1)
        self.assertIn("未封存", stderr)

    def test_required_job_retries_then_archives(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB}))
        self.set_control({"fail": {"v4-oauth-refresh": 99}})
        code, result, stderr = self.run_formal(campaign)
        self.assertEqual((code, result["status"]), (2, "failed"), stderr)
        attempt_root, attempt = self.attempt(campaign, result["attempt_id"])
        job = attempt["results"][0]
        root = f"{self.data}/runs/claude-official/claude-code-2_1_285-t1/{SMALL_SYNTHETIC_JOB}"
        self.assertEqual(job["attempt_index"], 3)
        self.assertEqual(job["evidence_roots"], [root + ".failed-attempt3"])
        self.assertEqual(job["archived_evidence_roots"], [root + ".failed-attempt1", root + ".failed-attempt2"])
        self.assertFalse(Path(root).exists())
        self.assertEqual(len(self.relay_calls()), 3)
        causes = attempt["root_causes"]
        self.assertTrue(causes and all(cause["stable_dimensions"] == {"client": "claude", "phase": "official"} for cause in causes))
        closure = json.loads((attempt_root / job["job_closure"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(closure["host_evidence_roots"]), 3)
        code, _again, stderr = self.run_formal(campaign)
        self.assertEqual(code, 1)
        self.assertIn("失败 attempt", stderr)

    def test_transient_failure_recovers_in_same_attempt(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB}))
        self.set_control({"fail": {"v4-oauth-refresh": 1}})
        code, result, stderr = self.run_formal(campaign)
        self.assertEqual(result["status"], "awaiting_receipts", stderr)
        _root, attempt = self.attempt(campaign, result["attempt_id"])
        job = attempt["results"][0]
        root = f"{self.data}/runs/claude-official/claude-code-2_1_285-t1/{SMALL_SYNTHETIC_JOB}"
        self.assertEqual((job["status"], job["attempt_index"], job["evidence_roots"]), ("complete", 2, [root]))
        self.assertEqual(job["archived_evidence_roots"], [root + ".failed-attempt1"])
        self.assertEqual(attempt["failure_observations"][0]["check_id"], f"job.{SMALL_SYNTHETIC_JOB}.step-1")

    def test_environment_drift_stops_without_retry(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB, SMALL_LIVE_JOB}))
        first_probe = "v4-native-tls-baseline"
        self.set_control({"contaminate": first_probe})
        code, result, stderr = self.run_formal(campaign)
        self.assertEqual((code, result["status"]), (2, "environment_contaminated"), stderr)
        _root, attempt = self.attempt(campaign, result["attempt_id"])
        self.assertEqual([item["id"] for item in attempt["results"]], [SMALL_LIVE_JOB])
        job = attempt["results"][0]
        self.assertTrue(job["environment_contaminated"])
        self.assertEqual(job["attempt_index"], 1)
        self.assertIn({"check_id": f"job.{SMALL_LIVE_JOB}.environment.configuration", "failure_code": "drifted"},
                      [{key: item[key] for key in ("check_id", "failure_code")} for item in attempt["failure_observations"]])
        self.assertEqual(attempt["environment"]["drifted_kinds"], ["configuration"])
        self.assertTrue((campaign / capture.CONTAMINATION_MARKER).is_file())
        self.assertNotIn("v4-oauth-refresh", {call["probe"] for call in self.relay_calls()})
        code, _again, stderr = self.run_formal(campaign)
        self.assertEqual(code, 1)
        self.assertIn("B2-08", stderr)


class DetachedRetryTests(CaptureFixture):
    """没有 attempt 上下文（Job 演练的合成作业）：只执行与归档，补跑上限与退避从编排器常量读取。"""

    def test_detached_job_retry_lifecycle(self) -> None:
        runs = self.data / "runs"
        script = self.root / "fail.py"
        script.write_text(
            "import pathlib, sys\nroot = pathlib.Path(sys.argv[1])\nroot.mkdir()\n(root / 'marker').write_text('x')\nsys.exit(3)\n",
            encoding="utf-8",
        )
        source = runs / "lifecycle-source"
        job = claude_upgrade.Job(
            job_id="failure-lifecycle", phase="official", suites=("full",), description="失败生命周期",
            steps=({"argv": ["python3", str(script), str(source)], "environment": {}, "timeout": 30},),
            evidence_roots=(str(source),), covers=(),
        )
        log_root = self.root / "logs"
        log_root.mkdir()
        with self.patched():
            result = claude_upgrade._run_job_with_retry(job, log_root, tool_identity=self.tool_identity)
        self.assertEqual((result["status"], result["attempt_index"]), ("failed", 3))
        self.assertEqual(result["evidence_roots"], [str(source) + ".failed-attempt3"])
        self.assertEqual(sorted(path.name for path in runs.iterdir()), [f"lifecycle-source.failed-attempt{n}" for n in (1, 2, 3)])
        self.assertEqual(result["environment_runs"], [])
        self.assertFalse(result["environment_contaminated"])


class AttemptFactsTests(unittest.TestCase):
    def test_failure_facts_use_claude_codes_with_client_dimension(self) -> None:
        payload = {
            "phase": "official",
            "failure_observations": [{"check_id": "attempt.environment.processes", "failure_code": "residual"}],
            "results": [{"id": "job-a", "phase": "official", "status": "failed", "failure_observations": []}],
        }
        observations, causes = claude_upgrade._attempt_failure_facts(payload)
        self.assertEqual({item["check_id"] for item in observations}, {"attempt.environment.processes", "job.job-a"})
        self.assertTrue(all(cause["stable_dimensions"] == {"client": "claude", "phase": "official"} for cause in causes))

    def test_deadline_bounds_and_monitor_wait(self) -> None:
        deadline = claude_upgrade._attempt_deadline(type("A", (), {"max_wall_seconds": 60, "heartbeat_seconds": 5})(), "official")
        self.assertEqual((deadline.phase, deadline.heartbeat_seconds), ("official", 5))
        with self.assertRaisesRegex(ConfigurationError, "max-wall-seconds"):
            claude_upgrade._attempt_deadline(type("A", (), {"max_wall_seconds": 10**9, "heartbeat_seconds": None})(), "official")
        self.assertEqual(claude_upgrade._wait_for_monitor_finalization(Path("/nonexistent"), {"monitor_pid": None, "state": "running"}),
                         {"monitor_pid": None, "state": "running"})


if __name__ == "__main__":
    unittest.main()
