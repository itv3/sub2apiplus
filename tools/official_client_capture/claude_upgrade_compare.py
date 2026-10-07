"""Claude VC-5 的 compare 读侧根（Claude 升级控制面设计 B4-06b）。

编排器服务面 ``claude_upgrade.compare_campaign`` 转调这里（Claude 身份策略的 ``evaluator.compare_reader_roots``，读侧闭包从
那个根按函数级计算）。compare 只读封存材料、不运行命令、不联网，产出三样东西：比较收据（compare 阶段结果）、官方侧评估用
manifest 与 builder 配置；之后 VC-5 的 ``assert-rules`` 动作按 builder 配置跑真实 Claude builder，accept 再读这三样。

一、输入与重放
==============
* 前置门：Campaign 锁内、没有仍在执行的租约、没有未隔离的污染事实；候选属于当前 revision（编排器侧与 Store 侧两份登记
  一致、未作废），计时账本 active；父监督器下运行时，账本当前评估基线与父 run 冻结的基线一致（Codex
  ``_verify_reader_runtime_baseline`` 同一判据，这里按 Claude 服务面自实现）。
* 候选阶段结果（最大轮次）经编排器读侧 ``_load_stage_result("capture-candidate")``，attempt 经封存后的读侧
  ``_capture_stage_attempt_context``（证据边界按封存证据清单加重绑链核对）；官方阶段结果与分类阶段结果同样走读侧。
* 批准断言画像：首次调用时冻结（``claude_upgrade_assertion_profile.freeze_approved_assertion_profile``，write-once，之后只
  读并核对）；生效画像按当前评估基线投影（approval-revision 基线取修订画像）。
* VC-4 候选构建收据：当前 revision 的 VC-4 checkpoint 必须绑定这一份，合同、Campaign、候选与 revision 逐项一致。

二、两侧评估用 manifest
=======================
* 候选侧：候选 seal 已合成并绑定（阶段结果 ``assertion_context.evaluation_manifest``），这里按合同重新校验；证据根是宿主
  数据根（封存深度扫描的路径基准）。网络面与逐 probe 请求路径取 seal 草稿绑定的候选 CaptureIndex。
* 官方侧（``official_evaluation_source``）：VC-1 封存时没有合成评估用 manifest，这里按同一口径合成——
  Store 里官方阶段结果登记的 EvidencePackage v3 → 完整性清单里的 capture manifest 与 CaptureIndex 绑定 → 深度扫描清单
  （采集路径取本 Campaign 官方 seal 的 ``evidence_manifest``；前序只读复用取前序 Campaign 官方阶段结果的同一份，前序
  Campaign 在宿主数据根 ``evidence/campaigns/<前序 ID>``，它登记的 EvidencePackage 必须就是导入的那一份）。
  EvidencePackage 的每条外部绑定都必须在深度扫描清单里且摘要、字节数一致；伴随文件（上游响应字节、中继连接索引）只在
  深度扫描清单里，按 ``claude_rule_assertion.bind_capture_manifest`` 的规则补绑。合成结果写进 compare 目录（write-once）。

三、比较与覆盖
==============
* 网络面（``host 请求行`` 计数）只作诊断：官方与候选的采集计划可以不同，``equal`` 为假不是失败。
* 覆盖：批准规则全集必须与生效画像的规则集逐项相等；每条规则在候选侧评估用 manifest 里至少有一条场景属于该规则
  ``scenario_ids`` 的 artifact，``dual_wire`` 规则另要求官方侧也有。覆盖不全即失败关闭（零写入）。

四、产物（全部 write-once，重跑逐字复用；输入漂移即拒绝）
=========================================================
compare 阶段目录（b0 为 ``comparisons/<候选>/``，b≥1 为评估基线 COMMIT 的写目标）：

* ``official-evaluation-manifest.json``：官方侧评估用 manifest；
* ``builder-config.json``：Claude builder 的配置（键与 Codex builder 相同）——生效画像、VC-2 批准的目标规则清单与画像
  摘要、两侧证据根与评估用 manifest、证据前缀、目标版本、批准规则、官方权威三摘要、Campaign 目录、候选、画像标签与摘要、
  三个包摘要；
* ``result.json``：比较收据（``claude-upgrade-comparison/v1``），绑定上面两个文件、各输入阶段结果与画像、候选外部门禁身份
  五项、覆盖与网络面诊断，以及 builder 的输出位置（当前评估基线的 ``assertions`` 写目标）。``package_digest`` 是收据核心
  （除 builder 绑定与比较时刻）的摘要，builder 配置里的 ``comparison_package_digest`` 就是它。

五、候选外部门禁身份（``candidate_gate_identity``，accept 与驱动 ``build_gate_facts`` 共用）
==========================================================================================
B4-06a 的 subject 要五项候选身份，Claude 的取法：

* ``profile_id``：Claude 画像没有 profile_id（B5b-01a 已定 PROFILE_ID 只是驱动标签），固定为
  ``claude-code-<目标版本>-official``，与驱动示例参数 ``TARGET_PROFILE_ID`` 同一写法；
* ``profile_digest``：VC-4 构建收据的 ``profile.profile_digest``（目标画像文件摘要）；
* ``candidate_package_digest``：第 1 轮候选阶段结果登记的 CandidateEvidencePackage 对象摘要。门禁重试（``--revalidate``）
  每轮换新证据包，但证据本身不变；用第 1 轮的摘要，门禁失败收据与重试收据的 subject 保持一致，断言结果也不因重试失效；
* ``candidate_image_id``／``candidate_image_reference``：VC-4 构建收据的 ``image.image_id``／``image.reference``。

本文件在 Claude 身份策略里按 ``claude_upgrade_`` 前缀归 control 层；Codex 身份策略忽略它（``claude_`` 前缀）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_acceptance_contract as acceptance_contract
from tools.official_client_capture import claude_rule_assertion
from tools.official_client_capture import claude_upgrade_assertion_profile
from tools.official_client_capture import claude_upgrade_candidate
from tools.official_client_capture import claude_upgrade_candidate_seal
from tools.official_client_capture import claude_upgrade_evidence
from tools.official_client_capture import claude_upgrade_revision
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.errors import ControlError

COMPARISON_SCHEMA = "claude-upgrade-comparison/v1"
STAGE_NAME = "compare"
OFFICIAL_EVALUATION_MANIFEST_FILENAME = "official-evaluation-manifest.json"
BUILDER_CONFIG_FILENAME = "builder-config.json"
BUILDER_RELATIVE = "tools/official_client_capture/claude_build_rule_assertion_results.py"
# builder 结果里证据引用的逻辑前缀（``<前缀>/<相对证据根的路径>``）。
EVIDENCE_PREFIXES = {"official": "official-evidence", "candidate": "candidate-evidence"}
PROFILE_ID_FORMAT = "claude-code-{version}-official"
SIDES = ("official", "candidate")
# 一侧评估来源的字段闭集（``official_evaluation_source``／``candidate_evaluation_source`` 的返回）。
SOURCE_FIELDS = frozenset(
    {
        "mode",
        "evidence_root",
        "capture_id",
        "evaluation_manifest",
        "package_ref",
        "capture_manifest",
        "capture_index",
        "inventory",
        "surfaces",
        "probe_request_paths",
    }
)
STAGE_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "client",
        "campaign_id",
        "stage",
        "candidate_id",
        "status",
        "offline_only",
        "candidate_revision",
        "validation_round",
        "evaluation_baseline",
        "baseline_commit_sha256",
        "inputs",
        "rules",
        "validation_modes",
        "official",
        "candidate",
        "equal",
        "surfaces",
        "coverage",
        "identity",
        "official_authority",
        "package_digest",
        "builder",
        "compared_at_utc",
    }
)
# 收据核心（进 ``package_digest``）之外的字段：builder 绑定（配置里要引用 package_digest，不能反过来进它）与比较时刻。
PACKAGE_EXCLUDED_FIELDS = frozenset({"package_digest", "builder", "compared_at_utc"})


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _read_json(path: Path, label: str) -> dict[str, Any]:
    return _orchestrator()._read_json(path, label)


def _campaign_binding(campaign_dir: Path, path: Path) -> dict[str, str]:
    return {"path": Path(path).relative_to(Path(campaign_dir)).as_posix(), "sha256": file_sha256(Path(path))}


def _bound_campaign_file(campaign_dir: Path, binding: Any, label: str) -> Path:
    """Campaign 内相对绑定（``{path, sha256}``，可带 ``bytes``）→ 文件路径；不是普通文件、摘要或字节数不符即失败关闭。"""

    orchestrator = _orchestrator()
    if (
        not isinstance(binding, Mapping)
        or not {"path", "sha256"} <= set(binding) <= {"path", "sha256", "bytes"}
        or not isinstance(binding.get("path"), str)
        or not binding["path"]
        or not orchestrator.SHA256_RE.fullmatch(str(binding.get("sha256")))
    ):
        raise ConfigurationError(f"{label}文件绑定非法。")
    path = orchestrator._campaign_file(Path(campaign_dir), str(binding["path"]))
    if path.is_symlink() or not path.is_file() or file_sha256(path) != binding["sha256"]:
        raise ConfigurationError(f"{label}缺失或摘要漂移：{binding.get('path')}")
    if "bytes" in binding and path.stat().st_size != binding["bytes"]:
        raise ConfigurationError(f"{label}字节数与绑定不一致：{binding.get('path')}")
    return path


def _data_root_file(data_root: Path, binding: Any, label: str) -> Path:
    """宿主数据根内相对绑定（``{path, sha256, bytes}``）→ 文件路径；越出数据根、不是普通文件或摘要／字节数不符即失败关闭。"""

    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
        raise ConfigurationError(f"{label}绑定形态非法。")
    relative = PurePosixPath(str(binding["path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise ConfigurationError(f"{label}路径越出宿主数据根：{binding['path']}")
    path = Path(data_root).joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file() or file_sha256(path) != binding.get("sha256"):
        raise ConfigurationError(f"{label}缺失或摘要漂移：{binding['path']}")
    if "bytes" in binding and path.stat().st_size != binding["bytes"]:
        raise ConfigurationError(f"{label}字节数与绑定不一致：{binding['path']}")
    return path


def data_root(campaign_dir: Path) -> Path:
    """宿主数据根：规范坐标 ``<数据根>/evidence/campaigns/<id>``（与采集布局同一规则）。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._host_data_root(Path(campaign_dir))


# ---------------------------------------------------------------------------
# 一、前置门
# ---------------------------------------------------------------------------


def verify_reader_runtime_baseline(campaign_dir: Path, candidate_id: str, *, label: str) -> None:
    """父监督器下运行时，账本当前评估基线与父 run 冻结的基线一致（与 Codex ``_verify_reader_runtime_baseline`` 同一判据）。

    清单没有冻结基线（非 VC-5 批次）、属于别的候选、或不在父监督器下运行时不核对。
    """

    run_value = os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_DIR_ENV)
    if not run_value:
        return
    inner = codex_upgrade_supervisor._run_inner_manifest(Path(run_value))
    if inner is None:
        raise ConfigurationError(f"{label}：父监督器 run 清单缺失或摘要不一致，不能核对当前评估基线。")
    if "evaluation_baseline" not in inner or inner.get("candidate_id") not in (None, candidate_id):
        return
    frozen_baseline = int(inner.get("evaluation_baseline") or 0)
    frozen_commit = inner.get("baseline_commit_sha256")
    baseline, commit = _orchestrator()._current_evaluation_baseline(Path(campaign_dir), candidate_id)
    actual_commit = commit.get("commit_sha256") if isinstance(commit, Mapping) else None
    if baseline != frozen_baseline or actual_commit != frozen_commit:
        raise ConfigurationError(
            f"{label}：账本当前评估基线 b{baseline}（COMMIT {str(actual_commit)[:12]}）与父 run 冻结的 "
            f"b{frozen_baseline}（COMMIT {str(frozen_commit)[:12]}）不一致；基线已变化，按当前基线重新编译派发。"
        )


def require_current_candidate(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, *, action: str) -> dict[str, Any]:
    """候选属于当前 revision（编排器侧与 Store 侧一致、Store 侧未作废），计时账本 active；返回 revision 与 Store。"""

    revision, record = claude_upgrade_revision.require_candidate_revision(Path(campaign_dir), manifest, action=action)
    if record is None or record.get("candidate_id") != candidate_id:
        raise ConfigurationError(
            f"{action} 拒绝：候选 {candidate_id} 不属于当前 revision r{revision}"
            f"（当前候选 {record.get('candidate_id') if record else None}）；被取代候选只读。"
        )
    store, store_campaign = claude_upgrade_store.open_campaign_store(Path(campaign_dir), manifest)
    claude_upgrade_seal._require_store_tool_current(store, store_campaign)
    if claude_upgrade_candidate.require_candidate_revision(store, store_campaign, candidate_id, action=action) != revision:
        raise ConfigurationError(f"{action} 拒绝：Store 的当前候选 revision 与编排器激活的 r{revision} 不一致。")
    claude_upgrade_candidate.require_timing_ledger_active(Path(campaign_dir), manifest, action=action)
    return {"revision": int(revision), "store": store, "store_campaign": store_campaign}


def load_candidate_build_receipt(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, revision: int) -> tuple[dict[str, Any], dict[str, str]]:
    """当前 revision 的 VC-4 候选构建收据：VC-4 checkpoint 绑定的就是它，合同、Campaign、候选与 revision 逐项一致。"""

    orchestrator = _orchestrator()
    plan = claude_upgrade_vc.vc_campaign_plan(Path(campaign_dir), manifest)
    _path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(Path(campaign_dir), plan, "VC-4", revision=revision)
    build_path = orchestrator._candidate_build_receipt_path(Path(campaign_dir), candidate_id)
    if build_path.is_symlink() or not build_path.is_file():
        raise ConfigurationError(f"候选 {candidate_id} 的 VC-4 构建收据缺失。")
    binding = _campaign_binding(Path(campaign_dir), build_path)
    stage_receipt = checkpoint.get("stage_receipt") if isinstance(checkpoint.get("stage_receipt"), Mapping) else {}
    receipt_path = Path(str(stage_receipt.get("path", "")))
    if not receipt_path.is_absolute():
        receipt_path = Path(campaign_dir) / receipt_path
    if receipt_path != build_path or stage_receipt.get("sha256") != binding["sha256"]:
        raise ConfigurationError(f"VC-4 checkpoint（r{revision}）没有绑定候选 {candidate_id} 的这份构建收据。")
    try:
        build = vc_artifacts.validate_official_client_candidate_build_receipt(_read_json(build_path, "候选构建收据"))
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"候选构建收据不符合合同：{error}") from error
    if (build["client"], build["campaign_id"], build["candidate_id"], build["candidate_revision"], build["target_version"]) != (
        "claude",
        manifest["campaign_id"],
        candidate_id,
        revision,
        manifest["target_version"],
    ):
        raise ConfigurationError("候选构建收据的客户端、Campaign、候选、revision 或目标版本与本轮不一致。")
    return build, binding


def candidate_gate_identity(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    build: Mapping[str, Any],
) -> dict[str, str]:
    """候选外部门禁 subject 的五项候选身份（见模块说明第五节）；accept 与驱动 ``build_gate_facts`` 共用这一处。"""

    _path, round_one = claude_upgrade_candidate_seal.load_round_one_result(Path(campaign_dir), candidate_id, manifest=manifest)
    package_ref = (round_one.get("store") or {}).get("candidate_evidence_package_ref")
    if not isinstance(package_ref, Mapping) or not _orchestrator().SHA256_RE.fullmatch(str(package_ref.get("sha256", ""))):
        raise ConfigurationError("第 1 轮候选阶段结果没有登记 CandidateEvidencePackage 引用。")
    return {
        "profile_id": PROFILE_ID_FORMAT.format(version=manifest["target_version"]),
        "profile_digest": str(build["profile"]["profile_digest"]),
        "candidate_package_digest": str(package_ref["sha256"]),
        "candidate_image_id": str(build["image"]["image_id"]),
        "candidate_image_reference": str(build["image"]["reference"]),
    }


# ---------------------------------------------------------------------------
# 二、两侧评估来源
# ---------------------------------------------------------------------------


def _request_path(line: str) -> str | None:
    """请求行 ``METHOD target HTTP/x`` 的路径部分（去掉查询串）；不是请求行时为 None。"""

    parts = str(line).split(" ")
    if len(parts) < 3 or not parts[1].startswith("/"):
        return None
    return parts[1].split("?", 1)[0]


def _index_probe_request_paths(index: Mapping[str, Any]) -> dict[str, list[str]]:
    """CaptureIndex 逐 probe 的请求路径集合（取有效连接上的请求行）。"""

    paths: dict[str, set[str]] = {}
    for job in index.get("jobs") or []:
        for probe in job.get("probes") or []:
            collected = paths.setdefault(str(probe.get("probe_id")), set())
            for connection in probe.get("connections") or []:
                for line in connection.get("requests") or []:
                    path = _request_path(str(line))
                    if path is not None:
                        collected.add(path)
    return {probe: sorted(values) for probe, values in sorted(paths.items())}


def _index_surfaces(index: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    inventory = index.get("network_inventory")
    surfaces = inventory.get("surfaces") if isinstance(inventory, Mapping) else None
    if not isinstance(surfaces, list) or any(
        not isinstance(item, Mapping) or set(item) != {"surface", "count"} for item in surfaces
    ):
        raise ConfigurationError(f"{label} CaptureIndex 没有网络面清单（network_inventory.surfaces）。")
    return [{"surface": str(item["surface"]), "count": int(item["count"])} for item in surfaces]


def _load_inventory(path: Path, label: str) -> dict[str, dict[str, Any]]:
    inventory = _read_json(path, label)
    if inventory.get("schema_version") != claude_upgrade_evidence.EVIDENCE_INVENTORY_SCHEMA or not isinstance(inventory.get("entries"), list):
        raise ConfigurationError(f"{label}合同名不符。")
    index: dict[str, dict[str, Any]] = {}
    for entry in inventory["entries"]:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "bytes", "sha256"}:
            raise ConfigurationError(f"{label}条目形态非法。")
        index[str(entry["path"])] = {"sha256": str(entry["sha256"]), "bytes": int(entry["bytes"])}
    return index


def _official_inventory(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    official: Mapping[str, Any],
    package_ref: Mapping[str, Any],
    root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """官方 seal 的深度扫描清单（见模块说明第二节）：返回 ``(路径索引, 来源绑定)``，绑定路径相对宿主数据根。"""

    mode = official.get("mode")
    if mode == "captured":
        seal = official.get("seal") if isinstance(official.get("seal"), Mapping) else {}
        path = _bound_campaign_file(campaign_dir, seal.get("evidence_manifest"), "官方 seal 深度扫描清单")
        return _load_inventory(path, "官方 seal 深度扫描清单"), {
            "path": path.relative_to(root).as_posix(),
            "sha256": file_sha256(path),
            "source": "official_seal",
        }
    if mode != "predecessor_reuse":
        raise ConfigurationError(f"官方阶段结果的模式不受支持：{mode!r}")
    reuse = official.get("predecessor_reuse") if isinstance(official.get("predecessor_reuse"), Mapping) else {}
    predecessor = reuse.get("predecessor") if isinstance(reuse.get("predecessor"), Mapping) else {}
    predecessor_id = predecessor.get("campaign_id")
    if not isinstance(predecessor_id, str) or not _orchestrator().SAFE_ID_RE.fullmatch(predecessor_id):
        raise ConfigurationError("只读复用的官方阶段结果没有登记前序 Campaign ID。")
    predecessor_dir = root / "evidence" / "campaigns" / predecessor_id
    stage_path = predecessor_dir / claude_upgrade_seal.STAGE_RESULT_RELATIVE
    try:
        predecessor_stage = claude_upgrade_seal.load_stage_result(predecessor_dir, stage_path)
    except (ConfigurationError, OSError) as error:
        raise ConfigurationError(f"前序 Campaign {predecessor_id} 的官方阶段结果不可读：{error}") from error
    if predecessor_stage.get("mode") != "captured":
        raise ConfigurationError(f"前序 Campaign {predecessor_id} 的官方证据不是采集路径封存的，没有深度扫描清单。")
    if dict((predecessor_stage.get("store") or {}).get("evidence_package_ref") or {}) != dict(package_ref):
        raise ConfigurationError(f"前序 Campaign {predecessor_id} 登记的 EvidencePackage 不是本 Campaign 导入的那一份。")
    seal = predecessor_stage.get("seal") if isinstance(predecessor_stage.get("seal"), Mapping) else {}
    path = _bound_campaign_file(predecessor_dir, seal.get("evidence_manifest"), "前序官方 seal 深度扫描清单")
    return _load_inventory(path, "前序官方 seal 深度扫描清单"), {
        "path": path.relative_to(root).as_posix(),
        "sha256": file_sha256(path),
        "source": "predecessor_official_seal",
    }


def official_evaluation_source(campaign_dir: Path, manifest: Mapping[str, Any], official: Mapping[str, Any]) -> dict[str, Any]:
    """官方侧评估来源（见模块说明第二节）：返回 ``SOURCE_FIELDS`` 闭集的字典，评估用 manifest 已按合同校验。"""

    root = data_root(Path(campaign_dir))
    package_ref = (official.get("store") or {}).get("evidence_package_ref")
    if not isinstance(package_ref, Mapping) or package_ref.get("object_kind") != "evidence_package":
        raise ConfigurationError("官方阶段结果没有登记 EvidencePackage 引用。")
    store, _campaign = claude_upgrade_store.open_campaign_store(Path(campaign_dir), manifest)
    try:
        package = dict(store.load_object(dict(package_ref))["payload"])
        completeness = dict(store.load_object(dict(package["completeness_ref"]))["payload"])
    except (ControlError, KeyError, TypeError, ValueError) as error:
        raise ConfigurationError(f"官方 EvidencePackage 或完整性清单无法从 Store 读取：{error}") from error
    if package.get("schema_version") != claude_upgrade_evidence.EVIDENCE_PACKAGE_V3_SCHEMA:
        raise ConfigurationError("官方 EvidencePackage 不是 v3 合同，不能合成评估用 manifest。")
    purposes: dict[str, dict[str, Any]] = {}
    for entry in completeness.get("entries") or []:
        facts = entry.get("facts") if isinstance(entry, Mapping) else None
        if isinstance(facts, Mapping) and isinstance(facts.get("binding"), Mapping):
            purposes[str(facts.get("purpose"))] = dict(facts["binding"])
    bindings = {name: purposes.get(claude_upgrade_evidence.COMPLETENESS_PURPOSES[name]) for name in ("capture_manifest", "capture_index")}
    for name, binding in bindings.items():
        if binding is None:
            raise ConfigurationError(f"官方完整性清单没有 {name} 绑定。")
    inventory, inventory_binding = _official_inventory(Path(campaign_dir), manifest, official, package_ref, root)
    # EvidencePackage 的每条外部绑定都必须在深度扫描清单里、摘要与字节数一致。
    package_bindings = [
        *(binding for item in package.get("evidence_items") or [] for binding in item.get("evidence_refs") or []),
        *purposes.values(),
    ]
    for binding in package_bindings:
        entry = inventory.get(str(binding.get("path")))
        if entry is None or entry["sha256"] != binding.get("sha256") or entry["bytes"] != binding.get("bytes"):
            raise ConfigurationError(f"官方 EvidencePackage 的绑定不在深度扫描清单里或摘要不一致：{binding.get('path')}")
    capture_path = _data_root_file(root, bindings["capture_manifest"], "官方 capture manifest")
    index_path = _data_root_file(root, bindings["capture_index"], "官方 CaptureIndex")
    capture = _read_json(capture_path, "官方 capture manifest")
    capture_index = _read_json(index_path, "官方 CaptureIndex")
    capture_id = str((capture.get("attempt") or {}).get("attempt_id", ""))
    try:
        evaluation = claude_rule_assertion.bind_capture_manifest(capture, inventory, capture_id=capture_id)
    except claude_rule_assertion.AssertionConfigurationError as error:
        raise ConfigurationError(f"官方评估用 manifest 合成失败：{error}") from error
    return {
        "mode": str(official.get("mode")),
        "evidence_root": str(root),
        "capture_id": capture_id,
        "evaluation_manifest": evaluation,
        "package_ref": dict(package_ref),
        "capture_manifest": dict(bindings["capture_manifest"]),
        "capture_index": dict(bindings["capture_index"]),
        "inventory": inventory_binding,
        "surfaces": _index_surfaces(capture_index, "官方"),
        "probe_request_paths": _index_probe_request_paths(capture_index),
    }


def candidate_evaluation_source(campaign_dir: Path, manifest: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """候选侧评估来源：seal 绑定的评估用 manifest、seal 草稿里的候选 CaptureIndex 与当前轮次的 CandidateEvidencePackage。"""

    root = data_root(Path(campaign_dir))
    context = candidate.get(claude_upgrade_candidate_seal.ASSERTION_CONTEXT_FIELD)
    if not isinstance(context, Mapping):
        raise ConfigurationError("候选阶段结果没有 assertion_context。")
    manifest_path = _bound_campaign_file(Path(campaign_dir), context.get(claude_upgrade_candidate_seal.EVALUATION_MANIFEST_FIELD), "候选评估用 manifest")
    try:
        evaluation = claude_rule_assertion.validate_evaluation_manifest(
            _read_json(manifest_path, "候选评估用 manifest"), expected_claude_version=str(manifest["target_version"])
        )
    except claude_rule_assertion.AssertionConfigurationError as error:
        raise ConfigurationError(f"候选评估用 manifest 不符合合同：{error}") from error
    seal = candidate.get("seal") if isinstance(candidate.get("seal"), Mapping) else {}
    draft_path = _bound_campaign_file(Path(campaign_dir), seal.get("seal_draft"), "候选 seal 草稿")
    draft = _read_json(draft_path, "候选 seal 草稿")
    drafts = draft.get("drafts") if isinstance(draft.get("drafts"), Mapping) else {}
    index_path = _bound_campaign_file(Path(campaign_dir), drafts.get("capture_index"), "候选 CaptureIndex")
    capture_path = _bound_campaign_file(Path(campaign_dir), drafts.get("capture_manifest"), "候选 capture manifest")
    capture_index = _read_json(index_path, "候选 CaptureIndex")
    package_ref = (candidate.get("store") or {}).get("candidate_evidence_package_ref")
    if not isinstance(package_ref, Mapping) or package_ref.get("object_kind") != "candidate_evidence_package":
        raise ConfigurationError("候选阶段结果没有登记 CandidateEvidencePackage 引用。")
    evidence_manifest = seal.get("evidence_manifest") if isinstance(seal.get("evidence_manifest"), Mapping) else None
    _bound_campaign_file(Path(campaign_dir), evidence_manifest, "候选深度扫描清单")
    return {
        "mode": "candidate_seal",
        "evidence_root": str(root),
        "capture_id": str(evaluation["capture_id"]),
        "evaluation_manifest": evaluation,
        "package_ref": dict(package_ref),
        "capture_manifest": _campaign_binding(Path(campaign_dir), capture_path),
        "capture_index": _campaign_binding(Path(campaign_dir), index_path),
        "inventory": {**dict(evidence_manifest or {}), "source": "candidate_seal"},
        "surfaces": _index_surfaces(capture_index, "候选"),
        "probe_request_paths": _index_probe_request_paths(capture_index),
    }


def _require_source(source: Any, label: str, target_version: str) -> dict[str, Any]:
    if not isinstance(source, Mapping) or set(source) != SOURCE_FIELDS:
        raise ConfigurationError(f"{label}评估来源的字段不闭合。")
    try:
        claude_rule_assertion.validate_evaluation_manifest(source["evaluation_manifest"], expected_claude_version=target_version)
    except claude_rule_assertion.AssertionConfigurationError as error:
        raise ConfigurationError(f"{label}评估用 manifest 不符合合同：{error}") from error
    if not Path(str(source["evidence_root"])).is_absolute() or not Path(str(source["evidence_root"])).is_dir():
        raise ConfigurationError(f"{label}证据根必须是存在的绝对路径。")
    return dict(source)


# ---------------------------------------------------------------------------
# 三、比较与覆盖
# ---------------------------------------------------------------------------


def compare_surfaces(official: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """网络面诊断：只在一侧出现的面与两侧计数不同的面（采集计划不同时允许不等）。"""

    left = {str(item["surface"]): int(item["count"]) for item in official}
    right = {str(item["surface"]): int(item["count"]) for item in candidate}
    counts = [
        {"surface": surface, "official": left[surface], "candidate": right[surface]}
        for surface in sorted(set(left) & set(right))
        if left[surface] != right[surface]
    ]
    official_only = sorted(set(left) - set(right))
    candidate_only = sorted(set(right) - set(left))
    return {
        "equal": not (official_only or candidate_only or counts),
        "official_only": official_only,
        "candidate_only": candidate_only,
        "count_differences": counts,
    }


def rule_coverage(
    profile: Mapping[str, Any],
    modes: Mapping[str, str],
    manifests: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """逐规则覆盖：规则 ``scenario_ids`` 在候选侧（``dual_wire`` 另加官方侧）评估用 manifest 里至少有一条 artifact。"""

    rows: list[dict[str, Any]] = []
    for rule in sorted(profile["rules"], key=lambda item: str(item["rule_id"])):
        rule_id = str(rule["rule_id"])
        scenarios = set(rule.get("scenario_ids") or [])
        counts = {
            side: sum(1 for artifact in manifests[side]["artifacts"] if scenarios & set(artifact.get("scenario_ids") or []))
            for side in SIDES
        }
        required = SIDES if modes[rule_id] == acceptance_contract.MODE_DUAL_WIRE else ("candidate",)
        missing = [side for side in required if counts[side] == 0]
        rows.append(
            {
                "rule": rule_id,
                "validation_mode": modes[rule_id],
                "scenario_ids": sorted(scenarios),
                "official_artifacts": counts["official"],
                "candidate_artifacts": counts["candidate"],
                "missing_sides": missing,
            }
        )
    return {"complete": all(not row["missing_sides"] for row in rows), "rules": rows}


# ---------------------------------------------------------------------------
# 四、compare
# ---------------------------------------------------------------------------


def _effective_profile(campaign_dir: Path, classification: Mapping[str, Any]) -> tuple[Path, dict[str, Any], dict[str, str]]:
    reference = classification.get("assertion_profile_manifest")
    path = _bound_campaign_file(Path(campaign_dir), reference, "生效的批准断言画像")
    try:
        profile = acceptance_contract.load_profile(path)
    except acceptance_contract.AcceptanceContractError as error:
        raise ConfigurationError(f"生效的批准断言画像不可用：{error}") from error
    return path, profile, {"path": str(reference["path"]), "sha256": str(reference["sha256"])}  # type: ignore[index]


def _package_digest(report: Mapping[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in report.items() if key not in PACKAGE_EXCLUDED_FIELDS})


def compare_campaign(campaign_dir: Path, candidate_id: str) -> dict[str, Any]:
    """compare 读侧根（见模块说明）：Campaign 锁内只读封存材料，写比较收据、官方评估用 manifest 与 builder 配置。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    orchestrator._validate_existing_campaign_path(campaign_dir)
    campaign_dir = campaign_dir.resolve(strict=True)
    if not isinstance(candidate_id, str) or not orchestrator.SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("compare 必须提供合法 --candidate-id。")
    orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        orchestrator._require_quiescent_campaign_lease(campaign_dir, action="compare")
        orchestrator._reject_contaminated_campaign(campaign_dir)
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        return _compare(campaign_dir, manifest, candidate_id)


def _compare(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    orchestrator = _orchestrator()
    current = require_current_candidate(campaign_dir, manifest, candidate_id, action="compare")
    verify_reader_runtime_baseline(campaign_dir, candidate_id, label="compare")
    candidate = orchestrator._load_stage_result(campaign_dir, "capture-candidate", candidate_id)
    orchestrator._capture_stage_attempt_context(campaign_dir, candidate, phase="candidate", candidate_id=candidate_id)
    official = orchestrator._load_stage_result(campaign_dir, "capture-official")
    for label, value in (("官方", official), ("候选", candidate)):
        if value.get("status") != "complete":
            raise ConfigurationError(f"{label}阶段尚未完整封存。")
    # 批准断言画像：VC-5 首次评估前冻结一次（write-once，已冻结即按绑定收据全量核对）。
    claude_upgrade_assertion_profile.freeze_approved_assertion_profile(campaign_dir, manifest)
    classification = orchestrator._load_stage_result(campaign_dir, "classify")
    if classification.get("status") != "complete":
        raise ConfigurationError("分类阶段尚未完整封存。")
    view = orchestrator._effective_classification_view(campaign_dir, candidate_id, classification)
    profile_path, profile, profile_binding = _effective_profile(campaign_dir, view)
    rules = list(orchestrator._approved_rules(campaign_dir, manifest, require_approved=True))
    modes = acceptance_contract.derive_validation_modes(profile)
    if sorted(rules) != sorted(modes):
        raise ConfigurationError(
            f"批准的目标规则与生效画像的规则集不一致：缺 {sorted(set(rules) - set(modes)) or '无'}，多 {sorted(set(modes) - set(rules)) or '无'}。"
        )
    rule_manifest_path = _bound_campaign_file(campaign_dir, (classification.get("approved") or {}).get("target_rules"), "VC-2 批准的目标规则清单")
    build, build_binding = load_candidate_build_receipt(campaign_dir, manifest, candidate_id, current["revision"])
    identity = candidate_gate_identity(campaign_dir, manifest, candidate_id, build)
    target_version = str(manifest["target_version"])
    try:
        official_source = official_evaluation_source(campaign_dir, manifest, official)
    except ConfigurationError as error:
        raise ConfigurationError(f"官方评估来源无法确定：{error}") from error
    sources = {
        "official": _require_source(official_source, "官方", target_version),
        "candidate": _require_source(candidate_evaluation_source(campaign_dir, manifest, candidate), "候选", target_version),
    }
    coverage = rule_coverage(profile, modes, {side: sources[side]["evaluation_manifest"] for side in SIDES})
    if not coverage["complete"]:
        missing = [f"{row['rule']}({'、'.join(row['missing_sides'])})" for row in coverage["rules"] if row["missing_sides"]]
        raise ConfigurationError(f"官方／候选逐规则证据覆盖不完整：{'；'.join(missing)}。")
    surfaces = compare_surfaces(sources["official"]["surfaces"], sources["candidate"]["surfaces"])
    baseline, commit = orchestrator._current_evaluation_baseline(campaign_dir, candidate_id)
    _canonical, stage_path = orchestrator._stage_path(campaign_dir, STAGE_NAME, candidate_id, mode="write")
    stage_dir = stage_path.parent
    official_manifest_path = stage_dir / OFFICIAL_EVALUATION_MANIFEST_FILENAME
    claude_upgrade_seal._write_once_or_reuse(official_manifest_path, sources["official"]["evaluation_manifest"])
    candidate_manifest_path = _bound_campaign_file(
        campaign_dir, candidate[claude_upgrade_candidate_seal.ASSERTION_CONTEXT_FIELD][claude_upgrade_candidate_seal.EVALUATION_MANIFEST_FIELD], "候选评估用 manifest"
    )
    candidate_path = orchestrator._stage_path(campaign_dir, "capture-candidate", candidate_id)[1]
    rounds = claude_upgrade_candidate_seal._existing_rounds(campaign_dir, candidate_id)
    if rounds:
        candidate_path = claude_upgrade_candidate_seal._round_result_path(campaign_dir, candidate_id, rounds[-1])
    official_authority = orchestrator._classification_official_authority(view)
    side_summary = {
        side: {
            "mode": sources[side]["mode"],
            "evaluation_manifest": _campaign_binding(campaign_dir, official_manifest_path if side == "official" else candidate_manifest_path),
            "evidence_root": sources[side]["evidence_root"],
            "capture_id": sources[side]["capture_id"],
            "package_ref": sources[side]["package_ref"],
            "capture_manifest": sources[side]["capture_manifest"],
            "capture_index": sources[side]["capture_index"],
            "inventory": sources[side]["inventory"],
            "probe_request_paths": sources[side]["probe_request_paths"],
        }
        for side in SIDES
    }
    report: dict[str, Any] = {
        "schema_version": COMPARISON_SCHEMA,
        "client": "claude",
        "campaign_id": str(manifest["campaign_id"]),
        "stage": STAGE_NAME,
        "candidate_id": candidate_id,
        "status": "complete",
        "offline_only": True,
        "candidate_revision": current["revision"],
        "validation_round": int(candidate.get("validation_round", claude_upgrade_candidate_seal.FIRST_ROUND)),
        "evaluation_baseline": int(baseline),
        "baseline_commit_sha256": commit.get("commit_sha256") if isinstance(commit, Mapping) else None,
        "inputs": {
            "candidate_stage": _campaign_binding(campaign_dir, candidate_path),
            "official_stage": _campaign_binding(campaign_dir, campaign_dir / claude_upgrade_seal.STAGE_RESULT_RELATIVE),
            "assertion_profile": profile_binding,
            "assertion_profile_binding": _campaign_binding(campaign_dir, campaign_dir / claude_upgrade_assertion_profile.BINDING_RELATIVE),
            "rule_manifest": _campaign_binding(campaign_dir, rule_manifest_path),
            "build_receipt": build_binding,
        },
        "rules": rules,
        "validation_modes": {rule: modes[rule] for rule in sorted(modes)},
        "official": side_summary["official"],
        "candidate": side_summary["candidate"],
        "equal": bool(surfaces["equal"]),
        "surfaces": surfaces,
        "coverage": coverage,
        "identity": identity,
        "official_authority": dict(official_authority),
    }
    report["package_digest"] = _package_digest(report)
    assertions_dir = orchestrator._stage_write_target(campaign_dir, candidate_id, baseline, "assertions")
    config = {
        "assertion_profile": str(profile_path),
        "rule_manifest": str(rule_manifest_path),
        "expected_profile_sha256": profile_binding["sha256"],
        "official_evidence_root": sources["official"]["evidence_root"],
        "candidate_evidence_root": sources["candidate"]["evidence_root"],
        "official_capture_manifest": str(official_manifest_path),
        "candidate_capture_manifest": str(candidate_manifest_path),
        "official_evidence_prefix": EVIDENCE_PREFIXES["official"],
        "candidate_evidence_prefix": EVIDENCE_PREFIXES["candidate"],
        "target_version": target_version,
        "candidate_id": candidate_id,
        "profile_id": identity["profile_id"],
        "profile_digest": identity["profile_digest"],
        "official_package_digest": str(sources["official"]["package_ref"]["sha256"]),
        "candidate_package_digest": identity["candidate_package_digest"],
        "comparison_package_digest": report["package_digest"],
        "official_authority": dict(official_authority),
        "rules": rules,
        "campaign_dir": str(campaign_dir),
    }
    config_path = stage_dir / BUILDER_CONFIG_FILENAME
    claude_upgrade_seal._write_once_or_reuse(config_path, config)
    report["builder"] = {
        "config": _campaign_binding(campaign_dir, config_path),
        "script": BUILDER_RELATIVE,
        "output": (assertions_dir / "results.json").relative_to(campaign_dir).as_posix(),
        "results_dir": (assertions_dir / "machine").relative_to(campaign_dir).as_posix(),
        "evaluation_baseline": int(baseline),
    }
    report["compared_at_utc"] = orchestrator._utc_now()
    sealed = claude_upgrade_seal._write_once_or_reuse(stage_path, report, ignore=("compared_at_utc",))
    return {**sealed, "stage_result": str(stage_path)}


# ---------------------------------------------------------------------------
# 五、compare 阶段结果的读侧
# ---------------------------------------------------------------------------


def load_stage_result(campaign_dir: Path, path: Path, candidate_id: str, *, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取并核对比较收据（``_load_stage_result("compare")`` 的 Claude 实现）：合同、字段闭集、Campaign、候选、阶段与状态，
    收据核心摘要复算，以及官方评估用 manifest、builder 配置、各输入阶段结果与画像绑定的摘要。没有收据时报「尚未封存」。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError("比较收据路径不可信。")
    if not path.exists():
        raise ConfigurationError(f"阶段尚未封存：{STAGE_NAME}（{path.relative_to(campaign_dir).as_posix()} 不存在）")
    report = _read_json(path, "比较收据")
    campaign_id = (manifest or _read_json(campaign_dir / "campaign.json", "Campaign 核心清单")).get("campaign_id")
    if (
        set(report) != STAGE_RESULT_FIELDS
        or report.get("schema_version") != COMPARISON_SCHEMA
        or report.get("client") != "claude"
        or report.get("campaign_id") != campaign_id
        or report.get("stage") != STAGE_NAME
        or report.get("candidate_id") != candidate_id
        or report.get("status") != "complete"
        or report.get("offline_only") is not True
        or not orchestrator._is_rfc3339_timestamp(report.get("compared_at_utc"))
    ):
        raise ConfigurationError("比较收据合同、Campaign、候选或状态不一致。")
    if report.get("package_digest") != _package_digest(report):
        raise ConfigurationError("比较收据核心摘要与 package_digest 不一致（收据被改动）。")
    for label, binding in (
        *((f"比较收据输入 {name}", value) for name, value in sorted((report.get("inputs") or {}).items())),
        ("官方评估用 manifest", (report.get("official") or {}).get("evaluation_manifest")),
        ("候选评估用 manifest", (report.get("candidate") or {}).get("evaluation_manifest")),
        ("builder 配置", (report.get("builder") or {}).get("config")),
    ):
        _bound_campaign_file(campaign_dir, binding, label)
    return report
