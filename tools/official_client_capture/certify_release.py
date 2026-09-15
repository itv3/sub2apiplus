"""C1：受管工具发布认证（``tool-release-certification/v1``）。

把 A2.5 pre-A3 路径认证与 P0 离线演练三件套（ARM64 全量 Job rehearsal、campaign-run
分批演练、atomic-double 原子双跑）合成一份发布认证收据，绑定当前 ARM64 部署收据与
策略 v2 五摘要，并替换 A2.6 的策略激活认证：VC-0 原子收口与 Formal ``plan`` 只接受
发布认证，不再逐项接受各演练收据；P0 门禁收据以 ``release_certification`` 角色绑定它。

签发（``issue``）时逐项重放各输入收据；校验（``verify``）只核对认证自摘要、五摘要与
各绑定文件的摘要，不重跑演练。收据只写一次，``superseded_by`` 非空即失效。
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_campaign_run_rehearsal_receipt as campaign_rehearsal
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as job_rehearsal
from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3

SCHEMA_VERSION = "tool-release-certification/v1"
SHA256_RE = codex_upgrade.SHA256_RE


class ReleaseCertificationError(ValueError):
    """发布认证输入不完整、绑定漂移或自摘要不一致。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    return codex_upgrade._fingerprint(value)


def _bind_file(path: Path, label: str) -> dict[str, str]:
    """绝对路径 + 内容摘要的最小文件绑定。"""

    resolved = Path(path)
    if not resolved.is_absolute() or resolved.is_symlink() or not resolved.is_file():
        raise ReleaseCertificationError(f"{label} 必须是存在的非符号链接绝对文件：{path}")
    return {"path": str(resolved), "sha256": codex_upgrade.file_sha256(resolved)}


def _check_binding(binding: Any, label: str) -> None:
    if not isinstance(binding, Mapping) or set(binding) < {"path", "sha256"}:
        raise ReleaseCertificationError(f"{label} 绑定字段不闭合")
    path = Path(str(binding.get("path", "")))
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or codex_upgrade.file_sha256(path) != binding.get("sha256")
    ):
        raise ReleaseCertificationError(f"{label} 缺失或摘要漂移：{path}")


def _relative_inside(root: Path, receipt: Path, label: str) -> str:
    root = Path(root).resolve(strict=True)
    candidate = Path(receipt) if Path(receipt).is_absolute() else root / receipt
    try:
        relative = candidate.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise ReleaseCertificationError(f"{label} 必须位于其证据根内") from error
    return relative


def replay_job_rehearsal(root: Path, receipt: Path) -> dict[str, Any]:
    """重放 ARM64 全量 Job 演练收据，并要求失败生命周期探针已登记。"""

    relative = _relative_inside(root, receipt, "Job rehearsal 收据")
    try:
        replayed = job_rehearsal.replay(Path(root).resolve(strict=True), relative)
    except (OSError, ValueError, job_rehearsal.JobRehearsalReceiptError) as error:
        raise ReleaseCertificationError(f"Job rehearsal 收据重放失败：{error}") from error
    preflight = replayed.get("preflight_campaign")
    if (
        replayed.get("status") != "passed"
        or not SHA256_RE.fullmatch(str(replayed.get("failure_lifecycle_probe_sha256", "")))
        or not SHA256_RE.fullmatch(str(replayed.get("storage_probe_sha256", "")))
        or not isinstance(preflight, Mapping)
    ):
        raise ReleaseCertificationError("Job rehearsal 收据未通过或缺少失败生命周期探针")
    binding = _bind_file(Path(root).resolve(strict=True) / relative, "Job rehearsal 收据")
    return {
        "evidence_root": str(Path(root).resolve(strict=True)),
        "receipt": relative,
        "sha256": binding["sha256"],
        "job_count": replayed.get("job_count"),
        "execution_contract_sha256": replayed.get("execution_contract_sha256"),
        "failure_lifecycle_probe_sha256": replayed["failure_lifecycle_probe_sha256"],
        "storage_probe_sha256": replayed["storage_probe_sha256"],
        "preflight_campaign_id": preflight.get("campaign_id"),
    }


def replay_atomic_double(root: Path, receipt: Path, *, require_arm64: bool | None = None) -> dict[str, Any]:
    """重放 atomic-double 原子双跑收据，工具身份必须来自当前受管树。"""

    relative = _relative_inside(root, receipt, "atomic-double 收据")
    try:
        replayed = campaign_rehearsal.replay_atomic_double(
            Path(root).resolve(strict=True), relative, require_arm64=require_arm64
        )
    except (OSError, ValueError, campaign_rehearsal.CampaignRunRehearsalError) as error:
        raise ReleaseCertificationError(f"atomic-double 收据重放失败：{error}") from error
    if replayed.get("tool_identity") != campaign_rehearsal._atomic_tool_identity():
        raise ReleaseCertificationError("atomic-double 收据的工具身份不是当前工具树")
    binding = _bind_file(Path(root).resolve(strict=True) / relative, "atomic-double 收据")
    return {
        "evidence_root": str(Path(root).resolve(strict=True)),
        "receipt": relative,
        "sha256": binding["sha256"],
        "campaign_ids": [str(item.get("campaign_id")) for item in replayed.get("instances", [])],
        "live_request_count": replayed.get("live_request_count"),
        "scanned_bytes": replayed.get("scanned_bytes"),
    }


def replay_atomic_double_in_container(
    root: Path,
    receipt: Path,
    *,
    data_root: Path,
    container: str,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """在 ``capture-cli`` 内重放 atomic-double 收据并绑定宿主侧同源文件。

    原子双跑的环境门禁（``/capture`` 只读、``/capture/staging`` 可写、双 runs 别名同
    inode）只能在容器内复算，宿主上无法本地重放；这里沿用 VC-0 收口曾用的容器内
    ``atomic-double-replay`` 委托，只接受零请求的通过输出。
    """

    evidence_root = Path(root).resolve(strict=True)
    try:
        relative_root = evidence_root.relative_to(Path(data_root).resolve(strict=True) / "staging")
    except (OSError, ValueError) as error:
        raise ReleaseCertificationError("atomic-double 证据根必须位于生产数据根 staging 下") from error
    if not relative_root.parts:
        raise ReleaseCertificationError("atomic-double 证据根不得直接使用 staging 根")
    if not container or not all(ch.isalnum() or ch in "-_." for ch in container):
        raise ReleaseCertificationError("capture 容器名非法")
    receipt_relative = _relative_inside(evidence_root, receipt, "atomic-double 收据")
    container_root = PurePosixPath("/capture/staging", *relative_root.parts)
    command = [
        "docker", "exec", "--env", "PYTHONPATH=/capture", "--workdir", "/capture", container,
        "python3", "-m", "tools.official_client_capture.codex_upgrade_campaign_run_rehearsal_receipt",
        "atomic-double-replay", "--evidence-root", str(container_root), "--receipt", receipt_relative,
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, timeout=timeout_seconds)
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseCertificationError(f"atomic-double 容器重放无法执行：{error}") from error
    stdout = completed.stdout.strip()
    if completed.returncode != 0 or not stdout or len(stdout) > 1_000_000:
        detail = completed.stderr.decode("utf-8", errors="replace")[:2000]
        raise ReleaseCertificationError(f"atomic-double 容器重放失败：exit={completed.returncode}，stderr={detail}")
    try:
        replayed = json.loads(stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseCertificationError("atomic-double 容器重放输出不是单一 JSON") from error
    if replayed != {"campaign_id": None, "live_request_count": 0, "status": "passed"}:
        raise ReleaseCertificationError("atomic-double 容器重放未闭合零请求边界")
    payload = policy_certification._read_json(evidence_root / receipt_relative, "atomic-double 收据")
    if payload.get("schema_version") != campaign_rehearsal.ATOMIC_RECEIPT_SCHEMA:
        raise ReleaseCertificationError("atomic-double 收据 schema 非法")
    if payload.get("tool_identity") != campaign_rehearsal._atomic_tool_identity():
        raise ReleaseCertificationError("atomic-double 收据的工具身份不是当前工具树")
    binding = _bind_file(evidence_root / receipt_relative, "atomic-double 收据")
    return {
        "evidence_root": str(evidence_root),
        "receipt": receipt_relative,
        "sha256": binding["sha256"],
        "campaign_ids": [str(item.get("campaign_id")) for item in payload.get("instances", [])],
        "live_request_count": payload.get("live_request_count"),
        "scanned_bytes": payload.get("scanned_bytes"),
        "replayed_in_container": container,
    }


def replay_campaign_run_rehearsal(root: Path, receipt: Path, *, preflight_campaign_dir: Path) -> dict[str, Any]:
    """重放 campaign-run 分批演练收据（需要 preflight Campaign 目录）。"""

    relative = _relative_inside(root, receipt, "campaign-run rehearsal 收据")
    try:
        replayed = campaign_rehearsal.replay(
            Path(root).resolve(strict=True), relative, campaign_dir=Path(preflight_campaign_dir)
        )
    except (OSError, ValueError, campaign_rehearsal.CampaignRunRehearsalError) as error:
        raise ReleaseCertificationError(f"campaign-run rehearsal 收据重放失败：{error}") from error
    binding = _bind_file(Path(root).resolve(strict=True) / relative, "campaign-run rehearsal 收据")
    return {
        "evidence_root": str(Path(root).resolve(strict=True)),
        "receipt": relative,
        "sha256": binding["sha256"],
        "campaign_id": replayed.get("campaign_id"),
        "live_request_count": replayed.get("live_request_count"),
    }


def compose_certification(
    *,
    identity: Mapping[str, Any],
    deployment_receipt: Mapping[str, Any],
    pre_a3_certification: Mapping[str, Any],
    policy_activation: Mapping[str, Any] | None,
    job_rehearsal: Mapping[str, Any],
    atomic_double_rehearsal: Mapping[str, Any],
    campaign_run_rehearsal: Mapping[str, Any] | None,
    issued_at_utc: str | None = None,
) -> dict[str, Any]:
    """按固定字段组装并自签发布认证；各绑定由调用方（签发或测试夹具）提供。"""

    core = {
        "schema_version": SCHEMA_VERSION,
        "status": "active",
        "issued_at_utc": issued_at_utc or _utc_now(),
        "identity": {name: identity[name] for name in policy_certification.IDENTITY_FIELDS},
        "policy_version": identity["policy_version"],
        "deployment_receipt": dict(deployment_receipt),
        "pre_a3_certification": dict(pre_a3_certification),
        "supersedes": {"policy_activation": dict(policy_activation) if policy_activation else None},
        "job_rehearsal": dict(job_rehearsal),
        "atomic_double_rehearsal": dict(atomic_double_rehearsal),
        "campaign_run_rehearsal": dict(campaign_run_rehearsal) if campaign_run_rehearsal else None,
        "authorized_scopes": ["VC-0", "A3b"],
        "superseded_by": None,
    }
    return {**core, "receipt_sha256": _fingerprint(core)}


def build_certification(
    *,
    deployment_receipt: Path,
    pre_a3_certification: Path,
    policy_activation: Path | None,
    job_rehearsal_root: Path,
    job_rehearsal_receipt: Path,
    atomic_rehearsal_root: Path,
    atomic_rehearsal_receipt: Path,
    campaign_run_rehearsal_root: Path | None = None,
    campaign_run_rehearsal_receipt: Path | None = None,
    preflight_campaign_dir: Path | None = None,
    require_arm64: bool | None = None,
    atomic_container: str | None = None,
    data_root: Path | None = None,
    issued_at_utc: str | None = None,
) -> dict[str, Any]:
    """逐项重放输入并组装发布认证；给出 ``atomic_container`` 时在容器内重放 atomic-double。"""

    identity = policy_certification.current_identity()
    try:
        deployment = policy_certification.load_deployment_receipt(deployment_receipt, expected_identity=identity)
        pre_a3_payload = pre_a3.verify_certification(pre_a3_certification, expected_identity=identity)
    except (policy_certification.PolicyCertificationError, pre_a3.CertificationError) as error:
        raise ReleaseCertificationError(str(error)) from error
    pre_a3_deployment = pre_a3_payload.get("deployment_receipt") or {}
    deployment_binding = _bind_file(deployment_receipt, "ARM64 部署收据")
    if pre_a3_deployment.get("sha256") != deployment_binding["sha256"]:
        raise ReleaseCertificationError("pre-A3 路径认证绑定的部署收据不是本次发布认证的部署收据")
    activation_binding: dict[str, Any] | None = None
    if policy_activation is not None:
        try:
            policy_certification.verify_activation_certification(policy_activation, expected_identity=identity)
        except policy_certification.PolicyCertificationError as error:
            raise ReleaseCertificationError(str(error)) from error
        activation_binding = {**_bind_file(policy_activation, "策略激活认证"), "schema_version": policy_certification.POLICY_ACTIVATION_SCHEMA}
    campaign_run: dict[str, Any] | None = None
    if (campaign_run_rehearsal_root is None) != (campaign_run_rehearsal_receipt is None):
        raise ReleaseCertificationError("campaign-run rehearsal 的证据根与收据必须同时提供")
    if campaign_run_rehearsal_root is not None:
        if preflight_campaign_dir is None:
            raise ReleaseCertificationError("重放 campaign-run rehearsal 需要 preflight Campaign 目录")
        campaign_run = replay_campaign_run_rehearsal(
            campaign_run_rehearsal_root, campaign_run_rehearsal_receipt, preflight_campaign_dir=preflight_campaign_dir
        )
    enforce = (platform.machine() == "aarch64") if require_arm64 is None else require_arm64
    if atomic_container is not None:
        if data_root is None:
            raise ReleaseCertificationError("容器内重放 atomic-double 需要 --data-root")
        atomic_binding = replay_atomic_double_in_container(
            atomic_rehearsal_root, atomic_rehearsal_receipt, data_root=data_root, container=atomic_container
        )
    else:
        atomic_binding = replay_atomic_double(atomic_rehearsal_root, atomic_rehearsal_receipt, require_arm64=enforce)
    return compose_certification(
        identity=identity,
        deployment_receipt={**deployment_binding, "created_at_utc": deployment.get("created_at_utc")},
        pre_a3_certification={
            **_bind_file(pre_a3_certification, "pre-A3 路径认证"),
            "receipt_sha256": pre_a3_payload.get("receipt_sha256"),
            "scenario_count": pre_a3_payload.get("scenario_count"),
        },
        policy_activation=activation_binding,
        job_rehearsal=replay_job_rehearsal(job_rehearsal_root, job_rehearsal_receipt),
        atomic_double_rehearsal=atomic_binding,
        campaign_run_rehearsal=campaign_run,
        issued_at_utc=issued_at_utc,
    )


def verify(path: Path, *, expected_identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """校验发布认证：schema、active、自摘要、五摘要等于期望身份、各绑定文件摘要一致。"""

    payload = policy_certification._read_json(Path(path), "发布认证")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseCertificationError("发布认证 schema 非法")
    unsigned = {k: v for k, v in payload.items() if k != "receipt_sha256"}
    if _fingerprint(unsigned) != payload.get("receipt_sha256"):
        raise ReleaseCertificationError("发布认证自摘要不一致")
    if payload.get("status") != "active" or payload.get("superseded_by") is not None:
        raise ReleaseCertificationError("发布认证不是 active 或已被替换")
    identity = expected_identity or policy_certification.current_identity()
    if {k: str(v) for k, v in (payload.get("identity") or {}).items()} != {
        name: str(identity[name]) for name in policy_certification.IDENTITY_FIELDS
    } or payload.get("policy_version") != identity["policy_version"]:
        raise ReleaseCertificationError("发布认证五摘要或策略版本与当前工具身份不一致")
    _check_binding(payload.get("deployment_receipt"), "发布认证绑定的部署收据")
    _check_binding(payload.get("pre_a3_certification"), "发布认证绑定的 pre-A3 路径认证")
    for label, key in (("Job rehearsal 收据", "job_rehearsal"), ("atomic-double 收据", "atomic_double_rehearsal")):
        section = payload.get(key)
        if not isinstance(section, Mapping):
            raise ReleaseCertificationError(f"发布认证缺少 {label} 绑定")
        _check_binding(
            {"path": str(Path(str(section.get("evidence_root", ""))) / str(section.get("receipt", ""))), "sha256": section.get("sha256")},
            f"发布认证绑定的 {label}",
        )
    if not SHA256_RE.fullmatch(str((payload.get("job_rehearsal") or {}).get("failure_lifecycle_probe_sha256", ""))):
        raise ReleaseCertificationError("发布认证缺少失败生命周期探针摘要")
    return payload


def issue(output: Path, **options: Any) -> dict[str, Any]:
    certification = build_certification(**options)
    policy_certification._write_once(Path(output), certification)
    return certification


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="受管工具发布认证：签发与校验 tool-release-certification/v1")
    commands = parser.add_subparsers(dest="command", required=True)
    issue_parser = commands.add_parser("issue", help="重放各输入收据并签发发布认证（只写一次）")
    issue_parser.add_argument("--deployment-receipt", type=Path, required=True)
    issue_parser.add_argument("--pre-a3-certification", type=Path, required=True)
    issue_parser.add_argument("--policy-activation", type=Path, help="被替换的 A2.6 策略激活认证；给出时校验并记录")
    issue_parser.add_argument("--job-rehearsal-root", type=Path, required=True)
    issue_parser.add_argument("--job-rehearsal-receipt", type=Path, required=True)
    issue_parser.add_argument("--atomic-rehearsal-root", type=Path, required=True)
    issue_parser.add_argument("--atomic-rehearsal-receipt", type=Path, required=True)
    issue_parser.add_argument("--campaign-run-rehearsal-root", type=Path)
    issue_parser.add_argument("--campaign-run-rehearsal-receipt", type=Path)
    issue_parser.add_argument("--preflight-campaign-dir", type=Path)
    issue_parser.add_argument("--allow-non-arm64", action="store_true", help="离线夹具：不强制 aarch64 挂载合同")
    issue_parser.add_argument("--atomic-container", help="在该 capture 容器内重放 atomic-double（宿主无法本地复算挂载合同）")
    issue_parser.add_argument("--data-root", type=Path, help="宿主生产数据根；与 --atomic-container 同时给出")
    issue_parser.add_argument("--output", type=Path, required=True)
    verify_parser = commands.add_parser("verify", help="只读校验发布认证")
    verify_parser.add_argument("--certification", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "issue":
            result = issue(
                arguments.output,
                deployment_receipt=arguments.deployment_receipt,
                pre_a3_certification=arguments.pre_a3_certification,
                policy_activation=arguments.policy_activation,
                job_rehearsal_root=arguments.job_rehearsal_root,
                job_rehearsal_receipt=arguments.job_rehearsal_receipt,
                atomic_rehearsal_root=arguments.atomic_rehearsal_root,
                atomic_rehearsal_receipt=arguments.atomic_rehearsal_receipt,
                campaign_run_rehearsal_root=arguments.campaign_run_rehearsal_root,
                campaign_run_rehearsal_receipt=arguments.campaign_run_rehearsal_receipt,
                preflight_campaign_dir=arguments.preflight_campaign_dir,
                require_arm64=False if arguments.allow_non_arm64 else None,
                atomic_container=arguments.atomic_container,
                data_root=arguments.data_root,
            )
            summary = {
                "status": result["status"],
                "output": str(arguments.output),
                "receipt_sha256": result["receipt_sha256"],
                "identity": result["identity"],
                "job_count": result["job_rehearsal"]["job_count"],
            }
        else:
            payload = verify(arguments.certification)
            summary = {"status": payload["status"], "receipt_sha256": payload["receipt_sha256"], "identity": payload["identity"]}
    except (ReleaseCertificationError, OSError, ValueError) as error:
        print(f"发布认证失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
