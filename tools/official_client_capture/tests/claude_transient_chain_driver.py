"""Claude transient 恢复段连续链的离线驱动；不单独登记认证通过。

VC-0/1 使用批次夹具明确的已封存输入，VC-3/4、候选 run/seal、失败 builder、恢复 apply、ar1 run/seal 与 b1 builder
执行产品代码。宿主/Docker/网关来自 CandidateFixture；中继替身按已有连接序列写原始字节，并由可改控制文件注入一条头值故障。
首次冻结使用保留批准规则全集的控制面画像接缝，官方字节来自 evaluator fixture；候选评估必须来自实际 seal。
每个基线真实 compare 后只用它生成的配置运行 builder，后继可继续严格 accept／VC-6；不等同于全规则真实语义认证。
不替换候选阶段结果、attempt 读侧、恢复裁定、权限/封存边界、Store、请求核算或断言结论。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import claude_upgrade_store as candidate_store
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_boundary_receipt_fixture as boundary_fixture
from tools.official_client_capture.tests import claude_client_receipt_fixture as client_fixture
from tools.official_client_capture.tests import claude_fault_chain_world as world_fixture
from tools.official_client_capture.tests import claude_late_control_chain_driver as late_control
from tools.official_client_capture.tests import claude_vc5_chain_fixture as vc5_sources
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
from tools.official_client_capture.tests import test_claude_upgrade_evaluation_recovery as evaluation_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests

CANDIDATE = candidate_tests.CANDIDATE
FAILED_JOB = candidate_tests.LIVE_JOB
FAILED_PROBE = "v4-replay-baseline"
HOST = "arm64-transient-fixture"


def _write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _candidate_relay_sequences(sequences: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    """只投影冻结候选标签允许的网关出站请求，保留来源连接号、请求顺序与合成干预。

    来源是离线官方序列夹具；这份投影只用于连续链执行源，不认证真实网关的版本语义。只丢掉已声明由网关
    本地处理的端点，未知端点或可转发但未获本 probe 标签允许的请求失败关闭，禁止把错误请求静默过滤掉。
    """

    local_requests = {
        "HEAD /api/hello", "GET /api/claude_code/policy_limits", "GET /api/claude_code/settings",
        "GET /api/oauth/profile", "GET /v1/mcp_servers",
    }
    model_requests = {"POST /v1/messages", "POST /v1/messages/count_tokens"}
    projected = copy.deepcopy(sequences)
    projected["description"] = "冻结候选标签约束下的离线网关出站投影；原官方序列保持不变，不表示真实候选请求。"
    projected["probes"] = {}
    for entry in labels["document"]["entries"]:
        if entry["side"] != "candidate":
            continue
        rules = {rule["labels"]["probe"]: rule for rule in entry["rules"] if "connection" in rule}
        for probe, connection_plan in labels["plans"][entry["job_id"]].items():
            if probe in projected["probes"] or connection_plan["mode"] not in {"none", "unordered"}:
                raise AssertionError(f"候选来源投影遇到重复 probe 或未支持的连接形态：{probe}")
            record = copy.deepcopy(sequences["probes"][probe])
            rule_labels = rules[probe]["labels"]
            if (record["response_mode"] != rule_labels["response_mode"]
                    or record["response_plan"] != rule_labels.get("response_plan")):
                raise AssertionError(f"候选来源的合成模式或计划与冻结标签不同：{probe}")
            forwarded = ({"POST /v1/oauth/token"} if plan.candidate_trigger(probe) == evidence.CANDIDATE_TRIGGER_ACCOUNT_REFRESH
                         else model_requests)
            allowed = set(connection_plan.get("requests", ()))
            if not allowed <= forwarded:
                raise AssertionError(f"候选标签超出网关转发边界：{probe}")
            connections = []
            for item in record["connections"]:
                requests = []
                for request in item["requests"]:
                    normalized = request.split("?", 1)[0]
                    if normalized in local_requests:
                        continue
                    if normalized not in forwarded or normalized not in allowed:
                        raise AssertionError(f"候选来源含未知或未获标签允许的出站请求：{probe}／{request}")
                    requests.append(request)
                # 没有上游请求的连接不生成有效记录或字节附件；保留其余连接号、原请求顺序与干预动作。
                if requests:
                    connections.append({**item, "requests": requests})
            record["connections"] = connections
            # 官方客户端直连的握手前重置不能转成候选网关的握手事实；投影不带这些零请求记录。
            record["excluded_connection_ids"] = []
            plan.match_relay_connections(connection_plan, connections)
            projected["probes"][probe] = record
    return projected


class ClaudeTransientChainDriver(late_control.ClaudeLateControlChainDriver):
    """只借用既有输入驱动，不继承那些测试用例；所有新段产物均由真实命令产生。"""

    recover = evaluation_tests.EvaluationRecoveryChainTests.recover
    preview = evaluation_tests.EvaluationRecoveryChainTests.preview

    def setUp(self) -> None:
        # 外部 Mac 来源与运行账本共用 staging；禁止最小来源夹具偷偷创建旁账。
        client_fixture.install_staging_scope(self)
        super().setUp()
        self.runtime = candidate_tests.CandidateFixture(methodName="runTest")
        self.runtime.tool_identity = self.tool_identity
        self.runtime.setUp()
        self.addCleanup(self.runtime.doCleanups)
        if os.environ.get(world_fixture.METHOD_RESUME_ENV):
            saved = world_fixture.resume_checkpoint(self, "transient-setup")
            if saved is None or saved.get("phase") != "ready":
                raise ValueError("transient 原方法缺少完整运行来源停点，禁止重新安装中继")
            return
        # 批次与执行源共享同一宿主数据根，避免用别处的证据或总账拼成连续链。
        self.root, self.data = self.runtime.root, self.runtime.data
        self.relay_control = _write(self.root / "transient-relay-control.json", {"bad_probe": FAILED_PROBE})
        self.mutations = _write(self.root / "transient-relay-mutations.json", {})
        self.install_relay_source()
        world_fixture.checkpoint(self, "transient-setup", phase="ready")
        world_fixture.checkpoint(self, "transient-chain", phase="unstarted", values={}, parameters=None)
        world_fixture.checkpoint(self, "transient-control-delivery-chain", phase="unstarted")

    def source_step(self, cursor: str, key: str, argv: list[Any], campaign: Path) -> tuple[int, dict | None, str]:
        """只转交共享的原命令承接接缝；相同 argv 的有意重复必须使用不同逻辑键。"""

        return world_fixture.command_step(self, cursor, key, argv,
            lambda: self.source_command(argv), campaign=campaign)

    def vc_step(self, cursor: str, key: str, context: dict[str, Any], phase: str,
                sequence: int, action: dict[str, Any]) -> tuple[int, dict | None, str]:
        """固定原批次序号和动作计划坐标，执行仍进入原父监督器。"""

        campaign = context["campaign"]
        plan_name = (f"vc-5-{sequence:04d}.json" if phase == "VC-5"
                     else f"{phase.lower()}-{action['action_id']}.json")
        plan_path = self.root / "action-plans" / plan_name
        revision = context.get("revision", 1)
        predecessor = "VC-4" if phase == "VC-5" else "VC-3"
        argv = ["compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", phase,
                "--sequence", sequence, "--predecessor-checkpoint",
                vc.vc_checkpoint_path(campaign, predecessor, revision=revision if phase == "VC-5" else None),
                "--action-plan", plan_path, "--state-dir", self.state_dir]
        invoke = (lambda: self.dispatch_vc5(context, sequence, action)) if phase == "VC-5" else (
            lambda: self.dispatch(campaign, phase, sequence, [action]))
        return world_fixture.command_step(self, cursor, key, argv, invoke, campaign=campaign)

    def client_inputs(self, campaign: Path, attempt_root: Path, attempt: dict) -> list[str]:
        """客户端来源只写一次；续作重验原输入字节，不改已发布的 attempt。"""

        name = "client-inputs:" + str(attempt_root.resolve())
        saved = world_fixture.load_state(name)
        if saved is not None:
            if not os.environ.get(world_fixture.METHOD_RESUME_ENV):
                raise ValueError("原客户端来源尚未形成完整原件，禁止重写或猜测收据")
            if saved.get("status") == "complete":
                for binding in saved["bindings"]:
                    world_fixture.read_cut(binding)
                return saved["argv"]
            self._resume_unwritten_client_inputs(name, saved, campaign, attempt_root, attempt)
        else:
            # 先核同一 staging 与原账本，路径错误不留下含糊的材料写入 pending。
            client_fixture.fixture_ledger(attempt_root)
        world_fixture.save_state(name, {"status": "pending", "inputs": [
            world_fixture.file_cut(attempt_root / "attempt.json"),
            world_fixture.file_cut(campaign / "campaign.json")]})
        argv = self.write_clients(campaign, attempt_root, attempt)
        paths = [attempt_root / "attempt.json",
                 *(path for path in (attempt_root / "client-evidence").rglob("*") if path.is_file())]
        world_fixture.save_state(name, {"status": "complete", "argv": argv,
            "bindings": [world_fixture.file_cut(path) for path in paths]})
        return argv

    def _resume_unwritten_client_inputs(self, name: str, saved: dict, campaign: Path,
                                       attempt_root: Path, attempt: dict) -> None:
        """只接续已有首写前路径错误；部分材料、未知 pending 或缺失败原件均拒绝。"""

        result = world_fixture.load_state("method-result")
        if (saved != {"status": "pending"} or result is None or result.get("complete")
                or result.get("returncode") != 1):
            raise ValueError("原客户端来源尚未形成完整原件，禁止重写或猜测收据")
        for binding in result["bindings"].values():
            world_fixture.read_cut(binding)
        context = world_fixture.load_state("method-context")
        original_exit = json.loads(world_fixture.read_cut(result["bindings"]["exit"]))
        if (context is None or original_exit.get("argv") != [sys.executable, "-m", "unittest", "-v",
                "tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains." + context["method"]]
                or original_exit.get("returncode") != 1 or original_exit.get("timed_out") is not False
                or original_exit.get("verify") is not False or original_exit.get("executor_error") is not None
                or result.get("expected") != context.get("expected")
                or not any(invocation.get("state") == "finished" and invocation.get("bindings") == result["bindings"]
                           for invocation in context.get("invocations", []))):
            raise ValueError("原客户端首写前失败不属于本方法的准确调用")
        from tools.official_client_capture.tests import claude_chain_diagnostic_budget as budget
        if bool(result.get("diagnostic_only")) != (budget.diagnostic_seconds() is not None):
            raise ValueError("原客户端首写前失败的诊断模式不一致")
        stderr = world_fixture.read_cut(result["bindings"]["stderr"]).decode("utf-8")
        error = "tools.official_client_capture.capturelib.model.ConfigurationError: Mac 客户端证据：Mac 夹具 Campaign 必须在同一临时 staging 树"
        if (stderr.count("Traceback (most recent call last):") != 1 or error not in stderr.splitlines()
                or not all(fragment in stderr for fragment in (
                "in client_inputs", "in write_clients", "in material_v1",
                "project = fixture_ledger(attempt_root)", "in fixture_ledger",
                "ConfigurationError: Mac 客户端证据：Mac 夹具 Campaign 必须在同一临时 staging 树"))):
            raise ValueError("原客户端 pending 没有材料首写前的准确路径失败原件")
        evidence = attempt_root / "client-evidence"
        if evidence.exists() or evidence.is_symlink():
            raise ValueError("原客户端材料已经部分形成，不能重跑首写")
        if (campaign not in attempt_root.parents
                or json.loads((attempt_root / "attempt.json").read_bytes()) != attempt):
            raise ValueError("原客户端材料首写的 Campaign 或 attempt 已改变")
        # 原命令输出和原世界由实际读侧核对；错误文本本身不是恢复授权。
        world_fixture.verify_commands((world_fixture.load_state("command-observations") or {}).get("commands", []))
        world_fixture.verify_case(self, campaign)
        project = client_fixture.fixture_ledger(attempt_root)
        values = {"campaign_id": campaign.name, "candidate_id": attempt["candidate_id"],
                  "attempt_id": attempt_root.name, "run_nonce": attempt["run_nonce"]}
        capture = project.parent / "server-originals" / client_fixture.contract.digest(values)[:12]
        if capture.exists() or capture.is_symlink():
            raise ValueError("原客户端服务端材料已经形成，不能重跑首写")
        # 旧 pending 原文另存，旧失败输出和方法结果始终保留；随后才许可第一次写材料。
        original = world_fixture._state_path(name)
        raw = original.read_bytes()
        archive = original.with_name(original.stem + ".before-material-failure.json")
        if archive.exists():
            if archive.is_symlink() or archive.read_bytes() != raw:
                raise ValueError("原客户端 pending 归档与首败原文不一致")
        else:
            with archive.open("xb") as stream:
                stream.write(raw)
            archive.chmod(0o600)
        world_fixture.save_state(name + ":before-material-resume", {
            "failure": result["bindings"], "pending": world_fixture.file_cut(archive),
            "attempt": world_fixture.file_cut(attempt_root / "attempt.json"),
            "campaign": world_fixture.file_cut(campaign / "campaign.json")})

    def install_relay_source(self) -> None:
        """中继输入保持真实序列与完整候选封套；头值从单独控制文件读取，修复不改工具源码或旧字节。"""

        prefix, tail = candidate_tests.CANDIDATE_RELAY.rsplit("sys.exit(0)", 1)
        source, source_tail = seal_tests.FULL_FAKE_RELAY.rsplit("sys.exit(0)", 1)
        self.assertFalse(tail.strip() or source_tail.strip())
        # 通用候选前缀只保留 dry-run 和宿主凭据核对，不能给零出站 probe 留下虚假的通用请求附件。
        generic_artifact = 'with open(os.path.join(output, "relay-raw.bin"), "wb") as handle:'
        self.assertEqual(prefix.count(generic_artifact), 1)
        prefix = prefix.split(generic_artifact, 1)[0]
        # 零连接 probe 不应因 all([]) 被归成 OAuth 专用主机。
        source = source.replace('oauth_only = all(', 'oauth_only = bool(connections) and all(')
        marker = '        stream += f"{method} {target} HTTP/1.1'
        self.assertEqual(source.count(marker), 1)
        source = source.replace(marker, '''        version = "2023-01-01" if probe == json.load(open(os.environ["CLAUDE_TRANSIENT_RELAY_CONTROL"], encoding="utf-8")).get("bad_probe") else "2023-06-01"
        stream += f"{method} {target} HTTP/1.1''')
        source = source.replace(r'Host: {host}\r\nContent-Length:', r'Host: {host}\r\nAuthorization: Bearer <secret>XXXXXXXXXXXXXXXXXXXXXXXX\r\nAnthropic-Version: {version}\r\nContent-Length:')
        envelope = '''
manifest.update(side="candidate", run_id=value("--run-id"))
manifest["candidate_gateway"] = {
    "base_url": "http://" + os.environ["SERVICE_CONTAINER"] + ":8080",
    "account_id": int(os.environ["ACCOUNT_ID"]), "api_key_id": int(os.environ["API_KEY_ID"]),
    "trigger": os.environ["GATEWAY_TRIGGER"], "client_authentication": "ANTHROPIC_AUTH_TOKEN", "oauth_credentials_read": False,
}
manifest["upstream"].update(relay_mode="connect", relay_listen_port=int(os.environ["RELAY_PORT"]))
manifest["hosts_recovery"] = HOSTS_RECOVERY
manifest["m_binding"] = {"complete": True, "requirements": {key: True for key in REQUIREMENTS}}
manifest["candidate_observation"] = {
    "client_outcome_valid": True, "message_request_expectation_met": True,
    "synthetic_plan_closed": True if not live else None, "dimension_result": "passed",
}
manifest["dimension_evidence"].update(side="candidate", observation_only=True)
if os.environ["GATEWAY_TRIGGER"] == "gateway-account-refresh":
    with open(os.path.join(output, "results", "gateway-trigger.json"), "w", encoding="utf-8") as handle:
        json.dump({"schema_version": "claude-candidate-gateway-trigger/v1", "trigger": "gateway-account-refresh",
                   "account_id": int(os.environ["ACCOUNT_ID"]), "valid": True}, handle)
with open(os.path.join(output, "relay-manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle)
sys.exit(0)
'''
        envelope = "\nHOSTS_RECOVERY = " + repr(evidence.CANDIDATE_HOSTS_RECOVERY) + "\nREQUIREMENTS = " + repr(sorted(evidence.CANDIDATE_M_REQUIREMENTS)) + "\n" + envelope
        relay = self.runtime.tool_copy / "claude_fw_e_relay.py"
        relay.write_text(prefix + source + envelope, encoding="utf-8")
        relay.chmod(0o700)

    def build_campaign(self, **kwargs: Any) -> Path:
        """在两本账注册之前冻结运行坐标、标签和官方／候选模板全集，沿用真实全量场景清单。"""

        campaign = super().build_campaign(**kwargs)
        manifest = upgrade.load_campaign_manifest(campaign)
        labels = _write(campaign / "inputs" / "evidence-labels.json", json.loads(plan.evidence_labels_path(capture_tests.TARGET).read_bytes()))
        scenarios = json.loads((campaign / manifest["inputs"]["target_scenarios"]["path"]).read_bytes())
        frozen_labels = plan.load_evidence_labels(labels, expected_version=manifest["target_version"], scenario_payload=scenarios)
        self.relay_sequences = _write(self.root / "transient-candidate-relay-sequences.json",
            _candidate_relay_sequences(json.loads(seal_tests.SEQUENCES.read_bytes()), frozen_labels))
        configuration = {
            **manifest["configuration"], **candidate_tests.CANDIDATE_CONFIGURATION,
            "runtime_image": capture_tests.IMAGE, "capture_root": str(self.data), "capture_tool_root": str(self.runtime.tool_copy),
            "capture_container": capture_tests.CAPTURE, "synthetic_capture_container": capture_tests.SYNTHETIC,
            "claude_bin": f"/opt/claude-{capture_tests.TARGET}/claude", "model": "claude-sonnet-5", "upstream_ip": "160.79.104.10",
            "credentials_file": "/root/.claude/.credentials.json", "ca_signing_pem": "/opt/mitm/mitmproxy-ca.pem",
            "ca_cert": "/opt/mitm/mitmproxy-ca-cert.pem",
        }
        manifest = seal_tests.rewrite_manifest(campaign, configuration=configuration, inputs={**manifest["inputs"], "evidence_labels": {
            "path": labels.relative_to(campaign).as_posix(), "sha256": upgrade.file_sha256(labels),
        }})
        # 工具演进读侧会重新展开两个阶段，不能只冻结候选模板后靠恢复时补写。
        jobs = [job for phase in ("official", "candidate")
                for job in capture._expand_phase_jobs(campaign, manifest, phase, candidate_id=None)]
        seal_tests.rewrite_manifest(campaign, jobs=[{"id": job.job_id, "phase": job.phase,
            "execution_sha256": capture._job_execution_sha256(job), "evidence_roots": list(job.evidence_roots)} for job in jobs])
        return campaign

    def prepare_runtime(self, campaign: Path) -> None:
        """环境来源承接已真实构建的候选身份；真实准入继续核 Store、VC-4、源码及镜像引用。"""

        self.build = json.loads((campaign / "candidates" / CANDIDATE / "build-receipt.json").read_bytes())
        gateway = copy.deepcopy(candidate_tests.BASE_GATEWAY)
        gateway["service"]["image_id"] = self.build["image"]["image_id"]
        _write(self.runtime.gateway_file, gateway)
        self.runtime.activation_fact = vc5_sources.activation_fact(self.build, capture_tests.TARGET)

    @contextlib.contextmanager
    def source_context(self) -> Iterator[None]:
        """只替换外部观测和进程来源；不替换 formal_admission、候选读侧、seal 或 provenance。"""

        def probe(argv: Any, **options: Any):
            if list(argv[:5]) == ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps([self.build["image"]["reference"]]), "")
            return self.runtime.host.run(argv, **options)

        with self.runtime.patched(), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(capture, "_run_probe_command", probe))
            stack.enter_context(mock.patch.object(candidate_capture, "_collect_gateway_facts", self.runtime.fake_gateway_facts))
            stack.enter_context(mock.patch.object(candidate_capture, "_read_activation_fact", self.runtime.fake_activation_fact))
            stack.enter_context(mock.patch.object(candidate_capture, "observe_service_image", lambda *_a, **_k: self.build["image"]["image_id"]))
            stack.enter_context(mock.patch.object(candidate_capture, "host_facts", return_value={"host_id": HOST, "architecture": "linux/arm64"}))
            stack.enter_context(mock.patch.dict(os.environ, {
                "FAKE_CANDIDATE_HOST": str(self.runtime.fake_host), "FAKE_CANDIDATE_CONTROL": str(self.runtime.candidate_control),
                "FAKE_CANDIDATE_CALLS": str(self.runtime.candidate_calls_file), "FAKE_GATEWAY_FILE": str(self.runtime.gateway_file),
                "ADMIN_BEARER_TOKEN_FILE": str(self.runtime.admin_token), "FAKE_RELAY_SEQUENCES": str(self.relay_sequences),
                "FAKE_RELAY_MUTATIONS": str(self.mutations), "CLAUDE_TRANSIENT_RELAY_CONTROL": str(self.relay_control),
            }))
            yield

    def source_command(self, argv: list[Any]) -> tuple[int, dict | None, str]:
        with self.source_context():
            return batch_tests.run_main(argv)

    def write_clients(self, campaign: Path, attempt_root: Path, attempt: dict) -> list[str]:
        """Mac 与边界收据属于外部来源；严格收据合同由共享 fixture 构造，封存仍由产品代码逐项复核。"""

        from tools.official_client_capture.tests import claude_client_receipt_fixture

        values = {"campaign_id": campaign.name, "candidate_id": CANDIDATE, "attempt_id": attempt_root.name,
                  "run_nonce": attempt["run_nonce"]}
        original_attempt = (attempt_root / "attempt.json").read_bytes()
        argv = []
        for client in ("claude-desktop", "claude-vscode"):
            path = claude_client_receipt_fixture.write_receipt(attempt_root, client, values)
            argv += ["--client-evidence", f"{client}={path}"]
        for check in ("codex-final-wire-isolation", "third-party-ingress-rejection"):
            boundary_fixture.write_boundary_receipt(attempt_root, check)
        self.assertEqual((attempt_root / "attempt.json").read_bytes(), original_attempt,
                         "客户端输入夹具不能改写真实 run 已发布的 attempt 或身份")
        return argv

    def assert_campaign_request_accounting(self, campaign: Path, *, expected_client_requests: int) -> None:
        """重放实际 Campaign 的 Mac 原件与总 provenance；模拟请求不能落旁账或在重读时再次入账。"""

        from tools.official_client_capture import claude_client_receipt_contract as contract
        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger

        self.assertEqual(project_ledger.find_project_ledger(campaign, client="claude"), self.project_root)
        before = project_ledger.replay_head(self.project_root)
        client_keys: set[str] = set()
        with client_fixture.source_scope():
            for attempt_root in sorted(campaign.glob("candidates/*/attempts/*")):
                for client_id in contract.CLIENTS:
                    path = attempt_root / "client-evidence" / f"{client_id}.json"
                    if path.is_file():
                        verified = contract.verify_client_receipt(attempt_root, client_id, path)
                        client_keys.update(row["identity_key"] for row in verified["requests"])
            receipt = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)
        keys = {row["identity_key"] for row in receipt["requests"]}
        self.assertEqual(len(client_keys), expected_client_requests)
        self.assertTrue(client_keys <= keys, "真实 provenance 不能漏掉同账本的模拟 Mac 请求")
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(keys, set(before["accounted_identity_index"]))
        self.assertEqual((before["precise_total"], before["estimated_total"]), (len(keys), 0))
        self.assertEqual(project_ledger.replay_head(self.project_root), before, "读侧复核必须保持原总账事件与请求数不变")

    def seal(self, campaign: Path, attempt_id: str, *, clients: list[str] | None = None, recovery: str | None = None) -> dict:
        argv = ["capture-candidate", "seal", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--attempt-id", attempt_id]
        argv += ["--attempt-recovery", recovery] if recovery else list(clients or [])
        cursor = f"candidate-seal:{CANDIDATE}:{attempt_id}:{recovery or 'b0'}"
        code, preview, stderr = self.source_step(cursor, "preview", argv, campaign)
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
        code, result, stderr = self.source_step(cursor, "apply",
            [*argv, "--approve-seal-sha256", preview["review_sha256"], "--approved-by", "连续链审核"], campaign)
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(result, stderr)
        return result

    def evaluation_context(self, campaign: Path) -> dict[str, Any]:
        """同世界的全部批准规则与官方外部来源；不合成或覆盖候选阶段结果。"""

        name = "evaluation-context:" + str(campaign.resolve())
        saved = world_fixture.load_state(name)
        if saved is not None:
            if saved.get("status") != "complete":
                raise ValueError("原评估来源物化尚未完成，禁止重建旁侧世界")
            return world_fixture.restore_world(self, name)
        world_fixture.save_state(name, {"status": "pending"})
        # 把当前执行源已经登记的映射作为外部输入冻结，供 VC-5 动作子进程登记同一布局。
        with self.runtime.patched():
            capture_layout = {
                "host_runs_root": str(upgrade.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT),
                "container_run_roots": [str(root) for root in upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS],
            }
            context = world_fixture.real_candidate_world(self, campaign, candidate_id=CANDIDATE, revision=1,
                expected_client_requests=2, capture_layout=capture_layout)
            context = world_fixture.prepare_delivery_sources(self, context)
            world_fixture.save_world(name, context)
            world_fixture.save_state(name, {"status": "complete"})
            return context

    def late_audit(self, name: str, world: dict[str, Any]) -> Any:
        """每次六项审计都保留新物化来源；刷新当前候选不能漏掉这些可信原件。"""

        config = json.loads(world["config_path"].read_bytes())
        roots = [*world.get("trusted_roots", []), world["config_path"],
                 Path(config["official_source"]), world["campaign"] / "seam" / "official"]
        audit_world = {**world, "trusted_roots": list(dict.fromkeys(roots))}
        return super().late_audit(name, audit_world)

    def dispatch_builder(self, context: dict[str, Any], sequence: int, *, baseline: int) -> tuple[int, dict | None, str]:
        """compare 与 builder 保留原序号；部分完成时承接原输出，不再次消耗 next_sequence。"""

        cursor = f"builder:{context['candidate_id']}:b{baseline}:s{sequence}"
        saved = world_fixture.resume_checkpoint(self, cursor)
        if saved is None:
            world_fixture.refresh_world(self, context)
            self.assertEqual(context['evaluation_baseline'], baseline)
            compare_sequence = world_fixture.take_sequence(context, sequence)
            saved = {"phase": "compare", "compare_sequence": compare_sequence, "campaign": context['campaign']}
            world_fixture.save_world(cursor, context)
            world_fixture.checkpoint(self, cursor, **saved)
        else:
            restored = world_fixture.restore_world(self, cursor)
            context.clear()
            context.update(restored)
        compare_sequence = saved['compare_sequence']
        if saved['phase'] == 'compare':
            self.assert_batch_passed(self.vc_step(cursor, 'compare', context, 'VC-5', compare_sequence,
                self.compare_action(context['campaign'], candidate_id=context['candidate_id'])), compare_sequence, 'VC-5')
            with vc5_sources.seams(context['config_path']):
                comparison = upgrade._load_stage_result(context['campaign'], 'compare', context['candidate_id'])
            self.assertEqual((comparison['candidate_revision'], comparison['evaluation_baseline']), (context['revision'], baseline))
            self.assertEqual(comparison['rules'], list(upgrade._approved_rules(context['campaign'], context['manifest'], require_approved=True)))
            builder_sequence = world_fixture.take_sequence(context)
            context.update(comparison=comparison, last_builder_sequence=builder_sequence)
            saved = {**saved, 'phase': 'builder', 'builder_sequence': builder_sequence, 'comparison': comparison}
            world_fixture.checkpoint(self, cursor, **saved)
            world_fixture.save_world(cursor, context)
        builder_sequence = saved['builder_sequence']
        context['next_sequence'] = max(context['next_sequence'], builder_sequence + 1)
        context['last_builder_sequence'] = builder_sequence
        comparison = saved['comparison']
        outcome = self.vc_step(cursor, 'builder', context, 'VC-5', builder_sequence,
                               self.assert_action(context['campaign'], comparison))
        if saved['phase'] != 'complete':
            world_fixture.save_world(cursor, context)
            world_fixture.checkpoint(self, cursor, **{**saved, 'phase': 'complete'})
        return outcome

    @staticmethod
    def boundary(paths: list[Path]) -> dict[str, tuple[str, int, int]]:
        """记录旧文件正文摘要与写边界；续跑不得通过复制或重写旧文件制造表面上的成功。"""

        files = {path for root in paths for path in ([root] if root.is_file() else root.rglob("*")) if path.is_file()}
        return {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_ino, path.stat().st_mtime_ns)
                for path in sorted(files)}

    def run_transient_chain(self, *, evaluate_recovery: bool = True, first_recovery_fault: str | None = None) -> dict[str, Any]:
        """原 transient 方法保存明确阶段；恢复先重验原世界，不再派发已完成命令。"""

        from tools.official_client_capture.tests import claude_client_receipt_fixture
        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        cursor = "transient-chain"
        parameters = {"evaluate_recovery": evaluate_recovery, "first_recovery_fault": first_recovery_fault}
        saved = world_fixture.resume_checkpoint(self, cursor)
        if saved is None:
            if os.environ.get(world_fixture.METHOD_RESUME_ENV):
                raise ValueError("transient 原方法缺少阶段游标，禁止重新建立 Campaign")
            saved = {"phase": "unstarted", "values": {}, "parameters": None}
            world_fixture.checkpoint(self, cursor, **saved)
        if saved["phase"] == "unstarted":
            saved = {"phase": "vc3-vc4", "values": {}, "parameters": parameters,
                     "initializations": {"vc3-vc4-parent": "uninitialized"}}
            world_fixture.checkpoint(self, cursor, **saved)
        if saved["parameters"] != parameters:
            raise ValueError("transient 原方法参数与原停点不同")
        phase, values = (0 if saved["phase"] == "vc3-vc4" else saved["phase"]), dict(saved["values"])
        campaign = values.get('campaign')
        manifest = values.get('manifest')
        first = values.get('first')
        attempt_id = values.get('attempt_id')
        attempt_root = values.get('attempt_root')
        attempt = values.get('attempt')
        clients = values.get('clients')
        failed = values.get('failed')
        reconciled = values.get('reconciled')
        preview = values.get('preview')
        recovered = values.get('recovered')
        baseline_root = values.get('baseline_root')
        recovery = values.get('recovery')
        jobs = values.get('jobs')
        original_stage = values.get('original_stage')
        old_roots = values.get('old_roots')
        original_seal_root = values.get('original_seal_root')
        old_boundary = values.get('old_boundary')
        before_calls = values.get('before_calls')
        before_count = values.get('before_count')
        run_argv = values.get('run_argv')
        result = values.get('result')
        executed = values.get('executed')
        expected = values.get('expected')
        segment = values.get('segment')
        summary = values.get('summary')
        stage_path = values.get('stage_path')
        stage = values.get('stage')
        previous_stage = values.get('previous_stage')
        effective = values.get('effective')
        receipt = values.get('receipt')
        delta = values.get('delta')
        count = values.get('count')
        calls = values.get('calls')
        replay_boundary = values.get('replay_boundary')
        context = world_fixture.restore_world(self, cursor) if phase >= 3 else None
        audit_name = "候选 transient 裁定与恢复段续跑"
        audit = self.late_audit(audit_name, context) if 3 < phase < 13 else None
        if phase == 13:
            ScenarioAudit.revalidate(self, audit_name)
            return context
        if phase == 0:
            campaign = self.drive_vc3_vc4_to_candidate_sealed()
            self.prepare_runtime(campaign)
            manifest = upgrade.load_campaign_manifest(campaign)
            world_fixture.freeze_control_profile(campaign)
            values.update(campaign=campaign, manifest=manifest)
            phase = 1
            world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
        with claude_client_receipt_fixture.source_scope():
            if phase == 1:
                code, first, stderr = self.source_step(cursor, "first-capture", [
                    "capture-candidate", "run", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--acknowledge-live-requests",
                ], campaign)
                self.assertIsNotNone(first, stderr)
                self.assertEqual((code, first["status"]), (2, "awaiting_receipts"), stderr)
                attempt_id = first["attempt_id"]
                with self.source_context():
                    attempt_root, attempt = upgrade._load_capture_attempt(campaign, "candidate", CANDIDATE, attempt_id)
                values.update(first=first, attempt_id=attempt_id, attempt_root=attempt_root, attempt=attempt)
                phase = 2
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 2:
                clients = self.client_inputs(campaign, attempt_root, attempt)
                self.seal(campaign, attempt_id, clients=clients)
                code, _accounted, stderr = self.source_step(cursor, "first-account", ["account-sealed-candidate", "--campaign-dir", campaign, "--candidate-id", CANDIDATE], campaign)
                self.assertEqual(code, 0, stderr)
                context = self.evaluation_context(campaign)
                values.update(clients=clients)
                phase = 3
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 3:
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters,
                    campaign=campaign, audit_snapshots={audit_name: (world_fixture.load_state("cursor:" + cursor) or {}).get("audit_snapshots", {}).get(audit_name, "uninitialized")})
                audit = self.late_audit("候选 transient 裁定与恢复段续跑", context)
                code, failed, stderr = self.dispatch_builder(context, 9, baseline=0)
                self.assertIsNotNone(failed, stderr)
                self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:assert-rules"), stderr)
                code, reconciled, stderr = self.source_step(cursor, "first-reconcile", [
                    "reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", Path(failed["campaign_run"]["run_dir"]),
                ], campaign)
                self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
                values.update(failed=failed, reconciled=reconciled)
                phase = 4
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 4:
                with self.source_context():
                    code, preview, stderr = self.source_step(cursor, "recovery-preview",
                        ["evaluation-recover", "preview", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--reviewer", "reviewer-a"], campaign)
                    self.assertEqual(code, 0, stderr)
                    self.assertIsNotNone(preview, stderr)
                    self.assertEqual(preview["failure_scope"]["jobs"], [FAILED_JOB])
                    code, recovered, stderr = self.source_step(cursor, "recovery-apply",
                        ["evaluation-recover", "apply", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--reviewer", "reviewer-a",
                         "--approve-sha256", preview["review_sha256"], "--root-cause-class", "transient-environment"], campaign)
                self.assertEqual(code, 0, stderr)
                self.assertIsNotNone(recovered, stderr)
                baseline_root = campaign / "candidates" / CANDIDATE / "revisions" / "b1"
                recovery = json.loads((baseline_root / "recovery.json").read_bytes())
                self.assertEqual((recovery["attempt_id"], recovery["recovery_revision"], recovery["execute_jobs"]), (attempt_id, "ar1", [FAILED_JOB]))
                jobs = {row["id"]: row for row in attempt["results"]}
                self.assertEqual(set(recovery["reuse_jobs"]), set(jobs) - {FAILED_JOB})
                values.update(preview=preview, recovered=recovered, baseline_root=baseline_root, recovery=recovery, jobs=jobs)
                phase = 5
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 5:
                original_stage = candidate_capture.legacy_stage_result_path(campaign, CANDIDATE)
                old_roots = [Path(value) for row in jobs.values() for value in row["evidence_roots"]]
                original_seal_root = campaign / "candidates" / CANDIDATE / "seal"
                old_boundary = self.boundary([original_stage, attempt_root, original_seal_root, *old_roots])
                before_calls = len(self.runtime.candidate_calls())
                before_count = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)["precise_total"]
                # 独立第二 transient 驱动可在首次 ar1 执行前指定另一 probe 的环境故障，不修改封存原件。
                run_argv = ["capture-candidate", "run", "--campaign-dir", campaign, "--candidate-id", CANDIDATE,
                            "--attempt-recovery", "ar1", "--acknowledge-live-requests"]
                _write(self.relay_control, {"bad_probe": first_recovery_fault})
                values.update(original_stage=original_stage, old_roots=old_roots, original_seal_root=original_seal_root, old_boundary=old_boundary, before_calls=before_calls, before_count=before_count, run_argv=run_argv)
                phase = 6
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 6:
                code, result, stderr = self.source_step(cursor, "recovery-capture", run_argv, campaign)
                self.assertIsNotNone(result, stderr)
                self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
                new_calls = self.runtime.candidate_calls()[before_calls:]
                executed = [row["probe"] for row in new_calls if not row["dry_run"]]
                expected = [row["probe_id"] for row in jobs[FAILED_JOB]["steps"] if row.get("argv")]
                self.assertCountEqual(executed, expected)
                segment = attempt_root / "recovery" / "ar1"
                summary = json.loads((segment / "attempt-recovery.json").read_bytes())
                self.assertEqual([row["id"] for row in summary["results"]], [FAILED_JOB])
                self.assertTrue(all("-recovery-ar1" in root for row in summary["results"] for root in row["evidence_roots"]))
                self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
                values.update(result=result, executed=executed, expected=expected, segment=segment, summary=summary)
                phase = 7
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 7:
                self.seal(campaign, attempt_id, recovery="ar1")
                stage_path = upgrade._stage_write_target(campaign, CANDIDATE, 1, "capture-candidate")
                with self.source_context():
                    stage = upgrade._load_stage_result(campaign, "capture-candidate", CANDIDATE, _baseline=1)
                self.assertTrue(stage_path.is_file())
                previous_stage = json.loads(original_stage.read_bytes())
                self.assertEqual(stage["client_evidence"], previous_stage["client_evidence"])
                self.assertEqual(set(stage["root_sets"]["dropped_roots"]), set(jobs[FAILED_JOB]["evidence_roots"]))
                effective_path = campaign / stage["effective_results"]["path"]
                self.assertEqual(upgrade.file_sha256(effective_path), stage["effective_results"]["sha256"])
                effective = json.loads(effective_path.read_bytes())
                self.assertEqual((effective["evaluation_baseline"], effective["recovery_revision"]), (1, "ar1"))
                effective_jobs = {row["id"]: row for row in effective["results"]}
                self.assertEqual(effective_jobs, {**jobs, FAILED_JOB: summary["results"][0]})
                self.assertEqual(stage["results"], effective["results"])
                store, store_campaign = candidate_store.open_campaign_store(campaign, manifest)
                validation_attempts = [fact for fact in store.list_facts(store_campaign["campaign_id"])
                                       if fact["fact_kind"] == "validation_attempt_created"]
                self.assertEqual({fact["payload"]["attempt_id"] for fact in validation_attempts},
                                 {f"{attempt_id}-v1", f"{attempt_id}-ar1-v1"})
                self.assertNotEqual(stage["store"]["validation_attempt_ref"], previous_stage["store"]["validation_attempt_ref"])
                execution = store.load_object(stage["store"]["validation_execution_plan_ref"])["payload"]
                self.assertEqual(set(execution["execute_item_ids"]) & set(jobs), {FAILED_JOB})
                self.assertEqual(set(execution["reuse_item_ids"]) & set(jobs), set(jobs) - {FAILED_JOB})
                self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
                values.update(stage_path=stage_path, stage=stage, previous_stage=previous_stage, effective=effective)
                phase = 8
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 8:
                code, _accounted, stderr = self.source_step(cursor, "recovery-account", ["account-sealed-candidate", "--campaign-dir", campaign,
                    "--candidate-id", CANDIDATE, "--attempt-recovery", "ar1"], campaign)
                self.assertEqual(code, 0, stderr)
                receipt = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)
                self.assertEqual(receipt["status"], "complete")
                # 离线增量核算只接中继运行根；Job 根里的网关恢复报告须由上面的 Campaign provenance 闭包验证。
                new_probes = [probe for row in summary["results"] for root in row["evidence_roots"]
                              for probe in Path(root).iterdir() if (probe / "relay-manifest.json").is_file()]
                self.assertCountEqual([probe.name for probe in new_probes], expected)
                delta_receipt = provenance.collect_root_provenance(new_probes)
                self.assertEqual(delta_receipt["unresolved_roots"], [])
                delta = delta_receipt["precise_total"]
                self.assertGreater(delta, 0)
                self.assertEqual(receipt["precise_total"] - before_count, delta)
                values.update(receipt=receipt, delta=delta)
                phase = 9
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 9:
                if evaluate_recovery:
                    code, passed, stderr = self.dispatch_builder(context, context["next_sequence"], baseline=1)
                    self.assertIsNotNone(passed, stderr)
                    self.assertEqual((code, passed["campaign_run"]["reason"]), (0, "queue-complete"), stderr)
                    index = json.loads((upgrade._stage_write_target(campaign, CANDIDATE, 1, "assertions") / "evaluation-run.json").read_bytes())
                    self.assertEqual(index["evaluation_baseline"], 1)
                    self.assertTrue(all(row["status"] == "pass" for row in index["rules"]))
                phase = 10
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 10:
                if 'count' not in values:
                    count, calls = receipt["precise_total"], len(self.runtime.candidate_calls())
                    values.update(count=count, calls=calls)
                    world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
                code, reused, stderr = self.source_step(cursor, "repeat-capture", run_argv, campaign)
                self.assertEqual((code, reused["reused"], reused["live_request_count"]), (2, True, 0), stderr)
                self.assertEqual(len(self.runtime.candidate_calls()), calls)
                self.assertEqual(provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)["precise_total"], count)
                values.update(count=count, calls=calls)
                phase = 11
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 11:
                if 'replay_boundary' not in values:
                    replay_boundary = self.boundary([segment, stage_path.parent, candidate_store.campaign_store_root(campaign)])
                    values.update(replay_boundary=replay_boundary)
                    world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
                code, resealed, stderr = self.source_step(cursor, "repeat-seal", [
                    "capture-candidate", "seal", "--campaign-dir", campaign, "--candidate-id", CANDIDATE,
                    "--attempt-id", attempt_id, "--attempt-recovery", "ar1", "--approve-seal-sha256",
                    stage["seal"]["review_sha256"], "--approved-by", "连续链审核",
                ], campaign)
                self.assertIsNotNone(resealed, stderr)
                self.assertEqual((code, resealed["status"], resealed["reused"]), (0, "complete", True), stderr)
                self.assertEqual(self.boundary([segment, stage_path.parent, candidate_store.campaign_store_root(campaign)]), replay_boundary)
                self.assertEqual(len(self.runtime.candidate_calls()), calls)
                self.assertEqual(provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)["precise_total"], count)
                self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
                self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")
                self.assert_campaign_request_accounting(campaign, expected_client_requests=2)
                values.update(replay_boundary=replay_boundary)
                phase = 12
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            if phase == 12:
                audit.check_trusted()
                audit.check_closure(executed=[row['id'] for row in summary['results']], expected=[FAILED_JOB],
                    reused=recovery['reuse_jobs'], expected_reused=sorted(set(jobs) - {FAILED_JOB}), what='恢复段 J*', source=audit.file_closure_source(segment / 'attempt-recovery.json',
                        ('results',), ('reuse_jobs',), row_key='id', reused_path=baseline_root / 'recovery.json'))
                audit.check_accounting(precise_delta=delta)
                audit.check_elapsed()
                audit.check_history()
                audit.check_entry(recovered, 'compile-and-run-vc-batch', 'VC-5')
                context['records'].append(audit.report(campaign_id=campaign.name, candidate_id=CANDIDATE,
                    candidate_revision=1, evaluation_baseline=1, executed_probes=executed, live_request_delta=delta))
                world_fixture.refresh_world(self, context)
                context.update(original_attempt_root=attempt_root, original_attempt=attempt, attempt_id=attempt_id,
                    recovery_revision='ar1', recovery_root=segment, recovery_summary=summary, baseline_recovery=recovery,
                    old_boundary=old_boundary, relay_control=self.relay_control)
                phase = 13
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, parameters=parameters, campaign=campaign)
            return context

    def run_transient_control_delivery_chain(self) -> dict[str, Any]:
        """原 A 组主方法续接已有世界；后段各场景仍由原父类继续。"""

        cursor = 'transient-control-delivery-chain'
        with client_fixture.source_scope():
            saved = world_fixture.resume_checkpoint(self, cursor)
            if saved is None:
                if os.environ.get(world_fixture.METHOD_RESUME_ENV):
                    raise ValueError('transient 后段原方法缺少交接游标，禁止推导续接位置')
                saved = {'phase': 'transient'}
                world_fixture.checkpoint(self, cursor, **saved)
            if saved['phase'] == 'unstarted':
                saved = {'phase': 'transient'}
                world_fixture.checkpoint(self, cursor, **saved)
            if saved['phase'] == 'transient':
                world = self.run_transient_chain(evaluate_recovery=False)
                world_fixture.save_world(cursor, world)
                saved = {'phase': 'late', 'campaign': world['campaign']}
                world_fixture.checkpoint(self, cursor, **saved)
            else:
                world = world_fixture.restore_world(self, cursor)
            if saved['phase'] == 'late':
                records = self.exercise_control_and_delivery_faults(world, sequence_start=world['next_sequence'])
                world['records'].extend(records)
                self.assertEqual({row['campaign_id'] for row in world['records']}, {world['campaign'].name})
                world_fixture.save_world(cursor, world)
                world_fixture.checkpoint(self, cursor, phase='complete', campaign=world['campaign'])
            return world

    def revalidate_completed_method(self, expected: list[str]) -> list[dict[str, Any]]:
        """成功原方法只重验原审计与原世界；输出统一交给原 runner 写入。"""

        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        own = '候选 transient 裁定与恢复段续跑'
        records = []
        with client_fixture.source_scope():
            if own in expected:
                saved = world_fixture.resume_checkpoint(self, 'transient-chain')
                if saved is None or saved['phase'] != 13:
                    raise ValueError('transient 原方法未形成完整终态游标')
                world_fixture.restore_world(self, 'transient-chain')
                with self.source_context():
                    records.append(ScenarioAudit.revalidate(self, own))
            remaining = [name for name in expected if name != own]
            if remaining:
                records.extend(super().revalidate_completed_method(remaining))
        by_name = {record['scenario']: record for record in records}
        if set(by_name) != set(expected):
            raise ValueError('transient 原方法审计集合与原声明不同')
        return [by_name[name] for name in expected]
