"""Claude 2.1.226 录制来源的连续控制回归驱动。

VC-0 建立前即冻结双侧场景、完整 Active 控制投影与候选运行参数。VC-0/1 复用真实录制链，后段只能
接续同一个 Campaign、Store 与两本账；审核输入属于明确的夹具来源，阶段判定、画像投影、封存和监督批次
仍由产品代码完成。本驱动不授予历史版本语义认证，也不允许把部分执行包装成 VC-0→VC-6 通过。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as vc1

ACTIVATION_MODULE = "tools.official_client_capture.tests.claude_recorded_full_validation_driver"
FULL_SCHEMA = "claude-recorded-full-validation-only/v1"
REVIEWER = "录制控制回归审核来源"


def _write_bytes(path: Path, raw: bytes) -> Path:
    """控制输入也只写一次；规范字节由正式序列化器生成，不能写漂亮 JSON 后绕过摘要判定。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as handle:
        handle.write(raw)
    path.chmod(0o600)
    return path


def _baseline_values(value: Any) -> Any:
    """只投影身份的精确值与版本条件串；保留业务合同内容，不做全文件字符串替换。"""

    if isinstance(value, dict):
        return {key: _baseline_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_baseline_values(item) for item in value]
    if value == source.VERSION:
        return vc1.BASELINE
    if value == "version=" + source.VERSION:
        return "version=" + vc1.BASELINE
    return value


def _inputs(data: Path) -> dict[str, Any]:
    """2.1.226 只读 Active → 2.1.220 控制基线；来源和投影分开绑定，绝不冒称真实历史制品。"""

    from tools.official_client_capture import claude_upgrade_profile as profile
    from tools.official_client_capture import claude_upgrade_release as release
    from tools.official_client_capture import claude_upgrade_wire_projection as wire
    from tools.official_client_capture.tests import test_claude_upgrade_profile as profile_fixture

    inputs = vc1._inputs(data)
    root = data / "control/fixture-active-projection"
    original_profile = source.read(profile_fixture.ACTIVE_PROFILE)
    projected_profile = _baseline_values(original_profile)
    profile_path = _write_bytes(root / "active-profile.json", profile.profile_bytes(projected_profile))
    profile_digest = source.binding(profile_path)["sha256"]
    projected_wire = _baseline_values(source.read(profile_fixture.ACTIVE_WIRE))
    projected_wire["identity"]["profile_digest"] = profile_digest
    wire_path = _write_bytes(root / "active-wire.json", wire.canonical_bytes(projected_wire))
    wire_digest = source.binding(wire_path)["sha256"]
    catalog = source.read(profile_fixture.ACTIVE_CATALOG)
    selector = catalog["selectors"]["production_active"]
    active = copy.deepcopy(next(row for row in catalog["releases"] if row["release_sha256"] == selector["release_sha256"]))
    baseline_digest = inputs["bindings"]["baseline"]["sha256"]
    release_digest, bundle_digest = wire.build_release_identity(
        version=vc1.BASELINE, platform=projected_profile["identity"]["platform"], profile_digest=profile_digest,
        wire_digest=wire_digest, model_catalog_digest=projected_wire["identity"]["model_capability_catalog_digest"],
        required_rules_manifest_sha256=baseline_digest, required_rule_count=len(projected_profile["rules"]),
    )
    active.update(version=vc1.BASELINE, profile={"path": "fixture-only/active-profile.json", "sha256": profile_digest},
                  wire={"path": "fixture-only/active-wire.json", "sha256": wire_digest},
                  required_rules_manifest_sha256=baseline_digest, release_sha256=release_digest, bundle_sha256=bundle_digest)
    catalog["source"] = "fixture_only:2.1.226-active-to-2.1.220-control-projection"
    catalog["releases"] = [active]
    for name in ("production_active", "validation_candidate"):
        catalog["selectors"][name] = {"kind": "release", "release_sha256": release_digest, "changeset": "fixture-only-control-baseline"}
    catalog_path = source.write(root / "active-release-catalog.json", catalog)
    policy, original_policy_binding = release.active_projection_policy(source.VERSION)
    policy["version"] = vc1.BASELINE
    policy_path = source.write(root / "active-projection-policy.json", policy)
    # 输入源改变也必须通过正式投影策略加载器；后续不替换其任何判据或投影计算。
    wire.load_projection_policy(policy_path)
    anchors_path = source.write(root / "static-baseline-anchors.json", {
        "fixture_only": True, "purpose": "control_input_projection", "baseline_version": vc1.BASELINE,
        "static_source_scope": "VC-1 seal 的已声明 inventory/containment/dispositions 输入；不声称当前 ARM64 静态重分析",
    })
    reachability_path = source.write(root / "static-baseline-reachability.json", {
        "fixture_only": True, "purpose": "control_input_projection", "baseline_version": vc1.BASELINE,
        "static_source_scope": "控制回归占位来源，不是历史 307 sink 词法结论或正式版本认证",
    })
    paths = {"active_release_catalog": catalog_path, "active_profile": profile_path, "active_wire": wire_path,
             "active_projection_policy": policy_path, "static_baseline_anchors": anchors_path,
             "static_baseline_reachability": reachability_path}
    inputs["bindings"].update({key: source.binding(path) for key, path in paths.items()})
    inputs["full_control_inputs"] = True
    origin = source.write(root / "projection-origin.json", {
        "fixture_only": True, "purpose": "control_input_projection", "baseline_version": vc1.BASELINE,
        "target_version": source.VERSION, "source_version": source.VERSION, "formal_version_semantics_certified": False,
        "originals": {"profile": source.binding(profile_fixture.ACTIVE_PROFILE), "wire": source.binding(profile_fixture.ACTIVE_WIRE),
                      "catalog": source.binding(profile_fixture.ACTIVE_CATALOG), "projection_policy": original_policy_binding},
        "projected": {key: inputs["bindings"][key] for key in paths},
        "method": "身份精确值投影，规范化画像/Wire 字节，按正式算法重算 Release/Bundle；业务规则不删减。",
    })
    inputs["bindings"]["active_projection_origin"] = source.binding(origin)
    return inputs


@contextlib.contextmanager
def installed(state_path: Path, digest: str) -> Iterator[Any]:
    """仅适配基线投影的输入路径；官方来源、监督器、Store 与阶段门禁沿用真实录制链。"""

    from tools.official_client_capture import claude_upgrade_release as release
    from tools.official_client_capture import claude_upgrade_wire_projection as wire

    state = source.load_state(state_path, digest)
    if state["inputs"].get("full_control_inputs") is not True:
        raise replay.ReplayError("完整链要求在 VC-0 前冻结完整控制输入")
    original = release.active_projection_policy

    def active_projection_policy(version: str):
        if version != vc1.BASELINE:
            return original(version)
        path = source.bound(state["inputs"]["bindings"]["active_projection_policy"], root=Path(state["data_root"]))
        policy = wire.load_projection_policy(path)
        if policy["version"] != version:
            raise replay.ReplayError("控制基线投影版本漂移")
        return policy, {"path": str(path), "sha256": source.binding(path)["sha256"]}

    with vc1.installed(state_path, digest) as adapter, mock.patch.object(release, "active_projection_policy", active_projection_policy):
        yield adapter


def activate() -> None:
    """子监督动作安装相同的显式输入来源，不能回落到未登记来源或另外生成一份 Campaign。"""

    vc1.activate(installer=installed)


def _latest_classification(campaign: Path) -> dict[str, Any]:
    """从真实只写一次的 revision 重验文件与门禁，不用伪造成功返回替代分类。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_classify as classify

    manifest = upgrade.load_campaign_manifest(campaign)
    revisions = classify.list_revisions(campaign, manifest["campaign_id"])
    if not revisions:
        raise replay.ReplayError("真实 classify 批次没有发布 revision")
    revision = revisions[-1][0]
    receipt, files = classify.load_revision(campaign, manifest["campaign_id"], revision)
    inputs = classify.collect_inputs(campaign, manifest)
    gate = classify.evaluate_revision(inputs, files)
    return {"revision": revision, "draft_dir": campaign / classify.DRAFT_ROOT / revision, "gate": gate,
            "review_sha256": classify.review_sha256(inputs, revision, receipt, gate)}


def _latest_prepare(campaign: Path) -> dict[str, Any]:
    """画像准备同样重读正式 revision、完整 Active 与 VC-2 Store 事实。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_profile as profile

    manifest = upgrade.load_campaign_manifest(campaign)
    revisions = profile.list_revisions(campaign, manifest["campaign_id"])
    if not revisions:
        raise replay.ReplayError("真实 prepare-profile 批次没有发布 revision")
    revision = revisions[-1][0]
    receipt, files = profile.load_revision(campaign, manifest["campaign_id"], revision)
    catalog, active = profile._bound_active_paths(campaign, receipt)
    inputs = profile.collect_inputs(campaign, manifest, catalog, active, allowed_status=profile._PREPARE_STATUSES)
    gate = profile.evaluate_prepare(inputs, files)
    return {"revision": revision, "draft_dir": campaign / profile.PREPARE_DRAFT_ROOT / revision, "gate": gate,
            "review_sha256": profile.review_sha256(inputs, revision, receipt, gate)}


def classify_and_prepare(data: Path, campaign: Path) -> dict[str, Any]:
    """同一 Campaign 上实际跑 VC-2/3；审核来源只构造输入，所有批准与 checkpoint 由真实命令产生。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_classify as classify
    from tools.official_client_capture import claude_upgrade_profile as profile
    from tools.official_client_capture.tests import test_claude_upgrade_classify as classify_fixture
    from tools.official_client_capture.tests import test_claude_upgrade_profile as profile_fixture
    from tools.official_client_capture.tests import claude_wire_fixture as wire_fixture

    batches = []
    batches.append(vc1._batch(data, campaign, "vc2-01-classify-draft", ["classify", "--campaign-dir", str(campaign)], phase="VC-2"))
    draft = _latest_classification(campaign)
    reviewer = classify_fixture._ClassifyCase(methodName="runTest")
    docs = reviewer.resolved_documents(Path(draft["draft_dir"]))
    for row in docs["rule_migration"]["rules"]:
        row["rationale"] = "fixture_only：同一份录制官方证据用于控制流程回归，全规则继承不构成版本语义认证。"
    review_input = reviewer.write_documents(docs, data / "control/review-inputs/vc2")
    batches.append(vc1._batch(data, campaign, "vc2-02-classify-revision", [
        "classify", "--campaign-dir", str(campaign), "--from-revision", draft["revision"], "--revision-input", str(review_input),
    ], phase="VC-2"))
    revised = _latest_classification(campaign)
    if revised["gate"]["status"] != "closed":
        raise replay.ReplayError(f"真实 VC-2 门禁未闭合：{revised['gate']}")
    batches.append(vc1._batch(data, campaign, "vc2-03-classify-approve", [
        "classify", "--campaign-dir", str(campaign), "--approve-revision", revised["revision"],
        "--approve-review-sha256", revised["review_sha256"],
    ], phase="VC-2"))
    manifest = upgrade.load_campaign_manifest(campaign)
    stage = classify.load_stage_result(campaign, manifest)
    if stage["affected_rules"] or len(stage["inherited_rules"]) != 40:
        raise replay.ReplayError("控制审核输入未形成明确的 40 规则继承分母")
    batches.append(vc1._batch(data, campaign, "vc3-01-prepare-draft", ["prepare-profile", "--campaign-dir", str(campaign)], phase="VC-3"))
    draft = _latest_prepare(campaign)
    preparer = profile_fixture._ProfileCase(methodName="runTest")
    docs = preparer.resolved_prepare(campaign, Path(draft["draft_dir"]))
    docs["projection"] = wire_fixture.single_model_policy(docs["projection"])
    for name in ("boundary_assertions", "persona_derivation", "compatibility_boundary"):
        for row in docs["joint_inputs"][name]:
            row["facts"].update(fixture_only=True, purpose="control_regression_input")
    review_input = preparer.write_prepare(docs, data / "control/review-inputs/vc3")
    batches.append(vc1._batch(data, campaign, "vc3-02-prepare-revision", [
        "prepare-profile", "--campaign-dir", str(campaign), "--from-revision", draft["revision"], "--revision-input", str(review_input),
    ], phase="VC-3"))
    revised = _latest_prepare(campaign)
    if revised["gate"]["status"] != "closed":
        raise replay.ReplayError(f"真实 VC-3 准备门禁未闭合：{revised['gate']}")
    batches.append(vc1._batch(data, campaign, "vc3-03-prepare-approve", [
        "prepare-profile", "--campaign-dir", str(campaign), "--approve-revision", revised["revision"],
        "--approve-review-sha256", revised["review_sha256"], "--approved-by", REVIEWER,
    ], phase="VC-3"))
    batches.append(vc1._batch(data, campaign, "vc3-04-stage-preview", ["stage-profile", "--campaign-dir", str(campaign)], phase="VC-3"))
    previews = list((campaign / profile.STAGE_ROOT).glob("*/" + profile.STAGE_PREVIEW_FILENAME))
    if len(previews) != 1:
        raise replay.ReplayError("真实 VC-3 暂存没有形成唯一联合预览")
    preview = source.read(previews[0])
    batches.append(vc1._batch(data, campaign, "vc3-05-stage-approve", [
        "stage-profile", "--campaign-dir", str(campaign), "--approve-joint-sha256", preview["joint_sha256"], "--approved-by", REVIEWER,
    ], phase="VC-3"))
    profile_stage = profile.load_stage_result(campaign, manifest)
    upgrade._replay_vc_checkpoint(campaign, upgrade._vc_campaign_plan(campaign, manifest), "VC-3")
    return {"batches": batches, "classification": source.binding(campaign / classify.STAGE_RESULT_RELATIVE),
            "profile": source.binding(campaign / profile.STAGE_RESULT_RELATIVE), "profile_store": profile_stage["store"]}


def run_through_profile(state_path: Path, digest: str) -> dict[str, Any]:
    """前半段诊断入口；即使 VC-0～VC-3 全部完成也固定不登记完整链通过。"""

    started = time.monotonic()
    first = vc1.run_chain(state_path, digest, installer=installed)
    with installed(state_path, digest):
        continuation = vc1.load_continuation(Path(first["continuation"]["path"]), first["continuation"]["sha256"])
        data, campaign, state = continuation["data"], continuation["campaign"], continuation["state"]
        next_stages = classify_and_prepare(data, campaign)
        report = {"schema_version": FULL_SCHEMA, "fixture_only": True, "purpose": "control_regression_progress",
                  "status": "unverified", "registered_chains": [], "scope": "VC-0→VC-3",
                  "campaign": str(campaign), "store_campaign_id": continuation["manifest"]["campaign_id"],
                  "first": source.binding(data / "control/recorded-chain-result.json"), "next_stages": next_stages,
                  "source_state": source.binding(state_path), "elapsed_seconds": time.monotonic() - started,
                  "network_requests_this_run": 0, "full_vc0_vc6_certified": False,
                  "formal_version_semantics_certified": False,
                  "remaining_phases": ["VC-4", "VC-5", "VC-6"]}
    replay.assert_source_untouched(state["snapshot"])
    for path, expected in state["freeze"]["source_sentinel"].items():
        if replay._stat(Path(path)) != expected:
            raise replay.ReplayError("旧冻结来源元数据发生变化")
    source.write(data / "control/recorded-full-progress.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude 录制控制回归前半段诊断，不能签发完整链认证")
    parser.add_argument("--data", required=True)
    parser.add_argument("--recorded", required=True)
    parser.add_argument("--freeze", required=True)
    parser.add_argument("--parent-namespaces", required=True)
    parser.add_argument("--through", required=True, choices=("VC-3",))
    args = parser.parse_args(argv)
    try:
        report = vc1.namespace_main(args, inputs_builder=_inputs, chain_runner=run_through_profile, activation_module=ACTIVATION_MODULE)
    except BaseException as error:
        traceback.print_exc()
        print(json.dumps({"status": "unverified", "fixture_only": True, "registered_chains": [], "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
