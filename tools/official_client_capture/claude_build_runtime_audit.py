#!/usr/bin/env python3
"""把运行中 Sub2API 写出的 Claude 画像激活事实组装成 Claude 运行画像审计文档（Claude 升级控制面设计 B4-03）。

对应 Codex 的 ``build_observed_profile_runtime_audit.py``（那份在 Claude 身份策略里被忽略，这里不导入，按 Claude 事实合同改写）。
画像事实只能来自运行中的服务：Go 侧 ``resolveClaudeOfficialEgressActivationFact``
（``backend/internal/service/claude_official_egress_activation_fact.go``）在启动装配后按当前 Claude Runtime 解析画像，
把部署者声明的 profile／release／approval 摘要与自己解析出的值逐字比对，不一致就拒绝落盘。本工具不产生任何画像
结论，只做三件事：

1. 校验事实文档：键集与 Go 结构体逐字一致、schema／来源／事件类型、``event_id`` 按 Go 口径复算一致（防止事实被手改
   而 event_id 未跟着变）、观测时刻是 RFC3339 UTC；
2. 只接受调用方要审计的那种 Runtime 写出的事实：
   * ``active``（正式 Runtime，生产激活与 B4-08 生产分支用）：版本与五项画像链摘要齐全，批准摘要是正式 Runtime 启动时
     复算的 ProductionApproval；
   * ``validation_candidate``（候选 Runtime，VC-5 候选验收用，B4-06a 起 Go 侧补写）：版本与 profile／wire／release／bundle
     四项摘要齐全，批准摘要恒为空——候选没有生产批准，声明了批准摘要一律拒绝；
   * ``legacy`` 事实不含任何 Claude 摘要，不能证明画像生效，一律拒绝；
3. 声明身份逐字比对（版本、画像链摘要、镜像与构建身份），再补上 attempt 坐标（campaign／attempt／run_nonce／
   candidate），这些坐标是采集侧自身的权威事实，服务在启动时无从得知。

候选事实与正式事实同一 schema（``claude-egress-activation-fact/v1``）、同一字段集合与 ``event_id`` 算法，只扩展
``profile_mode`` 的取值；审计文档也沿用 ``claude-egress-runtime-audit/v1``，``profile_mode`` 记录审计的是哪种 Runtime，
候选审计的 ``approval_digest`` 为空串。

本文件在 Claude 身份策略里归 evidence 层（整文件计入）；Codex 身份策略忽略它。只依赖标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ACTIVATION_SCHEMA = "claude-egress-activation-fact/v1"
RUNTIME_AUDIT_SCHEMA = "claude-egress-runtime-audit/v1"
ACTIVATION_SOURCE = "sub2api-runtime"
ACTIVATION_EVENT = "claude_profile_observed"
PROFILE_MODE_ACTIVE = "active"
PROFILE_MODE_CANDIDATE = "validation_candidate"
PROFILE_MODE_LEGACY = "legacy"
PROFILE_MODES = frozenset({PROFILE_MODE_LEGACY, PROFILE_MODE_ACTIVE, PROFILE_MODE_CANDIDATE})
# 可审计的 Runtime：legacy 不加载 Claude 画像，不在其中。
AUDITED_PROFILE_MODES = (PROFILE_MODE_ACTIVE, PROFILE_MODE_CANDIDATE)

# 与 Go 结构体 ClaudeOfficialEgressActivationFact 的字段顺序逐字一致：event_id 是该结构体（event_id 置空）经 json.Marshal
# 紧凑序列化后的 SHA-256，复算必须按这个顺序。
FACT_FIELDS = (
    "schema_version",
    "source",
    "event_type",
    "event_id",
    "observed_at_utc",
    "profile_mode",
    "claude_version",
    "profile_digest",
    "wire_digest",
    "release_digest",
    "bundle_digest",
    "approval_digest",
    "image_id",
    "image_reference",
    "source_tree_sha256",
    "build_id",
    "deployed_version",
)
# 画像链五项摘要：Go 在正式 Runtime 生效时全部填写；候选 Runtime 填前四项，批准摘要恒为空。
DIGEST_FIELDS = ("profile_digest", "wire_digest", "release_digest", "bundle_digest", "approval_digest")
CANDIDATE_DIGEST_FIELDS = DIGEST_FIELDS[:-1]
# 部署身份：由部署注入的环境变量原样带入事实。
DEPLOYMENT_FIELDS = ("image_id", "image_reference", "source_tree_sha256", "build_id", "deployed_version")

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# Go 的 time.RFC3339Nano UTC 形态：秒后可带 1～9 位小数（末尾零被裁掉），时区固定 Z。
RFC3339_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$")
# Go encoding/json 默认 HTML 转义与 JS 行分隔符转义；Python 的 json.dumps 不做，复算时补上。
GO_JSON_ESCAPES = (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029"))


class RuntimeAuditError(RuntimeError):
    """组装失败一律终止，绝不产出半成品审计文档。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeAuditError(message)


def load_fact(path: Path) -> dict[str, Any]:
    """读取激活事实：必须是非符号链接普通文件、合法 JSON 对象，且不含重复键。"""

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeAuditError("画像激活事实必须是非符号链接普通文件")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeAuditError(f"画像激活事实含重复键：{key}")
            result[key] = value
        return result

    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, ValueError) as error:
        raise RuntimeAuditError("画像激活事实不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeAuditError("画像激活事实必须是 JSON 对象")
    return payload


def go_marshal_compact(fields: Mapping[str, str]) -> bytes:
    """按 Go ``json.Marshal`` 口径紧凑序列化一组字符串字段（保持给定顺序、无空白、HTML 字符转义）。"""

    text = json.dumps(dict(fields), ensure_ascii=False, separators=(",", ":"))
    for raw, escaped in GO_JSON_ESCAPES:
        text = text.replace(raw, escaped)
    return text.encode("utf-8")


def activation_event_id(fact: Mapping[str, Any]) -> str:
    """复算 Go ``claudeOfficialEgressActivationEventID``：event_id 置空后按结构体字段顺序紧凑序列化取 SHA-256。"""

    ordered = {field: ("" if field == "event_id" else str(fact[field])) for field in FACT_FIELDS}
    return hashlib.sha256(go_marshal_compact(ordered)).hexdigest()


def validate_fact(fact: Mapping[str, Any]) -> dict[str, str]:
    """校验激活事实本身（不涉及声明身份），返回逐字段字符串化的副本。"""

    _require(isinstance(fact, Mapping), "画像激活事实必须是 JSON 对象")
    keys = set(fact)
    missing = sorted(set(FACT_FIELDS) - keys)
    extra = sorted(keys - set(FACT_FIELDS))
    _require(not missing and not extra, f"画像激活事实键集与 {ACTIVATION_SCHEMA} 不一致：缺 {missing}，多 {extra}")
    for field in FACT_FIELDS:
        _require(isinstance(fact[field], str), f"画像激活事实字段 {field} 必须是字符串")
    _require(fact["schema_version"] == ACTIVATION_SCHEMA, "画像激活事实 schema 不匹配")
    _require(fact["source"] == ACTIVATION_SOURCE, "画像激活事实来源不是运行中服务")
    _require(fact["event_type"] == ACTIVATION_EVENT, "画像激活事实事件类型不匹配")
    _require(fact["profile_mode"] in PROFILE_MODES, f"画像激活事实 profile_mode 非法：{fact['profile_mode']}")
    _require(bool(SHA256_RE.fullmatch(fact["event_id"])), "画像激活事实缺少内容寻址 event_id")
    _require(activation_event_id(fact) == fact["event_id"], "画像激活事实的 event_id 与内容复算不一致（事实被改动或不是服务写出的原件）")
    observed = fact["observed_at_utc"]
    _require(bool(RFC3339_UTC_RE.fullmatch(observed)), "画像激活事实观测时刻不是 RFC3339 UTC")
    try:
        datetime.strptime(observed[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise RuntimeAuditError("画像激活事实观测时刻不是合法日期时间") from error
    if fact["profile_mode"] in AUDITED_PROFILE_MODES:
        mode = fact["profile_mode"]
        required = DIGEST_FIELDS if mode == PROFILE_MODE_ACTIVE else CANDIDATE_DIGEST_FIELDS
        _require(bool(VERSION_RE.fullmatch(fact["claude_version"])), f"{mode} 事实的 claude_version 非法")
        for field in required:
            _require(bool(SHA256_RE.fullmatch(fact[field])), f"{mode} 事实的 {field} 必须是 SHA-256")
        if mode == PROFILE_MODE_CANDIDATE:
            _require(fact["approval_digest"] == "", "validation_candidate 事实不应携带 approval_digest（候选没有生产批准）")
    else:
        for field in ("claude_version", *DIGEST_FIELDS):
            _require(fact[field] == "", f"legacy 事实不应携带 {field}")
    return {field: str(fact[field]) for field in FACT_FIELDS}


def build_runtime_audit(
    fact: Mapping[str, Any],
    *,
    campaign_id: str,
    attempt_id: str,
    run_nonce: str,
    candidate_id: str,
    target_version: str,
    profile_digest: str,
    wire_digest: str,
    release_digest: str,
    bundle_digest: str,
    approval_digest: str,
    image_id: str,
    image_reference: str,
    source_tree_sha256: str,
    build_id: str,
    deployed_version: str,
    profile_mode: str = PROFILE_MODE_ACTIVE,
) -> dict[str, Any]:
    """校验事实并与声明身份逐字比对，产出 ``claude-egress-runtime-audit/v1``。

    ``profile_mode`` 是要审计的 Runtime：``active``（缺省，正式 Runtime）或 ``validation_candidate``（VC-5 候选验收）。
    候选审计的 ``approval_digest`` 必须声明为空串。
    """

    _require(profile_mode in AUDITED_PROFILE_MODES, f"只能审计 {'／'.join(AUDITED_PROFILE_MODES)} Runtime，收到 {profile_mode}")
    checked = validate_fact(fact)
    if checked["profile_mode"] == PROFILE_MODE_LEGACY:
        raise RuntimeAuditError(f"画像激活事实不是 {profile_mode}：legacy 模式不加载 Claude 画像，不能证明画像生效")
    _require(
        checked["profile_mode"] == profile_mode,
        f"画像激活事实来自 {checked['profile_mode']} Runtime，不是要审计的 {profile_mode} Runtime",
    )
    for name, value in (("campaign_id", campaign_id), ("attempt_id", attempt_id), ("candidate_id", candidate_id)):
        _require(bool(SAFE_ID_RE.fullmatch(str(value))), f"{name} 格式非法")
    _require(isinstance(run_nonce, str) and bool(run_nonce.strip()), "run_nonce 不能为空")
    _require(bool(VERSION_RE.fullmatch(str(target_version))), "target_version 格式非法")
    declared_digests = {
        "profile_digest": profile_digest,
        "wire_digest": wire_digest,
        "release_digest": release_digest,
        "bundle_digest": bundle_digest,
        "approval_digest": approval_digest,
    }
    for field, value in declared_digests.items():
        if profile_mode == PROFILE_MODE_CANDIDATE and field == "approval_digest":
            _require(value == "", "候选审计的 approval_digest 必须声明为空（候选没有生产批准）")
            continue
        _require(bool(SHA256_RE.fullmatch(str(value))), f"声明的 {field} 必须是 SHA-256")
    # 服务解析出的画像必须与声明身份逐字一致，否则说明跑的不是这份画像。
    _require(checked["claude_version"] == target_version, "运行时画像版本与目标版本不一致")
    for field, expected in declared_digests.items():
        _require(checked[field] == expected, f"运行时 {field} 与声明身份不一致")
    declared_deployment = {
        "image_id": image_id,
        "image_reference": image_reference,
        "source_tree_sha256": source_tree_sha256,
        "build_id": build_id,
        "deployed_version": deployed_version,
    }
    for field, expected in declared_deployment.items():
        _require(isinstance(expected, str) and bool(expected.strip()), f"声明的 {field} 不能为空")
        _require(checked[field] == expected, f"运行时事实的 {field} 与声明身份不一致；请确认部署已注入该身份")

    return {
        "schema_version": RUNTIME_AUDIT_SCHEMA,
        "source": ACTIVATION_SOURCE,
        "event_type": ACTIVATION_EVENT,
        "event_id": checked["event_id"],
        "profile_mode": checked["profile_mode"],
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "run_nonce": run_nonce,
        "candidate_id": candidate_id,
        "target_version": target_version,
        **declared_digests,
        **declared_deployment,
        "observed_at_utc": checked["observed_at_utc"],
    }


def _write_once(path: Path, payload: bytes) -> None:
    """write-once 写出 0600 文件；目标已存在（含符号链接）即拒绝。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise RuntimeAuditError("--output 必须是不存在的路径，拒绝覆盖既有审计文档") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 Claude 画像激活事实组装成运行画像审计文档")
    parser.add_argument("--activation-fact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-mode", choices=AUDITED_PROFILE_MODES, default=PROFILE_MODE_ACTIVE)
    # active 审计必须给批准摘要；候选审计不给（或给空串）。
    parser.add_argument("--approval-digest", default=None)
    for name in (
        "campaign-id",
        "attempt-id",
        "run-nonce",
        "candidate-id",
        "target-version",
        "profile-digest",
        "wire-digest",
        "release-digest",
        "bundle-digest",
        "image-id",
        "image-reference",
        "source-tree-sha256",
        "build-id",
        "deployed-version",
    ):
        parser.add_argument(f"--{name}", required=True)
    arguments = parser.parse_args(argv)

    if arguments.profile_mode == PROFILE_MODE_ACTIVE and arguments.approval_digest is None:
        raise RuntimeAuditError("active 审计必须提供 --approval-digest")
    if arguments.output.exists() or arguments.output.is_symlink():
        raise RuntimeAuditError("--output 必须是不存在的路径，拒绝覆盖既有审计文档")
    document = build_runtime_audit(
        load_fact(arguments.activation_fact),
        campaign_id=arguments.campaign_id,
        attempt_id=arguments.attempt_id,
        run_nonce=arguments.run_nonce,
        candidate_id=arguments.candidate_id,
        target_version=arguments.target_version,
        profile_digest=arguments.profile_digest,
        wire_digest=arguments.wire_digest,
        release_digest=arguments.release_digest,
        bundle_digest=arguments.bundle_digest,
        approval_digest=arguments.approval_digest or "",
        image_id=arguments.image_id,
        image_reference=arguments.image_reference,
        source_tree_sha256=arguments.source_tree_sha256,
        build_id=arguments.build_id,
        deployed_version=arguments.deployed_version,
        profile_mode=arguments.profile_mode,
    )
    _write_once(arguments.output, (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps({"output": str(arguments.output), "event_id": document["event_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeAuditError as error:
        print(f"Claude 运行画像审计组装失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error
