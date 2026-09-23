#!/usr/bin/env python3
"""Python 驱动与 shell 共用同一份闭合参数及派生坐标。"""

import os
from pathlib import Path

from parse_env import derive, parse


def load_config() -> dict[str, str]:
    path = Path(os.environ["ARM64_VC_ENV"])
    if path.is_symlink() or not path.is_file():
        raise ValueError("ARM64_VC_ENV 必须指向本轮普通参数文件")
    values = parse(path.read_text())
    result = {**values, **derive(values)}
    os.environ.update(result)
    return result


def candidate_job_ids(campaign: Path, candidate_id: str) -> list[str]:
    """从 Campaign 已批准的场景读取完整候选集合，不裁剪采集范围。"""

    from tools.official_client_capture import codex_upgrade as upgrade
    manifest = upgrade.load_campaign_manifest(campaign)
    return sorted(job.job_id for job in upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=candidate_id))
