"""Claude 2.1.226 录制来源的连续控制回归驱动。

VC-0 建立前即冻结双侧场景、完整 Active 控制投影与候选运行参数。VC-0/1 复用真实录制链，后段只能
接续同一个 Campaign、Store 与两本账；审核输入属于明确的夹具来源，阶段判定、画像投影、封存和监督批次
仍由产品代码完成。本驱动不授予历史版本语义认证，也不允许把部分执行包装成 VC-0→VC-6 通过。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import claude_recorded_candidate_source as candidate_source
from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as vc1

ACTIVATION_MODULE = "tools.official_client_capture.tests.claude_recorded_full_validation_driver"
FULL_SCHEMA = "claude-recorded-full-validation-only/v1"
REVIEWER = "录制控制回归审核来源"
CANDIDATE = "claude-recorded-cand-r1"
HOST = "arm64-recorded-control-fixture"
FULL_EXTRA_OFFICIAL_JOBS = ("official-generation-params", "official-agency-subagent")
# 完整来源选择登记的是原第二次及其确切收据；第一次失败原件和普通评估夹具选择不变。
FULL_SOURCE_SELECTION = {
    "source_id": "thinking-display-supplement-v1-94f7d00d704f",
    "probe_id": "v4-thinking-display-summarized",
    "attempt": "thinking-display-supplement-v1-94f7d00d704f/attempts/v4-thinking-display-summarized/attempt-002",
    "host_receipt": "thinking-display-supplement-v1-94f7d00d704f/runtime-receipts/v4-thinking-display-summarized-attempt-002.json",
    "manifest_sha256": "7c2c6ff5bc4ae357bb0e6f9ab2ff61092bc854fcdc6facae879aebb057d7f07d",
    "host_receipt_sha256": "c3c80dc19de9e0882008d28d3b23b9ff4902f020e56e59872f76710a5cbd9661",
}


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
    from tools.official_client_capture.tests import claude_wire_fixture as wire_fixture

    inputs = vc1._inputs(data, extra_official_jobs=FULL_EXTRA_OFFICIAL_JOBS)
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
    original_policy, original_policy_binding = release.active_projection_policy(source.VERSION)
    # 保留既有离线主模型语义，在 VC-0 之前冻结；完整 Active 原件及其三模型策略仍独立绑定。
    policy = wire_fixture.single_model_policy(original_policy)
    policy["version"] = vc1.BASELINE
    policy_path = source.write(root / "active-projection-policy.json", policy)
    review_policy_path = source.write(root / "review-projection-policy.json", release.machine_projection_policy(policy, source.VERSION))
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
             "static_baseline_reachability": reachability_path, "review_projection_policy": review_policy_path}
    inputs["bindings"].update({key: source.binding(path) for key, path in paths.items()})
    # 恢复链会合法聚合当前 Job；原完整 Job 单独绑定，完整性判据不能随聚合丢失。
    inputs["bindings"]["full_scenarios"] = inputs["bindings"]["scenarios"]
    inputs["full_control_inputs"] = True
    inputs["source_selections"] = [copy.deepcopy(FULL_SOURCE_SELECTION)]
    origin = source.write(root / "projection-origin.json", {
        "fixture_only": True, "purpose": "control_input_projection", "baseline_version": vc1.BASELINE,
        "target_version": source.VERSION, "source_version": source.VERSION, "formal_version_semantics_certified": False,
        "originals": {"profile": source.binding(profile_fixture.ACTIVE_PROFILE), "wire": source.binding(profile_fixture.ACTIVE_WIRE),
                      "catalog": source.binding(profile_fixture.ACTIVE_CATALOG), "projection_policy": original_policy_binding},
        "projected": {key: inputs["bindings"][key] for key in paths},
        "model_scope": {"original_models": original_policy["models"]["ordered"], "fixture_models": policy["models"]["ordered"],
                        "original_required_probe_count": len(wire.required_probes(original_policy)),
                        "fixture_required_probe_count": len(wire.required_probes(policy)),
                        "formal_model_scope_certified": False},
        "method": "身份精确值投影，按正式算法重算 Release/Bundle；业务规则不删减。原有主模型夹具审核策略在 VC-0 前单独冻结，不认证完整 Active 三模型语义。",
    })
    inputs["bindings"]["active_projection_origin"] = source.binding(origin)
    validate_frozen_inputs(data, inputs)
    return inputs


def validate_frozen_inputs(data: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    """零阶段运行地核完整 Job、双侧实际 probe 闭集及唯一冻结审核策略；不替代正式投影判定。"""

    from tools.official_client_capture import claude_upgrade_plan as plan
    from tools.official_client_capture import claude_upgrade_release as release
    from tools.official_client_capture import claude_upgrade_wire_projection as wire
    from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_fixture

    bindings = inputs["bindings"]
    active = wire.load_projection_policy(source.bound(bindings["active_projection_policy"], root=data))
    review = wire.load_projection_policy(source.bound(bindings["review_projection_policy"], root=data))
    if (active["version"] != vc1.BASELINE or review != release.machine_projection_policy(active, source.VERSION)
            or inputs.get("source_selections") != [FULL_SOURCE_SELECTION]):
        raise replay.ReplayError("完整链冻结审核策略或显式来源选择漂移")
    original = source.read(source.bound(bindings["full_scenarios"], root=data))
    template = source.read(plan.scenario_manifest_path("2.1.285"))
    official_ids = set(seal_fixture.SEAL_JOBS) | set(FULL_EXTRA_OFFICIAL_JOBS)
    official_probes = {step["probe_id"] for job in template["capture_jobs"] if job["id"] in official_ids for step in job["steps"]}
    candidate_ids = {job["id"] for job in template["capture_jobs"] if job["phase"] == "candidate"
                     and all(step["probe_id"] in official_probes for step in job["steps"])}
    for side, selected in (("official", official_ids), ("candidate", candidate_ids)):
        expected = {job["id"]: [step["probe_id"] for step in job["steps"]]
                    for job in template["capture_jobs"] if job["phase"] == side and job["id"] in selected}
        actual = {job["id"]: [step["probe_id"] for step in job["steps"]]
                  for job in original["capture_jobs"] if job["phase"] == side}
        if actual != expected:
            raise replay.ReplayError(f"{side} 完整链 Job 未闭合，禁止删除 Job 或拆步骤")
    current = source.read(source.bound(bindings["scenarios"], root=data))
    required = set(wire.required_probes(review))
    for side, key in (("official", "jobs"), ("candidate", "candidate_jobs")):
        jobs = {job["id"]: [step["probe_id"] for step in job["steps"]] for job in current["capture_jobs"] if job["phase"] == side}
        if jobs != inputs[key]:
            raise replay.ReplayError(f"{side} 冻结场景与 Job 映射漂移")
        missing = sorted(required - {probe for probes in jobs.values() for probe in probes})
        if missing:
            raise replay.ReplayError(f"{side} 冻结审核策略缺少正式 required probes：{missing}")
    return review


@contextlib.contextmanager
def installed(state_path: Path, digest: str) -> Iterator[Any]:
    """仅适配基线投影的输入路径；官方来源、监督器、Store 与阶段门禁沿用真实录制链。"""

    from tools.official_client_capture import claude_upgrade_release as release
    from tools.official_client_capture import claude_upgrade_wire_projection as wire

    state = source.load_state(state_path, digest)
    if state["inputs"].get("full_control_inputs") is not True:
        raise replay.ReplayError("完整链要求在 VC-0 前冻结完整控制输入")
    validate_frozen_inputs(Path(state["data_root"]), state["inputs"])
    original = release.active_projection_policy

    def active_projection_policy(version: str):
        if version != vc1.BASELINE:
            return original(version)
        path = source.bound(state["inputs"]["bindings"]["active_projection_policy"], root=Path(state["data_root"]))
        policy = wire.load_projection_policy(path)
        if policy["version"] != version:
            raise replay.ReplayError("控制基线投影版本漂移")
        return policy, {"path": str(path), "sha256": source.binding(path)["sha256"]}

    with vc1.installed(state_path, digest, adapter_factory=candidate_source.SourceAdapter) as adapter, \
            mock.patch.object(release, "active_projection_policy", active_projection_policy), runtime_sources(state):
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
    state = source.load_state(Path(os.environ[source.STATE_ENV]), os.environ[source.DIGEST_ENV])
    if Path(state["data_root"]) != data:
        raise replay.ReplayError("审核策略的来源状态不属于本次数据根")
    frozen_review = validate_frozen_inputs(data, state["inputs"])
    if source.read(Path(draft["draft_dir"]) / profile.PREPARE_FILES["projection"]) != frozen_review:
        raise replay.ReplayError("机器审核草案与 VC-0 前冻结策略不同")
    preparer = profile_fixture._ProfileCase(methodName="runTest")
    docs = preparer.resolved_prepare(campaign, Path(draft["draft_dir"]))
    if docs["projection"] != frozen_review:
        raise replay.ReplayError("审核来源改变了 VC-0 前冻结投影策略")
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


def _direct(argv: list[Any]) -> tuple[int, dict[str, Any], str]:
    """只借 CLI 输出捕获；真实命令的诊断码、事实和副作用原样保留。"""

    from tools.official_client_capture.tests import test_claude_upgrade_classify as cli_fixture

    code, result, stderr = cli_fixture.run_main([str(item) for item in argv])
    if code not in {0, 2} or not isinstance(result, dict):
        raise replay.ReplayError(f"真实命令失败：{argv[0]}，诊断码 {code}，{stderr}")
    return code, result, stderr


def _require_client_staging(data: Path) -> None:
    """完整链的 Mac 来源也受系统临时根约束；只能由外层隔离执行器明确统一 TMPDIR 和记录根。"""

    from tools.official_client_capture.tests import claude_client_receipt_fixture as clients

    data = data.resolve()
    root = clients.temporary_root(data)
    if not data.is_relative_to(root / "staging"):
        raise replay.ReplayError(f"完整链数据根须在实际临时根 {tempfile.gettempdir()} 的首层 <world>/staging 内")


class RecordedHostObservation:
    """明确登记的离线 Docker 观察源；宿主收据由正式生成器计算，不能补写旧 FakeHost 的不完整收据。

    只替换 Docker daemon 的 inspect/version 响应及容器文件可见性。nonce、镜像交叉核验、执行源摘要、
    收据规范字节和三项运行变量继续由正式生成器及签发器计算；独立来源文件明确说明这些不是生产观察。
    """

    def __init__(self, data: Path, configuration: dict[str, Any], build: dict[str, Any]) -> None:
        from tools.official_client_capture.tests import test_claude_upgrade_capture as fixture

        self.data, self.configuration, self.build = data, configuration, build
        self.host = fixture.FakeHost()
        containers = {configuration["capture_container"], configuration["synthetic_capture_container"]}
        if containers != set(fixture.CONTAINER_IDS):
            raise replay.ReplayError("宿主观察源只支持本链登记的两个采集容器")
        self.containers = {row["name"]: copy.deepcopy(row) for row in fixture.BASE_ENVIRONMENT["containers"]["containers"]}
        self.runtime_id = "sha256:" + "d" * 64

    def _network_file(self, container: str, name: str, raw: bytes) -> Path:
        path = self.data / "control/fixture-runtime-observation" / container / name
        if path.exists():
            source.bound(source.binding(path), root=self.data)
            if path.read_bytes() != raw:
                raise replay.ReplayError("离线 Docker 网络观察输入漂移")
            return path
        return _write_bytes(path, raw)

    def _docker_json(self, arguments: list[str]) -> Any:
        """只提供 producer 实际查询的闭集；未登记的 daemon 查询失败关闭。"""

        if len(arguments) == 2 and arguments[0] == "inspect" and arguments[1] in self.containers:
            container = arguments[1]
            row = self.containers[container]
            hosts = self._network_file(container, "hosts", b"127.0.0.1 localhost\n")
            resolv = self._network_file(container, "resolv.conf", b"nameserver 127.0.0.1\n")
            return [{"Id": row["id"], "Image": self.runtime_id,
                     "State": {"Running": True, "StartedAt": row["started_at"]},
                     "Config": {"Hostname": container}, "HostConfig": {"NetworkMode": row["network_mode"]},
                     "NetworkSettings": {"Networks": {}}, "HostsPath": str(hosts), "ResolvConfPath": str(resolv)}]
        if arguments == ["image", "inspect", self.runtime_id]:
            return [{"RepoDigests": [self.configuration["runtime_image"]]}]
        if arguments == ["version", "--format", "{{json .Server}}"]:
            return {"Version": "fixture-recorded-daemon", "Os": "linux", "Arch": "arm64"}
        raise replay.ReplayError(f"未登记的离线 Docker daemon 查询：{arguments}")

    def run(self, argv: Any, *, operation: str, timeout_seconds: float = 30, deadline: Any = None,
            heartbeat: Any = None, check: bool = True) -> subprocess.CompletedProcess:
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import runtime_host_receipt as producer

        command = [str(item) for item in argv]
        if len(command) > 1 and command[1].endswith("/runtime_host_receipt.py"):
            arguments = producer._build_parser().parse_args(command[2:])
            output, tool_root = arguments.output, arguments.capture_tool_root
            # 宿主签发器使用实际导入的仓库源码；容器执行副本由冻结 capture_tool_root 另行绑定。
            expected_tools = Path(capture.__file__).resolve().parent
            if (tool_root != expected_tools or Path(command[1]) != expected_tools / "runtime_host_receipt.py"
                    or arguments.runtime_image != self.configuration["runtime_image"]
                    or arguments.container not in self.containers or output.resolve() != output
                    or not output.is_relative_to(self.data / "evidence/campaigns")
                    or output.exists() or output.is_symlink()):
                raise replay.ReplayError("宿主收据观察命令与冻结工具、镜像、容器或输出坐标不符")
            with mock.patch.object(producer, "_docker_json", self._docker_json):
                receipt = producer.build_receipt(container=arguments.container, runtime_image=arguments.runtime_image,
                                                tool_root=tool_root, run_nonce=arguments.run_nonce)
            producer.secure_write_json(output, receipt)
            source.write(output.with_suffix(".observation-origin.json"), {
                "fixture_only": True, "purpose": "recorded_control_runtime_source", "source": "explicit_docker_observation",
                "architecture": "linux/arm64", "not_production_observation": True,
                "formal_version_semantics_certified": False, "receipt": source.binding(output),
                "producer": receipt["producer"], "capture_source_bundle": receipt["capture_source_bundle"],
                "container": receipt["container"], "docker_server": receipt["docker_server"],
            })
            return subprocess.CompletedProcess(argv, 0, "", "")
        if command[:4] == ["docker", "inspect", "--format", "{{json .Image}}"]:
            if command[4:] != [self.configuration.get("service_container")]:
                raise replay.ReplayError("服务镜像观察的容器与冻结坐标不符")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.build["image"]["image_id"]), "")
        if command[:5] == ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}"]:
            if command[5:] != [self.build["image"]["image_id"]]:
                raise replay.ReplayError("RepoDigests 观察的镜像与本链构建不符")
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.build["image"]["reference"]]), "")
        return self.host.run(argv, operation=operation, timeout_seconds=timeout_seconds,
                             deadline=deadline, heartbeat=heartbeat, check=check)


@contextlib.contextmanager
def runtime_sources(state: dict[str, Any]) -> Iterator[None]:
    """冻结候选形成后才安装其外部环境观察源；准入、Store、画像、selector 与 run/seal 判定不替换。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_capture as capture
    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate
    from tools.official_client_capture.tests import claude_client_receipt_fixture as clients
    from tools.official_client_capture.tests import claude_vc5_chain_fixture as observation_fixture
    from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as gateway_fixture
    from tools.official_client_capture.tests import claude_typed_vc6_fixture as delivery_fixture

    data = Path(state["data_root"])
    path = data / "control/full-runtime-source.json"
    if not path.exists():
        yield
        return
    source.bound(source.binding(path), root=data)
    config = source.read(path)
    if config.get("fixture_only") is not True or config.get("purpose") != "recorded_control_runtime_source":
        raise replay.ReplayError("候选环境来源不是明确的控制夹具")
    build_path = source.bound(config["build_receipt"], root=data)
    build = source.read(build_path)
    manifest = upgrade.load_campaign_manifest(build_path.parents[2])
    source.bound(config["profile_stage"], root=data)
    frozen = state["inputs"]["candidate_configuration"]
    if (build.get("candidate_id") != CANDIDATE or config["candidate_configuration"] != manifest["configuration"]
            or any(str(manifest["configuration"].get(name)) != value for name, value in frozen.items())):
        raise replay.ReplayError("外部候选运行观察与本链构建、冻结五变量不符")
    gateway = copy.deepcopy(gateway_fixture.BASE_GATEWAY)
    gateway["service"]["image_id"] = build["image"]["image_id"]
    fact = observation_fixture.activation_fact(build, source.VERSION)
    host = RecordedHostObservation(data, manifest["configuration"], build)

    def require_manifest(observed: dict[str, Any]) -> None:
        if observed.get("campaign_id") != manifest["campaign_id"] or observed.get("configuration") != manifest["configuration"]:
            raise replay.ReplayError("网关观察请求与冻结 Campaign 不符")

    def gateway_facts(observed: dict[str, Any], host_data_root: Path, *, deadline: Any, heartbeat: Any):
        del deadline, heartbeat
        require_manifest(observed)
        if host_data_root != data:
            raise replay.ReplayError("网关观察的数据根与本链不符")
        return copy.deepcopy(gateway)

    def activation(observed: dict[str, Any]):
        require_manifest(observed)
        return "/app/data/claude-egress-activation-fact.json", json.dumps(fact, sort_keys=True) + "\n"

    def host_facts():
        return {"host_id": HOST, "architecture": "linux/arm64"}

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(capture, "_run_probe_command", host.run))
        stack.enter_context(mock.patch.object(candidate, "_collect_gateway_facts", gateway_facts))
        stack.enter_context(mock.patch.object(candidate, "_read_activation_fact", activation))
        stack.enter_context(mock.patch.object(candidate, "host_facts", host_facts))
        stack.enter_context(clients.source_scope())
        delivery_control = data / "control/recorded-delivery-control.json"
        if delivery_control.exists():
            control = source.read(source.bound(source.binding(delivery_control), root=data))
            if control.get("fixture_only") is not True or control.get("formal_version_semantics_certified") is not False:
                raise replay.ReplayError("交付执行器来源未显式限定控制回归")
            stack.enter_context(delivery_fixture.source_scope(delivery_control))
        yield


def build_candidate(data: Path, campaign: Path) -> dict[str, Any]:
    """VC-4 的 Git/构建输入来自明确离线来源；冻结、构建收据、revision seal 与 checkpoint 全部走真实命令。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_profile as profile
    from tools.official_client_capture.tests import claude_candidate_build_fixture as fixture

    code, opened, _stderr = _direct(["revision-open", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--initial"])
    if code != 0 or opened.get("revision") != 1:
        raise replay.ReplayError("真实候选 initial revision 未建立")
    _code, preview, _stderr = _direct(["plan-candidate-gates", "--campaign-dir", campaign, "--candidate-id", CANDIDATE])
    builder = fixture.CandidateBuildFixture(data / "control/fixture-candidate-build", campaign, CANDIDATE, run_main=_direct)
    builder.build_source(preview=preview)
    builder.build_artifacts()
    builder.build_transition()
    builder.build_implementation_receipt(preview["requirements"], vc1.UPGRADE)
    plan_source = data / "control/candidate-plan-source"
    subprocess.run(["git", "clone", "-q", str(builder.source), str(plan_source)], check=True, capture_output=True, timeout=60)
    lifecycle = builder.lifecycle_dir.relative_to(builder.source)
    dispatched = plan_source / lifecycle / "gate-plan-dispatch.json"
    batches = [vc1._batch(data, campaign, "vc4-01-plan-gates", [
        "plan-candidate-gates", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE,
        "--candidate-source", str(plan_source), "--mapping", str(plan_source / lifecycle / "gate-mapping.json"), "--output", str(dispatched),
    ], phase="VC-4")]
    if dispatched.read_bytes() != builder.plan_path.read_bytes():
        raise replay.ReplayError("真实监督门禁计划与冻结源码里的计划不一致")
    binaries = builder.install_fake_commands(data / "control/fixture-build-bin")
    with mock.patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ.get("PATH", "")}):
        batches.append(vc1._batch(data, campaign, "vc4-02-record-build", builder.argv(), phase="VC-4"))
    manifest = upgrade.load_campaign_manifest(campaign)
    revision = upgrade._current_candidate_revision(campaign, manifest)
    if revision != 1:
        raise replay.ReplayError("候选构建未留在同一 initial revision")
    checkpoint, _payload = upgrade._replay_vc_checkpoint(campaign, upgrade._vc_campaign_plan(campaign, manifest), "VC-4", revision=revision)
    receipt = campaign / "candidates" / CANDIDATE / "build-receipt.json"
    source.write(data / "control/full-runtime-source.json", {
        "fixture_only": True, "purpose": "recorded_control_runtime_source", "build_receipt": source.binding(receipt),
        "profile_stage": source.binding(campaign / profile.STAGE_RESULT_RELATIVE), "candidate_configuration": manifest["configuration"],
        "formal_version_semantics_certified": False,
    })
    return {"batches": batches, "revision": revision, "candidate_id": CANDIDATE,
            "build_receipt": source.binding(receipt), "checkpoint": source.binding(checkpoint),
            "external_source": "fixture_only Git/二进制/前端/镜像/实现测试观察，不表示生产构建验收"}


def _client_inputs(data: Path, campaign: Path, attempt_root: Path, attempt: dict[str, Any]) -> dict[str, Any]:
    """外部 Mac/边界材料仅构造输入；Mac 导入、计账、附件验证与边界收据消费执行真实合同。"""

    from tools.official_client_capture import claude_client_receipt_contract as client_contract
    from tools.official_client_capture import claude_upgrade_boundary_checks as boundary
    from tools.official_client_capture.tests import claude_boundary_receipt_fixture as boundary_fixture
    from tools.official_client_capture.tests import claude_client_receipt_fixture as clients

    values = {name: attempt[name] for name in client_contract.COORDINATES}
    original = source.binding(attempt_root / "attempt.json")
    receipts, batches = {}, []
    for client in client_contract.CLIENTS:
        task, bundle, _capture = clients.material(attempt_root, client, values, issued_at_utc=attempt["completed_at_utc"])
        batches.append(vc1._batch(data, campaign, "vc5-client-" + client, [
            "capture-candidate", "client-evidence", "import", "--campaign-dir", str(campaign),
            "--candidate-id", CANDIDATE, "--attempt-id", attempt_root.name, "--client", client,
            "--client-task", str(task), "--bundle", str(bundle),
        ], phase="VC-5"))
        receipt = attempt_root / "client-evidence" / (client + ".json")
        client_contract.verify_client_receipt(attempt_root, client, receipt)
        receipts[client] = source.binding(receipt)
    for check in boundary.CHECK_IDS:
        receipt = boundary_fixture.write_boundary_receipt(attempt_root, check, mutate=lambda value: value["facts"].update(
            fixture_only=True, purpose="recorded_control_input", formal_version_semantics_certified=False,
        ))
        boundary.verify_boundary_receipt(attempt_root, check, receipt)
        receipts[check] = source.binding(receipt)
    if source.binding(attempt_root / "attempt.json") != original:
        raise replay.ReplayError("外部 Mac/边界输入改变了真实候选 attempt 原件")
    origin = source.write(data / "control/candidate-client-input-origin.json", {
        "fixture_only": True, "purpose": "recorded_control_input", "attempt": original, "receipts": receipts,
        "source_scope": "离线 Mac 应用/请求与边界材料；不表示实际 Mac 或 Codex 边界执行",
        "formal_version_semantics_certified": False,
    })
    return {"batches": batches, "origin": source.binding(origin), "receipts": receipts}


def capture_and_seal(data: Path, campaign: Path) -> dict[str, Any]:
    """同一 revision 上真实 run→客户端输入→预览→批准；完整 R/P 来源只能由监督动作投影复制。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_candidate_seal as seal

    batches = [vc1._batch(data, campaign, "vc5-01-candidate-run", [
        "capture-candidate", "run", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, "--acknowledge-live-requests",
    ], phase="VC-5")]
    attempts = sorted((campaign / "candidates" / CANDIDATE / "attempts").glob("*/attempt.json"))
    if len(attempts) != 1:
        raise replay.ReplayError("候选 run 未形成唯一真实 attempt")
    attempt_root = attempts[0].parent
    attempt = source.read(attempts[0])
    if attempt.get("status") != "awaiting_receipts" or upgrade._failed_job_ids(attempt.get("results")):
        raise replay.ReplayError("候选 run 未到真实 awaiting_receipts 停点或仍有失败 Job")
    clients = _client_inputs(data, campaign, attempt_root, attempt)
    argv = ["capture-candidate", "seal", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE,
            "--attempt-id", attempt_root.name]
    batches.append(vc1._batch(data, campaign, "vc5-02-candidate-seal-preview", argv, phase="VC-5"))
    previews = list((campaign / "candidates" / CANDIDATE / "seal").glob("*/seal-preview.json"))
    if len(previews) != 1:
        raise replay.ReplayError("候选 seal 未形成唯一真实预览")
    preview = source.read(previews[0])
    batches.append(vc1._batch(data, campaign, "candidate-seal", [
        *argv, "--approve-seal-sha256", preview["review_sha256"], "--approved-by", REVIEWER,
    ], phase="VC-5"))
    manifest = upgrade.load_campaign_manifest(campaign)
    stage_path, stage = seal.load_round_one_result(campaign, CANDIDATE, manifest=manifest)
    seal.sealed_attempt_context(campaign, stage, candidate_id=CANDIDATE)
    return {"batches": batches, "client_inputs": clients, "attempt": source.binding(attempts[0]),
            "stage": source.binding(stage_path), "store": stage["store"]}


def _gate_environment(root: Path, data: Path, attempt_id: str) -> dict[str, Any]:
    """外部观察由明确夹具提供；真实 ARM64 collect、finalize、replay 和前后等价比较一个都不替换。"""

    from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
    from tools.official_client_capture.tests import control_receipt_fixtures as fixture

    observation_root = data / "control/fixture-gate-observation"
    fixture.create_arm_receipt(observation_root, phase="gate_before", subject_id=attempt_id, prefix="source")
    observed = source.read(observation_root / "source-facts.json")
    moment = datetime.fromisoformat(observed["observed_at_utc"])
    filesystem = observed["root_filesystem"]
    block = 4096
    from types import SimpleNamespace
    statvfs = SimpleNamespace(f_frsize=block, f_bsize=block, f_blocks=filesystem["total_bytes"] // block,
                              f_bavail=filesystem["available_bytes"] // block,
                              f_bfree=(filesystem["total_bytes"] - filesystem["used_bytes"]) // block)

    def container(name: str, **_options: Any) -> dict[str, Any]:
        return copy.deepcopy(next(row for row in observed["containers"] if row["name"] == name))

    def tls(target: Any) -> dict[str, Any]:
        if any(observed["rust_tls_readiness"].get(name) != value for name, value in target.items()):
            raise replay.ReplayError("门禁外部 TLS 观察与正式探针目标不符")
        return copy.deepcopy(observed["rust_tls_readiness"])

    bindings = {}
    with mock.patch.object(arm.os, "statvfs", return_value=statvfs), \
            mock.patch.object(arm, "require_runtime_egress", return_value=copy.deepcopy(observed["runtime_egress"])), \
            mock.patch.object(arm, "_checked_runtime_egress", side_effect=lambda: (copy.deepcopy(observed["runtime_egress"]), moment)), \
            mock.patch.object(arm, "_container_observation", side_effect=container), \
            mock.patch.object(arm, "_rust_tls_readiness_observation", side_effect=tls):
        for role in ("before", "after"):
            facts, receipt = "environment/" + role + "-facts.json", "environment/" + role + "-receipt.json"
            arm.collect(root, facts, phase="gate_" + role, subject_id=attempt_id, rust_tls_codex_version="0.154.0")
            arm.finalize(root, facts, receipt)
            arm.replay(root, receipt)
            bindings[role] = {"path": receipt, "sha256": source.binding(root / receipt)["sha256"]}
    before = arm.replay(root, bindings["before"]["path"])
    after = arm.replay(root, bindings["after"]["path"])
    if not arm.receipts_equivalent(root, before, root, after):
        raise replay.ReplayError("真实门禁环境前后等价比较失败")
    source.write(observation_root / "origin.json", {"fixture_only": True, "purpose": "external_environment_observation",
                 "source": source.binding(observation_root / "source-facts.json"), "formal_version_semantics_certified": False})
    return bindings


def candidate_gate(data: Path, campaign: Path) -> tuple[Path, str]:
    """冻结输入形式的外部门禁观察；独立 finalizer 与 accept 消费端仍重放环境和正式 subject。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_accept as accept
    from tools.official_client_capture import claude_upgrade_baseline as baseline
    from tools.official_client_capture import claude_upgrade_compare as compare
    from tools.official_client_capture import claude_upgrade_store as store_module
    from tools.official_client_capture import codex_upgrade_gate_receipt as gate

    manifest = upgrade.load_campaign_manifest(campaign)
    store, store_campaign = store_module.open_campaign_store(campaign, manifest)
    revision, _current = baseline._store_revision(store, store_campaign)
    fact = baseline._store_candidate_fact(store, store_campaign, CANDIDATE, revision)
    if fact is None:
        raise replay.ReplayError("同一 Store 缺少本 revision 的候选冻结事实")
    build, _binding = compare.load_candidate_build_receipt(campaign, manifest, CANDIDATE, revision)
    identity = compare.candidate_gate_identity(campaign, manifest, CANDIDATE, build)
    subject = {**accept.expected_gate_subject(manifest, fact["payload"], identity),
               "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None}
    root = data / "control/candidate-external-gate"
    root.mkdir(mode=0o700)
    attempt_id = "recorded-candidate-gate"
    environment = _gate_environment(root, data, attempt_id)
    gates = []
    for gate_id, (directory, command) in sorted(gate.CANDIDATE_COMMANDS.items()):
        proof = source.write(root / "evidence" / (gate_id + ".json"), {
            "fixture_only": True, "purpose": "external_gate_control_observation", "gate_id": gate_id,
            "command": list(command), "command_executed": False, "not_production_gate_verdict": True,
            "formal_version_semantics_certified": False,
        })
        gates.append({"gate_id": gate_id, "working_directory": directory, "command": list(command),
                      "host": HOST, "architecture": "linux/arm64", "started_at_utc": upgrade._utc_now(),
                      "completed_at_utc": upgrade._utc_now(), "exit_code": 0, "status": "passed", "passed_count": 1,
                      "failed_count": 0, "skipped_count": 0, "stdout_sha256": "6" * 64, "stderr_sha256": "7" * 64,
                      "evidence": [{"path": proof.relative_to(root).as_posix(), "sha256": source.binding(proof)["sha256"]}]})
    source.write(root / "facts.json", {"schema_version": gate.FACTS_SCHEMA, "phase": gate.CANDIDATE_PHASE,
                 "attempt": {"attempt_id": attempt_id, "root_cause_id": None, "previous_receipt": None},
                 "subject": subject, "inputs": [], "gate_plan": None, "environment": environment, "gates": gates})
    gate.finalize(root, "facts.json", "receipt.json")
    gate.replay(root, "receipt.json")
    return root, "receipt.json"


def compare_and_accept(data: Path, campaign: Path) -> dict[str, Any]:
    """真实全规则画像和双侧 checker；官方评估来源直读本链 VC-1，任何缺证或检查失败都终止。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_assertion_profile as assertion_profile

    batches = [vc1._batch(data, campaign, "compare", ["compare", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE], phase="VC-5")]
    comparison = upgrade._load_stage_result(campaign, "compare", CANDIDATE)
    builder = comparison["builder"]
    action = {"action_id": "assert-rules", "operation": "VC-5:assert-rules", "timeout_seconds": 900.0,
              "command": [sys.executable, str(data / builder["script"]), "--config", str(campaign / builder["config"]["path"]),
                          "--output", str(campaign / builder["output"]), "--results-dir", str(campaign / builder["results_dir"]),
                          "--evaluation-baseline", str(builder["evaluation_baseline"]), "--reuse-authority", "none"],
              "item_ids": ["assert-rules"]}
    batches.append(vc1._batch(data, campaign, "assert-rules", [], phase="VC-5", action=action))
    manifest = upgrade.load_campaign_manifest(campaign)
    profile = assertion_profile.load_approved_assertion_profile(campaign, manifest)
    if len(comparison["rules"]) != 40:
        raise replay.ReplayError("全规则评估分母被缩减")
    root, receipt = candidate_gate(data, campaign)
    batches.append(vc1._batch(data, campaign, "acceptance", [
        "accept", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE,
        "--external-gate-root", str(root), "--external-gate-receipt", receipt,
    ], phase="VC-5"))
    upgrade._replay_vc_checkpoint(campaign, upgrade._vc_campaign_plan(campaign, manifest), "VC-5", revision=1)
    stage = upgrade._load_stage_result(campaign, "accept", CANDIDATE)
    if stage.get("status") != "complete" or stage.get("production_state") != "not_activated":
        raise replay.ReplayError("真实验收未形成 validation_only 未激活终态")
    return {"batches": batches, "assertion_profile": profile, "comparison": comparison,
            "gate": source.binding(root / receipt), "acceptance": stage}


def deliver(data: Path, campaign: Path, typed_fixture: Any) -> dict[str, Any]:
    """typed 文件现场和独立守卫给原件；Store、收据、账本与 checkpoint 由正式批次完成。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_delivery as delivery
    action = typed_fixture.run_action()
    batches = [vc1._batch(data, campaign, delivery.RUN_ACTION_ID, [], phase="VC-6", action=action)]
    status = delivery.delivery_status(campaign, CANDIDATE)
    if status.get("status") != "delivery_awaiting_finalize":
        raise replay.ReplayError(f"真实候选交付没有完成四阶段：{status.get('status')}")
    action = delivery.finalize_action(campaign, CANDIDATE, status["review_sha256"])
    batches.append(vc1._batch(data, campaign, delivery.FINALIZE_ACTION_ID, [], phase="VC-6", action=action))
    result = delivery.load_stage_result(campaign, CANDIDATE, manifest=upgrade.load_campaign_manifest(campaign))
    if (result is None or result.get("release_state") != "ready_for_operator_release"
            or result.get("production_state") != "not_activated"
            or delivery.delivery_status(campaign, CANDIDATE).get("status") != "ready_for_operator_release"):
        raise replay.ReplayError("真实交付终态与 validation_only 合同不符")
    return {"batches": batches, "stage": result, "source": source.binding(data / "control/recorded-delivery-control.json")}


def run_chain(state_path: Path, digest: str) -> dict[str, Any]:
    """完整链只有所有真实 checkpoint、Store 与两本账闭合才通过；来源标记不会升级成版本语义认证。"""

    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import codex_upgrade_project_ledger as project
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing
    from tools.official_client_control.store import ControlStore
    from tools.official_client_capture.tests import claude_typed_vc6_fixture as delivery_fixture

    started = time.monotonic()
    state = source.load_state(state_path, digest)
    data = Path(state["data_root"])
    _require_client_staging(data)
    first = vc1.run_chain(state_path, digest, installer=installed)
    continuation_path = Path(first["continuation"]["path"])
    continuation_digest = first["continuation"]["sha256"]
    with installed(state_path, digest):
        continuation = vc1.load_continuation(continuation_path, continuation_digest)
        campaign = continuation["campaign"]
        project_root = project.find_project_ledger(campaign, client="claude")
        if project_root != data / project.ledger_dir_name("claude"):
            raise replay.ReplayError("完整链项目总账不是同一原始数据根的 Claude 总账")
        initial_project = source.binding(project_root / "plan.json")
        stages = {"VC-1": first, "VC-2/3": classify_and_prepare(data, campaign), "VC-4": build_candidate(data, campaign)}
    # VC-4 后再安装新的外部观察源；禁止嵌套 SourceAdapter 造成一次动作重复签发/复制。
    with installed(state_path, digest):
        vc1.load_continuation(continuation_path, continuation_digest)
        stages["VC-5-capture"] = capture_and_seal(data, campaign)
        stages["VC-5-accept"] = compare_and_accept(data, campaign)
    with installed(state_path, digest), delivery_fixture.TypedDeliveryFixture(
            campaign, CANDIDATE, control_path=data / "control/recorded-delivery-control.json") as typed_fixture:
        stages["VC-6"] = deliver(data, campaign, typed_fixture)
        final = vc1.load_continuation(continuation_path, continuation_digest)
        manifest = final["manifest"]
        plan = upgrade._vc_campaign_plan(campaign, manifest)
        checkpoints = {}
        for phase in ("VC-0", "VC-1", "VC-2", "VC-3", "VC-4", "VC-5", "VC-6"):
            path, _payload = upgrade._replay_vc_checkpoint(campaign, plan, phase, revision=1 if phase in upgrade.CANDIDATE_VC_PHASES else None)
            checkpoints[phase] = source.binding(path)
        store = ControlStore(campaign / "control-store")
        store_replay = store.replay(external_root=data, require_external=True)
        store_campaign = store.load_campaign(manifest["campaign_id"])
        ledger = data / "control/timing"
        if (upgrade._campaign_timing_ledger_dir(campaign, manifest) != ledger or source.binding(project_root / "plan.json") != initial_project
                or project.find_project_ledger(campaign, client="claude") != project_root):
            raise replay.ReplayError("完整链换了时间账本或项目总账")
        timing_head, project_head = timing.phase_ledger_state(ledger), project.replay_head(project_root)
        if (timing_head["status"] not in {"active", "complete"} or timing_head["active_phase"] is not None
                or timing_head["completed_phases"] != ["VC-0", "VC-1", "VC-2", "VC-3", "VC-4", "VC-5", "VC-6"]):
            raise replay.ReplayError("VC-6 checkpoint 存在但七个阶段未在原时间账逐项收口")
    replay.assert_source_untouched(state["snapshot"])
    for path, expected in state["freeze"]["source_sentinel"].items():
        if replay._stat(Path(path)) != expected:
            raise replay.ReplayError("完整链改变了旧 freeze 来源元数据")
    report = {"schema_version": FULL_SCHEMA, "fixture_only": True, "purpose": "control_regression_execution",
              "status": "passed", "scope": "VC-0→VC-6", "registered_chains": ["vc-chain.claude-full-validation-only"],
              "target_version": source.VERSION, "campaign": str(campaign), "store_campaign_id": manifest["campaign_id"],
              "source_state": source.binding(state_path), "continuation": source.binding(continuation_path),
              "checkpoints": checkpoints, "store_replay": store_replay, "store_campaign": store_campaign,
              "timing_ledger": timing_head, "project_ledger": {"root": str(project_root), "plan": initial_project, "head": project_head},
              "stages": stages, "elapsed_seconds": time.monotonic() - started, "network_requests_this_run": 0,
              "historical_request_accounting": "真实录制和显式 Mac/边界来源按正式消费者计账，不代表本次实际联网请求",
              "full_vc0_vc6_certified": True, "formal_version_semantics_certified": False,
              "external_fixture_sources": ["历史 x64 单 npm 官方来源", "完整 Active 控制基线投影", "Docker/宿主观察",
                                           "离线候选构建输入", "Mac/边界来源输入", "外部门禁执行观察", "候选交付环境执行器"]}
    source.write(data / "control/recorded-full-chain-result.json", report)
    return report


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
    parser = argparse.ArgumentParser(description="Claude 录制控制回归；完整阶段实际执行后才登记控制链，不授予版本语义认证")
    parser.add_argument("--data", required=True)
    parser.add_argument("--recorded", required=True)
    parser.add_argument("--freeze", required=True)
    parser.add_argument("--parent-namespaces", required=True)
    parser.add_argument("--through", required=True, choices=("VC-3", "VC-6"))
    args = parser.parse_args(argv)
    try:
        if args.through == "VC-6":
            _require_client_staging(Path(args.data))
        report = vc1.namespace_main(args, inputs_builder=_inputs, chain_runner=run_chain if args.through == "VC-6" else run_through_profile,
                                    activation_module=ACTIVATION_MODULE)
    except BaseException as error:
        traceback.print_exc()
        print(json.dumps({"status": "unverified", "fixture_only": True, "registered_chains": [], "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
