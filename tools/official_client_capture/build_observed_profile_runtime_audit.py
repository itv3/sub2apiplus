#!/usr/bin/env python3
"""把运行中 Sub2API 产生的画像激活事实组装成候选运行画像审计文档。

画像事实（profile_id／profile_digest／release digest／观测时刻）只能来自运行中的服务：
服务在启动装配后按当前发布指针解析画像，并把声明身份中的 profile digest 与自己解析出的
digest 逐字比对，不一致就拒绝落盘。本工具不产生任何画像结论，只做两件事：

1. 校验事实文档的 schema、来源、事件类型与声明身份一致；
2. 补上 attempt 坐标（campaign／attempt／run_nonce／candidate），这些坐标是采集侧自身的
   权威事实，服务在启动时无从得知。

输出交给 codex_upgrade_receipt_finalizer.py observed-profile 生成正式收据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ACTIVATION_SCHEMA = "codex-egress-activation-fact/v1"
RUNTIME_AUDIT_SCHEMA = "codex-egress-runtime-audit/v1"
ACTIVATION_SOURCE = "sub2api-runtime"
ACTIVATION_EVENT = "profile_activated"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RuntimeAuditError(RuntimeError):
    """组装失败一律终止，绝不产出半成品收据。"""


def _load_fact(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeAuditError("画像激活事实必须是非符号链接普通文件")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeAuditError("画像激活事实不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeAuditError("画像激活事实必须是 JSON 对象")
    return payload


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeAuditError(message)


def build_runtime_audit(
    fact: dict[str, Any],
    *,
    campaign_id: str,
    attempt_id: str,
    run_nonce: str,
    candidate_id: str,
    target_version: str,
    profile_id: str,
    profile_digest: str,
    image_id: str,
    image_reference: str,
    source_tree_sha256: str,
    build_id: str,
    deployed_version: str,
) -> dict[str, Any]:
    _require(fact.get("schema_version") == ACTIVATION_SCHEMA, "画像激活事实 schema 不匹配")
    _require(fact.get("source") == ACTIVATION_SOURCE, "画像激活事实来源不是运行中服务")
    _require(fact.get("event_type") == ACTIVATION_EVENT, "画像激活事实事件类型不匹配")

    event_id = str(fact.get("event_id", ""))
    observed_at = str(fact.get("observed_at_utc", ""))
    _require(bool(SHA256_RE.fullmatch(event_id)), "画像激活事实缺少内容寻址 event_id")
    _require(bool(observed_at), "画像激活事实缺少观测时刻")

    # 服务解析出的画像必须与候选身份逐字一致，否则说明跑的不是这份候选画像。
    _require(
        str(fact.get("profile_digest", "")) == profile_digest,
        "运行时画像摘要与候选身份不一致",
    )
    _require(
        str(fact.get("codex_version", "")) == target_version,
        "运行时画像版本与目标版本不一致",
    )
    for field, expected in (
        ("profile_id", profile_id),
        ("image_id", image_id),
        ("image_reference", image_reference),
        ("source_tree_sha256", source_tree_sha256),
        ("build_id", build_id),
        ("deployed_version", deployed_version),
    ):
        declared = str(fact.get(field, ""))
        _require(
            declared == expected,
            f"运行时事实的 {field} 与候选身份不一致；请确认部署已注入该身份",
        )

    return {
        "schema_version": RUNTIME_AUDIT_SCHEMA,
        "source": ACTIVATION_SOURCE,
        "event_type": ACTIVATION_EVENT,
        "event_id": event_id,
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "run_nonce": run_nonce,
        "candidate_id": candidate_id,
        "target_version": target_version,
        "profile_id": profile_id,
        "profile_digest": profile_digest,
        "image_id": image_id,
        "image_reference": image_reference,
        "source_tree_sha256": source_tree_sha256,
        "build_id": build_id,
        "deployed_version": deployed_version,
        "observed_at_utc": observed_at,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation-fact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    for name in (
        "campaign-id",
        "attempt-id",
        "run-nonce",
        "candidate-id",
        "target-version",
        "profile-id",
        "profile-digest",
        "image-id",
        "image-reference",
        "source-tree-sha256",
        "build-id",
        "deployed-version",
    ):
        parser.add_argument(f"--{name}", required=True)
    arguments = parser.parse_args(argv)

    if arguments.output.exists() or arguments.output.is_symlink():
        raise RuntimeAuditError("--output 必须是不存在的路径，拒绝覆盖既有收据输入")
    if not SAFE_ID_RE.fullmatch(arguments.candidate_id):
        raise RuntimeAuditError("candidate-id 格式非法")

    document = build_runtime_audit(
        _load_fact(arguments.activation_fact),
        campaign_id=arguments.campaign_id,
        attempt_id=arguments.attempt_id,
        run_nonce=arguments.run_nonce,
        candidate_id=arguments.candidate_id,
        target_version=arguments.target_version,
        profile_id=arguments.profile_id,
        profile_digest=arguments.profile_digest,
        image_id=arguments.image_id,
        image_reference=arguments.image_reference,
        source_tree_sha256=arguments.source_tree_sha256,
        build_id=arguments.build_id,
        deployed_version=arguments.deployed_version,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(arguments.output, 0o600)
    print(json.dumps({"output": str(arguments.output), "event_id": document["event_id"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeAuditError as error:
        print(f"运行画像审计组装失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error
