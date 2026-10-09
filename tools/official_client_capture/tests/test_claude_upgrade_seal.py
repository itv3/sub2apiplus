"""Claude 官方取证 seal 链（Claude 升级控制面设计 B2-07）。

验收（设计 B2-07）：批准步骤中途 SIGKILL 后重跑收敛、Store 事实数不增、``status`` 为 ``evidence_recorded``。全部零请求：

* 采集路径端到端：B2-06 的 ``capture-official run`` 真跑（替身 relay 按 2.1.226 实测连接序列写完整的 FW-E 中继证据目录，
  监督器、权限收口、checkpoint 与 attempt 收据都是真实代码），再走 seal 四阶段：未决发现项让闭合门禁 blocked、显式处置
  后预览可批准；批准在 ``discovery-record`` 之后与 ``evidence-record`` 之后各被 SIGKILL 一次（fork 子进程自杀），重跑收敛到
  2 条事实、``evidence_recorded``、阶段结果与 VC-1 checkpoint；再重跑逐字复用。Store 按宿主数据根做外部证据重放。
* 标签错位（有效连接顺序与证据标签不一致）在预览阶段即让闭合门禁 blocked，不能批准。
* 只读复用路径（B2-09 挪来）：写本 Campaign 的 Discovery／Evidence（引用导入包）、阶段结果与 VC-1 checkpoint，补「VC-0
  完成、VC-1 开始、VC-1 完成」三条计时账本事件；冲突预检在任何 Store 写入之前拒绝；SIGKILL 后重跑收敛，事件不重复。
* 官方阶段的封存后读侧不变（B4-02 收尾）：编排器服务面 ``_capture_stage_attempt_context`` 对官方阶段照旧重放 run 末收口，
  不走候选的封存证据清单读侧，官方 seal 不写那份清单。
* evidence 闭包涉及的模块集合锁定：只有编排器四个 evidence 根、``claude_upgrade_evidence``、发现补全
  ``claude_upgrade_discovery``（B2-07 补充项）、``claude_upgrade_plan`` 的标签加载与匹配、``canonical`` 三个函数与
  ``ControlError``，加整文件的 ``claude_target_inventory``、``contracts`` 与两个 FW-E 历史工具（语义候选目录与归并词表、
  非流量结构结论闭集）；seal 控制流程、Store 写入与 ``load_campaign_manifest`` 都不在其中。
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal_module
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_discovery as discovery_module
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import claude_upgrade_seal as seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_project_ledger_fixture
from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
from tools.official_client_capture.tests import test_claude_upgrade_predecessor as predecessor_tests
from tools.official_client_control.contracts import derive_identity_conditions
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
TARGET = capture_tests.TARGET
CAMPAIGN_ID = "claude-code-2_1_285-t1"
SEQUENCES = TOOL_ROOT / "tests" / "fixtures" / "claude_2_1_226_relay_connection_sequences.json"
# 7 个 Job、42 个 probe：合起来覆盖全部 40 条必需规则，含合成 probe、两个 pcap 标签、两个 debug 日志标签与带握手前断开连接的后台 probe。
SEAL_JOBS = (
    "official-transport-baseline",
    "official-header-conditions",
    "official-body-context-cache",
    "official-tools",
    "official-tui",
    "official-synthetic-stream",
    "official-agency-session",
)
MODEL_REQUESTS = {"POST /v1/messages", "POST /v1/messages/count_tokens", "POST /v1/messages/batches"}
# 非主模型的模型能力取证 Job（B2-07 补充项）：裁剪到基础 probe 在 SEAL_JOBS 里由主模型执行的步骤。
MODEL_JOB = "official-model-capability-claude-opus-5"


def model_capability_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    """SEAL_JOBS 加裁剪过的 ``MODEL_JOB``：返回 (场景清单, 证据标签)，两者按同一组步骤闭合（标签只留保留步骤的规则）。"""

    payload = capture_tests._reduced_payload({*SEAL_JOBS, MODEL_JOB})
    primary = {step["probe_id"] for job in payload["capture_jobs"] if job["id"] != MODEL_JOB for step in job["steps"]}
    job = next(item for item in payload["capture_jobs"] if item["id"] == MODEL_JOB)
    job["steps"] = [step for step in job["steps"] if plan.step_relay_probe(step["probe_id"], step["argv"]) in primary]
    kept = {step["probe_id"] for step in job["steps"]}
    for scenario in payload["evidence_scenarios"]:
        if scenario["scenario_id"] == plan.MODEL_CAPABILITY_SCENARIO:
            scenario["probe_ids"] = [probe for probe in scenario["probe_ids"] if probe in kept]
    labels = json.loads(plan.evidence_labels_path(TARGET).read_text(encoding="utf-8"))
    labels["entries"] = [entry for entry in labels["entries"] if entry["job_id"] in {*SEAL_JOBS, MODEL_JOB}]
    for entry in labels["entries"]:
        if entry["job_id"] == MODEL_JOB:
            entry["rules"] = [rule for rule in entry["rules"] if rule["labels"]["probe"] in kept]
    return payload, labels

FULL_FAKE_RELAY = r'''#!/usr/bin/env python3
"""claude_fw_e_relay.py 的离线替身（B2-07）：按 2.1.226 实测连接序列写一份完整的 FW-E 中继证据目录，不联网。"""
import hashlib, json, os, sys
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
probe = value("--probe-id")
output = value("--output-root")
record = json.load(open(os.environ["FAKE_RELAY_SEQUENCES"], encoding="utf-8"))["probes"][probe]
mutations = json.load(open(os.environ["FAKE_RELAY_MUTATIONS"], encoding="utf-8")).get(probe, [])
receipt = value("--host-runtime-receipt")
digest = hashlib.sha256(open(receipt, "rb").read()).hexdigest()
if digest != value("--host-runtime-receipt-sha256"):
    print("receipt mismatch", file=sys.stderr)
    sys.exit(2)
connections = [dict(item) for item in record["connections"]]
if "swap_first_requests" in mutations:
    connections[0]["requests"], connections[1]["requests"] = connections[1]["requests"], connections[0]["requests"]
live = "--acknowledge-live-requests" in args
oauth_only = all(line.startswith("POST /v1/oauth/") for item in connections for line in item["requests"])
host = "platform.claude.com" if oauth_only else "api.anthropic.com"
os.makedirs(os.path.join(output, "relay"), mode=0o700)
os.makedirs(os.path.join(output, "results"), mode=0o700)
rows, inventory = [], []
for item in connections:
    number = item["connection_id"]
    stream = b""
    for line in item["requests"]:
        method, path = line.split(" ", 1)
        target = path + ("?beta=true" if path.startswith("/v1/messages") else "")
        body = json.dumps({"model": "claude-sonnet-5", "stream": True}).encode() if method == "POST" else b""
        inventory.append({"connection_id": number, "stream_offset": len(stream), "method": method,
                          "request_target": target, "http_version": "HTTP/1.1"})
        stream += f"{method} {target} HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    with open(os.path.join(output, "relay", f"conn{number:03d}.client_to_upstream.bin"), "wb") as handle:
        handle.write(stream)
    rows.append({"connection_id": number, "valid": True, "intervention": item["intervention"]})
excluded = []
for number in record["excluded_connection_ids"]:
    # 与中继真实的客户端握手前重置记录逐字段同形（零字节、零摘要、零分段，valid 为假）。
    rows.append({"connection_id": number, "client_alpn_offer": ["http/1.1"], "alpn_source": "assumed", "bytes": {}, "sha256": {},
                 "segments": [], "opened_at_unix_ms": 1, "closed_at_unix_ms": 2, "error": "ConnectionResetError: ", "valid": False})
    excluded.append({"connection_id": number, "reason": "client_reset_transport_before_tls_handshake",
                     "manifest_error": "ConnectionResetError: "})
rows.sort(key=lambda row: row["connection_id"])
with open(os.path.join(output, "relay", "relay.json"), "w", encoding="utf-8") as handle:
    json.dump({"schema_version": "byte-relay/v1", "connections": rows}, handle)
if not live:
    with open(os.path.join(output, "relay", "intervention.jsonl"), "w", encoding="utf-8") as handle:
        for row in rows:
            if row.get("intervention"):
                handle.write(json.dumps({"connection_id": row["connection_id"], "action": row["intervention"]}) + "\n")
with open(os.path.join(output, "tls-clienthello.pcap"), "wb") as handle:
    handle.write(b"\xd4\xc3\xb2\xa1synthetic-client-hello")
with open(os.path.join(output, "results", "debug.log"), "w", encoding="utf-8") as handle:
    handle.write("synthetic debug log\n")
manifest = {
    "schema_version": "claude-code-fw-e-relay-run/v1", "probe_id": probe, "model": value("--model"), "status": "complete",
    "client": {"version": value("--expected-version") + " (Claude Code)", "sha256": value("--expected-sha256")},
    "fw_f_v5_probe": value("--fw-f-v5-probe"), "fw_f_v4_probe": value("--fw-f-v4-probe"), "response_plan": record["response_plan"],
    "runtime": {"runtime_image_reference": value("--runtime-image"),
                "host_runtime_receipt": {"sha256": digest, "run_nonce": value("--run-nonce")}},
    "upstream": {"host": host, "live_requests": live},
    "cleanup": {"relay_stopped": True, "hosts_restored": True}, "secret_scan": {"passed": True},
    "m_binding": {"complete": True},
    "relay_integrity": {"result": "passed", "excluded_connections": excluded},
    "dimension_evidence": {"actual_wire_inventory": inventory},
}
with open(os.path.join(output, "relay-manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle)
sys.exit(0)
'''

# 目标 SinkInventory：一个精确 AST 调用（归并语义候选）、一个被 containment 判为非流量的词法命中，加两个入口未决项。
TARGET_INVENTORY = {
    "schema_version": "claude-code-target-sink-inventory/v1",
    "target_version": TARGET,
    "sinks": [
        {
            "sink_id": "TN-SINK-0001-1", "source_kind": "ast_call", "category": "fetch_call", "module": "0000-cli.js",
            "source_start": 100, "source_end": 160, "owner_symbol": "sendMessages",
        },
        {
            "sink_id": "TN-LEX-0001", "source_kind": "lexical_only", "category": "lexical_candidate_messages_create",
            "module": "0004-docs.js", "source_start": 500, "source_end": 500, "owner_symbol": "Ugh",
        },
    ],
    "unresolved_entries": [
        {"unresolved_id": "UNRESOLVED-EDGE-0001", "kind": "dynamic_edge", "module": "0000-cli.js", "source_start": 1234, "edge_kind": "require"},
        {"unresolved_id": "UNRESOLVED-MODULE-0001", "kind": "module", "module": "0003-chunk.js", "reason": "no_confirmed_entry_path"},
    ],
}
DISPOSITIONS = {
    "schema_version": evidence.DISCOVERY_DISPOSITIONS_SCHEMA,
    "target_version": TARGET,
    "items": [
        {"discovery_id": "UNRESOLVED-EDGE-0001", "disposition": "out_of_scope_proven", "rationale": "加载目标由运行证据确认只指向已登记模块。"},
        {"discovery_id": "UNRESOLVED-MODULE-0001", "disposition": "catalogued_context", "rationale": "模块只含 UI 资源，不产生出站请求。"},
    ],
}


def sink_containment(inventory_path: Path) -> dict[str, Any]:
    """对 ``TARGET_INVENTORY`` 的 containment 证据：绑定目标清单文件的摘要，精确调用带调用原文，词法命中判为非流量。"""

    return {
        "schema_version": discovery_module.CONTAINMENT_SCHEMA,
        "target_version": TARGET,
        "inventory": {"path": "analysis/target-inventory.json", "sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest()},
        "completeness": {"target_sink_count": 2, "evidence_row_count": 2, "unmatched_sink_ids": [], "result": "passed"},
        "evidence": [
            {
                "sink_id": "TN-SINK-0001-1", "source_kind": "ast_call", "category": "fetch_call", "module": "0000-cli.js",
                "structural_finding": "exact_ast_call",
                "call": {"excerpt": 'fetch(`${base}/v1/messages`,{method:"POST",headers:h})', "callee_tail": ["fetch"]},
            },
            {
                "sink_id": "TN-LEX-0001", "source_kind": "lexical_only", "category": "lexical_candidate_messages_create",
                "module": "0004-docs.js", "structural_finding": "non_executable_literal",
            },
        ],
    }


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def rewrite_manifest(campaign: Path, **updates: Any) -> dict[str, Any]:
    """改写测试 Campaign 的总计划并同步 ``campaign.sha256``（模拟建 Campaign 时的冻结结果）。"""

    manifest = json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))
    for key, value in updates.items():
        manifest[key] = value
    raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    (campaign / "campaign.json").write_bytes(raw)
    (campaign / "campaign.json").chmod(0o600)
    (campaign / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
    return manifest


def write_vc_control(campaign: Path, *, deadline_at_utc: str = "2026-12-31T00:00:00Z") -> dict[str, Any]:
    """VC-0 收口产物的最小替身：VC 总计划与 VC-0 checkpoint，并把 ``vc_control.campaign_plan`` 绑定写进总计划。"""

    manifest = json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))
    vc_plan = vc_artifacts.build_campaign_plan(
        campaign_id=manifest["campaign_id"],
        campaign_mode=manifest["campaign_mode"],
        campaign_purpose=manifest["campaign_purpose"],
        baseline_version=manifest["baseline_version"],
        target_version=manifest["target_version"],
        created_at_utc=manifest["created_at_utc"],
        original_deadline_at_utc=deadline_at_utc,
        timing_checkpoint_sha256="1" * 64,
        arm64_environment_sha256="2" * 64,
        job_rehearsal_sha256="3" * 64,
        p0_gate_sha256="4" * 64,
    )
    plan_path = _write_json(campaign / "control" / "vc" / "campaign-plan.json", vc_plan)
    receipt = _write_json(campaign / "control" / "vc" / "vc0-closeout-receipt.json", {"result": "synthetic-vc0-closeout"})
    checkpoint = vc_artifacts.build_vc_checkpoint(
        campaign_plan=vc_plan,
        phase="VC-0",
        status="complete",
        predecessor_checkpoint=None,
        stage_receipt={"path": "control/vc/vc0-closeout-receipt.json", "sha256": codex_upgrade.file_sha256(receipt)},
        completed_at_utc="2026-10-07T00:10:00Z",
        execute_item_ids=[],
        reuse_item_ids=[],
        live_request_count=0,
        scanned_bytes=0,
    )
    _write_json(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-0"), checkpoint)
    return rewrite_manifest(
        campaign, vc_control={"campaign_plan": {"path": "control/vc/campaign-plan.json", "sha256": codex_upgrade.file_sha256(plan_path)}}
    )


def run_main(argv: list[str]) -> tuple[int, dict | None, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main(argv)
    text = stdout.getvalue()
    return code, (json.loads(text) if text.strip() else None), stderr.getvalue()


def run_killed(argv: list[str], *, kill_after: str, environment: dict[str, str] | None = None) -> int:
    """fork 一个子进程跑 ``argv``，在 Store 命令 ``kill_after`` 返回后自杀（SIGKILL）；返回 waitpid 状态。

    子进程继承父进程当时的全部替身（fork 语义），与真实硬杀一样不执行任何清理。
    """

    real = seal.store_cli
    pid = os.fork()
    if pid == 0:  # pragma: no cover - 子进程
        try:
            if environment:
                os.environ.update(environment)

            def killing(command: list[Any]) -> Any:
                result = real(command)
                if str(command[0]) == kill_after:
                    os.kill(os.getpid(), signal.SIGKILL)
                return result

            seal.store_cli = killing
            with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                claude_upgrade.main(argv)
        finally:
            os._exit(99)
    _pid, status = os.waitpid(pid, 0)
    return status


def facts_by_kind(store_root: Path, campaign_id: str) -> collections.Counter:
    return collections.Counter(fact["fact_kind"] for fact in ControlStore(store_root).list_facts(campaign_id))


def store_status(store_root: Path, campaign_id: str) -> str:
    return str(seal.store_cli(["status", "--store", store_root, "--campaign", campaign_id])["checkpoint"])


class EvidenceClosureTests(unittest.TestCase):
    """evidence 根的闭包保持小：改 seal 控制流程、Store 写入或总计划读取不会被判成 evidence 变化。"""

    def test_evidence_closure_module_set_is_locked(self) -> None:
        policy = claude_identity.load_policy()
        digests = claude_identity.managed_tree_digests(REPOSITORY_ROOT, policy)
        index = claude_identity._module_index(policy, digests)
        roots = claude_identity._orchestrator_roots(policy, index, policy["orchestrator"]["evidence_roots"])
        closure = claude_identity.closure(policy, REPOSITORY_ROOT, digests, roots, layer="evidence_semantics")
        by_module: dict[str, set[str]] = collections.defaultdict(set)
        for item in closure["symbols"]:
            by_module[item["module"]].add(item["name"])
        capture_package = "tools.official_client_capture."
        control_package = "tools.official_client_control."
        self.assertEqual(
            set(by_module),
            {
                capture_package + "claude_upgrade",
                capture_package + "claude_upgrade_discovery",
                capture_package + "claude_upgrade_evidence",
                capture_package + "claude_upgrade_plan",
                control_package + "canonical",
                control_package + "errors",
            },
        )
        self.assertEqual(
            {item["module"] for item in closure["modules"]},
            {
                # Mac v2 收据的正式封存读侧已经调用此完整合同；新增项必须显式登记。
                capture_package + "claude_client_receipt_contract",
                capture_package + "claude_fw_e_dispositions",
                capture_package + "claude_fw_e_validation_closure",
                capture_package + "claude_target_inventory",
                control_package + "contracts",
            },
        )
        discovery_symbols = by_module[capture_package + "claude_upgrade_discovery"]
        self.assertTrue({"build_discovery_layers", "load_historical_baseline", "target_discovery_items"} <= discovery_symbols)
        # 冻结历史基线的生成命令只在一次性冻结时用，不在 evidence 闭包里。
        self.assertFalse({"build_historical_baseline", "main"} & discovery_symbols)
        self.assertEqual(
            by_module[capture_package + "claude_upgrade"],
            {"scan_evidence", "_evidence_inventory", "_finalize_scenario_receipt", "_seal_capture_attempt"},
        )
        self.assertEqual(by_module[control_package + "canonical"], {"canonical_json_bytes", "canonical_sha256", "sha256_bytes"})
        plan_symbols = by_module[capture_package + "claude_upgrade_plan"]
        self.assertTrue({"load_evidence_labels", "match_relay_connections", "LabelSequenceMismatch"} <= plan_symbols)
        # 场景展开与 wire 根不进 evidence 闭包（标签校验只读冻结的场景清单对象）
        for name in ("read_scenario_manifest", "expand_scenario_jobs", "plan_jobs", "plan_context", "bind_step_runtime"):
            self.assertNotIn(name, plan_symbols)
        evidence_symbols = by_module[capture_package + "claude_upgrade_evidence"]
        self.assertTrue({"build_seal_drafts", "scan_relay_evidence", "evidence_inventory", "relay_probe_view"} <= evidence_symbols)
        for path in ("claude_upgrade_evidence.py", "claude_upgrade_discovery.py", "claude_upgrade_seal.py", "claude_upgrade_vc.py"):
            self.assertEqual(claude_identity.classify_path(policy, f"official_client_capture/{path}"), "control")
        # 冻结的历史候选基线是 evidence 层整文件：内容变化即 evidence 语义变化。
        self.assertEqual(
            claude_identity.classify_path(policy, f"official_client_capture/{discovery_module.BASELINE_FILENAME}"), "evidence_semantics"
        )
        self.assertEqual(claude_identity.compute_identity(policy, REPOSITORY_ROOT)["defaulted_paths"], [])

    def test_finalize_scenario_receipt_fails_closed(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "不登记场景真实性收据"):
            claude_upgrade._finalize_scenario_receipt()


class RelayViewTests(unittest.TestCase):
    """中继证据视图：标签匹配前剔除握手前断开的连接（含 relay.json 记 valid: true 的 live 空握手）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()

    def write_probe(
        self,
        *,
        requests_on: dict[int, list[str]],
        connections: list[dict],
        excluded: list[int],
        name: str = "v4-background",
        job: str = "job",
        manifest: dict | None = None,
    ) -> Path:
        probe = self.base / "runs" / job / name
        (probe / "relay").mkdir(parents=True)
        inventory = []
        for number, lines in requests_on.items():
            (probe / "relay" / f"conn{number:03d}.client_to_upstream.bin").write_bytes(b"x")
            for offset, line in enumerate(lines):
                method, path = line.split(" ", 1)
                inventory.append({"connection_id": number, "stream_offset": offset, "method": method, "request_target": path + "?beta=true"})
        _write_json(probe / "relay" / "relay.json", {"schema_version": "byte-relay/v1", "connections": connections})
        _write_json(
            probe / "relay-manifest.json",
            {
                "schema_version": evidence.RELAY_MANIFEST_SCHEMA,
                "probe_id": "v4-background",
                **(manifest or {}),
                "relay_integrity": {"result": "passed", "excluded_connections": [{"connection_id": item, "reason": "r"} for item in excluded]},
                "dimension_evidence": {"actual_wire_inventory": inventory},
            },
        )
        return probe

    def test_excluded_connections_are_dropped_before_matching(self) -> None:
        probe = self.write_probe(
            requests_on={1: ["HEAD /api/hello"], 4: ["POST /v1/messages"]},
            connections=[
                {"connection_id": 1, "valid": True},
                {"connection_id": 2, "valid": True},  # live 空握手：valid 为真但中继清单判为握手前断开
                {"connection_id": 3, "valid": False},  # 客户端握手前重置
                {"connection_id": 4, "valid": True, "intervention": None},
            ],
            excluded=[2, 3],
        )
        view = evidence.relay_probe_view(probe, base=self.base)
        self.assertEqual([item["connection_id"] for item in view["connections"]], [1, 4])
        self.assertEqual(view["connections"][1]["requests"], ["POST /v1/messages"])
        self.assertEqual(view["excluded_connection_ids"], [2, 3])
        self.assertEqual(view["directory"], "runs/job/v4-background")
        scanned = evidence.scan_relay_evidence([probe.parent], "official", base=self.base)
        self.assertEqual(list(scanned["probes"]), ["v4-background"])

    def test_requests_on_dropped_connections_fail_closed(self) -> None:
        probe = self.write_probe(
            requests_on={2: ["POST /v1/messages"]},
            connections=[{"connection_id": 2, "valid": True}],
            excluded=[2],
        )
        with self.assertRaisesRegex(evidence.SealEvidenceError, "请求落在无效或握手前断开的连接上"):
            evidence.relay_probe_view(probe, base=self.base)

    def test_model_capability_step_identity(self) -> None:
        """非主模型（模型能力取证，B2-07 补充项）的目录名是「<模型>-<基础 probe>」，中继清单记基础 probe 与模型：扫描按目录
        名登记步骤身份、基础 probe 留在 relay_probe_id；目录名的模型与中继清单不符即失败关闭；逐 probe 复核认同一规则。"""

        step = "claude-opus-5-v4-background"
        relay = {"model": "claude-opus-5", "fw_f_v5_probe": "v4-background", "fw_f_v4_probe": None}
        probe = self.write_probe(
            requests_on={1: ["HEAD /api/hello"]}, connections=[{"connection_id": 1, "valid": True}], excluded=[], name=step, manifest=relay
        )
        scanned = evidence.scan_relay_evidence([probe.parent], "official", base=self.base)
        self.assertEqual(list(scanned["probes"]), [step])
        view = scanned["probes"][step]
        self.assertEqual((view["probe_id"], view["relay_probe_id"], view["manifest"]["model"]), (step, "v4-background", "claude-opus-5"))
        context = {"target_sha256": "a" * 64, "target_version": TARGET, "runtime_image": "image"}
        self.assertNotIn("probe_identity", evidence._probe_checks(view, probe=step, context=context, synthetic=False))
        # 同一视图当成别的步骤复核、或中继清单的 v5 probe 不是基础 probe：probe_identity 不通过。
        self.assertIn("probe_identity", evidence._probe_checks(view, probe="claude-fable-5-v4-background", context=context, synthetic=False))
        broken = {**view, "manifest": {**view["manifest"], "fw_f_v5_probe": step}}
        self.assertIn("probe_identity", evidence._probe_checks(broken, probe=step, context=context, synthetic=False))
        # 目录名的模型与中继清单记的模型不同、或中继清单缺模型：扫描失败关闭。
        for index, manifest in enumerate(({**relay, "model": "claude-fable-5"}, {**relay, "model": None})):
            with self.subTest(model=manifest["model"]):
                other = self.write_probe(
                    requests_on={1: ["HEAD /api/hello"]}, connections=[{"connection_id": 1, "valid": True}], excluded=[],
                    name=step, job=f"other-{index}", manifest=manifest,
                )
                with self.assertRaisesRegex(evidence.SealEvidenceError, "probe 目录名与中继清单的 probe_id 不一致"):
                    evidence.scan_relay_evidence([other.parent], "official", base=self.base)


class _SealCampaignCase(capture_tests.CaptureFixture):
    """B2-06 的取证夹具（临时数据根、替身 relay、监督器真跑）加 seal 需要的冻结输入、Store 规格与 VC-0 产物。"""

    tree: ManagedToolTree

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tree_directory = tempfile.TemporaryDirectory()
        cls.tree = ManagedToolTree(Path(cls._tree_directory.name) / "repo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tree_directory.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        relay = self.tool_copy / "claude_fw_e_relay.py"
        relay.write_text(FULL_FAKE_RELAY, encoding="utf-8")
        relay.chmod(0o700)
        self.mutations_file = self.root / "relay-mutations.json"
        self.mutations_file.write_text("{}", encoding="utf-8")
        self.sequences = json.loads(SEQUENCES.read_text(encoding="utf-8"))

    @contextlib.contextmanager
    def seal_patched(self):
        with self.patched(), mock.patch.dict(
            os.environ, {"FAKE_RELAY_SEQUENCES": str(SEQUENCES), "FAKE_RELAY_MUTATIONS": str(self.mutations_file)}
        ):
            yield

    def build_campaign(self, inputs: tuple[dict[str, Any], dict[str, Any]] | None = None) -> Path:
        """``inputs``：(场景清单, 证据标签)；缺省是 SEAL_JOBS 裁剪的真实清单与对应的标签条目。"""

        payload = inputs[0] if inputs is not None else capture_tests._reduced_payload(set(SEAL_JOBS))
        # Store Campaign v2 规格：合成 persona 与官方产物，冻结身份与总计划同源（B2-11 建 Campaign 时冻结同一份）。
        scratch = self.root / "store-build"
        scratch.mkdir()
        fixture = SyntheticCampaign(scratch, schema="v2")
        fixture.campaign_id = CAMPAIGN_ID
        fixture.bootstrap_and_campaign(
            target_version=TARGET,
            platforms=["linux/arm64"],
            entrypoints=["claude-cli"],
            default_conditions=["privacy=default"],
            campaign_purpose="validation_only",
            absolute_deadline_utc="2026-12-31T00:00:00Z",
        )
        derived = derive_identity_conditions(fixture.campaign_document)[0]
        campaign = self.write_campaign(
            payload,
            campaign_id=CAMPAIGN_ID,
            configuration={key: derived[key] for key in ("entrypoint", "privacy_mode", "oauth_org_id", "model_catalog_sha256")},
        )
        if inputs is not None:
            labels = inputs[1]
        else:
            labels = json.loads(plan.evidence_labels_path(TARGET).read_text(encoding="utf-8"))
            labels["entries"] = [entry for entry in labels["entries"] if entry["job_id"] in SEAL_JOBS]
        frozen = {
            "evidence_labels": _write_json(campaign / "inputs" / "evidence-labels.json", labels),
            "store_bootstrap": _write_json(
                campaign / "inputs" / "store-bootstrap.json", fixture.store.load_object(fixture.references["bootstrap"])["payload"]
            ),
            "store_campaign": _write_json(campaign / "inputs" / "store-campaign.json", fixture.campaign_document),
        }
        manifest = json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))
        inputs = dict(manifest["inputs"])
        for name, path in frozen.items():
            inputs[name] = {"path": path.relative_to(campaign).as_posix(), "sha256": codex_upgrade.file_sha256(path)}
        rewrite_manifest(campaign, inputs=inputs)
        write_vc_control(campaign)
        # 合成 Store 的外部文件（官方产物、bootstrap 来源）放进宿主数据根：Store 外部证据重放以数据根为 external root。
        shutil.copytree(fixture.external_root, self.data, dirs_exist_ok=True)
        self.inventory_path = _write_json(campaign / "analysis" / "target-inventory.json", TARGET_INVENTORY)
        self.containment_path = _write_json(campaign / "analysis" / "sink-containment.json", sink_containment(self.inventory_path))
        self.dispositions_path = _write_json(campaign / "analysis" / "discovery-dispositions.json", DISPOSITIONS)
        return campaign

    def run_capture(self, campaign: Path) -> str:
        with self.seal_patched():
            code, result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
        self.assertEqual((code, (result or {}).get("status")), (2, "awaiting_receipts"), stderr)
        return str(result["attempt_id"])

    def seal_argv(self, campaign: Path, attempt_id: str, *extra: str, dispositions: bool = True) -> list[str]:
        argv = [
            "capture-official", "seal", "--campaign-dir", str(campaign), "--attempt-id", attempt_id,
            "--target-inventory", str(self.inventory_path), "--sink-containment", str(self.containment_path),
        ]
        if dispositions:
            argv += ["--discovery-dispositions", str(self.dispositions_path)]
        return [*argv, *extra]

    def expected_live_requests(self) -> int:
        """B1-11 口径：客户端发往 api.anthropic.com 的模型端点 POST（不论合成与否），OAuth refresh 不计。"""

        payload = capture_tests._reduced_payload(set(SEAL_JOBS))
        probes = [step["probe_id"] for job in payload["capture_jobs"] for step in job["steps"]]
        return sum(
            1
            for probe in probes
            for connection in self.sequences["probes"][probe]["connections"]
            for line in connection["requests"]
            if line in MODEL_REQUESTS
        )


class CapturedSealChainTests(_SealCampaignCase):
    """采集路径四阶段：闭合门禁、批准中途两次 SIGKILL 后重跑收敛、再重跑逐字复用、Store 外部证据重放。"""

    def test_four_stage_seal_converges_after_sigkill(self) -> None:
        campaign = self.build_campaign()
        attempt_id = self.run_capture(campaign)
        store_root = claude_upgrade_store.campaign_store_root(campaign)
        with self.seal_patched():
            # 默认重放权限收口（B2-07 落实），收口参数与 Campaign 宿主数据根坐标一致。
            attempt_root, attempt = claude_upgrade._load_capture_attempt(campaign, "official", None, attempt_id)
            forged = json.loads(json.dumps(attempt))
            forged["evidence_permission_layout"]["managed_data_root"] = str(self.root)
            with self.assertRaisesRegex(ConfigurationError, "收口参数与 Campaign 宿主数据根坐标不一致"):
                claude_upgrade._replay_attempt_evidence_permissions(attempt_root, forged)

            # ①～③ 未决发现项没有处置：预览照常生成，闭合门禁 blocked，批准被拒。
            code, blocked, stderr = run_main(self.seal_argv(campaign, attempt_id, dispositions=False))
            self.assertEqual((code, blocked["status"]), (2, "seal_blocked"), stderr)
            self.assertEqual(blocked["closure"]["reasons"], ["discovery_unclassified"])
            code, _result, stderr = run_main(
                self.seal_argv(campaign, attempt_id, "--approve-seal-sha256", blocked["review_sha256"], dispositions=False)
            )
            self.assertEqual(code, 1)
            self.assertIn("闭合门禁 blocked", stderr)

            # 显式处置后换代：新预览可批准；同代重跑逐字复用。
            code, preview, stderr = run_main(self.seal_argv(campaign, attempt_id))
            self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
            self.assertNotEqual(preview["generation"], blocked["generation"])
            self.assertEqual(run_main(self.seal_argv(campaign, attempt_id))[1]["review_sha256"], preview["review_sha256"])
            self.assertEqual(preview["live_request_count"], self.expected_live_requests())
            self.assertGreater(self.expected_live_requests(), 0)
            seal_dir = campaign / seal.SEAL_ROOT / preview["generation"]
            self.assertEqual(
                sorted(path.name for path in seal_dir.iterdir()),
                sorted([seal.CLIENT_CHECKPOINT_FILENAME, seal.DRAFTS_DIRNAME, seal.EVIDENCE_MANIFEST_FILENAME, seal.SEAL_DRAFT_FILENAME, seal.SEAL_PREVIEW_FILENAME]),
            )
            draft = json.loads((seal_dir / seal.SEAL_DRAFT_FILENAME).read_text(encoding="utf-8"))
            index = json.loads((seal_dir / seal.DRAFTS_DIRNAME / "capture-index.json").read_text(encoding="utf-8"))
            manifest_draft = json.loads((seal_dir / seal.DRAFTS_DIRNAME / "capture-manifest.json").read_text(encoding="utf-8"))
            discovery = json.loads((seal_dir / seal.DRAFTS_DIRNAME / "discovery-inventory.json").read_text(encoding="utf-8"))
            matrix = json.loads((seal_dir / seal.DRAFTS_DIRNAME / "cross-source-matrix.json").read_text(encoding="utf-8"))
            self.assertEqual((index["result"], index["failures"]), ("passed", []))
            self.assertEqual(manifest_draft["label_mismatches"], [])
            self.assertEqual(len(draft["evidence_items"]), 42)
            self.assertEqual(matrix["rule_gaps"], [])
            # 完整发现清单（B2-07 补充项）：目标 sink 两项、入口未决两项（已显式处置），加历史候选基线逐项重放。
            history = discovery_module.load_historical_baseline()
            by_id = {item["discovery_id"]: item for item in discovery["items"]}
            self.assertEqual(discovery["item_count"], 4 + len(history["items"]))
            self.assertEqual(
                {key: by_id[key]["disposition"] for key in ("TN-SINK-0001-1", "TN-LEX-0001", "UNRESOLVED-EDGE-0001", "UNRESOLVED-MODULE-0001")},
                {
                    "TN-SINK-0001-1": "mapped_semantic_candidate", "TN-LEX-0001": "out_of_scope_proven",
                    "UNRESOLVED-EDGE-0001": "out_of_scope_proven", "UNRESOLVED-MODULE-0001": "catalogued_context",
                },
            )
            self.assertIn("CAND-EP-FULLSET", by_id["TN-SINK-0001-1"]["semantic_candidate_ids"])
            self.assertTrue(all(by_id[item["discovery_id"]] == item for item in history["items"]))
            denominator = json.loads((seal_dir / seal.DRAFTS_DIRNAME / "candidate-denominator.json").read_text(encoding="utf-8"))
            semantic = json.loads((seal_dir / seal.DRAFTS_DIRNAME / "semantic-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(
                denominator["counts"],
                {"historical_rules": 57, "hitcc_documents_2_1_197": 71, "semantic_candidate_families": 32, "source_mechanisms_2_1_88": 102, "target_send_points": 1},
            )
            self.assertEqual(semantic["candidate_count"], 32)
            self.assertEqual(
                (draft["metrics"]["discovery_item_count"], draft["metrics"]["orthogonal_candidate_count"]),
                (discovery["item_count"], 263),
            )
            self.assertEqual(matrix["historical_baseline"], history["binding"])
            background = next(item for job in index["jobs"] for item in job["probes"] if item["probe_id"] == "v4-background")
            self.assertEqual(background["excluded_connection_ids"], self.sequences["probes"]["v4-background"]["excluded_connection_ids"])
            package = next(item for item in draft["seal"]["objects"] if item["name"] == "evidence_package")["payload"]
            self.assertEqual(package["schema_version"], evidence.EVIDENCE_PACKAGE_V3_SCHEMA)
            purposes = {entry["facts"]["purpose"] for item in draft["seal"]["objects"] if item["name"] == "completeness" for entry in item["payload"]["entries"]}
            # B2-11 的两份可缺绑定（静态分析收据、冻结的生产现状清单）只在 B2-11 建的 Campaign 里有，本夹具总计划没有。
            absent = {evidence.COMPLETENESS_PURPOSES[name] for name in ("static_analysis", "production_inventory")}
            self.assertEqual(purposes, set(evidence.COMPLETENESS_PURPOSES.values()) - absent)

            # ④ 批准：discovery-record 之后硬杀，再在 evidence-record 之后硬杀（这一次在父监督器上下文里），重跑收敛。
            approve = self.seal_argv(campaign, attempt_id, "--approve-seal-sha256", preview["review_sha256"])
            status = run_killed(approve, kill_after="discovery-record")
            self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
            self.assertEqual(facts_by_kind(store_root, CAMPAIGN_ID), {"discovery_recorded": 1})
            self.assertFalse((campaign / seal.STAGE_RESULT_RELATIVE).exists())
            producer = {
                "CODEX_UPGRADE_CAMPAIGN_RUN_ACTIVE": "1",
                "CODEX_UPGRADE_CAMPAIGN_RUN_DIR": "control/vc/runs/run-0001",
                "CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE": "a" * 64,
                "CODEX_UPGRADE_CAMPAIGN_ACTION_ID": "vc1-seal-approve",
            }
            status = run_killed(approve, kill_after="evidence-record", environment=producer)
            self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
            self.assertEqual(facts_by_kind(store_root, CAMPAIGN_ID), {"discovery_recorded": 1, "evidence_recorded": 1})
            self.assertFalse(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-1").exists())
            code, sealed, stderr = run_main(approve)
            self.assertEqual((code, sealed["status"]), (0, "complete"), stderr)
            self.assertEqual(sealed["store"]["facts_reused"], {"discovery_recorded": True, "evidence_recorded": True})
            self.assertEqual(sealed["store"]["checkpoint"], "evidence_recorded")
            self.assertEqual(store_status(store_root, CAMPAIGN_ID), "evidence_recorded")
            facts = ControlStore(store_root).list_facts(CAMPAIGN_ID)
            self.assertEqual(len(facts), 2)
            producers = {fact["fact_kind"]: fact["producer_run"] for fact in facts}
            self.assertIsNone(producers["discovery_recorded"])
            self.assertEqual(producers["evidence_recorded"], {"run_dir": "control/vc/runs/run-0001", "owner_nonce": "a" * 64, "action_id": "vc1-seal-approve"})

            # 阶段结果与 VC-1 checkpoint：attempt 绑定供 B2-06 的阶段读侧、checkpoint 递归重放到 VC-0。
            stage_path = campaign / seal.STAGE_RESULT_RELATIVE
            stage = json.loads(stage_path.read_text(encoding="utf-8"))
            self.assertEqual(stage["attempt"], {"path": f"official/attempts/{attempt_id}/attempt.json", "sha256": codex_upgrade.file_sha256(attempt_root / "attempt.json")})
            bound_root, _bound = capture._capture_stage_attempt_context(campaign, stage, phase="official", candidate_id=None)
            self.assertEqual(bound_root, attempt_root)
            # B4-02 收尾：编排器服务面对官方阶段照旧转到取证模块、重放 run 末收口，不走候选的封存证据清单读侧，官方 seal
            # 也不写那份清单。
            replay = claude_upgrade._replay_attempt_evidence_permissions
            with mock.patch.object(
                candidate_seal_module, "sealed_attempt_context", side_effect=AssertionError("官方阶段不得走候选封存后读侧")
            ), mock.patch.object(claude_upgrade, "_replay_attempt_evidence_permissions", wraps=replay) as replayed:
                orchestrated_root, _orchestrated = claude_upgrade._capture_stage_attempt_context(
                    campaign, stage, phase="official", candidate_id=None
                )
            self.assertEqual(orchestrated_root, attempt_root)
            self.assertEqual(replayed.call_count, 1)
            self.assertFalse((attempt_root / "evidence-manifest.json").exists())
            manifest = claude_upgrade.load_campaign_manifest(campaign)
            _path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(campaign, claude_upgrade_vc.vc_campaign_plan(campaign, manifest), "VC-1")
            self.assertEqual(checkpoint["execute_item_ids"], sorted(SEAL_JOBS))
            self.assertEqual(checkpoint["reuse_item_ids"], [])
            self.assertEqual(checkpoint["metrics"], {"live_request_count": self.expected_live_requests(), "scanned_bytes": preview["scanned_bytes"]})

            # 再重跑：逐字复用，事实数不增，文件不变。
            before = {path: codex_upgrade.file_sha256(path) for path in (stage_path, claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-1"))}
            code, again, stderr = run_main(approve)
            self.assertEqual((code, again["checkpoint_sha256"]), (0, sealed["checkpoint_sha256"]), stderr)
            self.assertEqual(len(ControlStore(store_root).list_facts(CAMPAIGN_ID)), 2)
            self.assertEqual({path: codex_upgrade.file_sha256(path) for path in before}, before)
            # 外部证据重放：证据项、完整性绑定与官方产物都按宿主数据根找得到、摘要一致。
            ControlStore(store_root).replay(external_root=self.data, require_external=True)
            # 已封存后：另一 attempt 不得覆盖，旧代预览的批准摘要被拒。
            code, _result, stderr = run_main(self.seal_argv(campaign, "attempt-other", "--approve-seal-sha256", preview["review_sha256"]))
            self.assertEqual(code, 1)
            self.assertIn("另一 attempt", stderr)

    def test_label_sequence_mismatch_blocks_seal(self) -> None:
        self.mutations_file.write_text(json.dumps({"v4-native-tls-baseline": ["swap_first_requests"]}), encoding="utf-8")
        campaign = self.build_campaign()
        attempt_id = self.run_capture(campaign)
        with self.seal_patched():
            code, preview, stderr = run_main(self.seal_argv(campaign, attempt_id))
            self.assertEqual((code, preview["status"]), (2, "seal_blocked"), stderr)
            self.assertIn("label_mismatch", preview["closure"]["reasons"])
            drafts = campaign / seal.SEAL_ROOT / preview["generation"] / seal.DRAFTS_DIRNAME
            mismatches = json.loads((drafts / "capture-manifest.json").read_text(encoding="utf-8"))["label_mismatches"]
            self.assertEqual([item["probe_id"] for item in mismatches], ["v4-native-tls-baseline"])
            self.assertIn("不一致", mismatches[0]["reason"])
            code, _result, stderr = run_main(self.seal_argv(campaign, attempt_id, "--approve-seal-sha256", preview["review_sha256"]))
            self.assertEqual(code, 1)
            self.assertIn("闭合门禁 blocked", stderr)
            self.assertEqual(facts_by_kind(claude_upgrade_store.campaign_store_root(campaign), CAMPAIGN_ID), {})


class ModelCapabilitySealTests(_SealCampaignCase):
    """非主模型的模型能力取证（B2-07 补充项）走同一条采集与 seal 链：中继跑基础 probe、清单记模型，步骤身份「<模型>-<基础
    probe>」贯穿 CaptureIndex、capture manifest 与证据项（Wire 模型能力目录按 EVIDENCE-<模型>-<probe> 取证）。"""

    def test_model_capability_probes_seal_with_step_identity(self) -> None:
        payload, labels = model_capability_inputs()
        model_probes = sorted(step["probe_id"] for job in payload["capture_jobs"] if job["id"] == MODEL_JOB for step in job["steps"])
        self.assertEqual(len(model_probes), 8)
        campaign = self.build_campaign((payload, labels))
        attempt_id = self.run_capture(campaign)
        with self.seal_patched():
            code, preview, stderr = run_main(self.seal_argv(campaign, attempt_id))
        self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
        seal_dir = campaign / seal.SEAL_ROOT / preview["generation"]
        draft = json.loads((seal_dir / seal.SEAL_DRAFT_FILENAME).read_text(encoding="utf-8"))
        index = json.loads((seal_dir / seal.DRAFTS_DIRNAME / "capture-index.json").read_text(encoding="utf-8"))
        manifest_draft = json.loads((seal_dir / seal.DRAFTS_DIRNAME / "capture-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((index["result"], index["failures"], manifest_draft["label_mismatches"]), ("passed", [], []))
        rows = {item["probe_id"]: item for job in index["jobs"] if job["job_id"] == MODEL_JOB for item in job["probes"]}
        self.assertEqual(sorted(rows), model_probes)
        items = {item["evidence_id"]: item for item in draft["evidence_items"]}
        self.assertEqual(len(items), 42 + len(model_probes))
        probe = "claude-opus-5-v4-replay-tui"
        self.assertEqual(items[f"EVIDENCE-{probe}"]["source_ids"], [plan.MODEL_CAPABILITY_SCENARIO])
        self.assertIn(f"probe={probe}", items[f"EVIDENCE-{probe}"]["applicability"])
        artifacts = [item for item in manifest_draft["artifacts"] if item["probe_id"] == probe]
        self.assertTrue(artifacts)
        for artifact in artifacts:
            self.assertEqual((artifact["job_id"], artifact["labels"]["probe"], artifact["labels"]["model"]), (MODEL_JOB, probe, "claude-opus-5"))
            self.assertIn(f"/{MODEL_JOB}/{probe}/", artifact["path"])
        relay_artifact = next(item for item in artifacts if item["kind"] == "relay_manifest")
        relay = json.loads((self.data / relay_artifact["path"]).read_text(encoding="utf-8"))
        self.assertEqual((relay["probe_id"], relay["fw_f_v5_probe"], relay["model"]), ("v4-replay-tui", "v4-replay-tui", "claude-opus-5"))


class PredecessorReuseSealTests(predecessor_tests._PredecessorCase):
    """只读复用路径：零请求封存、三条计时账本事件、冲突预检与 SIGKILL 后重跑收敛。"""

    def setUp(self) -> None:
        super().setUp()
        self.predecessor = self.make_predecessor(schema="v2", **predecessor_tests.SYNTHETIC_TARGET)
        self.predecessor_root = self.predecessor.store_root

    def prepare(self, name: str = "target") -> tuple[Path, Path]:
        """总计划冻结前序只读复用、带 VC-0 产物与 Claude 计时账本，并已导入前序证据（B2-09）。"""

        binding = self.binding(ControlStore.open_read_only(self.predecessor_root), predecessor_tests.PREDECESSOR_ID)
        campaign_dir, _fixture = self.make_target(name, predecessor=binding)
        write_vc_control(campaign_dir)
        # B2-08 起 seal 先过项目总账消费者门禁。
        claude_project_ledger_fixture.register_claude_campaign(campaign_dir)
        self.run_import(campaign_dir, self.predecessor_root, predecessor_tests.PREDECESSOR_ID, "read_only_reuse")
        ledger = self.workspace / f"{name}-ledger"
        timing.create_ledger(
            ledger,
            upgrade_id=f"{predecessor_tests.TARGET_ID}-{name}",
            baseline_version="0.9.0",
            target_version="1.0.0",
            campaign_purpose="validation_only",
            evidence_decision="reuse",
            client="claude",
        )
        return campaign_dir, ledger

    @contextlib.contextmanager
    def ledger_bound(self, ledger: Path):
        # 总计划 control_receipts.upgrade_timing 的绑定形状由 B2-11 修（B2-09 记录的已知缺陷），这里直接给出账本目录。
        with mock.patch.object(claude_upgrade, "_campaign_timing_ledger_dir", return_value=ledger.resolve()):
            yield

    def seal_argv(self, campaign_dir: Path) -> list[str]:
        return ["capture-official", "seal", "--campaign-dir", str(campaign_dir)]

    def test_reuse_seal_records_evidence_and_aligns_timing(self) -> None:
        campaign_dir, ledger = self.prepare()
        store_root = claude_upgrade_store.campaign_store_root(campaign_dir)
        with self.ledger_bound(ledger):
            code, result, stderr = run_main(self.seal_argv(campaign_dir))
            self.assertEqual((code, result["mode"]), (0, "predecessor_reuse"), stderr)
            self.assertEqual(result["timing_events_appended"], [item[0] for item in seal.REUSE_TIMING_EVENTS])
            self.assertEqual((result["execute_item_ids"], result["reuse_item_ids"]), ([], sorted(predecessor_tests.OFFICIAL_JOBS)))
            self.assertEqual(facts_by_kind(store_root, predecessor_tests.TARGET_ID), {"discovery_recorded": 1, "evidence_recorded": 1})
            self.assertEqual(store_status(store_root, predecessor_tests.TARGET_ID), "evidence_recorded")
            evidence_fact = next(
                fact for fact in ControlStore(store_root).list_facts(predecessor_tests.TARGET_ID) if fact["fact_kind"] == "evidence_recorded"
            )
            imported = json.loads(json.dumps(result["store"]["evidence_package_ref"]))
            self.assertEqual(evidence_fact["payload"]["evidence_package_ref"], imported)
            state = timing.phase_ledger_state(ledger)
            self.assertTrue({"VC-0", "VC-1"} <= set(state["completed_phases"]))
            manifest = claude_upgrade.load_campaign_manifest(campaign_dir)
            _path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest), "VC-1")
            self.assertEqual(checkpoint["metrics"], {"live_request_count": 0, "scanned_bytes": 0})
            # 重跑：事实、文件与账本都不再增加。
            events = len(timing._load_events(ledger))
            code, again, stderr = run_main(self.seal_argv(campaign_dir))
            self.assertEqual((code, again["timing_events_appended"]), (0, []), stderr)
            self.assertEqual(again["checkpoint_sha256"], result["checkpoint_sha256"])
            self.assertEqual(len(timing._load_events(ledger)), events)
            self.assertEqual(len(ControlStore(store_root).list_facts(predecessor_tests.TARGET_ID)), 2)
            # 复用路径不接受采集参数。
            code, _result, stderr = run_main([*self.seal_argv(campaign_dir), "--attempt-id", "attempt-001"])
            self.assertEqual(code, 1)
            self.assertIn("零请求复用路径", stderr)

    def test_reuse_seal_converges_after_sigkill(self) -> None:
        campaign_dir, ledger = self.prepare()
        store_root = claude_upgrade_store.campaign_store_root(campaign_dir)
        with self.ledger_bound(ledger):
            status = run_killed(self.seal_argv(campaign_dir), kill_after="evidence-record")
            self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
            self.assertEqual(facts_by_kind(store_root, predecessor_tests.TARGET_ID), {"discovery_recorded": 1, "evidence_recorded": 1})
            self.assertEqual(len(timing._load_events(ledger)), 1)  # 只有建账本时的 VC-0 开始事件
            code, result, stderr = run_main(self.seal_argv(campaign_dir))
            self.assertEqual(code, 0, stderr)
            self.assertEqual(result["store"]["facts_reused"], {"discovery_recorded": True, "evidence_recorded": True})
            self.assertEqual(len(result["timing_events_appended"]), 3)
            self.assertEqual(len(ControlStore(store_root).list_facts(predecessor_tests.TARGET_ID)), 2)

    def test_conflicting_timing_event_is_rejected_before_store_writes(self) -> None:
        campaign_dir, ledger = self.prepare()
        conflicting = {**seal.reuse_expected_events()[0], "next_action": "另一条同编号事件"}
        timing.append_event(ledger, **conflicting)
        with self.ledger_bound(ledger):
            code, _result, stderr = run_main(self.seal_argv(campaign_dir))
        self.assertEqual(code, 1)
        self.assertIn("已存在但内容冲突", stderr)
        self.assertEqual(facts_by_kind(claude_upgrade_store.campaign_store_root(campaign_dir), predecessor_tests.TARGET_ID), {})
        self.assertFalse((campaign_dir / seal.STAGE_RESULT_RELATIVE).exists())


if __name__ == "__main__":
    unittest.main()
