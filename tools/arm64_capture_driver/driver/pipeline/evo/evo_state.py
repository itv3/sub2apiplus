#!/usr/bin/env python3
"""Formal Campaign 现场状态（EVO 路，纯只读，输出一个 JSON）。

evolve-and-resume.sh 用它判定“现在走到哪一步了”，每一步前后都靠现场产物判断，而不是靠自己的内存。
读取：Campaign 清单、计时账本（inspect_ledger）、control/vc 下的批次／COMMIT／checkpoint、监督器 run 目录、对账目录。
不写任何文件。用法：python3 -B evo_state.py [--campaign-dir …] [--ledger-dir …]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

DATA_ROOT = Path(os.environ.get("EVO_DATA_ROOT", "/root/docker/capture-cli/data"))   # evolve-and-resume.sh 导出为本轮参数文件的数据根


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--ledger-dir", type=Path, required=True)
    arguments = parser.parse_args()
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    os.chdir(DATA_ROOT)
    sys.path.insert(0, str(DATA_ROOT))
    from tools.official_client_capture import codex_upgrade_timing_ledger as ledger_module  # noqa: E402

    campaign = arguments.campaign_dir
    ledger = ledger_module.inspect_ledger(arguments.ledger_dir)
    result: dict = {
        "ledger_status": ledger.get("status"),
        "ledger_active_phase": ledger.get("active_phase"),
        "ledger_review_phase": ledger.get("review_phase"),
        "ledger_review_root_cause_id": ledger.get("review_root_cause_id"),
        "ledger_next_action": ledger.get("next_action"),
        "ledger_head_sequence": ledger.get("head_sequence"),
        "ledger_paused_scopes": ledger.get("paused_scopes"),
        "ledger_completed_phases": ledger.get("campaign_completed_phases"),
    }
    batches = []
    for path in sorted(glob.glob(str(campaign / "control" / "vc" / "batches" / "*.json"))):
        match = re.match(r"^(\d{4})-(vc-\d)\.json$", os.path.basename(path))
        if match:
            batches.append((int(match.group(1)), match.group(2).upper()))
    result["batches"] = batches
    result["max_batch_sequence"] = max((seq for seq, _ in batches), default=0)
    result["checkpoints"] = {
        str(n): (campaign / "control" / "vc" / f"vc-{n}-checkpoint.json").is_file() for n in range(0, 7)
    }
    result["classification_draft_count"] = len(list((campaign / "classification" / "draft").glob("*"))) \
        if (campaign / "classification" / "draft").is_dir() else 0
    result["classification_approved_exists"] = (campaign / "classification" / "approved").exists()
    # 最近一个已提交批次（COMMIT 存在）及其父 run 的状态与动作。
    last = None
    for path in sorted(glob.glob(str(campaign / "control" / "vc" / "commits" / "*.json"))):
        commit = read_json(Path(path))
        last = commit
    if last is not None:
        run_dir = Path(last["parent_run_dir"])
        info: dict = {"sequence": last["sequence"], "phase": last["phase"], "run_dir": str(run_dir)}
        try:
            state = read_json(run_dir / "state.json")
            info["run_state"] = state.get("state")
            manifest = read_json(run_dir / "campaign-run-manifest.json")["manifest"]
            info["action_ids"] = [a["action_id"] for a in manifest.get("actions", [])]
        except (OSError, KeyError, ValueError) as error:
            info["error"] = str(error)
        proofs = glob.glob(str(campaign / "control" / "reconciliation" / ("*" + run_dir.name + "*") / "stage-replay.json"))
        info["stage_replay_proofs"] = proofs
        result["last_commit"] = info
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
