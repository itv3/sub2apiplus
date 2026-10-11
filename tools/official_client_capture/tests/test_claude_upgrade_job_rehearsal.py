"""Claude 完整 Job 离线演练（Claude 升级控制面设计 B2-10）：共享收据模块按 ``--client claude`` 经适配器取 Claude 画像。

不起 Docker、不联网、不读凭据：
* 场景清单取 B2-01 冻结的真实 2.1.285 清单（14 个 Job、112 步：主模型 12 个 Job 79 步，加 B2-07 补充项的两个非主模型能力 Job 33 步；含断网容器里的合成 Job），Campaign 按 B2-06 的总计划形状
  写在临时数据根 ``<数据根>/evidence/campaigns`` 下；
* 数据根下的 ``tools`` 指向仓库的 ``tools``：宿主执行副本与受管树是同一份，「容器内」执行副本经 ``docker`` 替身在本机读同一
  路径（测试里容器坐标即宿主坐标）；
* ``docker`` 替身：``exec <容器> <命令…>`` 记下容器后在本机执行命令，``inspect`` 回两个采集容器在运行；
* 目标客户端三处核验（服务面 ``_verify_official_binaries``）换成按真实返回形状构造的替身；
* 失败生命周期探针（B2-11 第二部分）换成按 Claude 合同与当前登记坐标构造的合规事实（``test_claude_upgrade_failure_lifecycle``
  的 ``fake_probe``）；真实父监督器与 worker 的演练在那个模块的端到端用例里跑。

覆盖：执行合同（真实清单、拒绝项、合同名与客户端认法）；完整演练零请求通过、封存与逐字重放、Formal 与恢复兼容、增量复用；
合成 Job 的步骤只在断网容器里探；失败只重跑失败的 Job；facts 被改的各类拒绝；命令行 ``--client``；Codex 合同接口与画像取值
就是改造前的原函数与原值。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_failure_lifecycle as lifecycle
from tools.official_client_capture import claude_upgrade_job_rehearsal as claude_profile
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as receipt
from tools.official_client_capture import upgrade_client_adapter as uca
from tools.official_client_capture.tests.claude_job_rehearsal_v1_fixture import copy_formed_v1
from tools.official_client_capture.tests.test_claude_upgrade_failure_lifecycle import fake_probe

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
TARGET = "2.1.285"
BASELINE = "2.1.226"
TARGET_SHA = "a" * 64
IMAGE = "oauth-egress-capture-capture-cli@sha256:" + "b" * 64
IMAGE_ID = "sha256:" + "d" * 64
CAPTURE = "capture-cli"
SYNTHETIC = "capture-cli-synthetic"
CONTAINER_IDS = {CAPTURE: "c" * 64, SYNTHETIC: "9" * 64}
SYNTHETIC_JOBS = {"official-synthetic-retry", "official-synthetic-stream", "official-synthetic-oauth-refresh"}
# 测试环境的容器命令表：CI 宿主不保证装了 tcpdump 等；生产表由 test_profile_constants 单独钉住。
TEST_CONTAINER_COMMANDS = ("python3",)

FAKE_DOCKER = r'''#!{python}
"""docker 的离线替身：exec 在本机执行容器内命令（记下目标容器），inspect 回两个采集容器在运行。"""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if args and args[0] == "inspect":
    ids = json.loads(os.environ["FAKE_CONTAINER_IDS"])
    print(json.dumps([{{"Name": "/" + name, "Id": ids[name], "Image": os.environ["FAKE_IMAGE_ID"], "State": {{"Running": True}}}}
                      for name in args[1:]]))
    sys.exit(0)
if len(args) < 3 or args[0] != "exec":
    print("fake docker 只接受 exec／inspect", file=sys.stderr)
    sys.exit(125)
container, command = args[1], args[2:]
failing = os.environ.get("FAKE_FAIL_STEP_CONTAINER")
if failing == container and any("container_python_script" in item for item in command):
    print("relay missing in " + container, file=sys.stderr)
    sys.exit(1)
os.execvp(command[0], command)
'''


def _baseline_rules() -> tuple[str, ...]:
    return plan.load_baseline_rules(plan.baseline_rule_manifest_path(BASELINE), BASELINE)


def _scenario() -> dict:
    return json.loads(plan.scenario_manifest_path(TARGET).read_text(encoding="utf-8"))


class ClaudeRehearsalFixture(unittest.TestCase):
    """临时数据根（``tools`` 指向仓库）、docker 替身、Claude 预检 Campaign；Docker 与二进制边界都在 ``patched()`` 里替换。"""

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
        (self.data / "tools").symlink_to(REPOSITORY_ROOT / "tools", target_is_directory=True)
        self.tool_root = self.data / "tools" / "official_client_capture"
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        docker.chmod(0o700)
        self.calls_file = self.root / "docker-calls.jsonl"
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.binary_calls = 0
        self.lifecycle_calls = 0

    def write_campaign(self, *, campaign_id: str = "claude-code-2_1_285-jr1", configuration: dict | None = None) -> Path:
        campaign = self.data / "evidence" / "campaigns" / campaign_id
        (campaign / "inputs").mkdir(parents=True, mode=0o700)
        scenario = campaign / "inputs" / "target-scenarios.json"
        scenario.write_bytes(plan.scenario_manifest_path(TARGET).read_bytes())
        scenario.chmod(0o600)
        settings = {
            "platform": "linux/arm64",
            "runtime_image": IMAGE,
            "capture_root": str(self.data),
            "capture_tool_root": str(self.tool_root),
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
            "created_at_utc": "2026-10-07T00:00:00Z", "campaign_mode": "preflight_only", "campaign_purpose": "validation_only",
            "baseline_version": BASELINE, "target_version": TARGET, "target_sha256": TARGET_SHA,
            "official_identity": {"npm": {}, "gcs": {}, "binary_sha256": TARGET_SHA, "binary_equivalence": "npm=gcs"},
            "baseline_identity": {}, "tool_identity": self.tool_identity, "control_receipts": {},
            "inputs": {"target_scenarios": {"path": "inputs/target-scenarios.json", "sha256": codex_upgrade.file_sha256(scenario)}},
            "configuration": settings, "required_rules": list(_baseline_rules()), "coverage_plan": {},
            "jobs": [], "vc_control": {}, "predecessor": None,
        }
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        (campaign / "campaign.json").write_bytes(raw)
        (campaign / "campaign.json").chmod(0o600)
        (campaign / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
        return campaign

    def fake_binaries(self, manifest, *, deadline=None, heartbeat=None):
        """按 ``claude-upgrade-official-binary-verification/v2`` 的真实返回形状构造的三处核验结果。"""

        del deadline, heartbeat
        self.binary_calls += 1
        configuration = manifest["configuration"]
        binary = {"path": configuration["claude_bin"], "sha256": manifest["target_sha256"], "version": manifest["target_version"]}
        return {
            "schema_version": capture.BINARY_VERIFICATION_SCHEMA,
            "target_version": manifest["target_version"],
            "target_sha256": manifest["target_sha256"],
            "container_binary": dict(binary),
            "container_probe": {"process_exit_code": 0},
            "synthetic_container": {
                "name": configuration["synthetic_capture_container"], "container_id": CONTAINER_IDS[SYNTHETIC], "image_id": IMAGE_ID,
                "network_mode": "none", "network_names": ["none"], "reference_container": CAPTURE,
                "reference_container_id": CONTAINER_IDS[CAPTURE], "shared_mounts_sha256": "e" * 64, "shared_mount_count": 3,
                "excluded_reference_mounts": [], "client_binary": dict(binary), "client_binary_probe": {"process_exit_code": 0},
            },
            "host_binary": {"path": f"/opt/claude-{manifest['target_version']}/claude", "sha256": manifest["target_sha256"]},
            "runtime_image": {"reference": configuration["runtime_image"], "image_id": IMAGE_ID},
            "verified_at_utc": "2026-10-07T00:00:00Z",
        }

    def fake_lifecycle(self, configuration):
        """失败生命周期探针的替身：记次数，返回按当前登记坐标构造、已过 ``validate_probe`` 的合规事实。"""

        self.lifecycle_calls += 1
        return fake_probe(configuration)

    @contextlib.contextmanager
    def patched(self, *, fail_step_container: str | None = None):
        runs = self.data / "runs"
        environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_DOCKER_CALLS": str(self.calls_file),
            "FAKE_CONTAINER_IDS": json.dumps(CONTAINER_IDS),
            "FAKE_IMAGE_ID": IMAGE_ID,
            "FAKE_FAIL_STEP_CONTAINER": fail_step_container or "",
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(claude_upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", runs))
            stack.enter_context(
                mock.patch.object(
                    claude_upgrade,
                    "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS",
                    (PurePosixPath(str(runs)), PurePosixPath("/root/oauth-capture/runs")),
                )
            )
            stack.enter_context(mock.patch.object(claude_upgrade, "_verify_official_binaries", self.fake_binaries))
            stack.enter_context(mock.patch.object(lifecycle, "capture_probe", self.fake_lifecycle))
            stack.enter_context(mock.patch.object(claude_profile, "CONTAINER_REQUIRED_COMMANDS", TEST_CONTAINER_COMMANDS))
            stack.enter_context(mock.patch.object(receipt.platform, "machine", return_value="aarch64"))
            stack.enter_context(mock.patch.object(receipt.platform, "system", return_value="linux"))
            stack.enter_context(mock.patch.dict(os.environ, environment))
            yield

    def docker_calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()]

    def collect(self, campaign: Path, output: str, **options) -> dict:
        return receipt.collect(self.evidence, output, campaign_dir=campaign, client="claude", **options)


class ClaudeContractTests(ClaudeRehearsalFixture):
    """执行合同：真实 2.1.285 清单、拒绝项、合同名与客户端认法。"""

    def manifest(self) -> dict:
        campaign = self.write_campaign()
        with self.patched():
            return claude_upgrade.load_campaign_manifest(campaign)

    def test_real_manifest_contract(self) -> None:
        manifest = self.manifest()
        scenario = _scenario()
        adapter = uca.adapter_for("claude")
        contract = adapter.build_job_contract(manifest, scenario, None)
        self.assertIs(adapter.job_rehearsal_profile, claude_profile)
        self.assertEqual((contract["schema_version"], contract["client"]), (claude_profile.EXECUTION_CONTRACT_SCHEMA, "claude"))
        self.assertEqual((contract["job_count"], contract["phase_counts"]), (14, {"official": 14}))
        self.assertEqual(sum(contract["step_counts"].values()), 112)
        self.assertTrue(SYNTHETIC_JOBS <= set(contract["job_ids"]))
        self.assertEqual(contract["evidence_labels_sha256"], receipt._sha256_file(plan.evidence_labels_path(TARGET)))
        self.assertEqual(contract["official_identity_sha256"], receipt._fingerprint(manifest["official_identity"]))
        self.assertEqual(
            (contract["tool_files_sha256"], contract["wire_producer_sha256"], contract["policy_sha256"]),
            (self.tool_identity["files_sha256"], self.tool_identity["wire_producer_sha256"], self.tool_identity["policy_sha256"]),
        )
        self.assertEqual(list(contract["configuration"]), list(claude_profile.EXECUTION_CONFIGURATION_FIELDS))
        self.assertNotIn("c2pa_job_identities", contract)
        self.assertEqual(claude_profile.validate_execution_contract(contract), contract)
        # 合同名认客户端：共享流程的合同摘要按 Claude 画像校验，Codex 校验器不认它。
        self.assertEqual(receipt.execution_contract_sha256(contract), receipt._fingerprint(contract))
        with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "字段不闭合"):
            receipt.validate_execution_contract(contract)
        self.assertEqual(
            receipt._contract_reuse_identity(contract), {field: contract[field] for field in claude_profile.CONTRACT_REUSE_FIELDS}
        )

    def test_contract_rejections(self) -> None:
        manifest = self.manifest()
        scenario = _scenario()
        relabelled = copy.deepcopy(scenario)
        relabelled["capture_jobs"][0]["id"] = "official-renamed"
        cases = {
            "extra_jobs": (manifest, scenario, {"jobs": []}, "extra_jobs"),
            "官方身份": (dict(manifest, official_identity={"binary_sha256": "0" * 64}), scenario, None, "binary_sha256"),
            "工具身份": (dict(manifest, tool_identity={"files_sha256": "1" * 64}), scenario, None, "tool_identity"),
            "配置空值": (dict(manifest, configuration=dict(manifest["configuration"], model="")), scenario, None, "model"),
            "suite": (dict(manifest, suite="nightly"), scenario, None, "suite"),
            "证据标签不覆盖": (manifest, relabelled, None, "证据标签"),
            "版本": (dict(manifest, target_version="2.1"), scenario, None, "target_version"),
        }
        for label, (current, payload, extra, message) in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(receipt.JobRehearsalReceiptError, message):
                claude_profile.build_job_contract(current, payload, extra)
        contract = claude_profile.build_job_contract(manifest, scenario, None)
        for label, change, message in (
            ("合同名", {"schema_version": receipt.EXECUTION_CONTRACT_SCHEMA}, "schema_version"),
            ("客户端", {"client": "codex"}, "schema_version"),
            ("摘要", {"evidence_labels_sha256": "x"}, "evidence_labels_sha256"),
            ("配置不闭合", {"configuration": {"model": "m"}}, "configuration"),
            ("Job 集", {"job_count": 11}, "Job 集"),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(receipt.JobRehearsalReceiptError, message):
                claude_profile.validate_execution_contract(dict(contract, **change))

    def test_documents_choose_the_client_by_schema(self) -> None:
        self.assertEqual(receipt._document_adapter(claude_profile.FACTS_SCHEMA).client, "claude")
        self.assertEqual(receipt._document_adapter(claude_profile.RECEIPT_SCHEMA).client, "claude")
        self.assertEqual(receipt._document_adapter(claude_profile.EXECUTION_CONTRACT_SCHEMA).client, "claude")
        for schema in (receipt.FACTS_SCHEMA, receipt.RECEIPT_SCHEMA, receipt.EXECUTION_CONTRACT_SCHEMA, "unknown/v1", None):
            with self.subTest(schema=schema):
                self.assertEqual(receipt._document_adapter(schema).client, "codex")
        with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "未知客户端"):
            receipt._adapter("kilo")

    def test_profile_constants(self) -> None:
        self.assertEqual(set(claude_profile.EXECUTION_CONFIGURATION_FIELDS), set(capture.CONFIGURATION_VARIABLES))
        self.assertEqual(claude_profile.TARGET_SCENARIO_INPUT, "target_scenarios")
        self.assertEqual(claude_profile.CONTAINER_REQUIRED_COMMANDS, ("openssl", "python3", "sha256sum", "tcpdump", "test"))
        self.assertEqual(claude_profile.HOST_REQUIRED_COMMANDS, ("docker", "python3"))
        self.assertEqual(set(claude_profile.LAUNCHER_CONTAINER_FIELDS.values()), set(claude_profile.CONTAINER_FIELDS))
        self.assertEqual(claude_profile.FORMAL_PROBE_DIGESTS, ("storage_probe_sha256", "failure_lifecycle_probe_sha256"),
                         "与 Codex 同一组 Formal 必需摘要（B2-11 第二部分补上失败生命周期探针）")
        self.assertEqual(
            {"schema_version", "tool", "tool_sha256", "version", "profile", "profile_sha256"}, set(claude_profile.rehearsal_producer())
        )


class ClaudeRehearsalTests(ClaudeRehearsalFixture):
    """完整演练：零请求通过、封存与逐字重放、Formal 与恢复兼容、增量复用与失败重跑。"""

    def test_isolated_preflight_uses_registered_execution_tree(self) -> None:
        """入口隔离根只存预检原件，实际四棵工具树仍取登记的数据根。"""

        original = self.write_campaign()
        campaign = self.data / "staging" / "entry-dryrun-layout" / "evidence" / "campaigns" / original.name
        campaign.parent.mkdir(parents=True)
        shutil.copytree(original, campaign)
        original_bytes = (campaign / "campaign.json").read_bytes()
        with self.patched():
            manifest = claude_upgrade.load_campaign_manifest(campaign)
            verify_root, execution_root, container_root = claude_profile.rehearsal_execution_roots(campaign, manifest)
            self.assertEqual(execution_root, self.tool_root)
            self.assertEqual(verify_root, self.data)
            self.assertEqual(container_root, str(self.tool_root))
            with self.assertRaisesRegex(capture.ConfigurationError, "runs 根不一致"):
                capture._capture_layout(campaign, manifest)
            facts = self.collect(campaign, "isolated-facts.json")
            sealed = receipt.finalize(self.evidence, "isolated-facts.json", "isolated-receipt.json", client="claude")
            self.assertEqual(receipt.replay(self.evidence, "isolated-receipt.json", client="claude"), sealed)
        self.assertEqual(facts["summary"]["status"], "passed")
        self.assertEqual(facts["preflight_campaign"]["path"], str(campaign))
        self.assertEqual((campaign / "campaign.json").read_bytes(), original_bytes)

    def test_isolated_preflight_layout_rejects_invalid_scope_and_configuration(self) -> None:
        """正式模式、外部根、过深目录、符号链接及非法容器布局均在探针前拒绝。"""

        original = self.write_campaign()
        valid = self.data / "staging" / "entry-dryrun-layout" / "evidence" / "campaigns" / original.name
        valid.mkdir(parents=True)
        external = self.root / "external" / "evidence" / "campaigns" / original.name
        external.mkdir(parents=True)
        deep = self.data / "staging" / "entry-dryrun-layout" / "nested" / "evidence" / "campaigns" / original.name
        deep.mkdir(parents=True)
        alias = self.data / "staging" / "alias"
        alias.symlink_to(valid.parents[2], target_is_directory=True)
        with self.patched():
            manifest = claude_upgrade.load_campaign_manifest(original)
            cases = (
                ("正式模式", valid, dict(manifest, campaign_mode="formal"), "preflight_only"),
                ("外部根", external, manifest, "登记数据根"),
                ("过深根", deep, manifest, "规范目录"),
                ("符号链接", alias / "evidence" / "campaigns" / original.name, manifest, "无符号链接"),
                ("别名", valid, dict(manifest, configuration=dict(manifest["configuration"], capture_root="/unknown")), "登记的容器"),
                ("工具逃逸", valid, dict(manifest, configuration=dict(manifest["configuration"], capture_tool_root="/capture/../tools/official_client_capture")), "安全绝对路径"),
                ("工具根", valid, dict(manifest, configuration=dict(manifest["configuration"], capture_tool_root="/capture/tools/other")), "结尾"),
                ("相同容器", valid, dict(manifest, configuration=dict(manifest["configuration"], synthetic_capture_container=CAPTURE)), "断网容器名"),
                ("可变镜像", valid, dict(manifest, configuration=dict(manifest["configuration"], runtime_image="image:latest")), "repository@sha256"),
            )
            for label, current, payload, message in cases:
                with self.subTest(case=label), self.assertRaisesRegex(receipt.JobRehearsalReceiptError, message):
                    claude_profile.rehearsal_execution_roots(current, payload)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual((self.binary_calls, self.lifecycle_calls), (0, 0))

    def test_full_rehearsal_passes_replays_and_is_formal_compatible(self) -> None:
        campaign = self.write_campaign()
        with self.patched():
            facts = self.collect(campaign, "facts.json")
            sealed = receipt.finalize(self.evidence, "facts.json", "receipt.json", client="claude")
            replayed = receipt.replay(self.evidence, "receipt.json", client="claude")
            manifest = claude_upgrade.load_campaign_manifest(campaign)
            contract = uca.adapter_for("claude").build_job_contract(manifest, _scenario(), None)
        self.assertEqual(facts["schema_version"], claude_profile.FACTS_SCHEMA)
        self.assertEqual(facts["summary"]["status"], "passed", facts["summary"])
        self.assertIs(facts["summary"]["live_requests_sent"], False)
        self.assertEqual(facts["summary"]["job_count"], 14)
        self.assertEqual(facts["summary"]["executed_job_ids"], sorted(facts["execution_contract"]["job_ids"]))
        self.assertEqual(set(facts["tool_trees"]), {"managed_host", "execution_host", "execution_container", "execution_synthetic_container"})
        self.assertEqual(facts["tool_identity"], {field: self.tool_identity[field] for field in claude_profile.TOOL_IDENTITY_FIELDS})
        self.assertEqual(set(facts["probes"]), {"failure_lifecycle", "storage"})
        self.assertEqual(self.lifecycle_calls, 1, "失败生命周期探针在二进制核验之前跑一次")
        self.assertEqual(set(facts["dependencies"]), {"host", "capture_container", "synthetic_container"})
        self.assertEqual([item["name"] for item in facts["containers"]], [CAPTURE, SYNTHETIC])
        self.assertEqual(self.binary_calls, 1)
        # 合成 Job 的每一步都只在断网容器里探，真实上游 Job 只在抓包容器里探。
        launchers = {item["id"]: {step["launcher"] for step in item["steps"]} for item in facts["jobs"]}
        for job_id, values in launchers.items():
            with self.subTest(job=job_id):
                self.assertEqual(values, {"synthetic_container"} if job_id in SYNTHETIC_JOBS else {"capture_container"})
        step_probes = [call for call in self.docker_calls() if call[0] == "exec" and any("container_python_script" in item for item in call)]
        self.assertEqual(len(step_probes), 112)
        self.assertEqual({call[1] for call in step_probes}, {CAPTURE, SYNTHETIC})
        self.assertFalse([call for call in self.docker_calls() if call[0] not in {"exec", "inspect"}], "替身只收到 exec 与 inspect")
        self.assertEqual(sealed["schema_version"], claude_profile.RECEIPT_SCHEMA)
        self.assertEqual(sealed["status"], "passed")
        self.assertIn("storage_probe_sha256", sealed)
        self.assertEqual(sealed["failure_lifecycle_probe_sha256"], receipt._fingerprint(facts["probes"]["failure_lifecycle"]))
        self.assertEqual(replayed, sealed)
        self.assertEqual(sealed["execution_contract"], contract)
        receipt.assert_formal_compatible(sealed, contract)
        self.assertEqual(receipt.assert_recovery_compatible(sealed, contract), sealed)
        # Formal 必需的探针摘要按画像取：Claude 要证据 runs 根与失败生命周期两项，缺任何一项即拒绝。
        for digest in claude_profile.FORMAL_PROBE_DIGESTS:
            with self.subTest(digest=digest), self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "未通过"):
                receipt.assert_formal_compatible({key: value for key, value in sealed.items() if key != digest}, contract)
        with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "不一致"):
            receipt.assert_formal_compatible(sealed, dict(contract, suite="core"))
        # 证据 runs 根探针：两个容器看到的是同一个可写目录。
        storage = facts["probes"]["storage"]
        self.assertEqual(storage["runs_root"], str(self.data / "runs"))
        self.assertEqual(set(storage["containers"]), {"capture_container", "synthetic_container"})

        # 增量复用：全部通过的前序收据之后没有要执行的 Job，空执行事实不触碰任何运行时探针。
        before = len(self.docker_calls())
        with self.patched():
            noop = self.collect(campaign, "noop-facts.json", previous_receipt="receipt.json", rerun_failed=True)
            noop_receipt = receipt.finalize(self.evidence, "noop-facts.json", "noop-receipt.json", client="claude")
            source = receipt.assert_recovery_compatible(noop_receipt, contract)
        self.assertEqual((noop["schema_version"], noop["status"]), (claude_profile.FACTS_SCHEMA, receipt.INCREMENTAL_NOOP_STATUS))
        self.assertEqual(len(self.docker_calls()), before, "空执行不调用 docker")
        self.assertEqual(self.binary_calls, 1)
        self.assertEqual(noop_receipt["schema_version"], claude_profile.RECEIPT_SCHEMA)
        self.assertEqual(source, sealed)
        with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "incremental-noop"):
            receipt.assert_formal_compatible(noop_receipt, contract)

    def test_receipts_before_the_failure_lifecycle_probe_replay_but_are_not_formal(self) -> None:
        """真实 v1 storage-only 原件按原字节重放，当前生成与 Formal 均拒绝。"""

        copy_formed_v1(self.evidence)
        original = (self.evidence / "storage-only-receipt.json").read_bytes()
        replayed = receipt.replay(self.evidence, "storage-only-receipt.json", client="claude")
        self.assertEqual(receipt._canonical(replayed), original)
        self.assertIn("storage_probe_sha256", replayed)
        self.assertNotIn("failure_lifecycle_probe_sha256", replayed)
        for drift in (False, True):
            with self.subTest(drift=drift), self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "历史 Claude v1"):
                receipt.build_receipt(self.evidence, "storage-only-facts.json", allow_collector_drift=drift)
        with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "历史 Claude v1"):
            receipt.finalize(self.evidence, "storage-only-facts.json", "new-receipt.json", client="claude")
        self.assertFalse((self.evidence / "new-receipt.json").exists())
        with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "历史 Claude v1"):
            receipt.assert_formal_compatible(replayed, replayed["execution_contract"])

    def test_failed_synthetic_steps_are_sealed_and_only_they_rerun(self) -> None:
        campaign = self.write_campaign()
        with self.patched(fail_step_container=SYNTHETIC):
            facts = self.collect(campaign, "facts.json")
            sealed = receipt.finalize(self.evidence, "facts.json", "receipt.json", client="claude")
        self.assertEqual(facts["summary"]["status"], "failed")
        self.assertEqual(set(facts["summary"]["failed_job_ids"]), SYNTHETIC_JOBS)
        self.assertEqual(sealed["status"], "failed")
        self.assertEqual(set(sealed["failed_job_ids"]), SYNTHETIC_JOBS)
        failed = next(item for item in facts["jobs"] if item["id"] == "official-synthetic-oauth-refresh")
        self.assertIn("relay missing in capture-cli-synthetic", failed["error"])
        with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "未通过"):
            receipt.assert_formal_compatible(sealed, facts["execution_contract"])
        before = len(self.docker_calls())
        with self.patched():
            rerun = self.collect(campaign, "rerun-facts.json", previous_receipt="receipt.json", rerun_failed=True)
        self.assertEqual(rerun["summary"]["status"], "passed")
        self.assertEqual(set(rerun["summary"]["executed_job_ids"]), SYNTHETIC_JOBS)
        self.assertEqual(len(rerun["summary"]["reused_job_ids"]), 11)
        rerun_steps = [call for call in self.docker_calls()[before:] if any("container_python_script" in item for item in call)]
        self.assertEqual({call[1] for call in rerun_steps}, {SYNTHETIC})
        self.assertEqual(len(rerun_steps), sum(facts["execution_contract"]["step_counts"][job] for job in SYNTHETIC_JOBS))

    def test_tampered_facts_are_rejected(self) -> None:
        campaign = self.write_campaign()
        with self.patched():
            facts = self.collect(campaign, "facts.json")
            receipt.validate_facts(copy.deepcopy(facts))

        def binary(field: str, item: object):
            return lambda value: value["binary_verification"].__setitem__(field, item)

        def shrink(tree: dict) -> None:
            # 少一个文件后重算自摘要，仍必须因与冻结完整读集不符而拒绝。
            tree["entries"].pop()
            tree["entry_count"] = len(tree["entries"])
            tree["files_sha256"] = receipt._fingerprint({"entries": tree["entries"]})

        cases = {
            "缺工具身份": (lambda value: value.pop("tool_identity"), "字段不闭合"),
            "wire 身份漂移": (lambda value: value["tool_identity"].__setitem__("wire_producer_sha256", "0" * 64), "wire／策略身份"),
            "启动器": (lambda value: value["jobs"][0]["steps"][0].__setitem__("launcher", "host"), "步骤演练不完整"),
            "容器集合": (lambda value: value["containers"].pop(), "容器集合"),
            "断网容器工具树": (lambda value: shrink(value["tool_trees"]["execution_synthetic_container"]), "完整工具树与冻结合同"),
            "断网容器依赖": (lambda value: value["dependencies"].pop("synthetic_container"), "dependencies"),
            "宿主客户端摘要": (binary("host_binary", {"path": f"/opt/claude-{TARGET}/claude", "sha256": "0" * 64}), "核验事实"),
            "断网容器联网": (lambda value: value["binary_verification"]["synthetic_container"].__setitem__("network_mode", "bridge"), "核验事实"),
            "运行镜像": (lambda value: value["binary_verification"]["runtime_image"].__setitem__("reference", "x@sha256:" + "0" * 64), "核验事实"),
            "Codex 二进制形状": (binary("passed", True), "核验事实"),
            "runs 根不同目录": (lambda value: value["probes"]["storage"]["containers"]["synthetic_container"].__setitem__("inode", 1), "同一个目录"),
            "Codex 探针": (lambda value: value["probes"].__setitem__("bubblewrap", {}), "probes"),
            "缺失败生命周期探针": (lambda value: value["probes"].pop("failure_lifecycle"), "probes"),
            "失败生命周期补跑上限": (lambda value: value["probes"]["failure_lifecycle"].__setitem__("retry_limit", 1), "失败生命周期探针"),
            "失败生命周期在抓包容器执行": (
                lambda value: value["probes"]["failure_lifecycle"].__setitem__("execution_container", CAPTURE), "失败生命周期探针"),
            "失败生命周期少一次归档": (lambda value: value["probes"]["failure_lifecycle"]["archives"].pop(), "归档数量"),
            "采集器": (lambda value: value["collector"].pop("profile"), "collector"),
            "C2PA": (lambda value: value["jobs"][0].__setitem__("c2pa_identity", {"x": 1}), "C2PA"),
            "运行时摘要": (lambda value: value.__setitem__("runtime_identity_sha256", "0" * 64), "运行时身份摘要"),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label=label), self.patched():
                current = copy.deepcopy(facts)
                mutate(current)
                with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, message):
                    receipt.validate_facts(current)

    def test_cli_selects_the_client(self) -> None:
        campaign = self.write_campaign()

        def main(*argv: str) -> tuple[int, str, str]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = receipt.main(list(argv))
            return code, stdout.getvalue(), stderr.getvalue()

        root = str(self.evidence)
        with self.patched():
            code, out, err = main("collect", "--client", "claude", "--campaign-dir", str(campaign), "--evidence-root", root,
                                  "--output", "facts.json")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["job_count"], 14)
            # 认错客户端：Codex 的 finalize 不封存 Claude facts，Codex 的 replay 不认 Claude 收据；报错带客户端标签。
            code, _out, err = main("finalize", "--evidence-root", root, "--facts", "facts.json", "--output", "wrong.json")
            self.assertEqual(code, 1)
            self.assertIn("Codex 完整 Job 离线演练失败", err)
            self.assertFalse((self.evidence / "wrong.json").exists())
            code, out, err = main("finalize", "--client", "claude", "--evidence-root", root, "--facts", "facts.json",
                                  "--output", "receipt.json")
            self.assertEqual((code, json.loads(out)["status"]), (0, "passed"), err)
            self.assertEqual(main("replay", "--client", "claude", "--evidence-root", root, "--receipt", "receipt.json")[0], 0)
            code, _out, err = main("replay", "--evidence-root", root, "--receipt", "receipt.json")
            self.assertEqual(code, 1)
            self.assertIn("receipt.schema_version 不匹配", err)
            # Claude 的 Campaign 交给 Codex 的采集：Codex 编排器不认 Claude 总计划。
            with self.assertRaises(Exception):
                receipt.collect(self.evidence, "codex-facts.json", campaign_dir=campaign)
            code, _out, err = main("replay", "--client", "claude", "--evidence-root", root, "--receipt", "facts.json")
            self.assertEqual(code, 1)
            self.assertIn("Claude 完整 Job 离线演练失败", err)

    def test_step_probe_routes_and_rejects(self) -> None:
        manifest_configuration = {
            "capture_container": CAPTURE, "synthetic_capture_container": SYNTHETIC, "capture_tool_root": str(self.tool_root),
        }
        payload = _scenario()
        live = next(step for job in payload["capture_jobs"] for step in job["steps"] if "--upstream-ip" in step["argv"])
        context = {
            "capture_container": CAPTURE, "synthetic_capture_container": SYNTHETIC, "capture_tool_root": str(self.tool_root),
            "capture_root": str(self.data), "campaign_id": "c1", "claude_bin": "/opt/claude/claude", "target_version": TARGET,
            "target_sha256": TARGET_SHA, "credentials_file": "/c", "model": "m", "upstream_ip": "160.79.104.10",
            "ca_signing_pem": "/p", "ca_cert": "/q", "runtime_image": IMAGE,
        }
        runtime = plan.runtime_variable_types(payload)
        argv = [plan._expand(item, context, runtime) for item in live["argv"]]
        used = {name for value in live["argv"] for name in plan._template_fields(value)} & set(runtime)
        step = {"argv": argv, "environment": {}, "timeout": 900, "runtime_variables": {name: runtime[name] for name in used}}
        with self.patched():
            launcher, checks = claude_profile._step_probe("j", 1, step, manifest_configuration)
        self.assertEqual(launcher, "capture_container")
        self.assertEqual([item["kind"] for item in checks], ["container_python_script", "container_file", "container_file"])
        cases = {
            "真实上游步骤进断网容器": (dict(step, argv=["docker", "exec", SYNTHETIC, *argv[3:]]), "未绑定执行容器"),
            "不是受管中继": (dict(step, argv=[*argv[:4], "/tmp/other.py", *argv[5:]]), "受管中继"),
            "运行期变量声明不一致": (dict(step, runtime_variables={}), "运行期变量"),
            "目录模块越出执行副本": (
                dict(step, argv=[item if item != argv[argv.index("--fw-f-v5-catalog") + 1] else "/etc/passwd" for item in argv]),
                "执行副本",
            ),
            "版本事实摘要不符": (
                dict(step, argv=[item if item != argv[argv.index("--fw-f-v5-version-facts-sha256") + 1] else "0" * 64 for item in argv]),
                "版本事实文件摘要",
            ),
        }
        for label, (current, message) in cases.items():
            with self.subTest(label=label), self.patched(), self.assertRaisesRegex(receipt.JobRehearsalReceiptError, message):
                claude_profile._step_probe("j", 1, current, manifest_configuration)


class CodexInterfaceParityTests(unittest.TestCase):
    """Codex 的三个合同接口与画像就是改造前的原函数与原值（逐字节对照另在 ARM64 外的对照脚本里跑过全量）。"""

    def test_codex_bindings_and_contract(self) -> None:
        adapter = uca.adapter_for("codex")
        self.assertIs(adapter.job_rehearsal_profile, receipt)
        self.assertIs(adapter.build_job_contract, receipt.build_codex_job_contract)
        self.assertIs(adapter.binary_facts, receipt.codex_binary_facts)
        self.assertIs(adapter.job_contract_replay, receipt.codex_job_contract_replay)
        scenario = json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_151_0.json").read_text(encoding="utf-8"))
        configuration = {
            "runtime_image": f"capture-runtime@sha256:{'4' * 64}", "model": "gpt-5.5", "lite_model": "gpt-5.6-luna",
            "capture_root": "/root/oauth-capture", "capture_container": "capture-cli", "service_container": "sub2apiplus",
            "keeper_container": "sub2apiplus-keeper", "postgres_container": "sub2apiplus-postgres", "redis_container": "sub2apiplus-redis",
            "capture_codex_bin": "/opt/codex-0.151.0/bin/codex", "relay_codex_bin": "/opt/codex-0.151.0/bin/codex",
            "capture_code_mode_host_bin": "/opt/codex-0.151.0/bin/codex-code-mode-host",
            "relay_code_mode_host_bin": "/opt/codex-0.151.0/bin/codex-code-mode-host",
        }
        for wire in (None, ("5" * 64, "6" * 64)):
            with self.subTest(wire=bool(wire)):
                manifest = {
                    "target_version": "0.151.0", "target_sha256": "1" * 64, "suite": "full", "configuration": configuration,
                    "official_identity": {"package": {"asset_sha256": "2" * 64, "code_mode_host_sha256": "3" * 64}},
                    "tool_identity": {"files_sha256": "7" * 64, **({"wire_producer_sha256": wire[0], "policy_sha256": wire[1]} if wire else {})},
                }
                direct = receipt.build_execution_contract(
                    target_version="0.151.0", target_sha256="1" * 64, target_package_sha256="2" * 64,
                    target_code_mode_host_sha256="3" * 64, suite="full", tool_files_sha256="7" * 64,
                    configuration=configuration, target_scenario=scenario, extra_jobs=None,
                    wire_producer_sha256=wire[0] if wire else None, policy_sha256=wire[1] if wire else None,
                )
                self.assertEqual(receipt._canonical(adapter.build_job_contract(manifest, scenario, None)), receipt._canonical(direct))

    def test_codex_profile_values_are_the_former_literals(self) -> None:
        self.assertEqual(receipt.TARGET_SCENARIO_INPUT, "target_discovery_scenarios")
        self.assertEqual(receipt.JOB_PHASES, ("official", "candidate"))
        self.assertEqual(receipt.STEP_LAUNCHERS, {"host", "capture_container"})
        self.assertEqual(receipt.CONTAINER_TREE_NAMES, ("execution_container",))
        self.assertEqual(receipt.FORMAL_PROBE_DIGESTS, ("storage_probe_sha256", "failure_lifecycle_probe_sha256"))
        self.assertEqual(receipt.EXTRA_FACT_FIELDS, frozenset())
        self.assertEqual(receipt.PRODUCER_FIELDS, {"schema_version", "tool", "tool_sha256", "version"})
        self.assertEqual(receipt.rehearsal_producer(), receipt._producer())
        self.assertEqual(receipt.CODEX_CONTAINER_FIELDS, (
            "capture_container", "service_container", "keeper_container", "postgres_container", "redis_container",
        ))

    def test_codex_binary_replay_messages_are_unchanged(self) -> None:
        contract = {
            "target_version": "0.151.0", "target_sha256": "1" * 64, "target_package_sha256": "2" * 64,
            "target_code_mode_host_sha256": "3" * 64, "configuration": {"runtime_image": "img@sha256:" + "4" * 64},
        }
        facts = {"binary_verification": {
            "passed": True, "expected_version": "0.151.0", "expected_sha256": "1" * 64,
            "runtime_image_reference": "img@sha256:" + "4" * 64, "runtime_image_id": "sha256:" + "5" * 64,
            "package": {"asset_sha256": "2" * 64, "code_mode_host_sha256": "3" * 64},
            "identities": [{"label": label, "sha256": "1" * 64} for label in receipt.CODEX_BINARY_IDENTITY_LABELS],
            "helpers": [{"label": label, "sha256": "3" * 64} for label in receipt.CODEX_HELPER_IDENTITY_LABELS],
        }}
        self.assertEqual(receipt.codex_job_contract_replay(contract, facts), {"binary_facts": receipt.codex_binary_facts(contract)})
        for label, change, message in (
            ("运行镜像", lambda binary: binary.__setitem__("runtime_image_id", "x"), "Codex 二进制或运行镜像探针非法"),
            ("官方包", lambda binary: binary["package"].__setitem__("asset_sha256", "0" * 64), "Codex package 探针非法"),
            ("helper", lambda binary: binary["helpers"].pop(), "Codex 主程序或 code-mode-host 身份未闭合"),
        ):
            with self.subTest(label=label):
                current = copy.deepcopy(facts)
                change(current["binary_verification"])
                with self.assertRaisesRegex(receipt.JobRehearsalReceiptError, f"^{message}$"):
                    receipt.codex_job_contract_replay(contract, current)


if __name__ == "__main__":
    unittest.main()
