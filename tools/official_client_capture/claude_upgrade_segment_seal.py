"""Claude 恢复段的增量封存与合并证据读侧（B4-05b-3）。

原 attempt、原 Mac／边界收据和旧阶段结果均只读；当前基线新写 seal 代、有效 Job 清单及新 ValidationAttempt。
封存证据按整根投影保留旧 Job，再扫描本段新增根并合并。批准被打断后由同代预览与 Store 幂等动作继续。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_attempt_recovery as reader
from tools.official_client_capture import claude_upgrade_attempt_segment as segment_run
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_candidate_seal as seal
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import claude_upgrade_seal as official_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_evidence_manifest as boundary
from tools.official_client_capture import upgrade_control_plane as control
from tools.official_client_capture.capturelib.model import ConfigurationError

EFFECTIVE_SCHEMA = "claude-upgrade-effective-results/v1"
RECOVERY_STAGE_FIELDS = frozenset({"recovery", "previous_stage", "effective_results", "results", "root_sets"})
RELAY_VIEW_FILENAME = "relay-view.json"
SEGMENT_BOUNDARY_FILENAME = "sealed-boundary.json"


def _binding(campaign_dir: Path, path: Path) -> dict[str, Any]:
    return seal._stage_binding(path, campaign_dir)


def _bound(campaign_dir: Path, value: Any, label: str) -> Path:
    """阶段绑定只有路径与摘要，seal 产物另有字节数；两种形状都严格验证，不能把缺字节数视为零。"""

    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} 文件绑定不是对象。")
    upgrade._require_file_binding({key: value.get(key) for key in ("path", "sha256")}, label)
    path = upgrade._campaign_file(campaign_dir, str(value["path"]))
    upgrade._reject_symlink_components(path, campaign_dir, label)
    if not path.is_file() or upgrade.file_sha256(path) != value["sha256"] or ("bytes" in value and path.stat().st_size != value["bytes"]):
        raise ConfigurationError(f"{label} 缺失或摘要／字节数漂移。")
    return path


def _results(stage: Mapping[str, Any], attempt: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = stage.get("results", attempt.get("results", []))
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise ConfigurationError("候选合并结果必须是 Job 数组。")
    result = {str(row.get("id")): dict(row) for row in rows}
    if len(result) != len(rows) or any(row.get("status") != "complete" for row in result.values()):
        raise ConfigurationError("候选有效结果存在重复或未完成 Job。")
    return result


def _effective_jobs(source_jobs: Sequence[Any], results: Mapping[str, Mapping[str, Any]], revision: str) -> list[Any]:
    """每条结果的执行摘要必须匹配原计划或合法段重定位之一，之后的 relay／恢复报告按实际生产段复核。"""

    found = []
    with segment_run._scope():
        for job in source_jobs:
            result = results[job.job_id]
            choices = [job, *(control._relocate_job_for_recovery(job, f"ar{number}") for number in range(1, int(revision[2:]) + 1))]
            matching = [item for item in choices if upgrade._job_execution_sha256(item) == result.get("execution_sha256")]
            if len(matching) != 1:
                raise ConfigurationError(f"有效结果 {job.job_id} 的执行摘要不属于原计划或合法恢复段。")
            found.append(matching[0])
    return found


def rebound_projection_source(path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """整根投影前应用已验证的重绑链末 stat；内容摘要沿用原清单，不重新读取保留根正文。"""

    source = boundary.validate_manifest_document(manifest)
    chain = boundary.load_boundary_rebinds(path, source)
    if not chain:
        return source
    view = chain[-1]["new_boundary"]
    digests = {row["path"]: row["sha256"] for row in source["entries"]}
    value = {**source, "roots": [dict(row) for row in view["roots"]],
             "entries": [{**row, "sha256": digests[row["path"]]} for row in view["entries"]],
             "metadata_sha256": upgrade._fingerprint(view)}
    value.pop("manifest_digest")
    value["manifest_digest"] = upgrade._fingerprint(value)
    return boundary.validate_manifest_document(value)


def prepare_context(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, attempt_id: str, revision: str) -> dict[str, Any]:
    """只读重放当前授权、前序封存与成功段，合成唯一有效结果；任何来源漂移都拒绝生成预览。"""

    baseline, commit, recovery, _preview = segment_run.current_baseline(campaign_dir, candidate_id, revision, allow_successor=True)
    if attempt_id != recovery["attempt_id"]:
        raise ConfigurationError("--attempt-id 与当前恢复裁定不一致。")
    previous_path, previous = segment_run.previous_stage(campaign_dir, candidate_id, int(recovery["previous_baseline"]), manifest)
    original_root, original = seal.sealed_attempt_context(campaign_dir, previous, candidate_id=candidate_id)
    if original_root.name != attempt_id:
        raise ConfigurationError("前序封存结果不是当前裁定的原 attempt。")
    segment_root, reservation, summary = reader.load_segment(campaign_dir, candidate_id, attempt_id, revision)
    if summary.get("status") != upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS or upgrade._failed_job_ids(summary.get("results")):
        raise ConfigurationError("恢复段必须成功收口才能增量封存。")
    if summary.get("identity") != original.get("identity"):
        raise ConfigurationError("恢复段与原 attempt 的候选身份漂移。")
    prior_results = _results(previous, original)
    recovered = _results({"results": summary["results"]}, summary)
    execute, retained = set(recovery["execute_jobs"]), set(recovery["reuse_jobs"])
    if set(recovered) != execute or execute & retained or execute | retained != set(prior_results):
        raise ConfigurationError("段结果与裁定 J* 或原 Job 全集不一致。")
    effective = {job: (recovered[job] if job in execute else prior_results[job]) for job in sorted(prior_results)}
    source_jobs = upgrade._campaign_jobs(campaign_dir, manifest, "candidate", candidate_id=candidate_id)
    if set(effective) != {job.job_id for job in source_jobs}:
        raise ConfigurationError("合并结果没有精确覆盖当前候选计划。")
    jobs = _effective_jobs(source_jobs, effective, revision)
    seal._attempt_results({"results": list(effective.values())}, jobs)
    previous_boundary_path = _bound(campaign_dir, previous[seal.SEALED_EVIDENCE_MANIFEST_FIELD], "前序封存证据清单")
    previous_boundary = rebound_projection_source(previous_boundary_path, upgrade._read_json(previous_boundary_path, "前序封存证据清单"))
    layout = capture._capture_layout(campaign_dir, manifest)
    old_job_roots = {str(capture._host_path(str(value), layout)) for job in execute for value in prior_results[job]["evidence_roots"]}
    keep_roots = sorted(str(row["path"]) for row in previous_boundary["roots"] if str(row["path"]) not in old_job_roots)
    inherited = set(keep_roots)
    delta_roots = {str(segment_root / "evidence"), str(segment_root / "logs")}
    delta_roots.update(str(capture._host_path(str(value), layout)) for row in recovered.values() for value in row["evidence_roots"])
    delta_roots -= inherited
    context = {
        "baseline": baseline, "commit": commit, "recovery": recovery, "revision": revision,
        "previous_path": previous_path, "previous": previous, "previous_boundary": previous_boundary,
        "previous_boundary_path": previous_boundary_path,
        "original_root": original_root, "original": original, "segment_root": segment_root,
        "reservation": reservation, "summary": summary, "results": effective, "jobs": jobs,
        "layout": layout, "data_root": Path(layout.host_data_root).resolve(strict=True),
        "root_sets": {"retained_roots": keep_roots, "dropped_roots": sorted(old_job_roots), "delta_roots": sorted(delta_roots),
                      "final_roots": sorted(inherited | delta_roots)},
        "store_capture_attempt_id": seal._safe_id(f"{attempt_id}-{revision}", "恢复段 ValidationAttempt 前缀"),
        "execute_item_ids": sorted(reservation.get("execute_job_ids", execute)),
        "reuse_item_ids": sorted(retained | set(reservation.get("reuse_job_ids", []))),
    }
    seal.validation_attempt_id(context["store_capture_attempt_id"], seal.FIRST_ROUND)
    return context


def seal_segment(campaign_dir: Path, manifest: dict[str, Any], *, candidate_id: str, attempt_id: str, revision: str,
                 approve: str | None, approved_by: str | None, gate_overrides: Mapping[str, Mapping[str, str]] | None = None) -> tuple[dict[str, Any], int]:
    """增量 seal 的预览／批准入口；批准摘要绑定原 attempt、恢复段、前序阶段与保留／替换根集合。"""

    upgrade._reject_contaminated_campaign(campaign_dir)
    context = prepare_context(campaign_dir, manifest, candidate_id, attempt_id, revision)
    stage_path = upgrade._stage_write_target(campaign_dir, candidate_id, context["baseline"], "capture-candidate")
    current_tool = upgrade._tool_identity(include_git=False)
    upgrade._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action="capture-candidate seal --attempt-recovery")
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    tool_digest = official_seal._require_store_tool_current(store, store_campaign)
    candidate_context = seal._candidate_context(store, store_campaign, manifest, candidate_id)
    labels = official_seal._frozen_input(campaign_dir, manifest, "evidence_labels")
    scenarios_path = official_seal._frozen_input(campaign_dir, manifest, "target_scenarios")
    scenarios = claude_upgrade_plan.read_scenario_manifest(scenarios_path, expected_version=str(manifest["target_version"]),
                                                          rules=tuple(manifest["required_rules"]), verify_source_spec=False)
    client_files = {group: {item: _bound(campaign_dir, binding, f"继承 {item} 收据") for item, binding in values.items()}
                    for group, values in context["previous"]["client_evidence"].items()}
    attempt_path = context["original_root"] / "attempt.json"
    summary_path = context["segment_root"] / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
    generation_key = {
        "candidate_id": candidate_id, "attempt": seal._relative_binding(attempt_path, context["data_root"]),
        "recovery": _binding(campaign_dir, summary_path), "previous_stage": _binding(campaign_dir, context["previous_path"]),
        "baseline_commit_sha256": context["commit"]["commit_sha256"], "root_sets": context["root_sets"],
        "store_campaign_identity_sha256": store_campaign["identity_sha256"], "tool_files_sha256": tool_digest,
        "evidence_semantics_sha256": current_tool["evidence_semantics_sha256"], "scenario_plan_ref": candidate_context["scenario_plan_ref"],
        "evidence_labels": seal._relative_binding(labels, context["data_root"]),
        "target_scenarios": seal._relative_binding(scenarios_path, context["data_root"]),
        "client_evidence": context["previous"]["client_evidence"],
    }
    if gate_overrides is not None:
        generation_key["gate_execution_overrides"] = {key: dict(value) for key, value in sorted(gate_overrides.items())}
    generation = official_seal._generation(generation_key)
    seal_dir = stage_path.parent / "seal" / generation
    effective = {
        "schema_version": EFFECTIVE_SCHEMA, "client": "claude", "campaign_id": manifest["campaign_id"], "candidate_id": candidate_id,
        "attempt_id": attempt_id, "recovery_revision": revision, "evaluation_baseline": context["baseline"],
        "recovery_sha256": context["recovery"]["recovery_sha256"], "previous_stage": generation_key["previous_stage"],
        "segment": generation_key["recovery"], "results": list(context["results"].values()),
    }
    effective["effective_results_sha256"] = upgrade._fingerprint(effective)
    effective_path = seal_dir / "effective-results.json"
    official_seal._write_once_or_reuse(effective_path, effective)
    context["effective_binding"] = _binding(campaign_dir, effective_path)
    if approve is not None:
        result = seal._approve_candidate_seal(campaign_dir, manifest, seal_dir=seal_dir, generation=generation, approve=approve,
            approved_by=str(approved_by), jobs=context["jobs"], attempt_path=attempt_path, stage_path=stage_path,
            candidate_id=candidate_id, data_root=context["data_root"], segment_context=context)
        return result, 0
    return seal._preview_candidate_seal(
        campaign_dir, manifest, candidate_id=candidate_id, attempt=context["original"], attempt_path=attempt_path,
        jobs=context["jobs"], results=context["results"], gateway=seal._attempt_candidate_context(context["summary"]),
        layout=context["layout"], data_root=context["data_root"], context=candidate_context, scenarios=scenarios,
        labels_path=labels, scenarios_path=scenarios_path, client_files=client_files, generation=generation,
        generation_key=generation_key, seal_dir=seal_dir, segment_context=context,
    ), 2


def relay_view(campaign_dir: Path, context: Mapping[str, Any], jobs: Sequence[Any], roots: Sequence[Path], data_root: Path) -> dict[str, Any]:
    """保留 Job 取前次封存的解析视图；只有当前 J* 的根重新解析。早期没有视图快照的封存先只读兼容解析。"""

    replaced = set(context["recovery"]["execute_jobs"])
    delta = upgrade.scan_evidence([root for job, root in zip(jobs, roots) if job.job_id in replaced], evidence.CANDIDATE_SIDE, base=data_root)
    draft = upgrade._read_json(_bound(campaign_dir, context["previous"]["seal"]["seal_draft"], "前序 seal 草稿"), "前序 seal 草稿")
    if draft.get("relay_view") is not None:
        previous = upgrade._read_json(_bound(campaign_dir, draft["relay_view"], "前序解析视图"), "前序解析视图")
    else:
        previous = upgrade.scan_evidence([root for job, root in zip(jobs, roots) if job.job_id not in replaced], evidence.CANDIDATE_SIDE, base=data_root)
    probes = dict(delta["probes"])
    for job in jobs:
        if job.job_id not in replaced:
            for step in job.steps:
                probe = str(step["probe_id"])
                if probe not in previous["probes"] or probe in probes:
                    raise ConfigurationError("前序解析视图缺少保留 probe 或与新 probe 重叠。")
                probes[probe] = previous["probes"][probe]
    surfaces: Counter[str] = Counter()
    for probe in probes.values():
        for connection in probe["connections"]:
            host = probe["manifest"]["upstream_host"]
            for line in connection.get("requests", []):
                surfaces[f"{host} {line}"] += 1
    # scan_relay_evidence 的观察面按已解析连接中的请求头取值；没有请求的连接不产生观察面。
    return {"label": evidence.CANDIDATE_SIDE, "probes": dict(sorted(probes.items())),
            "surfaces": [{"surface": key, "count": value} for key, value in sorted(surfaces.items())]}


def incremental_inventory(campaign_dir: Path, context: Mapping[str, Any], roots: Sequence[Path], data_root: Path) -> tuple[dict[str, Any], int]:
    """旧清单只保留有效路径，新增路径深度扫描一次；返回合并清单及实际新读字节数。"""

    previous = upgrade._read_json(_bound(campaign_dir, context["previous"]["seal"]["evidence_manifest"], "前序深度扫描清单"), "前序深度扫描清单")
    index = {str(row["path"]): dict(row) for row in previous["entries"]}
    selected: dict[str, dict[str, Any]] = {}
    delta = []
    for root in roots:
        relative = root.relative_to(data_root).as_posix()
        old = {key: value for key, value in index.items() if key == relative or key.startswith(relative + "/")}
        if old:
            selected.update(old)
        else:
            delta.append(root)
    scanned = upgrade._evidence_inventory(delta, base=data_root)
    for row in scanned["entries"]:
        old = selected.get(row["path"])
        if old is not None and old != row:
            raise ConfigurationError("合并深度扫描清单出现同路径不同内容。")
        selected[row["path"]] = row
    entries = [selected[key] for key in sorted(selected)]
    return {"schema_version": evidence.EVIDENCE_INVENTORY_SCHEMA, "entry_count": len(entries),
            "total_bytes": sum(row["bytes"] for row in entries), "entries": entries,
            "digest": upgrade._fingerprint({"entries": entries})}, int(scanned["total_bytes"])


def seal_boundary(campaign_dir: Path, context: Mapping[str, Any], seal_dir: Path, inventory: Mapping[str, Any], job_roots: Sequence[str]) -> Path:
    """整根投影旧边界、扫描新增根、合并，并与预览的逐文件清单核对；半封存再次批准只重放边界。"""

    path = seal_dir / "sealed-evidence-manifest.json"
    roots = [Path(value) for value in context["root_sets"]["final_roots"]]
    if path.exists():
        manifest = seal._verify_sealed_manifest(path, roots, attempt_id=str(context["original"]["attempt_id"]), candidate_id=str(context["original"]["candidate_id"]))
    else:
        try:
            if context.get("previous_boundary_path") is not None:
                source_path = context["previous_boundary_path"]
                original_source = upgrade._read_json(source_path, "前序封存清单")
                official_seal._write_once_or_reuse(seal_dir / "projection-source.json", {
                    "source": _binding(campaign_dir, source_path),
                    "boundary_rebinds": boundary.load_boundary_rebinds(source_path, original_source),
                    "effective_manifest": context["previous_boundary"],
                })
            projected, projection = boundary.project_evidence_manifest(context["previous_boundary"], keep_roots=context["root_sets"]["retained_roots"])
            delta = boundary.build_evidence_manifest([Path(value) for value in context["root_sets"]["delta_roots"]],
                checkpoint_path=seal_dir / seal.SEALED_EVIDENCE_CHECKPOINT_FILENAME, secret_env_names=())
            manifest = boundary.merge_evidence_manifests(projected, delta)
            boundary.verify_manifest_boundary(manifest, roots)
        except boundary.EvidenceManifestError as error:
            raise ConfigurationError(f"恢复段封存边界合并失败：{error}") from error
        official_seal._write_once_or_reuse(seal_dir / "manifest-projection.json", projection)
        official_seal._write_once_or_reuse(path, manifest)
    seal._check_sealed_manifest_content(manifest, inventory, data_root=context["data_root"], job_roots=job_roots)
    official_seal._write_once_or_reuse(context["segment_root"] / SEGMENT_BOUNDARY_FILENAME, {
        "schema_version": "claude-upgrade-segment-boundary/v1", "candidate_id": context["original"]["candidate_id"],
        "attempt_id": context["original"]["attempt_id"], "recovery_revision": context["revision"],
        "summary": _binding(campaign_dir, context["segment_root"] / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME),
        "manifest": _binding(campaign_dir, path),
    })
    return path


def stage_fields(campaign_dir: Path, context: Mapping[str, Any]) -> dict[str, Any]:
    summary_path = context["segment_root"] / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
    return {
        "recovery": {**_binding(campaign_dir, summary_path), "recovery_revision": context["revision"],
                     "evaluation_baseline": context["baseline"], "recovery_sha256": context["recovery"]["recovery_sha256"]},
        "previous_stage": _binding(campaign_dir, context["previous_path"]), "effective_results": context["effective_binding"],
        "results": list(context["results"].values()), "root_sets": context["root_sets"],
    }


def validate_stage(campaign_dir: Path, stage: Mapping[str, Any], candidate_id: str) -> tuple[Path, dict[str, Any]]:
    """读合并结果时重放前序阶段、当前段、有效结果和边界；Mac／boundary 绑定必须逐字承接原阶段。"""

    if not RECOVERY_STAGE_FIELDS <= set(stage):
        raise ConfigurationError("恢复段阶段结果缺少合并证据字段。")
    recovery_binding = stage["recovery"]
    summary_path = _bound(campaign_dir, recovery_binding, "恢复段摘要")
    revision = reader.require_recovery_revision(recovery_binding.get("recovery_revision"))
    original_path = _bound(campaign_dir, stage["attempt"], "原 attempt")
    original_id = original_path.parent.name
    segment_root, reservation, summary = reader.load_segment(campaign_dir, candidate_id, original_id, revision)
    if summary_path != segment_root / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME or summary.get("status") != upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS:
        raise ConfigurationError("阶段结果绑定的恢复段不是成功段或路径不规范。")
    baseline, commit, recovery = reader.authoritative_execute_jobs(campaign_dir, candidate_id, reservation)
    if recovery_binding.get("evaluation_baseline") != baseline or recovery_binding.get("recovery_sha256") != recovery["recovery_sha256"]:
        raise ConfigurationError("阶段结果的恢复基线授权不一致。")
    source = upgrade._stage_read_source(campaign_dir, candidate_id, int(recovery["previous_baseline"]), "capture-candidate")
    previous_path = _bound(campaign_dir, stage["previous_stage"], "前序阶段结果")
    if previous_path != source["path"] or source["sha256"] != stage["previous_stage"]["sha256"]:
        raise ConfigurationError("恢复段阶段结果没有引用裁定授权的前序基线。")
    previous = seal.load_stage_result(campaign_dir, previous_path, candidate_id)
    root, original = seal.sealed_attempt_context(campaign_dir, previous, candidate_id=candidate_id)
    if stage["attempt"] != previous["attempt"] or stage["client_evidence"] != previous["client_evidence"]:
        raise ConfigurationError("恢复段必须逐字承接原 attempt 与 Mac／边界收据绑定。")
    document = upgrade._read_json(_bound(campaign_dir, stage["effective_results"], "有效结果"), "有效结果")
    unsigned = {key: value for key, value in document.items() if key != "effective_results_sha256"}
    if (document.get("schema_version") != EFFECTIVE_SCHEMA or document.get("effective_results_sha256") != upgrade._fingerprint(unsigned)
            or document.get("results") != stage["results"] or document.get("previous_stage") != stage["previous_stage"]
            or document.get("segment") != {key: recovery_binding[key] for key in ("path", "sha256")}):
        raise ConfigurationError("恢复段有效结果的摘要或来源绑定漂移。")
    prior_results = _results(previous, original)
    recovered = _results({"results": summary["results"]}, summary)
    expected = {job: (recovered[job] if job in set(recovery["execute_jobs"]) else prior_results[job]) for job in sorted(prior_results)}
    if _results(stage, original) != expected:
        raise ConfigurationError("阶段结果的有效 Job 不是裁定批准的旧证据与当前恢复段组合。")
    if (document.get("client") != "claude" or document.get("candidate_id") != candidate_id
            or document.get("campaign_id") != summary.get("campaign_id") or document.get("attempt_id") != original_id
            or document.get("recovery_revision") != revision or document.get("evaluation_baseline") != baseline
            or document.get("recovery_sha256") != recovery["recovery_sha256"]):
        raise ConfigurationError("有效结果的客户端、候选、原 attempt 或基线身份漂移。")
    layout = capture._capture_layout(campaign_dir, upgrade.load_campaign_manifest(campaign_dir))
    prior_boundary = upgrade._read_json(_bound(campaign_dir, previous[seal.SEALED_EVIDENCE_MANIFEST_FIELD], "前序封存边界"), "前序封存边界")
    dropped = {str(capture._host_path(str(value), layout)) for job in recovery["execute_jobs"] for value in prior_results[job]["evidence_roots"]}
    kept = {str(row["path"]) for row in prior_boundary["roots"]} - dropped
    delta = {str(segment_root / "evidence"), str(segment_root / "logs")}
    delta.update(str(capture._host_path(str(value), layout)) for row in recovered.values() for value in row["evidence_roots"])
    delta -= kept
    wanted_roots = {"retained_roots": sorted(kept), "dropped_roots": sorted(dropped), "delta_roots": sorted(delta), "final_roots": sorted(kept | delta)}
    execute = sorted(reservation.get("execute_job_ids", recovery["execute_jobs"]))
    reused = sorted(set(recovery["reuse_jobs"]) | set(reservation.get("reuse_job_ids", [])))
    validation_round = int(stage.get("validation_round", seal.FIRST_ROUND))
    if (stage["root_sets"] != wanted_roots or stage["execute_item_ids"] != execute or stage["reuse_item_ids"] != reused
            or stage["store"]["validation_attempt_id"] != seal.validation_attempt_id(f"{original_id}-{revision}", validation_round)):
        raise ConfigurationError("恢复段封存根集合、执行／复用项或 ValidationAttempt 不属于批准来源。")
    boundary_path = _bound(campaign_dir, stage[seal.SEALED_EVIDENCE_MANIFEST_FIELD], "合并封存边界")
    if segment_boundary(campaign_dir, segment_root, summary) != boundary_path:
        raise ConfigurationError("恢复段半封存绑定与正式阶段清单不一致。")
    roots = [Path(value) for value in stage["root_sets"]["final_roots"]]
    seal._verify_sealed_manifest(boundary_path, roots, attempt_id=original_id, candidate_id=candidate_id)
    return root, {**original, "results": list(expected.values())}


def segment_boundary(campaign_dir: Path, segment_root: Path, summary: Mapping[str, Any], *, verify: bool = True) -> Path | None:
    """封存／半封存段按只写一次的清单绑定替代运行期收口；绑定摘要与原生段证据根必须完整覆盖。"""

    pointer = segment_root / SEGMENT_BOUNDARY_FILENAME
    if not pointer.exists() and not pointer.is_symlink():
        return None
    upgrade._reject_symlink_components(pointer, campaign_dir, "段封存边界绑定")
    record = upgrade._read_json(pointer, "段封存边界绑定")
    if (record.get("schema_version") != "claude-upgrade-segment-boundary/v1"
            or any(record.get(key) != summary.get(key) for key in ("candidate_id", "attempt_id", "recovery_revision"))
            or _bound(campaign_dir, record.get("summary"), "已封存段摘要") != segment_root / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME):
        raise ConfigurationError("段封存边界绑定的身份或来源摘要不一致。")
    path = _bound(campaign_dir, record.get("manifest"), "段封存清单")
    stage_path = upgrade._stage_write_target(campaign_dir, str(summary["candidate_id"]), int(summary["evaluation_baseline"]), "capture-candidate")
    if path.name != "sealed-evidence-manifest.json" or path.parent.parent != stage_path.parent / "seal":
        raise ConfigurationError("段封存清单不在该基线的 seal 代目录内。")
    manifest = boundary.validate_manifest_document(upgrade._read_json(path, "段封存清单"))
    roots = {str(row["path"]) for row in manifest["roots"]}
    owned = set(str(value) for value in summary["evidence_roots"])
    layout = capture._capture_layout(campaign_dir, upgrade.load_campaign_manifest(campaign_dir))
    owned.update(str(capture._host_path(str(value), layout)) for result in summary["results"] for value in result["evidence_roots"])
    if not owned <= roots:
        raise ConfigurationError("段封存清单没有完整覆盖段收口与有效 Job 根。")
    if verify:
        try:
            seal._verify_sealed_manifest(path, [Path(value) for value in sorted(roots)],
                attempt_id=str(summary["attempt_id"]), candidate_id=str(summary["candidate_id"]))
        except seal.SealedEvidenceBoundaryError as error:
            if error.failure_class == "evidence-metadata-drift":
                error.args = (str(error) + f" --attempt-recovery {summary['recovery_revision']}",)
            raise
    return path


def rebind_boundary(campaign_dir: Path, candidate_id: str, attempt_id: str, revision: str, operator: str) -> dict[str, Any]:
    """恢复段只重绑已冻结的增量清单；逐文件内容不符、路径或权限变化均由共享清单算法拒绝。"""

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    with upgrade._campaign_lock(campaign_dir):
        upgrade._require_quiescent_campaign_lease(campaign_dir, action="rebind-boundary", attempt_id=attempt_id)
        segment_root, reservation, summary = reader.recovery_segment_summary(campaign_dir, candidate_id, attempt_id, revision)
        reader.authoritative_execute_jobs(campaign_dir, candidate_id, reservation)
        if summary is None or summary.get("status") != upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS:
            raise ConfigurationError("只有已成功收口并写入封存清单的恢复段可重绑。")
        path = segment_boundary(campaign_dir, segment_root, summary, verify=False)
        if path is None:
            raise ConfigurationError("恢复段尚未封存，没有可重绑的边界。")
        manifest = upgrade._read_json(path, "段封存清单")
        roots = [Path(row["path"]) for row in manifest["roots"]]
        try:
            chain = boundary.load_boundary_rebinds(path, manifest)
            payload = boundary.build_boundary_rebind(manifest, path, roots, existing_rebinds=chain,
                campaign_id=str(summary["campaign_id"]), phase="candidate", candidate_id=candidate_id,
                attempt_id=f"{attempt_id}.{revision}", operator=operator)
            receipt = path.parent / f"evidence-manifest-rebind-{payload['index']:02d}.json"
            upgrade._secure_write_json_once(receipt, payload)
            segment_boundary(campaign_dir, segment_root, summary)
        except boundary.EvidenceManifestError as error:
            raise ConfigurationError(f"恢复段证据边界重绑失败：{error}") from error
        return {"status": "rebound", "candidate_id": candidate_id, "attempt_id": attempt_id,
                "recovery_revision": revision, "rebind_receipt": _binding(campaign_dir, receipt),
                "manifest_digest": payload["manifest_digest"], "drifted_entry_count": len(payload["drifted_entries"])}


def audit_segments(campaign_dir: Path, candidate_id: str, stage: Mapping[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
    """审计当前有效证据实际来自的每个段，包含失败前段里保留下来的成功 Job，不把被替换段误作有效来源。"""

    revision = reader.require_recovery_revision(stage["recovery"]["recovery_revision"])
    original_id = Path(stage["attempt"]["path"]).parent.name
    source_jobs = upgrade._campaign_jobs(campaign_dir, upgrade.load_campaign_manifest(campaign_dir), "candidate", candidate_id=candidate_id)
    results = _results(stage, {})
    required = {revision}
    with segment_run._scope():
        for job in source_jobs:
            for number in range(1, int(revision[2:]) + 1):
                source_revision = f"ar{number}"
                if upgrade._job_execution_sha256(control._relocate_job_for_recovery(job, source_revision)) == results[job.job_id]["execution_sha256"]:
                    required.add(source_revision)
    rows = []
    for source_revision in sorted(required, key=lambda value: int(value[2:])):
        root, _reservation, summary = reader.load_segment(campaign_dir, candidate_id, original_id, source_revision)
        rows.append((root, {**summary, "attempt_id": f"{original_id}.{source_revision}"}))
    return rows
