"""测试基座：真实评估链驱动（改造 5 M1 审核修正，老板拍板 4.1）。

在**副本受管树**的子进程里按阶段驱动一条真实的 0.154 formal 评估链：

* ``init``：建 0.154 formal Campaign（只读导入形态：no-op 首批、真实 official seal 含 e2e 断言 bundle、
  合成 classify 五件套、VC-2／VC-3 合成动作、``revision-open --initial``、VC-4 合成动作、真实候选 seal
  含 e2e 断言 bundle）——plan 冻结的是**本副本树**的工具身份；
* ``dispatch``：以 ``compile-and-run-vc-batch``（staging 模型、正式监督器）派发 VC-5 批次，动作是真实 CLI
  ``compare``／真实 builder／真实 CLI ``accept``；可在父进程指定点注入崩溃（``os._exit``）；
* ``reconcile``／``recover``／``epoch``：既有控制命令；``recover apply`` 可在状态机 E1～E5 注入崩溃。

阶段之间只经磁盘（Campaign 目录 + ``chain-state.json``）传递状态；每次启动先断言三个关键受管模块的
``__file__`` 都在副本树内（禁止回落导入原仓库）。本文件位于 tests/，不进受管摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable
from unittest import mock

TREE_ROOT_ENV = "EVALUATION_CHAIN_TREE_ROOT"
CANDIDATE_ID = "candidate-r1"
RULES = ("SPEC-H1-001", "SPEC-EP-006")
TARGET_VERSION = "0.154.0"
CRASH_EXIT_CODE = 137
# C1 动作级崩溃注入：accept 动作仍是真实 CLI（同一 argv 交给 codex_upgrade.main），只是子进程内在
# 崩溃开关文件（argv[1]）存在时把 VC-5 completion 的写出点换成 SIGKILL 语义——AcceptanceFact 与证据
# 封印已落盘、completion 未写；父 run 因此按 action-failed 进入正式 stop-receipt／对账／重派链。
# 开关放在文件而不是命令里：环境恢复重派合同要求与原批次**逐字相同**的 actions，两次派发的命令因此
# 一致，只是续跑前删掉开关文件。
ACCEPT_WRAPPER_SCRIPT = (
    "import os, sys\n"
    "from unittest import mock\n"
    "from tools.official_client_capture import codex_upgrade\n"
    "def crash(*args, **kwargs):\n"
    f"    os._exit({CRASH_EXIT_CODE})\n"
    "if os.path.exists(sys.argv[1]):\n"
    "    with mock.patch.object(codex_upgrade, '_complete_vc_with_receipt', side_effect=crash):\n"
    "        raise SystemExit(codex_upgrade.main(sys.argv[2:]))\n"
    "raise SystemExit(codex_upgrade.main(sys.argv[2:]))\n"
)
ACCEPT_CRASH_FLAG = "accept-completion-crash.flag"


def _assert_tree_binding() -> Path:
    tree_root = Path(os.environ[TREE_ROOT_ENV]).resolve()
    from tools.official_client_capture import build_rule_assertion_results, candidate_rule_assertion, codex_upgrade

    for module in (codex_upgrade, build_rule_assertion_results, candidate_rule_assertion):
        path = Path(module.__file__).resolve()
        if tree_root not in path.parents:
            raise SystemExit(f"驱动回落到副本外模块：{module.__name__} -> {path}")
    return tree_root


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _crash_patch(target: Any, attribute: str):
    """让 ``target.attribute`` 被调用时立刻以 SIGKILL 语义退出（不执行原函数）。"""

    def crash(*_args: Any, **_kwargs: Any) -> None:
        os._exit(CRASH_EXIT_CODE)

    return mock.patch.object(target, attribute, side_effect=crash)


def _crash_after_patch(target: Any, attribute: str):
    """让 ``target.attribute`` 执行完原函数后立刻退出（原函数的落盘效果保留）。"""

    original = getattr(target, attribute)

    def crash_after(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        os._exit(CRASH_EXIT_CODE)

    return mock.patch.object(target, attribute, side_effect=crash_after)


# ---------------------------------------------------------------------------
# 证据：真实 e2e 断言 bundle 作为证据根本身
# ---------------------------------------------------------------------------


def _prepare_side_evidence(surface: str | None) -> Callable[[Path], None]:
    """证据根＝真实 bundle：来源在 ``<root>-source/run``，bundle 输出到证据根本身。"""

    from tools.official_client_capture import build_assertion_bundle as bundle_module
    from tools.official_client_capture import derive_official_observations as derive
    from tools.official_client_capture.tests import test_acceptance_end_to_end as e2e

    def prepare(evidence_root: Path) -> None:
        candidate_side = surface is not None
        side_dir = evidence_root.parent / f"{evidence_root.name}-source"
        source_root = side_dir / "run"
        (source_root / "relay").mkdir(parents=True)
        (source_root / "relay" / "conn001.client_to_upstream.bin").write_bytes(e2e.H1_STREAM)
        # provenance 的 source_root 名称必须等于该 Job 声明的证据根目录名（failure-scope 定位链按名称
        # 映射到唯一 Job）：bundle plan 的 root 名取证据根目录名。
        root_name = evidence_root.name
        entries = [{"root": root_name, "path": "relay/conn001.client_to_upstream.bin", "target": "run/relay/conn001.client_to_upstream.bin"}]
        if candidate_side:
            (source_root / "traces").mkdir()
            record = dict(e2e.INTERNAL_RECORD, data={"surface": surface})
            (source_root / "traces" / "surface.observation.jsonl").write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            entries.append({"root": root_name, "path": "traces/surface.observation.jsonl", "target": "run/traces/surface.observation.jsonl"})
        plan_path = side_dir / "bundle-plan.json"
        plan_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        bundle_module.build_bundle({root_name: source_root}, bundle_module.load_plan(plan_path), evidence_root)
        derive_plan = side_dir / "derive-plan.json"
        derive_plan.write_text(
            json.dumps({"entries": [{"source": "run/relay/conn001.client_to_upstream.bin", "parser": "h1_request_stream", "scenario_id": "A03", "kind": "process_trace", "target": "derived/A03/conn001.observation.jsonl", "connection_id": "conn001"}]}),
            encoding="utf-8",
        )
        derive.derive_observations(evidence_root, derive.load_derivation_plan(derive_plan))

    return prepare


def _extra_artifacts(candidate_side: bool) -> Callable[[Path], list[dict[str, Any]]]:
    def build(evidence_root: Path) -> list[dict[str, Any]]:
        def artifact(path: str, kind: str, parser: str) -> dict[str, Any]:
            return {"path": path, "sha256": _sha(evidence_root / path), "kind": kind, "parser": parser, "scenario_ids": ["A03"], "labels": {"transport": "http"}}

        items = [
            artifact("run/relay/conn001.client_to_upstream.bin", "relay_binary", "opaque_bound_source"),
            artifact("derived/A03/conn001.observation.jsonl", "process_trace", "observation_jsonl"),
        ]
        if candidate_side:
            items.append(artifact("run/traces/surface.observation.jsonl", "process_trace", "observation_jsonl"))
        return items

    return build


# ---------------------------------------------------------------------------
# init：建 Campaign 到 VC-4 完成、候选已 seal
# ---------------------------------------------------------------------------


def stage_init(arguments: argparse.Namespace) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture.tests import test_acceptance_end_to_end as e2e
    from tools.official_client_capture.tests import test_codex_upgrade

    class RealChainCase(test_codex_upgrade.CodexUpgradeTest):
        # 正式 0.154 证据标签声明覆盖的 Job id：受管子进程（CLI）的声明校验无需 mock 即可通过。
        synthetic_job_ids = {"official": "official-core", "candidate": "candidate-frozen-core"}

    case = RealChainCase("test_bound_evidence_path_accepts_legacy_attempt_relative_binding")
    case.setUp()
    root = Path(arguments.root).resolve()
    original_create = codex_upgrade._create_initial_vc_control_artifacts

    def as_reuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["reuse_official_jobs"] = True
        return original_create(*args, **kwargs)

    with mock.patch.object(codex_upgrade, "_create_initial_vc_control_artifacts", side_effect=as_reuse):
        fixture = case._b0_fixture(root, campaign_id=arguments.campaign_id)
    campaign_dir = Path(str(fixture["campaign_dir"]))
    manifest_path = campaign_dir / "campaign.json"
    manifest = _read(manifest_path)
    manifest["predecessor"] = {
        "campaign_dir": str(root / "predecessor-fixture"),
        "campaign_id": f"{arguments.campaign_id}-predecessor",
        "campaign_manifest_sha256": "0" * 64,
        "reason": codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (campaign_dir / "campaign.sha256").write_text(codex_upgrade.file_sha256(manifest_path) + "\n", encoding="utf-8")
    state_dir = root / "supervisor"
    state_dir.mkdir(mode=0o700)
    (campaign_dir / "control" / "vc-chain").mkdir(mode=0o700)

    # official seal（真实 bundle 作证据根；VC-1 随之封存）。
    case._write_capture_stage(
        campaign_dir,
        campaign_dir / "official-evidence",
        phase="official",
        identity=manifest["official_identity"],
        prepare_evidence=_prepare_side_evidence(None),
        extra_artifacts=_extra_artifacts(False),
    )
    # 最小完整候选身份（老板二次拍板 A 受限版）：docker／go 可用时真实执行 plan-candidate-gates 与
    # record-candidate-build（VC-4 构建收据 + VC-4 checkpoint），accept 可走到 VC-5 completion；不可用时
    # 退回 VC-4 合成动作，accept 段由用例按 candidate_identity_fixture.available() 跳过。
    from tools.official_client_capture.tests import candidate_identity_fixture as cif

    identity_fixture: cif.CandidateIdentityFixture | None = None
    if cif.available() and not arguments.no_candidate_identity:
        identity_fixture = cif.CandidateIdentityFixture(
            root / "candidate-identity", candidate_id=CANDIDATE_ID, target_version=TARGET_VERSION, baseline_version=str(manifest["baseline_version"])
        )
        identity_fixture.create_source_tree()

    # 合成 classify 阶段结果：五件套真实文件 + 联合摘要（画像＝e2e 简化画像，绑定真实 source_spec 摘要）；
    # 0.154 起还绑定画像派生收据与 post-promotion 门禁需求（候选身份夹具在场时）。
    profile_payload = json.loads(json.dumps(e2e.PROFILE))
    profile_payload["codex_version"] = TARGET_VERSION
    spec_path = Path(codex_upgrade.__file__).resolve().parents[2] / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
    if not spec_path.is_file():
        spec_path = Path(arguments.spec_path).resolve()
    profile_payload["source_spec_sha256"] = codex_upgrade.source_spec_section_sha256(spec_path, "第二章")
    runtime_profile_payload = {"transport": "codex-official-egress", "rule_count": len(RULES), "Version": TARGET_VERSION, "Digest": "c" * 64}
    target, migration, scenario, profile, assertion_profile, _rules = case._write_classification_manifests(
        root, rules=RULES, assertion_profile_payload=profile_payload, version=TARGET_VERSION, profile_payload=runtime_profile_payload
    )
    approved_root = campaign_dir / "classification" / "approved"
    approved_root.mkdir(parents=True, mode=0o700)
    references: dict[str, dict[str, str]] = {}
    for key, source, name in (
        ("target_rule_manifest", target, "target-rules.json"),
        ("migration_manifest", migration, "migration.json"),
        ("scenario_manifest", scenario, "scenarios.json"),
        ("profile_manifest", profile, "profile.json"),
        ("assertion_profile_manifest", assertion_profile, "assertion-profile.json"),
    ):
        destination = approved_root / name
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o600)
        references[key] = {"path": destination.relative_to(campaign_dir).as_posix(), "sha256": _sha(destination)}
    joint = codex_upgrade._fingerprint({key: value["sha256"] for key, value in references.items()})
    classify_payload: dict[str, Any] = {
        "status": "complete",
        **references,
        "joint_manifest_sha256": joint,
        "baseline_rule_count": len(manifest["required_rules"]),
        "target_rule_count": len(RULES),
        # 与真实 classify 的 migration 摘要同形（accept 的 classification_unblocked 门要求 unclassified_count＝0）。
        "migration": {"blocked": False, "entry_count": 0, "discovery_count": 0, "unclassified_count": 0},
        "source_diff_sha256": "0" * 64,
        "official_diff_sha256": "0" * 64,
    }
    requirements: dict[str, Any] | None = None
    if identity_fixture is not None:
        extras = cif.classification_extras(
            campaign_dir, approved_root, manifest=manifest, migration_path=approved_root / "migration.json",
            profile_manifest_path=approved_root / "profile.json", joint_manifest_sha256=joint, migration_reference=references["migration_manifest"],
        )
        requirements = extras.pop("requirements")
        classify_payload.update(extras)
    codex_upgrade.save_stage_result(campaign_dir, "classify", classify_payload)
    # VC-2 合成动作；VC-3 合成动作的阶段收据＝候选树内 Catalog stage 收据字节（record-candidate-build 的
    # revision-seal 要求二者逐字节一致），无候选身份夹具时沿用合成收据。
    vc3_receipt_source = identity_fixture.catalog_receipt_path if identity_fixture is not None else None
    for sequence, phase in ((2, "VC-2"), (3, "VC-3")):
        plan = case._vc_chain_action_plan(root, campaign_dir, phase, stage_receipt_source=(vc3_receipt_source if phase == "VC-3" else None))
        result, returncode = codex_upgrade.compile_and_run_vc_batch(case._vc_chain_arguments({**fixture, "state_dir": state_dir}, phase, sequence, plan))
        if returncode != 0:
            raise SystemExit(f"{phase} 合成批次失败：{json.dumps(result, ensure_ascii=False, default=str)[:2000]}")
    codex_upgrade.open_candidate_revision(argparse.Namespace(campaign_dir=campaign_dir, candidate_id=CANDIDATE_ID, initial=True, supersedes=None))
    identity: dict[str, Any]
    if identity_fixture is not None:
        assert requirements is not None
        mapping_path = identity_fixture.write_gate_mapping(requirements)
        identity_fixture.plan_gates(campaign_dir, mapping_path)
        identity_fixture.commit_candidate()
        identity_fixture.build_binary()
        identity_fixture.assemble_trees()
        identity_fixture.build_image()
        identity_fixture.write_builder_receipt()
        parameters_path = identity_fixture.build_parameters()
        identity_fixture.write_source_transition()
        timing = manifest["control_receipts"]["upgrade_timing"]
        implementation_root, implementation_receipt = identity_fixture.write_implementation_receipt(
            upgrade_id=str(timing["upgrade_id"]), campaign_id=str(manifest["campaign_id"]), campaign_purpose=str(manifest["campaign_purpose"]),
            source_tree_sha256=codex_upgrade._directory_tree_digest(identity_fixture.source),
        )
        recorded = identity_fixture.record_build(
            campaign_dir, manifest, build_parameters=parameters_path, implementation_root=implementation_root, implementation_receipt=implementation_receipt
        )
        receipt_path = Path(str(recorded["build_receipt"]))
        receipt = _read(receipt_path)
        identity = {
            "git_commit": receipt["source"]["git_commit"],
            "source_root": receipt["source"]["root"],
            "source_tree_sha256": receipt["source"]["tree_sha256"],
            "image_reference": receipt["image"]["reference"],
            "image_digest": receipt["image"]["manifest_digest"],
            "image_id": receipt["image"]["image_id"],
            "build_id": receipt["build"]["build_id"],
            "deployed_version": receipt["deployed_version"],
            "profile_id": receipt["profile"]["profile_id"],
            "profile_digest": receipt["profile"]["profile_digest"],
            "candidate_purpose": receipt["candidate_purpose"],
            "target_architecture": receipt["target_architecture"],
            "binary": dict(receipt["binary"]),
            "build_parameters_sha256": receipt["build"]["parameters_sha256"],
            "catalog_stage": dict(receipt["catalog_stage"]),
            "source_transition": dict(receipt["source_transition"]),
            "gate_requirements": dict(receipt["gate_requirements"]),
            "gate_plan": dict(receipt["gate_plan"]),
            "build_receipt": {"path": receipt_path.relative_to(campaign_dir).as_posix(), "sha256": _sha(receipt_path), "bytes": receipt_path.stat().st_size},
            "build_receipt_digest": receipt["receipt_digest"],
        }
    else:
        plan = case._vc_chain_action_plan(root, campaign_dir, "VC-4")
        result, returncode = codex_upgrade.compile_and_run_vc_batch(case._vc_chain_arguments({**fixture, "state_dir": state_dir}, "VC-4", 4, plan))
        if returncode != 0:
            raise SystemExit(f"VC-4 合成批次失败：{json.dumps(result, ensure_ascii=False, default=str)[:2000]}")
        identity = {
            "git_commit": "f" * 40,
            "source_tree_sha256": "d" * 64,
            "image_reference": f"sub2apiplus@sha256:{'9' * 64}",
            "image_digest": f"sha256:{'9' * 64}",
            "image_id": f"sha256:{'e' * 64}",
            "build_id": "build-0154-test",
            "deployed_version": TARGET_VERSION,
            "profile_id": f"codex-{TARGET_VERSION}-test-v1",
            "profile_digest": "c" * 64,
            "candidate_purpose": manifest["campaign_purpose"],
        }
    # 候选 seal（真实 bundle 作证据根；候选证据 surface 由参数决定，全程不再更换）。
    case._write_capture_stage(
        campaign_dir,
        campaign_dir / "candidate-evidence",
        phase="candidate",
        identity=identity,
        candidate_id=CANDIDATE_ID,
        prepare_evidence=_prepare_side_evidence(arguments.candidate_surface),
        extra_artifacts=_extra_artifacts(True),
    )
    # 候选 attempt 的 Job checkpoint 链：post-run-tooling 判定要求每个 Job 有 complete 记录
    # （合成 seal 夹具只写 attempt.json／reservation.json）。
    candidate_stage = codex_upgrade._load_stage_result(campaign_dir, "capture-candidate", CANDIDATE_ID)
    attempt_root = campaign_dir / str(candidate_stage["attempt"]["path"]).rsplit("/", 1)[0]
    attempt_payload = _read(attempt_root / "attempt.json")
    reservation = _read(attempt_root / "reservation.json")
    store = codex_upgrade.incremental_recovery.CheckpointStore(attempt_root / "checkpoints")
    previous: str | None = None
    for result_item in attempt_payload["results"]:
        appended = store.append(
            {
                "checkpoint_schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                "campaign_id": manifest["campaign_id"],
                "phase": "candidate",
                "attempt_id": attempt_root.name,
                "run_nonce": reservation["run_nonce"],
                "item_id": result_item["id"],
                "status": "complete",
                "disposition": "executed",
                "result_sha256": codex_upgrade.incremental_recovery.digest(result_item),
                "result_key": None,
                "result": result_item,
                "source_receipt": None,
                "previous_checkpoint_sha256": previous,
            }
        )
        previous = str(appended["checkpoint_sha256"])
    state = {
        "root": str(root),
        "campaign_dir": str(campaign_dir),
        "state_dir": str(state_dir),
        "data": str(fixture["data"]),
        "control": str(fixture["control"]),
        "ledger": str(fixture["ledger"]),
        "timing_ledger": str(fixture["timing_ledger"]),
        "deployment": str(fixture["deployment"]),
        "candidate_id": CANDIDATE_ID,
        "identity": identity,
        "gate_root": None,
        "gate_receipt": None,
        "job_ids": [str(item["id"]) for item in attempt_payload["results"]],
        "attempt_id": attempt_root.name,
        "rules": list(RULES),
        "candidate_identity": identity_fixture.identity_state() if identity_fixture is not None else None,
    }
    _write(root / "chain-state.json", state)
    case.doCleanups()
    return {"status": "initialized", **state}


# ---------------------------------------------------------------------------
# dispatch：VC-5 批次（真实 CLI compare／真实 builder／真实 CLI accept）
# ---------------------------------------------------------------------------


def _load_state(arguments: argparse.Namespace) -> dict[str, Any]:
    return _read(Path(arguments.root).resolve() / "chain-state.json")


def _builder_config(state: dict[str, Any], *, baseline: int, tag: str) -> Path:
    from tools.official_client_capture import codex_upgrade

    campaign_dir = Path(state["campaign_dir"])
    candidate_id = str(state["candidate_id"])
    official = codex_upgrade._load_stage_result(campaign_dir, "capture-official")
    candidate = codex_upgrade._load_stage_result(campaign_dir, "capture-candidate", candidate_id)
    classification = codex_upgrade._load_stage_result(campaign_dir, "classify")
    compare_source = codex_upgrade._stage_read_source(campaign_dir, candidate_id, baseline, "compare")
    comparison_digest = None
    if compare_source["status"] == "complete":
        comparison_digest = _read(Path(compare_source["path"]))["package_digest"]
    config = {
        "campaign_dir": str(campaign_dir),
        "assertion_profile": str((campaign_dir / classification["assertion_profile_manifest"]["path"]).resolve()),
        "rule_manifest": str((campaign_dir / classification["target_rule_manifest"]["path"]).resolve()),
        "expected_profile_sha256": classification["assertion_profile_manifest"]["sha256"],
        "official_evidence_root": official["assertion_context"]["evidence_root"],
        "candidate_evidence_root": candidate["assertion_context"]["evidence_root"],
        "official_capture_manifest": official["assertion_context"]["capture_manifest_path"],
        "candidate_capture_manifest": candidate["assertion_context"]["capture_manifest_path"],
        "official_evidence_prefix": official["assertion_context"]["evidence_prefix"],
        "candidate_evidence_prefix": candidate["assertion_context"]["evidence_prefix"],
        "target_version": TARGET_VERSION,
        "candidate_id": candidate_id,
        "profile_id": state["identity"]["profile_id"],
        "profile_digest": state["identity"]["profile_digest"],
        "official_package_digest": official["package_digest"],
        "candidate_package_digest": candidate["package_digest"],
        "comparison_package_digest": comparison_digest,
        "official_authority": {
            "assertion_profile_sha256": classification["assertion_profile_manifest"]["sha256"],
            "classification_package_digest": classification["package_digest"],
            "review_sha256": classification["joint_manifest_sha256"],
        },
        "rules": list(state["rules"]),
    }
    path = Path(state["root"]) / f"builder-config-{tag}.json"
    _write(path, config)
    return path


def _actions(state: dict[str, Any], arguments: argparse.Namespace) -> list[dict[str, Any]]:
    from tools.official_client_capture import codex_upgrade

    campaign_dir = Path(state["campaign_dir"])
    candidate_id = str(state["candidate_id"])
    cli = str(Path(codex_upgrade.__file__).resolve())
    builder = str(Path(codex_upgrade.__file__).resolve().parent / "build_rule_assertion_results.py")
    assertions_root = codex_upgrade._stage_write_target(campaign_dir, candidate_id, int(arguments.baseline), "assertions")
    # item id 是零请求后处理阶段项的正式命名（post-run-tooling 判定按 item 识别）；action_id 带序号表达
    # 批次内执行次序（清单按 action_id 排序）。
    item_ids = {"compare": "compare", "assert": "assert-rules", "accept": "acceptance"}
    actions: list[dict[str, Any]] = []
    for index, kind in enumerate(arguments.actions, start=1):
        action_id = f"vc5-{index}-{kind}"
        if kind == "compare":
            command = [sys.executable, cli, "compare", "--campaign-dir", str(campaign_dir), "--candidate-id", candidate_id]
        elif kind == "assert":
            command = [
                sys.executable, builder,
                "--config", str(_builder_config(state, baseline=int(arguments.baseline), tag=arguments.tag)),
                "--output", str(assertions_root / "results.json"),
                "--results-dir", str(assertions_root / "machine"),
                "--evaluation-baseline", str(arguments.baseline),
                "--reuse-authority", arguments.authority,
            ]
            if arguments.reuse_from:
                command.extend(["--reuse-from", str(Path(arguments.reuse_from).resolve())])
        elif kind == "accept":
            if not state.get("gate_root"):
                raise SystemExit("accept 动作前必须先在本副本树上执行 gate 阶段生成外部门禁收据。")
            cli_arguments = [
                "accept", "--campaign-dir", str(campaign_dir), "--candidate-id", candidate_id,
                "--external-gate-root", state["gate_root"], "--external-gate-receipt", state["gate_receipt"],
            ]
            if getattr(arguments, "accept_wrapper", False):
                flag = Path(state["root"]) / ACCEPT_CRASH_FLAG
                if getattr(arguments, "crash_at", "") == "accept-completion":
                    flag.write_text("crash\n", encoding="utf-8")
                elif flag.exists():
                    flag.unlink()
                command = [sys.executable, "-c", ACCEPT_WRAPPER_SCRIPT, str(flag), *cli_arguments]
            elif getattr(arguments, "crash_at", "") == "accept-completion":
                raise SystemExit("accept-completion 崩溃点需要 --accept-wrapper（两次派发命令逐字相同）。")
            else:
                command = [sys.executable, cli, *cli_arguments]
        else:
            raise SystemExit(f"未知动作：{kind}")
        actions.append({"action_id": action_id, "operation": f"VC-5:{kind}", "timeout_seconds": 900, "command": command, "item_ids": [item_ids[kind]]})
    return actions


def stage_dispatch(arguments: argparse.Namespace) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_supervisor as supervisor
    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

    state = _load_state(arguments)
    campaign_dir = Path(state["campaign_dir"])
    actions = _actions(state, arguments)
    plan_doc = {
        "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
        "execute_item_ids": sorted(item for action in actions for item in action["item_ids"]),
        "reuse_item_ids": sorted(set(state["job_ids"]) | set(arguments.reuse_items or [])),
        "actions": actions,
    }
    plan_path = Path(state["root"]) / f"plans-{arguments.tag}" / "vc-5.json"
    _write(plan_path, plan_doc)
    predecessor = codex_upgrade._vc_checkpoint_path(campaign_dir, "VC-4", revision=1)
    # 全局批次序号由既有 COMMIT 序号推算（真实 record-candidate-build 不占批次序号，有无候选身份夹具时
    # VC-5 首批序号不同）；显式 --sequence 只作核对。
    manifest = codex_upgrade._require_formal_campaign(campaign_dir)
    sequence = max(codex_upgrade._committed_vc_sequences(campaign_dir, manifest), default=0) + 1
    if arguments.sequence is not None and int(arguments.sequence) != sequence:
        raise SystemExit(f"dispatch 序号核对失败：请求 {arguments.sequence}，既有 COMMIT 推算为 {sequence}。")
    namespace = argparse.Namespace(
        campaign_dir=campaign_dir,
        state_dir=Path(state["state_dir"]),
        phase="VC-5",
        sequence=sequence,
        predecessor_checkpoint=predecessor,
        action_plan=plan_path.resolve(),
        heartbeat_seconds=0.2,
        watchdog_timeout_seconds=float(arguments.watchdog_timeout_seconds),
        ledger_interval_seconds=0.2,
    )
    patches = []
    if arguments.crash_at == "post-run-tooling":
        # R2：诊断与动作输出绑定已写、post-run-tooling 收据未写时父进程被 SIGKILL。
        patches.append(_crash_patch(supervisor, "_write_post_run_tooling_receipt"))
    elif arguments.crash_at == "before-binding":
        # R2-b：只有诊断落盘、动作输出绑定未写。
        patches.append(_crash_patch(supervisor, "write_action_output_binding"))
    elif arguments.crash_at == "accept-completion":
        pass  # C1：崩溃点在 accept 动作子进程内（见 _actions），父监督器不打补丁。
    elif arguments.crash_at:
        raise SystemExit(f"dispatch 不支持崩溃点：{arguments.crash_at}")
    for patcher in patches:
        patcher.start()
    result, returncode = codex_upgrade.compile_and_run_vc_batch(namespace)
    summary = {
        "returncode": returncode,
        "status": result.get("status"),
        "batch_sequence": result.get("batch_sequence"),
        "commit_sha256": (result.get("commit") or {}).get("commit_sha256"),
        "campaign_run": {
            "run_dir": result["campaign_run"]["run_dir"],
            "reason": result["campaign_run"]["reason"],
            "status": result["campaign_run"]["status"],
            "actions": [
                {
                    "action_id": action["action_id"],
                    "status": action["status"],
                    "effective_failure_class": (action.get("diagnostic") or {}).get("effective_failure_class"),
                    "post_run_tooling_rejected": (action.get("diagnostic") or {}).get("post_run_tooling_rejected"),
                }
                for action in result["campaign_run"]["actions"]
            ],
            "timing_closeout": result["campaign_run"].get("timing_closeout"),
        },
    }
    return summary


# ---------------------------------------------------------------------------
# reconcile / recover / epoch
# ---------------------------------------------------------------------------


def stage_reconcile(arguments: argparse.Namespace) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    state = _load_state(arguments)
    return reconciler.reconcile_supervisor_run(Path(arguments.run_dir), Path(state["campaign_dir"]))


def stage_recover(arguments: argparse.Namespace) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

    state = _load_state(arguments)
    namespace = argparse.Namespace(
        campaign_dir=Path(state["campaign_dir"]),
        candidate_id=str(state["candidate_id"]),
        recover_action=arguments.recover_action,
        reviewer="boss",
        root_cause_class=arguments.root_cause_class,
        fix_commit=arguments.fix_commit,
        deployment_receipt=(Path(arguments.deployment_receipt) if arguments.deployment_receipt else None),
        approve_sha256=arguments.approve_sha256,
        reason=arguments.reason,
    )
    crash_points = {
        "e1": (reconciler, "_binding"),                      # PREPARED 已写，outbox 未写（② 首个调用）
        "e2": (reconciler, "_push_and_replay"),              # outbox 已提交，未推送
        "e3": (artifacts, "build_evaluation_baseline_authorization"),  # 判定过，AUTHORIZATION 未写
        "e4": (artifacts, "build_evaluation_baseline_commit"),          # AUTHORIZATION 已写，COMMIT 未写
        "e5": (timing_ledger, "append_event"),               # COMMIT 已写，账本未写
    }
    if arguments.crash_at:
        if arguments.crash_at not in crash_points:
            raise SystemExit(f"recover 不支持崩溃点：{arguments.crash_at}")
        target, attribute = crash_points[arguments.crash_at]
        _crash_patch(target, attribute).start()
    try:
        return codex_upgrade.evaluation_recover(namespace)
    except codex_upgrade.ConfigurationError as error:
        return {"status": "error", "error": str(error)}


def stage_epoch(arguments: argparse.Namespace) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade

    state = _load_state(arguments)
    return codex_upgrade._evaluation_epoch_command(
        argparse.Namespace(
            campaign_dir=Path(state["campaign_dir"]),
            candidate_id=str(state["candidate_id"]),
            attempt_id=arguments.attempt_id,
            reason=arguments.reason,
        )
    )


def stage_accept_direct(arguments: argparse.Namespace) -> dict[str, Any]:
    """在本副本树进程内直接调用真实 ``accept_campaign``（不经父监督器），供 C1 崩溃注入：
    ``--crash-at completion`` 让 accept 结果已封存、VC-5 completion 写出前 SIGKILL。"""

    from tools.official_client_capture import codex_upgrade

    state = _load_state(arguments)
    if not state.get("gate_root"):
        raise SystemExit("accept-direct 前必须先在本副本树上执行 gate 阶段生成外部门禁收据。")
    campaign_dir = Path(state["campaign_dir"])
    candidate_id = str(state["candidate_id"])
    if arguments.crash_at == "completion":
        _crash_patch(codex_upgrade, "_complete_vc_with_receipt").start()
    elif arguments.crash_at:
        raise SystemExit(f"accept-direct 不支持崩溃点：{arguments.crash_at}")
    try:
        result = codex_upgrade.accept_campaign(
            campaign_dir, candidate_id, codex_upgrade._default_assertions_path(campaign_dir, candidate_id),
            Path(state["gate_root"]), Path(state["gate_receipt"]),
        )
    except codex_upgrade.ConfigurationError as error:
        return {"status": "error", "error": str(error)}
    return {
        "status": "accepted" if result.get("accepted") else "rejected",
        "accepted": result.get("accepted"),
        "vc5_checkpoint_sha256": result.get("vc5_checkpoint_sha256"),
        "vc5_completion_receipt_digest": result.get("vc5_completion_receipt_digest"),
        "failed_gates": result.get("failed_gates"),
    }


def stage_gate(arguments: argparse.Namespace) -> dict[str, Any]:
    """在本副本树上生成候选外部门禁收据（收据 producer 绑定 finalizer 的绝对路径与摘要，accept 必须在同一树重放）。"""

    from tools.official_client_capture.tests import test_codex_upgrade

    state = _load_state(arguments)
    case = test_codex_upgrade.CodexUpgradeTest("test_bound_evidence_path_accepts_legacy_attempt_relative_binding")
    case.setUp()
    gate_parent = Path(state["root"]) / f"gate-{arguments.tag}"
    gate_parent.mkdir(mode=0o700)
    gate_root, gate_receipt = case._candidate_gate_receipt(gate_parent, Path(state["campaign_dir"]), str(state["candidate_id"]))
    case.doCleanups()
    state["gate_root"] = str(gate_root)
    state["gate_receipt"] = str(gate_receipt)
    _write(Path(state["root"]) / "chain-state.json", state)
    return {"status": "gate_ready", "gate_root": str(gate_root), "gate_receipt": str(gate_receipt)}


def stage_identity(arguments: argparse.Namespace) -> dict[str, Any]:
    """输出本副本树的受管身份与 evaluator 四项摘要（父进程据此生成部署收据 fixture）。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_tool_identity_policy as policy

    identity = codex_upgrade._tool_identity(include_git=False)
    return {
        "files_sha256": identity["files_sha256"],
        "policy_version": identity["policy_version"],
        "policy_sha256": identity["policy_sha256"],
        "wire_producer_sha256": identity["wire_producer_sha256"],
        "evidence_semantics_sha256": identity["evidence_semantics_sha256"],
        "control_sha256": identity["control_sha256"],
        "evaluator_digests": dict(policy.evaluator_dependency_digests()),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="真实评估链驱动（副本受管树子进程内运行）")
    parser.add_argument("--root", required=True)
    subparsers = parser.add_subparsers(dest="stage", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--campaign-id", default="upgrade-0154-eval-chain")
    init.add_argument("--candidate-surface", default="codex")
    init.add_argument("--spec-path", default="")
    init.add_argument("--no-candidate-identity", action="store_true")
    dispatch = subparsers.add_parser("dispatch")
    dispatch.add_argument("--sequence", type=int, default=None)
    dispatch.add_argument("--accept-wrapper", action="store_true")
    dispatch.add_argument("--tag", required=True)
    dispatch.add_argument("--actions", nargs="+", required=True, choices=("compare", "assert", "accept"))
    dispatch.add_argument("--baseline", type=int, default=0)
    dispatch.add_argument("--reuse-from", default="")
    dispatch.add_argument("--authority", default="none")
    dispatch.add_argument("--reuse-items", nargs="*", default=[])
    dispatch.add_argument("--crash-at", default="")
    dispatch.add_argument("--watchdog-timeout-seconds", type=float, default=5.0)
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--run-dir", required=True)
    recover = subparsers.add_parser("recover")
    recover.add_argument("recover_action", choices=("preview", "apply", "abandon"))
    recover.add_argument("--root-cause-class", default=None)
    recover.add_argument("--fix-commit", default=None)
    recover.add_argument("--deployment-receipt", default=None)
    recover.add_argument("--approve-sha256", default=None)
    recover.add_argument("--reason", default=None)
    recover.add_argument("--crash-at", default="")
    epoch = subparsers.add_parser("epoch")
    epoch.add_argument("--attempt-id", default=None)
    epoch.add_argument("--reason", required=True)
    gate = subparsers.add_parser("gate")
    gate.add_argument("--tag", required=True)
    accept_direct = subparsers.add_parser("accept-direct")
    accept_direct.add_argument("--crash-at", default="")
    subparsers.add_parser("identity")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    _assert_tree_binding()
    arguments = _parser().parse_args(argv)
    stages = {
        "init": stage_init,
        "dispatch": stage_dispatch,
        "reconcile": stage_reconcile,
        "recover": stage_recover,
        "epoch": stage_epoch,
        "gate": stage_gate,
        "accept-direct": stage_accept_direct,
        "identity": stage_identity,
    }
    result = stages[arguments.stage](arguments)
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
