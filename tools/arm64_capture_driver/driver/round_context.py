#!/usr/bin/env python3
"""只读解析本轮 Campaign、账本激活的 Candidate 与规范收据根。

参数中的 NEW 是显式选择，CAND 和旧 B 仅作预期断言。绝不按目录排序选择候选，
也不把未创建的轮次标成已验证；初始化与正式消费使用不同模式。VC-5／VC-6 的
具体收据重放仍由对应阶段合同负责，本模块不签发批准、不发送请求、不写状态。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

# 独立入口也须零写入；在导入同目录模块前关闭字节码生成，不依赖调用方传 -B。
sys.dont_write_bytecode = True

try:
    from .parse_env import coordinates, derive, parse, plain_path
except ImportError:
    from parse_env import coordinates, derive, parse, plain_path


def binding(path: Path) -> dict:
    plain_path(str(path), "权威输入")
    if not path.is_file():
        raise ValueError(f"权威输入缺失或不是普通文件：{path}")
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _managed_upgrade(data: Path):
    """独立入口只加载本轮受管根；不继承调用方预先导入的另一份工具。"""

    sys.path.insert(0, str(data))
    module = importlib.import_module("tools.official_client_capture.codex_upgrade")
    expected = data / "tools" / "official_client_capture" / "codex_upgrade.py"
    plain_path(str(expected), "受管解析器")
    if Path(module.__file__).resolve() != expected:
        raise ValueError("Campaign 解析器不属于本轮 D 的受管工具")
    return module


def resolve(config: dict[str, str], *, mode: str = "candidate", upgrade=None) -> dict:
    """返回本次重放报告；upgrade 仅供进程内复用既有受管模块或隔离测试注入。"""

    if mode not in {"init", "campaign", "candidate"}:
        raise ValueError("根解析模式必须是 init、campaign 或 candidate")
    roots = coordinates(config)
    campaign = Path(roots["NEWDIR"])
    report = {"schema_version": "arm64-round-context/v1", "state": "initialization_coordinates",
              "campaign_id": config["NEW"], "candidate_id": config["CAND"],
              "candidate_revision": None, "parameters": roots, "bindings": {}}
    if mode == "init":
        if campaign.exists():
            raise ValueError("Campaign 已存在，必须按正式模式重放，不能降级初始化")
        return report
    if not campaign.is_dir():
        raise ValueError("当前 Campaign 尚未创建，不能消费正式收据根")
    # 首先检查完整清单；不能把部分创建或丢失清单的旧目录当成新轮次。
    report["bindings"]["campaign"] = binding(campaign / "campaign.json")
    report["bindings"]["campaign_digest"] = binding(campaign / "campaign.sha256")
    memo = os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
    try:
        cu = upgrade if upgrade is not None else _managed_upgrade(Path(config["D"]))
        manifest = cu._require_formal_campaign(campaign)
        for field, key in (("campaign_id", "NEW"), ("baseline_version", "BASELINE_VERSION"),
                           ("target_version", "TARGET_VERSION")):
            if manifest.get(field) != config[key]:
                raise ValueError(f"Campaign 的 {field} 与本轮参数不一致")
        plan = cu._vc_campaign_plan(campaign, manifest)
        ledger = cu._campaign_timing_ledger_dir(campaign, manifest)
        if str(ledger) != roots["L"]:
            raise ValueError("本轮 UP 与 Campaign 当前有效账本不一致")
        report["bindings"]["ledger"] = binding(ledger / "ledger.json")
        plan_ref = manifest["vc_control"]["campaign_plan"]
        report["bindings"]["campaign_plan"] = binding(campaign / plan_ref["path"])
        revision, record = cu._current_candidate_revision_record(campaign, manifest)
        report["state"] = "campaign_verified"
        if revision is None:
            if mode == "candidate":
                raise ValueError("尚无账本激活的 Candidate，pending 目录不能作为当前候选")
        else:
            if record is None:
                candidate = cu._implicit_r1_candidate_id(campaign, manifest)
                path, _ = cu._replay_vc_checkpoint(campaign, plan, "VC-4", revision=1)
                report["bindings"]["legacy_vc4_checkpoint"] = binding(path)
            else:
                if record.get("campaign_id") != config["NEW"]:
                    raise ValueError("已激活的 revision 未绑定当前 Campaign")
                candidate = record["candidate_id"]
                directory = cu._candidate_revision_dir(campaign, revision)
                report["bindings"]["revision"] = binding(directory / "revision.json")
                report["bindings"]["revision_commit"] = binding(directory / "COMMIT")
            if candidate != config["CAND"]:
                raise ValueError("CAND 与账本激活的 Candidate 不一致，拒绝旧候选参数")
            # r1 的 checkpoint 保持历史根，r2 起跟随实际激活的 revision；不能取最大目录号。
            revision_root = campaign / "control" / "vc"
            if revision >= 2:
                revision_root = cu._candidate_revision_dir(campaign, revision)
            plain_path(str(revision_root), "当前 revision 根")
            roots["CANDIDATE_REVISION"] = str(revision)
            roots["CANDIDATE_REVISION_ROOT"] = str(revision_root)
            report.update(state="candidate_verified", candidate_id=candidate, candidate_revision=revision)
        # 同一次只读解析中若清单或绑定文件被换掉，拒绝交付混合代次的路径。
        for item in report["bindings"].values():
            if binding(Path(item["path"])) != item:
                raise ValueError("根解析期间权威输入发生变化，请重新重放")
        if cu._current_candidate_revision_record(campaign, manifest) != (revision, record):
            raise ValueError("根解析期间当前 Candidate 发生变化，请重新重放")
        if cu._campaign_timing_ledger_dir(campaign, manifest) != ledger:
            raise ValueError("根解析期间当前有效账本发生变化，请重新重放")
        coordinates(config)
        roots["ROUND_CONTEXT_STATE"] = report["state"]
        return report
    finally:
        if memo is not None:
            os.environ["CODEX_UPGRADE_IDENTITY_MEMO"] = memo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, type=Path, help="本轮明确选择的参数文件")
    parser.add_argument("--mode", choices=("init", "campaign", "candidate"), default="candidate")
    args = parser.parse_args(argv)
    try:
        binding(args.env)
        values = parse(args.env.read_text(encoding="utf-8"))
        report = resolve({**values, **derive(values)}, mode=args.mode)
    except Exception as error:
        # 只输出错误类型和合同原因，不输出参数全文、token 或环境内容。
        print(f"本轮根解析拒绝：{error}", file=sys.stderr)
        return 3
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
