"""测试夹具：在 Claude Campaign 所在的宿主数据根建 Claude 项目总账并注册该 Campaign（Claude 升级控制面设计 B2-08）。

Claude 的 Formal Campaign 一律在项目总账内执行（``claude_upgrade_client_adapter.requires_project_ledger``）；B2-08 起
``capture-official run``（不在 campaign-run 父监督器里时）、``capture-official seal`` 与 ``resume`` 都先过总账消费者门禁。
离线夹具用本 helper 在 ``<数据根>/upgrade-project-ledger-claude`` 放一本宽松总账（截止在 Campaign 总计划截止之后、无请求
预算），Campaign 仍走真实注册与消费者门禁。缺省建非 fixture_only 总账（fixture_only 总账要求位于 staging 树内，本机临时
目录不满足），因此对账要求当前工具身份有通过的 Claude 受管部署收据（``write_claude_deploy_receipt``）；真实链外壳（B5a-05）
设置 ``project_ledger_fixture.FIXTURE_ONLY_ENV`` 为 "1"、临时根带 staging 段时，改建 fixture_only 总账（与 Codex 夹具同一
开关，A2.5 路径认证要求夹具总账一律 fixture_only）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture.tests.project_ledger_fixture import FIXTURE_ONLY_ENV

PROJECT_DEADLINE_UTC = "2027-01-31T00:00:00Z"
DEPLOY_RECEIPT_SCHEMA = "official-client-managed-deploy/v1"


def data_root_of(campaign_dir: Path) -> Path:
    """规范坐标 ``<数据根>/evidence/campaigns/<id>`` 的数据根。"""

    return Path(campaign_dir).resolve().parents[2]


def install_claude_project_ledger(data_root: Path) -> Path:
    """建（或复用）数据根下的 Claude 项目总账；数据根与 evidence 目录收紧为 0700（总账要求父目录私有）。"""

    data_root = Path(data_root)
    for directory in (data_root, data_root / "evidence", data_root / "evidence" / "campaigns"):
        if directory.is_dir():
            directory.chmod(0o700)
    root = data_root / project_ledger.ledger_dir_name("claude")
    if not (root / "plan.json").is_file():
        project_ledger.create_project_ledger(
            root,
            project_id="claude-fixture-project",
            absolute_deadline_utc=PROJECT_DEADLINE_UTC,
            deadline_approved_by="fixture",
            estimation_policy="none",
            estimation_policy_approved_by="fixture",
            fixture_only=os.environ.get(FIXTURE_ONLY_ENV) == "1",
            formal_open_limit=64,
            client="claude",
        )
    return root


def register_claude_campaign(campaign_dir: Path) -> Path:
    """在 Campaign 的数据根建总账并注册该 Campaign（注册幂等），返回总账根。"""

    root = install_claude_project_ledger(data_root_of(campaign_dir))
    project_ledger.register_existing_campaign(Path(campaign_dir))
    return root


def write_claude_deploy_receipt(data_root: Path, identity: Mapping[str, Any], *, stamp: str, created_at_utc: str | None = None) -> Path:
    """在 ``<数据根>/control`` 写一份通过的 Claude 受管部署收据（五摘要在 ``identity`` 子键，形状同 B5a-01）。"""

    control = Path(data_root) / "control"
    control.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "schema_version": DEPLOY_RECEIPT_SCHEMA,
        "status": "passed",
        "client": "claude",
        "created_at_utc": created_at_utc or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "identity": {
            "policy_version": identity["policy_version"],
            "policy_sha256": identity["policy_sha256"],
            "wire_producer_sha256": identity["wire_producer_sha256"],
            "evidence_semantics_sha256": identity["evidence_semantics_sha256"],
            "control_sha256": identity["control_sha256"],
            "tool_files_sha256": identity["files_sha256"],
        },
    }
    path = control / f"claude-managed-deploy-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path
