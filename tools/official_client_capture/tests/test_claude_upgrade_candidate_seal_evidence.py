"""Claude 候选证据封存的证据侧（Claude 升级控制面设计 B4-02 第二步：候选证据视图、capture 段、深度扫描、预览与批准）。

全部离线、零请求。候选 Campaign 用 2.1.285 真实场景清单裁剪到三对 Job（官方与镜像它的候选 Job：真实上游、账号刷新
触发的合成场景、含零连接 probe 的请求头条件），证据标签取同一份真实标签的对应条目；候选证据目录按 B4-01a 中继候选模式
与宿主脚本的形态合成（中继清单、逐连接请求字节、恢复报告、账号刷新触发结果）。Store 用 Store 夹具建到候选冻结
（``test_claude_upgrade_candidate_seal.SealWorld``），ScenarioPlan 改成裁剪清单的规则证据场景。

B4-01 b 段（候选 attempt.json、环境快照、恢复核验、selector 快照）还没落地：两个接缝 ``_load_capture_attempt`` 与
``_campaign_jobs`` 的候选分支在这里替换——前者读合成 attempt，后者用同一份冻结清单展开候选 Job（与 b 段只传 candidate_id
的约定一致）。合成 attempt 的候选网关上下文按 ``claude_upgrade_candidate_seal`` 的占位字段名写。

覆盖：
* 候选常量与 B4-01a 两边同值（中继候选模式的 M 门禁 15 项、hosts 恢复范围、触发结果合同；宿主脚本的恢复报告合同与目录）；
* 证据语义：合格证据逐 probe 复核通过、零连接 probe 按 order:none 匹配、网关行为观测只记录；每一项候选复核单独破坏都让
  该 probe 复核失败、闭合门禁 blocked（官方清单冒充候选、direct 中继、账号不符、hosts 恢复范围、官方 M 门禁键集、恢复报告
  缺失或恢复失败、账号刷新触发缺失、多出触发结果、观测缺失）；ScenarioPlan 场景缺证据 blocked；
* 端到端：缺客户端收据与边界核对时预览 blocked（只有这两个原因）；补齐后预览可批准、评估用 manifest 每个 artifact 带候选
  job_id；批准写 Store 到 candidate_sealed 并写候选阶段结果，阶段结果读侧、B4-05a 失败定位输入合同
  （``candidate_failure_scope_inputs`` 直接读这份阶段结果）、Store 按宿主数据根做外部证据重放都通过；再批准逐字复用；批准在
  Store 写完、阶段结果写出之前被打断，重跑收敛；
* 拒绝：批准摘要不符、未给批准人、闭合门禁 blocked 时批准、客户端收据不在 attempt 客户端证据目录、未登记的客户端；
* 封存后的证据边界（B4-02 收尾）：批准时在 attempt 根写共享合同的封存证据清单；封存后证据被复制回原路径（只有
  inode／mtime／ctime 漂移）时，封存后的读侧与批准重跑报可恢复的元数据漂移，``harden-evidence-permissions rebind-boundary``
  重绑后读侧、失败定位输入与批准重跑都恢复，没有新漂移时不重绑；同大小的内容改写读侧先见元数据漂移、重绑复算内容即拒绝，
  大小变化读侧直接判完整性异常；未封存或缺 ``--candidate-id`` 拒绝；
* 半封存（批准写完封存证据清单、写 Store 之前被打断，期间证据被复制回原路径）：测试替身按真实收口同一判据模拟 run 末收口
  重放（inode／mtime 变即失败），复现修复前重跑批准卡在收口重放；修复后重跑批准报元数据漂移与重绑命令、一条不写，
  rebind-boundary 接受半封存，重绑后批准走完；重绑别的 attempt 拒绝；
* 门禁重试链：第 1 轮门禁未失败时 ``--revalidate`` 拒绝；第 1 轮 v4 收据失败只能经 ``record_failed_gate_round`` 写进 Store
  （通过的 pass、失败的 failed，幂等；全部通过的收据、subject 不符、同一 attempt 换结果都拒绝且一条不写）；
  ``--revalidate`` 出第 2 轮（失败门禁重跑并绑最近失败收据、其余复用原收据，复用第 1 轮 selector 前态、换新
  ValidationAttempt／场景四阶段／证据包，零请求）；读侧取最大轮次；第 2 轮重复调用逐字复用；B4-06a 写入链用补跑收据在第 2 轮
  走到 ready，之后再重试只复用第 2 轮。
"""

from __future__ import annotations

import ast
import contextlib
import json
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_rule_assertion
from tools.official_client_capture import claude_upgrade_accept as accept
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture import claude_upgrade_capture as capture_module
from tools.official_client_capture import claude_upgrade_evaluation_recovery as evaluation_recovery
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_evidence_manifest as evidence_manifest
from tools.official_client_capture import codex_upgrade_gate_receipt as gate_receipt
from tools.official_client_capture.tests import test_claude_upgrade_accept as accept_tests
from tools.official_client_capture.tests import test_claude_upgrade_candidate_seal as store_layer_tests
from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.tests.fixtures import SyntheticCampaign

TOOL_ROOT = Path(__file__).resolve().parents[1]
TARGET = capture_tests.TARGET
CANDIDATE = accept_tests.CANDIDATE
ATTEMPT = "candidate-attempt-1"
RUN_NONCE = "e" * 64
ACCOUNT_ID = 7
API_KEY_ID = 9
BASE_URL = "http://sub2api:8080"
MODEL = "claude-sonnet-5"
# 三对 Job：真实上游（含 pcap）、账号刷新触发的合成场景、含零连接 probe 的请求头条件。
JOB_PAIRS = (
    ("official-transport-baseline", "candidate-transport-baseline"),
    ("official-synthetic-oauth-refresh", "candidate-synthetic-oauth-refresh"),
    ("official-header-conditions", "candidate-header-conditions"),
)
JOBS = {job for pair in JOB_PAIRS for job in pair}
CANDIDATE_CONFIGURATION = {
    "service_container": "sub2api",
    "keeper_container": "sub2api-keeper",
    "postgres_container": "sub2api-postgres",
    "claude_account_id": str(ACCOUNT_ID),
    "api_key_id": str(API_KEY_ID),
}


def reduced_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    """(场景清单, 证据标签)：真实 2.1.285 清单裁剪到 ``JOBS``，目录数只数官方主模型步骤（候选步骤镜像同一 probe）。"""

    payload = capture_tests._reduced_payload(set(JOBS))
    primary = [
        step
        for job in payload["capture_jobs"]
        if job["phase"] == "official"
        for step in job["steps"]
        if plan.step_model(step["probe_id"], step["argv"]) is None
    ]
    payload["probe_catalog"] = {**payload["probe_catalog"], "probe_count": len(primary)}
    labels = json.loads(plan.evidence_labels_path(TARGET).read_text(encoding="utf-8"))
    labels["entries"] = [entry for entry in labels["entries"] if entry["job_id"] in JOBS]
    return payload, labels


def plan_scenario_ids(payload: dict[str, Any]) -> list[str]:
    return sorted(item["scenario_id"] for item in payload["evidence_scenarios"] if item["covers"])


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _close_permissions(root: Path) -> None:
    """模拟 run 末权限收口：证据目录 0700、文件 0600（封存证据清单的前检要求不向 group／other 开放）。"""

    for path in [root, *root.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _sha_or_zero(path: Path) -> str:
    """文件的摘要；文件不存在时给一个不可能匹配的摘要（用于构造「run 时核过、seal 时缺失」的核对行）。"""

    return codex_upgrade.file_sha256(path) if path.is_file() else "0" * 64


def _request_stream(lines: list[str], host: str) -> tuple[bytes, list[dict[str, Any]]]:
    stream = b""
    inventory: list[dict[str, Any]] = []
    for line in lines:
        method, path = line.split(" ", 1)
        target = path + ("?beta=true" if path.startswith("/v1/messages") else "")
        body = json.dumps({"model": MODEL, "stream": True}).encode()
        inventory.append(
            {"connection_id": 1, "stream_offset": len(stream), "method": method, "request_target": target, "http_version": "HTTP/1.1"}
        )
        stream += f"{method} {target} HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    return stream, inventory


def write_candidate_probe(
    job_root: Path,
    probe: str,
    *,
    lines: list[str],
    synthetic: bool,
    expected: dict[str, Any],
    target_sha256: str,
    runtime_image: str,
) -> None:
    """按 B4-01a 中继候选模式与宿主脚本的形态写一个候选 probe 的证据：中继清单、连接与请求字节、恢复报告与触发结果。"""

    directory = job_root / probe
    (directory / "relay").mkdir(parents=True, mode=0o700)
    (directory / "results").mkdir(mode=0o700)
    host = "platform.claude.com" if lines and all(line.startswith("POST /v1/oauth/") for line in lines) else "api.anthropic.com"
    rows: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    if lines:
        stream, inventory = _request_stream(lines, host)
        (directory / "relay" / "conn001.client_to_upstream.bin").write_bytes(stream)
        rows.append({"connection_id": 1, "valid": True, "intervention": "refresh-rejected" if synthetic else None})
    _write_json(directory / "relay" / "relay.json", {"schema_version": "byte-relay/v1", "connections": rows})
    if synthetic:
        (directory / "relay" / "intervention.jsonl").write_text(
            "".join(json.dumps({"connection_id": row["connection_id"], "action": row["intervention"]}) + "\n" for row in rows),
            encoding="utf-8",
        )
    (directory / "tls-clienthello.pcap").write_bytes(b"\xd4\xc3\xb2\xa1synthetic-gateway-client-hello")
    (directory / "results" / "debug.log").write_text("synthetic debug log\n", encoding="utf-8")
    trigger = expected["trigger"]
    if trigger == evidence.CANDIDATE_TRIGGER_ACCOUNT_REFRESH:
        _write_json(
            directory / evidence.CANDIDATE_TRIGGER_RELATIVE,
            {
                "schema_version": evidence.CANDIDATE_TRIGGER_SCHEMA,
                "trigger": trigger,
                "endpoint": "POST /api/v1/admin/accounts/{account_id}/refresh",
                "account_id": expected["account_id"],
                "started_at_utc": "2026-10-07T00:20:00Z",
                "ended_at_utc": "2026-10-07T00:20:01Z",
                "http_status": 502,
                "response_sha256": "0" * 64,
                "response_bytes": 0,
                "transport_error": None,
                "valid": True,
            },
        )
    manifest = {
        "schema_version": evidence.RELAY_MANIFEST_SCHEMA,
        "side": evidence.CANDIDATE_SIDE,
        "run_id": expected["run_id"],
        "probe_id": probe,
        "status": "complete",
        "model": MODEL,
        "client": {"version": f"{TARGET} (Claude Code)", "sha256": target_sha256},
        "fw_f_v5_probe": probe,
        "fw_f_v4_probe": None,
        "response_plan": "claude-oauth-refresh-reject" if synthetic else None,
        "candidate_gateway": {
            "base_url": BASE_URL,
            "account_id": expected["account_id"],
            "api_key_id": expected["api_key_id"],
            "trigger": trigger,
            "client_authentication": evidence.CANDIDATE_CLIENT_AUTHENTICATION,
            "oauth_credentials_read": False,
        },
        "runtime": {"runtime_image_reference": runtime_image, "host_runtime_receipt": {"sha256": "c" * 64}},
        "upstream": {
            "host": host,
            "live_requests": not synthetic,
            "relay_mode": evidence.CANDIDATE_RELAY_MODE,
            "relay_listen_port": expected["relay_port"],
        },
        "cleanup": {"relay_stopped": True, "hosts_restored": True},
        "hosts_recovery": dict(evidence.CANDIDATE_HOSTS_RECOVERY),
        "secret_scan": {"performed": True, "passed": True, "matches": []},
        "m_binding": {"complete": True, "requirements": {name: True for name in sorted(evidence.CANDIDATE_M_REQUIREMENTS)}},
        "relay_integrity": {"result": "passed", "excluded_connections": []},
        "dimension_evidence": {"actual_wire_inventory": inventory, "side": "candidate", "observation_only": True},
        "candidate_observation": {
            "client_outcome_valid": True,
            "message_request_expectation_met": True,
            "synthetic_plan_closed": True if synthetic else None,
            "dimension_result": "passed",
        },
    }
    _write_json(directory / evidence.RELAY_MANIFEST_FILENAME, manifest)
    _write_json(
        job_root / evidence.CANDIDATE_RESTORATION_DIRNAME / f"{probe}.json",
        {
            "schema_version": evidence.CANDIDATE_RESTORATION_SCHEMA,
            "status": "complete",
            "run_id": expected["run_id"],
            "probe_id": probe,
            "account_id": expected["account_id"],
            "api_key_id": expected["api_key_id"],
            "trigger": trigger,
            "relay_port": expected["relay_port"],
            "capture_exit_code": 0,
            "account_proxy_original": "NULL|NULL",
            **{name: True for name in evidence.CANDIDATE_RESTORATION_EQUALITIES},
            "keeper_was_running": True,
            "runtime_secrets_removed": True,
            "started_at_utc": "2026-10-07T00:10:00Z",
            "ended_at_utc": "2026-10-07T00:30:00Z",
        },
    )


class PlanScenarioCampaign(SyntheticCampaign):
    """Store 夹具的变体：VC-3 ScenarioPlan 的场景换成裁剪清单的规则证据场景（规范与断言沿用夹具的唯一一条）。"""

    plan_scenarios: list[str] = []

    def profile_objects(self) -> None:
        real = self.store.seal_object

        def sealing(kind: str, payload: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            if kind == "scenario_plan":
                template = payload["scenarios"][0]
                payload = {**payload, "scenarios": [{**template, "id": scenario} for scenario in sorted(self.plan_scenarios)]}
            return real(kind, payload, *args, **kwargs)

        with mock.patch.object(self.store, "seal_object", side_effect=sealing):
            super().profile_objects()


class _CandidateSealCase(seal_tests._SealCampaignCase):
    """B2-06／B2-07 的取证与 seal 夹具（临时数据根、受管工具树、宿主边界替身）加候选 Campaign、Store 世界与两个接缝替身。"""

    def setUp(self) -> None:
        super().setUp()
        self.payload, self.labels = reduced_inputs()
        PlanScenarioCampaign.plan_scenarios = plan_scenario_ids(self.payload)
        with mock.patch.object(accept_tests, "SyntheticCampaign", PlanScenarioCampaign):
            self.world = store_layer_tests.SealWorld(self.root / "store-world")
        self.campaign = self.build_candidate_campaign()
        self.manifest = json.loads((self.campaign / "campaign.json").read_text(encoding="utf-8"))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(claude_upgrade_store, "open_campaign_store", side_effect=self._open_store))
        real_load = claude_upgrade._load_capture_attempt
        real_jobs = claude_upgrade._campaign_jobs

        def load_attempt(campaign_dir: Path, phase: str, candidate_id: str | None, attempt_id: str, **options: Any) -> Any:
            if phase != "candidate":
                return real_load(campaign_dir, phase, candidate_id, attempt_id, **options)
            root = Path(campaign_dir) / "candidates" / str(candidate_id) / "attempts" / attempt_id
            if options.get("_replay_evidence_permissions", True):
                # 模拟 run 末收口重放：收口边界含 inode 与 mtime，证据被复制回原路径即重放失败（与真实收口同一判据）。
                if self.closeout_boundary(root) != self.closeout_snapshot:
                    raise codex_upgrade.ConfigurationError("证据权限收口收据未通过：权限收口后的证据元数据边界漂移。")
            return root, json.loads((root / "attempt.json").read_text(encoding="utf-8"))

        def campaign_jobs(campaign_dir: Path, manifest: dict[str, Any], phase: str, **options: Any) -> Any:
            if phase != "candidate":
                return real_jobs(campaign_dir, manifest, phase, **options)
            return self.candidate_jobs(Path(campaign_dir), manifest, str(options["candidate_id"]))

        stack.enter_context(mock.patch.object(claude_upgrade, "_load_capture_attempt", side_effect=load_attempt))
        stack.enter_context(mock.patch.object(claude_upgrade, "_campaign_jobs", side_effect=campaign_jobs))

    def _open_store(self, campaign_dir: Path, manifest: Any) -> tuple[Any, dict[str, Any]]:
        del campaign_dir, manifest
        return self.world.store, self.world.store.load_campaign(self.world.campaign_id)

    # -- Campaign、候选 Job 与证据 -----------------------------------------------------------------------------------

    def build_candidate_campaign(self) -> Path:
        campaign = self.write_campaign(self.payload, campaign_id=self.world.campaign_id, configuration=CANDIDATE_CONFIGURATION)
        labels = _write_json(campaign / "inputs" / "evidence-labels.json", self.labels)
        manifest = json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))
        inputs = dict(manifest["inputs"])
        inputs["evidence_labels"] = {"path": labels.relative_to(campaign).as_posix(), "sha256": codex_upgrade.file_sha256(labels)}
        seal_tests.rewrite_manifest(campaign, inputs=inputs, campaign_purpose=self.world.candidate_payload["candidate_purpose"])
        return campaign

    def candidate_jobs(self, campaign_dir: Path, manifest: dict[str, Any], candidate_id: str) -> list[Any]:
        """``_campaign_jobs`` 候选分支的替身：同一份冻结清单与 Campaign 变量展开，只取候选阶段（B4-01 b 段只传 candidate_id）。"""

        arguments = claude_upgrade._campaign_arguments(campaign_dir, manifest, candidate_id=candidate_id)
        payload = plan.read_scenario_manifest(
            Path(arguments.scenario_manifest), expected_version=TARGET, rules=tuple(manifest["required_rules"]), verify_source_spec=False
        )
        context = claude_upgrade._job_context(arguments)
        jobs = plan.expand_scenario_jobs(payload, context, job_factory=claude_upgrade.Job, suite=arguments.suite)
        return [job for job in jobs if job.phase == "candidate"]

    def label_plans(self) -> dict[str, Any]:
        loaded = plan.load_evidence_labels(
            self.campaign / "inputs" / "evidence-labels.json",
            expected_version=TARGET,
            scenario_payload=plan.read_scenario_manifest(
                self.campaign / "inputs" / "target-scenarios.json",
                expected_version=TARGET,
                rules=tuple(self.manifest["required_rules"]),
                verify_source_spec=False,
            ),
        )
        return loaded["plans"]

    def synthetic_probes(self) -> set[str]:
        return {
            rule["labels"]["probe"]
            for entry in self.labels["entries"]
            for rule in entry["rules"]
            if rule["labels"].get("response_mode") == "synthetic"
        }

    def write_evidence_and_attempt(self, *, mutate: Any = None) -> Path:
        """写全部候选 Job 的证据与合成 attempt（relay／恢复报告核对行绑定写出后的摘要）；返回 attempt 根。"""

        jobs = self.candidate_jobs(self.campaign, self.manifest, CANDIDATE)
        plans = self.label_plans()
        synthetic = self.synthetic_probes()
        results = []
        self.model_requests = 0
        for job in jobs:
            root = Path(job.evidence_roots[0])
            expectations = candidate_seal._step_expectations(job)
            for probe in [str(step["probe_id"]) for step in job.steps]:
                probe_plan = plans[job.job_id][probe]
                lines = [] if probe_plan["mode"] == "none" else [sorted(probe_plan["requests"])[0]]
                self.model_requests += sum(1 for line in lines if line == "POST /v1/messages")
                write_candidate_probe(
                    root,
                    probe,
                    lines=lines,
                    synthetic=probe in synthetic,
                    expected=expectations[probe],
                    target_sha256=self.manifest["target_sha256"],
                    runtime_image=capture_tests.IMAGE,
                )
            if mutate is not None:
                mutate(job, root)
            _close_permissions(root)
            results.append(
                {
                    "id": job.job_id,
                    "phase": "candidate",
                    "required": job.required,
                    "execution_sha256": claude_upgrade._job_execution_sha256(job),
                    "status": "complete",
                    "evidence_roots": list(job.evidence_roots),
                    "relay_checks": [
                        {
                            "probe_id": str(step["probe_id"]),
                            "status": "passed",
                            "manifest": {
                                "path": f"{job.evidence_roots[0]}/{step['probe_id']}/{evidence.RELAY_MANIFEST_FILENAME}",
                                "sha256": codex_upgrade.file_sha256(root / str(step["probe_id"]) / evidence.RELAY_MANIFEST_FILENAME),
                            },
                        }
                        for step in job.steps
                    ],
                    "restoration_checks": [
                        {
                            "probe_id": str(step["probe_id"]),
                            "status": "passed",
                            "report": {
                                "path": f"{job.evidence_roots[0]}/{evidence.CANDIDATE_RESTORATION_DIRNAME}/{step['probe_id']}.json",
                                "sha256": _sha_or_zero(root / evidence.CANDIDATE_RESTORATION_DIRNAME / f"{step['probe_id']}.json"),
                            },
                        }
                        for step in job.steps
                    ],
                }
            )
        attempt_root = self.campaign / "candidates" / CANDIDATE / "attempts" / ATTEMPT
        attempt = {
            "attempt_id": ATTEMPT,
            "campaign_id": self.manifest["campaign_id"],
            "status": "awaiting_receipts",
            "run_nonce": RUN_NONCE,
            "started_at_utc": "2026-10-07T00:10:00Z",
            "completed_at_utc": "2026-10-07T00:40:00Z",
            "binary_verification": self.fake_binaries(self.manifest),
            # run 末收口登记的证据根（宿主路径）：封存证据清单与共享重绑都按它解析。
            "evidence_roots": sorted(str(Path(job.evidence_roots[0])) for job in jobs),
            "results": results,
            candidate_seal.ATTEMPT_CANDIDATE_CONTEXT_FIELD: {
                "base_url": BASE_URL,
                "host": {"host_id": accept_tests.HOST, "architecture": accept_tests.ARCHITECTURE},
                "run_conditions": {
                    "environment_sha256": "4" * 64,
                    "configuration_sha256": "5" * 64,
                    "network_sha256": "6" * 64,
                    "account_model_sha256": "a" * 64,
                },
                "environment_before": [{"id": "gateway-isolation-before", "facts": {"result": "pass", "digest": "1" * 64}}],
                "environment_after": [{"id": "gateway-isolation-after", "facts": {"result": "pass", "digest": "1" * 64}}],
                "recovery_verification": [{"id": "attempt-recovery-verified", "facts": {"result": "pass", "digest": "2" * 64}}],
                "selector_before_snapshot": self.world.store.load_object(self.world.refs["runtime-before"])["payload"],
            },
        }
        _write_json(attempt_root / "attempt.json", attempt)
        # 原子预约收据（与 B2-06 的预约同一合同，候选阶段带 candidate_id 与候选用途）：环境污染扫描逐个 attempt 读它。
        reservation = {
            "schema_version": capture_module.CAPTURE_RESERVATION_SCHEMA,
            "client": capture_module.CLIENT,
            "mode": capture_module.MODE_FORMAL,
            "campaign_id": self.manifest["campaign_id"],
            "campaign_mode": self.manifest["campaign_mode"],
            "campaign_purpose": self.manifest["campaign_purpose"],
            "campaign_manifest_sha256": codex_upgrade.file_sha256(self.campaign / "campaign.json"),
            "phase": "candidate",
            "candidate_id": CANDIDATE,
            "candidate_purpose": self.manifest["campaign_purpose"],
            "attempt_id": ATTEMPT,
            "run_nonce": RUN_NONCE,
            "started_at_utc": attempt["started_at_utc"],
            "identity_sha256": "d" * 64,
            "planned_jobs": [
                {"id": job.job_id, "required": job.required, "execution_sha256": claude_upgrade._job_execution_sha256(job)} for job in jobs
            ],
        }
        reservation["reservation_digest"] = claude_upgrade._fingerprint(dict(reservation))
        _write_json(attempt_root / "reservation.json", reservation)
        (attempt_root / candidate_seal.CLIENT_EVIDENCE_DIRNAME).mkdir(mode=0o700)
        self.closeout_snapshot = self.closeout_boundary(attempt_root)
        return attempt_root

    def closeout_boundary(self, attempt_root: Path) -> dict[str, tuple[int, int]]:
        """run 末收口登记的证据根下每个文件的 inode 与 mtime（模拟收口边界）。"""

        attempt = json.loads((attempt_root / "attempt.json").read_text(encoding="utf-8"))
        boundary: dict[str, tuple[int, int]] = {}
        for root in attempt["evidence_roots"]:
            for path in sorted(Path(root).rglob("*")):
                if path.is_file():
                    metadata = path.stat()
                    boundary[str(path)] = (metadata.st_ino, metadata.st_mtime_ns)
        return boundary

    def write_client_evidence(self, attempt_root: Path) -> list[str]:
        """两个 Mac 客户端收据与两条 attempt 级边界核对（最小绑定字段，占位合同）；返回 ``--client-evidence`` 参数。"""

        directory = attempt_root / candidate_seal.CLIENT_EVIDENCE_DIRNAME
        binding = {"campaign_id": self.manifest["campaign_id"], "candidate_id": CANDIDATE, "attempt_id": ATTEMPT, "run_nonce": RUN_NONCE}
        argv: list[str] = []
        for client in ("claude-desktop", "claude-vscode"):
            path = _write_json(directory / f"{client}.json", {"client_id": client, **binding, "status": "success"})
            argv += ["--client-evidence", f"{client}={path}"]
        for check in ("codex-final-wire-isolation", "third-party-ingress-rejection"):
            _write_json(directory / f"{check}.json", {"check_id": check, **binding, "result": "pass"})
        return argv

    def seal_argv(self, *extra: str) -> list[str]:
        return ["capture-candidate", "seal", "--campaign-dir", str(self.campaign), "--candidate-id", CANDIDATE, "--attempt-id", ATTEMPT, *extra]

    def run_seal(self, *extra: str) -> tuple[int, dict | None, str]:
        with self.seal_patched():
            return seal_tests.run_main(self.seal_argv(*extra))

    def store_footprint(self) -> tuple[dict[str, int], dict[str, int]]:
        return self.world.store_footprint()

    def seal_candidate(self) -> tuple[Path, list[str], dict[str, Any]]:
        """预览、批准一次走完；返回 (attempt 根, 客户端收据参数, 批准结果)。"""

        attempt_root = self.write_evidence_and_attempt()
        client = self.write_client_evidence(attempt_root)
        code, preview, stderr = self.run_seal(*client)
        self.assertEqual(code, 2, stderr)
        assert preview is not None
        code, sealed, stderr = self.run_seal("--approve-seal-sha256", preview["review_sha256"], "--approved-by", "reviewer-a", *client)
        self.assertEqual(code, 0, stderr)
        assert sealed is not None
        self.approve_argv = ("--approve-seal-sha256", preview["review_sha256"], "--approved-by", "reviewer-a", *client)
        return attempt_root, client, sealed

    def job_root(self, job_id: str) -> Path:
        return Path(next(job for job in self.candidate_jobs(self.campaign, self.manifest, CANDIDATE) if job.job_id == job_id).evidence_roots[0])

    def read_sealed_attempt(self) -> tuple[Path, dict[str, Any]]:
        stage = claude_upgrade._load_stage_result(self.campaign, "capture-candidate", CANDIDATE)
        with self.seal_patched():
            return claude_upgrade._capture_stage_attempt_context(self.campaign, stage, phase="candidate", candidate_id=CANDIDATE)

    def rebind(self, *extra: str) -> tuple[int, dict | None, str]:
        argv = ["harden-evidence-permissions", "rebind-boundary", "--campaign-dir", str(self.campaign), "--attempt-id", ATTEMPT, *extra]
        with self.seal_patched():
            return seal_tests.run_main(argv)

    def copy_back(self, job_id: str, relative: str) -> None:
        """封存后把一个证据文件「复制回原路径」：内容不变，inode／mtime／ctime 变化。"""

        target = self.job_root(job_id) / relative
        content = target.read_bytes()
        target.unlink()
        target.write_bytes(content)
        target.chmod(0o600)


# ---------------------------------------------------------------------------
# 候选常量与 B4-01a 两边同值
# ---------------------------------------------------------------------------


class CandidateConstantTests(unittest.TestCase):
    def test_constants_match_relay_candidate_mode_and_host_script(self) -> None:
        relay_source = (TOOL_ROOT / "claude_fw_e_relay.py").read_text(encoding="utf-8")
        tree = ast.parse(relay_source)
        capture_candidate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_capture_candidate")
        dicts = [
            node.value
            for node in ast.walk(capture_candidate)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "requirements" for target in node.targets)
            and isinstance(node.value, ast.Dict)
        ]
        self.assertEqual(len(dicts), 1)
        self.assertEqual({key.value for key in dicts[0].keys}, set(evidence.CANDIDATE_M_REQUIREMENTS))
        constants = {
            target.id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id
            in {"CANDIDATE_HOSTS_RECOVERY", "CANDIDATE_TRIGGER_RESULT_SCHEMA", "CANDIDATE_TRIGGER_CLIENT", "CANDIDATE_TRIGGER_ACCOUNT_REFRESH"}
        }
        self.assertEqual(constants["CANDIDATE_HOSTS_RECOVERY"], evidence.CANDIDATE_HOSTS_RECOVERY)
        self.assertEqual(constants["CANDIDATE_TRIGGER_RESULT_SCHEMA"], evidence.CANDIDATE_TRIGGER_SCHEMA)
        self.assertEqual(
            (constants["CANDIDATE_TRIGGER_CLIENT"], constants["CANDIDATE_TRIGGER_ACCOUNT_REFRESH"]),
            (evidence.CANDIDATE_TRIGGER_CLIENT, evidence.CANDIDATE_TRIGGER_ACCOUNT_REFRESH),
        )
        self.assertEqual(
            (plan.CANDIDATE_TRIGGER_CLIENT, plan.CANDIDATE_TRIGGER_ACCOUNT_REFRESH),
            (evidence.CANDIDATE_TRIGGER_CLIENT, evidence.CANDIDATE_TRIGGER_ACCOUNT_REFRESH),
        )
        self.assertIn('"relay_mode": "connect"', relay_source)
        self.assertIn('"client_authentication": "ANTHROPIC_AUTH_TOKEN"', relay_source)
        self.assertIn('secure_write_json(result_root / "gateway-trigger.json", result)', relay_source)
        self.assertEqual(evidence.CANDIDATE_TRIGGER_RELATIVE, Path("results") / "gateway-trigger.json")
        script = (TOOL_ROOT / plan.CANDIDATE_SCRIPT).read_text(encoding="utf-8")
        self.assertIn(f'"schema_version": "{evidence.CANDIDATE_RESTORATION_SCHEMA}"', script)
        self.assertIn(f'report_dir="$(dirname "$output_host")/{evidence.CANDIDATE_RESTORATION_DIRNAME}"', script)
        self.assertIn('report_path="$report_dir/$probe_id.json"', script)
        for name in evidence.CANDIDATE_RESTORATION_EQUALITIES:
            self.assertIn(f'"{name}":', script)


# ---------------------------------------------------------------------------
# 证据语义（预览闭合门禁）
# ---------------------------------------------------------------------------


class CandidateEvidenceTests(_CandidateSealCase):
    def preview(self, *extra: str) -> dict[str, Any]:
        code, result, stderr = self.run_seal(*extra)
        self.assertEqual(code, 2, stderr)
        assert result is not None
        return result

    def capture_index(self, result: dict[str, Any]) -> dict[str, Any]:
        draft = json.loads((Path(result["preview"]).parent / candidate_seal.SEAL_DRAFT_FILENAME).read_text(encoding="utf-8"))
        return json.loads((self.campaign / draft["drafts"]["capture_index"]["path"]).read_text(encoding="utf-8"))

    def test_valid_evidence_passes_every_probe_and_observations_are_only_recorded(self) -> None:
        attempt_root = self.write_evidence_and_attempt()
        result = self.preview(*self.write_client_evidence(attempt_root))
        self.assertEqual((result["status"], result["closure"]), ("awaiting_seal_approval", {"status": "closed", "reasons": []}))
        index = self.capture_index(result)
        rows = {probe["probe_id"]: probe for job in index["jobs"] for probe in job["probes"]}
        self.assertEqual(index["failures"], [])
        self.assertTrue(all(row["result"] == "passed" for row in rows.values()))
        # 零连接 probe 按 order:none 匹配：没有中继请求字节，只有中继清单与 pcap 等其余标签。
        self.assertEqual(rows["v4-replay-custom-header-invalid-name"]["connections"], [])
        # 账号刷新触发的步骤带送达的触发结果，其余步骤没有。
        self.assertEqual(rows["v4-oauth-refresh"]["gateway_trigger"]["failed_fields"], [])
        self.assertIsNone(rows["v4-native-tls-baseline"]["gateway_trigger"])
        self.assertEqual(rows["v4-native-tls-baseline"]["restoration"]["failed_fields"], [])
        self.assertEqual(index["scenario_plan"], {"scenario_ids": plan_scenario_ids(self.payload), "uncovered": []})
        self.assertEqual(result["live_request_count"], self.model_requests)

    def test_each_candidate_check_fails_closed(self) -> None:
        probe = "v4-native-tls-baseline"

        def manifest_edit(edit: Any) -> Any:
            def mutate(job: Any, root: Path) -> None:
                path = root / probe / evidence.RELAY_MANIFEST_FILENAME
                if path.is_file():
                    value = json.loads(path.read_text(encoding="utf-8"))
                    edit(value)
                    _write_json(path, value)
            return mutate

        def report_edit(edit: Any) -> Any:
            def mutate(job: Any, root: Path) -> None:
                path = root / evidence.CANDIDATE_RESTORATION_DIRNAME / f"{probe}.json"
                if path.is_file():
                    value = json.loads(path.read_text(encoding="utf-8"))
                    edit(value)
                    _write_json(path, value)
            return mutate

        def refresh_trigger(edit: Any) -> Any:
            def mutate(job: Any, root: Path) -> None:
                path = root / "v4-oauth-refresh" / evidence.CANDIDATE_TRIGGER_RELATIVE
                if path.is_file():
                    edit(path)
            return mutate

        cases = {
            "candidate_side": (probe, manifest_edit(lambda value: value.pop("side"))),
            "connect_relay": (probe, manifest_edit(lambda value: value["upstream"].update(relay_mode="direct"))),
            "candidate_gateway": (probe, manifest_edit(lambda value: value["candidate_gateway"].update(account_id=ACCOUNT_ID + 1))),
            "hosts_recovery": (probe, manifest_edit(lambda value: value.update(hosts_recovery={"scope": "capture-container-only", "restored": True}))),
            "m_binding_requirements": (
                probe,
                manifest_edit(lambda value: value["m_binding"].update(requirements={"runtime_identity": True, "hosts_restored": True})),
            ),
            "candidate_observation_recorded": (probe, manifest_edit(lambda value: value.pop("candidate_observation"))),
            "restoration": (probe, report_edit(lambda value: value.update(status="restoration_failed", hosts_sha256_equal=False))),
            "gateway_trigger": ("v4-oauth-refresh", refresh_trigger(lambda path: path.unlink())),
        }
        for check, (target, mutate) in cases.items():
            with self.subTest(check=check):
                shutil.rmtree(self.data / "runs" / "claude-candidate", ignore_errors=True)
                shutil.rmtree(self.campaign / "candidates", ignore_errors=True)
                attempt_root = self.write_evidence_and_attempt(mutate=mutate)
                result = self.preview(*self.write_client_evidence(attempt_root))
                # 复核失败的 probe 若是某个 ScenarioPlan 场景唯一的证据，闭合门禁另记场景缺口（账号刷新那一步就是）。
                self.assertEqual(result["closure"]["status"], "blocked")
                self.assertEqual(result["closure"]["reasons"][0], "capture_index_failed")
                self.assertLessEqual(set(result["closure"]["reasons"]), {"capture_index_failed", "scenario_uncovered"})
                failures = {item["probe_id"]: item["failed_checks"] for item in self.capture_index(result)["failures"]}
                self.assertEqual(failures, {target: [check]})

    def test_missing_restoration_report_fails_before_scanning(self) -> None:
        def mutate(job: Any, root: Path) -> None:
            if job.job_id == "candidate-transport-baseline":
                (root / evidence.CANDIDATE_RESTORATION_DIRNAME / "v4-connection-reuse.json").unlink()

        attempt_root = self.write_evidence_and_attempt(mutate=mutate)
        # 恢复报告缺失时 attempt 的恢复核对行也绑定不到它：seal 在扫描前就失败关闭。
        with self.seal_patched():
            code, _result, stderr = seal_tests.run_main(self.seal_argv(*self.write_client_evidence(attempt_root)))
        self.assertEqual(code, 1)
        self.assertIn("恢复报告缺失、未通过或在 run／seal 之间漂移", stderr)

    def test_unexpected_trigger_result_and_uncovered_scenario_block(self) -> None:
        def mutate(job: Any, root: Path) -> None:
            if job.job_id == "candidate-transport-baseline":
                _write_json(root / "v4-replay-baseline" / evidence.CANDIDATE_TRIGGER_RELATIVE, {"schema_version": evidence.CANDIDATE_TRIGGER_SCHEMA})

        attempt_root = self.write_evidence_and_attempt(mutate=mutate)
        result = self.preview(*self.write_client_evidence(attempt_root))
        failures = {item["probe_id"]: item["failed_checks"] for item in self.capture_index(result)["failures"]}
        self.assertEqual(failures, {"v4-replay-baseline": ["gateway_trigger"]})
        # v4-replay-baseline 是 agency.foreground 唯一的证据：复核失败后这个场景没有证据，闭合门禁另记场景缺口。
        self.assertEqual(result["closure"], {"status": "blocked", "reasons": ["capture_index_failed", "scenario_uncovered"]})
        self.assertEqual(self.capture_index(result)["scenario_plan"]["uncovered"], ["agency.foreground"])


# ---------------------------------------------------------------------------
# 端到端：预览 → 批准 → Store 与候选阶段结果
# ---------------------------------------------------------------------------


class CandidateSealChainTests(_CandidateSealCase):
    def test_blocked_without_client_evidence_then_approved_sealed_and_reused(self) -> None:
        attempt_root = self.write_evidence_and_attempt()
        code, blocked, stderr = self.run_seal()
        self.assertEqual(code, 2, stderr)
        assert blocked is not None
        self.assertEqual(
            (blocked["status"], blocked["closure"]["reasons"]), ("seal_blocked", ["client_binding_missing", "boundary_check_missing"])
        )
        client = self.write_client_evidence(attempt_root)
        code, preview, stderr = self.run_seal(*client)
        self.assertEqual(code, 2, stderr)
        assert preview is not None
        self.assertEqual(preview["status"], "awaiting_seal_approval")
        self.assertNotEqual(preview["generation"], blocked["generation"])
        seal_dir = Path(preview["preview"]).parent
        draft = json.loads((seal_dir / candidate_seal.SEAL_DRAFT_FILENAME).read_text(encoding="utf-8"))
        capture_manifest = json.loads((self.campaign / draft["drafts"]["capture_manifest"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual((capture_manifest["side"], capture_manifest["candidate_id"], capture_manifest["label_mismatches"]), ("candidate", CANDIDATE, []))
        self.assertTrue(all(item["job_id"].startswith("candidate-") and item["labels"]["egress"] == "candidate-gateway" for item in capture_manifest["artifacts"]))
        self.assertEqual(draft["store_plan"]["validation_attempt_id"], f"{ATTEMPT}-v1")
        # 批准：缺批准人、摘要不符都在任何写入之前拒绝。
        before = self.store_footprint()
        code, _result, stderr = self.run_seal("--approve-seal-sha256", preview["review_sha256"], *client)
        self.assertEqual(code, 1)
        self.assertIn("--approved-by", stderr)
        code, _result, stderr = self.run_seal("--approve-seal-sha256", "0" * 64, "--approved-by", "reviewer-a", *client)
        self.assertEqual(code, 1)
        self.assertIn("review_sha256 不一致", stderr)
        self.assertEqual(self.store_footprint(), before)
        code, sealed, stderr = self.run_seal("--approve-seal-sha256", preview["review_sha256"], "--approved-by", "reviewer-a", *client)
        self.assertEqual(code, 0, stderr)
        assert sealed is not None
        self.assertEqual((sealed["status"], sealed["reused"], sealed["store"]["checkpoint"]), ("complete", False, "candidate_sealed"))
        facts, objects = self.store_footprint()
        added = {kind: facts[kind] - before[0].get(kind, 0) for kind in facts if facts[kind] != before[0].get(kind, 0)}
        scenarios = len(plan_scenario_ids(self.payload))
        self.assertEqual(
            added,
            {
                "selector_observed": 1,
                "validation_attempt_created": 1,
                **{kind: scenarios for kind in ("scenario_prepared", "scenario_captured", "scenario_sealed", "scenario_approved")},
            },
        )
        self.assertEqual(objects["candidate_evidence_package"], 1)
        # Store：证据包的原始证据清单就是深度扫描清单，四份运行证据覆盖全部 probe 的恢复报告与秘密扫描。
        package = self.world.store.load_object(sealed["store"]["candidate_evidence_package_ref"])["payload"]
        inventory = json.loads((seal_dir / candidate_seal.EVIDENCE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(package["raw_evidence_inventory"], [{key: entry[key] for key in ("path", "sha256", "bytes")} for entry in inventory["entries"]])
        probes = sorted(str(step["probe_id"]) for job in self.candidate_jobs(self.campaign, self.manifest, CANDIDATE) for step in job.steps)
        recovery = self.world.store.load_object(package["recovery_evidence_ref"])["payload"]
        self.assertEqual(
            [entry["id"] for entry in recovery["entries"]],
            sorted(["attempt-recovery-verified", *(f"restoration-{probe}" for probe in probes)]),
        )
        secret = self.world.store.load_object(package["secret_scan_evidence_ref"])["payload"]
        self.assertEqual([entry["id"] for entry in secret["entries"]], [f"secret-scan-{probe}" for probe in probes])
        self.assertEqual(sorted(sealed["store"]["scenario_approval_refs"]), plan_scenario_ids(self.payload))
        # 候选阶段结果：读侧通过；评估用 manifest 按 Campaign 相对路径绑定，满足失败定位的输入合同（artifact 带 job_id，
        # job_id 都在原 attempt 的 Job 集合里）。
        stage = claude_upgrade._load_stage_result(self.campaign, "capture-candidate", CANDIDATE)
        self.assertEqual(stage["store"]["candidate_evidence_package_ref"], sealed["store"]["candidate_evidence_package_ref"])
        binding = stage[candidate_seal.ASSERTION_CONTEXT_FIELD][candidate_seal.EVALUATION_MANIFEST_FIELD]
        self.assertEqual(set(binding), {"path", "sha256"})
        evaluation_path = claude_upgrade._campaign_file(self.campaign, binding["path"])
        self.assertEqual(codex_upgrade.file_sha256(evaluation_path), binding["sha256"])
        evaluation = claude_rule_assertion.validate_evaluation_manifest(
            json.loads(evaluation_path.read_text(encoding="utf-8")), expected_claude_version=TARGET
        )
        with self.seal_patched():
            _root, attempt = claude_upgrade._capture_stage_attempt_context(self.campaign, stage, phase="candidate", candidate_id=CANDIDATE)
        # B4-05a 的失败定位输入合同直接读这份候选阶段结果与 attempt：评估用 manifest 每个条目归属唯一的候选 Job，
        # Job 集合就是原 attempt 的 results[].id。
        inputs = evaluation_recovery.candidate_failure_scope_inputs(self.campaign, stage, attempt)
        candidate_jobs = sorted(job.job_id for job in self.candidate_jobs(self.campaign, self.manifest, CANDIDATE))
        self.assertEqual(inputs["attempt_jobs"], candidate_jobs)
        self.assertEqual(inputs["artifact_jobs"], {str(item["path"]): str(item["job_id"]) for item in evaluation["artifacts"]})
        self.assertEqual(set(inputs["artifact_jobs"].values()), set(candidate_jobs))
        self.assertEqual(stage["client_evidence"]["client_bindings"].keys(), {"claude-desktop", "claude-vscode"})
        # 封存证据清单（共享合同）写在 attempt 根，覆盖全部候选 Job 证据根，阶段结果按摘要绑定它。
        sealed_manifest = attempt_root / candidate_seal.SEALED_EVIDENCE_MANIFEST_FILENAME
        self.assertEqual(stage[candidate_seal.SEALED_EVIDENCE_MANIFEST_FIELD]["sha256"], codex_upgrade.file_sha256(sealed_manifest))
        sealed_document = json.loads(sealed_manifest.read_text(encoding="utf-8"))
        self.assertEqual(sealed_document["schema_version"], evidence_manifest.MANIFEST_SCHEMA)
        self.assertEqual(sorted(root["path"] for root in sealed_document["roots"]), sorted(attempt["evidence_roots"]))
        # Store 外部证据重放：宿主数据根就是 external root（夹具自己的外部文件也放进去）。
        shutil.copytree(self.world.fixture.external_root, self.data, dirs_exist_ok=True)
        self.world.store.replay(external_root=self.data, require_external=True)
        # 再批准逐字复用：不写 Store、阶段结果不变。
        stage_bytes = (self.campaign / "candidates" / CANDIDATE / "result.json").read_bytes()
        code, again, stderr = self.run_seal("--approve-seal-sha256", preview["review_sha256"], "--approved-by", "reviewer-a", *client)
        self.assertEqual(code, 0, stderr)
        assert again is not None
        self.assertTrue(again["reused"])
        self.assertEqual(self.store_footprint(), (facts, objects))
        self.assertEqual((self.campaign / "candidates" / CANDIDATE / "result.json").read_bytes(), stage_bytes)
        # 另一批准人不得覆盖已封存的结果。
        code, _result, stderr = self.run_seal("--approve-seal-sha256", preview["review_sha256"], "--approved-by", "reviewer-b", *client)
        self.assertEqual(code, 1)
        self.assertIn("另一批准人", stderr)

    def test_interrupted_after_store_writes_converges_on_rerun(self) -> None:
        attempt_root = self.write_evidence_and_attempt()
        client = self.write_client_evidence(attempt_root)
        code, preview, stderr = self.run_seal(*client)
        self.assertEqual(code, 2, stderr)
        assert preview is not None
        real = claude_upgrade_seal._write_once_or_reuse
        stage_path = self.campaign / "candidates" / CANDIDATE / "result.json"

        def interrupted(path: Path, payload: Any, **options: Any) -> Any:
            if Path(path) == stage_path:
                raise KeyboardInterrupt
            return real(path, payload, **options)

        approve = ("--approve-seal-sha256", preview["review_sha256"], "--approved-by", "reviewer-a", *client)
        with mock.patch.object(claude_upgrade_seal, "_write_once_or_reuse", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_seal(*approve)
        self.assertFalse(stage_path.exists())
        after_store = self.store_footprint()
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "阶段尚未封存"):
            claude_upgrade._load_stage_result(self.campaign, "capture-candidate", CANDIDATE)
        code, sealed, stderr = self.run_seal(*approve)
        self.assertEqual(code, 0, stderr)
        assert sealed is not None
        self.assertEqual((sealed["reused"], sealed["store"]["checkpoint"]), (False, "candidate_sealed"))
        self.assertEqual(self.store_footprint(), after_store)
        self.assertTrue(stage_path.is_file())

    def test_metadata_drift_after_seal_is_rebound_and_readers_recover(self) -> None:
        self.seal_candidate()
        self.read_sealed_attempt()
        # 封存后证据被「复制回原路径」：内容不变，inode／mtime／ctime 变化。读侧报可恢复的元数据漂移并给出重绑命令。
        target = self.job_root("candidate-transport-baseline") / "v4-native-tls-baseline" / evidence.RELAY_MANIFEST_FILENAME
        content = target.read_bytes()
        target.unlink()
        target.write_bytes(content)
        target.chmod(0o600)
        with self.assertRaises(candidate_seal.SealedEvidenceBoundaryError) as caught:
            self.read_sealed_attempt()
        self.assertEqual(caught.exception.failure_class, evidence_manifest.METADATA_DRIFT_FAILURE_CLASS)
        self.assertIn(f"rebind-boundary --attempt-id {ATTEMPT} --candidate-id {CANDIDATE}", str(caught.exception))
        # 已封存 attempt 的批准重跑走同一读侧：同样先要求重绑。
        code, _result, stderr = self.run_seal(*self.approve_argv)
        self.assertEqual(code, 1)
        self.assertIn("rebind-boundary", stderr)
        # 重绑：逐文件复算内容一致后写重绑收据；之后读侧、失败定位输入与批准重跑都恢复。
        code, record, stderr = self.rebind("--candidate-id", CANDIDATE, "--operator", "tester")
        self.assertEqual(code, 0, stderr)
        assert record is not None
        self.assertEqual((record["status"], record["phase"], record["candidate_id"], record["drifted_entry_count"]), ("rebound", "candidate", CANDIDATE, 1))
        attempt_root, attempt = self.read_sealed_attempt()
        self.assertTrue((attempt_root / "evidence-manifest-rebind-01.json").is_file())
        stage = claude_upgrade._load_stage_result(self.campaign, "capture-candidate", CANDIDATE)
        self.assertTrue(evaluation_recovery.candidate_failure_scope_inputs(self.campaign, stage, attempt)["attempt_jobs"])
        code, again, stderr = self.run_seal(*self.approve_argv)
        self.assertEqual(code, 0, stderr)
        assert again is not None
        self.assertTrue(again["reused"])
        # 没有新的漂移时不重绑。
        code, _record, stderr = self.rebind("--candidate-id", CANDIDATE)
        self.assertEqual(code, 1)
        self.assertIn("无需 rebind", stderr)

    def test_content_drift_after_seal_is_not_rebindable(self) -> None:
        self.seal_candidate()
        target = self.job_root("candidate-transport-baseline") / "v4-native-tls-baseline" / "results" / "debug.log"
        content = target.read_bytes()
        rebind_receipt = self.campaign / "candidates" / CANDIDATE / "attempts" / ATTEMPT / "evidence-manifest-rebind-01.json"
        # 同样大小的内容改写：stat 边界只见 mtime／ctime 变化，读侧（不读内容）报元数据漂移；重绑逐文件复算内容即拒绝，不写收据。
        target.write_bytes(content.upper())
        with self.assertRaises(candidate_seal.SealedEvidenceBoundaryError) as caught:
            self.read_sealed_attempt()
        self.assertEqual(caught.exception.failure_class, evidence_manifest.METADATA_DRIFT_FAILURE_CLASS)
        code, _record, stderr = self.rebind("--candidate-id", CANDIDATE)
        self.assertEqual(code, 1)
        self.assertIn("证据文件内容与 EvidenceManifest 不一致", stderr)
        self.assertFalse(rebind_receipt.exists())
        with self.assertRaises(candidate_seal.SealedEvidenceBoundaryError):
            self.read_sealed_attempt()
        # 大小也变：读侧直接判完整性异常（不可恢复），重绑同样拒绝。
        target.write_bytes(content + b"tampered")
        with self.assertRaises(candidate_seal.SealedEvidenceBoundaryError) as caught:
            self.read_sealed_attempt()
        self.assertEqual(caught.exception.failure_class, "evidence-integrity")
        code, _record, stderr = self.rebind("--candidate-id", CANDIDATE)
        self.assertEqual(code, 1)
        self.assertIn("不能 rebind", stderr)
        self.assertFalse(rebind_receipt.exists())

    def test_rebind_requires_a_sealed_candidate_attempt(self) -> None:
        self.write_evidence_and_attempt()
        for extra, message in (
            ((), "必须给出 --candidate-id"),
            (("--candidate-id", CANDIDATE), "阶段尚未封存"),
        ):
            with self.subTest(extra=extra):
                code, _record, stderr = self.rebind(*extra)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)

    def test_half_sealed_interruption_with_metadata_drift_is_rebound_and_approval_finishes(self) -> None:
        attempt_root = self.write_evidence_and_attempt()
        client = self.write_client_evidence(attempt_root)
        code, preview, stderr = self.run_seal(*client)
        self.assertEqual(code, 2, stderr)
        assert preview is not None
        approve = ("--approve-seal-sha256", preview["review_sha256"], "--approved-by", "reviewer-a", *client)
        before = self.store_footprint()
        # 批准写完封存证据清单、写 Store 之前被打断：半封存（清单在、阶段结果与 Store 写入都没有）。
        with mock.patch.object(candidate_seal, "record_candidate_seal_chain", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_seal(*approve)
        self.assertTrue((attempt_root / candidate_seal.SEALED_EVIDENCE_MANIFEST_FILENAME).is_file())
        self.assertFalse((self.campaign / "candidates" / CANDIDATE / "result.json").exists())
        self.assertEqual(self.store_footprint(), before)
        # 期间证据被复制回原路径：run 末收口重放从此过不去（修复前重跑批准就卡在这里）。
        self.copy_back("candidate-transport-baseline", f"v4-native-tls-baseline/{evidence.RELAY_MANIFEST_FILENAME}")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "证据权限收口收据未通过"):
            claude_upgrade._load_capture_attempt(self.campaign, "candidate", CANDIDATE, ATTEMPT)
        # 重跑批准改按封存证据清单加重绑链核对：报可恢复的元数据漂移并给出重绑命令，一条不写。
        code, _result, stderr = self.run_seal(*approve)
        self.assertEqual(code, 1)
        self.assertIn(f"rebind-boundary --attempt-id {ATTEMPT} --candidate-id {CANDIDATE}", stderr)
        self.assertNotIn("证据权限收口收据未通过", stderr)
        self.assertEqual(self.store_footprint(), before)
        # 重绑接受半封存状态；之后重跑批准走完（Store 到 candidate_sealed、阶段结果写出），封存后读侧通过。
        code, record, stderr = self.rebind("--candidate-id", CANDIDATE)
        self.assertEqual(code, 0, stderr)
        assert record is not None
        self.assertEqual((record["status"], record["drifted_entry_count"]), ("rebound", 1))
        code, sealed, stderr = self.run_seal(*approve)
        self.assertEqual(code, 0, stderr)
        assert sealed is not None
        self.assertEqual((sealed["reused"], sealed["store"]["checkpoint"]), (False, "candidate_sealed"))
        self.read_sealed_attempt()

    def test_half_sealed_rebind_refuses_another_sealed_attempt(self) -> None:
        self.seal_candidate()
        code, _record, stderr = self.rebind("--candidate-id", CANDIDATE, "--attempt-id", "other-attempt")
        self.assertEqual(code, 1)
        self.assertIn(f"封存的是 attempt {ATTEMPT}", stderr)

    def test_client_evidence_must_be_registered_and_inside_the_attempt_directory(self) -> None:
        attempt_root = self.write_evidence_and_attempt()
        self.write_client_evidence(attempt_root)
        outside = _write_json(self.root / "claude-desktop.json", {"client_id": "claude-desktop"})
        for argv, message in (
            (["--client-evidence", f"claude-desktop={outside}"], "attempt 客户端证据目录内"),
            (["--client-evidence", f"kilo-compatible={attempt_root / 'client-evidence' / 'claude-desktop.json'}"], "不在场景清单登记"),
            (["--client-evidence", "claude-desktop"], "格式非法"),
        ):
            with self.subTest(argv=argv):
                code, _result, stderr = self.run_seal(*argv)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)

    def test_invalid_client_receipt_blocks(self) -> None:
        attempt_root = self.write_evidence_and_attempt()
        client = self.write_client_evidence(attempt_root)
        path = attempt_root / candidate_seal.CLIENT_EVIDENCE_DIRNAME / "claude-vscode.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["attempt_id"] = "another-attempt"
        _write_json(path, value)
        boundary = attempt_root / candidate_seal.CLIENT_EVIDENCE_DIRNAME / "third-party-ingress-rejection.json"
        value = json.loads(boundary.read_text(encoding="utf-8"))
        value["result"] = "fail"
        _write_json(boundary, value)
        code, result, stderr = self.run_seal(*client)
        self.assertEqual(code, 2, stderr)
        assert result is not None
        self.assertEqual(result["closure"]["reasons"], ["client_binding_invalid", "boundary_check_failed"])
        code, _approved, stderr = self.run_seal("--approve-seal-sha256", result["review_sha256"], "--approved-by", "reviewer-a", *client)
        self.assertEqual(code, 1)
        self.assertIn("闭合门禁 blocked", stderr)



# ---------------------------------------------------------------------------
# 门禁重试链：第 1 轮外部门禁失败 → 写失败轮次 → --revalidate 出第 2 轮 → B4-06a 写入链在第 2 轮到 ready
# ---------------------------------------------------------------------------

FAILED_GATE = "check-egress-spec"


def _write_private_json(gate_root: Path, path: Path, payload: Any) -> Path:
    """门禁收据根要求的私有文件：文件 0600，根以下的目录 0700（只动门禁收据根内）。"""

    _write_json(path, payload)
    path.chmod(0o600)
    for parent in path.relative_to(gate_root).parents:
        (gate_root / parent).chmod(0o700)
    return path
ROOT_CAUSE = "candidate-gate-check-egress-spec"


def write_gate_receipt(
    gate_root: Path,
    name: str,
    subject: dict[str, Any],
    *,
    failed: frozenset[str],
    hour: int,
    previous: str | None = None,
) -> str:
    """用 codex_upgrade_gate_receipt 原样生成候选外部门禁 v4 收据：``failed`` 里的门禁失败；``previous`` 给出时是补跑收据
    （只执行前序失败项、其余携带），返回证据根内的相对路径。"""

    stem = Path(name).stem
    gate_ids = sorted(gate_receipt.CANDIDATE_COMMANDS)
    if previous is not None:
        gate_ids = list(json.loads((gate_root / previous).read_text(encoding="utf-8"))["failed_gate_ids"])
    gates: list[dict[str, Any]] = []
    for index, gate_id in enumerate(gate_ids):
        working_directory, command = gate_receipt.CANDIDATE_COMMANDS[gate_id]
        passed = gate_id not in failed
        evidence_path = _write_private_json(gate_root, gate_root / "evidence" / f"{stem}-{gate_id}.json", {"gate_id": gate_id, "passed": passed})
        gates.append(
            {
                "gate_id": gate_id, "command": list(command), "working_directory": working_directory, "host": accept_tests.HOST,
                "architecture": accept_tests.ARCHITECTURE, "started_at_utc": f"2026-10-07T{hour:02d}:{index:02d}:00Z",
                "completed_at_utc": f"2026-10-07T{hour:02d}:{index:02d}:30Z", "exit_code": 0 if passed else 1,
                "status": "passed" if passed else "failed", "passed_count": 1 if passed else 0, "failed_count": 0 if passed else 1,
                "skipped_count": 0, "stdout_sha256": "6" * 64, "stderr_sha256": "7" * 64,
                "evidence": [{"path": evidence_path.relative_to(gate_root).as_posix(), "sha256": codex_upgrade.file_sha256(evidence_path)}],
            }
        )
    attempt_id = f"gate-{stem}"
    environment = {}
    for role in ("before", "after"):
        path = _write_private_json(gate_root, gate_root / "environment" / f"{attempt_id}-{role}.json", {"attempt_id": attempt_id, "role": role})
        environment[role] = {"path": path.relative_to(gate_root).as_posix(), "sha256": codex_upgrade.file_sha256(path)}
    previous_binding = None
    if previous is not None:
        previous_binding = {"path": previous, "sha256": codex_upgrade.file_sha256(gate_root / previous)}
    facts = {
        "schema_version": gate_receipt.FACTS_SCHEMA, "phase": gate_receipt.CANDIDATE_PHASE,
        "attempt": {"attempt_id": attempt_id, "root_cause_id": ROOT_CAUSE, "previous_receipt": previous_binding},
        "subject": subject, "inputs": [], "gate_plan": None, "environment": environment, "gates": gates,
    }
    _write_private_json(gate_root, gate_root / f"{stem}-facts.json", facts)
    gate_receipt.finalize(gate_root, f"{stem}-facts.json", name)
    return name


class CandidateRevalidateTests(_CandidateSealCase):
    def setUp(self) -> None:
        super().setUp()
        # 环境收据替身（与 B4-06a 验收测试、Codex 门禁收据测试同一做法）：前后两份都按 attempt 身份通过、连续性摘要相同。
        environment = gate_receipt.codex_upgrade_arm64_environment_receipt
        for name, side_effect in (
            ("replay", accept_tests._AcceptCase._replay_environment),
            ("receipts_equivalent", lambda left_root, left, right_root, right: left["continuity_identity_sha256"] == right["continuity_identity_sha256"]),
        ):
            patcher = mock.patch.object(environment, name, side_effect=side_effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.gate_root = self.root / "gate-evidence"
        self.gate_root.mkdir(mode=0o700)
        subject = accept.expected_gate_subject(self.manifest, self.world.candidate_payload, accept_tests.IDENTITY)
        self.subject = {**subject, "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None}

    def external_gate(self, receipt: str) -> dict[str, Any]:
        return {"evidence_root": str(self.gate_root), "receipt_path": receipt, "identity": dict(accept_tests.IDENTITY)}

    def revalidate(self) -> tuple[int, dict | None, str]:
        return self.run_seal("--revalidate")

    def gate_receipts(self, attempt_ref: dict[str, Any]) -> dict[str, dict[str, Any]]:
        store = self.world.store
        receipts = [store.load_receipt(reference) for reference in store.list_receipt_refs("validation_gate")]
        return {item["gate_id"]: item for item in receipts if item["validation_attempt_ref"] == attempt_ref}

    def test_failed_round_revalidates_to_round_two_and_accept_reaches_ready(self) -> None:
        _attempt_root, _client, sealed = self.seal_candidate()
        round_one = sealed["store"]
        # 第 1 轮还没有失败门禁：门禁重试拒绝。
        code, _result, stderr = self.revalidate()
        self.assertEqual(code, 1)
        self.assertIn("还没有全部定稿或没有失败门禁", stderr)
        # 第 1 轮外部门禁失败：v4 收据只能经 record_failed_gate_round 写进 Store（通过的记 pass、失败的记 failed），
        # 全部通过的写入链拒绝它。
        failed_receipt = write_gate_receipt(self.gate_root, "round-1-receipt.json", self.subject, failed=frozenset({FAILED_GATE}), hour=1)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不是全部通过"):
            accept.candidate_external_gate_binding(evidence_root=self.gate_root, receipt_path=Path(failed_receipt), expected_subject=accept.expected_gate_subject(self.manifest, self.world.candidate_payload, accept_tests.IDENTITY))
        before = self.store_footprint()
        request = {"candidate_id": CANDIDATE, "validation_attempt_ref": round_one["validation_attempt_ref"], "external_gate": self.external_gate(failed_receipt)}
        recorded = accept.record_failed_gate_round(self.campaign, self.manifest, request)
        self.assertEqual((recorded["status"], recorded["failed_gate_ids"]), ("failed", [FAILED_GATE]))
        self.assertEqual({item["gate_id"]: item["result"] for item in recorded["gate_receipts"]}, {gate: ("failed" if gate == FAILED_GATE else "pass") for gate in gate_receipt.CANDIDATE_COMMANDS})
        facts_after_failure = self.store_footprint()
        self.assertEqual(facts_after_failure[0], before[0])
        # 失败轮次写入幂等：同一收据重写逐条复用。
        again = accept.record_failed_gate_round(self.campaign, self.manifest, request)
        self.assertTrue(all(item["reused"] for item in again["gate_receipts"]))
        round_one_gates = self.gate_receipts(round_one["validation_attempt_ref"])
        # 门禁重试：第 2 轮的执行计划——已通过的门禁复用原收据，失败的重跑并绑定最近一份失败收据；零请求、不重扫。
        code, revalidated, stderr = self.revalidate()
        self.assertEqual(code, 0, stderr)
        assert revalidated is not None
        self.assertEqual((revalidated["validation_round"], revalidated["reused"], revalidated["live_request_count"]), (2, False, 0))
        self.assertEqual(revalidated["store"]["validation_attempt_id"], f"{ATTEMPT}-v2")
        refs = {item["gate_id"]: ref for item in recorded["gate_receipts"] for ref in [item["receipt_ref"]]}
        gates = {item["gate_id"]: item for item in revalidated["store"]["external_gates"]}
        self.assertEqual(gates[FAILED_GATE]["source"], "execute")
        self.assertEqual(gates[FAILED_GATE]["previous_failed_receipt_ref"], refs[FAILED_GATE])
        for gate_id in set(gates) - {FAILED_GATE}:
            self.assertEqual((gates[gate_id]["source"], gates[gate_id]["reused_receipt_ref"]), ("reuse", refs[gate_id]))
        self.assertEqual(round_one_gates[FAILED_GATE]["result"], "failed")
        plan = self.world.store.load_object(revalidated["store"]["validation_execution_plan_ref"])["payload"]
        self.assertEqual(plan["reuse_item_ids"], sorted(set(gates) - {FAILED_GATE}))
        self.assertIn(FAILED_GATE, plan["execute_item_ids"])
        # 复用第 1 轮 selector 前态；换新 ValidationAttempt、场景四阶段与证据包。
        self.assertEqual(revalidated["store"]["selector_before_ref"], round_one["selector_before_ref"])
        self.assertNotEqual(revalidated["store"]["candidate_evidence_package_ref"], round_one["candidate_evidence_package_ref"])
        # 读侧取最大轮次：第 2 轮结果承接第 1 轮、绑定同一 attempt 与同一份评估用 manifest。
        stage = claude_upgrade._load_stage_result(self.campaign, "capture-candidate", CANDIDATE)
        self.assertEqual((stage["validation_round"], stage["store"]["validation_attempt_id"]), (2, f"{ATTEMPT}-v2"))
        self.assertTrue((self.campaign / "candidates" / CANDIDATE / "validation-rounds" / "v2" / "result.json").is_file())
        round_one_doc = json.loads((self.campaign / "candidates" / CANDIDATE / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(stage[candidate_seal.ASSERTION_CONTEXT_FIELD], round_one_doc[candidate_seal.ASSERTION_CONTEXT_FIELD])
        self.read_sealed_attempt()
        # 第 2 轮重复调用：Store 写入与结果逐字复用。
        footprint = self.store_footprint()
        result_bytes = (self.campaign / "candidates" / CANDIDATE / "validation-rounds" / "v2" / "result.json").read_bytes()
        code, repeated, stderr = self.revalidate()
        self.assertEqual(code, 0, stderr)
        assert repeated is not None
        self.assertEqual((repeated["validation_round"], repeated["reused"]), (2, True))
        self.assertEqual(self.store_footprint(), footprint)
        self.assertEqual((self.campaign / "candidates" / CANDIDATE / "validation-rounds" / "v2" / "result.json").read_bytes(), result_bytes)
        # B4-06a 写入链在第 2 轮：补跑收据只执行前序失败项、携带其余；门禁定稿复用第 1 轮通过的收据，Store 到 ready。
        retry_receipt = write_gate_receipt(self.gate_root, "round-2-receipt.json", self.subject, failed=frozenset(), hour=2, previous=failed_receipt)
        row = dict(self.world.rows()[0], scenario_approval_refs=[sorted(stage["store"]["scenario_approval_refs"].values(), key=canonical_sha256)[0]])
        accepted = accept.record_acceptance_chain(
            self.campaign,
            self.manifest,
            {
                "candidate_id": CANDIDATE,
                "validation_attempt_ref": stage["store"]["validation_attempt_ref"],
                "candidate_evidence_package_ref": stage["store"]["candidate_evidence_package_ref"],
                "pair_rows": [row],
                "external_gate": self.external_gate(retry_receipt),
                "inventory_assertion_refs": [self.world.refs["operational-evidence"]],
                "selector_after_snapshot_ref": self.world.refs["runtime-before"],
                "issued_at_utc": "2026-10-07T03:00:00Z",
            },
        )
        self.assertEqual(sorted(record["gate_id"] for record in accepted["gate_receipts"]), sorted(gate_receipt.CANDIDATE_COMMANDS))
        self.assertEqual(self.world.status()["checkpoint"], "ready")
        # 第 2 轮门禁已全部通过：再门禁重试只会复用第 2 轮（不会开第 3 轮）。
        code, final, stderr = self.revalidate()
        self.assertEqual(code, 0, stderr)
        assert final is not None
        self.assertEqual((final["validation_round"], final["reused"]), (2, True))

    def test_failed_round_rejects_passed_receipts_and_writes_nothing_on_conflict(self) -> None:
        _attempt_root, _client, sealed = self.seal_candidate()
        attempt_ref = sealed["store"]["validation_attempt_ref"]
        passed = write_gate_receipt(self.gate_root, "all-passed.json", self.subject, failed=frozenset(), hour=1)
        before = self.store_footprint()
        receipts_before = len(self.world.store.list_receipt_refs("validation_gate"))
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "全部通过的收据走 record_acceptance_chain"):
            accept.record_failed_gate_round(self.campaign, self.manifest, {"candidate_id": CANDIDATE, "validation_attempt_ref": attempt_ref, "external_gate": self.external_gate(passed)})
        wrong = dict(accept_tests.IDENTITY, profile_digest="9" * 64)
        failed = write_gate_receipt(self.gate_root, "round-1-receipt.json", self.subject, failed=frozenset({FAILED_GATE}), hour=1)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "subject 与 Campaign／候选身份不一致"):
            accept.record_failed_gate_round(
                self.campaign, self.manifest,
                {"candidate_id": CANDIDATE, "validation_attempt_ref": attempt_ref, "external_gate": dict(self.external_gate(failed), identity=wrong)},
            )
        self.assertEqual((self.store_footprint(), len(self.world.store.list_receipt_refs("validation_gate"))), (before, receipts_before))
        accept.record_failed_gate_round(self.campaign, self.manifest, {"candidate_id": CANDIDATE, "validation_attempt_ref": attempt_ref, "external_gate": self.external_gate(failed)})
        # 同一 attempt 换一份结果不同的失败收据：门禁收据不得覆盖，一条不写。
        other = write_gate_receipt(self.gate_root, "round-1-other.json", self.subject, failed=frozenset({FAILED_GATE, "full-regression"}), hour=4)
        count = len(self.world.store.list_receipt_refs("validation_gate"))
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已有不同结果"):
            accept.record_failed_gate_round(self.campaign, self.manifest, {"candidate_id": CANDIDATE, "validation_attempt_ref": attempt_ref, "external_gate": self.external_gate(other)})
        self.assertEqual(len(self.world.store.list_receipt_refs("validation_gate")), count)
