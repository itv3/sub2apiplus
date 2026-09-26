#!/usr/bin/env python3
"""为指定恢复 Campaign 生成 VC-2 批次 action plan（草案 / 批准预览 / 批准）。
用法：gen_vc2_plans.py <输出目录> <campaign_id> <inputs 目录名> [approve_sha]（数据根由环境变量 D 给出）"""
import json, os, pathlib, sys
sys.dont_write_bytecode = True
from driver_config import load_config
CONFIG = load_config()

out = pathlib.Path(sys.argv[1]); cid = sys.argv[2]; inputs = sys.argv[3]; approve = sys.argv[4] if len(sys.argv) > 4 else None
D = os.environ["D"]; NEW = f"{D}/evidence/campaigns/{cid}"; W = f"{D}/control/{inputs}"; TOOLS = f"{D}/tools/official_client_capture"
ACTIVE = CONFIG["ACTIVE_PROFILE"]


def plan(item, operation, command):
    return {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": [item], "reuse_item_ids": [],
            "actions": [{"action_id": item, "operation": operation, "timeout_seconds": 1800, "command": command, "item_ids": [item]}]}


base = ["/usr/bin/python3", f"{TOOLS}/codex_upgrade.py", "classify", "--campaign-dir", NEW]
full = base + ["--target-rule-manifest", f"{W}/target-rules.json", "--migration-manifest", f"{W}/rule-migration.json",
    "--scenario-manifest", f"{W}/scenarios.json", "--profile-manifest", f"{W}/profile.json",
    "--assertion-profile-manifest", f"{W}/assertion-profile.json", "--active-profile", ACTIVE,
    "--profile-patch-manifest", CONFIG["PROFILE_PATCH_JSON"]]
out.mkdir(parents=True, exist_ok=True)
(out / "action-plan-vc2-draft.json").write_text(json.dumps(plan("classify-draft", "VC-2:classify-draft", base), ensure_ascii=False, indent=2) + "\n")
(out / "action-plan-vc2-preview.json").write_text(json.dumps(plan("classify-preview", "VC-2:classify-preview", full), ensure_ascii=False, indent=2) + "\n")
if approve:
    (out / "action-plan-vc2-approve.json").write_text(json.dumps(plan("classify-approve", "VC-2:classify-approve", full + ["--approve-manifest-sha256", approve]), ensure_ascii=False, indent=2) + "\n")
print("plans for", cid, "->", out)
