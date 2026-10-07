#!/usr/bin/env python3
"""Claude 候选 attempt 级边界核对（Claude 升级控制面设计 B4-01 f 段）。

场景清单登记两项 ``required_boundary_checks``（``claude_upgrade_plan.REQUIRED_BOUNDARY_CHECKS``），在候选 run 之后、seal 之前
对一个等待封存的候选 attempt 执行，通过后在 ``<attempt 根>/client-evidence/<check_id>.json`` 只写一次收据，seal 经
``verify_boundary_receipt`` 核对收据（第四节）后才把它交给证据侧的闭合门禁。

一、入口与时机
==============
``claude_upgrade.py capture-candidate boundary-check --campaign-dir … --candidate-id … --attempt-id … --check <check_id>``

* 只在 Formal Campaign 上、只对状态为 ``awaiting_receipts``、没有失败 Job、没有被隔离或冲突隔离作废、尚未封存的候选
  attempt 执行；候选正式入口门（``claude_upgrade_candidate_capture.formal_admission``：VC-4、Store、构建收据、服务镜像、
  覆盖收据、未封存）原样重放一遍，attempt 身份（源码树、服务镜像）重验，且 attempt 身份里的候选事实必须与入口门给出的
  当前候选事实逐项相等（同一 revision、同一构建）。
* 独立入口而不是 attempt 内的步骤：某一项失败只重跑这一项，不作废已经采完的候选 attempt；真实请求单独计数、单独入账。
* 收据已存在时先核对：合法即报 ``already_passed``（退出 0，零写入）；不合法失败关闭、要求人工审计（不自动删除或覆盖）。
* 在 Campaign 租约下执行（与 ``capture-candidate run`` 同一 phase／candidate 租约），步骤经共享监督器执行，操作名
  ``job:boundary-<check_id>:step-1:attempt-1``（出口守护下照常建立采集停止绑定）。

二、每次执行的落盘
==================
``<attempt 根>/client-evidence/<check_id>/runs/<UTC 时刻>-<8 位随机>/``（0700）：

* ``gateway-before.json``／``gateway-after.json``：网关只读快照（与候选 run 同一 ``claude-upgrade-candidate-gateway-environment/v1``，
  按 ``GATEWAY_COMPARED_FIELDS`` 逐字段比较）；执行前另做一次只读就绪核对（不落盘），残留在场即拒绝；
* 检查专属的证据（第三节）与步骤日志 ``logs/step-1.log``；
* ``verdict.json``（``claude-upgrade-boundary-check-run/v1``）：无论成败都写，记结论、失败码、环境污染、证据绑定与真实
  请求计数。失败不写收据，只留运行目录，修好后可以再跑。

网关隔离恢复失败（宿主脚本退出 97、恢复报告缺失或未核验）或前后快照漂移按环境污染处理：照候选 run 的口径写 Campaign
污染标记 ``environment-contaminated.json``（``phase=candidate``、``candidate_id``、``boundary_check``；不带 ``attempt_id``——
被核对的 attempt 的证据早于这次执行，不因此作废），随后走 environment-cleanup／environment-isolate。

三、两项核对
============
* ``third-party-ingress-rejection``：经宿主候选采集脚本（``GATEWAY_TRIGGER=ingress-negative``，与候选 run 同一网关隔离）
  调用中继的 ``ingress-negative`` 子命令：connect 端口只记录并拒绝连接、从不转发上游，向候选网关发固定的八个第三方与
  非法形态负例（``claude_fw_e_relay.INGRESS_NEGATIVE_CASES``）。通过条件：宿主脚本退出 0、恢复报告逐项核验通过且账号令牌
  前后摘要相等、中继清单完整（宿主运行凭据绑定一致、负例闭集齐全、每个负例的状态码在期望集合里、监听期间零连接、秘密
  扫描通过）、网关前后快照相等。真实上游请求恒为 0（中继从不转发），收据记 ``live_requests.count = 0``。
* ``codex-final-wire-isolation``：见第五节（structural＋runtime 两节）。

四、收据（``claude-upgrade-boundary-check/v1``）与校验入口
==========================================================
字段闭集 ``RECEIPT_FIELDS``：``check_id``、``campaign_id``、``candidate_id``、``attempt_id``、``run_nonce``（取 attempt 的）、
``result``（恒为 ``pass``）与证据侧闭合门禁逐字比较的这几项同名；另有 ``conclusion_scope``（结论范围的固定文字）、
``candidate``（候选 revision、构建 ID、镜像 ID、构建收据摘要、源码树摘要与提交，取自 attempt 身份）、``boundary_run``
（本次执行的 run-id、nonce、目录与起止时刻）、``evidence``（证据绑定，每条 ``{role, base, path, sha256, bytes}``，
``base`` 为 ``attempt``（相对 attempt 根）或 ``data_root``（相对宿主数据根）；目录型证据用 ``tree_sha256`` 代替
``sha256``／``bytes``）、``live_requests``（``count``、``exact``、``counting_rule``、``by_endpoint``）、``facts``（检查专属
摘要）与 ``issued_at_utc``。

``verify_boundary_receipt(attempt_root, check_id, path)`` 是 seal（``claude_upgrade_candidate_seal._client_evidence``）与
本入口共用的校验入口（e 段 Mac 客户端收据以后也走同一入口）：schema 与字段闭集、文件名与 ``check_id``、与 attempt 收据的
四项身份一致、``result=pass``、结论范围文字、候选事实等于 attempt 身份、运行目录在 attempt 的客户端证据目录内、证据角色
闭集齐全且每个文件（目录）存在且摘要一致、``verdict.json`` 的结论为 pass 且身份一致、真实请求计数合法、签发时刻与执行
起点都不早于 attempt 完成时刻。任一不成立即抛 ``ConfigurationError``（失败关闭）。

五、codex-final-wire-isolation：structural＋runtime（同一份收据）
=================================================================
结论范围固定为「构建输入零差异＋mitm 观测维度零差异」，不是 Codex 验收口径的全部规则通过（Codex checker 要求场景证据
齐全，只跑 candidate-core-mitm 时没有一条规则能完整判定；整套 10 个 Job 约 7.8 小时，不在边界核对的范围里）。

* **structural（必过、零请求，先做）**：候选源码树（构建输入，入口门已按构建收据复算树摘要）与 ``--production-commit``
  给出的生产部署提交（必须在候选源码仓里）在三组路径上逐字节比较（工作树文件按 git blob 算法取对象 ID，与生产提交的
  blob 比较），任何新增、删除、修改都失败并逐条列出：``codex_release_catalog``（Codex 运行目录、它指向的 Release graph
  与快照目录、active 节点的画像文件）、``codex_runtime_projection``（``officialegress/catalogdata`` 下除 ``claude/`` 外的
  全部目录数据）、``codex_egress_go``（officialegress 包含子包的非 Claude、非测试文件，加包外 Codex 出站定型面与人工复核
  接缝，界定同出站门禁 ``tools/check_ledger_completeness.py``，常量见 ``CODEX_*``）。structural 不过即不发任何真实请求。
* **runtime（candidate-core-mitm，真实请求）**：Codex 版本取候选源码树 Codex 运行目录的 active 选择（唯一版本与快照），
  步骤照 Codex 该版本冻结场景清单的 ``candidate-core-mitm`` 展开（脚本一个字节不改），只代入本次的坐标：Campaign ID 换成
  本次 run-id（每次新前缀，不复用旧坐标）、数据根取宿主与容器同一路径的别名、容器名、``--codex-account-id``／
  ``--codex-api-key-id``／``--codex-model`` 三个轮次参数、``/opt/codex-<版本>/bin/codex``；另给 ``CAPTURE_TOOL_ROOT``
  指向本 Campaign 的工具执行副本。只读前检核 Codex 运行画像副本与 active 画像逐字节相同、指纹转发器可执行、Codex 账号
  是未绑定代理的 OpenAI OAuth 账号且 API Key 分组只含它、没有残留临时代理；账号事实在执行前后逐项相等。证据按冻结的
  ``evidence_roots`` 通配收集，真实请求用 Codex 自己的 mitm 逐请求计数（``codex_upgrade_live_request_provenance``，
  失败归档根也计）精确入账（收据 ``live_requests``，与 Claude 的请求分开记）。判定走 Codex 的断言管线（编目→收口→
  派生→回填 manifest→重放）与逐规则 checker（``candidate_rule_assertion.py``，以命令行调用、不导入；工具文件摘要记进
  运行报告），每条检查分类：所需场景全在本次证据里且选中了观测的「已判」必须全部通过；场景覆盖检查与其余检查记「未
  覆盖」、不判，逐条列在 ``codex/runtime-report.json``；已判为零也算失败。

本文件在 Claude 身份策略里按 ``claude_upgrade_`` 前缀归 control 层，不进 wire 闭包。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_plan

# ---------------------------------------------------------------------------
# 合同常量
# ---------------------------------------------------------------------------

RECEIPT_SCHEMA = "claude-upgrade-boundary-check/v1"
RUN_VERDICT_SCHEMA = "claude-upgrade-boundary-check-run/v1"
ACTION = "capture-candidate boundary-check"
CLIENT_EVIDENCE_DIRNAME = "client-evidence"
RUNS_DIRNAME = "runs"
VERDICT_FILENAME = "verdict.json"
STEP_LOG_RELATIVE = PurePosixPath("logs") / "step-1.log"
CHECK_THIRD_PARTY = "third-party-ingress-rejection"
CHECK_CODEX = "codex-final-wire-isolation"
CHECK_IDS = tuple(sorted(claude_upgrade_plan.REQUIRED_BOUNDARY_CHECKS))
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "check_id",
        "campaign_id",
        "candidate_id",
        "attempt_id",
        "run_nonce",
        "result",
        "conclusion_scope",
        "candidate",
        "boundary_run",
        "evidence",
        "live_requests",
        "facts",
        "issued_at_utc",
    }
)
# 收据 ``candidate`` 取自 attempt 身份的字段（与入口门给出的当前候选事实逐项相等）。
CANDIDATE_FACT_FIELDS = ("candidate_revision", "build_id", "image_id", "source_tree_sha256", "git_commit")
BOUNDARY_RUN_FIELDS = frozenset({"run_id", "run_nonce", "path", "started_at_utc", "ended_at_utc"})
LIVE_REQUEST_FIELDS = frozenset({"count", "exact", "counting_rule", "by_endpoint"})
EVIDENCE_BASES = frozenset({"attempt", "data_root"})
# 每项核对的结论范围（收据逐字记录，校验入口逐字比较）：只写这项核对实际证明了什么。
CONCLUSION_SCOPES = {
    CHECK_THIRD_PARTY: "第三方与非法形态请求在读取 OAuth 凭据前被候选网关拒绝，监听期间网关经候选账号代理零出站（零上游请求）",
    CHECK_CODEX: "构建输入零差异＋mitm 观测维度零差异（不是 Codex 验收口径的全部规则通过）",
}
# 每项核对必须绑定的证据角色（闭集）；带前缀的角色可重复（``<前缀><标识>``），每个登记的前缀至少出现一次。
EVIDENCE_ROLES = {
    CHECK_THIRD_PARTY: frozenset(
        {"verdict", "gateway_before", "gateway_after", "host_runtime_receipt", "step_log", "relay_manifest", "restoration_report"}
    ),
    CHECK_CODEX: frozenset(
        {"verdict", "gateway_before", "gateway_after", "structural_report", "step_log", "capture_manifest", "runtime_report"}
    ),
}
EVIDENCE_ROLE_PREFIXES = {
    CHECK_THIRD_PARTY: frozenset(),
    CHECK_CODEX: frozenset({"codex_evidence_root:", "rule_assertion:"}),
}
# 第三方负例：中继子命令、固定 probe 与清单文件名（与 claude_fw_e_relay 同名常量一致，由测试锁定）。
INGRESS_NEGATIVE_ACTION = "ingress-negative"
INGRESS_NEGATIVE_PROBE_ID = "ingress-negative"
INGRESS_NEGATIVE_TRIGGER = "ingress-negative"
INGRESS_NEGATIVE_MANIFEST = "ingress-negative-manifest.json"
INGRESS_NEGATIVE_SCHEMA = "claude-code-fw-e-ingress-negative/v1"
INGRESS_NEGATIVE_CASE_IDS = (
    "messages-third-party-user-agent",
    "messages-official-user-agent-without-official-headers",
    "messages-query-outside-envelope",
    "count-tokens-third-party-user-agent",
    "openai-chat-completions",
    "openai-responses",
    "messages-missing-api-key",
    "messages-oauth-token-shaped-api-key",
)
THIRD_PARTY_STEP_TIMEOUT_SECONDS = 1500
THIRD_PARTY_CASE_TIMEOUT_SECONDS = 60
# 宿主脚本在任何修改之前的只读前检拒绝（或参数不合同）：没有改动可恢复，不是环境污染。
PRECHECK_EXIT_CODES = candidate_capture.PRECHECK_EXIT_CODES
RESTORATION_FAILED_EXIT_CODE = candidate_capture.RESTORATION_FAILED_EXIT_CODE


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not claude_upgrade.SAFE_ID_RE.fullmatch(value):
        raise ConfigurationError(f"{ACTION} 必须用 {label} 指定合法的标识。")
    return value


def _parse_utc(value: Any, label: str) -> dt.datetime:
    """编排器写的 UTC RFC 3339 时刻（``…Z``，秒或微秒精度）。"""

    if not isinstance(value, str) or not value.endswith("Z") or not claude_upgrade._is_rfc3339_timestamp(value):
        raise ConfigurationError(f"{label} 不是以 Z 结尾的 UTC RFC 3339 时刻。")
    try:
        return dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"{label} 不是以 Z 结尾的 UTC RFC 3339 时刻。") from error


def _binding(role: str, base: str, root: Path, path: Path) -> dict[str, Any]:
    return {"role": role, "base": base, **capture._probe_binding(root, path)}


def receipt_path(attempt_root: Path, check_id: str) -> Path:
    return Path(attempt_root) / CLIENT_EVIDENCE_DIRNAME / f"{check_id}.json"


def _campaign_dir_of(attempt_root: Path) -> Path:
    """``<Campaign>/candidates/<cid>/attempts/<aid>`` → Campaign 目录。"""

    attempt_root = Path(attempt_root)
    if attempt_root.parent.name != "attempts" or attempt_root.parents[2].name != "candidates":
        raise ConfigurationError(f"不是候选 attempt 根：{attempt_root}")
    return attempt_root.parents[3]


# ---------------------------------------------------------------------------
# 收据校验入口（seal 与本入口共用；见文件头第四节）
# ---------------------------------------------------------------------------


def _verify_evidence_entry(entry: Any, *, attempt_root: Path, data_root: Path) -> None:
    if not isinstance(entry, Mapping) or entry.get("base") not in EVIDENCE_BASES or not isinstance(entry.get("path"), str):
        raise ConfigurationError("边界核对收据的证据绑定形态非法。")
    base = attempt_root if entry["base"] == "attempt" else data_root
    relative = PurePosixPath(str(entry["path"]))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ConfigurationError(f"边界核对收据的证据路径不是规范相对路径：{entry['path']}")
    target = base.joinpath(*relative.parts)
    claude_upgrade._reject_symlink_components(target, base, "边界核对证据")
    if "tree_sha256" in entry:
        if set(entry) != {"role", "base", "path", "tree_sha256"}:
            raise ConfigurationError("边界核对收据的目录型证据字段不闭合。")
        if not target.is_dir() or claude_upgrade._directory_tree_digest(target) != entry["tree_sha256"]:
            raise ConfigurationError(f"边界核对证据目录 {entry['path']} 缺失或摘要漂移。")
        return
    if set(entry) != {"role", "base", "path", "sha256", "bytes"}:
        raise ConfigurationError("边界核对收据的文件型证据字段不闭合。")
    if not target.is_file() or file_sha256(target) != entry["sha256"] or target.stat().st_size != entry["bytes"]:
        raise ConfigurationError(f"边界核对证据文件 {entry['path']} 缺失或摘要漂移。")


def verify_boundary_receipt(attempt_root: Path, check_id: str, path: Path | None = None) -> dict[str, Any]:
    """核对一份 attempt 级边界核对收据（文件头第四节）；返回收据内容，任一项不成立即抛 ``ConfigurationError``。"""

    attempt_root = Path(attempt_root)
    if check_id not in CHECK_IDS:
        raise ConfigurationError(f"未登记的边界核对：{check_id}")
    expected_path = receipt_path(attempt_root, check_id)
    path = Path(path) if path is not None else expected_path
    if path.resolve(strict=False) != expected_path.resolve(strict=False):
        raise ConfigurationError(f"边界核对收据必须是 {CLIENT_EVIDENCE_DIRNAME}/{check_id}.json：{path}")
    claude_upgrade._reject_symlink_components(path, attempt_root, "边界核对收据")
    if not path.is_file():
        raise ConfigurationError(f"边界核对收据不存在：{path}")
    receipt = claude_upgrade._read_json(path, "边界核对收据")
    if receipt.get("schema_version") != RECEIPT_SCHEMA or set(receipt) != RECEIPT_FIELDS:
        raise ConfigurationError(f"边界核对收据 {check_id} 的 schema 或字段闭集不符。")
    campaign_dir = _campaign_dir_of(attempt_root)
    attempt = claude_upgrade._read_json(attempt_root / "attempt.json", "候选 attempt 收据")
    identity = attempt.get("identity") if isinstance(attempt.get("identity"), Mapping) else {}
    # candidate_id 与 attempt_id 取 attempt 根的坐标（调用方按坐标装载并重验过 attempt），campaign_id 与 run_nonce 取 attempt 收据。
    wanted = {
        "check_id": check_id,
        "campaign_id": attempt.get("campaign_id"),
        "candidate_id": attempt_root.parents[1].name,
        "attempt_id": attempt_root.name,
        "run_nonce": attempt.get("run_nonce"),
        "result": "pass",
        "conclusion_scope": CONCLUSION_SCOPES[check_id],
    }
    drift = sorted(name for name, value in wanted.items() if receipt.get(name) != value)
    if drift:
        raise ConfigurationError(f"边界核对收据 {check_id} 与 attempt 或合同不一致：{drift}")
    if receipt["candidate"] != {name: identity.get(name) for name in CANDIDATE_FACT_FIELDS}:
        raise ConfigurationError(f"边界核对收据 {check_id} 的候选事实与 attempt 身份不一致。")
    completed = _parse_utc(attempt.get("completed_at_utc"), "attempt 完成时刻")
    run = receipt["boundary_run"]
    if not isinstance(run, Mapping) or set(run) != BOUNDARY_RUN_FIELDS:
        raise ConfigurationError(f"边界核对收据 {check_id} 的 boundary_run 字段不闭合。")
    run_path = PurePosixPath(str(run["path"]))
    if run_path.parts[:3] != (CLIENT_EVIDENCE_DIRNAME, check_id, RUNS_DIRNAME) or len(run_path.parts) != 4:
        raise ConfigurationError(f"边界核对收据 {check_id} 的运行目录不在 {CLIENT_EVIDENCE_DIRNAME}/{check_id}/{RUNS_DIRNAME} 下。")
    run_root = attempt_root.joinpath(*run_path.parts)
    claude_upgrade._reject_symlink_components(run_root, attempt_root, "边界核对运行目录")
    if not run_root.is_dir():
        raise ConfigurationError(f"边界核对运行目录不存在：{run['path']}")
    started = _parse_utc(run["started_at_utc"], "边界核对执行起点")
    ended = _parse_utc(run["ended_at_utc"], "边界核对执行终点")
    issued = _parse_utc(receipt["issued_at_utc"], "边界核对收据签发时刻")
    if not completed <= started <= ended <= issued:
        raise ConfigurationError(f"边界核对收据 {check_id} 的执行与签发时刻早于 attempt 完成，或先后颠倒。")
    if not isinstance(run["run_id"], str) or not claude_upgrade.SAFE_ID_RE.fullmatch(run["run_id"]):
        raise ConfigurationError(f"边界核对收据 {check_id} 的 boundary_run.run_id 非法。")
    if not isinstance(run["run_nonce"], str) or not capture.SHA256_RE.fullmatch(run["run_nonce"]):
        raise ConfigurationError(f"边界核对收据 {check_id} 的 boundary_run.run_nonce 非法。")
    evidence = receipt["evidence"]
    if not isinstance(evidence, list):
        raise ConfigurationError(f"边界核对收据 {check_id} 的 evidence 不是列表。")
    roles = [entry.get("role") if isinstance(entry, Mapping) else None for entry in evidence]
    if not all(isinstance(role, str) and role for role in roles) or len(roles) != len(set(roles)):
        raise ConfigurationError(f"边界核对收据 {check_id} 的证据角色缺失或重复。")
    prefixes = EVIDENCE_ROLE_PREFIXES[check_id]
    fixed = {role for role in roles if ":" not in role}
    prefixed = [role for role in roles if ":" in role]
    if (
        fixed != EVIDENCE_ROLES[check_id]
        or any(not any(role.startswith(prefix) and len(role) > len(prefix) for prefix in prefixes) for role in prefixed)
        or any(not any(role.startswith(prefix) for role in prefixed) for prefix in prefixes)
    ):
        raise ConfigurationError(f"边界核对收据 {check_id} 的证据角色不是登记的闭集。")
    data_root = capture._host_data_root(campaign_dir)
    for entry in evidence:
        _verify_evidence_entry(entry, attempt_root=attempt_root, data_root=data_root)
    verdict_entry = next(entry for entry in evidence if entry["role"] == "verdict")
    if verdict_entry["base"] != "attempt" or PurePosixPath(verdict_entry["path"]) != run_path / VERDICT_FILENAME:
        raise ConfigurationError(f"边界核对收据 {check_id} 的结论文件不是本次运行目录里的 {VERDICT_FILENAME}。")
    verdict = claude_upgrade._read_json(run_root / VERDICT_FILENAME, "边界核对运行结论")
    if (
        verdict.get("schema_version") != RUN_VERDICT_SCHEMA
        or verdict.get("result") != "pass"
        or verdict.get("check_id") != check_id
        or verdict.get("attempt_id") != wanted["attempt_id"]
        or verdict.get("boundary_run_id") != run["run_id"]
        or verdict.get("boundary_run_nonce") != run["run_nonce"]
    ):
        raise ConfigurationError(f"边界核对收据 {check_id} 绑定的运行结论不是 pass 或身份不符。")
    live = receipt["live_requests"]
    if (
        not isinstance(live, Mapping)
        or set(live) != LIVE_REQUEST_FIELDS
        or type(live["count"]) is not int
        or live["count"] < 0
        or live["exact"] is not True
        or not isinstance(live["counting_rule"], str)
        or not isinstance(live["by_endpoint"], Mapping)
        or sum(int(value) for value in live["by_endpoint"].values()) != live["count"]
    ):
        raise ConfigurationError(f"边界核对收据 {check_id} 的真实请求计数非法（必须精确计数且分端点之和等于总数）。")
    if check_id == CHECK_THIRD_PARTY and live["count"] != 0:
        raise ConfigurationError("third-party-ingress-rejection 的真实上游请求必须为 0。")
    if live != verdict.get("live_requests"):
        raise ConfigurationError(f"边界核对收据 {check_id} 的真实请求计数与运行结论不一致。")
    if not isinstance(receipt["facts"], Mapping):
        raise ConfigurationError(f"边界核对收据 {check_id} 的 facts 不是对象。")
    return receipt


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


@dataclass
class BoundaryRun:
    """一次边界核对执行的上下文（检查专属的执行与判定都只经它取坐标）。"""

    check_id: str
    campaign_dir: Path
    manifest: dict[str, Any]
    admission: Any
    attempt_root: Path
    attempt: dict[str, Any]
    layout: Any
    run_root: Path
    run_id: str
    run_nonce: str
    started_at_utc: str
    arguments: argparse.Namespace
    deadline: Any
    heartbeat: Callable[[str], None] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    # codex-final-wire-isolation 的只读前检定下的运行坐标（CodexPlan）；其余核对为 None。
    codex: Any = None

    @property
    def job_id(self) -> str:
        return f"boundary-{self.check_id}"

    def relative(self, path: Path) -> str:
        return Path(path).relative_to(self.attempt_root).as_posix()

    def bind(self, role: str, path: Path) -> dict[str, Any]:
        entry = _binding(role, "attempt", self.attempt_root, Path(path))
        self.evidence.append(entry)
        return entry


@dataclass(frozen=True)
class CheckSpec:
    """一项核对：参数校验（零写入）、只读前检、执行与判定。"""

    check_id: str
    live_requests: bool
    validate_arguments: Callable[[argparse.Namespace], None]
    preflight: Callable[[BoundaryRun], None]
    execute: Callable[[BoundaryRun], dict[str, Any]]


def _require_check(arguments: argparse.Namespace) -> str:
    check_id = getattr(arguments, "check", None)
    if check_id not in CHECK_IDS:
        raise ConfigurationError(f"{ACTION} 必须用 --check 指定 {list(CHECK_IDS)} 之一。")
    return str(check_id)


def boundary_check_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``capture-candidate boundary-check``：返回 (摘要, 退出码)。通过或收据已存在 0，核对未通过 1。"""

    check_id = _require_check(arguments)
    spec = CHECKS[check_id]
    candidate_id = _safe_id(getattr(arguments, "candidate_id", None), "--candidate-id")
    attempt_id = _safe_id(getattr(arguments, "attempt_id", None), "--attempt-id")
    live = bool(getattr(arguments, "acknowledge_live_requests", False))
    if spec.live_requests and not live:
        raise ConfigurationError(f"{check_id} 会经候选网关发真实上游请求，必须显式确认 --acknowledge-live-requests。")
    if not spec.live_requests and live:
        raise ConfigurationError(f"{check_id} 零上游请求，不接受 --acknowledge-live-requests。")
    spec.validate_arguments(arguments)
    campaign_dir = Path(arguments.campaign_dir)
    claude_upgrade._validate_existing_campaign_path(campaign_dir)
    campaign_dir = campaign_dir.resolve(strict=True)
    manifest = claude_upgrade._require_formal_campaign(campaign_dir)
    active = claude_upgrade._ACTIVE_CAMPAIGN_LEASE
    if active is not None:
        if not claude_upgrade._lease_identity_matches(active, campaign_dir, phase="candidate", candidate_id=candidate_id):
            raise ConfigurationError(f"{ACTION} 与当前 Campaign lease 的 phase／candidate 不一致。")
        return _run_boundary_check(arguments, spec, campaign_dir, manifest, candidate_id, attempt_id, active.deadline)
    deadline = capture._attempt_deadline(arguments, "candidate")
    with claude_upgrade.CampaignLease(
        campaign_dir,
        phase="candidate",
        candidate_id=candidate_id,
        deadline=deadline,
        command="capture-candidate",
        allow_stale_recovery=False,
        campaign_id=str(manifest.get("campaign_id", "")),
    ):
        return _run_boundary_check(arguments, spec, campaign_dir, manifest, candidate_id, attempt_id, deadline)


def _admitted_attempt(
    campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, attempt_id: str, admission: Any
) -> tuple[Path, dict[str, Any]]:
    """被核对的 attempt：等待封存、没有失败 Job、没有被作废，身份重验且候选事实等于当前入口门的事实。"""

    key = ("candidate", candidate_id, attempt_id)
    if key in claude_upgrade._conflict_quarantined_attempts(campaign_dir) or key in capture._invalidated_attempts(campaign_dir):
        raise ConfigurationError(f"候选 attempt {attempt_id} 已被环境隔离或冲突隔离作废，不做边界核对。")
    attempt_root, attempt = claude_upgrade._load_capture_attempt(
        campaign_dir, "candidate", candidate_id, attempt_id, _verified_campaign_manifest=manifest, _replay_evidence_permissions=False
    )
    if attempt.get("status") != "awaiting_receipts":
        raise ConfigurationError(f"边界核对只对 awaiting_receipts 候选 attempt 执行（{attempt_id} 是 {attempt.get('status')}）。")
    failed = claude_upgrade._failed_job_ids(attempt.get("results"))
    if failed:
        raise ConfigurationError("候选 attempt 仍有失败 Job，先定向补跑：" + "、".join(failed))
    identity = attempt.get("identity") if isinstance(attempt.get("identity"), Mapping) else {}
    candidate_capture.verify_attempt_identity(manifest, identity)
    drift = sorted(name for name, value in admission.facts.items() if identity.get(name) != value)
    if drift:
        raise ConfigurationError(f"候选 attempt {attempt_id} 的身份与当前候选（入口门）不一致：{drift}；不是同一 revision 或同一构建。")
    return attempt_root, dict(attempt)


def _planned_run_root(attempt_root: Path, check_id: str) -> tuple[Path, str]:
    """本次执行的运行目录与它的名字（UTC 时刻＋8 位随机，同一秒内多次执行也不撞；只算路径，只读前检通过之后才建立）。"""

    name = f"{time.strftime('%Y%m%dt%H%M%Sz', time.gmtime())}-{secrets.token_hex(4)}"
    directory = attempt_root / CLIENT_EVIDENCE_DIRNAME / check_id / RUNS_DIRNAME / name
    if directory.exists() or directory.is_symlink():
        raise ConfigurationError(f"边界核对运行目录已存在：{directory}")
    return directory, name


def _write_contamination_marker(run: BoundaryRun, drifted: Sequence[str], failure_codes: Sequence[str]) -> None:
    """网关恢复失败或前后漂移：照候选 run 的口径写 Campaign 污染标记（不带 attempt_id，见文件头第二节）。"""

    try:
        claude_upgrade._secure_write_json_once(
            run.campaign_dir / capture.CONTAMINATION_MARKER,
            {
                "schema_version": capture.ENVIRONMENT_CONTAMINATION_SCHEMA,
                "client": capture.CLIENT,
                "mode": capture.MODE_FORMAL,
                "phase": "candidate",
                "candidate_id": run.admission.candidate_id,
                "drifted_kinds": [],
                "contaminated_jobs": [],
                "gateway_drifted_fields": list(drifted),
                "boundary_check": {
                    "check_id": run.check_id,
                    "attempt_id": run.attempt["attempt_id"],
                    "run": run.relative(run.run_root),
                    "failure_codes": list(failure_codes),
                },
                "recorded_at_utc": claude_upgrade._utc_now(),
            },
        )
    except (ConfigurationError, OSError):
        # 运行结论里的 environment_contaminated 是主事实，标记只是提示（与候选 run 同一口径）。
        pass


def _run_boundary_check(
    arguments: argparse.Namespace,
    spec: CheckSpec,
    campaign_dir: Path,
    manifest: dict[str, Any],
    candidate_id: str,
    attempt_id: str,
    deadline: Any,
) -> tuple[dict[str, Any], int]:
    check_id = spec.check_id
    # —— 只读门（零写入） ——
    claude_upgrade._reject_contaminated_campaign(campaign_dir)
    current_tool = claude_upgrade._tool_identity(include_git=False)
    claude_upgrade._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action=ACTION)
    admission = candidate_capture.formal_admission(campaign_dir, manifest, candidate_id, deadline=deadline)
    manifest = admission.manifest
    attempt_root, attempt = _admitted_attempt(campaign_dir, manifest, candidate_id, attempt_id, admission)
    existing = receipt_path(attempt_root, check_id)
    if existing.exists() or existing.is_symlink():
        try:
            receipt = verify_boundary_receipt(attempt_root, check_id, existing)
        except ConfigurationError as error:
            raise ConfigurationError(
                f"边界核对收据 {existing} 已存在但不可信（{error}）：先人工审计，本命令不删除、不覆盖已有收据。"
            ) from error
        return {
            "status": "already_passed",
            "check_id": check_id,
            "candidate_id": candidate_id,
            "attempt_id": attempt_id,
            "receipt": str(existing),
            "receipt_sha256": file_sha256(existing),
            "live_request_count": 0,
            "boundary_run": receipt["boundary_run"],
        }, 0
    layout = capture._capture_layout(campaign_dir, manifest)
    claude_upgrade._verify_execution_tree(capture._host_path(layout.capture_tool_root, layout).parents[1])
    readiness = candidate_capture.probe_gateway_environment(manifest, layout.host_data_root, "readiness", deadline=deadline)
    not_ready = candidate_capture.readiness_failures(readiness)
    if not_ready:
        raise ConfigurationError(f"候选网关环境未就绪：{not_ready}（先清理上一轮残留，或修正专用账号与 API Key）。")
    run_root, run_name = _planned_run_root(attempt_root, check_id)
    run = BoundaryRun(
        check_id=check_id,
        campaign_dir=campaign_dir,
        manifest=manifest,
        admission=admission,
        attempt_root=attempt_root,
        attempt=attempt,
        layout=layout,
        run_root=run_root,
        run_id=f"{str(manifest['campaign_id'])[:72]}-{check_id[:24]}-{run_name}".lower(),
        run_nonce=secrets.token_hex(32),
        started_at_utc=claude_upgrade._utc_now(),
        arguments=arguments,
        deadline=deadline,
    )
    if not claude_upgrade.SAFE_ID_RE.fullmatch(run.run_id):
        raise ConfigurationError(f"边界核对 run-id 非法：{run.run_id}")
    # 检查专属的只读前检（不通过即拒绝，不留运行目录），之后才建立运行目录。
    spec.preflight(run)
    capture._private_directory(run_root, attempt_root)
    # —— 执行：网关前后快照包住检查专属的执行 ——
    outcome: dict[str, Any] = {"failure_codes": [], "contaminated": False, "facts": {}, "live_requests": None}
    execution_error: BaseException | None = None
    before: dict[str, Any] | None = None
    drifted: list[str] = []
    gateway_error: BaseException | None = None
    try:
        deadline.check(f"boundary:{check_id}:gateway-before")
        before = candidate_capture.probe_gateway_environment(manifest, layout.host_data_root, "before", deadline=deadline)
        before_path = candidate_capture.write_gateway_snapshot(run_root, before)
        run.bind("gateway_before", before_path)
        not_ready = candidate_capture.readiness_failures(before)
        if not_ready:
            raise ConfigurationError(f"候选网关环境未就绪：{not_ready}，拒绝执行。")
        outcome = spec.execute(run)
    except BaseException as error:  # noqa: BLE001 —— 执行失败也要采 after 快照、写运行结论
        execution_error = error
    finally:
        if before is not None:
            try:
                after = candidate_capture.probe_gateway_environment(manifest, layout.host_data_root, "after")
                after_path = candidate_capture.write_gateway_snapshot(run_root, after)
                run.bind("gateway_after", after_path)
                drifted = candidate_capture.gateway_drift(before, after)
            except BaseException as error:  # noqa: BLE001
                gateway_error = error
    failure_codes = list(outcome.get("failure_codes") or [])
    contaminated = bool(outcome.get("contaminated"))
    if execution_error is not None:
        failure_codes.append(f"execution-error:{type(execution_error).__name__}")
    if before is not None and gateway_error is not None:
        failure_codes.append("gateway-after-probe-failed")
        contaminated = True
    if drifted:
        failure_codes.extend(f"gateway-drifted:{name}" for name in drifted)
        contaminated = True
    passed = not failure_codes and not contaminated and execution_error is None and outcome.get("live_requests") is not None
    ended_at = claude_upgrade._utc_now()
    live_requests = outcome.get("live_requests") or {"count": 0, "exact": True, "counting_rule": "not-executed", "by_endpoint": {}}
    verdict = {
        "schema_version": RUN_VERDICT_SCHEMA,
        "check_id": check_id,
        "campaign_id": manifest["campaign_id"],
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "attempt_run_nonce": attempt["run_nonce"],
        "boundary_run_id": run.run_id,
        "boundary_run_nonce": run.run_nonce,
        "started_at_utc": run.started_at_utc,
        "ended_at_utc": ended_at,
        "result": "pass" if passed else "fail",
        "failure_codes": sorted(set(failure_codes)),
        "environment_contaminated": contaminated,
        "gateway": {"compared_fields": list(candidate_capture.GATEWAY_COMPARED_FIELDS), "drifted_fields": drifted},
        "execution_error": capture._error_record(execution_error),
        "gateway_error": capture._error_record(gateway_error),
        "live_requests": live_requests,
        "facts": outcome.get("facts") or {},
        "evidence": list(run.evidence),
    }
    verdict_path = run_root / VERDICT_FILENAME
    claude_upgrade._secure_write_json_once(verdict_path, verdict)
    if contaminated:
        _write_contamination_marker(run, drifted, verdict["failure_codes"])
    result: dict[str, Any] = {
        "status": "passed" if passed else ("environment_contaminated" if contaminated else "failed"),
        "check_id": check_id,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "run": str(run_root),
        "verdict_sha256": file_sha256(verdict_path),
        "failure_codes": verdict["failure_codes"],
        "live_request_count": int(live_requests["count"]),
        "live_requests": live_requests,
    }
    if not passed:
        result["next_gate"] = (
            "环境隔离与恢复（environment-cleanup／environment-isolate，B2-08）后重跑本项"
            if contaminated
            else f"按 failure_codes 修复后重跑 {ACTION} --check {check_id}（不作废候选 attempt）"
        )
        if isinstance(execution_error, (KeyboardInterrupt, SystemExit)):
            raise execution_error
        return result, 1
    run.bind("verdict", verdict_path)
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "check_id": check_id,
        "campaign_id": manifest["campaign_id"],
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "run_nonce": attempt["run_nonce"],
        "result": "pass",
        "conclusion_scope": CONCLUSION_SCOPES[check_id],
        "candidate": {name: (attempt.get("identity") or {}).get(name) for name in CANDIDATE_FACT_FIELDS},
        "boundary_run": {
            "run_id": run.run_id,
            "run_nonce": run.run_nonce,
            "path": run.relative(run_root),
            "started_at_utc": run.started_at_utc,
            "ended_at_utc": ended_at,
        },
        "evidence": sorted(run.evidence, key=lambda entry: str(entry["role"])),
        "live_requests": live_requests,
        "facts": verdict["facts"],
        "issued_at_utc": claude_upgrade._utc_now(),
    }
    path = receipt_path(attempt_root, check_id)
    claude_upgrade._secure_write_json_once(path, receipt)
    # 写完立即按校验入口复核一遍（与 seal 同一判据）：不通过即失败关闭，收据留在原处供审计。
    verify_boundary_receipt(attempt_root, check_id, path)
    result.update({"receipt": str(path), "receipt_sha256": file_sha256(path), "next_gate": "capture-candidate seal（B4-02）"})
    return result, 0


# ---------------------------------------------------------------------------
# third-party-ingress-rejection（文件头第三节）
# ---------------------------------------------------------------------------


def _no_extra_arguments(arguments: argparse.Namespace) -> None:
    for name, flag in (
        ("codex_account_id", "--codex-account-id"),
        ("codex_api_key_id", "--codex-api-key-id"),
        ("codex_model", "--codex-model"),
        ("production_commit", "--production-commit"),
    ):
        if getattr(arguments, name, None) is not None:
            raise ConfigurationError(f"{CHECK_THIRD_PARTY} 不接受 {flag}（它只属于 {CHECK_CODEX}）。")


def _third_party_preflight(run: BoundaryRun) -> None:
    del run


def _third_party_restoration(report_path: Path, *, run: BoundaryRun, return_code: int | None) -> tuple[list[str], bool, dict[str, Any] | None]:
    """恢复报告核对：返回 (失败码, 是否环境污染, 报告内容)。前检退出（没有改动）不写报告，不算污染。"""

    variables = run.admission.variables
    if report_path.is_symlink() or not report_path.is_file():
        if isinstance(return_code, int) and return_code in PRECHECK_EXIT_CODES:
            return ["host-precheck-refused"], False, None
        return ["restoration-report-missing"], True, None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["restoration-report-unreadable"], True, None
    if not isinstance(report, Mapping) or report.get("schema_version") != candidate_capture.RESTORATION_REPORT_SCHEMA:
        return ["restoration-report-schema"], True, None
    if (
        report.get("run_id") != run.run_id
        or report.get("probe_id") != INGRESS_NEGATIVE_PROBE_ID
        or report.get("account_id") != variables["claude_account_id"]
        or report.get("api_key_id") != variables["api_key_id"]
        or report.get("trigger") != INGRESS_NEGATIVE_TRIGGER
    ):
        return ["restoration-report-identity"], True, dict(report)
    if report.get("status") == "restoration_failed" or return_code == RESTORATION_FAILED_EXIT_CODE:
        return ["restoration-failed"], True, dict(report)
    if not all(report.get(name) is True for name in candidate_capture.RESTORATION_EQUAL_FIELDS):
        return ["restoration-unverified"], True, dict(report)
    failures: list[str] = []
    if report.get("status") != "complete" or return_code != 0:
        failures.append("relay-step-failed")
    if report.get("account_tokens_unchanged") is not True:
        failures.append("account-tokens-changed")
    return failures, False, dict(report)


def _third_party_manifest(path: Path, *, run: BoundaryRun, host_receipt: Mapping[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    """中继负例清单的复核：返回 (失败码, 清单内容)。"""

    if path.is_symlink() or not path.is_file():
        return ["relay-manifest-missing"], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["relay-manifest-unreadable"], None
    if not isinstance(payload, Mapping):
        return ["relay-manifest-unreadable"], None
    variables = run.admission.variables
    gateway = payload.get("candidate_gateway") if isinstance(payload.get("candidate_gateway"), Mapping) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), Mapping) else {}
    bound = runtime.get("host_runtime_receipt") if isinstance(runtime.get("host_runtime_receipt"), Mapping) else {}
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), Mapping) else {}
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    failures: list[str] = []
    for code, passed in (
        ("relay-manifest-schema", payload.get("schema_version") == INGRESS_NEGATIVE_SCHEMA and payload.get("side") == "candidate"),
        ("relay-manifest-identity", payload.get("run_id") == run.run_id and payload.get("probe_id") == INGRESS_NEGATIVE_PROBE_ID),
        (
            "relay-manifest-gateway",
            gateway.get("account_id") == variables["claude_account_id"]
            and gateway.get("api_key_id") == variables["api_key_id"]
            and gateway.get("trigger") == INGRESS_NEGATIVE_TRIGGER
            and gateway.get("oauth_credentials_read") is False,
        ),
        (
            "runtime-binding-mismatch",
            bound.get("sha256") == host_receipt.get("sha256") and bound.get("run_nonce") == host_receipt.get("run_nonce"),
        ),
        ("relay-forwarding-enabled", upstream.get("live_requests") is False and upstream.get("production_forwarding_enabled") is False),
        ("relay-incomplete", payload.get("status") == "complete" and (payload.get("m_binding") or {}).get("complete") is True),
        ("secret-scan-failed", (payload.get("secret_scan") or {}).get("passed") is True),
        ("cases-incomplete", [item.get("case_id") if isinstance(item, Mapping) else None for item in cases] == list(INGRESS_NEGATIVE_CASE_IDS)),
        (
            "case-not-rejected",
            bool(cases)
            and all(
                isinstance(item, Mapping)
                and item.get("rejected") is True
                and item.get("transport_error") is None
                and item.get("http_status") in (item.get("expected_status") or [])
                for item in cases
            ),
        ),
        ("relay-connections", payload.get("relay_connections") == []),
    ):
        if not passed:
            failures.append(code)
    return failures, dict(payload)


def _third_party_execute(run: BoundaryRun) -> dict[str, Any]:
    layout = run.layout
    manifest = run.manifest
    configuration = manifest["configuration"]
    variables = run.admission.variables
    relay_output = run.run_root / "relay"
    output_container = capture._container_path(relay_output, layout)
    runtime_dir = f"{layout.capture_root}/runtime/claude-candidate/{run.run_id}"
    context = capture.ScenarioReceiptContext(
        campaign_id=str(manifest["campaign_id"]),
        attempt_id=str(run.attempt["attempt_id"]),
        run_nonce=run.run_nonce,
        evidence_root=run.run_root,
        campaign_dir=run.campaign_dir,
        attempt_root=run.run_root,
        mode=capture.MODE_FORMAL,
        layout=layout,
        manifest=manifest,
    )
    template = [
        "bash",
        f"{layout.capture_tool_root}/{claude_upgrade_plan.CANDIDATE_SCRIPT}",
        "docker",
        "exec",
        layout.capture_container,
        "python3",
        f"{layout.capture_tool_root}/{claude_upgrade_plan.RELAY_SCRIPT}",
        INGRESS_NEGATIVE_ACTION,
        "--execute",
        "--run-id",
        run.run_id,
        "--probe-id",
        INGRESS_NEGATIVE_PROBE_ID,
        "--output-root",
        str(output_container),
        "--expected-version",
        str(manifest["target_version"]),
        "--model",
        str(configuration["model"]),
        "--runtime-image",
        layout.runtime_image,
        "--timeout",
        str(THIRD_PARTY_CASE_TIMEOUT_SECONDS),
        "--candidate-runtime-dir",
        runtime_dir,
        "--candidate-relay-port",
        claude_upgrade_plan.CANDIDATE_RELAY_PORT,
    ]
    values, host_receipt = context.runtime_values(
        job_id=run.job_id,
        probe_id=INGRESS_NEGATIVE_PROBE_ID,
        run_index=1,
        step_index=1,
        argv=template,
        deadline=run.deadline,
        heartbeat=run.heartbeat,
    )
    # 凭据绑定的 path 相对执行上下文的 attempt_root，也就是本次运行目录。
    receipt_file = run.run_root.joinpath(*PurePosixPath(host_receipt["path"]).parts)
    run.bind("host_runtime_receipt", receipt_file)
    argv = [
        *template,
        "--host-runtime-receipt",
        values["host_runtime_receipt"],
        "--host-runtime-receipt-sha256",
        values["host_runtime_receipt_sha256"],
        "--run-nonce",
        values["run_nonce"],
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "CLAUDE_CANDIDATE_CAPTURE": claude_upgrade_plan.CANDIDATE_CAPTURE_GATE,
            "CAMPAIGN_DIR": str(run.campaign_dir),
            "CAPTURE_ROOT": str(layout.capture_root),
            "CAPTURE_CONTAINER": layout.capture_container,
            "SERVICE_CONTAINER": variables["service_container"],
            "KEEPER_CONTAINER": variables["keeper_container"],
            "POSTGRES_CONTAINER": variables["postgres_container"],
            "ACCOUNT_ID": str(variables["claude_account_id"]),
            "API_KEY_ID": str(variables["api_key_id"]),
            "RUN_ID": run.run_id,
            "PROBE_ID": INGRESS_NEGATIVE_PROBE_ID,
            "OUTPUT_ROOT": str(output_container),
            "CANDIDATE_RUNTIME_DIR": runtime_dir,
            "RELAY_PORT": claude_upgrade_plan.CANDIDATE_RELAY_PORT,
            "GATEWAY_TRIGGER": INGRESS_NEGATIVE_TRIGGER,
            "CA_CERT": str(configuration["ca_cert"]),
        }
    )
    log_path = run.run_root.joinpath(*STEP_LOG_RELATIVE.parts)
    capture._private_directory(log_path.parent, run.run_root)
    return_code = context.execute_step(
        argv,
        operation=f"job:{run.job_id}:step-1:attempt-1",
        timeout_seconds=THIRD_PARTY_STEP_TIMEOUT_SECONDS,
        environment=environment,
        log_path=log_path,
        job_id=run.job_id,
        deadline=run.deadline,
        heartbeat=run.heartbeat,
    )
    run.bind("step_log", log_path)
    report_path = run.run_root / candidate_capture.RESTORATION_REPORT_DIRNAME / f"{INGRESS_NEGATIVE_PROBE_ID}.json"
    restoration_failures, contaminated, report = _third_party_restoration(report_path, run=run, return_code=return_code)
    if report is not None:
        run.bind("restoration_report", report_path)
    manifest_path = relay_output / INGRESS_NEGATIVE_MANIFEST
    manifest_failures, payload = _third_party_manifest(manifest_path, run=run, host_receipt=host_receipt)
    if payload is not None:
        run.bind("relay_manifest", manifest_path)
    cases = [
        {
            "case_id": item.get("case_id"),
            "http_status": item.get("http_status"),
            "expected_status": item.get("expected_status"),
            "rejected": item.get("rejected"),
            "error_type": item.get("error_type"),
        }
        for item in ((payload or {}).get("cases") or [])
        if isinstance(item, Mapping)
    ]
    connections = (payload or {}).get("relay_connections")
    return {
        "failure_codes": [*restoration_failures, *manifest_failures],
        "contaminated": contaminated,
        "facts": {
            "step_return_code": return_code,
            "relay_run_id": run.run_id,
            "cases": cases,
            "relay_connection_count": len(connections) if isinstance(connections, list) else None,
            "account_tokens_unchanged": (report or {}).get("account_tokens_unchanged"),
            "restoration_status": (report or {}).get("status"),
        },
        # 中继从不转发上游：真实上游请求恒为 0，按「中继监听连接数」规则精确入账（有连接即判失败，不会签收据）。
        "live_requests": {"count": 0, "exact": True, "counting_rule": "relay-reject-only-zero-forwarding", "by_endpoint": {}},
    }


# ---------------------------------------------------------------------------
# 核对登记与命令行
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# codex-final-wire-isolation（文件头第五节：structural＋runtime）
# ---------------------------------------------------------------------------

STRUCTURAL_REPORT_SCHEMA = "claude-upgrade-codex-structural-isolation/v1"
RUNTIME_REPORT_SCHEMA = "claude-upgrade-codex-runtime-isolation/v1"
STRUCTURAL_REPORT_FILENAME = "structural-report.json"
CODEX_DIRNAME = "codex"
RUNTIME_REPORT_RELATIVE = PurePosixPath(CODEX_DIRNAME) / "runtime-report.json"
CODEX_JOB_ID = "candidate-core-mitm"
CODEX_MITM_SCRIPT = "run_sub2api_openai_mitm_matrix.sh"
CODEX_MITM_ARGV = ("bash", "{repo_root}/tools/official_client_capture/" + CODEX_MITM_SCRIPT)
# Codex 冻结步骤 environment 里允许出现的占位（其余一律失败关闭）与本项的取值来源（见 _codex_placeholders）。
CODEX_STEP_PLACEHOLDERS = frozenset(
    {
        "capture_container",
        "capture_root",
        "service_container",
        "keeper_container",
        "postgres_container",
        "codex_account_id",
        "api_key_id",
        "model",
        "target_version",
        "capture_codex_bin",
        "campaign_id",
        "repo_root",
    }
)
# 抓包容器里 Codex 官方二进制的固定坐标（与 Codex mitm 脚本缺省、ARM64 上 Codex 升级安装位置一致）。
CODEX_CAPTURE_BIN_TEMPLATE = "/opt/codex-{version}/bin/codex"
CODEX_PROFILE_COPY_TEMPLATE = "runtime/codex-profile-{version}.json"
CODEX_FINGERPRINT_PROXY = "runtime/codex-fingerprint-capture-proxy"
CODEX_CAPTURE_MANIFEST_SCHEMA = "codex-candidate-capture-manifest/v1"
CODEX_RUN_PREFIX = "cwi-"
CODEX_TOOL_SCRIPTS = (
    CODEX_MITM_SCRIPT,
    "build_evidence_catalog.py",
    "build_assertion_bundle.py",
    "derive_official_observations.py",
    "candidate_rule_assertion.py",
    "codex_upgrade_live_request_provenance.py",
)
CODEX_TOOL_DATA = {
    "scenarios": "codex_upgrade_scenarios_{key}.json",
    "labels": "codex_upgrade_evidence_labels_{key}.json",
    "expectations": "candidate_rule_expectations_{key}.json",
    "rules": "codex_upgrade_rules_{key}.json",
}
CODEX_PIPELINE_TIMEOUT_SECONDS = 1800
CODEX_TEMP_PROXY_PREFIX = "review-fix-sub2api-mitm-"
EXPECTED_CODEX_ACCOUNT_SHAPE = "openai|oauth|NULL|live|active"
# 运行时判定的检查分类：覆盖检查与没有选中观测、或所需场景不在本次证据里的检查记未覆盖、不判。
COVERAGE_CHECK_ID = "scenario-artifact-coverage"
INPUT_VALIDATION_CHECK_ID = "input-validation"

# structural 节的比较集合（具名常量与依据）。
# 1. officialegress 包（含子包与 catalogdata 目录数据）是 Codex 画像、Release、wire、编译器与执行器的唯一来源；其中
#    ``claude_`` 前缀文件与 ``catalogdata/claude/`` 只属于 Claude 侧，不进 Codex 运行面；测试文件不进生产二进制。
CODEX_EGRESS_PACKAGE = "backend/internal/officialegress/"
CODEX_EGRESS_EXCLUDED_DATA = "backend/internal/officialegress/catalogdata/claude/"
CODEX_CATALOGDATA = "backend/internal/officialegress/catalogdata/"
CODEX_RUNTIME_RELEASE_CATALOG = "backend/internal/officialegress/catalogdata/runtime/release-catalog.json"
# 2. officialegress 包外的 Codex 出站定型面：与出站门禁（tools/check_ledger_completeness.py）同一界定——backend 下非测试
#    Go 文件里命中 SURFACE_RE 的文件（去掉 SCOPE_EXCLUSIONS 的两个离线工具），加上 REQUIRED_REVIEW_TOUCHPOINTS、
#    IDENTITY_BOUNDARY_TOUCHPOINTS 与 HUMAN_REVIEW_SEAMS 三组人工复核接缝里的 backend Go 文件。受管根不得导入根外模块，
#    这里逐字复制，一致性由 test_claude_upgrade_boundary_checks 对照原模块锁定。
CODEX_SURFACE_SCAN_ROOT = "backend/"
CODEX_SURFACE_RE = re.compile(
    r"officialCodex|OfficialCodex|[Cc]odex\d{3,}"
    r"|OpenAIOfficialEgress|officialEgressWebSocket"
    r"|compiledOfficialEgressHTTPClient|officialEgressHTTPClient"
    r"|officialOpenAIHTTPBodyContract|OfficialEgressTransportWebSocket"
)
CODEX_SURFACE_SCOPE_EXCLUSIONS = frozenset(
    {"backend/cmd/egressscan/classify.go", "backend/cmd/egressprofiledump/genenums.go"}
)
CODEX_REVIEW_TOUCHPOINTS = frozenset(
    {
        # REQUIRED_REVIEW_TOUCHPOINTS
        "backend/cmd/server/wire.go",
        "backend/internal/config/config.go",
        "backend/internal/pkg/apicompat/chatcompletions_responses_bridge.go",
        "backend/internal/pkg/apicompat/responses_namespace.go",
        "backend/internal/pkg/httpclient/pool.go",
        "backend/internal/repository/http_upstream.go",
        "backend/internal/service/account.go",
        "backend/internal/service/openai_codex_identity.go",
        "backend/internal/service/openai_codex_transform.go",
        "backend/internal/service/openai_content_session_seed.go",
        "backend/internal/service/openai_cookie_jar.go",
        "backend/internal/service/openai_gateway_request_body.go",
        "backend/internal/service/openai_gateway_service.go",
        "backend/internal/service/openai_model_capabilities.go",
        "backend/internal/service/openai_oauth_service.go",
        "backend/internal/service/openai_responses_lite_tools.go",
        "backend/internal/service/openai_responses_namespace.go",
        "backend/internal/service/openai_ws_forwarder.go",
        "backend/internal/service/openai_ws_protocol_resolver.go",
        "backend/internal/service/official_egress_upstream_identity_bridge.go",
        # IDENTITY_BOUNDARY_TOUCHPOINTS 里的 backend Go 文件
        "backend/internal/handler/admin/setting_handler.go",
        "backend/internal/handler/admin/setting_handler_update.go",
        "backend/internal/handler/dto/settings.go",
        "backend/internal/pkg/openai/request.go",
        "backend/internal/service/domain_constants.go",
        "backend/internal/service/openai_codex_version_sync_service.go",
        "backend/internal/service/setting_gateway_runtime.go",
        "backend/internal/service/setting_parse.go",
        "backend/internal/service/setting_update.go",
        "backend/internal/service/settings_view.go",
        "backend/internal/service/wire.go",
        # HUMAN_REVIEW_SEAMS 里另有的 backend Go 文件
        "backend/internal/service/official_egress_identity_authority.go",
        "backend/internal/officialegress/compiler.go",
        "backend/internal/officialegress/executor.go",
        "backend/internal/service/openai_ws_forwarder_payload.go",
        "backend/internal/service/openai_quota_service.go",
    }
)
STRUCTURAL_GROUP_RATIONALES = {
    "codex_release_catalog": "Codex 运行目录与 production_active（Release graph 的 active 节点）指向的 Release、快照目录与画像文件",
    "codex_runtime_projection": "officialegress/catalogdata 下全部 Codex 目录数据（运行投影、画像、Release graph、迁移与回执），不含 catalogdata/claude",
    "codex_egress_go": "officialegress 包（含子包）非 Claude、非测试文件，加上包外 Codex 出站定型面与人工复核接缝（同出站门禁口径）",
}


def _codex_arguments(arguments: argparse.Namespace) -> None:
    """codex-final-wire-isolation 的轮次参数（零写入）：Codex 账号与 API Key ID、Codex 模型、生产部署提交。"""

    for name, flag in (("codex_account_id", "--codex-account-id"), ("codex_api_key_id", "--codex-api-key-id")):
        value = getattr(arguments, name, None)
        if type(value) is not int or value <= 0:
            raise ConfigurationError(f"{CHECK_CODEX} 必须用 {flag} 给出本轮的正整数 ID（轮次参数，不写死）。")
    model = getattr(arguments, "codex_model", None)
    if not isinstance(model, str) or not claude_upgrade.SAFE_ID_RE.fullmatch(model):
        raise ConfigurationError(f"{CHECK_CODEX} 必须用 --codex-model 给出本轮 Codex 主轨模型。")
    commit = getattr(arguments, "production_commit", None)
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ConfigurationError(f"{CHECK_CODEX} 必须用 --production-commit 给出当前生产部署的 40 位源码提交。")


# —— structural：git 与工作树读取 ——


def _git(source_root: Path, *args: str, stdin: bytes | None = None, timeout: float = 300) -> bytes:
    """只读 git 命令（不刷新索引：GIT_OPTIONAL_LOCKS=0）；失败即 ConfigurationError。"""

    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigurationError(f"git {args[0]} 无法执行：{type(error).__name__}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:300]
        raise ConfigurationError(f"git {args[0]} 失败：{detail}")
    return completed.stdout


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 —— git 对象 ID 的定义


def _commit_entries(source_root: Path, commit: str) -> dict[str, str]:
    """生产提交里 backend 下的文件 → git blob ID。"""

    raw = _git(source_root, "ls-tree", "-r", "-z", "--full-tree", commit, "--", "backend")
    entries: dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _tab, path = record.partition(b"\t")
        fields = meta.split(b" ")
        if len(fields) != 3 or fields[1] != b"blob":
            continue
        entries[path.decode("utf-8", "surrogateescape")] = fields[2].decode("ascii")
    return entries


def _commit_blobs(source_root: Path, blob_ids: Sequence[str]) -> dict[str, bytes]:
    """批量读取 blob 内容（``git cat-file --batch``）。"""

    wanted = sorted(set(blob_ids))
    if not wanted:
        return {}
    raw = _git(source_root, "cat-file", "--batch", stdin=("\n".join(wanted) + "\n").encode("ascii"), timeout=600)
    blobs: dict[str, bytes] = {}
    offset = 0
    while offset < len(raw):
        newline = raw.index(b"\n", offset)
        header = raw[offset:newline].decode("ascii").split(" ")
        if len(header) != 3 or header[1] != "blob":
            raise ConfigurationError(f"git cat-file 输出非预期：{header}")
        size = int(header[2])
        start = newline + 1
        blobs[header[0]] = raw[start : start + size]
        offset = start + size + 1
    if set(blobs) != set(wanted):
        raise ConfigurationError("git cat-file 没有返回全部 blob。")
    return blobs


def _worktree_entries(source_root: Path) -> dict[str, Path]:
    """候选源码树（构建输入，入口门已按构建收据复算树摘要）里 backend 下的文件。"""

    base = source_root / "backend"
    entries: dict[str, Path] = {}
    if not base.is_dir() or base.is_symlink():
        raise ConfigurationError(f"候选源码树没有 backend 目录：{base}")
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(source_root)
        if ".git" in relative.parts or path.is_dir():
            continue
        entries[relative.as_posix()] = path
    return entries


def _worktree_bytes(path: Path) -> bytes:
    return os.readlink(path).encode("utf-8") if path.is_symlink() else path.read_bytes()


def _egress_package_member(path: str) -> bool:
    if not path.startswith(CODEX_EGRESS_PACKAGE) or path.startswith(CODEX_EGRESS_EXCLUDED_DATA):
        return False
    name = PurePosixPath(path).name
    return not name.startswith("claude_") and not name.endswith("_test.go")


def _surface_candidate(path: str) -> bool:
    return (
        path.startswith(CODEX_SURFACE_SCAN_ROOT)
        and path.endswith(".go")
        and not path.endswith("_test.go")
        and path not in CODEX_SURFACE_SCOPE_EXCLUSIONS
    )


def _codex_active(source_root: Path) -> dict[str, Any]:
    """候选源码树里 Codex 运行目录的 active 选择：唯一版本、唯一快照摘要与画像文件（路径相对源码根）。"""

    package = source_root / CODEX_EGRESS_PACKAGE
    catalog_path = source_root / CODEX_RUNTIME_RELEASE_CATALOG
    catalog = claude_upgrade._read_json(catalog_path, "Codex 运行目录")
    graph_binding = catalog.get("release_graph")
    snapshot_binding = catalog.get("snapshot_catalog")
    bindings: dict[str, Path] = {}
    for label, binding in (("release_graph", graph_binding), ("snapshot_catalog", snapshot_binding)):
        if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
            raise ConfigurationError(f"Codex 运行目录缺少 {label} 绑定。")
        target = package / str(binding["path"])
        claude_upgrade._reject_symlink_components(target, package, f"Codex {label}")
        if not target.is_file() or file_sha256(target) != binding.get("sha256"):
            raise ConfigurationError(f"Codex 运行目录的 {label} 文件缺失或摘要不符。")
        bindings[label] = target
    graph = claude_upgrade._read_json(bindings["release_graph"], "Codex Release graph")
    active = [node for node in graph.get("nodes") or [] if isinstance(node, Mapping) and node.get("mode") == "active"]
    versions = {str((node.get("build") or {}).get("version", "")) for node in active}
    digests = {str((node.get("snapshot") or {}).get("digest", "")) for node in active}
    if not active or len(versions) != 1 or len(digests) != 1:
        raise ConfigurationError(f"Codex Release graph 的 active 节点不是唯一版本与唯一快照：{sorted(versions)}／{sorted(digests)}")
    version, digest = versions.pop(), digests.pop()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not capture.SHA256_RE.fullmatch(digest):
        raise ConfigurationError("Codex active 版本或快照摘要非法。")
    profile = package / "catalogdata" / "runtime" / "profiles" / version / f"{digest}.json"
    claude_upgrade._reject_symlink_components(profile, package, "Codex active 画像")
    if not profile.is_file() or claude_upgrade._read_json(profile, "Codex active 画像").get("Digest") != digest:
        raise ConfigurationError("Codex active 画像文件缺失或内容 Digest 与快照摘要不符。")
    return {
        "version": version,
        "snapshot_digest": digest,
        "purposes": sorted(str(node.get("purpose", "")) for node in active),
        "files": sorted(
            path.relative_to(source_root).as_posix()
            for path in (catalog_path, bindings["release_graph"], bindings["snapshot_catalog"], profile)
        ),
        "profile": profile.relative_to(source_root).as_posix(),
        "profile_sha256": file_sha256(profile),
    }


def _structural_report(run: BoundaryRun) -> dict[str, Any]:
    """structural 节（零请求）：候选源码树（构建输入）与生产部署提交在三组 Codex 构建输入上逐字节比较。"""

    facts = run.admission.facts
    source_root = Path(str(facts["source_root"]))
    production = str(run.arguments.production_commit)
    toplevel = Path(_git(source_root, "rev-parse", "--show-toplevel").decode("utf-8").strip())
    if toplevel.resolve() != source_root.resolve():
        raise ConfigurationError(f"候选源码根不是 git 仓库根：{source_root}")
    _git(source_root, "cat-file", "-e", f"{production}^{{commit}}")
    active = _codex_active(source_root)
    worktree = _worktree_entries(source_root)
    committed = _commit_entries(source_root, production)
    # 包外定型面：两侧各自按 SURFACE_RE 扫描非测试 Go 文件后取并集。
    production_go = {path: blob for path, blob in committed.items() if _surface_candidate(path)}
    production_text = _commit_blobs(source_root, list(production_go.values()))
    surface = {
        path
        for path, blob in production_go.items()
        if CODEX_SURFACE_RE.search(production_text[blob].decode("utf-8", "ignore"))
    }
    for path, file in worktree.items():
        if _surface_candidate(path) and not file.is_symlink() and CODEX_SURFACE_RE.search(file.read_text(encoding="utf-8", errors="ignore")):
            surface.add(path)
    universe = set(worktree) | set(committed)
    groups_paths = {
        "codex_release_catalog": set(active["files"]),
        "codex_runtime_projection": {
            path for path in universe if path.startswith(CODEX_CATALOGDATA) and not path.startswith(CODEX_EGRESS_EXCLUDED_DATA)
        },
        "codex_egress_go": {
            path for path in universe if _egress_package_member(path) and not path.startswith(CODEX_CATALOGDATA)
        }
        | surface
        | set(CODEX_REVIEW_TOUCHPOINTS),
    }
    groups: dict[str, Any] = {}
    for name, paths in groups_paths.items():
        differences: list[dict[str, str]] = []
        for path in sorted(paths):
            in_tree, in_commit = path in worktree, path in committed
            if not in_tree and not in_commit:
                continue
            if in_tree and not in_commit:
                differences.append({"path": path, "change": "added"})
            elif in_commit and not in_tree:
                differences.append({"path": path, "change": "removed"})
            elif _blob_id(_worktree_bytes(worktree[path])) != committed[path]:
                differences.append({"path": path, "change": "modified"})
        groups[name] = {
            "rationale": STRUCTURAL_GROUP_RATIONALES[name],
            "compared": len([path for path in paths if path in worktree or path in committed]),
            "differences": differences,
        }
    return {
        "schema_version": STRUCTURAL_REPORT_SCHEMA,
        "production_commit": production,
        "candidate_source_root": str(source_root),
        "candidate_git_commit": facts.get("git_commit"),
        "candidate_source_tree_sha256": facts.get("source_tree_sha256"),
        "codex_active": active,
        "groups": groups,
        "result": "pass" if all(not group["differences"] for group in groups.values()) else "fail",
        "recorded_at_utc": claude_upgrade._utc_now(),
    }


# —— runtime：环境、Codex 冻结步骤、执行与判定 ——


def _capture_root_alias(layout: Any) -> Path:
    """Codex mitm 脚本把 CAPTURE_ROOT 同时当宿主与容器坐标：取一个在宿主存在、且与宿主数据根是同一目录的容器别名。"""

    host = Path(layout.host_data_root).resolve(strict=True)
    for alias in layout.container_data_aliases():
        candidate = Path(str(alias))
        try:
            if candidate.is_dir() and candidate.resolve(strict=True) == host:
                return candidate
        except OSError:
            continue
    raise ConfigurationError(
        "Codex mitm 脚本要求数据根在宿主与抓包容器里是同一路径：没有任何容器数据根别名在宿主上指向宿主数据根。"
    )


def _codex_tool_paths(tool_root: Path, version: str) -> dict[str, Path]:
    key = version.replace(".", "_")
    paths = {name: tool_root / name for name in CODEX_TOOL_SCRIPTS}
    paths.update({label: tool_root / template.format(key=key) for label, template in CODEX_TOOL_DATA.items()})
    for label, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"工具执行副本缺少 Codex {version} 的 {label}：{path.name}")
    return paths


def _codex_job(scenarios: Path, version: str) -> dict[str, Any]:
    """Codex 冻结场景清单里的 candidate-core-mitm（只读数据，不导入 Codex 编排器）。"""

    manifest = claude_upgrade._read_json(scenarios, "Codex 场景清单")
    if manifest.get("codex_version") != version:
        raise ConfigurationError(f"Codex 场景清单版本不是 {version}。")
    jobs = [job for job in manifest.get("capture_jobs") or [] if isinstance(job, Mapping) and job.get("id") == CODEX_JOB_ID]
    if len(jobs) != 1:
        raise ConfigurationError(f"Codex {version} 场景清单没有唯一的 {CODEX_JOB_ID}。")
    job = dict(jobs[0])
    steps = job.get("steps")
    if (
        job.get("phase") != "candidate"
        or not isinstance(steps, list)
        or len(steps) != 1
        or list(steps[0].get("argv") or []) != list(CODEX_MITM_ARGV)
        or not isinstance(steps[0].get("environment"), Mapping)
        or not isinstance(job.get("evidence_roots"), list)
        or len(job["evidence_roots"]) != 1
        or not isinstance(job.get("covers"), list)
        or not job["covers"]
    ):
        raise ConfigurationError(f"Codex {CODEX_JOB_ID} 的冻结步骤形态不是预期的单步 mitm 矩阵。")
    for value in [*steps[0]["environment"].values(), job["evidence_roots"][0]]:
        unknown = set(re.findall(r"\{([A-Za-z_]+)\}", str(value))) - CODEX_STEP_PLACEHOLDERS
        if unknown:
            raise ConfigurationError(f"Codex {CODEX_JOB_ID} 的冻结步骤含未登记的占位：{sorted(unknown)}")
    return job


def _expand(template: str, values: Mapping[str, str]) -> str:
    return re.sub(r"\{([A-Za-z_]+)\}", lambda match: values[match.group(1)], template)


def _codex_db_query(manifest: Mapping[str, Any], sql: str, operation: str, *, deadline: Any | None) -> str:
    postgres = candidate_capture.candidate_variables(manifest)["postgres_container"]
    environment = capture._run_probe_command(
        ["docker", "inspect", "--format", candidate_capture.ENV_TEMPLATE, postgres], operation="codex-boundary:postgres-env", deadline=deadline
    ).stdout
    user = candidate_capture._env_value(environment, "POSTGRES_USER")
    database = candidate_capture._env_value(environment, "POSTGRES_DB")
    if not user or not database:
        raise ConfigurationError("无法读取 PostgreSQL 连接元数据。")
    completed = capture._run_probe_command(
        ["docker", "exec", postgres, "psql", "-U", user, "-d", database, "-qAtc", sql], operation=f"codex-boundary:{operation}", deadline=deadline
    )
    return str(completed.stdout).strip()


def _codex_account_facts(manifest: Mapping[str, Any], account_id: int, api_key_id: int, *, deadline: Any | None = None) -> dict[str, Any]:
    """Codex 专用账号与 API Key 的只读事实（不读任何秘密）：形态、代理、分组隔离与残留临时代理。"""

    def query(sql: str, operation: str) -> str:
        return _codex_db_query(manifest, sql, operation, deadline=deadline)

    account_shape = query(
        "select platform || '|' || type || '|' || coalesce(parent_account_id::text,'NULL') || '|' || "
        f"case when deleted_at is null then 'live' else 'deleted' end || '|' || status from accounts where id = {int(account_id)}",
        "account-shape",
    )
    proxy_state = query(
        "select coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') "
        f"from accounts where id = {int(account_id)}",
        "account-proxy",
    )
    key_shape = query(
        "select status || '|' || coalesce(group_id::text,'NULL') || '|' || "
        f"case when deleted_at is null then 'live' else 'deleted' end from api_keys where id = {int(api_key_id)}",
        "api-key-shape",
    )
    match = re.fullmatch(r"active\|([0-9]+)\|live", key_shape)
    eligible = (
        query(
            "select coalesce(string_agg(a.id::text, ',' order by a.id), '') from account_groups ag join accounts a on a.id = ag.account_id "
            f"where ag.group_id = {int(match.group(1))} and a.platform = 'openai' and a.status = 'active' "
            "and a.schedulable = true and a.deleted_at is null",
            "api-key-group",
        )
        if match
        else ""
    )
    residual = query(f"select count(*) from proxies where name like '{CODEX_TEMP_PROXY_PREFIX}%'", "residual-proxies")
    return {
        "account": {"id": int(account_id), "shape": account_shape, "proxy_state": proxy_state},
        "api_key": {"id": int(api_key_id), "shape": key_shape, "eligible_accounts": eligible},
        "residual_temporary_proxies": int(residual) if residual.isdigit() else residual,
    }


def _codex_account_failures(facts: Mapping[str, Any]) -> list[str]:
    account, key = facts["account"], facts["api_key"]
    failures: list[str] = []
    if account["shape"] != EXPECTED_CODEX_ACCOUNT_SHAPE:
        failures.append("codex-account-shape")
    if account["proxy_state"] != "NULL|NULL":
        failures.append("codex-account-proxy-bound")
    if not re.fullmatch(r"active\|[0-9]+\|live", str(key["shape"])):
        failures.append("codex-api-key-shape")
    if key["eligible_accounts"] != str(account["id"]):
        failures.append("codex-api-key-group-not-isolated")
    if facts["residual_temporary_proxies"] != 0:
        failures.append("codex-residual-temporary-proxy")
    return failures


@dataclass
class CodexPlan:
    """只读前检定下的 Codex 运行坐标（执行阶段原样使用）。"""

    active: dict[str, Any]
    tools: dict[str, Path]
    tool_root: Path
    repo_root: Path
    capture_root: Path
    job: dict[str, Any]
    argv: list[str]
    environment: dict[str, str]
    roots_glob: str
    expected_roots: int


def _codex_plan(run: BoundaryRun) -> CodexPlan:
    layout = run.layout
    arguments = run.arguments
    source_root = Path(str(run.admission.facts["source_root"]))
    active = _codex_active(source_root)
    version = active["version"]
    tool_root = capture._host_path(layout.capture_tool_root, layout)
    tools = _codex_tool_paths(tool_root, version)
    job = _codex_job(tools["scenarios"], version)
    capture_root = _capture_root_alias(layout)
    relative_tools = tool_root.resolve(strict=True).relative_to(Path(layout.host_data_root).resolve(strict=True))
    variables = run.admission.variables
    values = {
        "repo_root": str(tool_root.parents[1]),
        "capture_root": str(capture_root),
        "capture_container": layout.capture_container,
        "service_container": variables["service_container"],
        "keeper_container": variables["keeper_container"],
        "postgres_container": variables["postgres_container"],
        "codex_account_id": str(arguments.codex_account_id),
        "api_key_id": str(arguments.codex_api_key_id),
        "model": str(arguments.codex_model),
        "target_version": version,
        "capture_codex_bin": CODEX_CAPTURE_BIN_TEMPLATE.format(version=version),
        # 只换 Campaign 坐标：每次执行一个新的短前缀（证据根名受 128 字符上限约束），Codex 的场景 checkpoint 不会把旧坐标
        # 当已通过复用；运行目录名里的随机段保证同一秒内的多次执行也不撞。
        "campaign_id": f"{CODEX_RUN_PREFIX}{run.run_root.name}",
    }
    step = job["steps"][0]
    argv = [_expand(str(item), values) for item in step["argv"]]
    environment = {str(key): _expand(str(value), values) for key, value in step["environment"].items()}
    # 工具执行副本按同一别名给出（脚本缺省 $CAPTURE_ROOT/tools/official_client_capture 未必是本 Campaign 的执行副本）。
    environment["CAPTURE_TOOL_ROOT"] = str(capture_root.joinpath(*relative_tools.parts))
    subjects = environment.get("SUBJECTS", "").split()
    scenarios = environment.get("SCENARIOS", "").split()
    if not subjects or not scenarios:
        raise ConfigurationError(f"Codex {CODEX_JOB_ID} 的冻结步骤没有 SUBJECTS／SCENARIOS。")
    data_root = Path(layout.host_data_root)
    profile_copy = data_root / CODEX_PROFILE_COPY_TEMPLATE.format(version=version)
    if profile_copy.is_symlink() or not profile_copy.is_file() or profile_copy.read_bytes() != (source_root / active["profile"]).read_bytes():
        raise ConfigurationError(
            f"数据根的 Codex 运行画像副本 {profile_copy.name} 缺失或与候选源码树的 active 画像不一致（先补齐 Codex 运行画像副本）。"
        )
    proxy = data_root / CODEX_FINGERPRINT_PROXY
    if proxy.is_symlink() or not proxy.is_file() or not os.access(proxy, os.X_OK):
        raise ConfigurationError("数据根缺少可执行的 Codex 指纹转发器（runtime/codex-fingerprint-capture-proxy）。")
    return CodexPlan(
        active=active,
        tools=tools,
        tool_root=tool_root,
        repo_root=tool_root.parents[1],
        capture_root=capture_root,
        job=job,
        argv=argv,
        environment=environment,
        roots_glob=_expand(str(job["evidence_roots"][0]), values),
        expected_roots=len(subjects) * len(scenarios),
    )


def _codex_preflight(run: BoundaryRun) -> None:
    """只读前检：候选源码仓与生产提交、Codex 运行坐标与执行环境齐备、Codex 专用账号与 API Key 形态正确（不通过即拒绝、
    不留运行目录）。"""

    source_root = Path(str(run.admission.facts["source_root"]))
    toplevel = Path(_git(source_root, "rev-parse", "--show-toplevel").decode("utf-8").strip())
    if toplevel.resolve() != source_root.resolve():
        raise ConfigurationError(f"候选源码根不是 git 仓库根：{source_root}")
    _git(source_root, "cat-file", "-e", f"{run.arguments.production_commit}^{{commit}}")
    run.codex = _codex_plan(run)
    facts = _codex_account_facts(run.manifest, run.arguments.codex_account_id, run.arguments.codex_api_key_id, deadline=run.deadline)
    failures = _codex_account_failures(facts)
    if failures:
        raise ConfigurationError(f"Codex 专用账号或 API Key 未就绪：{failures}（单账号隔离分组、未绑定代理、无残留临时代理）。")


def _codex_roots(plan: CodexPlan, layout: Any) -> tuple[list[Path], list[Path]]:
    """本次执行产出的 Codex 证据根（按冻结的 evidence_roots 通配）与失败归档根（``.failed``）。"""

    pattern = PurePosixPath(plan.roots_glob)
    host_dir = Path(str(pattern.parent))
    complete = sorted(path for path in host_dir.glob(pattern.name) if path.is_dir() and not path.is_symlink())
    failed = sorted(path for path in host_dir.glob(pattern.name + ".failed") if path.is_dir() and not path.is_symlink())
    return complete, failed


def _codex_request_count(complete: Sequence[Path], failed: Sequence[Path]) -> dict[str, Any]:
    """真实请求精确计数：调用 Codex 自己的 mitm 逐请求计数（只调用、不改）；失败归档根也计（请求已经发出）。"""

    from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance

    by_transport: dict[str, int] = {}
    exact = True
    errors: list[str] = []

    def add(requests: Sequence[Mapping[str, Any]]) -> None:
        for request in requests:
            transport = str((request.get("native_coordinate") or {}).get("transport", "unknown"))
            by_transport[transport] = by_transport.get(transport, 0) + 1

    for root in complete:
        try:
            for branch in provenance._mitm_candidate_branches(root):
                add(branch["requests"])
        except Exception as error:  # noqa: BLE001 —— 任一根计不出即整体不精确
            exact = False
            errors.append(f"{root.name}: {type(error).__name__}")
    for root in failed:
        mitm = root / "mitm"
        for subject in sorted(mitm.iterdir()) if mitm.is_dir() else []:
            for name, parser in (("codex-http.jsonl", provenance._mitm_http_requests), ("codex-ws.jsonl", provenance._mitm_ws_requests)):
                path = subject / name
                if not path.is_file():
                    continue
                try:
                    add(parser(path, producer_run_id=root.name, source_kind="candidate_mitm", coordinate_prefix={"evidence": "mitm", "subject": subject.name}))
                except Exception as error:  # noqa: BLE001
                    exact = False
                    errors.append(f"{root.name}/{subject.name}/{name}: {type(error).__name__}")
    return {
        "count": sum(by_transport.values()),
        "exact": exact,
        "counting_rule": "codex_upgrade_live_request_provenance mitm 分支（HTTP POST 模型端点＋WS response.create）",
        "by_endpoint": dict(sorted(by_transport.items())),
        **({"errors": errors} if errors else {}),
    }


def _run_codex_tool(run: BoundaryRun, plan: CodexPlan, label: str, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """零请求的 Codex 判定工具（编目、收口、派生、逐规则断言）：以命令行调用、不导入，输出落 ``codex/logs/<label>.log``。"""

    logs = run.run_root / CODEX_DIRNAME / "logs"
    capture._private_directory(logs, run.run_root)
    remaining = float(getattr(run.deadline, "remaining_seconds", CODEX_PIPELINE_TIMEOUT_SECONDS))
    try:
        completed = subprocess.run(
            [sys.executable, *[str(item) for item in argv]],
            cwd=str(plan.repo_root),
            capture_output=True,
            text=True,
            timeout=max(1.0, min(float(CODEX_PIPELINE_TIMEOUT_SECONDS), remaining)),
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired as error:
        raise ConfigurationError(f"Codex 判定工具 {label} 超时。") from error
    claude_upgrade._secure_write_json_once(
        logs / f"{label}.log.json",
        {"argv": [str(item) for item in argv], "return_code": completed.returncode, "stdout": completed.stdout[-20000:], "stderr": completed.stderr[-20000:]},
    )
    return completed


def _selected_count(actual: Any) -> int | None:
    """一条检查选中的观测数（取自 checker 的 actual）；形态不认识时返回 None（按已判处理，必须通过）。"""

    if not isinstance(actual, Mapping):
        return None
    if isinstance(actual.get("matched_count"), int):
        return int(actual["matched_count"])
    for key in ("values", "distinct_values", "pairs"):
        if isinstance(actual.get(key), list):
            return len(actual[key])
    if isinstance(actual.get("groups"), list):
        return sum(int(group.get("record_count", 0)) for group in actual["groups"] if isinstance(group, Mapping)) + len(
            actual.get("invalid_record_ids") or []
        )
    return None


def _classify_rule(document: Mapping[str, Any], rule: Mapping[str, Any]) -> dict[str, Any]:
    """一条规则的逐条检查分类：已判（场景全在本次证据里且选中了观测）必须通过；其余记未覆盖、不判。"""

    checks = [check for check in document.get("checks") or [] if isinstance(check, Mapping)]
    profile_checks = {str(check.get("id")): check for check in rule.get("checks") or [] if isinstance(check, Mapping)}
    judged: list[str] = []
    failed: list[str] = []
    uncovered: list[dict[str, str]] = []
    covered: set[str] = set()
    for check in checks:
        if check.get("id") == COVERAGE_CHECK_ID:
            expected = check.get("expected") if isinstance(check.get("expected"), Mapping) else {}
            actual = check.get("actual") if isinstance(check.get("actual"), Mapping) else {}
            covered = {str(scenario) for scenario, kinds in expected.items() if set(kinds or []) <= set(actual.get(scenario) or [])}
    for check in checks:
        check_id = str(check.get("id"))
        if check_id == COVERAGE_CHECK_ID:
            uncovered.append({"check_id": check_id, "reason": "scenario-coverage-not-judged"})
            continue
        if check_id == INPUT_VALIDATION_CHECK_ID:
            judged.append(check_id)
            failed.append(check_id)
            continue
        profile = profile_checks.get(check_id)
        if profile is None:
            judged.append(check_id)
            failed.append(check_id)
            continue
        select = profile.get("select") if isinstance(profile.get("select"), Mapping) else {}
        scenarios = set(select.get("scenario_ids") or rule.get("scenario_ids") or [])
        selected = _selected_count(check.get("actual"))
        if not scenarios <= covered:
            uncovered.append({"check_id": check_id, "reason": "scenario-not-captured"})
        elif selected == 0:
            uncovered.append({"check_id": check_id, "reason": "no-observation-selected"})
        else:
            judged.append(check_id)
            if check.get("passed") is not True:
                failed.append(check_id)
    return {
        "rule_id": document.get("rule_id"),
        "checker_status": document.get("status"),
        "covered_scenarios": sorted(covered),
        "judged": judged,
        "failed": failed,
        "uncovered": uncovered,
    }


def _finalize_capture_manifest(draft_path: Path, bundle: Path, *, version: str, capture_id: str) -> Path:
    """与 Codex 编目器 finalize_manifest 同一算法（回填 sha256、包外壳）：不导入 Codex 模块，checker 再逐项校验。"""

    draft = claude_upgrade._read_json(draft_path, "Codex manifest 草稿")
    artifacts = []
    for artifact in draft.get("artifacts") or []:
        path = bundle / str(artifact["path"])
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"Codex manifest 引用的产物不存在：{artifact['path']}")
        artifacts.append({**artifact, "sha256": file_sha256(path)})
    manifest = {
        "schema_version": CODEX_CAPTURE_MANIFEST_SCHEMA,
        "codex_version": version,
        "capture_id": capture_id,
        "status": "complete",
        "artifacts": artifacts,
    }
    target = bundle / "capture-manifest.json"
    claude_upgrade._secure_write_json_once(target, manifest)
    return target


def _codex_runtime(run: BoundaryRun, plan: CodexPlan, complete: Sequence[Path]) -> tuple[list[str], dict[str, Any]]:
    """runtime 节的判定：Codex 断言管线（编目→收口→派生→回填→重放）＋逐规则 checker，按选中观测分类。"""

    version = plan.active["version"]
    tools = plan.tools
    codex_dir = run.run_root / CODEX_DIRNAME
    capture._private_directory(codex_dir, run.run_root)
    catalog_dir, bundle = codex_dir / "catalog", codex_dir / "assertion-bundle"
    failures: list[str] = []
    job_roots = [f"{CODEX_JOB_ID}={root.name}={root}" for root in complete]
    source_roots = [f"{root.name}={root}" for root in complete]
    steps = [
        (
            "catalog",
            [tools["build_evidence_catalog.py"], "--declaration", tools["labels"], "--expected-codex-version", version, "--side", "candidate",
             *[item for root in job_roots for item in ("--job-root", root)], "--output-dir", catalog_dir],
        ),
        (
            "bundle",
            [tools["build_assertion_bundle.py"], *[item for root in source_roots for item in ("--source-root", root)],
             "--plan", catalog_dir / "bundle-plan.json", "--bundle-dir", bundle],
        ),
    ]
    for label, argv in steps:
        if _run_codex_tool(run, plan, label, argv).returncode != 0:
            return [f"codex-pipeline-{label}-failed"], {}
    derivation = claude_upgrade._read_json(catalog_dir / "derivation-plan.json", "Codex 派生清单")
    if derivation.get("entries"):
        if _run_codex_tool(run, plan, "derive", [tools["derive_official_observations.py"], "--bundle-dir", bundle, "--plan", catalog_dir / "derivation-plan.json"]).returncode != 0:
            return ["codex-pipeline-derive-failed"], {}
    manifest_path = _finalize_capture_manifest(catalog_dir / "manifest-draft.json", bundle, version=version, capture_id=run.run_id)
    verify = [tools["build_assertion_bundle.py"], *[item for root in source_roots for item in ("--source-root", root)], "--bundle-dir", bundle,
              "--verify", "--allow-extra", "derived/", "--allow-extra", "capture-manifest.json"]
    if _run_codex_tool(run, plan, "bundle-verify", verify).returncode != 0:
        return ["codex-pipeline-bundle-verify-failed"], {}
    run.bind("capture_manifest", manifest_path)
    expectations = claude_upgrade._read_json(tools["expectations"], "Codex 断言画像")
    if expectations.get("codex_version") != version or expectations.get("status", "approved") != "approved":
        return ["codex-expectations-version"], {}
    rules = {str(rule.get("rule_id")): rule for rule in expectations.get("rules") or [] if isinstance(rule, Mapping)}
    assertions = codex_dir / "assertions"
    capture._private_directory(assertions, run.run_root)
    expectations_sha256 = file_sha256(tools["expectations"])
    results: list[dict[str, Any]] = []
    for rule_id in sorted(str(item) for item in plan.job["covers"]):
        if rule_id not in rules:
            failures.append(f"codex-rule-not-in-expectations:{rule_id}")
            continue
        output = assertions / f"{rule_id}.json"
        completed = _run_codex_tool(
            run,
            plan,
            f"assert-{rule_id}",
            [tools["candidate_rule_assertion.py"], "--rule-id", rule_id, "--capture-manifest", manifest_path, "--evidence-root", bundle,
             "--profile", tools["expectations"], "--rule-manifest", tools["rules"], "--expected-codex-version", version,
             "--expected-profile-sha256", expectations_sha256, "--side", "candidate", "--output", output],
        )
        if completed.returncode not in (0, 1) or output.is_symlink() or not output.is_file():
            failures.append(f"codex-checker-crashed:{rule_id}")
            continue
        document = claude_upgrade._read_json(output, "Codex 单规则断言")
        if document.get("rule_id") != rule_id:
            failures.append(f"codex-checker-output:{rule_id}")
            continue
        run.bind(f"rule_assertion:{rule_id}", output)
        row = _classify_rule(document, rules[rule_id])
        results.append(row)
        failures.extend(f"codex-check-failed:{rule_id}:{check}" for check in row["failed"])
    judged = sum(len(row["judged"]) for row in results)
    if judged == 0:
        failures.append("codex-runtime-nothing-judged")
    summary = {
        "rules": results,
        "judged_check_count": judged,
        "uncovered_check_count": sum(len(row["uncovered"]) for row in results),
        "failed_checks": [f"{row['rule_id']}:{check}" for row in results for check in row["failed"]],
        "expectations_sha256": expectations_sha256,
    }
    return failures, summary


def _codex_execute(run: BoundaryRun) -> dict[str, Any]:
    plan: CodexPlan = run.codex
    zero = {"count": 0, "exact": True, "counting_rule": "structural-only-no-capture", "by_endpoint": {}}
    # —— structural（零请求，必过；不过即不发任何真实请求） ——
    structural = _structural_report(run)
    structural_path = run.run_root / STRUCTURAL_REPORT_FILENAME
    claude_upgrade._secure_write_json_once(structural_path, structural)
    run.bind("structural_report", structural_path)
    differences = {name: group["differences"] for name, group in structural["groups"].items() if group["differences"]}
    facts: dict[str, Any] = {
        "codex_version": plan.active["version"],
        "codex_snapshot_digest": plan.active["snapshot_digest"],
        "production_commit": structural["production_commit"],
        "structural": {name: {"compared": group["compared"], "differences": len(group["differences"])} for name, group in structural["groups"].items()},
    }
    if differences:
        facts["structural_differences"] = differences
        return {"failure_codes": ["structural-difference"], "contaminated": False, "facts": facts, "live_requests": zero}
    # —— runtime（candidate-core-mitm，真实请求） ——
    before = _codex_account_facts(run.manifest, run.arguments.codex_account_id, run.arguments.codex_api_key_id, deadline=run.deadline)
    failures = _codex_account_failures(before)
    if failures:
        return {"failure_codes": failures, "contaminated": False, "facts": facts, "live_requests": zero}
    log_path = run.run_root.joinpath(*STEP_LOG_RELATIVE.parts)
    capture._private_directory(log_path.parent, run.run_root)
    environment = {**os.environ, **plan.environment}
    with log_path.open("w", encoding="utf-8") as log:
        os.chmod(log_path, 0o600)
        return_code = capture._run_step_command(
            plan.argv,
            operation=f"job:{run.job_id}:step-1:attempt-1",
            timeout_seconds=float(plan.job["steps"][0].get("timeout_seconds", 3600)),
            environment=environment,
            log_stream=log,
            job_id=run.job_id,
            deadline=run.deadline,
            heartbeat=run.heartbeat,
        )
    run.bind("step_log", log_path)
    complete, failed_roots = _codex_roots(plan, run.layout)
    live = _codex_request_count(complete, failed_roots)
    for root in [*complete, *failed_roots]:
        relative = root.resolve(strict=True).relative_to(Path(run.layout.host_data_root).resolve(strict=True)).as_posix()
        run.evidence.append(
            {"role": f"codex_evidence_root:{root.name}", "base": "data_root", "path": relative, "tree_sha256": claude_upgrade._directory_tree_digest(root)}
        )
    after = _codex_account_facts(run.manifest, run.arguments.codex_account_id, run.arguments.codex_api_key_id)
    codes: list[str] = []
    contaminated = False
    if return_code == RESTORATION_FAILED_EXIT_CODE:
        codes.append("codex-environment-restoration-failed")
        contaminated = True
    elif return_code != 0:
        codes.append(f"codex-capture-exit-{return_code}")
    if after != before:
        codes.append("codex-account-drifted")
        contaminated = True
    if live.get("exact") is not True:
        codes.append("codex-request-count-unresolved")
    if failed_roots:
        codes.append("codex-capture-failed-coordinates")
    if len(complete) != plan.expected_roots:
        codes.append(f"codex-evidence-roots-{len(complete)}-of-{plan.expected_roots}")
    facts.update(
        {
            "codex_capture_return_code": return_code,
            "codex_evidence_roots": [root.name for root in complete],
            "codex_failed_roots": [root.name for root in failed_roots],
            "codex_account_before": before,
            "codex_account_unchanged": after == before,
        }
    )
    runtime: dict[str, Any] = {}
    if not codes:
        runtime_codes, runtime = _codex_runtime(run, plan, complete)
        codes.extend(runtime_codes)
    report = {
        "schema_version": RUNTIME_REPORT_SCHEMA,
        "conclusion_scope": CONCLUSION_SCOPES[CHECK_CODEX],
        "codex_active": plan.active,
        "job_id": CODEX_JOB_ID,
        "covers": sorted(str(item) for item in plan.job["covers"]),
        "codex_tools": {name: file_sha256(path) for name, path in sorted(plan.tools.items())},
        "capture_root": str(plan.capture_root),
        "environment": {key: value for key, value in sorted(plan.environment.items())},
        "live_requests": {key: value for key, value in live.items() if key != "errors"},
        "failure_codes": sorted(set(codes)),
        **facts,
        **runtime,
        "recorded_at_utc": claude_upgrade._utc_now(),
    }
    report_path = run.run_root.joinpath(*RUNTIME_REPORT_RELATIVE.parts)
    capture._private_directory(report_path.parent, run.run_root)
    claude_upgrade._secure_write_json_once(report_path, report)
    run.bind("runtime_report", report_path)
    facts.update(
        {
            "judged_check_count": runtime.get("judged_check_count", 0),
            "uncovered_check_count": runtime.get("uncovered_check_count", 0),
            "failed_checks": runtime.get("failed_checks", []),
        }
    )
    return {
        "failure_codes": codes,
        "contaminated": contaminated,
        "facts": facts,
        "live_requests": {key: value for key, value in live.items() if key != "errors"},
    }


CHECKS: dict[str, CheckSpec] = {
    CHECK_THIRD_PARTY: CheckSpec(
        check_id=CHECK_THIRD_PARTY,
        live_requests=False,
        validate_arguments=_no_extra_arguments,
        preflight=_third_party_preflight,
        execute=_third_party_execute,
    ),
    CHECK_CODEX: CheckSpec(
        check_id=CHECK_CODEX,
        live_requests=True,
        validate_arguments=_codex_arguments,
        preflight=_codex_preflight,
        execute=_codex_execute,
    ),
}


def add_boundary_check_arguments(target: argparse.ArgumentParser) -> None:
    """``capture-candidate boundary-check`` 的参数（``--candidate-id``、``--attempt-id`` 与 ``--acknowledge-live-requests``、
    ``--max-wall-seconds``、``--heartbeat-seconds`` 与 run／seal 共用，已由它们登记）。"""

    target.add_argument("--check", choices=CHECK_IDS, help="boundary-check：要执行的 attempt 级边界核对。")
    target.add_argument(
        "--codex-account-id",
        type=int,
        default=None,
        help=f"boundary-check {CHECK_CODEX}：本轮 Codex 专用 OpenAI 账号 ID（轮次参数，不写死）。",
    )
    target.add_argument(
        "--codex-api-key-id",
        type=int,
        default=None,
        help=f"boundary-check {CHECK_CODEX}：该 Codex 账号所在单账号分组的网关 API Key ID（轮次参数）。",
    )
    target.add_argument(
        "--codex-model",
        default=None,
        help=f"boundary-check {CHECK_CODEX}：本轮 Codex 主轨模型（轮次参数，例如 Codex 升级 Campaign 的 --model）。",
    )
    target.add_argument(
        "--production-commit",
        default=None,
        help=f"boundary-check {CHECK_CODEX}：当前生产部署的源码提交（40 位十六进制），structural 节与候选源码树逐字节比较。",
    )
