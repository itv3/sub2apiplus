"""只读审计一份 official attempt：身份、账号、模型、环境恢复链、证据完整性。

用途：0.154 现场有两份 29/29 complete 但停在 ``awaiting_receipts`` 的 attempt，
复用它们的证据之前必须证明五件事，而不是只看 Job 状态：

1. **身份**：attempt 冻结的官方产物身份十项与 Campaign ``official_identity`` 逐项
   相等，二进制验证收据 ``passed`` 且与 attempt 内嵌副本一致。
2. **账号**：reservation 绑定的 Campaign 清单摘要、attempt ID、阶段与账号坐标一致。
3. **模型**：声明 ``required_model_receipt`` 的 Job 重放模型条件收据（字段闭合、
   ``model_fallback`` 为假、观测模型等于目标模型、证据绑定逐文件可复算）；所有
   Job 再从 provenance v2 的逐请求记录核对线上模型：用户线程只能出现目标模型
   与受控目录（``comp-hash-catalog.json``／``model-downshift-catalog.json``）声明
   的第二模型，第二模型必须等于 Campaign 冻结的 Lite 模型；官方客户端自发的
   系统线程（``thread_source == system``）模型只记录不判定；缺少 ``model`` 字段的
   模型端点请求判失败。最后与各轨道由收据冻结的模型目录交叉验证，可选地与
   调用方给出的期望轨道模型对照。
4. **环境恢复链**：before／after 探针、ARM64 前后收据、恢复报告五份绑定文件存在、
   大小与摘要一致，前后 ARM64 收据按原 producer 重放后，真实依赖的版本化投影连续。
5. **证据完整性**：checkpoint 链由 ``CheckpointStore.records`` 重放（序号连续、
   前序摘要、自摘要），尾部与 attempt 的 ``job_checkpoint`` 一致；对全部
   evidence roots 生成逐文件 inventory（realpath、大小、SHA-256）。

请求核算直接复用 provenance v2；权限现状只统计不修改。本模块不写任何证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import (
    codex_upgrade_live_request_provenance as provenance,
)
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm64
from tools.official_client_capture import incremental_recovery

SCHEMA_VERSION = "official-attempt-audit/v1"
IDENTITY_FIELDS = (
    "architecture",
    "binary_sha256",
    "cargo_lock_sha256",
    "cli_version",
    "git_commit",
    "operating_system",
    "package",
    "runtime_image",
    "source_tree_sha256",
    "tls_dependencies_sha256",
)
ENVIRONMENT_ROLES = (
    "before_probe",
    "after_probe",
    "arm64_before_receipt",
    "arm64_after_receipt",
    "restoration_report",
)
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "job_id",
        "run_id",
        "track",
        "model_id",
        "models_response_sha256",
        "use_responses_lite",
        "model_fallback",
        "observed_request_models",
        "evidence_root",
        "evidence_bindings",
    }
)
MAX_INVENTORY_FILE_BYTES = 4 * 1024 * 1024 * 1024


class AttemptAuditError(ValueError):
    """审计输入不可信或结构非法；审计结论本身以收据 status 表达。"""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _file_sha256_nofollow(path: Path) -> tuple[int, str]:
    """对证据文件做摘要：拒绝符号链接，不要求属主（pcap 由 tcpdump 降权写出）。"""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AttemptAuditError(f"inventory 遇到非普通文件：{path}")
        if metadata.st_size > MAX_INVENTORY_FILE_BYTES:
            raise AttemptAuditError(f"inventory 文件过大：{path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return metadata.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _binding_file(base: Path, binding: Any, label: str) -> tuple[Path, dict[str, Any]]:
    """校验 ``{path, sha256, bytes}`` 形式的绑定并返回落地文件。"""

    if not isinstance(binding, Mapping):
        raise AttemptAuditError(f"{label} 绑定不是对象")
    relative = str(binding.get("path", ""))
    if not relative or relative.startswith("/") or ".." in relative.split("/"):
        raise AttemptAuditError(f"{label} 绑定路径非法：{relative!r}")
    target = base / relative
    if target.is_symlink() or not target.is_file():
        raise AttemptAuditError(f"{label} 绑定文件不存在：{target}")
    size, digest = _file_sha256_nofollow(target)
    report = {
        "path": str(target),
        "bytes": size,
        "sha256": digest,
        "bytes_match": binding.get("bytes") in (None, size),
        "sha256_match": binding.get("sha256") == digest,
    }
    return target, report


def _check_identity(attempt: Mapping[str, Any], manifest: Mapping[str, Any], attempt_root: Path) -> dict[str, Any]:
    identity = attempt.get("identity")
    official = manifest.get("official_identity")
    if not isinstance(identity, Mapping) or not isinstance(official, Mapping):
        raise AttemptAuditError("attempt 或 Campaign 缺少身份对象")
    mismatched = [field for field in IDENTITY_FIELDS if identity.get(field) != official.get(field)]
    embedded = attempt.get("binary_verification")
    on_disk, _raw = closeout._load_json(
        attempt_root / "official-binary-verification.json", "二进制验证收据"
    )
    passed = (
        isinstance(embedded, Mapping)
        and embedded.get("passed") is True
        and dict(embedded) == dict(on_disk)
        and embedded.get("expected_sha256") == official.get("binary_sha256")
        and embedded.get("expected_version") == manifest.get("target_version")
    )
    return {
        "passed": not mismatched and passed,
        "mismatched_fields": mismatched,
        "binary_verification_passed": bool(passed),
        "cli_version": identity.get("cli_version"),
        "binary_sha256": identity.get("binary_sha256"),
    }


def _check_account(
    manifest: Mapping[str, Any], raw_manifest: bytes, attempt_root: Path, attempt_id: str
) -> dict[str, Any]:
    reservation, _raw = closeout._load_json(attempt_root / "reservation.json", "reservation")
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise AttemptAuditError("Campaign 缺少 configuration")
    checks = {
        "attempt_id": reservation.get("attempt_id") == attempt_id,
        "campaign_id": reservation.get("campaign_id") == manifest.get("campaign_id"),
        "campaign_manifest_sha256": reservation.get("campaign_manifest_sha256") == _sha256(raw_manifest),
        "phase": reservation.get("phase") == "official",
        "campaign_mode": reservation.get("campaign_mode") == "formal",
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "codex_account_id": configuration.get("codex_account_id"),
        "api_key_id": configuration.get("api_key_id"),
        "run_nonce": reservation.get("run_nonce"),
    }


def _replay_model_receipt(
    result: Mapping[str, Any],
    *,
    capture_root: Path,
    host_data_root: Path,
) -> dict[str, Any]:
    binding = result.get("model_condition_receipt")
    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
        return {"passed": False, "reason": "缺少模型条件收据绑定"}
    host_path = closeout._map_container_evidence_root(
        binding["path"], capture_root=capture_root, host_data_root=host_data_root
    )
    payload, raw = closeout._load_json(host_path, "模型条件收据")
    problems: list[str] = []
    if _sha256(raw) != binding.get("sha256"):
        problems.append("收据摘要与 attempt 绑定不一致")
    if set(payload) != RECEIPT_FIELDS:
        problems.append("收据字段不闭合")
    expected = {
        "status": "success",
        "job_id": result.get("id"),
        "track": result.get("track"),
        "model_id": result.get("model_id"),
        "use_responses_lite": result.get("expected_use_responses_lite"),
        "model_fallback": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            problems.append(f"收据 {key} 不一致")
    if payload.get("observed_request_models") != [result.get("model_id")]:
        problems.append("observed_request_models 不等于目标模型")
    evidence_root = closeout._map_container_evidence_root(
        payload.get("evidence_root", "/"), capture_root=capture_root, host_data_root=host_data_root
    )
    bindings = payload.get("evidence_bindings")
    binding_reports: list[dict[str, Any]] = []
    if not isinstance(bindings, list) or not bindings:
        problems.append("收据 evidence_bindings 为空")
    else:
        for item in bindings:
            try:
                _target, report = _binding_file(evidence_root, item, "模型条件收据证据")
            except AttemptAuditError as error:
                problems.append(str(error))
                continue
            report["roles"] = item.get("roles") if isinstance(item, Mapping) else None
            if not (report["sha256_match"] and report["bytes_match"]):
                problems.append(f"收据证据绑定漂移：{report['path']}")
            binding_reports.append(report)
    return {
        "passed": not problems,
        "problems": problems,
        "receipt_path": str(host_path),
        "models_response_sha256": payload.get("models_response_sha256"),
        "evidence_bindings": binding_reports,
    }


CONTROLLED_CATALOG_FILES = ("comp-hash-catalog.json", "model-downshift-catalog.json")


def _declared_second_models(
    roots: list[Path], primary: str, problems: list[str]
) -> tuple[set[str], list[str]]:
    """读取证据根下由中继脚本产出的受控模型目录，返回其声明的第二模型。

    受控目录由 build_compaction_model_catalog.py 生成：恰好两条，首条必须是
    Job 的目标模型，第二条是压缩场景切换到的模型。文件名由
    run_official_relay_scenario.sh 写死；任何形态偏差都记为问题而不猜。
    """

    declared: set[str] = set()
    sources: list[str] = []
    for root in roots:
        for name in CONTROLLED_CATALOG_FILES:
            path = root / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                catalog, _raw = closeout._load_json(
                    closeout._trusted_file(path, "受控模型目录"), "受控模型目录"
                )
            except closeout.VC0CloseoutError as error:
                problems.append(f"受控模型目录 {name} 不可信：{error}")
                continue
            models = catalog.get("models")
            slugs = (
                [item.get("slug") for item in models]
                if isinstance(models, list) and all(isinstance(item, Mapping) for item in models)
                else None
            )
            if not slugs or len(slugs) != 2 or not all(isinstance(slug, str) for slug in slugs):
                problems.append(f"受控模型目录 {name} 形态非法")
                continue
            if slugs[0] != primary:
                problems.append(f"受控模型目录 {name} 首模型 {slugs[0]} 不是目标模型 {primary}")
                continue
            declared.add(slugs[1])
            sources.append(str(path))
    return declared, sources


def _check_models(
    attempt: Mapping[str, Any],
    manifest: Mapping[str, Any],
    provenance_receipt: Mapping[str, Any],
    *,
    campaign_dir: Path,
    expected_track_models: Mapping[str, str] | None,
) -> dict[str, Any]:
    """核对每个 Job 的线上请求模型。

    判定口径：
    * 用户线程（thread_source 不是 system，含取不到元数据的请求）的模型只能是
      目标模型或受控目录声明的第二模型，且目标模型必须真实出现过；
    * 受控目录的第二模型必须等于 Campaign 冻结的 Lite 模型；
    * 官方客户端自发的系统线程（thread_source == system）模型只记录不判定；
    * 模型端点请求缺少 model 字段直接判失败，不视为"未知即通过"。
    """

    configuration = manifest["configuration"]
    capture_root = Path(str(configuration.get("capture_root", "")))
    host_data_root = closeout._formal_host_data_root(campaign_dir)
    lite_model = configuration.get("lite_model")
    buckets: dict[str, dict[str, Any]] = {}
    for request in provenance_receipt.get("requests", []):
        bucket = buckets.setdefault(
            str(request["job_id"]), {"all": set(), "user": set(), "system": set(), "missing": 0}
        )
        model = request.get("model")
        if not isinstance(model, str) or not model:
            bucket["missing"] += 1
            continue
        bucket["all"].add(model)
        if request.get("thread_source") == "system":
            bucket["system"].add(model)
        else:
            bucket["user"].add(model)
    job_roots: dict[str, list[Path]] = {}
    for job in provenance_receipt.get("jobs", []):
        job_roots[str(job.get("job_id"))] = [
            Path(str(root.get("root"))) for root in job.get("roots", []) if isinstance(root, Mapping)
        ]
    track_catalog: dict[str, set[str]] = {}
    jobs: list[dict[str, Any]] = []
    for result in attempt.get("results", []):
        if not isinstance(result, Mapping):
            raise AttemptAuditError("attempt results 项不是对象")
        job_id = str(result.get("id"))
        model_id = str(result.get("model_id"))
        entry: dict[str, Any] = {
            "job_id": job_id,
            "track": result.get("track"),
            "model_id": result.get("model_id"),
            "expected_use_responses_lite": result.get("expected_use_responses_lite"),
            "status": result.get("status"),
        }
        if result.get("required_model_receipt"):
            replay = _replay_model_receipt(
                result, capture_root=capture_root, host_data_root=host_data_root
            )
            entry["receipt"] = replay
            if replay["passed"]:
                track_catalog.setdefault(str(result.get("track")), set()).add(model_id)
        bucket = buckets.get(job_id, {"all": set(), "user": set(), "system": set(), "missing": 0})
        job_problems: list[str] = []
        declared, sources = _declared_second_models(job_roots.get(job_id, []), model_id, job_problems)
        if bucket["missing"]:
            job_problems.append(f"{bucket['missing']} 条模型端点请求缺少 model 字段")
        unexpected = bucket["user"] - ({model_id} | declared)
        if unexpected:
            job_problems.append(f"用户线程出现未声明模型：{sorted(unexpected)}")
        if bucket["user"] and model_id not in bucket["user"]:
            job_problems.append("用户线程请求中没有目标模型")
        if declared:
            if not isinstance(lite_model, str) or not lite_model:
                job_problems.append("Campaign 未冻结 Lite 模型，无法核对受控目录第二模型")
            elif declared != {lite_model}:
                job_problems.append(
                    f"受控目录第二模型 {sorted(declared)} 不等于 Campaign Lite 模型 {lite_model}"
                )
        entry.update(
            {
                "wire_models": sorted(bucket["all"]),
                "user_thread_models": sorted(bucket["user"]),
                "system_thread_models": sorted(bucket["system"]),
                "model_missing_count": bucket["missing"],
                "declared_second_models": sorted(declared),
                "controlled_catalogs": sources,
                "wire_consistent": not job_problems,
                "problems": job_problems,
            }
        )
        jobs.append(entry)
    catalog = {track: sorted(models) for track, models in track_catalog.items()}
    catalog_unique = all(len(models) == 1 for models in track_catalog.values())
    for entry in jobs:
        track = str(entry["track"])
        entry["track_catalog_consistent"] = (
            track in track_catalog and str(entry["model_id"]) in track_catalog[track]
        )
    expectation_ok = True
    expectation_report: dict[str, Any] = {}
    if expected_track_models:
        for track, model in expected_track_models.items():
            actual = catalog.get(track)
            ok = actual == [model]
            expectation_report[track] = {"expected": model, "actual": actual, "passed": ok}
            expectation_ok = expectation_ok and ok
    receipts_ok = all(
        entry["receipt"]["passed"] for entry in jobs if "receipt" in entry
    )
    passed = (
        receipts_ok
        and catalog_unique
        and bool(track_catalog)
        and all(entry["wire_consistent"] for entry in jobs)
        and all(entry["track_catalog_consistent"] for entry in jobs)
        and expectation_ok
    )
    return {
        "passed": passed,
        "lite_model": lite_model,
        "track_catalog": catalog,
        "catalog_unique_per_track": catalog_unique,
        "receipts_replayed": sum(1 for entry in jobs if "receipt" in entry),
        "receipts_passed": sum(1 for entry in jobs if entry.get("receipt", {}).get("passed")),
        "expected_track_models": expectation_report,
        "problems": [f"{entry['job_id']}: {problem}" for entry in jobs for problem in entry["problems"]],
        "jobs": jobs,
    }


def _check_environment(attempt: Mapping[str, Any], attempt_root: Path) -> dict[str, Any]:
    environment = attempt.get("environment")
    if not isinstance(environment, Mapping):
        raise AttemptAuditError("attempt 缺少 environment")
    evidence_root = attempt_root / "evidence"
    declared = Path(str(environment.get("evidence_root", "")))
    files: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    if declared.resolve() != evidence_root.resolve():
        problems.append("environment.evidence_root 不在 attempt 目录内")
    for role in ENVIRONMENT_ROLES:
        try:
            _target, report = _binding_file(evidence_root, environment.get(role), role)
        except AttemptAuditError as error:
            problems.append(str(error))
            continue
        files[role] = report
        if not (report["sha256_match"] and report["bytes_match"]):
            problems.append(f"{role} 摘要或大小漂移")
    continuity: dict[str, Any] = {}
    if "arm64_before_receipt" in files and "arm64_after_receipt" in files:
        before, _b = closeout._load_json(Path(files["arm64_before_receipt"]["path"]), "ARM64 前收据")
        after, _a = closeout._load_json(Path(files["arm64_after_receipt"]["path"]), "ARM64 后收据")
        continuity = {
            "before_status": before.get("status"),
            "after_status": after.get("status"),
            "before_identity": before.get("continuity_identity_sha256"),
            "after_identity": after.get("continuity_identity_sha256"),
        }
        if before.get("status") != "passed" or after.get("status") != "passed":
            problems.append("ARM64 环境收据状态不是 passed")
        try:
            if not arm64.receipts_equivalent(Path(files["arm64_before_receipt"]["path"]).parent, before,
                                             Path(files["arm64_after_receipt"]["path"]).parent, after):
                problems.append("前后 ARM64 环境身份不连续")
        except (OSError, ValueError, KeyError) as error:
            problems.append(f"ARM64 环境收据缺少可信的原 producer 重放证明：{type(error).__name__}")
    if "restoration_report" in files:
        report, _r = closeout._load_json(Path(files["restoration_report"]["path"]), "恢复报告")
        continuity["restoration_status"] = report.get("status")
    return {
        "passed": not problems,
        "problems": problems,
        "files": files,
        "continuity": continuity,
        "restoration_error": attempt.get("restoration_error"),
        "execution_error": attempt.get("execution_error"),
    }


def _walk_inventory(root: Path) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    nonconforming = 0
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        if current_path.is_symlink():
            raise AttemptAuditError(f"inventory 遇到符号链接目录：{current_path}")
        if stat.S_IMODE(current_path.stat().st_mode) != 0o700:
            nonconforming += 1
        directories.sort()
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink():
                raise AttemptAuditError(f"inventory 遇到符号链接：{path}")
            size, digest = _file_sha256_nofollow(path)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                nonconforming += 1
            entries.append({"path": str(path.resolve()), "bytes": size, "sha256": digest})
    return entries, nonconforming


def _check_integrity(
    attempt: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    attempt_root: Path,
    campaign_dir: Path,
) -> dict[str, Any]:
    problems: list[str] = []
    store = incremental_recovery.CheckpointStore(attempt_root / "checkpoints", create=False)
    try:
        records = store.records()
    except incremental_recovery.IncrementalRecoveryError as error:
        raise AttemptAuditError(f"checkpoint 链重放失败：{error}") from error
    job_checkpoint = attempt.get("job_checkpoint")
    if not isinstance(job_checkpoint, Mapping):
        problems.append("attempt 缺少 job_checkpoint")
    else:
        if job_checkpoint.get("record_count") != len(records):
            problems.append("job_checkpoint.record_count 与链长度不一致")
        if job_checkpoint.get("last_sequence") != len(records):
            problems.append("job_checkpoint.last_sequence 与链长度不一致")
        last_digest = records[-1].get("checkpoint_sha256") if records else None
        if job_checkpoint.get("last_sha256") != last_digest:
            problems.append("job_checkpoint.last_sha256 与链尾不一致")
    statuses: dict[str, int] = {}
    for record in records:
        statuses[str(record.get("status"))] = statuses.get(str(record.get("status")), 0) + 1
    configuration = manifest["configuration"]
    capture_root = Path(str(configuration.get("capture_root", "")))
    host_data_root = closeout._formal_host_data_root(campaign_dir)
    inventory: list[dict[str, Any]] = []
    nonconforming = 0
    missing: list[str] = []
    seen: set[Path] = set()
    for value in attempt.get("evidence_roots", []):
        candidate = Path(str(value))
        # attempt 自己的证据目录以宿主路径记录，Job 证据根以容器路径记录；
        # 前者必须已经在宿主数据根内，后者才需要映射。
        if candidate.is_absolute() and (
            candidate == host_data_root or host_data_root in candidate.parents
        ):
            mapped = candidate
        else:
            mapped = closeout._map_container_evidence_root(
                value, capture_root=capture_root, host_data_root=host_data_root
            )
        if mapped in seen:
            continue
        seen.add(mapped)
        if not mapped.is_dir() or mapped.is_symlink():
            missing.append(str(mapped))
            continue
        entries, bad = _walk_inventory(mapped)
        inventory.extend(entries)
        nonconforming += bad
    if missing:
        problems.append(f"evidence roots 缺失：{len(missing)}")
    return {
        "passed": not problems,
        "problems": problems,
        "checkpoint_count": len(records),
        "checkpoint_statuses": statuses,
        "evidence_root_count": len(seen),
        "missing_evidence_roots": missing,
        "inventory_file_count": len(inventory),
        "inventory_bytes": sum(int(item["bytes"]) for item in inventory),
        "inventory_sha256": _sha256(_canonical(sorted(inventory, key=lambda item: item["path"]))),
        "inventory": inventory,
        "nonconforming_permission_entries": nonconforming,
    }


def audit_official_attempt(
    formal_campaign_dir: Path,
    *,
    attempt_id: str,
    estimation_policy: str = "none",
    expected_track_models: Mapping[str, str] | None = None,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    campaign_dir = closeout._trusted_directory(formal_campaign_dir, "Formal Campaign")
    manifest, raw_manifest = closeout._load_json(campaign_dir / "campaign.json", "Formal Campaign")
    digest_path = closeout._trusted_file(campaign_dir / "campaign.sha256", "Campaign 摘要")
    if digest_path.read_text(encoding="ascii").strip() != _sha256(raw_manifest):
        raise AttemptAuditError("Campaign 摘要漂移")
    if manifest.get("campaign_mode") != "formal":
        raise AttemptAuditError("只审计 formal Campaign")
    campaign_id = str(manifest.get("campaign_id", ""))
    if not closeout.SAFE_ID_RE.fullmatch(attempt_id):
        raise AttemptAuditError("attempt_id 非法")
    attempt_root = closeout._trusted_directory(
        campaign_dir / "official" / "attempts" / attempt_id, "official attempt"
    )
    attempt, raw_attempt = closeout._load_json(attempt_root / "attempt.json", "attempt")
    if attempt.get("attempt_id") != attempt_id or attempt.get("campaign_id") != campaign_id:
        raise AttemptAuditError("attempt 身份与目录不一致")
    if attempt.get("phase") != "official":
        raise AttemptAuditError("只审计 official attempt")

    provenance_receipt = provenance.collect_campaign_provenance(
        campaign_dir,
        formal_campaign_id=campaign_id,
        estimation_policy=estimation_policy,
        observed_at_utc=observed_at_utc,
    )
    sections = {
        "identity": _check_identity(attempt, manifest, attempt_root),
        "account": _check_account(manifest, raw_manifest, attempt_root, attempt_id),
        "models": _check_models(
            attempt,
            manifest,
            provenance_receipt,
            campaign_dir=campaign_dir,
            expected_track_models=expected_track_models,
        ),
        "environment": _check_environment(attempt, attempt_root),
        "integrity": _check_integrity(
            attempt, manifest, attempt_root=attempt_root, campaign_dir=campaign_dir
        ),
    }
    failed = [name for name, section in sections.items() if not section["passed"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "attempt_status": attempt.get("status"),
        "attempt_sha256": _sha256(raw_attempt),
        "observed_at_utc": observed_at_utc or _utc_now(),
        "status": "passed" if not failed else "failed",
        "failed_sections": failed,
        **sections,
        "requests": {
            "status": provenance_receipt["status"],
            "counting_rule": provenance_receipt["counting_rule"],
            "estimation_policy": estimation_policy,
            "precise_total": provenance_receipt["precise_total"],
            "estimated_total": provenance_receipt["estimated_total"],
            "unresolved_job_ids": provenance_receipt["unresolved_job_ids"],
            "identity_keys_sha256": provenance_receipt["identity_keys_sha256"],
        },
        "permissions": {
            "nonconforming_entries": sections["integrity"]["nonconforming_permission_entries"],
            "action": "只报告；收口由 harden-evidence-permissions 两步式命令执行",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读审计一份 official attempt。")
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument(
        "--estimation-policy", choices=provenance.ESTIMATION_POLICIES, default="none"
    )
    parser.add_argument(
        "--expected-track-model",
        action="append",
        default=[],
        metavar="TRACK=MODEL",
        help="可重复；例如 main=gpt-5.5、lite=gpt-6-astra。",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    expected: dict[str, str] = {}
    for item in arguments.expected_track_model:
        if "=" not in item:
            print(f"--expected-track-model 需要 TRACK=MODEL：{item!r}", file=sys.stderr)
            return 2
        track, model = item.split("=", 1)
        expected[track] = model
    try:
        payload = audit_official_attempt(
            arguments.campaign_dir,
            attempt_id=arguments.attempt_id,
            estimation_policy=arguments.estimation_policy,
            expected_track_models=expected or None,
        )
        provenance.write_receipt(payload, arguments.output)
    except (
        AttemptAuditError,
        provenance.ProvenanceError,
        closeout.VC0CloseoutError,
        incremental_recovery.IncrementalRecoveryError,
        OSError,
    ) as error:
        print(f"attempt 审计失败：{error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": payload["status"],
                "failed_sections": payload["failed_sections"],
                "requests": payload["requests"]["status"],
                "output": str(arguments.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
