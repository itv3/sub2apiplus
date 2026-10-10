"""Claude 官方取证 Job 动作体与环境闭环（Claude 升级控制面设计 B2-06）。

不起 Docker、不联网、不读凭据：
* relay 用夹具替身（``claude_fw_e_relay.py`` 同名脚本，dry-run 回显计划、execute 写 relay 清单），步骤仍经真实
  Campaign 租约的独立监督器 ``SupervisorClient.run_command`` 执行；
* 步骤保留场景清单里的 ``docker exec <容器>`` 前缀（B2-05 要求真实上游步骤进 ``capture-cli``、合成步骤进断网容器
  ``capture-cli-synthetic``），PATH 最前面放一个 ``docker`` 替身：记下目标容器，再在本机执行容器内命令；
* 宿主命令边界（宿主运行凭据签发、容器内可见核对、两个容器里的必需文件与全局状态）由 ``FakeHost`` 替身应答；
* 环境探针的采集函数换成读一份可被替身 relay 改写的环境文件，用来制造「执行后漂移」与「残留取证进程」。

场景清单取 B2-05 的真实 2.1.285 清单，裁剪到少数 Job；测试里的「容器坐标」与宿主坐标相同（``capture_root`` 指向临时
数据根）。
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import io
import json
import os
import shutil
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
from tools.official_client_capture.tests import claude_project_ledger_fixture

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
TARGET = "2.1.285"
BASELINE = "2.1.226"
TARGET_SHA = "a" * 64
IMAGE = "oauth-egress-capture-capture-cli@sha256:" + "b" * 64
SMALL_SYNTHETIC_JOB = "official-synthetic-oauth-refresh"  # 1 个合成 probe（断网容器）
SMALL_LIVE_JOB = "official-transport-baseline"  # 4 个真实上游 probe（零请求演练改写成 dry-run）
CAPTURE = "capture-cli"
SYNTHETIC = "capture-cli-synthetic"
CONTAINER_IDS = {CAPTURE: "c" * 64, SYNTHETIC: "9" * 64}
ROLE_OF = {CAPTURE: "capture", SYNTHETIC: "synthetic"}
GLOBAL_STATE = "/root/.claude.json"

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""docker 的离线替身：只接受 ``docker exec <容器> <命令…>``，记下目标容器后在本机执行命令（测试里容器坐标即宿主坐标）。"""
import json, os, sys
args = sys.argv[1:]
if len(args) < 3 or args[0] != "exec" or args[1].startswith("-"):
    print("fake docker 只接受 exec <容器> <命令>", file=sys.stderr)
    sys.exit(125)
container, command = args[1], args[2:]
with open(os.environ["FAKE_DOCKER_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"container": container, "command": os.path.basename(command[0])}) + "\n")
os.environ["FAKE_DOCKER_CONTAINER"] = container
os.execvp(command[0], command)
'''

FAKE_RELAY = r'''#!/usr/bin/env python3
"""claude_fw_e_relay.py 的离线替身：dry-run 回显计划；execute 核对宿主凭据摘要后写 relay 清单与一份非空产物。"""
import hashlib, json, os, sys
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
control = json.load(open(os.environ["FAKE_RELAY_CONTROL"], encoding="utf-8"))
probe = value("--probe-id")
dry = "--dry-run" in args
container = os.environ.get("FAKE_DOCKER_CONTAINER")
role = {"capture-cli": "capture", "capture-cli-synthetic": "synthetic"}.get(container)
with open(os.environ["FAKE_RELAY_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"probe": probe, "dry_run": dry, "execute": "--execute" in args, "container": container,
                             "acknowledgements": [item for item in args if item.startswith("--acknowledge")]}) + "\n")
output = value("--output-root")
if dry:
    if os.path.exists(output):
        print("output exists", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"schema_version": "claude-code-fw-e-relay-plan/v1", "run_id": value("--run-id"), "probe_id": probe,
                      "model": value("--model"),
                      "version": value("--expected-version"), "live_requests": value("--upstream-ip") is not None,
                      "upstream_ip": value("--upstream-ip"), "production_changes": False}, indent=2, sort_keys=True))
    sys.exit(0)
receipt = value("--host-runtime-receipt")
digest = hashlib.sha256(open(receipt, "rb").read()).hexdigest()
if digest != value("--host-runtime-receipt-sha256"):
    print("receipt mismatch", file=sys.stderr)
    sys.exit(2)
# 与真实 relay 同一要求：凭据绑定的容器必须是当前所在容器（真实 relay 用 hostname 与 mountinfo 复核）。
if json.load(open(receipt, encoding="utf-8"))["container"]["name"] != container:
    print("receipt container mismatch", file=sys.stderr)
    sys.exit(2)
counts_path = os.environ["FAKE_RELAY_CONTROL"] + ".counts"
counts = json.load(open(counts_path, encoding="utf-8")) if os.path.exists(counts_path) else {}
counts[probe] = counts.get(probe, 0) + 1
json.dump(counts, open(counts_path, "w", encoding="utf-8"))
os.makedirs(output, mode=0o700)
with open(os.path.join(output, "relay-raw.bin"), "wb") as handle:
    handle.write(b"POST /v1/messages HTTP/1.1\r\n\r\n")
if probe in (control.get("contaminate"), control.get("residual")):
    env_path = os.environ["FAKE_RELAY_ENV"]
    environment = json.load(open(env_path, encoding="utf-8"))
    if control.get("contaminate") == probe:
        environment["configuration"]["targets"][role]["hosts_sha256"] = "f" * 64
    else:
        environment["processes"]["capture_processes"].append(role + ":relay")
    json.dump(environment, open(env_path, "w", encoding="utf-8"))
status = "failed" if counts[probe] <= int(control.get("fail", {}).get(probe, 0)) else "complete"
# 与真实 relay 同一形态：probe_id 记中继跑的基础 probe（--probe-id），model 记 --model（非主模型步骤靠它定步骤身份）。
manifest = {"schema_version": "claude-code-fw-e-relay-run/v1", "probe_id": probe, "model": value("--model"), "status": status,
            "m_binding": {"complete": status == "complete"}, "secret_scan": {"passed": True},
            "cleanup": {"relay_stopped": True, "hosts_restored": True},
            "client": {"version": value("--expected-version") + " (Claude Code)", "sha256": value("--expected-sha256")},
            "runtime": {"host_runtime_receipt": {"sha256": digest, "run_nonce": value("--run-nonce")}}}
with open(os.path.join(output, "relay-manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle)
sys.exit(0 if status == "complete" else 2)
'''

BASE_ENVIRONMENT = {
    "containers": {
        "probe_kind": "containers",
        "containers": [
            {"role": "capture", "name": CAPTURE, "id": CONTAINER_IDS[CAPTURE], "started_at": "t0", "network_mode": "capture-network"},
            {"role": "synthetic", "name": SYNTHETIC, "id": CONTAINER_IDS[SYNTHETIC], "started_at": "t0", "network_mode": "none"},
        ],
    },
    "configuration": {"probe_kind": "configuration", "targets": {"capture": {"hosts_sha256": "1" * 64}, "synthetic": {"hosts_sha256": "2" * 64}}},
    "client": {"probe_kind": "client", "targets": {role: {"global_state": {"exists": True, "mtime_ns": 1}} for role in ("capture", "synthetic")}},
    "processes": {"probe_kind": "processes", "capture_processes": []},
    "listeners": {"probe_kind": "listeners", "targets": {"capture": [{"family": "tcp", "port": 22}], "synthetic": []}},
}


def _baseline_rules() -> tuple[str, ...]:
    return plan.load_baseline_rules(plan.baseline_rule_manifest_path(BASELINE), BASELINE)


def _reduced_payload(job_ids: set[str]) -> dict:
    """真实 2.1.285 场景清单裁剪到指定 Job（场景只保留被执行的 probe），步骤原样保留 ``docker exec <容器>`` 前缀。"""

    payload = json.loads(plan.scenario_manifest_path(TARGET).read_text(encoding="utf-8"))
    jobs = [job for job in payload["capture_jobs"] if job["id"] in job_ids]
    executed = {step["probe_id"] for job in jobs for step in job["steps"]}
    scenarios = []
    for scenario in payload["evidence_scenarios"]:
        probes = [probe for probe in scenario["probe_ids"] if probe in executed]
        if probes:
            scenarios.append({**scenario, "probe_ids": probes})
    payload["capture_jobs"] = jobs
    payload["evidence_scenarios"] = scenarios
    # probe_catalog.probe_count 只数主模型步骤（非主模型的模型能力取证步骤复用基础 probe，不计入目录数）。
    primary = [step for job in jobs for step in job["steps"] if plan.step_model(step["probe_id"], step["argv"]) is None]
    payload["probe_catalog"] = {**payload["probe_catalog"], "probe_count": len(primary)}
    return payload


class FakeHost:
    """宿主命令边界替身：签发宿主运行凭据、容器内 sha256sum、两个容器里的必需文件与全局状态（容器坐标即宿主坐标）。

    ``missing`` 列出缺失的 ``(容器, 路径)``；``global_state_digests`` 是两个容器里全局状态文件的摘要（缺省相同）。
    """

    def __init__(self, *, files_present: bool = True, missing: frozenset = frozenset(), global_state_digests: dict | None = None) -> None:
        self.files_present = files_present
        self.missing = set(missing)
        self.global_state_digests = dict(global_state_digests or {CAPTURE: "5" * 64, SYNTHETIC: "5" * 64})
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
                        "container": {"name": value("--container"), "id": CONTAINER_IDS[value("--container")], "hostname": value("--container")},
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
            assert argv[2] in CONTAINER_IDS, argv
            code = 0 if self.files_present and (argv[2], argv[5]) not in self.missing else 1
            if check and code:
                raise ConfigurationError("missing")
            return subprocess.CompletedProcess(argv, code, "", "")
        if argv[:2] == ["docker", "exec"] and argv[3] == "/usr/bin/sha256sum":
            assert argv[2] in CONTAINER_IDS, argv
            if argv[4] == GLOBAL_STATE:
                return subprocess.CompletedProcess(argv, 0, f"{self.global_state_digests[argv[2]]}  {argv[4]}\n", "")
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
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        self.root, restoring = stops.fixture_directory(self, 'capture')
        if restoring:
            if not stops.restore_case(self, 'capture-case'):
                raise ValueError('原取证夹具缺少值字段停点')
            return
        self.data = self.root / "data"
        (self.data / "runs").mkdir(parents=True, mode=0o700)
        (self.data / "evidence" / "campaigns").mkdir(parents=True, mode=0o700)
        self.tool_copy = self.data / "tools" / "official_client_capture"
        self.tool_copy.mkdir(parents=True, mode=0o700)
        relay = self.tool_copy / "claude_fw_e_relay.py"
        relay.write_text(FAKE_RELAY, encoding="utf-8")
        relay.chmod(0o700)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir(mode=0o700)
        docker = self.fake_bin / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o700)
        self.control_file = self.root / "relay-control.json"
        self.env_file = self.root / "environment.json"
        self.calls_file = self.root / "relay-calls.jsonl"
        self.docker_calls_file = self.root / "docker-calls.jsonl"
        self.set_control({})
        self.env_file.write_text(json.dumps(BASE_ENVIRONMENT), encoding="utf-8")
        self.host = FakeHost()
        self.execution_tree_calls: list[Path | None] = []
        stops.save_case(self, 'capture-case')

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
            "capture_container": CAPTURE,
            "synthetic_capture_container": SYNTHETIC,
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
        if mode == "formal":
            # B2-08 起正式 run 与 seal 先过项目总账消费者门禁：Formal Campaign 在数据根的 Claude 总账里注册。
            claude_project_ledger_fixture.register_claude_campaign(campaign)
        return campaign

    def fake_states(self, manifest, *, deadline=None, heartbeat=None):
        del manifest, deadline, heartbeat
        return json.loads(self.env_file.read_text(encoding="utf-8"))

    def fake_binaries(self, manifest, *, deadline=None, heartbeat=None):
        del deadline, heartbeat
        return {
            "schema_version": capture.BINARY_VERIFICATION_SCHEMA,
            "target_sha256": manifest["target_sha256"],
            "synthetic_container": {"name": SYNTHETIC, "network_mode": "none", "client_binary": {"sha256": manifest["target_sha256"]}},
            "fake": True,
        }

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
                        "FAKE_DOCKER_CALLS": str(self.docker_calls_file),
                        "PATH": str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    },
                )
            )
            yield

    def run_main(self, argv: list[str]) -> tuple[int, dict | None, str]:
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        pending = stops.begin_command(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = None
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = claude_upgrade.main(argv)
        finally:
            stops.record_command(argv, code, stdout.getvalue(), stderr.getvalue(), pending=pending)
        text = stdout.getvalue()
        return code, (json.loads(text) if text.strip() else None), stderr.getvalue()

    def relay_calls(self) -> list[dict]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()]

    def docker_calls(self) -> list[dict]:
        if not self.docker_calls_file.exists():
            return []
        return [json.loads(line) for line in self.docker_calls_file.read_text(encoding="utf-8").splitlines()]

    def supervisor_ledger(self, campaign: Path) -> str:
        ledgers = list((campaign / ".supervisor").glob("run-*/events.ndjson"))
        self.assertEqual(len(ledgers), 1)
        return ledgers[0].read_text(encoding="utf-8")


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
        # 合成步骤展开到断网容器，真实上游步骤展开到抓包容器（B2-05 plan 校验同一规则）。
        containers = {job.job_id: {tuple(step["argv"][:3]) for step in job.steps} for job in jobs}
        self.assertEqual(containers[SMALL_SYNTHETIC_JOB], {("docker", "exec", SYNTHETIC)})
        self.assertEqual(containers[SMALL_LIVE_JOB], {("docker", "exec", CAPTURE)})

    def test_frozen_rows_and_bindings_fail_closed(self) -> None:
        payload = _reduced_payload({SMALL_SYNTHETIC_JOB})
        rows = [{"id": SMALL_SYNTHETIC_JOB, "phase": "official", "execution_sha256": "0" * 64}]
        campaign = self.write_campaign(payload, jobs=rows)
        with self.assertRaisesRegex(ConfigurationError, "执行摘要漂移"):
            claude_upgrade._campaign_jobs(campaign, claude_upgrade.load_campaign_manifest(campaign), "official")
        other = self.write_campaign(payload, campaign_id="claude-code-2_1_285-t2", configuration={"capture_tool_root": ""})
        with self.assertRaisesRegex(ConfigurationError, "capture_tool_root"):
            claude_upgrade._campaign_jobs(other, claude_upgrade.load_campaign_manifest(other), "official")
        # 早于断网容器的总计划（configuration 没有 synthetic_capture_container）不能再展开，也不在运行期补默认值。
        legacy = self.write_campaign(payload, campaign_id="claude-code-2_1_285-t4", configuration={"synthetic_capture_container": ""})
        with self.assertRaisesRegex(ConfigurationError, "synthetic_capture_container"):
            claude_upgrade._campaign_jobs(legacy, claude_upgrade.load_campaign_manifest(legacy), "official")
        same = self.write_campaign(payload, campaign_id="claude-code-2_1_285-t5", configuration={"synthetic_capture_container": CAPTURE})
        with self.assertRaisesRegex(ConfigurationError, "独立的断网容器"):
            claude_upgrade._campaign_jobs(same, claude_upgrade.load_campaign_manifest(same), "official")
        # 候选阶段（B4-01b）：没有冻结候选变量的总计划不能展开候选任务；批准场景的展开不在 B4-01 范围内。
        with self.assertRaisesRegex(ConfigurationError, "没有冻结候选变量"):
            claude_upgrade._campaign_jobs(campaign, claude_upgrade.load_campaign_manifest(campaign), "candidate", candidate_id="c1")
        with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, "B4-04"):
            claude_upgrade._campaign_jobs(campaign, claude_upgrade.load_campaign_manifest(campaign), "official", use_approved_scenario=True)

    def test_campaign_arguments_reads_only_the_given_manifest(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB}))
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with mock.patch.object(claude_upgrade, "load_campaign_manifest", side_effect=AssertionError("wire 根不得读总计划")):
            arguments = claude_upgrade._campaign_arguments(campaign, manifest)
            context = claude_upgrade._job_context(arguments)
        # 候选阶段变量（B4-01）也在上下文里：这份总计划没有它们，取值是空字符串（official 展开不受影响）。
        self.assertEqual(set(context), set(plan.CAMPAIGN_VARIABLES) | set(plan.CANDIDATE_CAMPAIGN_VARIABLES))
        self.assertEqual({context[name] for name in plan.CANDIDATE_CAMPAIGN_VARIABLES}, {""})
        self.assertEqual(context["capture_tool_root"], str(self.tool_copy))
        with self.assertRaisesRegex(ConfigurationError, "越过|非法"):
            capture._campaign_relative_file(campaign, "../escape.json")

    def test_tool_dependencies_map_policy_ignored_files(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_LIVE_JOB}))
        job = claude_upgrade._campaign_jobs(campaign, claude_upgrade.load_campaign_manifest(campaign), "official")[0]
        files = claude_upgrade._job_tool_dependency_files(job, self.tool_identity)
        # 38 个原有依赖加 FW-F v5 目录模块与 2.1.285 版本事实（步骤 argv 引用，作业依赖分析器同样识别）。
        self.assertEqual(len(files), 40)
        self.assertIn("official_client_capture/drive_codex_model_catalog.py", files)
        self.assertIn("official_client_capture/claude_fw_e_relay.py", files)
        self.assertIn("official_client_capture/capturelib/claude_fw_f_v5.py", files)
        self.assertIn("official_client_capture/capturelib/claude_fw_f_v5_version_facts_2_1_285.json", files)
        policy = claude_identity.load_policy()
        self.assertIsNone(claude_identity.classify_path(policy, "official_client_capture/drive_codex_model_catalog.py"))
        broken = dict(self.tool_identity)
        broken["entries"] = [entry for entry in self.tool_identity["entries"] if not entry["path"].endswith("drive_codex_model_catalog.py")]
        with self.assertRaisesRegex(ConfigurationError, "不在受管工具清单"):
            claude_upgrade._job_tool_dependency_files(job, broken)


class WireClosureTests(unittest.TestCase):
    """wire 根闭包保持小：只到 claude_upgrade、claude_upgrade_plan、claude_upgrade_capture 与三个整文件 wire 模块。

    FW-F v5 起多一个整文件模块 ``capturelib.claude_fw_f_v5``：plan 校验场景清单时用它核版本事实与 probe 集合（与中继
    同一份实现），它在 Claude 策略里属 wire 层，整文件计入；它 import 的 v4 不再展开（闭包对整文件模块不追 import）。
    """

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
            {package + "capturelib.model", package + "capturelib.security", package + "capturelib.claude_fw_f_v5"},
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
    """Claude 只读探针：两个采集容器各一次 inspect 加一次容器内只读脚本，五类快照按角色记录、逐字比较，任一容器残留
    取证进程即漂移。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.hosts = {CAPTURE: ["127.0.0.1 localhost", "172.30.0.10 " + ("c" * 12)], SYNTHETIC: ["127.0.0.1 localhost"]}
        self.processes: dict[str, list[str]] = {CAPTURE: [], SYNTHETIC: []}
        self.network = {SYNTHETIC: ("none", {"none": {"IPAddress": ""}})}
        self.probed: list[tuple[str, str]] = []
        self.manifest = {
            "configuration": {
                "capture_container": CAPTURE, "synthetic_capture_container": SYNTHETIC,
                "ca_cert": "/opt/mitm/mitmproxy-ca-cert.pem", "ca_signing_pem": "/opt/mitm/mitmproxy-ca.pem",
                "claude_bin": f"/opt/claude-{TARGET}/claude", "credentials_file": "/root/.claude/.credentials.json",
            }
        }

    def fake_command(self, argv, *, operation, timeout_seconds=30, deadline=None, heartbeat=None, check=True):
        del operation, timeout_seconds, deadline, heartbeat, check
        if argv[:2] == ["docker", "inspect"]:
            name = argv[2]
            mode, networks = self.network.get(name, ("capture-network", {"capture-network": {"IPAddress": "172.30.0.10"}}))
            item = {
                "Id": CONTAINER_IDS[name], "Image": "sha256:" + "d" * 64, "Config": {"Image": "capture"}, "RestartCount": 0,
                "State": {"Status": "running", "Running": True, "StartedAt": "2026-10-07T00:00:00Z"},
                "HostConfig": {"Privileged": name == CAPTURE, "NetworkMode": mode},
                "NetworkSettings": {"Networks": networks},
                "Mounts": [{"Destination": "/capture", "Source": "/data", "RW": False, "Type": "bind"}],
            }
            self.probed.append(("inspect", name))
            return subprocess.CompletedProcess(argv, 0, json.dumps([item]), "")
        self.assertEqual((argv[:2], argv[3:5]), (["docker", "exec"], ["python3", "-c"]))
        name = argv[2]
        self.probed.append(("exec", name))
        arguments = json.loads(argv[6])
        self.assertEqual(arguments["state_dir"], "/root/.claude")
        observed = {
            "hosts_lines": self.hosts[name], "resolv_conf": {"exists": True, "sha256": "2" * 64},
            "ca_bundle": {"exists": True, "sha256": "3" * 64}, "mitm_ca_cert": {"exists": True, "sha256": "4" * 64},
            "mitm_ca_signing_pem": {"exists": True, "size": 10}, "claude_bin": {"exists": True, "size": 5},
            "global_state": {"exists": True, "mtime_ns": 1}, "credentials_file": {"exists": False},
            "state_dir": {"path": "/root/.claude", "entries": [], "truncated": False},
            "capture_processes": list(self.processes[name]), "tcp_listen_ports": [],
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
        clients = json.loads((self.root / "before" / "client-state.json").read_text(encoding="utf-8"))["targets"]
        self.assertEqual(set(clients), {"capture", "synthetic"})
        self.assertTrue(all("sha256" not in item["credentials_file"] for item in clients.values()))
        # 两个容器都探：各一次 inspect 加一次只读脚本，快照按角色记录。
        self.assertEqual(self.probed, [("inspect", CAPTURE), ("exec", CAPTURE), ("inspect", SYNTHETIC), ("exec", SYNTHETIC)])
        self.assertEqual(probe["targets"], {"capture": CAPTURE, "synthetic": SYNTHETIC})
        containers = json.loads((self.root / "before" / "containers-state.json").read_text(encoding="utf-8"))["containers"]
        self.assertEqual([(item["role"], item["name"], item["network_mode"]) for item in containers],
                         [("capture", CAPTURE, "capture-network"), ("synthetic", SYNTHETIC, "none")])

    def test_drift_rules(self) -> None:
        before = self.probe("a", "before")
        self.hosts[CAPTURE] = list(reversed(self.hosts[CAPTURE])) + ["172.30.0.11 " + ("c" * 12)]
        self.assertEqual(capture._probe_drift(before, self.probe("b", "after")), [])
        self.hosts[CAPTURE].append("127.0.0.1 api.anthropic.com")
        self.assertEqual(capture._probe_drift(before, self.probe("c", "after")), ["configuration"])
        self.hosts[CAPTURE].pop()
        self.processes[CAPTURE] = ["relay"]
        drifted = self.probe("d", "after")
        self.assertEqual(capture._probe_drift(before, drifted), ["processes"])
        self.assertEqual(drifted["capture_processes"], ["capture:relay"])

    def test_synthetic_container_drift_is_detected(self) -> None:
        """断网容器同样逐字比较：hosts 残留、残留取证进程、被接入网络，都判漂移。"""

        before = self.probe("a", "before")
        self.hosts[SYNTHETIC].append("127.0.0.1 platform.claude.com")
        self.assertEqual(capture._probe_drift(before, self.probe("b", "after")), ["configuration"])
        self.hosts[SYNTHETIC].pop()
        self.processes[SYNTHETIC] = ["claude", "relay"]
        residual = self.probe("c", "after")
        self.assertEqual(capture._probe_drift(before, residual), ["processes"])
        self.assertEqual(residual["capture_processes"], ["synthetic:claude", "synthetic:relay"])
        self.processes[SYNTHETIC] = []
        self.network[SYNTHETIC] = ("capture-network", {"capture-network": {"IPAddress": "172.30.0.11"}})
        self.assertEqual(capture._probe_drift(before, self.probe("d", "after")), ["containers"])

    def test_both_containers_are_required(self) -> None:
        for override in ({"synthetic_capture_container": ""}, {"synthetic_capture_container": CAPTURE}):
            with self.subTest(override=override):
                manifest = {"configuration": {**self.manifest["configuration"], **override}}
                with mock.patch.object(capture, "_run_probe_command", self.fake_command), self.assertRaises(ConfigurationError):
                    claude_upgrade._probe_capture_environment(
                        manifest, self.root / f"x-{override['synthetic_capture_container'] or 'empty'}", "before"
                    )


class BinaryVerificationTests(unittest.TestCase):
    """抓包容器与断网容器里的二进制（复用 B1-10 版本探针与环境收据版本 2 的断网容器核验）、宿主实体安装与运行镜像
    RepoDigests 一起核对。"""

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
            "configuration": {
                "capture_container": CAPTURE, "synthetic_capture_container": SYNTHETIC,
                "claude_bin": f"/opt/claude-{TARGET}/claude", "runtime_image": IMAGE,
            },
        }
        self.synthetic_error: Exception | None = None
        self.synthetic_overrides: dict = {}

    def synthetic_observation(self, version: str) -> dict:
        """环境收据版本 2 的断网容器核验结果（替身）；``synthetic_error`` 模拟它拒绝（联网、换镜像、挂载不一致等）。"""

        self.assertEqual(version, TARGET)
        if self.synthetic_error is not None:
            raise self.synthetic_error
        value = {
            "name": SYNTHETIC, "container_id": CONTAINER_IDS[SYNTHETIC], "image_id": "sha256:" + "d" * 64, "network_mode": "none",
            "network_names": ["none"], "reference_container": CAPTURE, "reference_container_id": CONTAINER_IDS[CAPTURE],
            "shared_mounts_sha256": "6" * 64, "reference_shared_mounts_sha256": "6" * 64, "shared_mount_count": 6,
            "excluded_reference_mounts": ["/usr/bin/docker", "/var/run/docker.sock"],
            "client_binary": {"path": f"/opt/claude-{TARGET}/claude", "sha256": self.sha, "version": TARGET},
            "client_binary_probe": {"container": SYNTHETIC, "isolated_empty_home": True},
            "raw_sha256": {"docker_inspect": "7" * 64, "reference_docker_inspect": "8" * 64},
        }
        value.update(self.synthetic_overrides)
        return value

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
        ), mock.patch.object(
            capture.claude_arm64, "_synthetic_container_observation", side_effect=self.synthetic_observation
        ), mock.patch.object(capture, "HOST_CLIENT_BINARY_TEMPLATE", str(self.root / "opt" / "claude-{version}" / "claude")):
            return claude_upgrade._verify_official_binaries(self.manifest)

    def test_success_and_rejections(self) -> None:
        result = self.verify(self.sha)
        self.assertEqual(result["schema_version"], "claude-upgrade-official-binary-verification/v2")
        self.assertEqual((result["host_binary"]["sha256"], result["runtime_image"]["reference"]), (self.sha, IMAGE))
        self.assertEqual(
            (result["synthetic_container"]["name"], result["synthetic_container"]["network_mode"], result["synthetic_container"]["client_binary"]["sha256"]),
            (SYNTHETIC, "none", self.sha),
        )
        self.assertNotIn("raw_sha256", result["synthetic_container"])
        with self.assertRaisesRegex(ConfigurationError, "容器内目标客户端摘要"):
            self.verify("f" * 64)
        self.digests = ["other@sha256:" + "9" * 64]
        with self.assertRaisesRegex(ConfigurationError, "RepoDigests"):
            self.verify(self.sha)
        self.digests = [IMAGE]
        self.manifest["configuration"]["capture_container"] = "other"
        with self.assertRaisesRegex(ConfigurationError, "B5a-07"):
            self.verify(self.sha)

    def test_synthetic_container_rejections(self) -> None:
        """断网容器：名字不是合同容器、环境收据核验拒绝、二进制或镜像与抓包容器不同，一律在任何请求之前失败。"""

        cases = (
            ("name", lambda: self.manifest["configuration"].update(synthetic_capture_container="capture-cli-offline"), "断网容器必须是"),
            ("networked", lambda: setattr(self, "synthetic_error", capture.claude_arm64.Arm64EnvironmentReceiptError(
                "capture-cli-synthetic 必须以 network_mode none 运行（断网）")), "断网容器核验失败.*network_mode none"),
            ("binary", lambda: self.synthetic_overrides.update(
                client_binary={"path": f"/opt/claude-{TARGET}/claude", "sha256": "e" * 64, "version": TARGET}), "断网容器里的目标客户端"),
            ("image", lambda: self.synthetic_overrides.update(image_id="sha256:" + "8" * 64), "同一 image ID"),
        )
        for name, mutate, message in cases:
            with self.subTest(case=name):
                self.setUp()
                mutate()
                with self.assertRaisesRegex(ConfigurationError, message):
                    self.verify(self.sha)


class RoutingTests(unittest.TestCase):
    """执行容器路由：真实上游步骤只进抓包容器、合成步骤只进断网容器；合成步骤的操作名换成 ``synthetic-job:`` 前缀。"""

    LIVE = ["docker", "exec", CAPTURE, "python3", "/capture/tools/official_client_capture/claude_fw_e_relay.py", "capture",
            "--execute", "--acknowledge-live-requests", "--upstream-ip", "160.79.104.10"]
    SYNTHETIC_STEP = ["docker", "exec", SYNTHETIC, "python3", "/capture/tools/official_client_capture/claude_fw_e_relay.py", "capture",
                      "--execute", "--acknowledge-synthetic-responses"]

    @staticmethod
    def layout() -> capture.CaptureLayout:
        return capture.CaptureLayout(
            host_data_root=Path("/data"), capture_root=PurePosixPath("/capture"), capture_container=CAPTURE,
            capture_tool_root=PurePosixPath("/capture/tools/official_client_capture"), host_tool_root=TOOL_ROOT,
            runtime_image=IMAGE, synthetic_container=SYNTHETIC,
        )

    def test_step_route(self) -> None:
        layout = self.layout()
        self.assertEqual(capture._step_route(self.LIVE, layout), ("capture", CAPTURE))
        self.assertEqual(capture._step_route(self.SYNTHETIC_STEP, layout), ("synthetic", SYNTHETIC))
        # 零请求演练去掉确认参数后仍按 --upstream-ip 判定。
        self.assertEqual(capture._step_route(capture._rehearsal_argv(self.LIVE), layout), ("capture", CAPTURE))
        self.assertEqual(capture._step_route(capture._rehearsal_argv(self.SYNTHETIC_STEP), layout), ("synthetic", SYNTHETIC))
        for name, argv, message in (
            ("live-in-synthetic", [*self.LIVE[:2], SYNTHETIC, *self.LIVE[3:]], "真实上游步骤必须在容器 capture-cli 里"),
            ("synthetic-in-capture", [*self.SYNTHETIC_STEP[:2], CAPTURE, *self.SYNTHETIC_STEP[3:]], "合成步骤必须在容器 capture-cli-synthetic"),
            ("no-docker", self.SYNTHETIC_STEP[3:], "docker exec"),
            ("exec-option", ["docker", "exec", "-u", "root", SYNTHETIC, "python3"], "合成步骤必须在容器"),
        ):
            with self.subTest(case=name), self.assertRaisesRegex(ConfigurationError, message):
                capture._step_route(argv, layout)

    def test_execute_step_routes_and_renames_synthetic_operations(self) -> None:
        context = capture.ScenarioReceiptContext(layout=self.layout())
        seen: list[tuple[str, str]] = []

        def fake_run(argv, *, operation, **_kwargs):
            seen.append((argv[2], operation))
            return 0

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(capture, "_run_step_command", fake_run):
            log = Path(directory) / "step.log"
            options = {"timeout_seconds": 1, "environment": {}, "log_path": log, "job_id": "j", "deadline": None, "heartbeat": None}
            context.execute_step(self.SYNTHETIC_STEP, operation="job:j:step-1:attempt-1", **options)
            context.execute_step(self.LIVE, operation="job:j:step-2:attempt-1", **options)
            with self.assertRaisesRegex(ConfigurationError, "合成步骤必须"):
                context.execute_step([*self.SYNTHETIC_STEP[:2], CAPTURE, *self.SYNTHETIC_STEP[3:]], operation="job:j:step-3:attempt-1", **options)
            with self.assertRaisesRegex(ConfigurationError, "操作名必须以 job: 开头"):
                context.execute_step(self.SYNTHETIC_STEP, operation="other:j", **options)
        self.assertEqual(seen, [(SYNTHETIC, "synthetic-job:j:step-1:attempt-1"), (CAPTURE, "job:j:step-2:attempt-1")])

    def test_host_runtime_receipt_rejects_unregistered_container(self) -> None:
        context = capture.ScenarioReceiptContext(layout=self.layout(), attempt_root=Path("/nonexistent"), manifest={})
        with self.assertRaisesRegex(ConfigurationError, "登记的两个采集容器"):
            capture._issue_host_runtime_receipt(
                context, job_id="j", probe_id="p", run_index=1, step_index=1, container="other", deadline=None, heartbeat=None
            )


class ModelCapabilityStepTests(unittest.TestCase):
    """非主模型（模型能力取证，B2-07 补充项）步骤：步骤身份取输出目录名「<模型>-<基础 probe>」，relay 计划回显与清单记的是
    基础 probe 与模型（真实 relay 的形态）；模型或基础 probe 对不上即判失败（正式执行 schema-mismatch、演练 plan-mismatch）。"""

    MODEL_JOB = "official-model-capability-claude-opus-5"
    VALUES = {
        "{capture_root}": "/capture", "{campaign_id}": "c1", "{target_version}": TARGET, "{target_sha256}": TARGET_SHA,
        "{upstream_ip}": "160.79.104.10",
    }
    RECEIPT = {"sha256": "1" * 64, "run_nonce": "2" * 64}

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.layout = capture.CaptureLayout(
            host_data_root=self.root, capture_root=PurePosixPath("/capture"), capture_container=CAPTURE,
            capture_tool_root=PurePosixPath("/capture/tools/official_client_capture"), host_tool_root=TOOL_ROOT,
            runtime_image=IMAGE, synthetic_container=SYNTHETIC,
        )
        payload = json.loads(plan.scenario_manifest_path(TARGET).read_text(encoding="utf-8"))
        self.steps = {step["probe_id"]: step for job in payload["capture_jobs"] for step in job["steps"]}

    def argv(self, probe_id: str) -> list[str]:
        argv = []
        for item in self.steps[probe_id]["argv"]:
            for placeholder, value in self.VALUES.items():
                item = item.replace(placeholder, value)
            argv.append(item)
        return argv

    def manifest_row(self, coordinates: dict, **overrides) -> dict:
        output = capture._host_path(PurePosixPath(coordinates["output_root"]), self.layout)
        output.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": capture.RELAY_RUN_SCHEMA, "probe_id": coordinates["relay_probe_id"], "model": coordinates["model"],
            "status": "complete", "m_binding": {"complete": True}, "secret_scan": {"passed": True},
            "cleanup": {"relay_stopped": True, "hosts_restored": True},
            "client": {"version": f"{TARGET} (Claude Code)", "sha256": TARGET_SHA}, "runtime": {"host_runtime_receipt": dict(self.RECEIPT)},
        }
        manifest.update(overrides)
        (output / "relay-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return capture._relay_manifest_check(coordinates, self.layout, receipt=self.RECEIPT)

    def plan_row(self, coordinates: dict, **overrides) -> dict:
        echo = {
            "schema_version": capture.RELAY_PLAN_SCHEMA, "run_id": coordinates["run_id"], "probe_id": coordinates["relay_probe_id"],
            "model": coordinates["model"], "version": TARGET, "production_changes": False, "live_requests": True,
            "upstream_ip": "160.79.104.10",
        }
        echo.update(overrides)
        log = self.root / "plan.log"
        log.write_text(json.dumps(echo), encoding="utf-8")
        return capture._relay_plan_check(coordinates, log)

    def test_step_identity_and_relay_checks(self) -> None:
        probe = "claude-opus-5-v4-replay-tui"
        coordinates = capture._relay_step_coordinates(self.argv(probe))
        self.assertEqual(
            (coordinates["probe_id"], coordinates["relay_probe_id"], coordinates["model"], coordinates["run_id"]),
            (probe, "v4-replay-tui", "claude-opus-5", f"c1-{probe}"),
        )
        self.assertEqual(coordinates["output_root"], f"/capture/runs/claude-official/c1/{self.MODEL_JOB}/{probe}")
        passed = self.manifest_row(coordinates)
        self.assertEqual((passed["status"], passed["probe_id"]), ("passed", probe))
        self.assertEqual(self.plan_row(coordinates)["status"], "passed")
        for label, overrides in (
            ("模型对不上", {"model": "claude-fable-5"}),
            ("缺模型", {"model": None}),
            ("probe_id 写成步骤身份", {"probe_id": probe}),
        ):
            with self.subTest(label=label):
                row = self.manifest_row(coordinates, **overrides)
                self.assertEqual((row["status"], row["failure_code"]), ("failed", "schema-mismatch"))
                plan_row = self.plan_row(coordinates, **overrides)
                self.assertEqual((plan_row["status"], plan_row["failure_code"]), ("failed", "plan-mismatch"))
        # 主模型步骤：步骤身份就是基础 probe，模型取 Campaign 主模型，不按模型推身份。
        primary = capture._relay_step_coordinates(self.argv("v4-replay-tui"))
        self.assertEqual((primary["probe_id"], primary["relay_probe_id"]), ("v4-replay-tui", "v4-replay-tui"))
        self.assertEqual(self.manifest_row(primary, model="claude-sonnet-5")["status"], "passed")

    def test_output_directory_must_carry_step_identity(self) -> None:
        argv = self.argv("claude-opus-5-v4-replay-tui")
        position = argv.index("--output-root") + 1
        argv[position] = argv[position].rsplit("/", 1)[0] + "/claude-fable-5-v4-replay-tui"
        with self.assertRaisesRegex(ConfigurationError, "输出目录名既不是 --probe-id"):
            capture._relay_step_coordinates(argv)


class SupervisorCaptureContractTests(unittest.TestCase):
    """钉住合成步骤所依赖的监督器行为（只读调用，不改监督器）：出口守护下只有 ``job:`` 开头的操作会建立 capture-cli 停止
    绑定；绑定后 ``docker exec`` 到断网容器会被拒绝。监督器这两条行为变了，本测试先红，合成步骤的路由需要重新评估。"""

    def test_synthetic_operations_skip_capture_stop_binding(self) -> None:
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        state = {"egress_guard": {"campaign_dir": "/nonexistent", "required": True}, "campaign_id": "c", "owner_nonce": "n"}

        class AdmissionReached(Exception):
            pass

        with mock.patch.object(supervisor.arm64_environment, "require_runtime_egress", side_effect=AssertionError("合成步骤不得进入停止绑定")):
            self.assertIsNone(
                supervisor._capture_begin(Path("/nonexistent"), state, operation="synthetic-job:j:step-1:attempt-1", job_id="j", environment={})
            )
        with mock.patch.object(supervisor.arm64_environment, "require_runtime_egress", side_effect=AdmissionReached):
            with self.assertRaises(AdmissionReached):
                supervisor._capture_begin(Path("/nonexistent"), state, operation="job:j:step-1:attempt-1", job_id="j", environment={})
        container_id = CONTAINER_IDS[CAPTURE]
        pinned, environment = supervisor._capture_pin_command(["docker", "exec", CAPTURE, "python3"], {}, container_id)
        self.assertEqual((pinned[2], environment["CAPTURE_CONTAINER"]), (container_id, container_id))
        with self.assertRaisesRegex(supervisor.SupervisorError, "停止绑定不一致"):
            supervisor._capture_pin_command(["docker", "exec", SYNTHETIC, "python3"], {}, container_id)
        self.assertEqual(supervisor.CAPTURE_STOP_SERVICE, CAPTURE)
        self.assertEqual((capture.LIVE_STEP_OPERATION_PREFIX, capture.SYNTHETIC_STEP_OPERATION_PREFIX), ("job:", "synthetic-job:"))


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
        # 真实上游步骤只在联网的抓包容器里执行。
        self.assertEqual({call["container"] for call in calls}, {CAPTURE})
        self.assertEqual({call["container"] for call in self.docker_calls()}, {CAPTURE})
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
        ledger = self.supervisor_ledger(campaign)
        for index in range(1, 5):
            self.assertIn(f'"job:{SMALL_LIVE_JOB}:step-{index}:attempt-1"', ledger)
        self.assertNotIn("synthetic-job:", ledger)
        self.assertEqual({step["host_runtime_receipt"]["container_name"] for step in job["steps"]}, {CAPTURE})
        # 判定方法的反例：证据根一旦出现（有输出落盘）或收据被改，复核即判失败。
        (self.data / "runs" / "claude-official" / "claude-code-2_1_285-t1" / SMALL_LIVE_JOB).mkdir(parents=True)
        with self.patched():
            tampered = capture.verify_rehearsal_attempt(campaign, result["attempt_id"])
        self.assertEqual(tampered["verdict"], "failed")
        self.assertIn(f"job:{SMALL_LIVE_JOB}:evidence-roots-absent", [item["check"] for item in tampered["checks"] if not item["passed"]])

    def test_synthetic_job_rehearsal_runs_in_offline_container(self) -> None:
        """合成 probe 的零请求演练（ARM64 上 official-synthetic-oauth-refresh 的重跑方式）：步骤、宿主凭据与可见核对都在
        断网容器里，监督器操作名是 synthetic-job:，复核通过且包含路由与断网容器核验两项。"""

        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB, SMALL_LIVE_JOB}), mode="preflight_only")
        with self.patched():
            code, result, stderr = self.run_main(
                ["capture-official", "run", "--campaign-dir", str(campaign), "--zero-request-rehearsal", "--job-id", SMALL_SYNTHETIC_JOB]
            )
            self.assertEqual((code, result["status"]), (0, "rehearsal_passed"), stderr)
            verdict = capture.verify_rehearsal_attempt(campaign, result["attempt_id"])
        self.assertEqual(verdict["verdict"], "passed", [item for item in verdict["checks"] if not item["passed"]])
        names = {item["check"] for item in verdict["checks"]}
        self.assertTrue({f"job:{SMALL_SYNTHETIC_JOB}:container-routing", "synthetic-container-verified"} <= names)
        calls = self.relay_calls()
        self.assertEqual([(call["probe"], call["container"], call["dry_run"]) for call in calls], [("v4-oauth-refresh", SYNTHETIC, True)])
        self.assertEqual({call["container"] for call in self.docker_calls()}, {SYNTHETIC})
        receipts = [argv for argv in self.host.calls if argv[1].endswith("runtime_host_receipt.py")]
        self.assertEqual([argv[argv.index("--container") + 1] for argv in receipts], [SYNTHETIC])
        visibility = [argv for argv in self.host.calls if argv[:2] == ["docker", "exec"] and argv[3] == "/usr/bin/sha256sum" and argv[4] != GLOBAL_STATE]
        self.assertEqual({argv[2] for argv in visibility}, {SYNTHETIC})
        ledger = self.supervisor_ledger(campaign)
        self.assertIn(f'"synthetic-job:{SMALL_SYNTHETIC_JOB}:step-1:attempt-1"', ledger)
        self.assertNotIn(f'"job:{SMALL_SYNTHETIC_JOB}:step-1:attempt-1"', ledger)
        attempt = json.loads((campaign / "rehearsal" / "official" / "attempts" / result["attempt_id"] / "attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(attempt["identity"]["synthetic_capture_container"], SYNTHETIC)
        self.assertEqual(attempt["layout"]["synthetic_capture_container"], SYNTHETIC)
        step = attempt["results"][0]["steps"][0]
        self.assertEqual((step["argv"][:3], step["host_runtime_receipt"]["container_name"]), (["docker", "exec", SYNTHETIC], SYNTHETIC))
        # 演练只记录前提：两个容器里的必需文件都在、全局状态两边相同。
        self.assertEqual((attempt["preconditions"]["missing"], attempt["preconditions"]["global_state"]["identical_across_containers"]), ([], True))
        self.assertEqual(set(attempt["preconditions"]["targets"]), {"capture", "synthetic"})

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
        # 12 个主模型 Job 加 2 个非主模型的模型能力取证 Job；步骤 79 个主模型 probe 加 33 个非主模型 probe。
        self.assertEqual((summary["campaign_mode"], len(summary["jobs"])), ("preflight_only", 14))
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.assertTrue(manifest["rehearsal_only"])
        self.assertEqual(manifest["configuration"]["capture_root"], "/capture")
        self.assertEqual(
            (manifest["configuration"]["capture_container"], manifest["configuration"]["synthetic_capture_container"]), (CAPTURE, SYNTHETIC)
        )
        self.assertEqual(manifest["configuration"]["capture_tool_root"], "/root/oauth-capture/tools/official_client_capture")
        self.assertEqual(sum(job["steps"] for job in summary["jobs"]), 112)
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
        # 合成 probe 在断网容器、真实上游 probe 在抓包容器；宿主凭据签给步骤实际所在的容器（替身 relay 也逐步核对）。
        self.assertEqual({call["probe"]: call["container"] for call in calls}["v4-oauth-refresh"], SYNTHETIC)
        self.assertEqual({call["container"] for call in calls if call["probe"] != "v4-oauth-refresh"}, {CAPTURE})
        for job in attempt["results"]:
            expected = SYNTHETIC if job["id"] == SMALL_SYNTHETIC_JOB else CAPTURE
            self.assertEqual({step["host_runtime_receipt"]["container_name"] for step in job["steps"]}, {expected})
            self.assertEqual({step["argv"][2] for step in job["steps"]}, {expected})
        ledger = self.supervisor_ledger(campaign)
        self.assertIn(f'"synthetic-job:{SMALL_SYNTHETIC_JOB}:step-1:attempt-1"', ledger)
        self.assertIn(f'"job:{SMALL_LIVE_JOB}:step-1:attempt-1"', ledger)
        self.assertEqual(attempt["preconditions"]["global_state"], {"path": GLOBAL_STATE, "identical_across_containers": True})
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
        # B2-08 落实后：未隔离的污染封锁 Campaign，提示先清理残留进程、再 environment-isolate。
        self.assertIn("environment-isolate", stderr)


class OfflineContainerFormalTests(CaptureFixture):
    """正式 run 对断网容器的预约前门禁与污染判定。"""

    def run_formal(self, campaign: Path) -> tuple[int, dict, str]:
        with self.patched():
            code, result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
        return code, result or {}, stderr

    def test_preconditions_cover_both_containers_before_reservation(self) -> None:
        cases = (
            ("synthetic-global-state-missing", {"missing": frozenset({(SYNTHETIC, GLOBAL_STATE)})}, "synthetic:global_state"),
            ("capture-credentials-missing", {"missing": frozenset({(CAPTURE, "/root/.claude/.credentials.json")})}, "capture:credentials_file"),
            ("global-state-differs", {"global_state_digests": {CAPTURE: "5" * 64, SYNTHETIC: "6" * 64}}, "全局状态文件内容不同"),
        )
        for index, (name, options, message) in enumerate(cases):
            with self.subTest(case=name):
                self.host = FakeHost(**options)
                campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB}), campaign_id=f"claude-code-2_1_285-p{index}")
                code, _result, stderr = self.run_formal(campaign)
                self.assertEqual(code, 1, stderr)
                self.assertIn(message, stderr)
                self.assertFalse((campaign / "official").exists())
        self.assertEqual(self.relay_calls(), [])

    def test_residual_process_in_offline_container_contaminates(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB}))
        self.set_control({"residual": "v4-oauth-refresh"})
        code, result, stderr = self.run_formal(campaign)
        self.assertEqual((code, result["status"]), (2, "environment_contaminated"), stderr)
        with self.patched():
            _root, attempt = claude_upgrade._load_capture_attempt(campaign, "official", None, result["attempt_id"], _replay_evidence_permissions=False)
        job = attempt["results"][0]
        self.assertTrue(job["environment_contaminated"])
        self.assertEqual(job["attempt_index"], 1)
        self.assertIn(
            {"check_id": f"job.{SMALL_SYNTHETIC_JOB}.environment.processes", "failure_code": "drifted"},
            [{key: item[key] for key in ("check_id", "failure_code")} for item in attempt["failure_observations"]],
        )
        self.assertEqual(len(self.relay_calls()), 1)

    def test_offline_container_drift_contaminates(self) -> None:
        campaign = self.write_campaign(_reduced_payload({SMALL_SYNTHETIC_JOB}))
        self.set_control({"contaminate": "v4-oauth-refresh"})
        code, result, stderr = self.run_formal(campaign)
        self.assertEqual((code, result["status"]), (2, "environment_contaminated"), stderr)
        environment = json.loads(self.env_file.read_text(encoding="utf-8"))
        self.assertEqual(environment["configuration"]["targets"]["synthetic"]["hosts_sha256"], "f" * 64)
        self.assertEqual(environment["configuration"]["targets"]["capture"]["hosts_sha256"], "1" * 64)


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
        # 原短验内补恢复边界：保留故障后的宿主值和原字节，不另登记场景 UID。
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        from tools.official_client_capture.tests.test_claude_upgrade_candidate_capture import CandidateFixture
        with tempfile.TemporaryDirectory() as records, mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: records}):
            first = CandidateFixture('runTest')
            first.setUp()
            self.addCleanup(shutil.rmtree, first.root, ignore_errors=True)
            first.host.calls.append(['真实故障前已调用的宿主命令'])
            first.execution_tree_calls.append(first.tool_copy)
            first.set_candidate_control({'interrupted': True})
            original = first.candidate_control.read_bytes()
            stops.checkpoint(first, 'candidate-runtime', phase='runtime-bound')
            with mock.patch.dict(os.environ, {stops.METHOD_RESUME_ENV: '1'}), \
                    mock.patch.object(stops.tempfile, 'mkdtemp', side_effect=AssertionError('恢复不能新建世界')):
                resumed = CandidateFixture('runTest')
                resumed.setUp()
                saved = stops.resume_checkpoint(resumed, 'candidate-runtime')
                self.assertEqual(saved['phase'], 'runtime-bound')
                self.assertEqual(resumed.root, first.root)
                self.assertEqual(resumed.host.calls, first.host.calls)
                self.assertEqual(resumed.execution_tree_calls, first.execution_tree_calls)
                self.assertEqual(resumed.candidate_control.read_bytes(), original)
            cursor = stops._state_path('cursor:candidate-runtime')
            raw = cursor.read_bytes()
            damaged = json.loads(raw)
            damaged['payload']['state']['phase'] = '伪造完成'
            cursor.write_text(json.dumps(damaged), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '摘要'):
                stops.resume_checkpoint(first, 'candidate-runtime')
            cursor.write_bytes(raw)
            self.assertEqual(stops.active_seconds('method', 1.5), 1.5)
            self.assertEqual(stops.active_seconds('method', 2.0), 3.5)
            self.assertEqual(stops.active_seconds('method'), 3.5)
            # 后续阶段尚未首次建场必须由原阶段游标精确允许；曾开始却丢停点仍拒绝。
            stops.save_state('cursor:future-stage', {'phase': 'before-fixture',
                'initializations': {'fixture:future': 'uninitialized'}})
            with mock.patch.dict(os.environ, {stops.METHOD_RESUME_ENV: '1'}):
                with self.assertRaisesRegex(ValueError, '缺少夹具停点'):
                    stops.fixture_directory(self, 'future')
                with stops.initialization_scope('fixture:future', cursor='future-stage', phase='before-fixture'):
                    future, restoring = stops.fixture_directory(self, 'future')
                self.addCleanup(shutil.rmtree, future, ignore_errors=True)
                self.assertFalse(restoring)
                self.assertEqual(stops.fixture_directory(self, 'future'), (future, True))
                with self.assertRaisesRegex(ValueError, '首次初始化许可'):
                    with stops.initialization_scope('fixture:future', cursor='future-stage', phase='before-fixture'):
                        self.fail('完成的初始化不能再建场')


if __name__ == "__main__":
    unittest.main()
