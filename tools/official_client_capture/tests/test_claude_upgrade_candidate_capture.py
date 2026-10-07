"""Claude 候选采集 run（Claude 升级控制面设计 B4-01b）。

不起 Docker、不联网、不读凭据，沿用官方取证测试的夹具（``test_claude_upgrade_capture.CaptureFixture``：临时数据根、docker
替身、宿主命令边界替身、探针替身），另加：

* 候选宿主脚本替身（``claude_run_candidate_capture.sh`` 同名，转调一份 Python 替身）：记下监督器给的环境变量，正式执行时
  跑中继命令后按控制文件写恢复报告（可制造恢复失败、报告缺失、前检拒绝、网关漂移），演练时直接执行 dry-run；
* 候选形态的中继替身：dry-run 回显候选侧计划，execute 写候选侧 relay 清单（``side``、``candidate_gateway``、connect 上游）；
* 网关环境事实换成读一份可被宿主脚本替身改写的文件；服务镜像与正式入口门在 run 流程用例里换成替身——正式入口门本身
  由 ``FormalAdmissionTests`` 在 VC-4 真实链（record-candidate-build 到 candidate_sealed）上验收。

覆盖：候选步骤路由与宿主脚本坐标映射、``_campaign_jobs`` 只凭 candidate-id 展开、网关只读快照与漂移字段、每步恢复报告
核对、正式 run 的 attempt 字段与 schema、恢复失败与网关漂移判环境污染、零请求演练与复核、候选读侧（装载、活跃判定、
失败 attempt 拦截）、两个服务面成员与正式入口门。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_campaign
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_project_ledger_fixture
from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
from tools.official_client_control import contracts

CAPTURE = capture_tests.CAPTURE
SYNTHETIC = capture_tests.SYNTHETIC
CANDIDATE = "claude-cand-r1"
REFRESH_JOB = "candidate-synthetic-oauth-refresh"  # 1 个合成 probe，网关账号刷新触发
LIVE_JOB = "candidate-transport-baseline"  # 4 个真实上游 probe，客户端触发
SERVICE, KEEPER, POSTGRES = "sub2apiplus", "sub2apiplus-keeper", "sub2apiplus-postgres"
CANDIDATE_CONFIGURATION = {
    "service_container": SERVICE,
    "keeper_container": KEEPER,
    "postgres_container": POSTGRES,
    "claude_account_id": "7",
    "api_key_id": "9",
}
SERVICE_IMAGE_ID = "sha256:" + "8" * 64
SERVICE_IMAGE = "registry.local/sub2api@sha256:" + "7" * 64
BASE_GATEWAY = {
    "service": {
        "container": SERVICE, "container_id": "a" * 64, "image_id": SERVICE_IMAGE_ID, "running": True, "started_at": "t0",
        "port": 8080, "hosts_sha256": "1" * 64, "ca_bundle_sha256": "2" * 64, "custom_ca_present": False,
    },
    "keeper": {"container": KEEPER, "running": True},
    "postgres": {"container": POSTGRES},
    "account": {"id": 7, "shape": "anthropic|oauth|NULL|live", "proxy_state": "3|NULL", "gate_sha256": "5" * 64},
    "api_key": {"id": 9, "shape": "active|5|live"},
    "residual_candidate_proxies": 0,
    "runtime_residue": [],
}
ACTIVE_CATALOG = {
    "schema_version": "official-client-release-catalog/v1",
    "selectors": {
        "production_active": {"kind": "release", "release_sha256": "1" * 64},
        "production_rollback": {"kind": "operational-deployment", "deployment": "dmit"},
        "validation_candidate": None,
    },
    "releases": [
        {"release_sha256": "1" * 64, "version": "2.1.226", "platform": "linux/arm64", "profile": {"sha256": "2" * 64},
         "wire": {"sha256": "3" * 64}, "bundle_sha256": "4" * 64},
    ],
}

FAKE_HOST_WRAPPER = '#!/usr/bin/env bash\nexec python3 "$FAKE_CANDIDATE_HOST" "$0" "$@"\n'

FAKE_HOST = r'''#!/usr/bin/env python3
"""claude_run_candidate_capture.sh 的离线替身：记下环境，正式执行时跑中继命令并按控制文件写恢复报告。"""
import json, os, subprocess, sys
script, args = sys.argv[1], sys.argv[2:]
control = json.load(open(os.environ["FAKE_CANDIDATE_CONTROL"], encoding="utf-8"))
probe = os.environ["PROBE_ID"]
dry = "--dry-run" in args
with open(os.environ["FAKE_CANDIDATE_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "script": script, "probe": probe, "dry_run": dry, "target": args[2],
        "capture_container": os.environ.get("CAPTURE_CONTAINER"), "gate": os.environ.get("CLAUDE_CANDIDATE_CAPTURE"),
        "trigger": os.environ.get("GATEWAY_TRIGGER"), "account": os.environ.get("ACCOUNT_ID"), "api_key": os.environ.get("API_KEY_ID"),
        "admin_file": bool(os.environ.get("ADMIN_BEARER_TOKEN_FILE")),
    }) + "\n")
if dry:
    os.execvp(args[0], args)
if probe in control.get("precheck_fail", []):
    print("只读前检拒绝", file=sys.stderr)
    sys.exit(1)
code = subprocess.run(args).returncode
restored = probe not in control.get("restoration_fail", [])
if probe in control.get("gateway_drift", []):
    path = os.environ["FAKE_GATEWAY_FILE"]
    facts = json.load(open(path, encoding="utf-8"))
    facts["residual_candidate_proxies"] = 1
    json.dump(facts, open(path, "w", encoding="utf-8"))
if probe not in control.get("no_report", []):
    report_dir = os.path.join(os.path.dirname(os.environ["OUTPUT_ROOT"]), "candidate-gateway")
    os.makedirs(report_dir, mode=0o700, exist_ok=True)
    status = "restoration_failed" if not restored else ("complete" if code == 0 else "capture_failed")
    report = {
        "schema_version": "claude-candidate-gateway-restoration/v1", "status": status, "run_id": os.environ["RUN_ID"],
        "probe_id": probe, "account_id": int(os.environ["ACCOUNT_ID"]), "api_key_id": int(os.environ["API_KEY_ID"]),
        "trigger": os.environ["GATEWAY_TRIGGER"], "relay_port": int(os.environ["RELAY_PORT"]), "capture_exit_code": code,
        "account_proxy_original": "3|NULL", "account_proxy_restored_equal": restored, "account_gate_restored_equal": True,
        "hosts_sha256_equal": True, "ca_bundle_sha256_equal": True, "keeper_was_running": True, "runtime_secrets_removed": True,
        "started_at_utc": "2026-10-07T00:00:00Z", "ended_at_utc": "2026-10-07T00:00:01Z",
    }
    with open(os.path.join(report_dir, probe + ".json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle)
sys.exit(97 if not restored else code)
'''

CANDIDATE_RELAY = r'''#!/usr/bin/env python3
"""候选模式中继替身：dry-run 回显候选侧计划；execute 核对宿主凭据后写候选侧 relay 清单与一份非空产物。"""
import hashlib, json, os, sys
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
probe = value("--probe-id")
dry = "--dry-run" in args
container = os.environ.get("FAKE_DOCKER_CONTAINER")
with open(os.environ["FAKE_RELAY_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"probe": probe, "dry_run": dry, "execute": "--execute" in args, "container": container,
                             "acknowledgements": [item for item in args if item.startswith("--acknowledge")]}) + "\n")
output = value("--output-root")
if dry:
    if os.path.exists(output):
        sys.exit(2)
    print(json.dumps({"schema_version": "claude-code-fw-e-relay-plan/v1", "side": "candidate", "relay_mode": "connect",
                      "run_id": value("--run-id"), "probe_id": probe, "model": value("--model"), "version": value("--expected-version"),
                      "live_requests": value("--upstream-ip") is not None, "upstream_ip": value("--upstream-ip"),
                      "production_changes": False}, indent=2, sort_keys=True))
    sys.exit(0)
receipt = value("--host-runtime-receipt")
digest = hashlib.sha256(open(receipt, "rb").read()).hexdigest()
if digest != value("--host-runtime-receipt-sha256") or json.load(open(receipt, encoding="utf-8"))["container"]["name"] != container:
    sys.exit(2)
os.makedirs(output, mode=0o700)
with open(os.path.join(output, "relay-raw.bin"), "wb") as handle:
    handle.write(b"POST /v1/messages HTTP/1.1\r\n\r\n")
manifest = {"schema_version": "claude-code-fw-e-relay-run/v1", "side": "candidate", "probe_id": probe, "model": value("--model"),
            "status": "complete", "m_binding": {"complete": True}, "secret_scan": {"passed": True},
            "cleanup": {"relay_stopped": True, "hosts_restored": True},
            "client": {"version": value("--expected-version") + " (Claude Code)", "sha256": value("--expected-sha256")},
            "candidate_gateway": {"base_url": "http://sub2apiplus:8080", "account_id": 7, "api_key_id": 9, "trigger": "client",
                                  "client_authentication": "x-api-key", "oauth_credentials_read": False},
            "upstream": {"relay_mode": "connect"},
            "runtime": {"host_runtime_receipt": {"sha256": digest, "run_nonce": value("--run-nonce")}}}
with open(os.path.join(output, "relay-manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle)
sys.exit(0)
'''


def _payload() -> dict:
    """裁剪的真实 2.1.285 清单：两个候选 Job 加镜像的两个官方 Job（场景的 probe 集合按官方 Job 核对）。"""

    payload = capture_tests._reduced_payload({REFRESH_JOB, LIVE_JOB, capture_tests.SMALL_SYNTHETIC_JOB, capture_tests.SMALL_LIVE_JOB})
    # probe_catalog.probe_count 只数官方 Job 的主模型步骤（候选 Job 镜像同一组 probe，不另计）。
    official = [step for job in payload["capture_jobs"] if job["phase"] == "official" for step in job["steps"]
                if plan.step_model(step["probe_id"], step["argv"]) is None]
    payload["probe_catalog"] = {**payload["probe_catalog"], "probe_count": len(official)}
    return payload


class CandidateFixture(capture_tests.CaptureFixture):
    """官方取证夹具 + 候选宿主脚本替身、候选中继替身、网关事实文件与正式入口门替身。"""

    def setUp(self) -> None:
        super().setUp()
        relay = self.tool_copy / "claude_fw_e_relay.py"
        relay.write_text(CANDIDATE_RELAY, encoding="utf-8")
        wrapper = self.tool_copy / plan.CANDIDATE_SCRIPT
        wrapper.write_text(FAKE_HOST_WRAPPER, encoding="utf-8")
        wrapper.chmod(0o700)
        self.fake_host = self.root / "fake-candidate-host.py"
        self.fake_host.write_text(FAKE_HOST, encoding="utf-8")
        self.candidate_control = self.root / "candidate-control.json"
        self.candidate_calls_file = self.root / "candidate-calls.jsonl"
        self.gateway_file = self.root / "gateway.json"
        self.gateway_file.write_text(json.dumps(BASE_GATEWAY), encoding="utf-8")
        self.set_candidate_control({})
        self.admin_token = self.root / "admin-token"
        self.admin_token.write_text("hdr.payload.sig\n", encoding="utf-8")
        self.admin_token.chmod(0o600)
        self.source = self.root / "candidate-source"
        (self.source / "backend").mkdir(parents=True)
        (self.source / "backend" / "main.go").write_text("package main\n", encoding="utf-8")

    def set_candidate_control(self, control: dict) -> None:
        self.candidate_control.write_text(json.dumps(control), encoding="utf-8")

    def candidate_calls(self) -> list[dict]:
        if not self.candidate_calls_file.exists():
            return []
        return [json.loads(line) for line in self.candidate_calls_file.read_text(encoding="utf-8").splitlines()]

    def write_candidate_campaign(self, *, mode: str = "formal", campaign_id: str = "claude-code-2_1_285-t1", catalog: bool = True) -> Path:
        """候选变量冻结进 configuration；正式 Campaign 另冻结 Active Release 目录（建 Campaign 时冻结，之后才在总账注册）。"""

        campaign = self.write_campaign(_payload(), mode="preflight_only", campaign_id=campaign_id, configuration=CANDIDATE_CONFIGURATION)
        manifest = json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))
        manifest["campaign_mode"] = mode
        if catalog:
            path = campaign / "inputs" / "active-release-catalog.json"
            path.write_text(json.dumps(ACTIVE_CATALOG, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            manifest["inputs"]["active_release_catalog"] = {"path": "inputs/active-release-catalog.json", "sha256": claude_upgrade.file_sha256(path)}
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        (campaign / "campaign.json").write_bytes(raw)
        (campaign / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
        if mode == "formal":
            claude_project_ledger_fixture.register_claude_campaign(campaign)
        return campaign

    def fake_gateway_facts(self, manifest, host_data_root, *, deadline=None, heartbeat=None):
        del manifest, host_data_root, deadline, heartbeat
        return json.loads(self.gateway_file.read_text(encoding="utf-8"))

    def fake_admission(self, campaign_dir, manifest, candidate_id, *, deadline=None, heartbeat=None):
        del campaign_dir, deadline, heartbeat
        facts = {
            "candidate_revision": 1,
            "build_receipt": {"path": f"candidates/{candidate_id}/build-receipt.json", "sha256": "6" * 64, "receipt_digest": "6" * 64},
            "build_id": "claude-build-0001",
            "deployed_version": capture_tests.TARGET,
            "image_reference": SERVICE_IMAGE,
            "image_id": SERVICE_IMAGE_ID,
            "manifest_digest": "sha256:" + "7" * 64,
            "source_root": str(self.source),
            "source_tree_sha256": claude_upgrade._directory_tree_digest(self.source),
            "git_commit": "c" * 40,
            "profile_digest": "d" * 64,
            "release_sha256": "e" * 64,
            "wire_sha256": "f" * 64,
            "candidate_frozen_ref": {"fact_id": "fake"},
            "candidate_build_receipt_ref": {"receipt_id": "fake"},
        }
        return candidate_capture.CandidateAdmission(
            candidate_id, manifest, candidate_capture.candidate_variables(manifest), facts, persona=dict(claude_upgrade_campaign.CLAUDE_PERSONA)
        )

    @contextlib.contextmanager
    def candidate_patched(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.patched())
            stack.enter_context(mock.patch.object(candidate_capture, "_collect_gateway_facts", self.fake_gateway_facts))
            stack.enter_context(mock.patch.object(candidate_capture, "formal_admission", self.fake_admission))
            stack.enter_context(mock.patch.object(candidate_capture, "observe_service_image", lambda *_a, **_k: SERVICE_IMAGE_ID))
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {
                        "FAKE_CANDIDATE_HOST": str(self.fake_host),
                        "FAKE_CANDIDATE_CONTROL": str(self.candidate_control),
                        "FAKE_CANDIDATE_CALLS": str(self.candidate_calls_file),
                        "FAKE_GATEWAY_FILE": str(self.gateway_file),
                        "ADMIN_BEARER_TOKEN_FILE": str(self.admin_token),
                    },
                )
            )
            yield

    def run_candidate(self, campaign: Path, *extra: str) -> tuple[int, dict, str]:
        with self.candidate_patched():
            code, result, stderr = self.run_main(
                ["capture-candidate", "run", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, *extra]
            )
        return code, result or {}, stderr

    def load_attempt(self, campaign: Path, attempt_id: str) -> tuple[Path, dict]:
        with self.candidate_patched():
            return claude_upgrade._load_capture_attempt(campaign, "candidate", CANDIDATE, attempt_id, _replay_evidence_permissions=False)


class CandidateJobsTests(CandidateFixture):
    """``_campaign_jobs`` 候选分支：只凭 candidate-id 展开，共享流程传入的 Codex 风格身份参数不改变 Job。"""

    def test_candidate_jobs_expand_with_candidate_id_only(self) -> None:
        campaign = self.write_candidate_campaign(mode="preflight_only", catalog=False)
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        jobs = claude_upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=CANDIDATE)
        self.assertEqual({job.job_id for job in jobs}, {REFRESH_JOB, LIVE_JOB})
        run_id = f"claude-code-2_1_285-t1-{CANDIDATE}"
        for job in jobs:
            self.assertEqual(job.evidence_roots, (f"{self.data}/runs/claude-candidate/{run_id}/{job.job_id}",))
            for step in job.steps:
                self.assertEqual(step["argv"][:2], ["bash", f"{self.tool_copy}/{plan.CANDIDATE_SCRIPT}"])
                self.assertEqual(step["argv"][2:5], ["docker", "exec", CAPTURE])
                self.assertEqual(step["environment"]["RUN_ID"], f"{run_id}-{step['probe_id']}")
                self.assertEqual((step["environment"]["SERVICE_CONTAINER"], step["environment"]["ACCOUNT_ID"]), (SERVICE, "7"))
        # 共享流程按 Codex 习惯传入的候选身份参数在 Claude 一律不参与展开（run 与 seal 得到同一组 Job）。
        noisy = claude_upgrade._campaign_jobs(
            campaign, manifest, "candidate", candidate_id=CANDIDATE, runtime_image="other@sha256:" + "0" * 64, build_id="x",
            profile_digest="0" * 64, candidate_image_id=SERVICE_IMAGE_ID, source_tree_sha256="0" * 64, candidate_purpose="validation_only",
        )
        self.assertEqual([capture._job_execution_sha256(job) for job in noisy], [capture._job_execution_sha256(job) for job in jobs])
        with self.assertRaisesRegex(ConfigurationError, "合法的 candidate-id"):
            claude_upgrade._campaign_jobs(campaign, manifest, "candidate")
        with self.assertRaisesRegex(ConfigurationError, "不得携带 candidate-id"):
            claude_upgrade._campaign_jobs(campaign, manifest, "official", candidate_id=CANDIDATE)

    def test_frozen_candidate_rows_are_checked_against_the_plan_context(self) -> None:
        """冻结行按不带候选 ID 的展开（plan 时的上下文）核执行摘要：模板没变即通过，被改即拒绝。"""

        good = self.write_candidate_campaign(mode="preflight_only", catalog=False)
        manifest = claude_upgrade.load_campaign_manifest(good)
        base = capture._expand_phase_jobs(good, manifest, "candidate", candidate_id=None)
        rows = [{"id": job.job_id, "phase": "candidate", "execution_sha256": capture._job_execution_sha256(job)} for job in base]
        manifest = {**manifest, "jobs": rows}
        self.assertEqual(len(claude_upgrade._campaign_jobs(good, manifest, "candidate", candidate_id=CANDIDATE)), 2)
        tampered = {**manifest, "jobs": [{**rows[0], "execution_sha256": "0" * 64}, rows[1]]}
        with self.assertRaisesRegex(ConfigurationError, "执行摘要漂移"):
            claude_upgrade._campaign_jobs(good, tampered, "candidate", candidate_id=CANDIDATE)


class RehearsalCampaignBuilderTests(CandidateFixture):
    """演练 Campaign 生成器冻结候选五个变量；按 plan 冻结的候选行（不带候选 ID 的展开）核对后展开全部 12 个候选 Job。"""

    def test_builder_freezes_candidate_variables(self) -> None:
        campaign = self.data / "evidence" / "campaigns" / "claude-code-2_1_285-r2"
        arguments = capture._build_parser().parse_args(
            ["rehearsal-campaign", "--campaign-dir", str(campaign), "--target-version", capture_tests.TARGET,
             "--target-sha256", capture_tests.TARGET_SHA, "--runtime-image", capture_tests.IMAGE, "--model", "claude-sonnet-5",
             "--upstream-ip", "160.79.104.10", "--campaign-id", "claude-code-2_1_285-r2", "--service-container", SERVICE,
             "--keeper-container", KEEPER, "--postgres-container", POSTGRES, "--claude-account-id", "7", "--api-key-id", "9"]
        )
        summary = capture.build_rehearsal_campaign(arguments)
        self.assertIn("capture-candidate run", summary["next_candidate_command"])
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.assertEqual({key: manifest["configuration"][key] for key in CANDIDATE_CONFIGURATION}, CANDIDATE_CONFIGURATION)
        rows = [row for row in manifest["jobs"] if row["phase"] == "candidate"]
        self.assertEqual(len(rows), 12)
        jobs = claude_upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=CANDIDATE)
        self.assertEqual(sorted(job.job_id for job in jobs), sorted(row["id"] for row in rows))
        self.assertTrue(all(job.evidence_roots[0].startswith(f"/capture/runs/claude-candidate/claude-code-2_1_285-r2-{CANDIDATE}/") for job in jobs))


class CandidateModuleLayerTests(unittest.TestCase):
    def test_candidate_capture_module_is_control_layer(self) -> None:
        """候选 run 的代码都在 control 层：新模块按前缀归 control，wire 闭包的模块集合由 test_claude_upgrade_capture 锁定。"""

        from tools.official_client_capture import claude_tool_identity_policy as claude_identity

        policy = claude_identity.load_policy()
        self.assertEqual(claude_identity.classify_path(policy, "official_client_capture/claude_upgrade_candidate_capture.py"), "control")
        self.assertEqual(claude_identity.classify_path(policy, "official_client_capture/claude_run_candidate_capture.sh"), "wire_producer")


class CandidateRoutingTests(unittest.TestCase):
    """候选步骤路由：一律进抓包容器；宿主脚本的容器坐标映射到同一工具执行副本的宿主坐标。"""

    @staticmethod
    def layout() -> capture.CaptureLayout:
        return capture.CaptureLayout(
            host_data_root=Path("/data"), capture_root=PurePosixPath("/capture"), capture_container=CAPTURE,
            capture_tool_root=PurePosixPath("/root/oauth-capture/tools/official_client_capture"), host_tool_root=capture_tests.TOOL_ROOT,
            runtime_image=capture_tests.IMAGE, synthetic_container=SYNTHETIC,
        )

    def step(self, *, container: str = CAPTURE, script: str | None = None) -> list[str]:
        script = script or f"/root/oauth-capture/tools/official_client_capture/{plan.CANDIDATE_SCRIPT}"
        return ["bash", script, "docker", "exec", container, "python3", "/root/oauth-capture/tools/official_client_capture/claude_fw_e_relay.py",
                "capture", "--execute", "--acknowledge-synthetic-responses"]

    def test_candidate_steps_route_to_capture_container(self) -> None:
        layout = self.layout()
        self.assertEqual(capture._step_route(self.step(), layout), ("capture", CAPTURE))
        self.assertEqual(capture._step_route(capture._rehearsal_argv(self.step()), layout), ("capture", CAPTURE))
        with self.assertRaisesRegex(ConfigurationError, "必须在容器 capture-cli 里执行"):
            capture._step_route(self.step(container=SYNTHETIC), layout)
        with self.assertRaisesRegex(ConfigurationError, "工具执行副本"):
            capture._step_route(self.step(script=f"/tmp/{plan.CANDIDATE_SCRIPT}"), layout)
        # 容器坐标 /root/oauth-capture/... 经登记的 runs 别名映射到宿主数据根下的同一副本。
        host = capture._host_step_argv(self.step(), layout)
        self.assertEqual(host[1], f"/data/tools/official_client_capture/{plan.CANDIDATE_SCRIPT}")
        self.assertEqual(host[2:], self.step()[2:])
        self.assertEqual(capture._host_step_argv(self.step()[2:], layout), self.step()[2:])

    def test_execute_step_runs_host_script_under_live_operation(self) -> None:
        context = capture.ScenarioReceiptContext(layout=self.layout())
        seen: list[tuple[list[str], str]] = []

        def fake_run(argv, *, operation, **_kwargs):
            seen.append((list(argv), operation))
            return 0

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(capture, "_run_step_command", fake_run):
            options = {"timeout_seconds": 1, "environment": {}, "log_path": Path(directory) / "step.log", "job_id": "j",
                       "deadline": None, "heartbeat": None}
            context.execute_step(self.step(), operation="job:j:step-1:attempt-1", **options)
        self.assertEqual(seen[0][1], "job:j:step-1:attempt-1")
        self.assertEqual(seen[0][0][1], f"/data/tools/official_client_capture/{plan.CANDIDATE_SCRIPT}")


class GatewayProbeTests(unittest.TestCase):
    """网关只读快照：只读命令、不记秘密；就绪核对；比较字段与不比较的字段。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.manifest = {"configuration": dict(CANDIDATE_CONFIGURATION)}
        self.commands: list[list[str]] = []

    def fake_command(self, argv, *, operation, deadline=None, heartbeat=None, check=True, timeout_seconds=30):
        del operation, deadline, heartbeat, timeout_seconds
        argv = [str(item) for item in argv]
        self.commands.append(argv)

        def done(stdout: str = "", code: int = 0):
            return subprocess.CompletedProcess(argv, code, stdout, "")

        if argv[:3] == ["docker", "inspect", "--format"]:
            template, target = argv[3], argv[4]
            if template == candidate_capture.SERVICE_INSPECT_TEMPLATE:
                return done(json.dumps({"id": "a" * 64, "image": SERVICE_IMAGE_ID, "running": True, "started_at": "t0"}))
            if template == candidate_capture.ENV_TEMPLATE:
                if target == POSTGRES:
                    return done("POSTGRES_USER=sub2api\nPOSTGRES_DB=sub2api\nPOSTGRES_PASSWORD=secret-pg\n")
                return done("SERVER_PORT=18080\nJWT_SECRET=secret-jwt\n")
            if template == "{{json .State.Running}}":
                return done("true\n")
        if argv[:2] == ["docker", "exec"] and argv[3] == "sha256sum":
            return done(("1" if argv[4] == "/etc/hosts" else "2") * 64 + "  " + argv[4] + "\n")
        if argv[:2] == ["docker", "exec"] and argv[3] == "test":
            return done(code=1)
        if argv[:2] == ["docker", "exec"] and argv[3] == "psql":
            query = argv[-1]
            self.assertTrue(query.startswith("select"), query)
            for marker, answer in (
                ("platform ||", "anthropic|oauth|NULL|live"), ("proxy_id::text", "3|NULL"), ("temp_unschedulable_until", "|"),
                ("from api_keys", "active|5|live"), ("from proxies", "0"),
            ):
                if marker in query:
                    return done(answer + "\n")
        raise AssertionError(f"未登记的命令：{argv}")

    def test_snapshot_is_read_only_and_secret_free(self) -> None:
        with mock.patch.object(capture, "_run_probe_command", self.fake_command):
            snapshot = candidate_capture.probe_gateway_environment(self.manifest, self.data, "before")
        self.assertEqual(snapshot["schema_version"], candidate_capture.GATEWAY_ENVIRONMENT_SCHEMA)
        self.assertEqual(snapshot["service"]["port"], 18080)
        self.assertEqual((snapshot["account"]["shape"], snapshot["account"]["proxy_state"]), ("anthropic|oauth|NULL|live", "3|NULL"))
        self.assertEqual(candidate_capture.readiness_failures(snapshot), [])
        text = json.dumps(snapshot)
        self.assertNotIn("secret-", text)
        mutating = {"cp", "restart", "stop", "start", "rm"}
        self.assertFalse([argv for argv in self.commands if argv[1] in mutating])
        self.assertEqual(candidate_capture.gateway_coordinates(candidate_capture.candidate_variables(self.manifest), service_port=18080)["base_url"],
                         "http://sub2apiplus:18080")

    def test_readiness_and_drift(self) -> None:
        dirty = json.loads(json.dumps(BASE_GATEWAY))
        dirty["service"]["custom_ca_present"] = True
        dirty["residual_candidate_proxies"] = 2
        dirty["runtime_residue"] = ["cid-v4-x"]
        dirty["account"]["shape"] = "anthropic|oauth|5|live"
        dirty["api_key"]["shape"] = "disabled|5|live"
        self.assertEqual(
            candidate_capture.readiness_failures(dirty),
            ["custom-ca-present", "residual-candidate-proxy", "runtime-residue", "account-shape", "api-key-shape"],
        )
        after = json.loads(json.dumps(BASE_GATEWAY))
        # 每步装／撤临时 CA 各重启服务一次：启动时刻变化、调度门摘要变化都不算漂移。
        after["service"]["started_at"] = "t9"
        after["account"]["gate_sha256"] = "9" * 64
        self.assertEqual(candidate_capture.gateway_drift(BASE_GATEWAY, after), [])
        after["service"]["hosts_sha256"] = "f" * 64
        after["account"]["proxy_state"] = "77|NULL"
        self.assertEqual(candidate_capture.gateway_drift(BASE_GATEWAY, after), ["service.hosts_sha256", "account.proxy_state"])

    def test_runtime_residue_lists_host_directory(self) -> None:
        runtime = self.data.joinpath(*candidate_capture.RUNTIME_DIR_PARTS)
        runtime.mkdir(parents=True)
        (runtime / ".capture.lock").write_text("", encoding="utf-8")
        with mock.patch.object(capture, "_run_probe_command", self.fake_command):
            self.assertEqual(candidate_capture.probe_gateway_environment(self.manifest, self.data, "after")["runtime_residue"], [])
            (runtime / "run-x").mkdir()
            snapshot = candidate_capture.probe_gateway_environment(self.manifest, self.data, "after")
        self.assertEqual(snapshot["runtime_residue"], ["run-x"])
        self.assertIn("runtime-residue", candidate_capture.readiness_failures(snapshot))


class RestorationCheckTests(unittest.TestCase):
    """每步恢复报告核对：通过、前检拒绝、报告缺失、恢复失败、退出码不符、恢复项不等、身份不符。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve()
        self.layout = capture.CaptureLayout(
            host_data_root=self.data, capture_root=PurePosixPath(str(self.data)), capture_container=CAPTURE,
            capture_tool_root=PurePosixPath(str(self.data / "tools" / "official_client_capture")), host_tool_root=capture_tests.TOOL_ROOT,
            runtime_image=capture_tests.IMAGE, synthetic_container=SYNTHETIC,
        )
        self.manifest = {"configuration": dict(CANDIDATE_CONFIGURATION)}
        self.output = f"{self.data}/runs/claude-candidate/cid-c1/{LIVE_JOB}/v4-native-tls-baseline"
        self.report = self.data / "runs" / "claude-candidate" / "cid-c1" / LIVE_JOB / "candidate-gateway" / "v4-native-tls-baseline.json"

    def row(self, code: int | None) -> dict[str, Any]:
        argv = ["bash", "script.sh", "docker", "exec", CAPTURE, "python3", "/x/claude_fw_e_relay.py", "capture", "--execute",
                "--run-id", "cid-c1-v4-native-tls-baseline", "--probe-id", "v4-native-tls-baseline", "--output-root", self.output]
        return {"argv": argv, "return_code": code, "probe_id": "v4-native-tls-baseline"}

    def write(self, **changes: Any) -> None:
        report = {
            "schema_version": candidate_capture.RESTORATION_REPORT_SCHEMA, "status": "complete", "run_id": "cid-c1-v4-native-tls-baseline",
            "probe_id": "v4-native-tls-baseline", "account_id": 7, "api_key_id": 9,
            **{name: True for name in candidate_capture.RESTORATION_EQUAL_FIELDS},
        }
        report.update(changes)
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(json.dumps(report), encoding="utf-8")

    def check(self, code: int | None) -> dict[str, Any]:
        result = candidate_capture.restoration_check(self.row(code), self.layout, self.manifest)
        assert result is not None
        return result

    def test_outcomes(self) -> None:
        self.assertEqual(self.check(2)["status"], "not_started")
        self.assertEqual(self.check(124)["failure_code"], "report-missing")
        self.write()
        passed = self.check(0)
        self.assertEqual((passed["status"], passed["report"]["path"]), ("passed", str(PurePosixPath(self.output).parent / "candidate-gateway" / "v4-native-tls-baseline.json")))
        self.assertEqual(passed["report"]["sha256"], claude_upgrade.file_sha256(self.report))
        self.assertEqual(self.check(3)["failure_code"], "report-exit-mismatch")
        for changes, code, failure in (
            ({"status": "capture_failed"}, 3, None),
            ({"status": "restoration_failed"}, 97, "restoration-failed"),
            ({"hosts_sha256_equal": False}, 0, "restoration-unverified"),
            ({"account_id": 8}, 0, "report-identity"),
            ({"schema_version": "other"}, 0, "report-schema"),
            ({"status": "unknown"}, 0, "report-status"),
        ):
            with self.subTest(changes=changes):
                self.write(**changes)
                result = self.check(code)
                self.assertEqual(result.get("failure_code"), failure)
                self.assertEqual(result["status"], "passed" if failure is None else "failed")
        self.assertIsNone(candidate_capture.restoration_check({"argv": [], "return_code": None}, self.layout, self.manifest))


class OverrideIdentityTests(CandidateFixture):
    """两个服务面成员：运行坐标覆盖（没有收据原样返回、有即失败关闭）与候选 attempt 身份重验。"""

    def fake_docker(self, image_id: str, digests: list[str]):
        def run(argv, *, operation, deadline=None, heartbeat=None, check=True, timeout_seconds=30):
            del operation, deadline, heartbeat, check, timeout_seconds
            if argv[:4] == ["docker", "inspect", "--format", "{{json .Image}}"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(image_id), "")
            if argv[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(digests), "")
            raise AssertionError(argv)

        return run

    def test_runtime_override_member(self) -> None:
        campaign = self.write_candidate_campaign()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.assertIs(claude_upgrade._apply_candidate_runtime_override(campaign, manifest, CANDIDATE), manifest)
        self.assertIs(claude_upgrade._apply_candidate_runtime_override(campaign, manifest, None), manifest)
        path = candidate_capture.runtime_override_path(campaign, CANDIDATE)
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "没有登记覆盖的命令"):
            claude_upgrade._apply_candidate_runtime_override(campaign, manifest, CANDIDATE)

    def test_attempt_identity_member(self) -> None:
        campaign = self.write_candidate_campaign()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        admission = self.fake_admission(campaign, manifest, CANDIDATE)
        with self.patched():
            layout = capture._capture_layout(campaign, manifest)
        identity = candidate_capture.attempt_identity(manifest, layout, capture.MODE_FORMAL, admission)
        self.assertEqual(identity["schema_version"], candidate_capture.CANDIDATE_IDENTITY_SCHEMA)
        good = self.fake_docker(SERVICE_IMAGE_ID, [SERVICE_IMAGE])
        with mock.patch.object(capture, "_run_probe_command", good):
            claude_upgrade._verify_candidate_attempt_identity(manifest, identity)
        cases = {
            "镜像不是候选构建": (self.fake_docker("sha256:" + "0" * 64, [SERVICE_IMAGE]), identity, "不是候选构建的镜像"),
            "引用不在 RepoDigests": (self.fake_docker(SERVICE_IMAGE_ID, ["other@sha256:" + "1" * 64]), identity, "RepoDigests"),
            "用途不一致": (good, {**identity, "candidate_purpose": "production_replacement"}, "用途"),
            "演练身份": (good, {**identity, "mode": capture.MODE_REHEARSAL}, "正式候选 attempt"),
            "服务容器换了": (good, {**identity, "service_container": "other"}, "服务容器"),
        }
        for label, (runner, value, message) in cases.items():
            with self.subTest(label=label), mock.patch.object(capture, "_run_probe_command", runner):
                with self.assertRaisesRegex(ConfigurationError, message):
                    claude_upgrade._verify_candidate_attempt_identity(manifest, value)
        (self.source / "backend" / "main.go").write_text("package main // changed\n", encoding="utf-8")
        with mock.patch.object(capture, "_run_probe_command", good), self.assertRaisesRegex(ConfigurationError, "源码树"):
            claude_upgrade._verify_candidate_attempt_identity(manifest, identity)


class SelectorObservationTests(CandidateFixture):
    def test_observation_reads_frozen_catalog(self) -> None:
        campaign = self.write_candidate_campaign()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        observed = candidate_capture.observe_selectors(campaign, manifest, required=True)
        assert observed is not None
        self.assertEqual((observed["schema_version"], observed["kind"]), (candidate_capture.SELECTOR_OBSERVATION_SCHEMA, "read_only"))
        self.assertEqual(observed["source"]["path"], "inputs/active-release-catalog.json")
        self.assertEqual(observed["source"]["sha256"], manifest["inputs"]["active_release_catalog"]["sha256"])
        self.assertEqual(observed["production_active_release"]["release_sha256"], "1" * 64)
        self.assertEqual(observed["selectors"]["production_rollback"]["kind"], "operational-deployment")
        self.assertIsNone(observed["production_rollback_release"])
        bare = self.write_candidate_campaign(mode="preflight_only", campaign_id="claude-code-2_1_285-t2", catalog=False)
        bare_manifest = claude_upgrade.load_campaign_manifest(bare)
        self.assertIsNone(candidate_capture.observe_selectors(bare, bare_manifest, required=False))
        with self.assertRaisesRegex(ConfigurationError, "Active Release 目录"):
            candidate_capture.observe_selectors(bare, bare_manifest, required=True)
        (campaign / "inputs" / "active-release-catalog.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "摘要漂移"):
            candidate_capture.observe_selectors(campaign, manifest, required=True)


class FormalCandidateRunTests(CandidateFixture):
    """正式候选 run：attempt 字段与 schema、每步恢复报告、网关前后快照、selector 观察、运行条件与候选读侧。"""

    def test_formal_run_writes_candidate_attempt(self) -> None:
        campaign = self.write_candidate_campaign()
        code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual((code, result.get("status")), (2, "awaiting_receipts"), stderr)
        self.assertEqual(result["candidate_id"], CANDIDATE)
        attempt_root, attempt = self.load_attempt(campaign, result["attempt_id"])
        self.assertEqual(attempt_root.parent.parent, campaign / "candidates" / CANDIDATE)
        self.assertEqual((attempt["phase"], attempt["candidate_id"], attempt["candidate_purpose"]), ("candidate", CANDIDATE, "validation_only"))
        self.assertEqual(attempt["identity"]["schema_version"], candidate_capture.CANDIDATE_IDENTITY_SCHEMA)
        self.assertEqual(attempt["identity"]["image_id"], SERVICE_IMAGE_ID)
        self.assertEqual(attempt["gateway"]["base_url"], "http://sub2apiplus:8080")
        self.assertEqual((attempt["gateway"]["account_id"], attempt["gateway"]["api_key_id"], attempt["gateway"]["relay_port"]), (7, 9, 18443))
        environment = attempt["gateway_environment"]
        self.assertEqual(environment["schema_version"], candidate_capture.GATEWAY_ENVIRONMENT_SCHEMA)
        self.assertEqual((environment["before"]["path"], environment["after"]["path"]), ("environment/gateway-before.json", "environment/gateway-after.json"))
        self.assertEqual(environment["drifted_fields"], [])
        self.assertEqual(attempt["gateway_recovery"]["status"], "passed")
        # 共享的候选恢复准入读 environment.restoration_report（Codex 同名）：Claude 绑定网关隔离恢复核验文件。
        self.assertEqual(attempt["environment"]["restoration_report"], attempt["gateway_recovery"]["file"])
        recovery = json.loads((attempt_root / "evidence" / attempt["gateway_recovery"]["file"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual((recovery["schema_version"], recovery["environment_equal"]), (candidate_capture.GATEWAY_RECOVERY_SCHEMA, True))
        self.assertEqual(len(recovery["restoration_checks"]), 5)
        self.assertEqual(attempt["selector_observed"]["path"], "environment/selector-observed.json")
        # 与 B4-02 证据侧约定的 candidate_gateway：七个字段，三组运行证据条目都是 {id, facts}、按 id 排序、全部通过。
        facts = attempt["candidate_gateway"]
        self.assertEqual(set(facts), candidate_capture.CANDIDATE_GATEWAY_FIELDS)
        self.assertEqual(facts["base_url"], attempt["gateway"]["base_url"])
        self.assertEqual(set(facts["host"]), {"host_id", "architecture"})
        self.assertEqual(set(facts["run_conditions"]), set(candidate_capture.RUN_CONDITION_DIGEST_FIELDS))
        for name in ("environment_before", "environment_after", "recovery_verification"):
            ids = [entry["id"] for entry in facts[name]]
            self.assertEqual(ids, sorted(set(ids)))
            self.assertTrue(all(set(entry) == {"id", "facts"} and entry["facts"]["result"] == "pass" for entry in facts[name]), name)
        self.assertEqual([entry["id"] for entry in facts["environment_before"]], ["capture-containers", "gateway"])
        self.assertEqual(len([entry for entry in facts["recovery_verification"] if entry["id"].startswith("gateway-restoration-")]), 5)
        # selector 前态快照是 Store 的 runtime_catalog_snapshot 对象 payload（按 Store 合同校验）；观察原文另存文件。
        snapshot = contracts._validate_runtime_catalog_snapshot(facts["selector_before_snapshot"])
        self.assertEqual(snapshot["catalog_digest"], snapshot["source_ref"]["sha256"])
        self.assertEqual(snapshot["source_ref"]["path"], "inputs/active-release-catalog.json")
        self.assertEqual((snapshot["production_active_ref"], snapshot["production_rollback_ref"]), (None, None))
        self.assertEqual(attempt["next_gate"], "capture-candidate seal（B4-02）")
        for job in attempt["results"]:
            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["evidence_roots"], [f"{self.data}/runs/claude-candidate/claude-code-2_1_285-t1-{CANDIDATE}/{job['id']}"])
            self.assertEqual(len(job["relay_checks"]), len(job["steps"]))
            for relay, restoration, step in zip(job["relay_checks"], job["restoration_checks"], job["steps"], strict=True):
                self.assertEqual((relay["status"], restoration["status"]), ("passed", "passed"))
                self.assertEqual(set(restoration), {"probe_id", "status", "report"})
                self.assertEqual(set(restoration["report"]), {"path", "sha256"})
                self.assertEqual({"path", "sha256"}, set(relay["manifest"]))
                self.assertEqual(restoration["probe_id"], step["probe_id"])
                self.assertTrue(restoration["report"]["path"].startswith(job["evidence_roots"][0] + "/candidate-gateway/"))
                self.assertEqual(step["host_runtime_receipt"]["container_name"], CAPTURE)
                self.assertEqual(step["argv"][:2], ["bash", f"{self.tool_copy}/{plan.CANDIDATE_SCRIPT}"])
        # 与 B4-02 证据侧的读取接缝对齐：候选网关上下文与恢复报告核对行按封存层的读法通过。
        from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal

        context = candidate_seal._attempt_candidate_context(attempt)
        self.assertEqual(context["run_conditions"], facts["run_conditions"])
        with self.candidate_patched():
            manifest = claude_upgrade.load_campaign_manifest(campaign)
            layout = capture._capture_layout(campaign, manifest)
            jobs = {job.job_id: job for job in claude_upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=CANDIDATE)}
            for row in attempt["results"]:
                job_root = capture._host_path(row["evidence_roots"][0], layout)
                candidate_seal._verify_restoration_checks(jobs[row["id"]], row, layout, job_root)
        calls = self.candidate_calls()
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call["gate"] == plan.CANDIDATE_CAPTURE_GATE and call["target"] == CAPTURE and not call["dry_run"] for call in calls))
        self.assertEqual({call["trigger"] for call in calls}, {"client", "gateway-account-refresh"})
        self.assertEqual({call["container"] for call in self.relay_calls()}, {CAPTURE})
        ledger = self.supervisor_ledger(campaign)
        self.assertIn(f'"job:{REFRESH_JOB}:step-1:attempt-1"', ledger)
        self.assertNotIn("synthetic-job:", ledger)
        # 候选读侧：等待封存即活跃；再跑在预约前被拒；官方阶段读侧不受影响。
        with self.candidate_patched():
            self.assertEqual(claude_upgrade._active_unsealed_attempts(campaign, "candidate"), [f"{CANDIDATE}:{result['attempt_id']}"])
            self.assertEqual(claude_upgrade._active_unsealed_attempts(campaign, "official"), [])
            replayed_root, replayed = claude_upgrade._load_capture_attempt(campaign, "candidate", CANDIDATE, result["attempt_id"])
            self.assertEqual(replayed["attempt_digest"], attempt["attempt_digest"])
        code, _again, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual(code, 1)
        self.assertIn("未封存", stderr)

    def test_restoration_failure_contaminates_and_stops(self) -> None:
        campaign = self.write_candidate_campaign()
        self.set_candidate_control({"restoration_fail": ["v4-oauth-refresh"]})
        code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual((code, result.get("status")), (2, "environment_contaminated"), stderr)
        _root, attempt = self.load_attempt(campaign, result["attempt_id"])
        failed = [job for job in attempt["results"] if job.get("environment_contaminated")]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["restoration_checks"][0]["failure_code"], "restoration-failed")
        # 污染即停：受污染的 Job 不补跑，后续 Job 不再执行。
        self.assertEqual(len(attempt["results"]), [job["id"] for job in attempt["results"]].index(failed[0]["id"]) + 1)
        self.assertEqual(attempt["gateway_recovery"]["status"], "failed")
        self.assertIn(
            (f"job.{failed[0]['id']}.restoration.v4-oauth-refresh", "restoration-failed"),
            [(item["check_id"], item["failure_code"]) for item in attempt["failure_observations"]],
        )
        marker = json.loads((campaign / capture.CONTAMINATION_MARKER).read_text(encoding="utf-8"))
        self.assertEqual((marker["phase"], marker["candidate_id"]), ("candidate", CANDIDATE))

    def test_missing_report_and_gateway_drift_contaminate(self) -> None:
        for label, control, expected in (
            ("报告缺失", {"no_report": ["v4-oauth-refresh"]}, "report-missing"),
            ("网关漂移", {"gateway_drift": ["v4-oauth-refresh"]}, None),
        ):
            with self.subTest(label=label):
                campaign = self.write_candidate_campaign(campaign_id=f"claude-code-2_1_285-{'m' if expected else 'd'}")
                self.gateway_file.write_text(json.dumps(BASE_GATEWAY), encoding="utf-8")
                self.set_candidate_control(control)
                code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
                self.assertEqual(result.get("status"), "environment_contaminated", stderr)
                _root, attempt = self.load_attempt(campaign, result["attempt_id"])
                if expected:
                    codes = {item.get("failure_code") for job in attempt["results"] for item in job.get("restoration_checks") or []}
                    self.assertIn(expected, codes)
                else:
                    self.assertEqual(attempt["gateway_environment"]["drifted_fields"], ["residual_candidate_proxies"])
                    self.assertIn(
                        ("attempt.gateway.residual_candidate_proxies", "drifted"),
                        [(item["check_id"], item["failure_code"]) for item in attempt["failure_observations"]],
                    )

    def test_precheck_refusal_is_not_contamination_and_blocks_rerun(self) -> None:
        campaign = self.write_candidate_campaign()
        self.set_candidate_control({"precheck_fail": ["v4-oauth-refresh"]})
        with mock.patch.object(claude_upgrade, "JOB_RETRY_LIMIT", 0):
            code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual(result.get("status"), "failed", stderr)
        _root, attempt = self.load_attempt(campaign, result["attempt_id"])
        refresh = next(job for job in attempt["results"] if job["id"] == REFRESH_JOB)
        self.assertEqual(refresh["restoration_checks"][0]["status"], "not_started")
        self.assertFalse(refresh["environment_contaminated"])
        self.assertEqual(attempt["next_gate"], "候选续跑（B4-01c）")
        self.assertFalse((campaign / capture.CONTAMINATION_MARKER).exists())
        # 失败 attempt 只能经候选续跑（B4-01c）处理：直接再跑在预约前拒绝。
        self.set_candidate_control({})
        code, _again, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual(code, 1)
        self.assertIn("B4-01c", stderr)

    def test_gates_refuse_before_reservation(self) -> None:
        campaign = self.write_candidate_campaign()
        bare = self.write_candidate_campaign(campaign_id="claude-code-2_1_285-t2", catalog=False)
        dirty = json.loads(json.dumps(BASE_GATEWAY))
        dirty["residual_candidate_proxies"] = 1
        cases = [
            ("缺 candidate-id", campaign, ["--acknowledge-live-requests"], None, "--candidate-id"),
            ("缺确认", campaign, [], None, "--acknowledge-live-requests"),
            ("网关未就绪", campaign, ["--acknowledge-live-requests"], dirty, "residual-candidate-proxy"),
            ("没有冻结 Active 目录", bare, ["--acknowledge-live-requests"], None, "Active Release 目录"),
        ]
        for label, target, extra, gateway, message in cases:
            with self.subTest(label=label):
                self.gateway_file.write_text(json.dumps(gateway or BASE_GATEWAY), encoding="utf-8")
                with self.candidate_patched():
                    argv = ["capture-candidate", "run", "--campaign-dir", str(target), *extra]
                    if label != "缺 candidate-id":
                        argv += ["--candidate-id", CANDIDATE]
                    code, _result, stderr = self.run_main(argv)
                self.assertEqual(code, 1, stderr)
                self.assertIn(message, stderr)
                self.assertFalse((target / "candidates").exists())
        with self.candidate_patched(), mock.patch.dict(os.environ, {"ADMIN_BEARER_TOKEN_FILE": ""}):
            code, _result, stderr = self.run_main(
                ["capture-candidate", "run", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, "--acknowledge-live-requests"]
            )
        self.assertEqual(code, 1)
        self.assertIn("管理 token", stderr)
        self.assertFalse((campaign / "candidates").exists())


class CandidateRehearsalTests(CandidateFixture):
    """零请求演练：preflight_only Campaign，宿主脚本替身直接执行 dry-run，复核通过，宿主与网关零改动。"""

    def test_zero_request_candidate_rehearsal(self) -> None:
        campaign = self.write_candidate_campaign(mode="preflight_only", catalog=False)
        code, result, stderr = self.run_candidate(campaign, "--zero-request-rehearsal", "--job-id", LIVE_JOB)
        self.assertEqual((code, result.get("status")), (0, "rehearsal_passed"), stderr)
        with self.candidate_patched():
            verdict = capture.verify_rehearsal_attempt(campaign, result["attempt_id"], CANDIDATE)
        self.assertEqual(verdict["verdict"], "passed", [item for item in verdict["checks"] if not item["passed"]])
        self.assertTrue({"gateway-environment-stable", "gateway-untouched", f"job:{LIVE_JOB}:container-routing"} <= {item["check"] for item in verdict["checks"]})
        calls = self.candidate_calls()
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call["dry_run"] for call in calls))
        self.assertTrue(all(call["dry_run"] and not call["acknowledgements"] for call in self.relay_calls()))
        self.assertFalse((self.data / "runs" / "claude-candidate").exists())
        attempt_root = campaign / "rehearsal" / "candidates" / CANDIDATE / "attempts" / result["attempt_id"]
        attempt = json.loads((attempt_root / "attempt.json").read_text(encoding="utf-8"))
        self.assertIsNone(attempt["identity"]["build_receipt"])
        self.assertIsNone(attempt["selector_observed"])
        self.assertEqual([check["status"] for check in attempt["results"][0]["relay_checks"]], ["passed"] * 4)
        self.assertEqual(attempt["results"][0]["restoration_checks"], [])
        self.assertFalse((campaign / "candidates").exists())
        # 演练只认 preflight_only；正式 Campaign 上的演练在预约前拒绝。
        formal = self.write_candidate_campaign(campaign_id="claude-code-2_1_285-t2")
        code, _result, stderr = self.run_candidate(formal, "--zero-request-rehearsal", "--job-id", LIVE_JOB)
        self.assertEqual(code, 1)
        self.assertIn("preflight_only", stderr)


class FormalAdmissionTests(unittest.TestCase):
    """正式入口门在 VC-4 真实链上验收：record-candidate-build 到 candidate_sealed 之后放行，逐项破坏即拒绝。

    借 B3-04 的候选构建用例基类走到 candidate_sealed：在用例里临时派生并手工驱动（不在模块级派生，免得测试收集把基类原有
    用例再跑一遍）。
    """

    def test_admission_on_real_vc4_chain(self) -> None:
        from tools.official_client_capture.tests import test_claude_upgrade_candidate as candidate_tests

        class AdmissionChain(candidate_tests.RecordCandidateBuildTests):
            run_chain = _run_admission_chain

        AdmissionChain.setUpClass()
        try:
            case = AdmissionChain("run_chain")
            case.setUp()
            try:
                case.run_chain()
            finally:
                case.doCleanups()
        finally:
            AdmissionChain.tearDownClass()


def _run_admission_chain(self) -> None:
        from tools.official_client_capture.tests import test_claude_upgrade_candidate as candidate_tests

        campaign, fixture = self.fixture()
        code, result, stderr = self.record(fixture)
        self.assertEqual((code, result["status"]), (0, "complete"), stderr)
        manifest = dict(claude_upgrade.load_campaign_manifest(campaign))
        manifest["configuration"] = {**manifest["configuration"], **CANDIDATE_CONFIGURATION}
        receipt = json.loads((campaign / "candidates" / candidate_tests.CANDIDATE / "build-receipt.json").read_text(encoding="utf-8"))

        def docker(image_id: str, digests: list[str]):
            def run(argv, *, operation, deadline=None, heartbeat=None, check=True, timeout_seconds=30):
                del operation, deadline, heartbeat, check, timeout_seconds
                if argv[:4] == ["docker", "inspect", "--format", "{{json .Image}}"]:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(image_id), "")
                if argv[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(digests), "")
                raise AssertionError(argv)

            return run

        good = docker(receipt["image"]["image_id"], [receipt["image"]["reference"]])
        with mock.patch.object(capture, "_run_probe_command", good):
            admission = candidate_capture.formal_admission(campaign, manifest, candidate_tests.CANDIDATE)
        self.assertEqual(admission.facts["build_id"], receipt["build"]["build_id"])
        self.assertEqual(admission.facts["candidate_revision"], 1)
        self.assertEqual(admission.facts["candidate_frozen_ref"], receipt["store"]["candidate_frozen_ref"])
        self.assertEqual(admission.facts["source_tree_sha256"], receipt["source"]["tree_sha256"])
        self.assertTrue(admission.persona)
        with mock.patch.object(capture, "_run_probe_command", docker("sha256:" + "0" * 64, [])):
            with self.assertRaisesRegex(ConfigurationError, "不是候选构建的镜像"):
                candidate_capture.formal_admission(campaign, manifest, candidate_tests.CANDIDATE)
        with mock.patch.object(capture, "_run_probe_command", good):
            with self.assertRaisesRegex(ConfigurationError, "不属于当前 revision"):
                candidate_capture.formal_admission(campaign, manifest, candidate_tests.OTHER)
            with self.assertRaisesRegex(ConfigurationError, "候选变量"):
                candidate_capture.formal_admission(campaign, claude_upgrade.load_campaign_manifest(campaign), candidate_tests.CANDIDATE)
            result_path = candidate_capture.legacy_stage_result_path(campaign, candidate_tests.CANDIDATE)
            result_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "已经封存"):
                candidate_capture.formal_admission(campaign, manifest, candidate_tests.CANDIDATE)
            result_path.unlink()
            source_file = next(path for path in Path(receipt["source"]["root"]).rglob("*") if path.is_file() and ".git" not in path.parts)
            candidate_tests.build_fixture.make_writable(source_file)
            source_file.write_bytes(source_file.read_bytes() + b"\n")
            with self.assertRaisesRegex(ConfigurationError, "源码树"):
                candidate_capture.formal_admission(campaign, manifest, candidate_tests.CANDIDATE)


if __name__ == "__main__":
    unittest.main()
