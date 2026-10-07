#!/usr/bin/env python3
"""Claude 候选采集 run 的候选专属部分（Claude 升级控制面设计 B4-01b）。

``claude_upgrade.py capture-candidate run`` 与官方取证共用 ``claude_upgrade_capture.run_capture_attempt`` 的 attempt 主流程
（租约、监督器、预约、逐 Job 环境闭环、补跑与归档、Job 闭环收口、run 末权限收口、只写一次的 attempt.json）；这里只放
候选阶段多出来的东西：入口门、attempt 身份、网关环境前后快照、生产 selector 只读观察、每步恢复报告核对与运行条件摘要，
以及服务面两个成员 ``_apply_candidate_runtime_override``、``_verify_candidate_attempt_identity`` 的实现。run 不写 Store。

一、入口门（预约之前，零写入；顺序固定）
========================================
正式 run 只在 Formal Campaign 上，依次：

1. **VC-4 checkpoint**：编排器侧当前候选 revision 属于本候选，重放该 revision 的 VC-4 checkpoint（逐级回溯到 VC-0），其阶段
   收据就是 ``candidates/<cid>/build-receipt.json``；
2. **Store**：受管工具身份与 Store 有效身份一致，Store 检查点为 ``candidate_sealed``，Store 当前候选 revision 是本候选、
   未作废且等于编排器侧 revision，该 revision 恰有一条本候选的 ``candidate_frozen``；计时账本 active；
3. **构建收据重放**：客户端中立合同校验（自摘要）＋与 Campaign、候选、revision、用途、目标版本逐项一致，Store 两个引用
   （``candidate_frozen_ref``、``candidate_build_receipt_ref``）与冻结事实逐字一致，构建 ID 与镜像 manifest 摘要等于冻结
   事实，候选源码树按同一算法复算摘要相等；
4. **服务容器镜像**：``configuration.service_container`` 当前运行的 config image ID 等于构建收据的 ``image.image_id``，
   构建收据的不可变镜像引用在该镜像的 RepoDigests 里；
5. **运行坐标覆盖**：Claude 没有登记覆盖的命令，``candidates/<cid>/runtime-override.json`` 一旦存在即失败关闭；
6. **未封存**：没有候选阶段结果（``candidates/<cid>/result.json``）、该 revision 的 VC-5 checkpoint 不存在；Campaign 锁内
   另核没有未封存或待续跑的 attempt（候选续跑属于 B4-01c）。

此外与官方 run 相同的门（污染标记、工具演进已登记、Job 展开与冻结行、坐标布局、执行树、官方二进制、容器内 CA 文件、
deadline 预算）照常执行；候选五个 Campaign 变量必须非空；账号刷新触发的步骤要求环境里给出管理凭据；网关只读就绪核对
（服务运行、临时 CA 不在、没有残留临时代理与运行目录、专用账号与 API Key 形态）也在预约之前做一次（不落盘）。

零请求演练（``--zero-request-rehearsal --job-id``）只在 ``preflight_only`` Campaign 上：没有 VC 链、Store 与构建收据，
身份里的镜像取服务容器当前 image ID；其余与正式 run 同一路径，宿主脚本只做只读前检、中继只回显计划。

二、attempt.json 的候选字段（字段名与 schema 固定，供 B4-02 封存与 B4-05b／B4-06b 读取）
=======================================================================================
与官方 attempt 同一 ``claude-upgrade-capture-attempt/v1`` 外形（``phase="candidate"``、``candidate_id``、``candidate_purpose``），
另有：

* ``identity``（``claude-upgrade-candidate-attempt-identity/v1``，预约收据的 ``identity_sha256`` 绑定它）：二进制与运行身份——
  目标版本与官方二进制、采集运行镜像与容器、候选 revision、构建收据绑定、构建 ID、部署版本、候选镜像引用／image ID／
  manifest 摘要、源码根与源码树摘要、提交、画像／Release／wire 摘要、Store 的两个引用、网关坐标；
* ``gateway``：网关坐标，``base_url`` 在 attempt 级记一份（``http://<服务容器>:<容器内端口>``，与宿主脚本写给中继的
  ``gateway.json`` 同一取法），另有服务／keeper／postgres 容器、账号与 API Key ID、connect 中继端口；
* ``gateway_environment``（``claude-upgrade-candidate-gateway-environment/v1``）：``before``／``after`` 两份快照文件的绑定
  （路径相对 ``<attempt>/evidence``）、参与比较的字段 ``compared_fields`` 与漂移字段 ``drifted_fields``；对应 Store 的
  environment_before／environment_after。另有 ``activation_fact``（c1 补充项，供 B4-06b 运行审计）：after 快照时从服务容器
  环境 ``GATEWAY_CLAUDE_EGRESS_ACTIVATION_FACT_PATH`` 指向的文件只读复制的 Go 激活事实，固定写在
  ``<attempt>/evidence/environment/claude-egress-activation-fact.json``，绑定字段 ``path``（相对 ``<attempt>/evidence``）、
  ``sha256``、``bytes``、``source_path``（容器内路径）、``schema_version``（``claude-egress-activation-fact/v1``）、
  ``profile_mode``（必须是 ``validation_candidate``）、``event_id``。缺失、不可读或不是候选 Runtime：正式 run 按环境污染处理，
  零请求演练判演练失败；等待封存的 attempt 必有这份副本；
* ``gateway_recovery``（``claude-upgrade-candidate-gateway-recovery/v1``）：网关隔离恢复核验文件的绑定与结论 ``status``
  （``passed``／``failed``）；文件里汇总每步恢复报告的核对、前后快照是否相等；对应 Store 的 recovery_evidence；
* ``selector_observed``（``claude-upgrade-candidate-selector-observation/v1``）：生产 selector 只读快照文件的绑定（正式 run
  必有；演练 Campaign 没有冻结 Active 目录时为 null）；对应 selector_observed；
* ``candidate_gateway``（与 B4-02 证据侧约定的字段名，见 ``candidate_gateway_facts``）：``base_url``、``host``
  （``host_id``、``architecture``，门禁执行计划的缺省主机）、``run_conditions``（``environment_sha256``、
  ``configuration_sha256``、``network_sha256``、``account_model_sha256`` 四项摘要；``run_nonce`` 取 attempt 顶层）、
  ``environment_before``／``environment_after``／``recovery_verification``（条目都是 ``{id, facts}``、按 id 排序，
  ``facts.result == "pass"`` 才算通过）、``selector_before_snapshot``（Store ``runtime_catalog_snapshot`` 对象 payload，由
  ``runtime_catalog_snapshot`` 从 selector 只读观察组装；演练为 null）；
* ``environment.restoration_report``：与 Codex 候选 attempt 同名，绑定网关隔离恢复核验文件（共享的候选恢复准入读它）；
* ``results[]``：与官方同形（``id``、``evidence_roots``、``relay_checks`` 每项带 ``probe_id``、``status``、``manifest`` 的
  ``path`` 与 ``sha256``，路径是逻辑坐标），另加 ``restoration_checks``：每步恢复报告的核对 ``{probe_id, status, report:
  {path, sha256}}``（逻辑坐标；失败行另带 ``failure_code``；只读前检拒绝、没有改动的步骤 ``status`` 为
  ``not_started``）。恢复报告在 Job 证据根里（``<证据根>/candidate-gateway/<probe>.json``），随证据根一起封存；失败归档时
  路径随证据根一起改写。
* 照官方提供：``binary_verification``、``reservation.json``、逻辑坐标形式的 ``relay_checks``。客户端证据目录
  ``<attempt 根>/client-evidence/``（Mac 侧客户端收据与 attempt 级边界核对收据，B4-02 约定）不由 run 建立，由收据的产出方写入。

秘密扫描（Store 的 secret_scan_evidence）不另写文件：每个 ``relay_checks`` 通过都意味着该步 relay 清单的逐字节秘密扫描通过。

三、每步恢复报告与环境污染
==========================
宿主脚本在开始修改之后无论成败都写恢复报告；只读前检失败（退出 1／2）不写报告、也没有改动。编排器对每个执行过的
步骤核对：报告存在、身份与本步一致、状态是 ``complete``（且退出码为 0）或 ``capture_failed``（退出码非 0），五项恢复
相等全部为真。报告缺失（且不是前检退出）、``restoration_failed``（退出 97）或任一项不等，都按环境污染处理：该 Job 不补跑、
归档证据、停止后续 Job，attempt 记 ``environment_contaminated``。attempt 末的网关 after 快照与 before 逐字段比较，漂移同样
判污染。

本文件在 Claude 身份策略里按 ``claude_upgrade_`` 前缀归 control 层，不进 wire 闭包。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts

# ---------------------------------------------------------------------------
# 合同常量
# ---------------------------------------------------------------------------

CANDIDATE_IDENTITY_SCHEMA = "claude-upgrade-candidate-attempt-identity/v1"
GATEWAY_ENVIRONMENT_SCHEMA = "claude-upgrade-candidate-gateway-environment/v1"
GATEWAY_RECOVERY_SCHEMA = "claude-upgrade-candidate-gateway-recovery/v1"
SELECTOR_OBSERVATION_SCHEMA = "claude-upgrade-candidate-selector-observation/v1"
RESTORATION_REPORT_SCHEMA = "claude-candidate-gateway-restoration/v1"
RESTORATION_REPORT_DIRNAME = "candidate-gateway"
RUNTIME_OVERRIDE_FILENAME = "runtime-override.json"
ACTION = "capture-candidate run"
STORE_CANDIDATE_SEALED = "candidate_sealed"
RUN_CONDITION_DIGEST_FIELDS = ("environment_sha256", "configuration_sha256", "network_sha256", "account_model_sha256")

# attempt 证据目录（``<attempt>/evidence/environment``）里的候选文件名。
GATEWAY_BEFORE_FILENAME = "gateway-before.json"
GATEWAY_AFTER_FILENAME = "gateway-after.json"
GATEWAY_RECOVERY_FILENAME = "gateway-recovery.json"
SELECTOR_FILENAME = "selector-observed.json"
# 网关进程写出的 Claude 激活事实（Go ``claude_official_egress_activation_fact.go``）：after 快照时只读复制进 attempt 证据目录，
# seal 深度扫描自然封进去，供 B4-06b 运行审计读取（c1 补充项）。
ACTIVATION_FACT_FILENAME = "claude-egress-activation-fact.json"
ACTIVATION_FACT_PATH_ENV = "GATEWAY_CLAUDE_EGRESS_ACTIVATION_FACT_PATH"
ACTIVATION_FACT_SCHEMA = "claude-egress-activation-fact/v1"
ACTIVATION_FACT_CANDIDATE_MODE = "validation_candidate"

# 与宿主脚本 claude_run_candidate_capture.sh 同一组坐标（改一处必须同步另一处）。
CUSTOM_CA_PATH = "/usr/local/share/ca-certificates/claude-candidate-capture.crt"
SERVICE_HOSTS_PATH = "/etc/hosts"
SERVICE_CA_BUNDLE_PATH = "/etc/ssl/certs/ca-certificates.crt"
CANDIDATE_PROXY_PREFIX = "claude-candidate-"
RUNTIME_DIR_PARTS = ("runtime", "claude-candidate")
RUNTIME_LOCK_NAME = ".capture.lock"
DEFAULT_SERVICE_PORT = 8080
EXPECTED_ACCOUNT_SHAPE = "anthropic|oauth|NULL|live"
API_KEY_SHAPE_RE = re.compile(r"^active\|[0-9]+\|live$")
# 宿主脚本只读前检失败的退出码（还没有任何改动，不写恢复报告）；97 是恢复失败。
PRECHECK_EXIT_CODES = frozenset({1, 2})
RESTORATION_FAILED_EXIT_CODE = 97
RESTORATION_EQUAL_FIELDS = (
    "account_proxy_restored_equal",
    "account_gate_restored_equal",
    "hosts_sha256_equal",
    "ca_bundle_sha256_equal",
    "runtime_secrets_removed",
)
# 网关前后快照逐字段比较的字段（点号路径）。不比较：服务容器启动时刻（每步装／撤临时 CA 各重启一次）、观察时刻、
# 账号调度门摘要（每步由宿主脚本按运行前值精确恢复并在恢复报告里核验；步间网关自己的调度也可能合法地改它）。
GATEWAY_COMPARED_FIELDS = (
    "service.container_id",
    "service.image_id",
    "service.running",
    "service.port",
    "service.hosts_sha256",
    "service.ca_bundle_sha256",
    "service.custom_ca_present",
    "keeper.running",
    "account.shape",
    "account.proxy_state",
    "api_key.shape",
    "residual_candidate_proxies",
    "runtime_residue",
)
SHA256_LINE_RE = re.compile(r"^(?P<sha256>[0-9a-f]{64})\s")
SERVICE_INSPECT_TEMPLATE = (
    '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},"started_at":{{json .State.StartedAt}}}'
)
ENV_TEMPLATE = "{{range .Config.Env}}{{println .}}{{end}}"
ADMIN_TOKEN_ENVIRONMENT = ("ADMIN_BEARER_TOKEN", "ADMIN_BEARER_TOKEN_FILE")


# ---------------------------------------------------------------------------
# 候选 Campaign 变量与运行坐标覆盖
# ---------------------------------------------------------------------------


def require_candidate_id(arguments: argparse.Namespace) -> str:
    value = getattr(arguments, "candidate_id", None)
    if not isinstance(value, str) or not claude_upgrade.SAFE_ID_RE.fullmatch(value):
        raise ConfigurationError("capture-candidate run 必须用 --candidate-id 指定合法的候选 ID。")
    return value


def candidate_variables(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Campaign 冻结的候选五个变量（非空、容器名合法、账号与 API Key 是正整数），返回规范化取值。"""

    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ConfigurationError("Claude Campaign 总计划缺少 configuration。")
    missing = [
        name
        for name in claude_upgrade_plan.CANDIDATE_CAMPAIGN_VARIABLES
        if not isinstance(configuration.get(name), str) or not configuration[name]
    ]
    if missing:
        raise ConfigurationError(
            f"候选采集要求 Campaign 冻结候选变量 {missing}（plan 的 --service-container／--keeper-container／"
            "--postgres-container／--claude-account-id／--api-key-id）；B4-01 之前建立的 Campaign 只能做 official 阶段。"
        )
    values: dict[str, Any] = {}
    for name in ("service_container", "keeper_container", "postgres_container"):
        if not capture.SAFE_ID_RE.fullmatch(str(configuration[name])):
            raise ConfigurationError(f"Claude Campaign configuration.{name} 非法。")
        values[name] = str(configuration[name])
    for name in ("claude_account_id", "api_key_id"):
        if not re.fullmatch(r"[1-9][0-9]{0,9}", str(configuration[name])):
            raise ConfigurationError(f"Claude Campaign configuration.{name} 必须是正整数。")
        values[name] = int(str(configuration[name]))
    return values


def runtime_override_path(campaign_dir: Path, candidate_id: str) -> Path:
    if not claude_upgrade.SAFE_ID_RE.fullmatch(str(candidate_id)):
        raise ConfigurationError("候选运行坐标覆盖的 candidate-id 非法。")
    return Path(campaign_dir) / "candidates" / str(candidate_id) / RUNTIME_OVERRIDE_FILENAME


def apply_runtime_override(campaign_dir: Path, manifest: dict[str, Any], candidate_id: str | None) -> dict[str, Any]:
    """服务面 ``_apply_candidate_runtime_override`` 的实现：没有覆盖收据时原样返回清单。

    Claude 没有登记运行坐标覆盖的命令（Codex 的 ``create-candidate-runtime-override`` 没有 Claude 对应物），所以覆盖收据
    只可能来自手工放置或残留，一旦存在即失败关闭——要换运行坐标只能新建 Campaign 或改用新的 candidate-id。
    """

    if not candidate_id:
        return manifest
    path = runtime_override_path(campaign_dir, candidate_id)
    if path.exists() or path.is_symlink():
        raise ConfigurationError(
            f"候选 {candidate_id} 存在运行坐标覆盖收据 {RUNTIME_OVERRIDE_FILENAME}，但 Claude 没有登记覆盖的命令，"
            "无法证明它的来源：拒绝采用（运行坐标只能由 Campaign 冻结）。"
        )
    return manifest


# ---------------------------------------------------------------------------
# 服务容器镜像与 attempt 身份
# ---------------------------------------------------------------------------


def observe_service_image(container: str, *, deadline: Any | None = None, heartbeat: Any | None = None) -> str:
    """服务容器当前运行的 config image ID（只读 ``docker inspect``）。"""

    completed = capture._run_probe_command(
        ["docker", "inspect", "--format", "{{json .Image}}", container],
        operation="candidate:service-image",
        deadline=deadline,
        heartbeat=heartbeat,
    )
    try:
        image_id = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ConfigurationError("服务容器 image ID 输出不是 JSON。") from error
    if not isinstance(image_id, str) or not capture.IMAGE_ID_RE.fullmatch(image_id):
        raise ConfigurationError("无法读取服务容器的实际 image ID。")
    return image_id


def verify_service_image(
    container: str,
    image_reference: str,
    image_id: str,
    *,
    deadline: Any | None = None,
    heartbeat: Any | None = None,
) -> str:
    """服务容器实际 image ID 等于候选构建的 image ID，且候选不可变引用在该镜像的 RepoDigests 里（与 Codex 同一判据）。"""

    actual = observe_service_image(container, deadline=deadline, heartbeat=heartbeat)
    if actual != image_id:
        raise ConfigurationError(f"服务容器 {container} 运行的镜像 {actual} 不是候选构建的镜像 {image_id}。")
    completed = capture._run_probe_command(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", actual],
        operation="candidate:service-image-repo-digests",
        deadline=deadline,
        heartbeat=heartbeat,
    )
    try:
        digests = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ConfigurationError("服务镜像 RepoDigests 输出不是 JSON。") from error
    if not isinstance(digests, list) or image_reference not in digests:
        raise ConfigurationError("候选构建收据的镜像引用不在服务镜像实际 RepoDigests 中。")
    return actual


@dataclass(frozen=True)
class CandidateAdmission:
    """入口门的结论：生效清单与进 attempt 身份的候选事实。"""

    candidate_id: str
    manifest: dict[str, Any]
    variables: dict[str, Any]
    facts: dict[str, Any] = field(default_factory=dict)
    # Store Campaign 的 persona（正式 run 由入口门从 Store 读出，组装 selector 前态快照用；演练没有 Store，为 None）。
    persona: dict[str, Any] | None = None


def _build_receipt_facts(campaign_dir: Path, receipt_path: Path, receipt: Mapping[str, Any], revision: int) -> dict[str, Any]:
    return {
        "candidate_revision": revision,
        "build_receipt": {
            "path": receipt_path.relative_to(campaign_dir).as_posix(),
            "sha256": file_sha256(receipt_path),
            "receipt_digest": str(receipt["receipt_digest"]),
        },
        "build_id": str(receipt["build"]["build_id"]),
        "deployed_version": str(receipt["deployed_version"]),
        "image_reference": str(receipt["image"]["reference"]),
        "image_id": str(receipt["image"]["image_id"]),
        "manifest_digest": str(receipt["image"]["manifest_digest"]),
        "source_root": str(receipt["source"]["root"]),
        "source_tree_sha256": str(receipt["source"]["tree_sha256"]),
        "git_commit": str(receipt["source"]["git_commit"]),
        "profile_digest": str(receipt["profile"]["profile_digest"]),
        "release_sha256": str(receipt["profile"]["release_sha256"]),
        "wire_sha256": str(receipt["profile"]["wire_sha256"]),
        "candidate_frozen_ref": dict(receipt["store"]["candidate_frozen_ref"]),
        "candidate_build_receipt_ref": dict(receipt["store"]["candidate_build_receipt_ref"]),
    }


def _require_source_tree(source_root: str, expected: str) -> None:
    root = Path(source_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"候选源码根不存在或不可信：{source_root}")
    if claude_upgrade._directory_tree_digest(root) != expected:
        raise ConfigurationError("候选源码树与构建收据登记的摘要不一致（构建之后被改动或换包）。")


def formal_admission(
    campaign_dir: Path,
    manifest: dict[str, Any],
    candidate_id: str,
    *,
    deadline: Any | None = None,
    heartbeat: Any | None = None,
    recovery_revision: str | None = None,
) -> CandidateAdmission:
    """正式候选 run 的入口门（见文件头第一节，顺序固定）；任何一项不满足即在预约之前拒绝，零写入。"""

    from tools.official_client_capture import claude_upgrade_candidate as candidate_module
    from tools.official_client_capture import claude_upgrade_profile as profile
    from tools.official_client_capture import claude_upgrade_revision
    from tools.official_client_capture import claude_upgrade_seal
    from tools.official_client_capture import claude_upgrade_store
    from tools.official_client_capture import claude_upgrade_vc

    variables = candidate_variables(manifest)
    # 1. VC-4 checkpoint：编排器侧当前 revision 属于本候选，重放该 revision 的 VC-4，阶段收据就是本候选的构建收据。
    revision, record = claude_upgrade_revision.require_candidate_revision(campaign_dir, manifest, action=ACTION)
    if record is None or record.get("candidate_id") != candidate_id:
        current = record.get("candidate_id") if isinstance(record, Mapping) else None
        raise ConfigurationError(f"{ACTION} 拒绝：候选 {candidate_id} 不属于当前 revision r{revision}（当前候选 {current}）。")
    plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
    _checkpoint_path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, "VC-4", revision=revision)
    receipt_path = claude_upgrade._candidate_build_receipt_path(campaign_dir, candidate_id)
    stage = checkpoint.get("stage_receipt") if isinstance(checkpoint.get("stage_receipt"), Mapping) else {}
    if stage.get("path") != receipt_path.relative_to(campaign_dir).as_posix():
        raise ConfigurationError(f"r{revision} 的 VC-4 checkpoint 绑定的不是候选 {candidate_id} 的构建收据。")
    # 2. Store：工具身份、candidate_sealed、Store 当前 revision 与冻结事实。
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    claude_upgrade_seal._require_store_tool_current(store, store_campaign)
    status, facts = profile._store_state(store, str(store_campaign["campaign_id"]))
    if status != STORE_CANDIDATE_SEALED:
        raise ConfigurationError(f"{ACTION} 要求 Store 检查点为 {STORE_CANDIDATE_SEALED}（当前 {status}）。")
    if candidate_module.require_candidate_revision(store, store_campaign, candidate_id, action=ACTION) != revision:
        raise ConfigurationError(f"{ACTION} 拒绝：Store 的当前候选 revision 与编排器激活的 r{revision} 不一致。")
    frozen = [
        fact
        for fact in facts.get("candidate_frozen", [])
        if (fact.get("formed_under") or {}).get("candidate_revision") == revision
    ]
    if len(frozen) != 1 or frozen[0]["payload"].get("candidate_id") != candidate_id:
        raise ConfigurationError(f"{ACTION} 拒绝：Store 在 r{revision} 没有唯一的候选 {candidate_id} 冻结事实。")
    candidate_module.require_timing_ledger_active(campaign_dir, manifest, action=ACTION)
    # 3. 构建收据重放。
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ConfigurationError("候选构建收据缺失或路径不可信。")
    if file_sha256(receipt_path) != stage.get("sha256"):
        raise ConfigurationError("候选构建收据与 VC-4 checkpoint 绑定的摘要不一致。")
    try:
        receipt = vc_artifacts.validate_official_client_candidate_build_receipt(claude_upgrade._read_json(receipt_path, "候选构建收据"))
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"候选构建收据非法：{error}") from error
    expected = {
        "client": "claude",
        "campaign_id": manifest.get("campaign_id"),
        "candidate_id": candidate_id,
        "candidate_revision": revision,
        "candidate_purpose": manifest.get("campaign_purpose"),
        "target_version": manifest.get("target_version"),
    }
    drift = sorted(key for key, value in expected.items() if receipt.get(key) != value)
    if drift:
        raise ConfigurationError(f"候选构建收据与 Campaign／候选／revision 不一致：{drift}")
    frozen_payload = frozen[0]["payload"]
    if dict(receipt["store"]["candidate_frozen_ref"]) != dict(store.fact_ref(frozen[0])):
        raise ConfigurationError("候选构建收据的 candidate_frozen_ref 不是 Store 当前 revision 的冻结事实。")
    if dict(receipt["store"]["candidate_build_receipt_ref"]) != dict(frozen_payload.get("candidate_build_receipt_ref") or {}):
        raise ConfigurationError("候选构建收据的 CandidateBuildReceipt 引用与 Store 冻结事实不一致。")
    if (
        frozen_payload.get("build_id") != receipt["build"]["build_id"]
        or frozen_payload.get("image_digest") != receipt["image"]["manifest_digest"]
    ):
        raise ConfigurationError("候选构建收据的构建 ID 或镜像摘要与 Store 冻结事实不一致。")
    _require_source_tree(str(receipt["source"]["root"]), str(receipt["source"]["tree_sha256"]))
    # 4. 服务容器镜像。
    verify_service_image(
        variables["service_container"],
        str(receipt["image"]["reference"]),
        str(receipt["image"]["image_id"]),
        deadline=deadline,
        heartbeat=heartbeat,
    )
    # 5. 运行坐标覆盖（Claude 恒为失败关闭或原样返回）。
    effective = apply_runtime_override(campaign_dir, manifest, candidate_id)
    # 6. 未封存。
    if recovery_revision is None:
        require_unsealed(campaign_dir, candidate_id, revision=revision)
    else:
        from tools.official_client_capture import claude_upgrade_attempt_segment

        claude_upgrade_attempt_segment.validate_recovery_admission(campaign_dir, candidate_id, recovery_revision)
    return CandidateAdmission(
        candidate_id=candidate_id,
        manifest=effective,
        variables=variables,
        facts=_build_receipt_facts(campaign_dir, receipt_path, receipt, revision),
        persona=dict(store_campaign["persona"]),
    )


def rehearsal_admission(
    campaign_dir: Path,
    manifest: dict[str, Any],
    candidate_id: str,
    *,
    deadline: Any | None = None,
    heartbeat: Any | None = None,
) -> CandidateAdmission:
    """零请求演练的入口门：演练 Campaign 没有 VC 链、Store 与构建收据，只核候选变量与覆盖收据，镜像取当前运行值。"""

    variables = candidate_variables(manifest)
    effective = apply_runtime_override(campaign_dir, manifest, candidate_id)
    image_id = observe_service_image(variables["service_container"], deadline=deadline, heartbeat=heartbeat)
    facts = {
        "candidate_revision": None,
        "build_receipt": None,
        "build_id": None,
        "deployed_version": None,
        "image_reference": None,
        "image_id": image_id,
        "manifest_digest": None,
        "source_root": None,
        "source_tree_sha256": None,
        "git_commit": None,
        "profile_digest": None,
        "release_sha256": None,
        "wire_sha256": None,
        "candidate_frozen_ref": None,
        "candidate_build_receipt_ref": None,
    }
    return CandidateAdmission(candidate_id=candidate_id, manifest=effective, variables=variables, facts=facts)


def legacy_stage_result_path(campaign_dir: Path, candidate_id: str) -> Path:
    """候选阶段结果的 b0 路径（与 Codex 活跃判定同一取法；b≥1 的恢复段属于 B4-01c）。"""

    return claude_upgrade._legacy_stage_path(Path(campaign_dir), "capture-candidate", candidate_id)[1]


def require_unsealed(campaign_dir: Path, candidate_id: str, *, revision: int | None) -> None:
    """候选证据尚未封存：没有候选阶段结果，该 revision 的 VC-5 checkpoint 不存在。"""

    from tools.official_client_capture import claude_upgrade_vc

    result = legacy_stage_result_path(campaign_dir, candidate_id)
    if result.exists() or result.is_symlink():
        raise ConfigurationError(f"候选 {candidate_id} 的证据已经封存，禁止再建候选 attempt。")
    if revision is not None:
        checkpoint = claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-5", revision=revision)
        if checkpoint.exists() or checkpoint.is_symlink():
            raise ConfigurationError(f"r{revision} 的 VC-5 checkpoint 已存在，候选采集已收口。")


def gateway_coordinates(variables: Mapping[str, Any], *, service_port: int) -> dict[str, Any]:
    return {
        "base_url": f"http://{variables['service_container']}:{int(service_port)}",
        "service_container": variables["service_container"],
        "service_port": int(service_port),
        "keeper_container": variables["keeper_container"],
        "postgres_container": variables["postgres_container"],
        "account_id": int(variables["claude_account_id"]),
        "api_key_id": int(variables["api_key_id"]),
        "relay_port": int(claude_upgrade_plan.CANDIDATE_RELAY_PORT),
    }


def attempt_identity(
    manifest: Mapping[str, Any],
    layout: Any,
    mode: str,
    admission: CandidateAdmission,
) -> dict[str, Any]:
    """候选 attempt 的身份（预约收据的 ``identity_sha256`` 绑定它；不含 nonce、端口与证据路径）。"""

    configuration = manifest["configuration"]
    return {
        "schema_version": CANDIDATE_IDENTITY_SCHEMA,
        "client": capture.CLIENT,
        "phase": "candidate",
        "mode": mode,
        "candidate_id": admission.candidate_id,
        "candidate_purpose": manifest.get("campaign_purpose"),
        "target_version": str(manifest["target_version"]),
        "target_sha256": str(manifest["target_sha256"]),
        "official_identity_sha256": claude_upgrade._fingerprint(dict(manifest["official_identity"])),
        "runtime_image": layout.runtime_image,
        "capture_container": layout.capture_container,
        "capture_tool_root": str(layout.capture_tool_root),
        "claude_bin": str(configuration["claude_bin"]),
        "service_container": admission.variables["service_container"],
        "keeper_container": admission.variables["keeper_container"],
        "postgres_container": admission.variables["postgres_container"],
        "account_id": admission.variables["claude_account_id"],
        "api_key_id": admission.variables["api_key_id"],
        **admission.facts,
    }


def verify_attempt_identity(manifest: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    """服务面 ``_verify_candidate_attempt_identity`` 的实现：seal 与恢复前重验，防止 run／seal 之间换源码或换镜像。

    只接受正式候选 attempt 的身份：Campaign 用途一致、服务容器仍是冻结坐标、源码树按同一算法复算摘要相等、服务容器实际
    镜像等于候选构建镜像且不可变引用在 RepoDigests 里。
    """

    if (
        identity.get("schema_version") != CANDIDATE_IDENTITY_SCHEMA
        or identity.get("client") != capture.CLIENT
        or identity.get("phase") != "candidate"
        or identity.get("mode") != capture.MODE_FORMAL
    ):
        raise ConfigurationError("不是 Claude 正式候选 attempt 的身份。")
    if identity.get("candidate_purpose") != manifest.get("campaign_purpose"):
        raise ConfigurationError("候选 attempt 用途与 Campaign 冻结用途不一致。")
    variables = candidate_variables(manifest)
    if identity.get("service_container") != variables["service_container"]:
        raise ConfigurationError("候选 attempt 的服务容器与 Campaign 冻结坐标不一致。")
    _require_source_tree(str(identity.get("source_root", "")), str(identity.get("source_tree_sha256", "")))
    verify_service_image(
        variables["service_container"], str(identity.get("image_reference", "")), str(identity.get("image_id", ""))
    )


def require_admin_token_for(jobs: Sequence[Any]) -> None:
    """账号刷新触发的步骤由宿主脚本读管理凭据：预约之前确认环境里给出了（有效期由宿主脚本核对）。"""

    triggers = {
        str(dict(step.get("environment") or {}).get("GATEWAY_TRIGGER", ""))
        for job in jobs
        for step in job.steps
    }
    if claude_upgrade_plan.CANDIDATE_TRIGGER_ACCOUNT_REFRESH not in triggers:
        return
    if os.environ.get("ADMIN_BEARER_TOKEN"):
        return
    token_file = os.environ.get("ADMIN_BEARER_TOKEN_FILE", "")
    path = Path(token_file)
    if token_file and path.is_absolute() and path.is_file() and not path.is_symlink():
        return
    raise ConfigurationError(
        "计划里有网关账号刷新触发的步骤：先签发管理 token，并用 ADMIN_BEARER_TOKEN_FILE 指向只读 token 文件（不要放进命令行）。"
    )


# ---------------------------------------------------------------------------
# 网关环境只读快照（attempt 级 before／after）
# ---------------------------------------------------------------------------


def _sha256_line(stdout: str, label: str) -> str:
    match = SHA256_LINE_RE.match(stdout)
    if match is None:
        raise ConfigurationError(f"{label} 的 sha256sum 输出格式非法。")
    return match.group("sha256")


def _env_value(lines: str, name: str) -> str | None:
    for line in lines.splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    return None


def _collect_gateway_facts(
    manifest: Mapping[str, Any],
    host_data_root: Path,
    *,
    deadline: Any | None,
    heartbeat: Any | None,
) -> dict[str, Any]:
    """只读采集网关环境事实：docker inspect、容器内 sha256sum／test、PostgreSQL 只读查询与宿主运行目录列表。

    不读也不记录任何秘密（API Key 值、账号令牌、容器环境变量里除端口外的任何项都不进快照）。
    """

    variables = candidate_variables(manifest)
    service, keeper, postgres = variables["service_container"], variables["keeper_container"], variables["postgres_container"]
    account_id, api_key_id = int(variables["claude_account_id"]), int(variables["api_key_id"])

    def run(argv: list[str], operation: str, *, check: bool = True) -> Any:
        return capture._run_probe_command(argv, operation=f"candidate-gateway:{operation}", deadline=deadline, heartbeat=heartbeat, check=check)

    try:
        state = json.loads(run(["docker", "inspect", "--format", SERVICE_INSPECT_TEMPLATE, service], "service").stdout)
    except json.JSONDecodeError as error:
        raise ConfigurationError("服务容器 inspect 输出不是 JSON。") from error
    if (
        not isinstance(state, Mapping)
        or not capture.CONTAINER_ID_RE.fullmatch(str(state.get("id", "")))
        or not capture.IMAGE_ID_RE.fullmatch(str(state.get("image", "")))
        or type(state.get("running")) is not bool
    ):
        raise ConfigurationError("服务容器身份字段非法。")
    port_value = _env_value(run(["docker", "inspect", "--format", ENV_TEMPLATE, service], "service-port").stdout, "SERVER_PORT")
    port = int(port_value) if port_value and re.fullmatch(r"[1-9][0-9]{0,4}", port_value) else DEFAULT_SERVICE_PORT
    hosts = _sha256_line(run(["docker", "exec", service, "sha256sum", SERVICE_HOSTS_PATH], "service-hosts").stdout, "服务容器 hosts")
    bundle = _sha256_line(run(["docker", "exec", service, "sha256sum", SERVICE_CA_BUNDLE_PATH], "service-ca").stdout, "服务容器 CA bundle")
    custom = run(["docker", "exec", service, "test", "-e", CUSTOM_CA_PATH], "service-custom-ca", check=False).returncode
    if custom not in (0, 1):
        raise ConfigurationError("无法确认服务容器里是否残留临时 CA。")
    try:
        keeper_running = json.loads(run(["docker", "inspect", "--format", "{{json .State.Running}}", keeper], "keeper").stdout)
    except json.JSONDecodeError as error:
        raise ConfigurationError("keeper 容器状态输出不是 JSON。") from error
    if type(keeper_running) is not bool:
        raise ConfigurationError("keeper 容器运行状态非法。")
    postgres_env = run(["docker", "inspect", "--format", ENV_TEMPLATE, postgres], "postgres-env").stdout
    user, database = _env_value(postgres_env, "POSTGRES_USER"), _env_value(postgres_env, "POSTGRES_DB")
    if not user or not database:
        raise ConfigurationError("无法读取 PostgreSQL 连接元数据。")

    def query(sql: str, operation: str) -> str:
        return str(run(["docker", "exec", postgres, "psql", "-U", user, "-d", database, "-qAtc", sql], f"db:{operation}").stdout).strip()

    account_shape = query(
        "select platform || '|' || type || '|' || coalesce(parent_account_id::text,'NULL') || '|' || "
        f"case when deleted_at is null then 'live' else 'deleted' end from accounts where id = {account_id}",
        "account-shape",
    )
    proxy_state = query(
        f"select coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = {account_id}",
        "account-proxy",
    )
    gate = query(
        "select coalesce(temp_unschedulable_until::text,'') || '|' || coalesce(temp_unschedulable_reason,'') "
        f"from accounts where id = {account_id}",
        "account-gate",
    )
    api_key_shape = query(
        "select status || '|' || coalesce(group_id::text,'NULL') || '|' || "
        f"case when deleted_at is null then 'live' else 'deleted' end from api_keys where id = {api_key_id}",
        "api-key-shape",
    )
    residual = query(f"select count(*) from proxies where name like '{CANDIDATE_PROXY_PREFIX}%'", "residual-proxies")
    if not re.fullmatch(r"[0-9]+", residual):
        raise ConfigurationError("残留临时代理计数输出非法。")
    runtime_root = Path(host_data_root).joinpath(*RUNTIME_DIR_PARTS)
    residue: list[str] = []
    if runtime_root.exists() or runtime_root.is_symlink():
        if runtime_root.is_symlink() or not runtime_root.is_dir():
            raise ConfigurationError("宿主候选运行目录不可信。")
        residue = sorted(item.name for item in runtime_root.iterdir() if item.name != RUNTIME_LOCK_NAME)
    return {
        "service": {
            "container": service,
            "container_id": str(state["id"]),
            "image_id": str(state["image"]),
            "running": bool(state["running"]),
            "started_at": str(state.get("started_at", "")),
            "port": port,
            "hosts_sha256": hosts,
            "ca_bundle_sha256": bundle,
            "custom_ca_present": custom == 0,
        },
        "keeper": {"container": keeper, "running": bool(keeper_running)},
        "postgres": {"container": postgres},
        "account": {
            "id": account_id,
            "shape": account_shape,
            "proxy_state": proxy_state,
            "gate_sha256": hashlib.sha256(gate.encode("utf-8")).hexdigest(),
        },
        "api_key": {"id": api_key_id, "shape": api_key_shape},
        "residual_candidate_proxies": int(residual),
        "runtime_residue": residue,
    }


def probe_gateway_environment(
    manifest: Mapping[str, Any],
    host_data_root: Path,
    phase: str,
    *,
    deadline: Any | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """一份网关环境快照（``claude-upgrade-candidate-gateway-environment/v1``）；``phase`` 取 readiness／before／after。"""

    if phase not in {"readiness", "before", "after"}:
        raise ConfigurationError("网关环境快照 phase 非法。")
    facts = _collect_gateway_facts(manifest, host_data_root, deadline=deadline, heartbeat=heartbeat)
    return {"schema_version": GATEWAY_ENVIRONMENT_SCHEMA, "phase": phase, "observed_at_utc": claude_upgrade._utc_now(), **facts}


def readiness_failures(snapshot: Mapping[str, Any]) -> list[str]:
    """网关就绪核对：服务运行、临时 CA 不在、没有残留临时代理与运行目录、专用账号与 API Key 形态正确。"""

    failures: list[str] = []
    service = snapshot.get("service") or {}
    if service.get("running") is not True:
        failures.append("service-not-running")
    if service.get("custom_ca_present") is not False:
        failures.append("custom-ca-present")
    if snapshot.get("residual_candidate_proxies") != 0:
        failures.append("residual-candidate-proxy")
    if snapshot.get("runtime_residue"):
        failures.append("runtime-residue")
    if (snapshot.get("account") or {}).get("shape") != EXPECTED_ACCOUNT_SHAPE:
        failures.append("account-shape")
    if not API_KEY_SHAPE_RE.fullmatch(str((snapshot.get("api_key") or {}).get("shape", ""))):
        failures.append("api-key-shape")
    return failures


def _field(snapshot: Mapping[str, Any], dotted: str) -> Any:
    value: Any = snapshot
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    return value


def gateway_drift(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    return [name for name in GATEWAY_COMPARED_FIELDS if _field(before, name) != _field(after, name)]


def write_gateway_snapshot(environment_root: Path, snapshot: Mapping[str, Any]) -> Path:
    name = GATEWAY_BEFORE_FILENAME if snapshot["phase"] == "before" else GATEWAY_AFTER_FILENAME
    path = environment_root / name
    claude_upgrade._secure_write_json_once(path, dict(snapshot))
    return path


class ActivationFactInvalid(ConfigurationError):
    """网关的 Claude 激活事实读到了，但 schema 或 profile_mode 不对（不是候选 Runtime）。"""


def _read_activation_fact(manifest: Mapping[str, Any]) -> tuple[str, str]:
    """docker 边界：从服务容器环境取激活事实路径，在容器内只读 ``cat`` 出全文；返回 ``(容器内路径, 原文)``。"""

    service = candidate_variables(manifest)["service_container"]
    environment = capture._run_probe_command(
        ["docker", "inspect", "--format", ENV_TEMPLATE, service], operation="candidate-gateway:activation-fact-path"
    ).stdout
    path = _env_value(environment, ACTIVATION_FACT_PATH_ENV)
    if not path or not capture.SAFE_ABSOLUTE_PATH_RE.fullmatch(path) or ".." in PurePosixPath(path).parts:
        raise ConfigurationError(f"服务容器没有配置 {ACTIVATION_FACT_PATH_ENV}，或路径不是安全绝对路径。")
    completed = capture._run_probe_command(["docker", "exec", service, "cat", path], operation="candidate-gateway:activation-fact")
    return path, completed.stdout


def copy_activation_fact(manifest: Mapping[str, Any], environment_root: Path, *, evidence_root: Path) -> dict[str, Any]:
    """只读复制 Go 写出的 Claude 激活事实到 ``<attempt>/evidence/environment/claude-egress-activation-fact.json``（只写一次）。

    校验：JSON 对象、``schema_version`` 为 ``claude-egress-activation-fact/v1``、``profile_mode`` 为 ``validation_candidate``
    （候选 Runtime）；任一不成立即失败关闭。不改网关、不写 Store。返回写进 ``gateway_environment.activation_fact`` 的绑定。
    """

    source_path, raw = _read_activation_fact(manifest)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfigurationError("网关的 Claude 激活事实不是 JSON。") from error
    if not isinstance(payload, Mapping) or payload.get("schema_version") != ACTIVATION_FACT_SCHEMA:
        raise ActivationFactInvalid(f"网关的 Claude 激活事实 schema 不是 {ACTIVATION_FACT_SCHEMA}。")
    if payload.get("profile_mode") != ACTIVATION_FACT_CANDIDATE_MODE:
        raise ActivationFactInvalid(
            f"网关的 Claude 激活事实 profile_mode 是 {payload.get('profile_mode')!r}，不是候选 Runtime（{ACTIVATION_FACT_CANDIDATE_MODE}）。"
        )
    target = environment_root / ACTIVATION_FACT_FILENAME
    capture._exclusive_write_bytes(target, raw.encode("utf-8"))
    return {
        **capture._probe_binding(evidence_root, target),
        "source_path": source_path,
        "schema_version": ACTIVATION_FACT_SCHEMA,
        "profile_mode": str(payload["profile_mode"]),
        "event_id": payload.get("event_id"),
    }


# ---------------------------------------------------------------------------
# 强杀后的网关残留（B4-01 c2）：只读列出与按批准调用宿主脚本的 --restore-only
# ---------------------------------------------------------------------------

RESTORE_STATE_FILENAME = "state.json"
RESTORE_STATE_SCHEMA = "claude-candidate-gateway-restore-state/v1"
CLEANUP_REPORT_SCHEMA = "claude-candidate-gateway-cleanup/v1"
RESTORE_ONLY_FLAG = "--restore-only"
RESTORE_ONLY_TIMEOUT_SECONDS = 600
# 网关就绪核对里属于「取证留下的改动」的项（其余如账号形态不对不是残留，清理不处理）。
RESIDUE_FAILURES = frozenset({"custom-ca-present", "residual-candidate-proxy", "runtime-residue"})
# 宿主脚本写的运行前原值字段（与 claude_run_candidate_capture.sh 的 state.json 同一闭集；脚本恢复前另按正则逐项校验）。
RESTORE_STATE_FIELDS = frozenset(
    {
        "run_id", "probe_id", "account_id", "api_key_id", "relay_port", "gateway_trigger", "service_container", "keeper_container",
        "postgres_container", "capture_name", "proxy_name", "service_container_id_before", "original_proxy_state",
        "original_gate_state", "keeper_was_running", "original_hosts_hash", "original_ca_hash", "started_at",
    }
)


def gateway_cleanup_enabled(manifest: Mapping[str, Any]) -> bool:
    """总计划冻结了候选变量（会跑候选采集）的 Campaign 才有候选网关残留可清理。"""

    try:
        candidate_variables(manifest)
    except ConfigurationError:
        return False
    return True


def candidate_runtime_root(host_data_root: Path) -> Path:
    return Path(host_data_root).joinpath(*RUNTIME_DIR_PARTS)


def _restore_state_entry(directory: Path) -> tuple[dict[str, Any] | None, str | None]:
    """一个宿主运行目录的运行前原值摘要（只读）；没有或不合法时返回 ``(None, 原因)``。"""

    if directory.is_symlink() or not directory.is_dir():
        return None, "不是普通目录"
    path = directory / "restore" / RESTORE_STATE_FILENAME
    if path.is_symlink() or not path.is_file():
        return None, "没有运行前原值（restore/state.json），无法证明要恢复成什么"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "restore/state.json 无法读取"
    values = document.get("values") if isinstance(document, Mapping) else None
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != RESTORE_STATE_SCHEMA
        or not isinstance(values, Mapping)
        or set(values) != RESTORE_STATE_FIELDS
        or not all(isinstance(value, str) for value in values.values())
    ):
        return None, "restore/state.json 的 schema 或字段不闭合"
    return {
        "name": directory.name,
        "run_id": values["run_id"],
        "probe_id": values["probe_id"],
        "account_id": values["account_id"],
        "api_key_id": values["api_key_id"],
        "proxy_name": values["proxy_name"],
        "keeper_was_running": values["keeper_was_running"],
        "started_at_utc": values["started_at"],
        "state_sha256": file_sha256(path),
    }, None


def list_gateway_residue(manifest: Mapping[str, Any], host_data_root: Path, *, deadline: Any | None = None) -> dict[str, Any]:
    """只读列出候选网关残留：宿主候选运行目录（带运行前原值的可自动恢复，没有的记无法自动恢复）与网关只读快照。

    网关快照显示有改动残留（临时 CA、临时代理）却没有任何可恢复的运行目录时，同样记无法自动恢复（需人工处置）。
    返回 ``summary``（进预览与批准摘要，不含观测时刻）与 ``snapshot``（留证）。
    """

    entries: list[dict[str, Any]] = []
    unrecoverable: list[dict[str, Any]] = []
    root = candidate_runtime_root(host_data_root)
    if root.exists() or root.is_symlink():
        if root.is_symlink() or not root.is_dir():
            raise ConfigurationError("宿主候选运行目录不可信。")
        for child in sorted(root.iterdir()):
            if child.name == RUNTIME_LOCK_NAME:
                continue
            entry, reason = _restore_state_entry(child)
            if entry is not None:
                entries.append(entry)
            else:
                unrecoverable.append({"name": child.name, "reason": reason})
    snapshot = probe_gateway_environment(manifest, host_data_root, "readiness", deadline=deadline)
    failures = readiness_failures(snapshot)
    residue = sorted(set(failures) & RESIDUE_FAILURES)
    mutations = sorted(set(residue) - {"runtime-residue"})
    if mutations and not entries:
        unrecoverable.append({"name": None, "reason": f"网关有改动残留 {mutations}，但没有带运行前原值的运行目录可以恢复"})
    return {
        "summary": {"entries": entries, "unrecoverable": unrecoverable, "residue_failures": residue},
        "snapshot": snapshot,
    }


def residue_present(summary: Mapping[str, Any] | None) -> bool:
    return bool(summary) and bool(summary.get("entries") or summary.get("unrecoverable") or summary.get("residue_failures"))


def _run_restore_only(argv: Sequence[str], environment: Mapping[str, str], timeout: float) -> Any:
    """执行边界（离线测试替换它）：宿主脚本的 --restore-only。"""

    import subprocess

    return subprocess.run(list(argv), env=dict(environment), capture_output=True, text=True, timeout=timeout, check=False)


def restore_runtime_entry(layout: Any, host_data_root: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    """对一个带运行前原值的宿主运行目录执行宿主脚本的 ``--restore-only``（工具执行副本里的同一脚本），返回结果与报告。"""

    target = candidate_runtime_root(host_data_root) / str(entry["name"])
    script = capture._host_path(f"{layout.capture_tool_root}/{claude_upgrade_plan.CANDIDATE_SCRIPT}", layout)
    environment = {**os.environ, "CLAUDE_CANDIDATE_CAPTURE": claude_upgrade_plan.CANDIDATE_CAPTURE_GATE}
    try:
        completed = _run_restore_only(["bash", str(script), RESTORE_ONLY_FLAG, str(target)], environment, RESTORE_ONLY_TIMEOUT_SECONDS)
    except Exception as error:  # noqa: BLE001 —— 超时或无法启动一律记失败，由调用方判清理未完成
        return {"name": entry["name"], "exit_code": None, "status": "failed", "error": f"{type(error).__name__}: {str(error)[:300]}", "report": None}
    report: dict[str, Any] | None = None
    lines = [line for line in str(completed.stdout).splitlines() if line.strip()]
    if lines:
        try:
            parsed = json.loads(lines[-1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping) and parsed.get("schema_version") == CLEANUP_REPORT_SCHEMA:
            report = dict(parsed)
    restored = completed.returncode == 0 and report is not None and report.get("status") == "restored" and not target.exists()
    return {
        "name": entry["name"],
        "exit_code": completed.returncode,
        "status": "restored" if restored else "failed",
        "error": None if restored else str(completed.stderr)[-500:],
        "report": report,
    }


# ---------------------------------------------------------------------------
# 生产 selector 只读观察
# ---------------------------------------------------------------------------


def _release_summary(catalog: Mapping[str, Any], selector: Any) -> dict[str, Any] | None:
    if not isinstance(selector, Mapping) or selector.get("kind") != "release":
        return None
    matches = [
        item
        for item in catalog.get("releases") or []
        if isinstance(item, Mapping) and item.get("release_sha256") == selector.get("release_sha256")
    ]
    if len(matches) != 1:
        raise ConfigurationError("Active Release 目录的选择器指向的 Release 不唯一或不存在。")
    release = matches[0]
    return {
        "release_sha256": str(release["release_sha256"]),
        "version": str(release.get("version", "")),
        "platform": str(release.get("platform", "")),
        "profile_sha256": str((release.get("profile") or {}).get("sha256", "")),
        "wire_sha256": str((release.get("wire") or {}).get("sha256", "")),
        "bundle_sha256": str(release.get("bundle_sha256", "")),
    }


def observe_selectors(campaign_dir: Path, manifest: Mapping[str, Any], *, required: bool) -> dict[str, Any] | None:
    """生产 selector 的只读快照：读 Campaign 冻结的 Active Release 目录（候选验证不连接、不修改生产环境）。

    记目录文件绑定（Campaign 内相对路径、摘要与字节数，可直接作 runtime_catalog_snapshot 的 ``source_ref``）、两个生产
    选择器原样与它们指向的 Release 摘要；``production_rollback`` 不是 Release（例如运维部署）时只记选择器、Release 为 null。
    """

    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    binding = inputs.get("active_release_catalog")
    if binding is None:
        if required:
            raise ConfigurationError("正式 Campaign 没有冻结 Active Release 目录（inputs.active_release_catalog），无法观察生产 selector。")
        return None
    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
        raise ConfigurationError("Campaign 的 Active Release 目录绑定非法。")
    path = claude_upgrade._campaign_file(Path(campaign_dir), str(binding["path"]))
    if path.is_symlink() or not path.is_file() or file_sha256(path) != binding.get("sha256"):
        raise ConfigurationError("Campaign 冻结的 Active Release 目录缺失或摘要漂移。")
    catalog = claude_upgrade._read_json(path, "Active Release 目录")
    selectors = catalog.get("selectors")
    if not isinstance(selectors, Mapping) or not isinstance(selectors.get("production_active"), Mapping):
        raise ConfigurationError("Active Release 目录没有 production_active 选择器。")
    active = _release_summary(catalog, selectors["production_active"])
    if active is None:
        raise ConfigurationError("Active Release 目录的 production_active 选择器不是 Release。")
    rollback_selector = selectors.get("production_rollback")
    return {
        "schema_version": SELECTOR_OBSERVATION_SCHEMA,
        "kind": "read_only",
        "observed_at_utc": claude_upgrade._utc_now(),
        "source": {"path": path.relative_to(Path(campaign_dir)).as_posix(), "sha256": file_sha256(path), "bytes": path.stat().st_size},
        "catalog_schema_version": str(catalog.get("schema_version", "")),
        "selectors": {
            "production_active": dict(selectors["production_active"]),
            "production_rollback": dict(rollback_selector) if isinstance(rollback_selector, Mapping) else None,
        },
        "production_active_release": active,
        "production_rollback_release": _release_summary(catalog, rollback_selector),
    }


RUNTIME_CATALOG_SNAPSHOT_SCHEMA = "official-client-runtime-catalog-snapshot/v1"


def runtime_catalog_snapshot(observation: Mapping[str, Any], persona: Mapping[str, Any]) -> dict[str, Any]:
    """由 selector 只读观察组装 Store 的 ``runtime_catalog_snapshot`` 对象 payload（候选 attempt 的
    ``candidate_gateway.selector_before_snapshot``；VC-5 收口的后态快照应调用同一函数，保证前后判据同源）。

    * ``catalog_digest``：Campaign 冻结的 Active Release 目录文件的 SHA-256；``source_ref`` 是它的 Campaign 内相对绑定；
    * ``production_active_ref``／``production_rollback_ref``：Store 只登记候选侧 ReleaseArtifact，生产 Active（运行中的
      Release）与 Rollback（运维部署）都不在 Store 里，一律为 null——候选验证不连接、不修改、不验证生产环境，前后态由
      同一份冻结目录给出，``catalog_digest`` 与两个引用前后必然相等。
    """

    source = observation["source"]
    return {
        "schema_version": RUNTIME_CATALOG_SNAPSHOT_SCHEMA,
        "persona": dict(persona),
        "catalog_digest": str(source["sha256"]),
        "production_active_ref": None,
        "production_rollback_ref": None,
        "observed_at_utc": str(observation["observed_at_utc"]),
        "source_ref": {"path": str(source["path"]), "sha256": str(source["sha256"]), "bytes": int(source["bytes"])},
    }


# ---------------------------------------------------------------------------
# 每步：relay 清单的候选核对与恢复报告核对
# ---------------------------------------------------------------------------


def relay_manifest_expectation(manifest: Mapping[str, Any]) -> dict[str, Any]:
    variables = candidate_variables(manifest)
    return {"account_id": variables["claude_account_id"], "api_key_id": variables["api_key_id"]}


def candidate_manifest_failure(payload: Mapping[str, Any], expectation: Mapping[str, Any]) -> str | None:
    """中继候选清单的候选身份：side、网关账号与 API Key、没有读官方 OAuth 凭据、connect 上游。"""

    gateway = payload.get("candidate_gateway") if isinstance(payload.get("candidate_gateway"), Mapping) else {}
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), Mapping) else {}
    if (
        payload.get("side") != "candidate"
        or gateway.get("account_id") != expectation["account_id"]
        or gateway.get("api_key_id") != expectation["api_key_id"]
        or gateway.get("oauth_credentials_read") is not False
        or upstream.get("relay_mode") != "connect"
    ):
        return "candidate-mismatch"
    return None


def candidate_plan_failure(plan: Mapping[str, Any]) -> str | None:
    if plan.get("side") != "candidate" or plan.get("relay_mode") != "connect":
        return "plan-mismatch"
    return None


def restoration_check(row: Mapping[str, Any], layout: Any, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """一个执行过的候选步骤的恢复报告核对（见文件头第三节）；没有执行（argv 为空）的步骤返回 None。"""

    argv = [str(item) for item in row.get("argv") or []]
    if not argv:
        return None
    coordinates = capture._relay_step_coordinates(argv)
    output_root = PurePosixPath(str(coordinates["output_root"]))
    report = output_root.parent / RESTORATION_REPORT_DIRNAME / f"{coordinates['probe_id']}.json"
    host = capture._host_path(report, layout)
    return_code = row.get("return_code")
    # 行形状与 B4-02 证据侧约定：{probe_id, status, report: {path, sha256}}（路径是逻辑坐标，同 relay_checks）；
    # 失败行另带 failure_code。报告状态与退出码在恢复报告文件里，不另记。
    result: dict[str, Any] = {"probe_id": coordinates["probe_id"], "status": "failed", "report": None}
    if host.is_symlink() or not host.is_file():
        if isinstance(return_code, int) and return_code in PRECHECK_EXIT_CODES:
            # 宿主脚本在任何修改之前的只读前检里拒绝（或参数不合同），没有改动可恢复。
            result["status"] = "not_started"
            return result
        result["failure_code"] = "report-missing"
        return result
    try:
        payload = json.loads(host.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        result["failure_code"] = "report-unreadable"
        return result
    result["report"] = {"path": str(report), "sha256": file_sha256(host)}
    expectation = relay_manifest_expectation(manifest)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != RESTORATION_REPORT_SCHEMA:
        result["failure_code"] = "report-schema"
    elif (
        payload.get("run_id") != coordinates["run_id"]
        or payload.get("probe_id") != coordinates["probe_id"]
        or payload.get("account_id") != expectation["account_id"]
        or payload.get("api_key_id") != expectation["api_key_id"]
    ):
        result["failure_code"] = "report-identity"
    elif payload.get("status") == "restoration_failed" or return_code == RESTORATION_FAILED_EXIT_CODE:
        result["failure_code"] = "restoration-failed"
    elif payload.get("status") not in {"complete", "capture_failed"}:
        result["failure_code"] = "report-status"
    elif (payload.get("status") == "complete") != (return_code == 0):
        result["failure_code"] = "report-exit-mismatch"
    elif not all(payload.get(name) is True for name in RESTORATION_EQUAL_FIELDS):
        result["failure_code"] = "restoration-unverified"
    else:
        result["status"] = "passed"
    return result


def restoration_contaminated(result: Mapping[str, Any]) -> bool:
    return any(item.get("status") == "failed" for item in result.get("restoration_checks") or [])


# ---------------------------------------------------------------------------
# attempt 末：恢复核验、运行条件与 attempt 字段
# ---------------------------------------------------------------------------


def write_recovery(
    environment_root: Path,
    results: Sequence[Mapping[str, Any]],
    *,
    before: Path | None,
    after: Path | None,
    drifted: Sequence[str],
    evidence_root: Path,
) -> tuple[Path, str]:
    """网关隔离恢复核验（``claude-upgrade-candidate-gateway-recovery/v1``）：每步恢复报告核对的汇总加前后快照比较。"""

    checks = [
        {"job_id": str(result.get("id")), **dict(item)}
        for result in results
        for item in result.get("restoration_checks") or []
    ]
    environment_equal = before is not None and after is not None and not drifted
    status = "passed" if environment_equal and all(item["status"] != "failed" for item in checks) else "failed"
    document = {
        "schema_version": GATEWAY_RECOVERY_SCHEMA,
        "status": status,
        "restoration_checks": checks,
        "environment_equal": environment_equal,
        "compared_fields": list(GATEWAY_COMPARED_FIELDS),
        "drifted_fields": list(drifted),
        "gateway_before": capture._probe_binding(evidence_root, before) if before is not None else None,
        "gateway_after": capture._probe_binding(evidence_root, after) if after is not None else None,
        "recorded_at_utc": claude_upgrade._utc_now(),
    }
    path = environment_root / GATEWAY_RECOVERY_FILENAME
    claude_upgrade._secure_write_json_once(path, document)
    return path, status


def run_condition_digests(
    *,
    manifest: Mapping[str, Any],
    gateway: Mapping[str, Any],
    capture_before: Path | None,
    gateway_before: Path | None,
) -> dict[str, str]:
    """``candidate_gateway.run_conditions`` 的四项摘要（B4-02 写入请求的 ``run_conditions`` 去掉 ``run_nonce``；``run_nonce`` 取
    attempt 顶层，工具摘要由封存层取 Store 有效工具身份补上）。

    * ``environment_sha256``：attempt 级采集容器 before 探针清单与网关 before 快照两个文件摘要的规范摘要；
    * ``configuration_sha256``：生效清单 ``configuration`` 的规范摘要；
    * ``network_sha256``：网关 base URL、connect 中继端口、采集与服务容器、冻结上游 IP 与中继模式的规范摘要；
    * ``account_model_sha256``：专用账号 ID、ingress API Key ID 与主模型的规范摘要。
    """

    configuration = manifest["configuration"]
    return {
        "environment_sha256": claude_upgrade._fingerprint(
            {
                "capture_environment_before_sha256": file_sha256(capture_before) if capture_before is not None else None,
                "gateway_environment_before_sha256": file_sha256(gateway_before) if gateway_before is not None else None,
            }
        ),
        "configuration_sha256": claude_upgrade._fingerprint(dict(configuration)),
        "network_sha256": claude_upgrade._fingerprint(
            {
                "base_url": gateway.get("base_url"),
                "relay_port": gateway.get("relay_port"),
                "relay_mode": "connect",
                "capture_container": configuration.get("capture_container"),
                "service_container": gateway.get("service_container"),
                "upstream_ip": configuration.get("upstream_ip"),
            }
        ),
        "account_model_sha256": claude_upgrade._fingerprint(
            {"account_id": gateway.get("account_id"), "api_key_id": gateway.get("api_key_id"), "model": configuration.get("model")}
        ),
    }


def host_facts() -> dict[str, str | None]:
    """执行候选 run 的主机（门禁执行计划的缺省 ``host_id``／``architecture``）：主机名不是合法标识时记 null。"""

    hostname = socket.gethostname()
    machine = platform.machine().lower()
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(machine, machine)
    return {
        "host_id": hostname if claude_upgrade.SAFE_ID_RE.fullmatch(hostname) else None,
        "architecture": f"{platform.system().lower()}/{architecture}",
    }


def _file_entry(entry_id: str, evidence_root: Path, path: Path | None, *, passed: bool, **facts: Any) -> dict[str, Any] | None:
    if path is None:
        return None
    return {
        "id": entry_id,
        "facts": {"result": "pass" if passed else "fail", **capture._probe_binding(evidence_root, path), **facts},
    }


def candidate_gateway_facts(
    *,
    evidence_root: Path,
    gateway: Mapping[str, Any] | None,
    run_conditions: Mapping[str, str],
    capture_before: Path | None,
    capture_after: Path | None,
    capture_drifted: Sequence[str],
    gateway_before: Path | None,
    gateway_after: Path | None,
    gateway_drifted: Sequence[str],
    results: Sequence[Mapping[str, Any]],
    recovery: Path | None,
    recovery_status: str,
    selector: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """attempt 顶层 ``candidate_gateway``（与 B4-02 证据侧约定的字段名）：三组运行证据条目都是 ``{id, facts}``、按 id 排序，
    ``facts.result`` 为 ``pass`` 才算通过；文件绑定的 ``path`` 相对 ``<attempt>/evidence``。

    * ``environment_before``：``capture-containers``（采集容器 before 探针清单）、``gateway``（网关 before 快照）；
    * ``environment_after``：同名两条 after，另带漂移字段；
    * ``recovery_verification``：每个候选步骤一条 ``gateway-restoration-<job>-<probe>``（恢复报告的逻辑路径与摘要；与封存层
      按恢复报告生成的 ``restoration-<probe>`` 不同名，合并进 Store 的恢复证据时不撞 id），加 ``gateway-environment-equal``
      （前后快照逐字段相等）与 ``gateway-recovery``（恢复核验文件）；
    * ``selector_before_snapshot``：Store 的 ``runtime_catalog_snapshot`` 对象 payload（``runtime_catalog_snapshot`` 由
      selector 只读观察组装；没有 Store 的演练为 null）；观察原文另存 ``environment/selector-observed.json``。
    """

    def entries(*items: dict[str, Any] | None) -> list[dict[str, Any]]:
        return sorted((item for item in items if item is not None), key=lambda item: item["id"])

    restoration = [
        {
            "id": f"gateway-restoration-{result.get('id')}-{item.get('probe_id')}",
            "facts": {
                "result": "pass" if item.get("status") == "passed" else "fail",
                "status": item.get("status"),
                "report": item.get("report"),
                **({"failure_code": item["failure_code"]} if "failure_code" in item else {}),
            },
        }
        for result in results
        for item in result.get("restoration_checks") or []
    ]
    environment_equal = gateway_before is not None and gateway_after is not None and not gateway_drifted
    return {
        "base_url": gateway.get("base_url") if isinstance(gateway, Mapping) else None,
        "host": host_facts(),
        "run_conditions": dict(run_conditions),
        "environment_before": entries(
            _file_entry("capture-containers", evidence_root, capture_before, passed=True, schema_version=capture.PROBE_MANIFEST_SCHEMA),
            _file_entry("gateway", evidence_root, gateway_before, passed=True, schema_version=GATEWAY_ENVIRONMENT_SCHEMA),
        ),
        "environment_after": entries(
            _file_entry(
                "capture-containers", evidence_root, capture_after, passed=not capture_drifted,
                schema_version=capture.PROBE_MANIFEST_SCHEMA, drifted_kinds=list(capture_drifted),
            ),
            _file_entry(
                "gateway", evidence_root, gateway_after, passed=not gateway_drifted,
                schema_version=GATEWAY_ENVIRONMENT_SCHEMA, drifted_fields=list(gateway_drifted),
            ),
        ),
        "recovery_verification": entries(
            *restoration,
            {
                "id": "gateway-environment-equal",
                "facts": {
                    "result": "pass" if environment_equal else "fail",
                    "compared_fields": list(GATEWAY_COMPARED_FIELDS),
                    "drifted_fields": list(gateway_drifted),
                },
            },
            _file_entry("gateway-recovery", evidence_root, recovery, passed=recovery_status == "passed", schema_version=GATEWAY_RECOVERY_SCHEMA),
        ),
        "selector_before_snapshot": dict(selector) if selector is not None else None,
    }


CANDIDATE_GATEWAY_FIELDS = frozenset(
    {"base_url", "host", "run_conditions", "environment_before", "environment_after", "recovery_verification", "selector_before_snapshot"}
)


def verify_attempt_bindings(attempt_root: Path, payload: Mapping[str, Any]) -> None:
    """候选 attempt 收据里的候选文件绑定（网关前后快照、恢复核验、selector 观察）逐个存在且摘要一致，``candidate_gateway`` 闭合。"""

    evidence_root = attempt_root / "evidence"
    environment = payload.get("gateway_environment")
    recovery = payload.get("gateway_recovery")
    if not isinstance(environment, Mapping) or environment.get("schema_version") != GATEWAY_ENVIRONMENT_SCHEMA:
        raise ConfigurationError("候选 attempt 缺少网关环境快照绑定。")
    if not isinstance(recovery, Mapping) or recovery.get("schema_version") != GATEWAY_RECOVERY_SCHEMA:
        raise ConfigurationError("候选 attempt 缺少网关隔离恢复核验绑定。")
    gateway = payload.get("gateway")
    # 网关 before 快照之前就失败的 attempt 没有网关坐标；等待封存与演练通过的 attempt 必须有。
    if gateway is None and payload.get("status") in {"awaiting_receipts", "rehearsal_passed"}:
        raise ConfigurationError("候选 attempt 缺少网关坐标。")
    if gateway is not None and (not isinstance(gateway, Mapping) or not str(gateway.get("base_url", "")).startswith("http://")):
        raise ConfigurationError("候选 attempt 的网关坐标非法。")
    facts = payload.get("candidate_gateway")
    if not isinstance(facts, Mapping) or set(facts) != CANDIDATE_GATEWAY_FIELDS:
        raise ConfigurationError("候选 attempt 的 candidate_gateway 字段不闭合。")
    if not isinstance(facts["run_conditions"], Mapping) or set(facts["run_conditions"]) != set(RUN_CONDITION_DIGEST_FIELDS):
        raise ConfigurationError("候选 attempt 的运行条件摘要字段不闭合。")
    if facts["base_url"] != (gateway.get("base_url") if isinstance(gateway, Mapping) else None):
        raise ConfigurationError("候选 attempt 的 candidate_gateway.base_url 与网关坐标不一致。")
    bindings = [
        environment.get("before"),
        environment.get("after"),
        environment.get("activation_fact"),
        recovery.get("file"),
        payload.get("selector_observed"),
    ]
    if payload.get("status") == "awaiting_receipts" and environment.get("activation_fact") is None:
        raise ConfigurationError("等待封存的候选 attempt 缺少网关 Claude 激活事实副本。")
    for binding in bindings:
        if binding is None:
            continue
        if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
            raise ConfigurationError("候选 attempt 的文件绑定形态非法。")
        target = evidence_root / str(binding["path"])
        claude_upgrade._reject_symlink_components(target, evidence_root, "候选 attempt 文件")
        if not target.is_file() or file_sha256(target) != binding.get("sha256"):
            raise ConfigurationError(f"候选 attempt 文件 {binding['path']} 缺失或摘要漂移。")


# ---------------------------------------------------------------------------
# 候选 run 的环节对象（c1：官方 run、候选 run、候选续跑与恢复段共用）
# ---------------------------------------------------------------------------


@dataclass
class CandidateRunHooks:
    """一次候选 attempt（首个 run、续跑或恢复段）里候选专属的全部环节，按调用顺序：

    1. ``admit``：入口门（正式核 VC-4／Store／构建收据／服务镜像／覆盖收据／未封存，演练只核变量与覆盖），返回生效清单；
    2. ``identity``：attempt 身份（预约收据的 ``identity_sha256`` 绑定它）；
    3. ``pre_reservation``：管理凭据、网关只读就绪、生产 selector 只读观察（都在预约之前，零写入）；
    4. ``before_jobs``：网关 before 快照落盘、就绪复核、selector 观察落盘（attempt before 探针之后、任何 Job 之前）；
    5. ``after_jobs``：网关 after 快照与逐字段比较（finally 里调用，deadline 到期也采）；
    6. ``attempt_fields``：网关隔离恢复核验落盘并给出 attempt 的候选字段（权限收口之前调用）。

    调用方（``run_capture_attempt``、``claude_upgrade_recovery._run_recovery_attempt``，以及路线二的候选恢复段）按同一顺序
    调用即得到同一份 attempt 合同（见文件头第二节）。
    """

    campaign_dir: Path
    candidate_id: str
    mode: str
    admission: CandidateAdmission | None = None
    selector_observation: dict[str, Any] | None = None
    gateway: dict[str, Any] | None = None
    gateway_before: dict[str, Any] | None = None
    gateway_before_path: Path | None = None
    gateway_after_path: Path | None = None
    gateway_drifted: list[str] = field(default_factory=list)
    selector_path: Path | None = None
    activation_fact: dict[str, Any] | None = None
    recovery_revision: str | None = None

    def admit(self, manifest: dict[str, Any], *, deadline: Any | None) -> dict[str, Any]:
        admit = formal_admission if self.mode == capture.MODE_FORMAL else rehearsal_admission
        self.admission = admit(self.campaign_dir, manifest, self.candidate_id, deadline=deadline,
                               **({"recovery_revision": self.recovery_revision} if self.recovery_revision is not None else {}))
        return self.admission.manifest

    def _require_admitted(self) -> CandidateAdmission:
        if self.admission is None:
            raise ConfigurationError("候选环节必须先过入口门（admit）。")
        return self.admission

    def identity(self, manifest: Mapping[str, Any], layout: Any) -> dict[str, Any]:
        return attempt_identity(manifest, layout, self.mode, self._require_admitted())

    def pre_reservation(self, manifest: Mapping[str, Any], layout: Any, jobs: Sequence[Any], *, deadline: Any | None) -> None:
        self._require_admitted()
        # 账号刷新触发的步骤（演练时宿主脚本的只读前检同样核管理凭据）：预约前确认环境里给出了管理凭据。
        require_admin_token_for(jobs)
        # 网关只读就绪核对（不落盘）：残留临时代理、临时 CA、运行目录或账号形态不对时在预约前拒绝，不留失败 attempt。
        readiness = probe_gateway_environment(manifest, layout.host_data_root, "readiness", deadline=deadline)
        not_ready = readiness_failures(readiness)
        if not_ready:
            raise ConfigurationError(f"候选网关环境未就绪：{not_ready}（先清理上一轮残留或修正专用账号与 API Key）。")
        # 生产 selector 只读观察：正式 run 必须有 Campaign 冻结的 Active Release 目录（预约前读一次，预约后原样落盘）。
        self.selector_observation = observe_selectors(self.campaign_dir, manifest, required=self.mode == capture.MODE_FORMAL)

    def before_jobs(
        self,
        manifest: Mapping[str, Any],
        layout: Any,
        environment_root: Path,
        observations: list[dict[str, str]],
        *,
        deadline: Any | None,
        heartbeat: Any | None,
    ) -> None:
        admission = self._require_admitted()
        if deadline is not None:
            deadline.check("attempt:gateway-before")
        self.gateway_before = probe_gateway_environment(manifest, layout.host_data_root, "before", deadline=deadline, heartbeat=heartbeat)
        self.gateway_before_path = write_gateway_snapshot(environment_root, self.gateway_before)
        self.gateway = gateway_coordinates(admission.variables, service_port=self.gateway_before["service"]["port"])
        not_ready = readiness_failures(self.gateway_before)
        if not_ready:
            observations.extend({"check_id": f"attempt.gateway.{code}", "failure_code": "not-ready"} for code in not_ready)
            raise ConfigurationError(f"候选网关环境未就绪：{not_ready}，拒绝执行任何 Job。")
        if self.selector_observation is not None:
            self.selector_path = environment_root / SELECTOR_FILENAME
            claude_upgrade._secure_write_json_once(self.selector_path, self.selector_observation)

    def after_jobs(
        self, manifest: Mapping[str, Any], layout: Any, environment_root: Path, observations: list[dict[str, str]]
    ) -> tuple[bool, BaseException | None]:
        """返回 ``(是否污染, 采集错误)``。before 快照都没采到（没有执行任何 Job）时不采、不判。"""

        if self.gateway_before is None:
            return False, None
        # deadline 到期也要采（每条只读命令各有 30 秒上限）：生产形态的网关状态不能因为预算用完就不核验。
        try:
            gateway_after = probe_gateway_environment(manifest, layout.host_data_root, "after")
            self.gateway_after_path = write_gateway_snapshot(environment_root, gateway_after)
            self.gateway_drifted = gateway_drift(self.gateway_before, gateway_after)
        except BaseException as error:
            observations.append({"check_id": "attempt.gateway.after-probe", "failure_code": "probe-failed"})
            return True, error
        # Claude 激活事实只读复制：缺失、不可读或不是候选 Runtime 一律失败关闭。正式 run 按环境污染处理（网关没有证明自己
        # 装配的是候选 Runtime）；零请求演练没有动网关，只判演练失败。
        try:
            self.activation_fact = copy_activation_fact(manifest, environment_root, evidence_root=environment_root.parent)
        except BaseException as error:
            failure = "invalid" if isinstance(error, ActivationFactInvalid) else "missing"
            observations.append({"check_id": "attempt.gateway.activation-fact", "failure_code": failure})
            return self.mode == capture.MODE_FORMAL, error
        if self.gateway_drifted:
            observations.extend({"check_id": f"attempt.gateway.{name}", "failure_code": "drifted"} for name in self.gateway_drifted)
            return True, None
        return False, None

    def attempt_fields(
        self,
        manifest: Mapping[str, Any],
        *,
        evidence_root: Path,
        environment_root: Path,
        results: Sequence[Mapping[str, Any]],
        capture_before: Path | None,
        capture_after: Path | None,
        capture_drifted: Sequence[str],
    ) -> tuple[dict[str, Any], BaseException | None]:
        """网关隔离恢复核验落盘并返回 ``(attempt 候选字段, 落盘错误)``；结果须是归档改写之后的终值。"""

        admission = self._require_admitted()
        recovery: dict[str, Any] = {"schema_version": GATEWAY_RECOVERY_SCHEMA, "status": "failed", "file": None}
        recovery_path: Path | None = None
        recovery_status = "failed"
        error: BaseException | None = None
        try:
            recovery_path, recovery_status = write_recovery(
                environment_root,
                results,
                before=self.gateway_before_path,
                after=self.gateway_after_path,
                drifted=self.gateway_drifted,
                evidence_root=evidence_root,
            )
            recovery = {"schema_version": GATEWAY_RECOVERY_SCHEMA, "status": recovery_status, "file": capture._probe_binding(evidence_root, recovery_path)}
        except BaseException as caught:
            error = caught
        selector = (
            runtime_catalog_snapshot(self.selector_observation, admission.persona)
            if self.selector_path is not None and self.selector_observation is not None and admission.persona is not None
            else None
        )
        fields = {
            "gateway": self.gateway,
            "gateway_environment": {
                "schema_version": GATEWAY_ENVIRONMENT_SCHEMA,
                "before": capture._probe_binding(evidence_root, self.gateway_before_path) if self.gateway_before_path is not None else None,
                "after": capture._probe_binding(evidence_root, self.gateway_after_path) if self.gateway_after_path is not None else None,
                "compared_fields": list(GATEWAY_COMPARED_FIELDS),
                "drifted_fields": list(self.gateway_drifted),
                # Claude 激活事实的只读副本（路径相对 <attempt>/evidence；没有复制成功时为 null，attempt 不会是 awaiting_receipts）。
                "activation_fact": dict(self.activation_fact) if self.activation_fact is not None else None,
            },
            "gateway_recovery": recovery,
            "selector_observed": (
                {"schema_version": SELECTOR_OBSERVATION_SCHEMA, **capture._probe_binding(evidence_root, self.selector_path)}
                if self.selector_path is not None
                else None
            ),
            # 与 B4-02 证据侧约定的字段（见 candidate_gateway_facts）。
            "candidate_gateway": candidate_gateway_facts(
                evidence_root=evidence_root,
                gateway=self.gateway,
                run_conditions=run_condition_digests(
                    manifest=manifest, gateway=self.gateway or {}, capture_before=capture_before, gateway_before=self.gateway_before_path
                ),
                capture_before=capture_before,
                capture_after=capture_after,
                capture_drifted=capture_drifted,
                gateway_before=self.gateway_before_path,
                gateway_after=self.gateway_after_path,
                gateway_drifted=self.gateway_drifted,
                results=results,
                recovery=recovery_path,
                recovery_status=recovery_status,
                selector=selector,
            ),
        }
        return fields, error

    def contamination_marker_fields(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "gateway_drifted_fields": list(self.gateway_drifted)}

    def verify_continuity(self, source_attempt_root: Path) -> dict[str, Any]:
        """续跑承接时的网关连续性：来源 attempt 的网关 after 快照与本轮 before 快照逐字段相等（``GATEWAY_COMPARED_FIELDS``）。

        返回写进 ``recovery.continuity.gateway`` 的记录；不成立时抛 ``GatewayContinuityDrift``（类名与官方连续性漂移同一
        判据由调用方换算）。
        """

        if self.gateway_before is None:
            raise ConfigurationError("网关连续性核对必须在 before 快照之后。")
        source = Path(source_attempt_root) / "evidence" / "environment" / GATEWAY_AFTER_FILENAME
        if source.is_symlink() or not source.is_file():
            raise GatewayContinuityDrift("被承接的候选 attempt 缺少网关 after 快照，无法证明网关连续性。")
        drifted = gateway_drift(claude_upgrade._read_json(source, "被承接 attempt 的网关 after 快照"), self.gateway_before)
        if drifted:
            raise GatewayContinuityDrift(f"上一轮结束后网关环境已漂移（{'、'.join(drifted)}）")
        return {"source_after_sha256": file_sha256(source), "compared_fields": list(GATEWAY_COMPARED_FIELDS)}


class GatewayContinuityDrift(ConfigurationError):
    """被承接候选 attempt 的网关收尾快照与本轮起始快照不一致（调用方换算成官方同一个连续性漂移错误类型）。"""


# ---------------------------------------------------------------------------
# 命令行（claude_upgrade.py capture-candidate run）
# ---------------------------------------------------------------------------


def add_run_arguments(target: argparse.ArgumentParser) -> None:
    """``capture-candidate run`` 的参数（``--candidate-id`` 由编排器统一登记；seal 的参数在 ``claude_upgrade_candidate_seal``）。"""

    target.add_argument("--acknowledge-live-requests", action="store_true", help="候选取证会经候选网关发真实请求，必须显式确认。")
    target.add_argument(
        "--zero-request-rehearsal",
        action="store_true",
        help="零请求演练：只用于 preflight_only Campaign，宿主脚本只做只读前检、relay 改为 --dry-run，不发任何请求。",
    )
    target.add_argument("--job-id", help="零请求演练要执行的单个 candidate Job。")
    target.add_argument("--attempt-recovery", help="当前 transient 裁定授权的恢复段 ar<k>（run 与 seal 共用）。")
    target.add_argument("--rerun-failed", action="store_true", help="恢复段失败后，按已批准预览开连续后继段。")
    target.add_argument("--recovery-preview", type=Path, help="直接前段经 reconcile-attempt 批准的恢复预览。")
    target.add_argument(
        "--max-wall-seconds",
        type=int,
        default=None,
        help=f"一次受管命令的单调墙钟预算（默认 {claude_upgrade.DEFAULT_ATTEMPT_WALL_SECONDS} 秒）。",
    )
    target.add_argument("--heartbeat-seconds", type=int, default=None, help="watchdog heartbeat 间隔（秒）。")
