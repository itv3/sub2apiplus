"""B4-06b 真实批次链（compare → assert-rules → accept）的测试夹具与输入源接缝（只在测试里，不进受管树）。

世界是 B3-01 的真实批次夹具：VC-3／VC-4 真实批次走到 Store ``candidate_sealed``（``_RealTreeBatchCase``），之后 compare、
assert-rules 与 accept 都以 ``compile-and-run-vc-batch`` 的 VC-5 批次派发，在父监督器的动作子进程里执行；Store 的 VC-5 前缀由
B4-02 的真实写入层 ``claude_upgrade_candidate_seal.record_candidate_seal_chain`` 写入，验收链由 B4-06a 的
``record_acceptance_chain`` 写入。compare 与 accept 本身不打桩，接缝只在输入源层（B5b-02 录制回放链接手时逐个换成真实来源）：

1. **画像内容**：``claude_rule_assertion.build_expectations`` 的输入换成评估器夹具口径——Store 台账与 VC-2 批准的目标规则清单
   照旧（与真实目标规则全集同为 39 条、原子断言 ID 不变），场景清单只让 ``transport.http`` 覆盖全部规则，每条原子断言套用
   评估器夹具 ``SPEC-HDR-001`` 的两条判据模板，Release Wire 用评估器夹具的；生成器本身是真实的。冻结在测试进程里完成
   （write-once），之后 compare 子进程只读冻结结果。真实来源：真实台账、真实场景清单与判据模板库。
2. **官方评估来源**：``claude_upgrade_compare.official_evaluation_source`` 换成评估器夹具官方侧（批次夹具的 VC-1 官方包是
   ``opaque_bound_source`` 合成数据，没有中继连接索引、网络面与深度扫描清单，不可评估）。真实来源：录制的 VC-1 官方证据。
3. **候选证据与候选阶段结果**：评估器夹具候选侧落到宿主数据根内的接缝 attempt 目录（``<Campaign>/seam/candidate-attempts/
   <attempt>/evidence/``，不放进 ``candidates/<候选>/attempts/``，免得被静止判据与污染扫描当作真实 attempt），候选阶段结果按
   B4-02 合同写满全部字段（走真实读侧）；seal 预览、草稿、CaptureIndex 与客户端收据是合成的。真实来源：候选 run（B4-01b）
   与候选 seal 预览／批准（B4-02）。
4. **候选 attempt 上下文**：``claude_upgrade._capture_stage_attempt_context`` 换成合成 attempt（含 c1 定形的候选激活事实登记
   ``gateway_environment.activation_fact``）。真实来源：候选 run 写出的 attempt 与封存证据清单。
5. **门禁环境收据**：默认沿用 B4-06a 的旧替身。typed VC-6 新世界显式选 ``real_receipts`` 模式，以离线合成 ARM64
   事实走原 producer finalizer 与门禁 finalizer；父进程和独立 worker 都调用原 replay，不安装环境重放或等价比较替身。
   这只补齐新测试世界的读侧事实，不代表 ARM64 现场实采。

子进程里的接缝经 ``usercustomize`` 注入：只在 ``SEAM_ACTIONS_ENV`` 列出的外层动作里生效，配置是测试写的一份 JSON。
独立规则检查器虽然继承父动作 ID，也只读已经冻结的输入，不重复安装这套来源接缝。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping
from unittest import mock

from tools.official_client_capture import claude_build_runtime_audit as runtime_audit
from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture import claude_upgrade_accept as accept
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import codex_upgrade_gate_receipt as gate_receipt
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator_fixture
from tools.official_client_capture.tests import claude_client_receipt_fixture
from tools.official_client_capture.tests.control_receipt_fixtures import create_arm_receipt

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SEAM_CONFIG_ENV = "CLAUDE_VC5_CHAIN_SEAMS"
SEAM_ACTIONS_ENV = "CLAUDE_VC5_CHAIN_SEAM_ACTIONS"
SEAM_REPOSITORY_ENV = "CLAUDE_VC5_CHAIN_SEAM_REPOSITORY"
LEGACY_GATE_ENVIRONMENT = "legacy"
REAL_GATE_ENVIRONMENT = "real_receipts"
TEMPLATE_ATOMIC = "SPEC-HDR-001"
EVIDENCE_SCENARIO = "transport.http"
PROBE_REQUEST_PATHS = {"failure-529": ["/v1/messages"], "print": ["/v1/messages", "/v1/messages/count_tokens"], "tls": []}

USERCUSTOMIZE = f'''"""B4-06b 测试注入（不进受管树）：列出的 VC-5 动作子进程里安装输入源接缝。"""
import os
import sys

if os.environ.get("{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV}") in os.environ.get("{SEAM_ACTIONS_ENV}", "").split(","):
    repository = os.environ["{SEAM_REPOSITORY_ENV}"]
    checker = os.path.realpath(os.path.join(repository, "tools", "official_client_capture", "claude_rule_assertion.py"))
    # 只排除权威命令构造器直接派发的检查器；同名文件与外层动作仍保持原接缝。
    if os.path.realpath(sys.argv[0]) != checker:
        sys.path.insert(0, repository)
        from tools.official_client_capture.tests import claude_vc5_chain_fixture

        claude_vc5_chain_fixture.install(os.environ["{SEAM_CONFIG_ENV}"])
'''


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def binding(path: Path, root: Path, *, with_bytes: bool = True) -> dict[str, Any]:
    value: dict[str, Any] = {"path": Path(path).relative_to(root).as_posix(), "sha256": _sha256(path)}
    if with_bytes:
        value["bytes"] = Path(path).stat().st_size
    return value


# ---------------------------------------------------------------------------
# 接缝一：画像内容
# ---------------------------------------------------------------------------


def seam_build_expectations(real: Any) -> Any:
    """``build_expectations`` 的输入换成评估器夹具口径（见模块说明第 1 条），台账与目标规则清单照旧，再调真实生成器。"""

    def build(*, claude_version: str, ledger: Mapping[str, Any], scenarios_payload: Mapping[str, Any], templates: Mapping[str, Any], release_profile: Any = None, release_wire: Any = None, rule_manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
        del scenarios_payload, templates, release_profile, release_wire
        owners = sorted({str(item["owner_id"]) for item in ledger["assertions"] if item["owner_kind"] == "required_rule"})
        atomic_ids = sorted(str(item["assertion_id"]) for item in ledger["assertions"] if item["owner_kind"] == "required_rule")
        scenarios = copy.deepcopy(evaluator_fixture.SCENARIOS_PAYLOAD)
        scenarios["claude_version"] = claude_version
        for scenario in scenarios["evidence_scenarios"]:
            scenario["covers"] = list(owners) if scenario["scenario_id"] == EVIDENCE_SCENARIO else []
        template = evaluator_fixture.TEMPLATES["templates"][TEMPLATE_ATOMIC]
        seam_templates = {
            "schema_version": assertion.TEMPLATES_SCHEMA_VERSION,
            "description": "B4-06b 真实批次链的画像接缝：每条原子断言套用评估器夹具 SPEC-HDR-001 的判据",
            "templates": {atomic_id: copy.deepcopy(template) for atomic_id in atomic_ids},
        }
        return real(
            claude_version=claude_version,
            ledger=ledger,
            scenarios_payload=scenarios,
            templates=seam_templates,
            release_profile=None,
            release_wire=copy.deepcopy(evaluator_fixture.RELEASE_WIRE),
            rule_manifest=rule_manifest,
        )

    return build


@contextlib.contextmanager
def profile_seam() -> Iterator[None]:
    real = assertion.build_expectations
    with mock.patch.object(assertion, "build_expectations", side_effect=seam_build_expectations(real)):
        yield


# ---------------------------------------------------------------------------
# 接缝二：官方评估来源
# ---------------------------------------------------------------------------


def surfaces(paths: Mapping[str, list[str]]) -> list[dict[str, Any]]:
    counted: dict[str, int] = {}
    for values in paths.values():
        for path in values:
            key = f"api.anthropic.com POST {path}?beta=true HTTP/1.1"
            counted[key] = counted.get(key, 0) + 1
    return [{"surface": key, "count": counted[key]} for key in sorted(counted)]


def official_source(side: evaluator_fixture.SideEvidence, package_ref: Mapping[str, Any]) -> dict[str, Any]:
    """评估器夹具官方侧 → ``official_evaluation_source`` 的返回形状（字段闭集 ``SOURCE_FIELDS``）。"""

    manifest = json.loads(side.manifest_path.read_text(encoding="utf-8"))
    return {
        "mode": "predecessor_reuse",
        "evidence_root": str(side.evidence_root),
        "capture_id": str(manifest["capture_id"]),
        "evaluation_manifest": manifest,
        "package_ref": dict(package_ref),
        "capture_manifest": {"path": "seam/official/capture-manifest.json", "sha256": "e" * 64, "bytes": 1},
        "capture_index": {"path": "seam/official/capture-index.json", "sha256": "e" * 64, "bytes": 1},
        "inventory": {"path": "seam/official/evidence-manifest.json", "sha256": "e" * 64, "source": "seam"},
        "surfaces": surfaces(PROBE_REQUEST_PATHS),
        "probe_request_paths": dict(PROBE_REQUEST_PATHS),
    }


# ---------------------------------------------------------------------------
# 接缝的安装（测试进程与动作子进程共用）
# ---------------------------------------------------------------------------


def _replay_environment(root: Path, relative: str) -> dict[str, Any]:
    del root
    attempt_id, role = Path(relative).stem.rsplit("-", 1)
    return {"status": "passed", "phase": "gate_before" if role == "before" else "gate_after", "subject_id": attempt_id, "continuity_identity_sha256": "8" * 64}


def gate_environment_mode(config: Mapping[str, Any]) -> str:
    """新世界须显式选择真实封存合同；未声明时保持旧夹具行为，拼错模式直接拒绝。"""

    mode = config.get("gate_environment_mode", LEGACY_GATE_ENVIRONMENT)
    if mode not in (LEGACY_GATE_ENVIRONMENT, REAL_GATE_ENVIRONMENT):
        raise ValueError("门禁环境夹具模式须为 legacy 或 real_receipts")
    return mode


def patchers(config: Mapping[str, Any]) -> list[Any]:
    """按配置组出输入源接缝，并按可选的运行源布局登记映射；正式布局校验仍由产品读侧执行。"""

    from tools.official_client_capture import claude_upgrade
    from tools.official_client_capture import claude_upgrade_compare

    environment_mode = gate_environment_mode(config)
    result: list[Any] = [claude_client_receipt_fixture.source_patch()]
    if config.get("capture_layout") is not None:
        # 只登记外部运行源已冻结的两个值；不从 capture_root 推导别名、不替换正式布局校验器。
        layout = config["capture_layout"]
        result.append(mock.patch.object(claude_upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", Path(layout["host_runs_root"])))
        result.append(mock.patch.object(claude_upgrade, "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS",
            tuple(PurePosixPath(value) for value in layout["container_run_roots"])))
    if config.get("official_source"):
        source = json.loads(Path(config["official_source"]).read_text(encoding="utf-8"))
        result.append(mock.patch.object(claude_upgrade_compare, "official_evaluation_source", side_effect=lambda *a, **k: copy.deepcopy(source)))
    if config.get("attempt"):
        attempt = json.loads(Path(config["attempt"]).read_text(encoding="utf-8"))
        root = Path(config["attempt_root"])
        result.append(mock.patch.object(claude_upgrade, "_capture_stage_attempt_context", side_effect=lambda *a, **k: (root, copy.deepcopy(attempt))))
    if environment_mode == LEGACY_GATE_ENVIRONMENT:
        environment = gate_receipt.codex_upgrade_arm64_environment_receipt
        result.append(mock.patch.object(environment, "replay", side_effect=_replay_environment))
        result.append(
            mock.patch.object(
                environment,
                "receipts_equivalent",
                side_effect=lambda left_root, left, right_root, right: left["continuity_identity_sha256"] == right["continuity_identity_sha256"],
            )
        )
    return result


def install(config_path: str) -> None:
    """动作子进程入口：按配置安装接缝，进程退出前一直生效。"""

    for patcher in patchers(json.loads(Path(config_path).read_text(encoding="utf-8"))):
        patcher.start()


@contextlib.contextmanager
def seams(config_path: Path) -> Iterator[None]:
    """测试进程里的同一组接缝。"""

    with contextlib.ExitStack() as stack:
        for patcher in patchers(json.loads(Path(config_path).read_text(encoding="utf-8"))):
            stack.enter_context(patcher)
        yield


def subprocess_environment(inject_dir: Path, config_path: Path, actions: list[str]) -> dict[str, str]:
    inject_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (inject_dir / "usercustomize.py").write_text(USERCUSTOMIZE, encoding="utf-8")
    return {
        "PYTHONPATH": str(inject_dir),
        SEAM_CONFIG_ENV: str(config_path),
        SEAM_ACTIONS_ENV: ",".join(actions),
        SEAM_REPOSITORY_ENV: str(REPOSITORY_ROOT),
    }


# ---------------------------------------------------------------------------
# 接缝三：候选证据、候选 attempt、Store VC-5 前缀与候选阶段结果
# ---------------------------------------------------------------------------


def activation_fact(build: Mapping[str, Any], target_version: str) -> dict[str, str]:
    """候选 Runtime（validation_candidate）的激活事实：画像链四项与部署身份取 VC-4 构建收据，``event_id`` 按 Go 口径复算。"""

    fact = {
        "schema_version": runtime_audit.ACTIVATION_SCHEMA,
        "source": runtime_audit.ACTIVATION_SOURCE,
        "event_type": runtime_audit.ACTIVATION_EVENT,
        "event_id": "",
        "observed_at_utc": "2026-10-08T01:00:00Z",
        "profile_mode": runtime_audit.PROFILE_MODE_CANDIDATE,
        "claude_version": target_version,
        "profile_digest": str(build["profile"]["profile_digest"]),
        "wire_digest": str(build["profile"]["wire_sha256"]),
        "release_digest": str(build["profile"]["release_sha256"]),
        "bundle_digest": str(build["profile"]["bundle_sha256"]),
        "approval_digest": "",
        "image_id": str(build["image"]["image_id"]),
        "image_reference": str(build["image"]["reference"]),
        "source_tree_sha256": str(build["source"]["tree_sha256"]),
        "build_id": str(build["build"]["build_id"]),
        "deployed_version": str(build["deployed_version"]),
    }
    fact["event_id"] = runtime_audit.activation_event_id(fact)
    return {field: fact[field] for field in runtime_audit.FACT_FIELDS}


def materialize_candidate(
    *,
    campaign: Path,
    data_root: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    attempt_id: str,
    side: evaluator_fixture.SideEvidence,
    build: Mapping[str, Any],
    run_nonce: str,
) -> dict[str, Any]:
    """把评估器夹具候选侧落到候选 attempt 坐标，写激活事实与客户端收据，合成 seal 草稿、CaptureIndex 与评估用 manifest。"""

    # 合成 attempt 不放进 ``candidates/<候选>/attempts/``：那里的目录会被静止判据与污染扫描当作真实 attempt 读预约收据；
    # 候选 attempt 上下文本来就是接缝（模块说明第 4 条），放在 Campaign 内的接缝目录即可（证据仍在宿主数据根内）。
    attempt_root = campaign / "seam" / "candidate-attempts" / attempt_id
    evidence_root = attempt_root / "evidence"
    shutil.copytree(side.evidence_root, evidence_root)
    prefix = evidence_root.relative_to(data_root).as_posix()
    fact = activation_fact(build, str(manifest["target_version"]))
    fact_path = _write_json(evidence_root / "environment" / "claude-egress-activation-fact.json", fact)
    files = sorted(path for path in evidence_root.rglob("*") if path.is_file())
    inventory = {path.relative_to(data_root).as_posix(): {"sha256": _sha256(path), "bytes": path.stat().st_size} for path in files}
    capture = copy.deepcopy(side.capture_manifest)
    capture.update({"side": "candidate", "candidate_id": candidate_id, "attempt": {"attempt_id": attempt_id}})
    for artifact in capture["artifacts"]:
        artifact["path"] = f"{prefix}/{artifact['path']}"
    seal_dir = campaign / "candidates" / candidate_id / "seal" / "g-seam"
    capture_path = _write_json(seal_dir / "drafts" / "capture-manifest.json", capture)
    index = {
        "schema_version": evidence.CANDIDATE_CAPTURE_INDEX_SCHEMA,
        "side": "candidate",
        "campaign_id": manifest["campaign_id"],
        "candidate_id": candidate_id,
        "attempt": {"attempt_id": attempt_id},
        "jobs": [
            {
                "job_id": "job-main",
                "evidence_root": f"{prefix}/job-main",
                "probes": [
                    {"probe_id": probe, "connections": [{"requests": [f"POST {path}?beta=true HTTP/1.1" for path in paths]}]}
                    for probe, paths in sorted(PROBE_REQUEST_PATHS.items())
                ],
            }
        ],
        "network_inventory": {"host_prefilter_disabled": True, "surfaces": surfaces(PROBE_REQUEST_PATHS)},
        "result": "passed",
    }
    index_path = _write_json(seal_dir / "drafts" / "capture-index.json", index)
    inventory[capture_path.relative_to(data_root).as_posix()] = {"sha256": _sha256(capture_path), "bytes": capture_path.stat().st_size}
    evaluation = assertion.bind_capture_manifest(capture, inventory, capture_id=attempt_id)
    evaluation_path = _write_json(seal_dir / "evaluation-manifest.json", evaluation)
    entries = [{"path": path, **inventory[path]} for path in sorted(inventory)]
    evidence_manifest = _write_json(
        seal_dir / "evidence-manifest.json",
        {"schema_version": evidence.EVIDENCE_INVENTORY_SCHEMA, "entry_count": len(entries), "total_bytes": sum(item["bytes"] for item in entries), "entries": entries, "digest": "0" * 64},
    )
    seal_draft = _write_json(
        seal_dir / "seal-draft.json",
        {"drafts": {"capture_manifest": binding(capture_path, campaign), "capture_index": binding(index_path, campaign)}},
    )
    preview = _write_json(seal_dir / "seal-preview.json", {"seam": "B4-06b 合成预览"})
    checkpoint = _write_json(seal_dir / "client-checkpoint.json", {"seam": "B4-06b 合成客户端检查点"})
    sealed_manifest = _write_json(attempt_root / "evidence-manifest.json", {"seam": "B4-06b 合成封存证据清单"})
    values = {"campaign_id": manifest["campaign_id"], "candidate_id": candidate_id, "attempt_id": attempt_id, "run_nonce": run_nonce}
    client_dir = attempt_root / "client-evidence"
    attempt = {
        "attempt_id": attempt_id,
        "candidate_id": candidate_id,
        "status": "awaiting_receipts",
        "run_nonce": run_nonce,
        "started_at_utc": "2026-10-08T00:30:00Z",
        # 候选 attempt 身份里驱动阶段解析（B5b-01b）核对的那几项，取 VC-4 构建收据（候选 run 的入口门同一取法）。
        "identity": {
            "image_id": str(build["image"]["image_id"]),
            "image_reference": str(build["image"]["reference"]),
            "build_id": str(build["build"]["build_id"]),
            "source_tree_sha256": str(build["source"]["tree_sha256"]),
            "git_commit": str(build["source"]["git_commit"]),
            "source_root": str(build["source"]["root"]),
            "deployed_version": str(build["deployed_version"]),
            "profile_digest": str(build["profile"]["profile_digest"]),
        },
        "gateway_environment": {
            "activation_fact": {
                "path": fact_path.relative_to(evidence_root).as_posix(),
                "sha256": _sha256(fact_path),
                "bytes": fact_path.stat().st_size,
                "source_path": "/var/lib/sub2api/claude-egress-activation-fact.json",
                "schema_version": runtime_audit.ACTIVATION_SCHEMA,
                "profile_mode": runtime_audit.PROFILE_MODE_CANDIDATE,
                "event_id": fact["event_id"],
            }
        },
    }
    attempt_path = _write_json(attempt_root / "attempt.json", attempt)
    clients = {
        "client_bindings": {
            client: claude_client_receipt_fixture.write_receipt(attempt_root, client, values)
            for client in ("claude-desktop", "claude-vscode")
        },
        "boundary_checks": {
            check: _write_json(client_dir / f"{check}.json", {"check_id": check, **values, "result": "pass"})
            for check in ("codex-final-wire-isolation", "third-party-ingress-rejection")
        },
    }
    attempt = json.loads(attempt_path.read_bytes())
    raw = [
        {"path": path, **inventory[path]}
        for path in sorted(inventory)
    ]
    return {
        "attempt_root": attempt_root,
        "attempt_path": attempt_path,
        "attempt": attempt,
        "evaluation_path": evaluation_path,
        "capture_path": capture_path,
        "evidence_manifest": evidence_manifest,
        "seal_draft": seal_draft,
        "preview": preview,
        "client_checkpoint": checkpoint,
        "sealed_manifest": sealed_manifest,
        "clients": clients,
        "raw_inventory": raw,
        "relay_manifest": f"{prefix}/job-main/print/relay-manifest.json",
    }


def seal_request(
    *,
    campaign: Path,
    data_root: Path,
    candidate_id: str,
    attempt_id: str,
    scenario_ids: list[str],
    materialized: Mapping[str, Any],
    selector_snapshot: Mapping[str, Any],
    architecture: str,
    host_id: str,
    run_nonce: str,
    issued_at: str,
) -> dict[str, Any]:
    """B4-02 写入层的封存请求：每个 ScenarioPlan 场景绑定候选 print probe 的中继清单，运行证据全部通过。"""

    raw = list(materialized["raw_inventory"])
    by_path = {item["path"]: item for item in raw}
    relay = by_path[materialized["relay_manifest"]]
    capture = materialized["capture_path"].relative_to(data_root).as_posix()
    passed = [{"id": "fixture-pass", "facts": {"result": "pass"}}]
    return {
        "candidate_id": candidate_id,
        "capture_attempt_id": attempt_id,
        "validation_round": 1,
        "issued_at_utc": issued_at,
        "run_conditions": {
            "run_nonce": run_nonce,
            "environment_sha256": "4" * 64,
            "configuration_sha256": "5" * 64,
            "network_sha256": "6" * 64,
            "account_model_sha256": "a" * 64,
        },
        "selector_before_snapshot": dict(selector_snapshot),
        "gate_execution": {"host_id": host_id, "architecture": architecture},
        "job_ids": {"execute": ["claude-candidate-core"], "reuse": []},
        "scenario_evidence": {scenario: [dict(relay)] for scenario in scenario_ids},
        "seal": {
            "generation": "g-seam",
            "review_sha256": "9" * 64,
            "reviewer": "reviewer-a",
            "evidence_manifest": binding(materialized["evidence_manifest"], data_root),
            "capture_manifest": dict(by_path[capture]),
        },
        "raw_evidence_inventory": raw,
        "operational_evidence": {name: list(passed) for name in ("environment_before", "environment_after", "recovery", "secret_scan")},
    }


def candidate_stage_result(
    *,
    campaign: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    materialized: Mapping[str, Any],
    recorded: Mapping[str, Any],
    store_checkpoint: str,
) -> Path:
    """按 B4-02 的候选阶段结果合同（``STAGE_RESULT_FIELDS``）写满全部字段；Store 摘要取真实写入层的返回。"""

    stage = {
        "schema_version": "claude-upgrade-candidate-stage-result/v1",
        "client": "claude",
        "campaign_id": manifest["campaign_id"],
        "stage": "capture-candidate",
        "candidate_id": candidate_id,
        "status": "complete",
        "attempt": binding(materialized["attempt_path"], campaign, with_bytes=False),
        "seal": {
            "generation": "g-seam",
            "preview": binding(materialized["preview"], campaign),
            "client_checkpoint": binding(materialized["client_checkpoint"], campaign),
            "evidence_manifest": binding(materialized["evidence_manifest"], campaign),
            "seal_draft": binding(materialized["seal_draft"], campaign),
            "review_sha256": "9" * 64,
            "reviewer": "reviewer-a",
        },
        "store": {
            "root": "control-store",
            "campaign_id": manifest["campaign_id"],
            "checkpoint": store_checkpoint,
            "validation_attempt_id": recorded["validation_attempt_id"],
            "validation_attempt_ref": recorded["validation_attempt_ref"],
            "validation_execution_plan_ref": recorded["validation_execution_plan_ref"],
            "selector_before_ref": recorded["selector_before"]["fact_ref"],
            "candidate_evidence_package_ref": recorded["candidate_evidence_package_ref"],
            "scenario_approval_refs": {scenario: dict(ref) for scenario, ref in sorted(recorded["scenario_approval_refs"].items())},
        },
        "client_evidence": {
            group: {item: binding(path, campaign, with_bytes=False) for item, path in sorted(files.items())}
            for group, files in materialized["clients"].items()
        },
        "sealed_evidence_manifest": binding(materialized["sealed_manifest"], campaign, with_bytes=False),
        "assertion_context": {
            "evaluation_manifest": binding(materialized["evaluation_path"], campaign, with_bytes=False),
            "capture_manifest": binding(materialized["capture_path"], campaign, with_bytes=False),
        },
        "execute_item_ids": ["claude-candidate-core"],
        "reuse_item_ids": [],
        "live_request_count": 3,
        "scanned_bytes": sum(int(item["bytes"]) for item in materialized["raw_inventory"]),
        "sealed_at_utc": "2026-10-08T01:30:00Z",
    }
    return _write_json(campaign / "candidates" / candidate_id / "result.json", stage)


# ---------------------------------------------------------------------------
# 候选外部门禁收据（codex_upgrade_gate_receipt 原样生成，B4-06a 测试同一做法）
# ---------------------------------------------------------------------------


def _private_json(root: Path, path: Path, payload: Any) -> Path:
    """门禁收据根要求的私有文件：文件 0600，根以下的目录 0700（只动门禁收据根内）。"""

    _write_json(path, payload)
    for parent in path.relative_to(root).parents:
        (root / parent).chmod(0o700)
    return path


def gate_receipt_file(
    root: Path,
    *,
    name: str,
    subject: Mapping[str, Any],
    host: str,
    architecture: str,
    failed_gate: str | None = None,
    environment_mode: str = LEGACY_GATE_ENVIRONMENT,
) -> str:
    """生成候选外部门禁 v4；真实合同模式必须提供可独立重放的前后环境事实，默认保持旧夹具。"""

    environment_mode = gate_environment_mode({"gate_environment_mode": environment_mode})
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = Path(name).stem
    gates: list[dict[str, Any]] = []
    for index, gate_id in enumerate(sorted(gate_receipt.CANDIDATE_COMMANDS)):
        working_directory, command = gate_receipt.CANDIDATE_COMMANDS[gate_id]
        passed = gate_id != failed_gate
        proof = _private_json(root, root / "evidence" / f"{stem}-{gate_id}.json", {"gate_id": gate_id, "passed": passed})
        gates.append(
            {
                "gate_id": gate_id, "command": list(command), "working_directory": working_directory, "host": host,
                "architecture": architecture, "started_at_utc": f"2026-10-08T02:{index:02d}:00Z",
                "completed_at_utc": f"2026-10-08T02:{index:02d}:30Z", "exit_code": 0 if passed else 1,
                "status": "passed" if passed else "failed", "passed_count": 1 if passed else 0, "failed_count": 0 if passed else 1,
                "skipped_count": 0, "stdout_sha256": "6" * 64, "stderr_sha256": "7" * 64,
                "evidence": [binding(proof, root, with_bytes=False)],
            }
        )
    attempt_id = f"gate-{stem}"
    if environment_mode == REAL_GATE_ENVIRONMENT:
        environment = {}
        for role, phase in (("before", "gate_before"), ("after", "gate_after")):
            receipt = create_arm_receipt(root, phase=phase, subject_id=attempt_id,
                                         prefix=f"environment/{attempt_id}-{role}")
            environment[role] = binding(receipt, root, with_bytes=False)
    else:
        environment = {
            role: binding(_private_json(root, root / "environment" / f"{attempt_id}-{role}.json", {"attempt_id": attempt_id, "role": role}), root, with_bytes=False)
            for role in ("before", "after")
        }
    facts = {
        "schema_version": gate_receipt.FACTS_SCHEMA, "phase": gate_receipt.CANDIDATE_PHASE,
        "attempt": {"attempt_id": attempt_id, "root_cause_id": f"candidate-gate-{failed_gate}" if failed_gate else None, "previous_receipt": None},
        "subject": {**dict(subject), "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None},
        "inputs": [], "gate_plan": None, "environment": environment, "gates": gates,
    }
    _private_json(root, root / f"{stem}-facts.json", facts)
    gate_receipt.finalize(root, f"{stem}-facts.json", name)
    return name


def expected_subject(manifest: Mapping[str, Any], candidate_payload: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    return accept.expected_gate_subject(manifest, candidate_payload, identity)
