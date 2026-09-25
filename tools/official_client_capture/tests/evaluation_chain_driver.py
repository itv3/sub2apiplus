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
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
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
# M2 两 Job 链：accept／status 等读侧真实 CLI 读增量封存结果时要重放恢复段的 v3 权限收口（测试机上 runs 别名
# 须注入），与段 run／seal 同一套环境替身；崩溃开关仍走文件（命令逐字稳定）。
# argv：[1]=受管 CLI 路径（只作编译侧评估动作识别的坐标，脚本不用）、[2]=链状态、[3]=崩溃开关文件、[4:]=CLI 参数。
AR_READ_SIDE_WRAPPER = (
    "import os, sys\n"
    "from unittest import mock\n"
    "from tools.official_client_capture.tests import evaluation_chain_driver as driver\n"
    "with driver.attempt_recovery_environment_patches(sys.argv[2]):\n"
    "    from tools.official_client_capture import codex_upgrade\n"
    "    def crash(*args, **kwargs):\n"
    f"        os._exit({CRASH_EXIT_CODE})\n"
    "    if os.path.exists(sys.argv[3]):\n"
    "        with mock.patch.object(codex_upgrade, '_complete_vc_with_receipt', side_effect=crash):\n"
    "            raise SystemExit(codex_upgrade.main(sys.argv[4:]))\n"
    "    raise SystemExit(codex_upgrade.main(sys.argv[4:]))\n"
)


def _read_side_command(state: dict[str, Any], *cli_arguments: str) -> list[str]:
    """两 Job 链读侧真实 CLI 的包装命令：第 4 个 token 是受管 CLI 路径，编译侧据此识别 compare／accept
    评估动作（冻结 output_bindings、失败父 run 归评估动作失败），与生产命令 ``python codex_upgrade.py …`` 同坐标。"""

    from tools.official_client_capture import codex_upgrade

    return [
        sys.executable, "-c", AR_READ_SIDE_WRAPPER, str(Path(codex_upgrade.__file__).resolve()),
        str(Path(state["root"]) / "chain-state.json"), str(Path(state["root"]) / ACCEPT_CRASH_FLAG), *cli_arguments,
    ]


def _assert_tree_binding() -> Path:
    tree_root = Path(os.environ[TREE_ROOT_ENV]).resolve()
    from tools.official_client_capture import build_rule_assertion_results, candidate_rule_assertion, codex_upgrade

    for module in (codex_upgrade, build_rule_assertion_results, candidate_rule_assertion):
        path = Path(module.__file__).resolve()
        if tree_root not in path.parents:
            raise SystemExit(f"驱动回落到副本外模块：{module.__name__} -> {path}")
    return tree_root


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


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


def _prepare_side_evidence(surface: str | None, *, client_version: str | None = None) -> Callable[[Path], None]:
    """证据根＝真实 bundle：来源在 ``<root>-source/run``，bundle 输出到证据根本身。"""

    from tools.official_client_capture import build_assertion_bundle as bundle_module
    from tools.official_client_capture import derive_official_observations as derive
    from tools.official_client_capture.tests import test_acceptance_end_to_end as e2e

    def prepare(evidence_root: Path) -> None:
        candidate_side = surface is not None
        side_dir = evidence_root.parent / f"{evidence_root.name}-source"
        source_root = side_dir / "run"
        (source_root / "relay").mkdir(parents=True)
        stream = e2e.H1_STREAM
        if client_version is not None:
            stream = stream.replace(b"Host: chatgpt.com\r\n", f"Host: chatgpt.com\r\nUser-Agent: codex_cli_rs/{client_version}\r\n".encode())
        (source_root / "relay" / "conn001.client_to_upstream.bin").write_bytes(stream)
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


def new_real_chain_case(*, full_chain: bool = False) -> Any:
    from tools.official_client_capture.tests import test_codex_upgrade

    class RealChainCase(test_codex_upgrade.CodexUpgradeTest):
        # 正式 0.154 证据标签声明覆盖的 Job id：受管子进程（CLI）的声明校验无需 mock 即可通过。
        synthetic_job_ids = {"official": "official-core", "candidate": "candidate-frozen-core"}

        def _write_scenario_manifest(self, root, rule_manifest, rules, **kwargs):
            # 连续链在建 Campaign 前即冻结两条合成规则，不能事后改写 Campaign
            # 或伪造 classify 结果来绕过基线规则与场景执行合同的对应校验。
            if full_chain:
                version = kwargs.get("version", "0.145.0")
                rule_manifest = root / f"full-chain-rules-{version}.json"
                self._write_json(rule_manifest, {
                    "schema_version": "codex-egress-rule-manifest/v1",
                    "codex_version": version,
                    "required_rules": list(RULES),
                })
                rules = RULES
            return super()._write_scenario_manifest(root, rule_manifest, rules, **kwargs)

        def _campaign_arguments(self, root, **kwargs):
            result = super()._campaign_arguments(root, **kwargs)
            if full_chain:
                result.rule_manifest = root / f"full-chain-rules-{result.baseline_version}.json"
            return result

    case = RealChainCase("test_bound_evidence_path_accepts_legacy_attempt_relative_binding")
    case.setUp()
    return case


def init_campaign_to_vc4(arguments: argparse.Namespace, case: Any) -> dict[str, Any]:
    """Campaign 建到 VC-4 完成（official seal、classify、VC-2／VC-3、候选身份与 VC-4 构建收据），
    返回后续候选 seal 所需的上下文；M1 单 Job 链与 M2 attempt-recovery 链共用。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture.tests import test_acceptance_end_to_end as e2e

    root = Path(arguments.root).resolve()
    original_create = codex_upgrade._create_initial_vc_control_artifacts

    def as_reuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["reuse_official_jobs"] = True
        return original_create(*args, **kwargs)

    full_chain = getattr(arguments, "full_chain", False)
    if full_chain:
        fixture = case._b0_fixture(root, campaign_id=f"{arguments.campaign_id}-source")
    else:
        with mock.patch.object(codex_upgrade, "_create_initial_vc_control_artifacts", side_effect=as_reuse):
            fixture = case._b0_fixture(root, campaign_id=arguments.campaign_id)
    campaign_dir = Path(str(fixture["campaign_dir"]))
    manifest_path = campaign_dir / "campaign.json"
    manifest = _read(manifest_path)
    if not full_chain:
        manifest["predecessor"] = {
            "campaign_dir": str(root / "predecessor-fixture"),
            "campaign_id": f"{arguments.campaign_id}-predecessor",
            "campaign_manifest_sha256": "0" * 64,
            "reason": codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON,
        }
    if getattr(arguments, "two_job_candidate", False):
        # Campaign 冻结 Job 清单（provenance 按它裁定 Job 收据身份）补第二个候选 Job；执行定义以批准场景清单为准。
        core = next(job for job in manifest["jobs"] if job["id"] == JOB_A)
        aux = json.loads(json.dumps(core))
        aux["id"] = JOB_B
        aux["description"] = f"测试候选抓包 {JOB_B}"
        manifest["jobs"].append(aux)
    if not full_chain:
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
        prepare_evidence=_prepare_side_evidence(None, client_version=TARGET_VERSION if full_chain else None),
        extra_artifacts=_extra_artifacts(False),
    )
    if full_chain:
        successor = Path(fixture["data"]) / "evidence" / "campaigns" / arguments.campaign_id
        import_arguments = [
            "reuse-official-evidence", "--predecessor-campaign-dir", str(campaign_dir),
            "--campaign-dir", str(successor), "--campaign-id", arguments.campaign_id,
            "--codex-account-id", str(manifest["configuration"]["codex_account_id"]),
        ]
        code, stdout, stderr = case._run_main(import_arguments)
        if code != 0:
            raise RuntimeError(f"连续链复用导入失败：{stderr}")
        imported = json.loads(stdout)
        if imported["live_request_count"] != 0 or imported["executed_job_count"] != 0:
            raise RuntimeError("连续链复用导入不满足零请求、零执行边界")
        campaign_dir = successor
        manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
        fixture = {**fixture, "campaign_dir": campaign_dir, "manifest": manifest}
        (campaign_dir / "control" / "vc-chain").mkdir(mode=0o700, exist_ok=True)
        _write(root / "full-chain-import.json", imported)
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
    if getattr(arguments, "two_job_candidate", False):
        profile_payload = two_scenario_profile(profile_payload)
    profile_payload["codex_version"] = TARGET_VERSION
    if full_chain:
        # 行为版本由真实 HTTP 头断言承载；场景对象不能添加 schema 未登记的字段。
        profile_payload["rules"][0]["checks"].append({
            "id": "client-version", "description": "合成请求的 UA 精确绑定目标客户端版本",
            "select": {"record_type": "http_request"},
            "assertion": {"operator": "all_equal", "path": "data.header_values.user-agent", "value": [f"codex_cli_rs/{TARGET_VERSION}"]},
        })
    spec_path = Path(codex_upgrade.__file__).resolve().parents[2] / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
    if not spec_path.is_file():
        spec_path = Path(arguments.spec_path).resolve()
    profile_payload["source_spec_sha256"] = codex_upgrade.source_spec_section_sha256(spec_path, "第二章")
    runtime_profile_payload = {"transport": "codex-official-egress", "rule_count": len(RULES), "Version": TARGET_VERSION, "Digest": "c" * 64}
    target, migration, scenario, profile, assertion_profile, _rules = case._write_classification_manifests(
        root, rules=RULES, assertion_profile_payload=profile_payload, version=TARGET_VERSION, profile_payload=runtime_profile_payload
    )
    if getattr(arguments, "two_job_candidate", False):
        _rewrite_two_job_scenarios(scenario, runs_root=Path(str(fixture["data"])) / "runs")
    if full_chain:
        from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import classify_full_chain

        classify_full_chain(case, root, fixture, state_dir, (target, migration, scenario, profile, assertion_profile))
    approved_root = campaign_dir / "classification" / "approved"
    if full_chain:
        classify_payload = codex_upgrade._load_stage_result(campaign_dir, "classify")
        references = {key: classify_payload[key] for key in (
            "target_rule_manifest", "migration_manifest", "scenario_manifest", "profile_manifest", "assertion_profile_manifest",
        )}
        joint = classify_payload["joint_manifest_sha256"]
        requirements = _read(campaign_dir / classify_payload["post_promotion_gate_requirements"]["path"])
    else:
        classify_payload, references, joint, requirements = _prepare_synthetic_classification(
            campaign_dir, manifest, approved_root, target, migration, scenario, profile, assertion_profile, identity_fixture, case,
        )
    # 连续链的 VC-2 已通过 classify 草案、预览、批准三批封存；旧评估链保留原有两批基座。
    if full_chain:
        from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import stage_full_chain

        stage_full_chain(case, root, fixture, state_dir, identity_fixture)
    phases = () if full_chain else ((2, "VC-2"), (3, "VC-3"))
    vc3_receipt_source = identity_fixture.catalog_receipt_path if identity_fixture is not None else None
    for sequence, phase in phases:
        plan = case._vc_chain_action_plan(root, campaign_dir, phase, stage_receipt_source=(vc3_receipt_source if phase == "VC-3" else None))
        result, returncode = codex_upgrade.compile_and_run_vc_batch(case._vc_chain_arguments({**fixture, "state_dir": state_dir}, phase, sequence, plan))
        if returncode != 0:
            raise SystemExit(f"{phase} 合成批次失败：{json.dumps(result, ensure_ascii=False, default=str)[:2000]}")
    return _finish_candidate_identity(arguments, case, root, fixture, campaign_dir, manifest, state_dir, identity_fixture, requirements)


def _prepare_synthetic_classification(campaign_dir, manifest, approved_root, target, migration, scenario, profile, assertion_profile, identity_fixture, case):
    """保留旧评估恢复链的合成分类输入；连续链不得调用本函数。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture.tests import candidate_identity_fixture as cif

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
    return classify_payload, references, joint, requirements


def _record_build_batch(case, root, fixture, state_dir, manifest, identity_fixture, parameters_path, implementation_root, implementation_receipt):
    """以 VC-4 正式批次派发 record-candidate-build（参数与候选身份夹具的进程内调用逐项相同）。"""

    from tools.official_client_capture.tests import candidate_identity_fixture as cif
    from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import dispatch_cli

    fixture_identity = identity_fixture
    campaign_dir = Path(fixture["campaign_dir"])
    arguments = [
        "record-candidate-build", "--campaign-dir", str(campaign_dir), "--candidate-id", fixture_identity.candidate_id,
        "--candidate-purpose", str(manifest["campaign_purpose"]),
        "--candidate-source", str(fixture_identity.source.resolve()),
        "--candidate-binary", str(fixture_identity.binary.resolve()),
        "--runtime-image", str(fixture_identity.runtime_image),
        "--candidate-image-id", str(fixture_identity.image_id),
        "--build-id", f"build-eval-{fixture_identity.nonce}",
        "--deployed-version", str(manifest["target_version"]),
        "--target-architecture", cif.TARGET_ARCHITECTURE,
        "--build-parameters", str(parameters_path.resolve()),
        "--build-tree", str(fixture_identity.build_tree.resolve()),
        "--docker-context", str(fixture_identity.context.resolve()),
        "--frontend-dist-source", str(fixture_identity.dist_source.resolve()),
        "--catalog-stage-dir", str((fixture_identity.source / "catalog").resolve()),
        "--source-transition", str(fixture_identity.transition_path.resolve()),
        "--gate-plan", str((fixture_identity.source / "gates" / "gate-plan.json").resolve()),
        "--implementation-test-root", str(implementation_root.resolve()),
        "--implementation-test-receipt", str(implementation_receipt.resolve()),
    ]
    candidate_id = fixture_identity.candidate_id

    def recorded(campaign):
        from tools.official_client_capture import codex_upgrade

        return {"status": "complete", "build_receipt": str(codex_upgrade._candidate_build_receipt_path(Path(campaign), candidate_id))}

    return dispatch_cli(case, root, fixture, state_dir, "VC-4", None, "record-candidate-build", arguments,
                        timeout_seconds=900, payload=recorded)


def _finish_candidate_identity(arguments, case, root, fixture, campaign_dir, manifest, state_dir, identity_fixture, requirements):
    """按共享候选夹具建立真实制品；保留旧链的无 Docker 开发机分支。"""

    from tools.official_client_capture import codex_upgrade

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
        from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import late_stage_faults

        if "record-candidate-build" in late_stage_faults():
            # R18 后段注入链：与生产 vc4.sh 同形，经 VC-4 批次派发 record-candidate-build，
            # 便于注入父批次失败并走对账与同批重派；其余链保持进程内调用不变。
            recorded = _record_build_batch(
                case, root, {**fixture, "campaign_dir": campaign_dir}, state_dir, manifest, identity_fixture,
                parameters_path, implementation_root, implementation_receipt,
            )
        else:
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
        sequence = max(codex_upgrade._committed_vc_sequences(campaign_dir, manifest), default=0) + 1
        result, returncode = codex_upgrade.compile_and_run_vc_batch(case._vc_chain_arguments({**fixture, "state_dir": state_dir}, "VC-4", sequence, plan))
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
    return {
        "root": root,
        "fixture": fixture,
        "campaign_dir": campaign_dir,
        "manifest": manifest,
        "state_dir": state_dir,
        "identity": identity,
        "identity_fixture": identity_fixture,
    }


def stage_init(arguments: argparse.Namespace) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade

    case = new_real_chain_case(full_chain=getattr(arguments, "full_chain", False))
    context = init_campaign_to_vc4(arguments, case)
    root, fixture, campaign_dir, manifest = context["root"], context["fixture"], context["campaign_dir"], context["manifest"]
    state_dir, identity, identity_fixture = context["state_dir"], context["identity"], context["identity_fixture"]
    # 候选 seal（真实 bundle 作证据根；候选证据 surface 由参数决定，全程不再更换）。
    case._write_capture_stage(
        campaign_dir,
        campaign_dir / "candidate-evidence",
        phase="candidate",
        identity=identity,
        candidate_id=CANDIDATE_ID,
        prepare_evidence=_prepare_side_evidence(arguments.candidate_surface, client_version=TARGET_VERSION if getattr(arguments, "full_chain", False) else None),
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
# 改造 5 M2（T5.18）：attempt-recovery 端到端链的驱动扩展
#
# 两 Job 候选：Job-A（candidate-frozen-core，证据＝surface 记录，场景 A04）与 Job-B（candidate-frozen-aux，
# 证据＝H1 relay 流＋派生观测，场景 A03）；SPEC-EP-006 只绑定 A04、SPEC-H1-001 只绑定 A03，两条规则的
# 依赖投影分属两个 Job。b0 的 Job-A 证据 surface=other → SPEC-EP-006 fail → transient-environment
# → ar1 只重采 Job-A（合成步骤真实执行，写 surface=codex）→ 增量封存 → b1：SPEC-EP-006 重跑、SPEC-H1-001
# 复用（一条复用、一条重跑）→ accept → completion。
# ---------------------------------------------------------------------------

JOB_A = "candidate-frozen-core"
JOB_B = "candidate-frozen-aux"
SCENARIO_A = "A04"  # Job-A：surface 身份
SCENARIO_B = "A03"  # Job-B：H1 relay
AR_STATE_ENV = "EVALUATION_CHAIN_STATE"

# 恢复段 run／seal 动作的包装脚本：同一 argv 交给真实 codex_upgrade.main，只 mock 外部环境依赖
# （容器身份、ARM64／容器探针、恢复 finalizer、候选凭据、Kilo 后探针）、Campaign 冻结 Job 定义的
# 读取（合成 Job，步骤真实执行）与权限收口的 runs 别名（<data>/runs 与宿主 runs 同 inode）。
# 恢复段 run 动作的崩溃开关（A1：Job 证据已写、段摘要未写时动作子进程被 SIGKILL）；开关放文件不放命令。
AR_CRASH_FLAG = "ar-segment-crash.flag"
# 恢复段 Job 失败开关（R2 变体：段 run 动作以 status=failed 退出，父 run 再在 post-run-tooling 收据前崩溃）。
AR_FAIL_FLAG = "ar-segment-fail.flag"
AR_ACTION_WRAPPER = (
    "import sys\n"
    "from tools.official_client_capture.tests import evaluation_chain_driver as driver\n"
    "with driver.attempt_recovery_environment_patches(sys.argv[1]):\n"
    "    from tools.official_client_capture import codex_upgrade\n"
    "    raise SystemExit(codex_upgrade.main(sys.argv[2:]))\n"
)
AR_SEAL_WRAPPER = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from tools.official_client_capture.tests import evaluation_chain_driver as driver\n"
    "with driver.attempt_recovery_environment_patches(sys.argv[1]):\n"
    "    from tools.official_client_capture import codex_upgrade\n"
    "    argv = sys.argv[2:]\n"
    "    # 预览（真实 CLI 以退出码 2 + approval_required 停靠）→ 读取机器事实摘要 → 以同一摘要批准（真实 CLI）。\n"
    "    rc = codex_upgrade.main(argv)\n"
    "    if rc not in (0, 2):\n"
    "        raise SystemExit(3)\n"
    "    state = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
    "    preview_path = Path(state['segment_root']) / 'seal-preview.json'\n"
    "    if not preview_path.is_file():\n"
    "        sys.stderr.write('seal 预览未停靠在 approval_required：' + str(preview_path) + '\\n')\n"
    "        raise SystemExit(4)\n"
    "    preview = json.loads(preview_path.read_text(encoding='utf-8'))\n"
    "    raise SystemExit(codex_upgrade.main([*argv, '--approve-seal-sha256', preview['review_sha256']]))\n"
)


class attempt_recovery_environment_patches:
    """恢复段 run／seal 子进程内的外部环境替身（上下文管理器）。"""

    def __init__(self, state_path: str) -> None:
        self.state = _read(Path(state_path))
        self.patchers: list[Any] = []

    def __enter__(self) -> "attempt_recovery_environment_patches":
        from tools.official_client_capture import codex_upgrade
        from tools.official_client_capture import codex_upgrade_evidence_permissions as permissions

        state = self.state
        data_root = Path(state["data"])
        alias_root = data_root / "runs"

        def arm64_receipt(target: Path, *, phase: str, subject_id: str, **_kwargs: Any) -> tuple[Path, dict[str, Any]]:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = target / "receipt.json"
            receipt = {"schema_version": "codex-upgrade-arm64-environment-receipt/v1", "status": "passed", "phase": phase, "subject_id": subject_id, "continuity_identity_sha256": "c" * 64}
            _write(path, receipt)
            return path, receipt

        def probe(_manifest: Any, target: Path, phase: str, **_kwargs: Any) -> dict[str, Any]:
            # 环境探针替身：写五份 guard 规范化状态快照与探针清单（与真实探针同一目录布局、同一文件名、同一快照绑定），
            # 恢复 finalizer（真实 _finalize_attempt_restoration → receipts/restoration-report.json）不再替换。
            # 清单逐项列出快照摘要：R11 判据④在段摘要缺失时要拿它与恢复报告的 after 引用逐项核对。
            from tools.official_client_capture import codex_upgrade_environment_probe as probe_module
            from tools.official_client_capture.tests import test_codex_upgrade

            fixture = test_codex_upgrade.CodexUpgradeTest
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            snapshots = []
            for key, filename in probe_module.STATE_FILES.items():
                state_payload = fixture._database_state(after=True) if key == "database" else {"probe_kind": f"attempt_recovery_{key}", "stable_value": "restored"}
                fixture._write_state_snapshot(target / filename, state_payload)
                snapshots.append(probe_module._snapshot_binding(target / filename, (target / filename).read_bytes(), key))
            document = {"schema_version": "codex-upgrade-environment-probe/v1", "phase": phase, "observed_at_utc": _utc_now(),
                        "snapshots": snapshots}
            _write(target / "probe-manifest.json", document)
            return document

        def closeout(attempt_root: Path, roots: list[Path]) -> dict[str, Any]:
            receipt_path, _receipt = permissions.close_evidence_permissions(attempt_root, roots, managed_data_root=data_root, logical_runs_roots=(alias_root,))
            return permissions.receipt_binding(attempt_root, receipt_path)

        def replay(attempt_root: Path, roots: list[Path], binding: Mapping[str, Any]) -> dict[str, Any]:
            return permissions.replay_evidence_permission_closeout(attempt_root, list(roots), binding, managed_data_root=data_root, logical_runs_roots=(alias_root,))

        def post_client(_manifest: Any, evidence_root: Path, _candidate_id: str, **_kwargs: Any) -> tuple[Path, dict[str, Any], str, bool]:
            # Kilo 后检查点：段证据根内固定派生路径 environment/client-after/probe-manifest.json 与
            # receipts/client-restoration-report.json 由 ar-prepare 合成（与生产落点一致）。
            checkpoint = _read(evidence_root / "environment" / "client-after" / "probe-manifest.json")
            report = evidence_root / "receipts" / "client-restoration-report.json"
            return report, _read(report), str(checkpoint["observed_at_utc"]), False

        def run_job(job: Any, log_root: Path, attempt_index: int = 1, scenario_context: Any = None, **_kwargs: Any) -> dict[str, Any]:
            # 真实 Job 定义（execution_sha256 与原预约同源）＋合成执行：证据写到 <data>/runs/<重定位后的根名>
            # （真实模式在 /root/oauth-capture/runs 下，测试机不写那里）。
            root = alias_root / Path(str(job.evidence_roots[0])).name
            if (Path(state["root"]) / AR_FAIL_FLAG).exists():
                # 段 Job 真实失败（步骤退出 3、证据缺失）→ 段 status=failed → 段对账。
                return {
                    "id": job.job_id, "phase": "candidate", "required": job.required, "execution_sha256": codex_upgrade._job_execution_sha256(job),
                    "status": "failed", "attempt_index": attempt_index, "description": job.description, "duration_seconds": 0.01,
                    "steps": [{"argv": ["synthetic"], "return_code": 3, "log": ""}], "evidence_roots": [], "missing_evidence_patterns": [str(root)],
                    "empty_evidence_patterns": [], "covers": list(job.covers), "scenario_ids": list(job.scenario_ids), "scenario_receipts": [],
                    "scenario_receipt_failures": [], "track": getattr(job, "track", "main"), "model_id": getattr(job, "model_id", ""),
                    "expected_use_responses_lite": getattr(job, "expected_use_responses_lite", False), "required_model_receipt": getattr(job, "required_model_receipt", False),
                    "model_condition_receipt": None, "model_condition_receipt_failure": None, "disposition": "executed",
                }
            _write_job_evidence(root, job_id=job.job_id, surface="codex")
            if (Path(state["root"]) / AR_CRASH_FLAG).exists():
                # A1：Job 证据已落盘、job 收据与段摘要未写时段 run 子进程被 SIGKILL。
                import signal

                os.kill(os.getpid(), signal.SIGKILL)
            log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_path = log_root / f"{job.job_id}-1.log"
            log_path.write_text("synthetic recovery run\n", encoding="utf-8")
            log_path.chmod(0o600)
            return {
                "id": job.job_id, "phase": "candidate", "required": job.required, "execution_sha256": codex_upgrade._job_execution_sha256(job),
                "status": "complete", "attempt_index": attempt_index, "description": job.description, "duration_seconds": 0.01,
                "steps": [{"argv": ["synthetic"], "return_code": 0, "log": str(log_path)}], "evidence_roots": [str(root)],
                "missing_evidence_patterns": [], "empty_evidence_patterns": [], "covers": list(job.covers), "scenario_ids": list(job.scenario_ids),
                "scenario_receipts": [], "scenario_receipt_failures": [], "track": getattr(job, "track", "main"), "model_id": getattr(job, "model_id", ""),
                "expected_use_responses_lite": getattr(job, "expected_use_responses_lite", False), "required_model_receipt": getattr(job, "required_model_receipt", False),
                "model_condition_receipt": None, "model_condition_receipt_failure": None, "disposition": "executed",
            }

        # seal 断言门禁（ACC-03）按"仓库冻结验收画像"执行；本 Campaign 的批准画像是 e2e 两场景简化画像，
        # 门禁逻辑（bundle provenance／覆盖矩阵／selector 可达性）真实执行，只把画像来源换成 Campaign 批准画像、
        # 冻结契约摘要改为按该画像现算（与 test_assertion_bundle_wiring 同一口径）。
        approved_profile = _read(Path(state["campaign_dir"]) / "classification" / "approved" / "assertion-profile.json")

        self.patchers = [
            mock.patch.object(codex_upgrade, "load_acceptance_profile", side_effect=lambda _path: json.loads(json.dumps(approved_profile))),
            mock.patch.object(codex_upgrade, "verify_frozen_contract", side_effect=codex_upgrade.build_acceptance_contract),
            mock.patch.object(codex_upgrade, "run_job", side_effect=run_job),
            mock.patch.object(codex_upgrade, "_verify_candidate_attempt_identity"),
            mock.patch.object(codex_upgrade, "_validate_candidate_admin_credential"),
            mock.patch.object(codex_upgrade, "_capture_arm64_environment_receipt", side_effect=arm64_receipt),
            # 本夹具的环境收据是明确的零请求外部替身，按其固定 continuity 字段模拟相等。
            mock.patch.object(codex_upgrade.codex_upgrade_arm64_environment_receipt, "receipts_equivalent",
                              side_effect=lambda _a, before, _b, after: before["continuity_identity_sha256"] == after["continuity_identity_sha256"]),
            mock.patch.object(codex_upgrade, "_probe_capture_environment", side_effect=probe),
            mock.patch.object(codex_upgrade, "_close_attempt_evidence_permissions", side_effect=closeout),
            mock.patch.object(codex_upgrade, "_replay_evidence_permission_closeout", side_effect=replay),
            mock.patch.object(codex_upgrade, "_candidate_post_client_restoration", side_effect=post_client),
        ]
        for patcher in self.patchers:
            patcher.start()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        for patcher in reversed(self.patchers):
            patcher.stop()
        return False


def _timestamp_from(base: datetime, offset_seconds: int) -> str:
    return (base + timedelta(seconds=offset_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_job_evidence(root: Path, *, job_id: str, surface: str, h1_method: str = "POST") -> None:
    """Job 证据落盘：Job-A（A04）＝surface 记录 + 自身场景的 relay 流（结构化 trace 只能绑定同场景原始证据）；
    Job-B（A03）＝H1 relay 流（``h1_method`` 非 POST 即 SPEC-H1-001 失败的坏证据）。"""

    from tools.official_client_capture.tests import test_acceptance_end_to_end as e2e

    stream = e2e.H1_STREAM if h1_method == "POST" else e2e.H1_STREAM.replace(b"POST ", h1_method.encode("ascii") + b" ", 1)
    (root / "relay").mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "relay" / "conn001.client_to_upstream.bin").write_bytes(stream)
    if job_id == JOB_A:
        record = dict(e2e.INTERNAL_RECORD, scenario_id=SCENARIO_A, data={"surface": surface}, source_artifacts=[f"{root.name}/relay/conn001.client_to_upstream.bin"])
        (root / "traces").mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "traces" / "surface.observation.jsonl").write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    _private_tree(root)


def _private_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _build_two_root_bundle(bundle_dir: Path, root_a: Path, root_b: Path) -> Path:
    """由两个 Job 根收口 assertion bundle（bundle plan root 名＝Job 根目录名，供 failure-scope 定位链映射）。"""

    from tools.official_client_capture import build_assertion_bundle as bundle_module
    from tools.official_client_capture import derive_official_observations as derive
    from tools.official_client_capture import assertion_gate as gate
    from tools.official_client_capture import candidate_rule_assertion as assertion

    side_dir = bundle_dir.parent / f".{bundle_dir.name}-plan"
    side_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = [
        {"root": root_a.name, "path": "traces/surface.observation.jsonl", "target": f"{root_a.name}/traces/surface.observation.jsonl"},
        {"root": root_a.name, "path": "relay/conn001.client_to_upstream.bin", "target": f"{root_a.name}/relay/conn001.client_to_upstream.bin"},
        {"root": root_b.name, "path": "relay/conn001.client_to_upstream.bin", "target": f"{root_b.name}/relay/conn001.client_to_upstream.bin"},
    ]
    plan_path = side_dir / "bundle-plan.json"
    plan_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    bundle_module.build_bundle({root_a.name: root_a, root_b.name: root_b}, bundle_module.load_plan(plan_path), bundle_dir)
    derive_plan = side_dir / "derive-plan.json"
    derive_plan.write_text(
        json.dumps({"entries": [{"source": f"{root_b.name}/relay/conn001.client_to_upstream.bin", "parser": "h1_request_stream", "scenario_id": SCENARIO_B, "kind": "process_trace", "target": f"derived/{SCENARIO_B}/conn001.observation.jsonl", "connection_id": "conn001"}]}),
        encoding="utf-8",
    )
    derive.derive_observations(bundle_dir, derive.load_derivation_plan(derive_plan))

    def artifact(path: str, kind: str, parser: str, scenario: str) -> dict[str, Any]:
        return {"path": path, "sha256": _sha(bundle_dir / path), "kind": kind, "parser": parser, "scenario_ids": [scenario], "labels": {"transport": "http"}}

    manifest = {
        "schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION,
        "codex_version": TARGET_VERSION,
        "capture_id": f"candidate-{CANDIDATE_ID}",
        "status": "complete",
        "artifacts": [
            artifact(f"{root_b.name}/relay/conn001.client_to_upstream.bin", "relay_binary", "opaque_bound_source", SCENARIO_B),
            artifact(f"derived/{SCENARIO_B}/conn001.observation.jsonl", "process_trace", "observation_jsonl", SCENARIO_B),
            artifact(f"{root_a.name}/relay/conn001.client_to_upstream.bin", "relay_binary", "opaque_bound_source", SCENARIO_A),
            artifact(f"{root_a.name}/traces/surface.observation.jsonl", "process_trace", "observation_jsonl", SCENARIO_A),
        ],
    }
    manifest_path = bundle_dir / gate.MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _private_tree(bundle_dir)
    return manifest_path


def _rewrite_two_job_scenarios(scenario_path: Path, *, runs_root: Path) -> None:
    """批准场景清单改为两候选 Job：Job-A（A04，SPEC-EP-006）与 Job-B（A03，SPEC-H1-001）；官方 Job 绑定两场景。

    候选 Job 证据根写成 ``<data>/runs/{campaign_id}-<job>``（真实模式 ``{capture_root}/runs/<campaign>-<job>`` 的
    测试等价：位于受管数据根的 runs 别名下，权限收口与段证据根重定位按同一规则生效）；执行摘要不含
    scenario_ids／covers，官方 Job 改绑场景不影响已封存官方结果的 execution_sha256。"""

    doc = json.loads(scenario_path.read_text(encoding="utf-8"))
    doc["evidence_scenarios"] = [
        {"scenario_id": SCENARIO_B, "description": "responses 出站（Job-B）", "trigger": "codex exec", "preconditions": ["已登录"], "required_artifact_kinds": ["relay_binary", "process_trace"], "covers": ["SPEC-H1-001"]},
        {"scenario_id": SCENARIO_A, "description": "内部 surface 身份（Job-A）", "trigger": "codex exec", "preconditions": ["已登录"], "required_artifact_kinds": ["relay_binary", "process_trace"], "covers": ["SPEC-EP-006"]},
    ]
    doc["variable_contract"].append(
        {"name": "campaign_id", "type": "string", "required": True, "sensitive": False, "description": "测试 Campaign 标识（候选 Job 证据根目录名前缀）。"}
    )
    official = next(job for job in doc["capture_jobs"] if job["phase"] == "official")
    official["scenario_ids"] = [SCENARIO_B, SCENARIO_A]
    template = next(job for job in doc["capture_jobs"] if job["phase"] == "candidate")
    jobs = []
    for job_id, scenario_id, rule in ((JOB_A, SCENARIO_A, "SPEC-EP-006"), (JOB_B, SCENARIO_B, "SPEC-H1-001")):
        job = json.loads(json.dumps(template))
        job["id"] = job_id
        job["description"] = f"测试候选抓包 {job_id}"
        job["covers"] = [rule]
        job["scenario_ids"] = [scenario_id]
        job["evidence_roots"] = [f"{runs_root}/{{campaign_id}}-{job_id}"]
        jobs.append(job)
    doc["capture_jobs"] = [official, *jobs]
    scenario_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def two_scenario_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """e2e 画像改为两场景：SPEC-H1-001→A03（Job-B），SPEC-EP-006→A04（Job-A）。"""

    profile = json.loads(json.dumps(profile))
    profile["scenarios"] = [
        {"scenario_id": SCENARIO_B, "description": "responses 出站", "trigger": "codex exec", "preconditions": ["已登录"], "required_artifact_kinds": ["relay_binary", "process_trace"]},
        {"scenario_id": SCENARIO_A, "description": "内部 surface 身份", "trigger": "codex exec", "preconditions": ["已登录"], "required_artifact_kinds": ["relay_binary", "process_trace"]},
    ]
    for rule in profile["rules"]:
        rule["scenario_ids"] = [SCENARIO_A] if rule["rule_id"] == "SPEC-EP-006" else [SCENARIO_B]
    return profile


def _synthesize_two_job_candidate_stage(case: Any, context: dict[str, Any], *, surface: str, h1_method: str = "POST") -> dict[str, Any]:
    """两 Job 候选 attempt（真实预约／attempt.json／checkpoint 链）+ bundle + 候选收据 + EvidenceManifest +
    候选阶段结果（b0）。attempt.json 按 M1 夹具口径冻结为 v2 schema（不伪造 v3 权限收据）。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_evidence_manifest
    from tools.official_client_capture import codex_upgrade_receipt_finalizer as finalizer
    from tools.official_client_capture import assertion_gate as gate

    campaign_dir: Path = context["campaign_dir"]
    manifest = context["manifest"]
    identity = context["identity"]
    data_root = Path(str(context["fixture"]["data"]))
    runs_root = data_root / "runs"
    runs_root.mkdir(mode=0o700, exist_ok=True)
    campaign_id = str(manifest["campaign_id"])
    # 真实 Job 定义（Campaign 冻结场景清单）：compare／seal 校验 results 的 execution_sha256 必须等于它；
    # 证据根＝<data>/runs/<模式目录名>（真实模式 {capture_root}/runs/<campaign>-<job> 的目录名）。
    real_jobs = {
        job.job_id: job
        for job in codex_upgrade._campaign_jobs(
            campaign_dir, manifest, "candidate", candidate_id=CANDIDATE_ID,
            runtime_image=str(identity.get("image_reference", "")), profile_id=str(identity.get("profile_id", "")),
            profile_digest=str(identity.get("profile_digest", "")), build_id=str(identity.get("build_id", "")),
            deployed_version=str(identity.get("deployed_version", "")), candidate_image_id=str(identity.get("image_id", "")),
            source_tree_sha256=str(identity.get("source_tree_sha256", "")), candidate_purpose=str(identity.get("candidate_purpose", "")),
        )
    }
    jobs = [real_jobs[JOB_A], real_jobs[JOB_B]]
    root_a = runs_root / Path(str(real_jobs[JOB_A].evidence_roots[0])).name
    root_b = runs_root / Path(str(real_jobs[JOB_B].evidence_roots[0])).name
    _write_job_evidence(root_a, job_id=JOB_A, surface=surface)
    _write_job_evidence(root_b, job_id=JOB_B, surface=surface, h1_method=h1_method)
    attempt_root, reservation = codex_upgrade._reserve_capture_attempt(
        campaign_dir, phase="candidate", candidate_id=CANDIDATE_ID, identity=identity, jobs=jobs, allow_failed_rerun=True
    )
    attempt_id = attempt_root.name
    run_nonce = str(reservation["run_nonce"])
    evidence_root = attempt_root / "evidence"
    logs_root = attempt_root / "logs"
    environment_root = evidence_root / "environment"
    for path in (evidence_root, logs_root, environment_root):
        path.mkdir(mode=0o700, exist_ok=True)
    attempt_started_at_utc = str(reservation["started_at_utc"])
    # 收据时间窗：观测时刻必须落在 attempt 开始与 Kilo 后检查点之间——以预约时刻为基准。
    base = datetime.fromisoformat(attempt_started_at_utc.replace("Z", "+00:00"))
    client_checkpoint_at_utc = _timestamp_from(base, 360)
    # 探针／恢复／ARM64 收据（evidence_root 内）。
    for phase_name in ("before", "after"):
        _write(environment_root / phase_name / "probe-manifest.json", {"schema_version": "codex-upgrade-environment-probe/v1", "phase": phase_name, "observed_at_utc": _timestamp_from(base, 5 if phase_name == "before" else 300)})
    for phase_name in ("arm64-before", "arm64-after"):
        _write(environment_root / phase_name / "receipt.json", {"schema_version": "codex-upgrade-arm64-environment-receipt/v1", "status": "passed", "phase": phase_name.replace("arm64-", "attempt_"), "subject_id": attempt_id, "continuity_identity_sha256": "c" * 64})
    restoration_report = evidence_root / "restoration-report.json"
    restoration_arguments: dict[str, Any] = {"evidence_root": evidence_root, "output": Path(restoration_report.name), "phase": "candidate", "candidate_id": CANDIDATE_ID}
    for check_id, before_name, after_name, comparator in finalizer.RESTORATION_INPUTS:
        before_path = evidence_root / f"{before_name}.json"
        after_path = evidence_root / f"{after_name}.json"
        if comparator == "before_subset":
            before_state, after_state = case._database_state(after=False), case._database_state(after=True)
        else:
            before_state = {"probe_kind": check_id, "stable_value": "restored"}
            after_state = dict(before_state)
        case._write_state_snapshot(before_path, before_state)
        case._write_state_snapshot(after_path, after_state)
        restoration_arguments[before_name] = Path(before_path.name)
        restoration_arguments[after_name] = Path(after_path.name)
    _private_tree(evidence_root)
    finalizer.finalize_restoration(argparse.Namespace(**restoration_arguments))
    # bundle（两 Job 根收口）与候选收据（Kilo／observed／post-client／client-after）。
    bundle_dir = evidence_root / gate.BUNDLE_DIR_NAME
    capture_manifest = _build_two_root_bundle(bundle_dir, root_a, root_b)
    third_party_model = codex_upgrade._third_party_client_model(manifest["configuration"])
    observed_profile_path, client_receipts, post_client_report = case._synthesize_candidate_receipts(
        evidence_root,
        campaign_manifest=manifest,
        attempt_id=attempt_id,
        run_nonce=run_nonce,
        attempt_started_at_utc=attempt_started_at_utc,
        client_checkpoint_at_utc=client_checkpoint_at_utc,
        identity=identity,
        candidate_id=CANDIDATE_ID,
        target_version=TARGET_VERSION,
        third_party_model=third_party_model,
        timestamp=lambda offset: _timestamp_from(base, offset),
    )
    _private_tree(evidence_root)
    # results／checkpoint 链／job 收据（两 Job 均 complete）。
    results: list[dict[str, Any]] = []
    store = codex_upgrade.incremental_recovery.CheckpointStore(attempt_root / "checkpoints")
    previous: str | None = None
    job_root_by_id = {JOB_A: root_a, JOB_B: root_b}
    for job in jobs:
        result = {
            "id": job.job_id, "phase": "candidate", "required": True, "execution_sha256": codex_upgrade._job_execution_sha256(job), "status": "complete",
            "description": job.description, "duration_seconds": 0.0, "steps": [], "evidence_roots": [str(job_root_by_id[job.job_id])], "missing_evidence_patterns": [],
            "empty_evidence_patterns": [], "covers": [], "scenario_ids": list(job.scenario_ids), "scenario_receipts": [], "scenario_receipt_failures": [],
            "track": "main", "model_id": "gpt-5.5", "expected_use_responses_lite": False, "required_model_receipt": False, "model_condition_receipt": None,
            "model_condition_receipt_failure": None, "disposition": "executed",
        }
        codex_upgrade._secure_write_json_once(attempt_root / f"job-{job.job_id}.json", result)
        appended = store.append(
            {
                "checkpoint_schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA, "campaign_id": campaign_id, "phase": "candidate", "attempt_id": attempt_id,
                "run_nonce": run_nonce, "item_id": job.job_id, "status": "complete", "disposition": "executed",
                "result_sha256": codex_upgrade.incremental_recovery.digest(result), "result_key": None, "result": result, "source_receipt": None,
                "previous_checkpoint_sha256": previous,
            }
        )
        previous = str(appended["checkpoint_sha256"])
        results.append(result)
    all_roots = [root_a, root_b, evidence_root, logs_root]
    environment = {
        "evidence_root": str(evidence_root),
        "before_probe": case._environment_binding(evidence_root, environment_root / "before" / "probe-manifest.json"),
        "after_probe": case._environment_binding(evidence_root, environment_root / "after" / "probe-manifest.json"),
        "restoration_report": case._environment_binding(evidence_root, restoration_report),
        "arm64_before_receipt": case._environment_binding(evidence_root, environment_root / "arm64-before" / "receipt.json"),
        "arm64_after_receipt": case._environment_binding(evidence_root, environment_root / "arm64-after" / "receipt.json"),
    }
    with mock.patch.object(codex_upgrade, "_replay_attempt_evidence_permissions", return_value={}):
        attempt = codex_upgrade._write_capture_attempt(
            campaign_dir,
            attempt_root,
            {
                "campaign_id": campaign_id, "phase": "candidate", "candidate_id": CANDIDATE_ID, "status": "awaiting_receipts",
                "identity": identity, "results": results, "evidence_roots": [str(root) for root in all_roots],
                "environment": environment, "binary_verification": None, "execution_error": None, "restoration_error": None,
                "next_gate": "生成机器收据后 seal",
            },
        )
    attempt["schema_version"] = codex_upgrade.LEGACY_CAPTURE_ATTEMPT_SCHEMA
    attempt.pop("attempt_digest", None)
    attempt["attempt_digest"] = codex_upgrade._fingerprint(attempt)
    (attempt_root / "attempt.json").write_text(json.dumps(attempt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (attempt_root / "attempt.json").chmod(0o600)
    # 阶段结果（同 _write_capture_stage 口径）+ EvidenceManifest（候选也写）。
    restoration = codex_upgrade._validate_restoration_report(restoration_report, all_roots, phase="candidate", candidate_id=CANDIDATE_ID)
    restoration["post_client"] = codex_upgrade._validate_restoration_report(post_client_report, all_roots, phase="candidate", candidate_id=CANDIDATE_ID)
    receipt_identity = dict(campaign_id=campaign_id, attempt_id=attempt_id, run_nonce=run_nonce, attempt_started_at_utc=attempt_started_at_utc, client_checkpoint_at_utc=client_checkpoint_at_utc, candidate_id=CANDIDATE_ID, target_version=TARGET_VERSION)
    observed_profile, _ = codex_upgrade._validate_observed_profile_receipt(
        observed_profile_path, [evidence_root], expected_profile_id=str(identity["profile_id"]), expected_profile_digest=str(identity["profile_digest"]),
        image_id=str(identity["image_id"]), image_reference=str(identity["image_reference"]), source_tree_sha256=str(identity["source_tree_sha256"]),
        build_id=str(identity["build_id"]), deployed_version=str(identity["deployed_version"]), **receipt_identity,
    )
    client_bindings = codex_upgrade._parse_client_evidence(client_receipts, [evidence_root], model=third_party_model, identity=identity, **receipt_identity)
    evidence_manifest = codex_upgrade_evidence_manifest.build_evidence_manifest(all_roots, checkpoint_path=attempt_root / "evidence-manifest-checkpoint.json")
    manifest_path = attempt_root / "evidence-manifest.json"
    _write(manifest_path, evidence_manifest)
    normalized_surface = codex_upgrade.scan_evidence(all_roots, "target-candidate")
    surface_path = campaign_dir / "candidates" / CANDIDATE_ID / "surface.json"
    _write(surface_path, normalized_surface)
    capture_binding = case._binding(capture_manifest, f"{evidence_root.name}/{gate.BUNDLE_DIR_NAME}/{capture_manifest.name}")
    payload: dict[str, Any] = {
        "status": "complete",
        "campaign_mode": manifest["campaign_mode"],
        "campaign_purpose": manifest["campaign_purpose"],
        "candidate_purpose": manifest["campaign_purpose"],
        "attempt": case._binding(attempt_root / "attempt.json", (attempt_root / "attempt.json").relative_to(campaign_dir).as_posix()),
        "evidence_roots": [str(root) for root in all_roots],
        "identity": identity,
        "results": results,
        "surface": case._binding(surface_path, surface_path.relative_to(campaign_dir).as_posix()),
        "client_bindings": client_bindings,
        "assertion_context": {
            "capture_manifest": capture_binding,
            "capture_manifest_path": str(capture_manifest.resolve()),
            "evidence_root": str(bundle_dir.resolve()),
            "evidence_prefix": f"{evidence_root.name}/{gate.BUNDLE_DIR_NAME}",
        },
        "assertion_gate": {
            "side": "candidate", "bundle_dir_name": gate.BUNDLE_DIR_NAME, "bundle_provenance_sha256": _sha(bundle_dir / "provenance.json"), "bundle_entry_count": 2,
            "derived_provenance_sha256": None, "candidate_trace_receipt_sha256": None,
            "capture_manifest": {"path": "capture-manifest.json", "sha256": capture_binding["sha256"]},
            "acceptance_contract_sha256": "2" * 64, "artifact_count": 3, "observation_count": 2, "checked_rule_count": 2, "checked_check_count": 2,
        },
        "restoration": restoration,
        "observed_profile": observed_profile,
        "evidence_manifest": case._binding(manifest_path, manifest_path.relative_to(campaign_dir).as_posix()),
        "evidence_inventory": evidence_manifest["inventory"],
        "scan_summary": evidence_manifest["scan"],
        "security": {"raw_evidence_private": True, **evidence_manifest["security"]},
    }
    codex_upgrade._seal_preview(campaign_dir, attempt_root, phase="candidate", candidate_id=CANDIDATE_ID, attempt=attempt, stage_payload=payload, approve_sha256=None)
    preview_path = codex_upgrade._seal_preview_path(attempt_root, codex_upgrade._seal_transition_index(None))
    payload["seal_preview"] = case._binding(preview_path, preview_path.relative_to(campaign_dir).as_posix())
    codex_upgrade.save_stage_result(campaign_dir, "capture-candidate", payload, candidate_id=CANDIDATE_ID)
    return {
        "attempt_root": attempt_root,
        "attempt_id": attempt_id,
        "job_roots": {JOB_A: str(root_a), JOB_B: str(root_b)},
        "evidence_root": evidence_root,
    }


def stage_init_attempt_recovery(arguments: argparse.Namespace) -> dict[str, Any]:
    """M2 端到端：Campaign 到 VC-4 + 两 Job 候选 b0（surface 由参数决定，默认 other 让 SPEC-EP-006 失败）。"""

    case = new_real_chain_case()
    arguments.two_job_candidate = True
    context = init_campaign_to_vc4(arguments, case)
    root, fixture, campaign_dir, manifest = context["root"], context["fixture"], context["campaign_dir"], context["manifest"]
    state_dir, identity, identity_fixture = context["state_dir"], context["identity"], context["identity_fixture"]
    candidate = _synthesize_two_job_candidate_stage(case, context, surface=arguments.candidate_surface, h1_method=arguments.candidate_h1_method)
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
        "job_ids": [JOB_A, JOB_B],
        "job_roots": candidate["job_roots"],
        "two_job": True,
        "attempt_id": candidate["attempt_id"],
        "rules": list(RULES),
        "candidate_identity": identity_fixture.identity_state() if identity_fixture is not None else None,
        "recovery_revision": None,
        "segment_root": None,
        "build_receipt": identity.get("build_receipt", {}).get("path") if isinstance(identity.get("build_receipt"), Mapping) else None,
    }
    _write(root / "chain-state.json", state)
    case.doCleanups()
    return {"status": "initialized", **state}


def stage_ar_prepare(arguments: argparse.Namespace) -> dict[str, Any]:
    """恢复段 run 之后、seal 之前的 finalizer 产物（父进程合成，非受管）：本基线 bundle（两根：Job-A 新根、
    Job-B 原根）与恢复段证据根内的候选收据（Kilo 后探针清单／Kilo 后恢复报告／observed-profile／Kilo 收据）。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import assertion_gate as gate

    state = _load_state(arguments)
    campaign_dir = Path(state["campaign_dir"])
    recovery_revision = str(arguments.recovery_revision)
    baseline = int(arguments.baseline)
    segment_root, reservation, summary = _load_segment_without_permission_replay(campaign_dir, state, recovery_revision)
    recovered = {str(item["id"]): list(item["evidence_roots"]) for item in summary["results"]}
    root_a = Path(recovered[JOB_A][0]) if JOB_A in recovered else Path(state["job_roots"][JOB_A])
    root_b = Path(recovered[JOB_B][0]) if JOB_B in recovered else Path(state["job_roots"][JOB_B])
    private_root = codex_upgrade._baseline_private_root(campaign_dir, CANDIDATE_ID, baseline)
    private_root.mkdir(mode=0o700, exist_ok=True)
    bundle_dir = private_root / gate.BUNDLE_DIR_NAME
    capture_manifest = _build_two_root_bundle(bundle_dir, root_a, root_b)
    _private_tree(private_root)
    # 段证据根内的候选收据（绑定原 attempt id + 段 run_nonce + 段开始时刻）。段是 v3 权限收口口径：run 后
    # 只允许 client/、environment/client-after/、receipts/client-restoration-report.json 这些派生路径新增，
    # 合成收据全部落在段证据根的 client/ 子树（与生产 Kilo finalizer 的落点一致），否则 seal 重放收口即漂移。
    case = new_real_chain_case()
    manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
    segment_evidence_root = Path(str(summary["environment"]["evidence_root"]))
    base = datetime.fromisoformat(str(summary["started_at_utc"]).replace("Z", "+00:00"))
    # Kilo 后检查点须晚于恢复段完成时刻。
    completed = datetime.fromisoformat(str(summary["completed_at_utc"]).replace("Z", "+00:00"))
    client_checkpoint_at_utc = _timestamp_from(completed, 60)
    observed_profile_path, client_receipts, post_client_report = case._synthesize_candidate_receipts(
        segment_evidence_root,
        campaign_manifest=manifest,
        attempt_id=str(summary["attempt_id"]),
        run_nonce=str(summary["run_nonce"]),
        attempt_started_at_utc=str(summary["started_at_utc"]),
        client_checkpoint_at_utc=client_checkpoint_at_utc,
        identity=state["identity"],
        candidate_id=CANDIDATE_ID,
        target_version=TARGET_VERSION,
        third_party_model=codex_upgrade._third_party_client_model(manifest["configuration"]),
        timestamp=lambda offset: _timestamp_from(base, offset),
        receipts_subdir="client",
    )
    case.doCleanups()
    state["segment_root"] = str(segment_root)
    state["recovery_revision"] = recovery_revision
    state["baseline"] = baseline
    state["ar_seal"] = {
        "capture_manifest": str(capture_manifest),
        "assertion_evidence_root": str(bundle_dir),
        "observed_profile_receipt": str(observed_profile_path),
        "client_evidence": list(client_receipts),
    }
    _write(Path(state["root"]) / "chain-state.json", state)
    return {"status": "ar_prepared", "bundle": str(bundle_dir), "segment_root": str(segment_root), "recovered_roots": recovered}


def _load_segment_without_permission_replay(campaign_dir: Path, state: dict[str, Any], recovery_revision: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """父进程只读段预约与 run-summary（权限收口重放需 runs 别名注入，留给受管子进程）。"""

    attempt_root = campaign_dir / "candidates" / CANDIDATE_ID / "attempts" / str(state["attempt_id"])
    segment_root = attempt_root / "recovery" / recovery_revision
    return segment_root, _read(segment_root / "recovery-reservation.json"), _read(segment_root / "attempt-recovery.json")


def stage_ar_account(arguments: argparse.Namespace) -> dict[str, Any]:
    """真实 CLI account-sealed-candidate --attempt-recovery ar<k>（子进程，副本树）。"""

    from tools.official_client_capture import codex_upgrade

    state = _load_state(arguments)
    # 读增量封存结果要重放恢复段的 v3 权限收口 → 与 accept／status 同一读侧包装（runs 别名注入）。
    completed = subprocess.run(
        _read_side_command(state, "account-sealed-candidate", "--campaign-dir", state["campaign_dir"], "--candidate-id", CANDIDATE_ID, "--attempt-recovery", str(state["recovery_revision"])),
        capture_output=True, text=True, cwd=os.getcwd(), env=dict(os.environ),
    )
    if completed.returncode != 0:
        return {"status": "error", "returncode": completed.returncode, "stderr": completed.stderr[-4000:]}
    # CLI 输出是缩进的多行 JSON：从首个 "{" 起整体解析。
    stdout = completed.stdout
    payload = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
    return {"status": "accounted", **{k: payload.get(k) for k in ("attempt_id", "recovery_revision", "request", "project_ledger")}}



# ---------------------------------------------------------------------------
# dispatch：VC-5 批次（真实 CLI compare／真实 builder／真实 CLI accept）
# ---------------------------------------------------------------------------


def _load_state(arguments: argparse.Namespace) -> dict[str, Any]:
    return _read(Path(arguments.root).resolve() / "chain-state.json")


def _builder_config(state: dict[str, Any], *, baseline: int, tag: str, reuse_from: str = "") -> Path:
    from tools.official_client_capture import codex_upgrade

    campaign_dir = Path(state["campaign_dir"])
    candidate_id = str(state["candidate_id"])
    official = codex_upgrade._load_stage_result(campaign_dir, "capture-official")
    candidate = codex_upgrade._load_stage_result(campaign_dir, "capture-candidate", candidate_id)
    reuse_candidate_prefix: str | None = None
    if reuse_from:
        # 被复用基线的候选阶段（可能是另一份阶段结果：attempt-recovery 基线之前的基线）的逻辑路径前缀。
        reuse_index = _read(Path(reuse_from))
        reused_stage = codex_upgrade._load_stage_result(campaign_dir, "capture-candidate", candidate_id, _baseline=int(reuse_index["evaluation_baseline"]))
        reuse_candidate_prefix = str(reused_stage["assertion_context"]["evidence_prefix"])
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
        "reuse_candidate_evidence_prefix": reuse_candidate_prefix,
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
            if state.get("two_job"):
                command = _read_side_command(state, "compare", "--campaign-dir", str(campaign_dir), "--candidate-id", candidate_id)
        elif kind == "assert":
            command = [
                sys.executable, builder,
                "--config", str(_builder_config(state, baseline=int(arguments.baseline), tag=arguments.tag, reuse_from=str(arguments.reuse_from or ""))),
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
            if state.get("two_job"):
                flag = Path(state["root"]) / ACCEPT_CRASH_FLAG
                if getattr(arguments, "crash_at", "") == "accept-completion":
                    flag.write_text("crash\n", encoding="utf-8")
                elif flag.exists():
                    flag.unlink()
                command = _read_side_command(state, *cli_arguments)
            elif getattr(arguments, "accept_wrapper", False):
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
        elif kind == "ar-run":
            # 恢复段补跑：真实 CLI capture-candidate run --attempt-recovery（包装脚本只 mock 外部环境依赖）；
            # 后继段（A1）再加 --rerun-failed --recovery-preview。崩溃开关走文件（命令逐字稳定）。
            flag = Path(state["root"]) / AR_CRASH_FLAG
            if getattr(arguments, "crash_at", "") == "segment-job":
                flag.write_text("crash\n", encoding="utf-8")
            elif flag.exists():
                flag.unlink()
            fail_flag = Path(state["root"]) / AR_FAIL_FLAG
            if getattr(arguments, "segment_fail", False):
                fail_flag.write_text("fail\n", encoding="utf-8")
            elif fail_flag.exists():
                fail_flag.unlink()
            command = [
                sys.executable, "-c", AR_ACTION_WRAPPER, str(Path(state["root"]) / "chain-state.json"),
                "capture-candidate", "run", "--campaign-dir", str(campaign_dir), "--candidate-id", candidate_id,
                "--candidate-purpose", str(state["identity"]["candidate_purpose"]), "--attempt-recovery", str(arguments.recovery_revision),
                "--acknowledge-live-requests",
            ]
            if getattr(arguments, "rerun_failed", False):
                command.extend(["--rerun-failed", "--recovery-preview", str(arguments.recovery_preview)])
            # 恢复段动作的 item 是被重采的 Job 本身（不是零请求后处理项）；动作输出绑定＝段 run-summary
            # （操作员自带声明，编译侧只为评估动作冻结绑定）。
            summary_relative = (
                Path("candidates") / candidate_id / "attempts" / str(state["attempt_id"]) / "recovery" / str(arguments.recovery_revision) / "attempt-recovery.json"
            ).as_posix()
            actions.append({
                "action_id": action_id, "operation": "VC-5:capture-candidate", "timeout_seconds": 900, "command": command,
                "item_ids": list(arguments.execute_jobs), "output_bindings": [summary_relative],
            })
            continue
        elif kind == "ar-seal":
            seal = state["ar_seal"]
            build_receipt = codex_upgrade._candidate_build_receipt_path(campaign_dir, candidate_id)
            command = [
                sys.executable, "-c", AR_SEAL_WRAPPER, str(Path(state["root"]) / "chain-state.json"),
                "capture-candidate", "seal", "--campaign-dir", str(campaign_dir), "--candidate-id", candidate_id,
                "--candidate-purpose", str(state["identity"]["candidate_purpose"]), "--attempt-id", str(state["attempt_id"]),
                "--attempt-recovery", str(state["recovery_revision"]), "--build-receipt", str(build_receipt),
                "--capture-manifest", seal["capture_manifest"], "--assertion-evidence-root", seal["assertion_evidence_root"],
                "--observed-profile-receipt", seal["observed_profile_receipt"],
            ]
            for spec in seal["client_evidence"]:
                command.extend(["--client-evidence", spec])
            actions.append({"action_id": action_id, "operation": "VC-5:candidate-seal", "timeout_seconds": 900, "command": command, "item_ids": ["candidate-seal"]})
            continue
        else:
            raise SystemExit(f"未知动作：{kind}")
        actions.append({"action_id": action_id, "operation": f"VC-5:{kind}", "timeout_seconds": 900, "command": command, "item_ids": [item_ids[kind]]})
    return actions


def _build_plan(state: dict[str, Any], arguments: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    """VC-5 action plan（dispatch 与 seal 预演共用同一份：预演门禁按 action_id／item_ids／命令摘要匹配）。"""

    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

    actions = _actions(state, arguments)
    execute_item_ids = sorted(item for action in actions for item in action["item_ids"])
    plan_doc = {
        "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
        "execute_item_ids": execute_item_ids,
        # 已封存 Job 默认为复用项；恢复段补跑的 Job 是执行项，不能同时出现在 reuse 集合。
        "reuse_item_ids": sorted((set(state["job_ids"]) | set(arguments.reuse_items or [])) - set(execute_item_ids)),
        "actions": actions,
    }
    return plan_doc, Path(state["root"]) / f"plans-{arguments.tag}" / "vc-5.json"


def stage_ar_rehearse(arguments: argparse.Namespace) -> dict[str, Any]:
    """正式 seal 批次派发前的隔离预演（Framework §5.1.2）：真实 rehearse-candidate-seal 在 OverlayFS 副本上
    执行与 dispatch 完全相同的 action plan（段 seal 包装脚本内 preview→approve），收据落在正式 Campaign 目录。
    只能在 Linux root 上执行；upper 目录放在数据根之外。"""

    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_seal_rehearsal as seal_rehearsal

    state = _load_state(arguments)
    campaign_dir = Path(state["campaign_dir"])
    plan_doc, plan_path = _build_plan(state, arguments)
    _write(plan_path, plan_doc)
    upper_root = Path(arguments.upper_root) if arguments.upper_root else Path(state["root"]) / f"rehearsal-upper-{arguments.tag}"
    upper_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_command = _read_side_command(state, "status", "--campaign-dir", str(campaign_dir.resolve()))
    result = seal_rehearsal.rehearse(
        campaign_dir=campaign_dir,
        candidate_id=CANDIDATE_ID,
        attempt_id=str(state["attempt_id"]),
        actions=plan_doc["actions"],
        data_root=Path(state["data"]).resolve(),
        alias_roots=[],
        upper_root=upper_root.resolve(),
        status_command=status_command,
    )
    return {
        "status": result["status"],
        "receipt_path": result["receipt_path"],
        "actions_sha256": result["actions_sha256"],
        "lower_unchanged": result["lower_unchanged"],
        "final_status": result.get("final_status"),
        "results": [
            {key: item.get(key) for key in ("action_id", "status", "returncode", "legal_stop", "stderr_tail", "stdout_tail")}
            for item in result.get("results", [])
        ],
    }


def stage_dispatch(arguments: argparse.Namespace) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade
    from tools.official_client_capture import codex_upgrade_supervisor as supervisor
    from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

    state = _load_state(arguments)
    campaign_dir = Path(state["campaign_dir"])
    plan_doc, plan_path = _build_plan(state, arguments)
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
    elif arguments.crash_at == "after-binding":
        # R2 的 attempt-recovery 变体：动作（段 run）成功、动作输出绑定已写、父 run 终态未写时 SIGKILL。
        patches.append(_crash_after_patch(supervisor, "write_action_output_binding"))
    elif arguments.crash_at in {"accept-completion", "segment-job"}:
        pass  # C1／A1：崩溃点在动作子进程内（见 _actions），父监督器不打补丁。
    elif arguments.crash_at:
        raise SystemExit(f"dispatch 不支持崩溃点：{arguments.crash_at}")
    if getattr(arguments, "skip_seal_rehearsal_gate", False):
        # 仅本地探路（macOS 无 OverlayFS 不能真实预演）：真机用例必须先 ar-rehearse 拿到预演收据。
        patches.append(mock.patch.object(supervisor, "_validate_vc5_seal_rehearsal_gate"))
    for patcher in patches:
        patcher.start()
    result, returncode = codex_upgrade.compile_and_run_vc_batch(namespace)
    if returncode == 0 and (Path(state["root"]) / "full-chain-import.json").is_file():
        from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import (
            _record_duplicate_check, assert_duplicate_dispatch_unchanged,
        )

        _record_duplicate_check(state["root"], arguments.tag, *assert_duplicate_dispatch_unchanged(namespace))
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
    if returncode != 0:
        summary["diagnostics"] = [
            {key: document.get(key) for key in ("action_id", "message", "failure_class")}
            for path in Path(result["campaign_run"]["run_dir"]).glob("action-diagnostics/*.json")
            for document in [_read(path)]
        ]
        # 失败时仅展示已落盘的诊断，不能让未封存的 accept 再抛错并掩盖原始原因。
        _, acceptance_path = codex_upgrade._stage_path(campaign_dir, "accept", str(state["candidate_id"]))
        summary["acceptance"] = _read(acceptance_path) if acceptance_path.is_file() else None
    return summary


# ---------------------------------------------------------------------------
# reconcile / recover / epoch
# ---------------------------------------------------------------------------


def stage_reconcile(arguments: argparse.Namespace) -> dict[str, Any]:
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    state = _load_state(arguments)
    return reconciler.reconcile_supervisor_run(Path(arguments.run_dir), Path(state["campaign_dir"]))


def stage_reconcile_ar(arguments: argparse.Namespace) -> dict[str, Any]:
    """真实 reconcile-attempt --recovery-revision（段中断／失败对账，写 attempt_recovery_failed、总账入账、恢复预览）；
    ``--approve`` 再以预览摘要批准（B0：后继段开段前的人工批准）。段摘要的权限收口重放需同一 runs 别名注入。"""

    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    state = _load_state(arguments)
    campaign_dir = Path(state["campaign_dir"])
    if True:  # 环境替身由 main 统一套用
        reconciled = reconciler.reconcile_attempt(campaign_dir, str(state["attempt_id"]), recovery_revision=str(arguments.recovery_revision))
        result: dict[str, Any] = {
            "status": reconciled["status"],
            "recovery_revision": reconciled["recovery_revision"],
            "root_cause": reconciled["root_cause"],
            "jobs": reconciled["jobs"],
            "decision": reconciled["decision"],
            "ledger_events": [event.get("event_id") for event in reconciled.get("ledger_events", [])],
            "recovery_preview_path": reconciled.get("recovery_preview_path"),
            "review_sha256": (reconciled.get("recovery_preview") or {}).get("review_sha256"),
            "next_command": reconciled.get("next_command"),
        }
        if arguments.approve and reconciled["status"] == reconciler.DECISION_RECOVERABLE:
            approval = reconciler.approve_recovery_preview(
                campaign_dir, str(state["attempt_id"]), approve_sha256=str(result["review_sha256"]), recovery_revision=str(arguments.recovery_revision)
            )
            result["approval_sha256"] = approval["approved_sha256"]
            # 消费批准（recovery_required → recovery_authorized → active），后继段批次才能编译派发。
            authorized = reconciler.authorize_recovery_preview(
                campaign_dir, str(state["attempt_id"]), Path(str(result["recovery_preview_path"])), recovery_revision=str(arguments.recovery_revision)
            )
            result["authorized"] = authorized["status"]
            result["timing_recovery_event"] = authorized.get("timing_recovery_event")
    return result


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
    init.add_argument("--full-chain", action="store_true", help="R13：真实复用导入及三批分类，供连续链调用")
    dispatch = subparsers.add_parser("dispatch")
    dispatch.add_argument("--sequence", type=int, default=None)
    dispatch.add_argument("--accept-wrapper", action="store_true")
    dispatch.add_argument("--recovery-revision", default=None)
    dispatch.add_argument("--execute-jobs", nargs="*", default=[])
    dispatch.add_argument("--skip-seal-rehearsal-gate", action="store_true", help="仅本地探路：macOS 无 OverlayFS，跳过 seal 预演门禁")
    dispatch.add_argument("--rerun-failed", action="store_true", help="A1：开失败段的后继段")
    dispatch.add_argument("--segment-fail", action="store_true", help="R2 变体：恢复段 Job 真实失败（段 status=failed）")
    dispatch.add_argument("--recovery-preview", default="")
    reconcile_ar = subparsers.add_parser("reconcile-ar")
    reconcile_ar.add_argument("--recovery-revision", required=True)
    reconcile_ar.add_argument("--approve", action="store_true")
    ar_rehearse = subparsers.add_parser("ar-rehearse")
    ar_rehearse.add_argument("--upper-root", default="")
    ar_rehearse.add_argument("--recovery-revision", default=None)
    ar_rehearse.add_argument("--execute-jobs", nargs="*", default=[])
    ar_rehearse.add_argument("--accept-wrapper", action="store_true")
    ar_rehearse.add_argument("--crash-at", default="")
    init_ar = subparsers.add_parser("init-ar")
    init_ar.add_argument("--campaign-id", default="upgrade-0154-eval-chain")
    init_ar.add_argument("--candidate-surface", default="other")
    init_ar.add_argument("--candidate-h1-method", default="POST", help="Job-B 的 H1 relay 方法；非 POST 使 SPEC-H1-001 在 b0 失败（两 Job 同在 failure-scope）")
    init_ar.add_argument("--spec-path", default="")
    init_ar.add_argument("--no-candidate-identity", action="store_true")
    ar_prepare = subparsers.add_parser("ar-prepare")
    ar_prepare.add_argument("--recovery-revision", required=True)
    ar_prepare.add_argument("--baseline", type=int, required=True)
    subparsers.add_parser("ar-account")
    for batch_parser in (dispatch, ar_rehearse):
        batch_parser.add_argument("--tag", required=True)
        batch_parser.add_argument("--actions", nargs="+", required=True, choices=("compare", "assert", "accept", "ar-run", "ar-seal"))
        batch_parser.add_argument("--baseline", type=int, default=0)
        batch_parser.add_argument("--reuse-from", default="")
        batch_parser.add_argument("--authority", default="none")
        batch_parser.add_argument("--reuse-items", nargs="*", default=[])
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
    subparsers.add_parser("deliver-full")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    _assert_tree_binding()
    arguments = _parser().parse_args(argv)
    # M2 两 Job 链：驱动进程内一切读取增量封存结果的路径（builder 配置、门禁收据、恢复裁定、批次编译、
    # 段对账…）都要重放恢复段的 v3 权限收口，统一套同一环境替身（runs 别名注入）；init 阶段尚无状态。
    state_path = Path(arguments.root).resolve() / "chain-state.json"
    if arguments.stage not in {"init", "init-ar"} and state_path.is_file() and _read(state_path).get("two_job"):
        with attempt_recovery_environment_patches(str(state_path)):
            return _run_stage(arguments)
    return _run_stage(arguments)


def _run_stage(arguments: argparse.Namespace) -> int:
    from tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain import deliver_full_chain

    stages = {
        "init": stage_init,
        "dispatch": stage_dispatch,
        "reconcile": stage_reconcile,
        "recover": stage_recover,
        "epoch": stage_epoch,
        "gate": stage_gate,
        "accept-direct": stage_accept_direct,
        "identity": stage_identity,
        "init-ar": stage_init_attempt_recovery,
        "ar-prepare": stage_ar_prepare,
        "ar-rehearse": stage_ar_rehearse,
        "ar-account": stage_ar_account,
        "reconcile-ar": stage_reconcile_ar,
        "deliver-full": deliver_full_chain,
    }
    result = stages[arguments.stage](arguments)
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
