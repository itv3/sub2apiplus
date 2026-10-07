"""后继候选的前序封存覆盖证明（B4-01 c3b）。

证明跟随最初生产者，绑定来源 Campaign、首轮阶段结果和封存清单；边界重绑沿来源清单自己的追加链重放。
它只替代已封存根的旧权限收口重放，不改变 Job 身份、relay／恢复报告和批准摘要的校验。前序根始终只读，
后继的权限收口及新封存清单只管理本轮根；阶段结果另登记前序证明，读侧同时重放两种边界。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256

COVER_FIELD = "predecessor_sealed_cover"
STAGE_FIELD = "predecessor_sealed_evidence"
COVER_SCHEMA = "claude-upgrade-predecessor-sealed-cover/v1"


def build_cover(campaign_dir: Path, candidate_id: str, attempt_id: str) -> dict[str, Any]:
    """只给已完整封存的来源构造绑定；半封存仍须在前序补完批准，不能冒充可复用来源。"""

    # 延迟导入：seal、恢复和后继复用都消费本模块，模块装载时不形成环。
    from tools.official_client_capture import claude_upgrade as orchestrator
    from tools.official_client_capture import claude_upgrade_candidate_seal as seal

    campaign_dir = Path(campaign_dir)
    manifest = orchestrator.load_campaign_manifest(campaign_dir)
    path, stage = seal.load_round_one_result(campaign_dir, candidate_id, manifest=manifest)
    root, _attempt = seal.sealed_attempt_context(campaign_dir, stage, candidate_id=candidate_id)
    if root.name != attempt_id:
        raise ConfigurationError("前序封存阶段绑定的不是请求复用的 attempt。")
    return {
        "schema_version": COVER_SCHEMA,
        "campaign": {
            "campaign_id": manifest["campaign_id"], "campaign_dir": str(campaign_dir),
            "campaign_manifest_sha256": file_sha256(campaign_dir / "campaign.json"), "candidate_id": candidate_id,
        },
        "attempt_id": attempt_id,
        "stage_result": seal._stage_binding(path, campaign_dir),
        "sealed_evidence_manifest": dict(stage[seal.SEALED_EVIDENCE_MANIFEST_FIELD]),
    }


def verify_cover(cover: Any, *, required_roots: Sequence[Path] = ()) -> dict[str, Any]:
    """重放不可变来源绑定、封存清单及其元数据重绑链，并确认指定根全部处于该清单覆盖范围。"""

    from tools.official_client_capture import claude_upgrade as orchestrator
    from tools.official_client_capture import claude_upgrade_capture as capture
    from tools.official_client_capture import claude_upgrade_candidate_seal as seal

    if not isinstance(cover, Mapping) or set(cover) != {
        "schema_version", "campaign", "attempt_id", "stage_result", "sealed_evidence_manifest"
    } or cover.get("schema_version") != COVER_SCHEMA:
        raise ConfigurationError("前序封存覆盖证明合同非法。")
    origin = cover["campaign"]
    if not isinstance(origin, Mapping) or set(origin) != {
        "campaign_id", "campaign_dir", "campaign_manifest_sha256", "candidate_id"
    }:
        raise ConfigurationError("前序封存覆盖证明的 Campaign 绑定非法。")
    directory = Path(str(origin["campaign_dir"]))
    if not directory.is_absolute() or directory.is_symlink() or directory.resolve() != directory:
        raise ConfigurationError("前序封存覆盖证明必须引用规范绝对 Campaign 路径。")
    candidate_id, attempt_id = str(origin["candidate_id"]), str(cover["attempt_id"])
    if not capture.SAFE_ID_RE.fullmatch(candidate_id) or not capture.SAFE_ID_RE.fullmatch(attempt_id):
        raise ConfigurationError("前序封存覆盖证明的候选或 attempt 坐标非法。")
    manifest_path = directory / "campaign.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or file_sha256(manifest_path) != origin["campaign_manifest_sha256"]:
        raise ConfigurationError("前序封存覆盖证明的 Campaign 总计划缺失或摘要漂移。")
    manifest = orchestrator.load_campaign_manifest(directory)
    if manifest["campaign_id"] != origin["campaign_id"]:
        raise ConfigurationError("前序封存覆盖证明的 Campaign 身份不一致。")
    key = ("candidate", candidate_id, attempt_id)
    if key in capture._invalidated_attempts(directory) or key in orchestrator._conflict_quarantined_attempts(directory):
        raise ConfigurationError("前序封存覆盖证明的来源已被隔离作废。")
    if capture._attempt_evolution_reconciled(directory, attempt_id):
        raise ConfigurationError("前序封存覆盖证明的来源已按工具演进作废对账。")
    if orchestrator._campaign_contamination_records(directory, _manifest=manifest):
        raise ConfigurationError("前序封存覆盖证明的 Campaign 有未隔离污染。")
    # 只读首轮：后续验收轮次不能改变已有覆盖证明的内容身份。
    stage_path, stage = seal.load_round_one_result(directory, candidate_id, manifest=manifest)
    if seal._stage_binding(stage_path, directory) != cover["stage_result"]:
        raise ConfigurationError("前序封存覆盖证明的首轮阶段结果缺失或摘要漂移。")
    root = seal._attempt_root(directory, candidate_id, attempt_id)
    if stage["attempt"]["path"] != (root / "attempt.json").relative_to(directory).as_posix():
        raise ConfigurationError("前序封存覆盖证明的阶段结果没有绑定该 attempt。")
    binding = stage[seal.SEALED_EVIDENCE_MANIFEST_FIELD]
    if binding != cover["sealed_evidence_manifest"]:
        raise ConfigurationError("前序封存覆盖证明的封存清单绑定不一致。")
    path = orchestrator._campaign_file(directory, str(binding["path"]))
    if path != root / seal.SEALED_EVIDENCE_MANIFEST_FILENAME:
        raise ConfigurationError("前序封存覆盖证明的清单不在来源 attempt 根。")
    # 不递归消费来源阶段附带的其它前序证明：本证明只承诺本清单实际覆盖的根。
    document = seal._verify_sealed_manifest(
        path, seal._sealed_evidence_roots(directory, attempt_id, candidate_id), attempt_id=attempt_id, candidate_id=candidate_id
    )
    roots = [Path(str(row["path"])) for row in document["roots"]]
    for required in required_roots:
        required = Path(required)
        if not required.is_absolute() or required.is_symlink() or required.resolve() != required or not required.exists():
            raise ConfigurationError(f"前序封存覆盖的证据根不可信：{required}")
        if not any(required == base or (base.is_dir() and required.is_relative_to(base)) for base in roots):
            raise ConfigurationError(f"前序封存清单未覆盖要求的证据根：{required}")
    return document


def stage_evidence(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """从已绑定 attempt 的复用结果确定性提取前序证明，供阶段结果及读侧比对。"""

    entries = []
    for result in results:
        reused = result.get("reused_from")
        producer = reused.get("producer") if isinstance(reused, Mapping) else None
        if not isinstance(producer, Mapping) or COVER_FIELD not in producer:
            continue
        if result.get("disposition") != "reused" or result.get("status") != "complete":
            raise ConfigurationError("前序封存覆盖证明只能绑定已完成的复用结果。")
        if not isinstance(result.get("evidence_roots"), list) or not result["evidence_roots"]:
            raise ConfigurationError("前序封存覆盖证明缺少复用证据根。")
        entries.append({"job_id": str(result["id"]), "producer": dict(producer), "evidence_roots": list(result["evidence_roots"])})
    if len({entry["job_id"] for entry in entries}) != len(entries):
        raise ConfigurationError("前序封存证据登记包含重复 Job。")
    return sorted(entries, key=lambda entry: entry["job_id"])


def verified_manifests(entries: Any, layout: Any) -> list[dict[str, Any]]:
    """复核阶段登记的每份证明与其证据根；返回清单供本轮封存与深度扫描逐文件比对。"""

    from tools.official_client_capture import claude_upgrade_capture as capture

    if not isinstance(entries, list):
        raise ConfigurationError("predecessor_sealed_evidence 必须是数组。")
    documents = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"job_id", "producer", "evidence_roots"}:
            raise ConfigurationError("前序封存证据登记字段非法。")
        if not isinstance(entry["job_id"], str) or entry["job_id"] in seen or not capture.SAFE_ID_RE.fullmatch(entry["job_id"]):
            raise ConfigurationError("前序封存证据登记的 Job 非法或重复。")
        seen.add(entry["job_id"])
        producer, roots = entry["producer"], entry["evidence_roots"]
        if not isinstance(producer, Mapping) or COVER_FIELD not in producer or not isinstance(roots, list) or not roots:
            raise ConfigurationError("前序封存证据登记缺少生产者证明或证据根。")
        documents.append(verify_cover(producer[COVER_FIELD], required_roots=[capture._host_path(str(root), layout) for root in roots]))
    return documents


def closeout_roots(results: Sequence[Mapping[str, Any]], layout: Any, *, owned_roots: Sequence[Path] = ()) -> list[str]:
    """后继收口只接收未由前序封存覆盖的根；有证明时先验证，拒绝把无证明的根静默排除。"""

    from tools.official_client_capture import claude_upgrade_capture as capture

    retained = set()
    protected: set[Path] = set()
    for result in results:
        roots = [*(result.get("evidence_roots") or []), *(result.get("archived_evidence_roots") or [])]
        entries = stage_evidence([result])
        if entries:
            document = verify_cover(entries[0]["producer"][COVER_FIELD], required_roots=[capture._host_path(str(root), layout) for root in roots])
            protected.update(Path(str(root["path"])) for root in document["roots"])
        else:
            retained.update(str(root) for root in roots)
    # 另一个执行结果不能把前序根（或其父／子目录）再次带进收口；发现重叠直接失败，绝不靠删根掩盖冲突。
    for root in [*owned_roots, *(capture._host_path(value, layout) for value in retained)]:
        if any(root == base or root.is_relative_to(base) or base.is_relative_to(root) for base in protected):
            raise ConfigurationError(f"后继权限收口根与前序封存根重叠：{root}")
    return sorted(retained)
