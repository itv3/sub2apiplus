#!/usr/bin/env python3
"""Python 驱动与 shell 共用同一份闭合参数及派生坐标。"""

import os
from pathlib import Path

from parse_env import client_table, derive, parse, stale_keys


def load_config(*, require_current: bool = False) -> dict[str, str]:
    path = Path(os.environ["ARM64_VC_ENV"])
    if path.is_symlink() or not path.is_file():
        raise ValueError("ARM64_VC_ENV 必须指向本轮普通参数文件")
    values = parse(path.read_text())
    result = {**values, **derive(values)}
    if require_current:
        from round_context import resolve
        result.update(resolve(result)["parameters"])
    # 与 parse_env.py 主程序同一清理集合（stale_keys）：没出现的可选键，以及另一客户端的专属键（B5a-06）。
    for key in stale_keys(result, client_table().client_of(values)):
        os.environ.pop(key, None)
    os.environ.update(result)
    return result


def candidate_job_ids(campaign: Path, candidate_id: str) -> list[str]:
    """从 Campaign 已批准的场景读取完整候选集合，不裁剪采集范围。"""

    from tools.official_client_capture import codex_upgrade as upgrade
    manifest = upgrade.load_campaign_manifest(campaign)
    return sorted(job.job_id for job in upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=candidate_id))
