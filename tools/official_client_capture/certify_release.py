"""C1：受管工具发布认证（``tool-release-certification/v1``）。

把 A2.5 pre-A3 路径认证与 P0 离线演练三件套（ARM64 全量 Job rehearsal、campaign-run
分批演练、atomic-double 原子双跑）合成一份发布认证收据，绑定当前 ARM64 部署收据与
策略 v2 五摘要，并替换 A2.6 的策略激活认证：VC-0 原子收口与 Formal ``plan`` 只接受
发布认证，不再逐项接受各演练收据；P0 门禁收据以 ``release_certification`` 角色绑定它。

签发（``issue``）时逐项重放各输入收据；校验（``verify``）只核对认证自摘要、五摘要与
各绑定文件的摘要，不重跑演练。收据只写一次，``superseded_by`` 非空即失效。

Claude 升级 B5a-04 起按客户端分组：``issue／verify --client``（缺省 codex，签 ``tool-release-certification/v1``，字段与
内容逐字不变）；Claude 签 ``…/v2``（v1 字段加 ``client``），绑定 Claude 部署收据、Claude 的 pre-A3 v3 认证与 Claude 身份。
Job 演练与 atomic-double 都按客户端的合同重放（收据必须属于本次认证的客户端）：两客户端同样要求 Job 演练带存储与失败生命周期
两项探针摘要（Claude 的失败生命周期探针由 B2-11 第二部分补上，见 ``claude_upgrade_failure_lifecycle``），atomic-double 收据按
客户端的合同名与工具身份核对（Claude 签 ``official-client-atomic-vc0-vc1-rehearsal/v1``，B5a-09 续，画像见
``claude_upgrade_atomic_rehearsal``），容器内重放给 Claude 追加 ``--client claude``。pre-A3 场景集合为空时 Claude 签发更早被
pre-A3 拒绝。B5b-04 另设 Claude 发布必需链集合：基座 pre-A3 可以先认证已实现的链，发布签发与验收则必须包含完整回放和
后段故障链，不能因某条链尚未登记而自动缩小 Formal 准入范围。对编排器的引用（指纹、文件摘要）经客户端适配器取。
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from tools.official_client_capture import codex_upgrade_campaign_run_rehearsal_receipt as campaign_rehearsal
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as job_rehearsal
from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
from tools.official_client_capture import upgrade_client_adapter

SCHEMA_VERSION = "tool-release-certification/v1"
# B5a-04：Claude 的发布认证（v1 字段加 client），只由 Claude 使用（设计 D2、附录 B）。
SCHEMA_VERSION_V2 = "tool-release-certification/v2"
CLIENT_SCHEMAS: Mapping[str, str] = {"codex": SCHEMA_VERSION, "claude": SCHEMA_VERSION_V2}
SCHEMAS_WITH_CLIENT = frozenset({SCHEMA_VERSION_V2})
DEFAULT_CLIENT = "codex"
# 与 pre-A3 当前可执行登记分开：尚未实现的必需链也必须阻止发布，不能悄悄从准入全集消失。
# Codex 继续沿用它已有的登记集合核验，不改变历史 v1 认证的形状及行为。
RELEASE_REQUIRED_CHAINS: Mapping[str, tuple[str, ...]] = {
    "claude": (
        "vc-chain.claude-reconcile",
        "vc-chain.claude-deadline",
        "vc-chain.claude-root-cause",
        "vc-chain.claude-tool-evolution",
        "vc-chain.claude-vc1-capture",
        "vc-chain.claude-full-validation-only",
        "vc-chain.claude-recovery-chain",
        "vc-chain.claude-late-stage-faults",
    ),
}
# 64 位小写十六进制摘要；与编排器 SHA256_RE 同一个模式（两客户端相同，测试核对），不为一个正则导入编排器。
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ReleaseCertificationError(ValueError):
    """发布认证输入不完整、绑定漂移或自摘要不一致。"""


def require_client(client: Any) -> str:
    """认证客户端：只认登记的客户端名，不认识即拒绝，不默认成 codex。"""

    if not isinstance(client, str) or client not in CLIENT_SCHEMAS:
        raise ReleaseCertificationError(f"不认识的客户端：{client!r}（可选：{list(CLIENT_SCHEMAS)}）")
    return client


def _adapter(client: str) -> upgrade_client_adapter.ClientAdapter:
    return upgrade_client_adapter.adapter_for(require_client(client))


def require_release_chain_coverage(coverage: Any, *, client: str) -> None:
    """核对发布必需链，拒绝缺报、重复、跳过与失败；调用方仍须重放绑定的 pre-A3 收据。"""

    required = RELEASE_REQUIRED_CHAINS.get(require_client(client), ())
    if not required:
        return
    if not isinstance(coverage, list) or any(
        not isinstance(row, Mapping) or not isinstance(row.get("id"), str)
        or not isinstance(row.get("test"), str) or not row["test"] or row.get("status") != "passed"
        for row in coverage
    ):
        raise ReleaseCertificationError(f"{client} 发布必需链覆盖缺失、非法或未通过")
    ids = [row["id"] for row in coverage]
    if len(ids) != len(set(ids)):
        raise ReleaseCertificationError(f"{client} 发布必需链覆盖含重复结果")
    missing = sorted(set(required) - set(ids))
    if missing:
        raise ReleaseCertificationError(f"{client} 发布必需链未认证：{', '.join(missing)}")


def certification_client(payload: Mapping[str, Any]) -> str:
    """发布认证所属的客户端：v2 读 ``client`` 字段，v1 没有该字段、按合同隐含 codex。"""

    if payload.get("schema_version") in SCHEMAS_WITH_CLIENT:
        return require_client(payload.get("client"))
    return "codex"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fingerprint(value: Any, client: str = DEFAULT_CLIENT) -> str:
    return _adapter(client).orchestrator._fingerprint(value)


def _bind_file(path: Path, label: str, client: str = DEFAULT_CLIENT) -> dict[str, str]:
    """绝对路径 + 内容摘要的最小文件绑定。"""

    resolved = Path(path)
    if not resolved.is_absolute() or resolved.is_symlink() or not resolved.is_file():
        raise ReleaseCertificationError(f"{label} 必须是存在的非符号链接绝对文件：{path}")
    return {"path": str(resolved), "sha256": _adapter(client).orchestrator.file_sha256(resolved)}


def _check_binding(binding: Any, label: str, client: str = DEFAULT_CLIENT) -> None:
    if not isinstance(binding, Mapping) or set(binding) < {"path", "sha256"}:
        raise ReleaseCertificationError(f"{label} 绑定字段不闭合")
    path = Path(str(binding.get("path", "")))
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or _adapter(client).orchestrator.file_sha256(path) != binding.get("sha256")
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


def replay_job_rehearsal(root: Path, receipt: Path, client: str = DEFAULT_CLIENT) -> dict[str, Any]:
    """重放 ARM64 全量 Job 演练收据，并要求存储与失败生命周期两项探针已登记。

    收据按 ``client`` 的合同重放（B2-11 第二部分）：另一客户端的收据即拒绝，不会因为字段齐全被绑进本客户端的认证；
    Codex 的合法收据结果与改造前相同。"""

    relative = _relative_inside(root, receipt, "Job rehearsal 收据")
    try:
        replayed = job_rehearsal.replay(Path(root).resolve(strict=True), relative, client=require_client(client))
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
    binding = _bind_file(Path(root).resolve(strict=True) / relative, "Job rehearsal 收据", client)
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


def replay_atomic_double(
    root: Path, receipt: Path, *, require_arm64: bool | None = None, client: str = DEFAULT_CLIENT,
) -> dict[str, Any]:
    """重放 atomic-double 原子双跑收据，工具身份必须来自当前受管树；收据必须是 ``client`` 的合同（B5a-09 续）。"""

    relative = _relative_inside(root, receipt, "atomic-double 收据")
    try:
        replayed = campaign_rehearsal.replay_atomic_double(
            Path(root).resolve(strict=True), relative, require_arm64=require_arm64, client=require_client(client)
        )
    except (OSError, ValueError, campaign_rehearsal.CampaignRunRehearsalError) as error:
        raise ReleaseCertificationError(f"atomic-double 收据重放失败：{error}") from error
    if replayed.get("tool_identity") != campaign_rehearsal._atomic_tool_identity(client):
        raise ReleaseCertificationError("atomic-double 收据的工具身份不是当前工具树")
    binding = _bind_file(Path(root).resolve(strict=True) / relative, "atomic-double 收据", client)
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
    client: str = DEFAULT_CLIENT,
) -> dict[str, Any]:
    """在 ``capture-cli`` 内重放 atomic-double 收据并绑定宿主侧同源文件。

    原子双跑的环境门禁（``/capture`` 只读、``/capture/staging`` 可写、双 runs 别名同
    inode）只能在容器内复算，宿主上无法本地重放；这里沿用 VC-0 收口曾用的容器内
    ``atomic-double-replay`` 委托，只接受零请求的通过输出。收据按 ``client`` 的合同核对，
    Claude 的容器内重放追加 ``--client claude``（Codex 的命令行与改造前逐字相同）。
    """

    profile = campaign_rehearsal.atomic_profile(require_client(client))

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
    # 容器里的 /capture 就是生产数据根：禁写字节码，受管树里不留 __pycache__（E2-02）。
    command = [
        "docker", "exec", "--env", "PYTHONPATH=/capture", "--env", "PYTHONDONTWRITEBYTECODE=1", "--workdir", "/capture", container,
        "python3", "-m", "tools.official_client_capture.codex_upgrade_campaign_run_rehearsal_receipt",
        "atomic-double-replay", "--evidence-root", str(container_root), "--receipt", receipt_relative,
        *([] if client == DEFAULT_CLIENT else ["--client", client]),
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
    if payload.get("schema_version") != profile.ATOMIC_RECEIPT_SCHEMA or any(
        payload.get(key) != value for key, value in profile.ATOMIC_CLIENT_FIELDS.items()
    ):
        raise ReleaseCertificationError("atomic-double 收据 schema 非法")
    if payload.get("tool_identity") != campaign_rehearsal._atomic_tool_identity(client):
        raise ReleaseCertificationError("atomic-double 收据的工具身份不是当前工具树")
    binding = _bind_file(evidence_root / receipt_relative, "atomic-double 收据", client)
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
    real_chain_coverage: list[dict[str, Any]] | None = None,
    pre_a3_reuse_receipt: Mapping[str, Any] | None = None,
    client: str = DEFAULT_CLIENT,
) -> dict[str, Any]:
    """按固定字段组装并自签发布认证；各绑定由调用方（签发或测试夹具）提供。合同按客户端：Codex v1（字段不变）、
    Claude v2（另带 ``client``）。"""

    core = {
        "schema_version": CLIENT_SCHEMAS[require_client(client)],
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
        # pre-A3 路径认证自 2026-09-16 起含 VC-2～VC-6 派发链场景，发布认证据此授权后续阶段。
        "authorized_scopes": ["VC-0", "A3b", "VC-2", "VC-3", "VC-4", "VC-5", "VC-6"],
        "superseded_by": None,
    }
    # 历史夹具不补字段、不改旧自摘要；本次正式签发必须从 pre-A3 结果提取覆盖。
    if real_chain_coverage is not None:
        core["real_chain_coverage"] = real_chain_coverage
    # 修好接着跑第 19 项：pre-A3 认证是跨部署复用来的才绑定复用收据；同一部署下签发的认证不加此字段。
    if pre_a3_reuse_receipt is not None:
        core["pre_a3_reuse_receipt"] = dict(pre_a3_reuse_receipt)
    if core["schema_version"] in SCHEMAS_WITH_CLIENT:
        core["client"] = client
    return {**core, "receipt_sha256": _fingerprint(core, client)}


def _require_release_platform() -> None:
    """只在能实际验证夹具镜像及 Go 制品的 Linux ARM64 主机签发发布认证。"""

    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise ReleaseCertificationError("发布认证只能在 Linux ARM64 上签发")
    for name, arguments in (("go", ["version"]), ("docker", ["info", "--format", "{{.Architecture}}"] )):
        executable = shutil.which(name)
        if executable is None:
            raise ReleaseCertificationError(f"发布认证缺少 {name}")
        try:
            completed = subprocess.run([executable, *arguments], capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise ReleaseCertificationError(f"发布认证无法验证 {name}") from error
        if completed.returncode != 0:
            raise ReleaseCertificationError(f"发布认证的 {name} 不可用")
        if name == "docker" and completed.stdout.strip() not in {"aarch64", "arm64"}:
            raise ReleaseCertificationError("Docker 服务端不是 ARM64")


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
    pre_a3_reuse_receipt: Path | None = None,
    client: str = DEFAULT_CLIENT,
) -> dict[str, Any]:
    """逐项重放输入并组装发布认证；给出 ``atomic_container`` 时在容器内重放 atomic-double。

    pre-A3 认证绑定的部署收据不是本次部署收据时（修好接着跑第 19 项：工具身份未变、跨部署复用），必须给出
    把这份认证绑定到本次部署收据的复用收据 ``pre_a3_reuse_receipt``，否则失败关闭。

    ``client`` 决定身份、部署收据与 pre-A3 认证的合同（Claude：v3 引擎身份、Claude 部署收据、pre-A3 v3）；各项必须属于
    同一个客户端，混用即五摘要或合同不符而拒绝。
    """

    identity = policy_certification.current_identity(require_client(client))
    _require_release_platform()
    try:
        deployment = policy_certification.load_deployment_receipt(deployment_receipt, expected_identity=identity, client=client)
        pre_a3_payload = pre_a3.verify_certification(pre_a3_certification, expected_identity=identity, client=client)
        coverage = pre_a3.real_chain_coverage(pre_a3_payload, client=client)
    except (policy_certification.PolicyCertificationError, pre_a3.CertificationError) as error:
        raise ReleaseCertificationError(str(error)) from error
    require_release_chain_coverage(coverage, client=client)
    pre_a3_deployment = pre_a3_payload.get("deployment_receipt") or {}
    deployment_binding = _bind_file(deployment_receipt, "ARM64 部署收据", client)
    activation_binding: dict[str, Any] | None = None
    if policy_activation is not None:
        try:
            policy_certification.verify_activation_certification(policy_activation, expected_identity=identity, client=client)
        except policy_certification.PolicyCertificationError as error:
            raise ReleaseCertificationError(str(error)) from error
        activation_binding = {**_bind_file(policy_activation, "策略激活认证", client),
                              "schema_version": policy_certification.POLICY_ACTIVATION_SCHEMA}
    reuse_binding: dict[str, Any] | None = None
    if pre_a3_deployment.get("sha256") != deployment_binding["sha256"] and pre_a3_reuse_receipt is None:
        raise ReleaseCertificationError(
            "pre-A3 路径认证绑定的部署收据不是本次发布认证的部署收据；跨部署复用须给出 --pre-a3-reuse-receipt"
        )
    if pre_a3_reuse_receipt is not None:
        try:
            reuse_payload = pre_a3.verify_reuse_receipt(
                pre_a3_reuse_receipt,
                certification=pre_a3_payload,
                deployment_receipt_sha256=deployment_binding["sha256"],
                policy_activation_sha256=activation_binding["sha256"] if activation_binding else None,
                expected_identity=identity,
                client=client,
            )
        except pre_a3.CertificationError as error:
            raise ReleaseCertificationError(str(error)) from error
        reuse_binding = {
            **_bind_file(pre_a3_reuse_receipt, "pre-A3 复用收据", client),
            "schema_version": pre_a3.REUSE_SCHEMA_VERSION,
            "receipt_sha256": reuse_payload.get("receipt_sha256"),
            "reused_at_utc": reuse_payload.get("reused_at_utc"),
            "bound_deployment_receipt_sha256": (reuse_payload.get("reused_certification") or {}).get("bound_deployment_receipt_sha256"),
        }
    campaign_run: dict[str, Any] | None = None
    if (campaign_run_rehearsal_root is None) != (campaign_run_rehearsal_receipt is None):
        raise ReleaseCertificationError("campaign-run rehearsal 的证据根与收据必须同时提供")
    if campaign_run_rehearsal_root is not None and client != DEFAULT_CLIENT:
        # 可选的分批演练只有 Codex 的合同（消费 Codex 的 preflight Campaign）；Claude 不绑定它。
        raise ReleaseCertificationError(f"客户端 {client} 没有 campaign-run 分批演练合同，不接受 --campaign-run-rehearsal-*")
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
            atomic_rehearsal_root, atomic_rehearsal_receipt, data_root=data_root, container=atomic_container, client=client
        )
    else:
        atomic_binding = replay_atomic_double(atomic_rehearsal_root, atomic_rehearsal_receipt, require_arm64=enforce, client=client)
    return compose_certification(
        real_chain_coverage=coverage,
        identity=identity,
        deployment_receipt={**deployment_binding, "created_at_utc": deployment.get("created_at_utc")},
        pre_a3_certification={
            **_bind_file(pre_a3_certification, "pre-A3 路径认证", client),
            "receipt_sha256": pre_a3_payload.get("receipt_sha256"),
            "scenario_count": pre_a3_payload.get("scenario_count"),
        },
        policy_activation=activation_binding,
        job_rehearsal=replay_job_rehearsal(job_rehearsal_root, job_rehearsal_receipt, client),
        atomic_double_rehearsal=atomic_binding,
        campaign_run_rehearsal=campaign_run,
        issued_at_utc=issued_at_utc,
        pre_a3_reuse_receipt=reuse_binding,
        client=client,
    )


def verify(
    path: Path, *, expected_identity: Mapping[str, Any] | None = None, client: str = DEFAULT_CLIENT,
) -> dict[str, Any]:
    """校验发布认证：schema、active、自摘要、五摘要等于期望身份、各绑定文件摘要一致。

    认证必须是 ``client`` 的合同（Codex v1、Claude v2）且属于该客户端，身份按该客户端现算；缺省 codex 与改造前相同。"""

    payload = policy_certification._read_json(Path(path), "发布认证")
    if payload.get("schema_version") != CLIENT_SCHEMAS[require_client(client)]:
        raise ReleaseCertificationError("发布认证 schema 非法")
    if certification_client(payload) != client:
        raise ReleaseCertificationError(f"发布认证不属于客户端 {client}")
    unsigned = {k: v for k, v in payload.items() if k != "receipt_sha256"}
    if _fingerprint(unsigned, client) != payload.get("receipt_sha256"):
        raise ReleaseCertificationError("发布认证自摘要不一致")
    if payload.get("status") != "active" or payload.get("superseded_by") is not None:
        raise ReleaseCertificationError("发布认证不是 active 或已被替换")
    identity = expected_identity or policy_certification.current_identity(client)
    if {k: str(v) for k, v in (payload.get("identity") or {}).items()} != {
        name: str(identity[name]) for name in policy_certification.IDENTITY_FIELDS
    } or payload.get("policy_version") != identity["policy_version"]:
        raise ReleaseCertificationError("发布认证五摘要或策略版本与当前工具身份不一致")
    _check_binding(payload.get("deployment_receipt"), "发布认证绑定的部署收据", client)
    _check_binding(payload.get("pre_a3_certification"), "发布认证绑定的 pre-A3 路径认证", client)
    if payload.get("pre_a3_reuse_receipt") is not None:
        _check_binding(payload.get("pre_a3_reuse_receipt"), "发布认证绑定的 pre-A3 复用收据", client)
    if "real_chain_coverage" in payload:
        try:
            paths = policy_certification._read_json(Path(payload["pre_a3_certification"]["path"]), "pre-A3 路径认证")
            expected_coverage = pre_a3.real_chain_coverage(paths, historical=True, client=client)
        except pre_a3.CertificationError as error:
            raise ReleaseCertificationError(str(error)) from error
        if payload["real_chain_coverage"] != expected_coverage:
            raise ReleaseCertificationError("发布认证真实链覆盖与已绑定的 pre-A3 结果不一致")
    require_release_chain_coverage(payload.get("real_chain_coverage"), client=client)
    for label, key in (("Job rehearsal 收据", "job_rehearsal"), ("atomic-double 收据", "atomic_double_rehearsal")):
        section = payload.get(key)
        if not isinstance(section, Mapping):
            raise ReleaseCertificationError(f"发布认证缺少 {label} 绑定")
        _check_binding(
            {"path": str(Path(str(section.get("evidence_root", ""))) / str(section.get("receipt", ""))), "sha256": section.get("sha256")},
            f"发布认证绑定的 {label}",
            client,
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
    issue_parser.add_argument(
        "--pre-a3-reuse-receipt", type=Path, help="pre-A3 认证是跨部署复用来的时，把它绑定到本次部署收据的复用收据"
    )
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
    # B5a-04：按客户端签发与校验（缺省 codex，签 v1，与改造前相同；claude 签 v2）。
    for command in (issue_parser, verify_parser):
        command.add_argument("--client", choices=tuple(CLIENT_SCHEMAS), default=DEFAULT_CLIENT,
                             help="按哪个客户端的身份、部署收据与 pre-A3 认证签发或校验（缺省 codex）")
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
                pre_a3_reuse_receipt=arguments.pre_a3_reuse_receipt,
                client=arguments.client,
            )
            summary = {
                "status": result["status"],
                "output": str(arguments.output),
                "receipt_sha256": result["receipt_sha256"],
                "identity": result["identity"],
                "job_count": result["job_rehearsal"]["job_count"],
            }
        else:
            payload = verify(arguments.certification, client=arguments.client)
            summary = {"status": payload["status"], "receipt_sha256": payload["receipt_sha256"], "identity": payload["identity"]}
    except (ReleaseCertificationError, OSError, ValueError) as error:
        print(f"发布认证失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
