"""测试夹具：为任意临时根安装一个已批准的项目总账，让 0.154 formal 夹具通过 admission。

生产上项目总账由 A0b-6 创建并冻结老板批准的截止时间与估计政策；离线夹具用本
helper 在 Campaign 目录的祖先处放一个宽松总账（截止 48 小时后、无请求预算、
非 fixture_only 以免限制路径），Campaign 仍要走真实注册与消费者门禁。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger

# A2.5 路径认证把全部夹具总账切成 fixture_only：设置该环境变量为 "1" 即可。
FIXTURE_ONLY_ENV = "CODEX_FIXTURE_LEDGER_FIXTURE_ONLY"


def install_fixture_ledger(root: Path, *, fixture_only: bool = False, hours: int = 48) -> Path:
    """在 ``root`` 下创建 ``upgrade-project-ledger``；已存在则原样返回。"""

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    ledger_root = root / project_ledger.LEDGER_DIR_NAME
    if (ledger_root / "plan.json").is_file():
        return ledger_root
    if os.environ.get(FIXTURE_ONLY_ENV) == "1":
        fixture_only = True
    deadline = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    project_ledger.create_project_ledger(
        ledger_root,
        project_id="fixture-project",
        absolute_deadline_utc=deadline,
        deadline_approved_by="fixture",
        estimation_policy="upper_bound_from_sibling_or_turn_ratio",
        estimation_policy_approved_by="fixture",
        fixture_only=fixture_only,
        formal_open_limit=64,
    )
    return ledger_root


def register_fixture_campaign(campaign_dir: Path) -> dict:
    """把夹具里已写好清单的 Campaign 注册进祖先总账。"""

    return project_ledger.register_existing_campaign(Path(campaign_dir))
