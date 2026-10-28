#!/usr/bin/env python3
"""生成 VC-1 官方封存的两个批次计划（预览、批准），格式沿用历史轮次已验证的手写计划。

用法（在 ARM64 上，ARM64_VC_ENV 已设置）：
  gen_vc1_seal_plans.py preview                      # 生成 action-plan-vc1-seal-preview.json
  gen_vc1_seal_plans.py approve <seal 预览联合摘要>   # 生成 action-plan-vc1-seal-approve.json
计划写入 $D/control/$IN/，随后用：
  bash ${PIPE_DRIVER_DIR}/vc-batch.sh $NEW $IN VC-1 <序号> VC-0 <计划文件名>
attempt 取 Campaign 下 official/attempts 的最新目录（只允许恰好一个 awaiting_receipts 的 attempt）。
"""
import json, os, subprocess, sys
from pathlib import Path
sys.dont_write_bytecode = True
mode = sys.argv[1]
drv = os.environ.get("PIPE_DRIVER_DIR") or str(Path(__file__).resolve().parents[2])   # vc2/ 的上两级就是驱动目录
env = subprocess.run(["python3", "-B", f"{drv}/parse_env.py", os.environ["ARM64_VC_ENV"]],
                     capture_output=True, text=True, check=True).stdout
cfg = {}
for line in env.splitlines():
    if line.startswith("export "):
        key, _, value = line[7:].partition("=")
        cfg[key] = value.strip("'")
D, NEW, IN = cfg["D"], cfg["NEW"], cfg["IN"]
camp = Path(D) / "evidence/campaigns" / NEW
attempts = sorted((camp / "official/attempts").glob("*/attempt.json"))
assert attempts, "还没有 official attempt"
ready = []
for path in attempts:
    status = json.load(open(path)).get("status")
    if status == "awaiting_receipts":
        ready.append(path.parent.name)
assert len(ready) == 1, f"需要恰好一个 awaiting_receipts 的 attempt，实际 {ready}"
attempt = ready[0]
bundle = camp / "official/attempts" / attempt / "evidence/assertion-bundle"
seal = ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "capture-official", "seal",
        "--campaign-dir", str(camp), "--attempt-id", attempt,
        "--capture-manifest", str(bundle / "capture-manifest.json"), "--assertion-evidence-root", str(bundle)]
tail = ["--max-wall-seconds", "1440", "--heartbeat-seconds", "5"]
head = {"schema_version": "codex-upgrade-vc-action-plan/v1", "reuse_item_ids": []}
if mode == "preview":
    plan = {**head, "execute_item_ids": ["prepare-official-assertion-bundle", "seal-official-preview"], "actions": [
        {"action_id": "prepare-official-assertion-bundle", "operation": "VC-1:prepare-official-assertion-bundle", "timeout_seconds": 300,
         "item_ids": ["prepare-official-assertion-bundle"],
         "command": ["/usr/bin/env", f"CAMPAIGN_DIR={camp}", f"ATTEMPT_ID={attempt}", "SIDE=official", f"REPO_ROOT={D}",
                     f"TOOL_ROOT={D}/tools/official_client_capture", "/usr/bin/bash", f"{D}/tools/prepare_assertion_bundle.sh"]},
        {"action_id": "seal-official-preview", "operation": "VC-1:capture-official-seal-preview", "timeout_seconds": 1500,
         "item_ids": ["seal-official-preview"], "command": seal + tail}]}
    out = "action-plan-vc1-seal-preview.json"
elif mode == "approve":
    sha = sys.argv[2]
    assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)
    plan = {**head, "execute_item_ids": ["seal-official-approve"], "actions": [
        {"action_id": "seal-official-approve", "operation": "VC-1:capture-official-seal-approve", "timeout_seconds": 1500,
         "item_ids": ["seal-official-approve"], "command": seal + ["--approve-seal-sha256", sha] + tail}]}
    out = "action-plan-vc1-seal-approve.json"
else:
    raise SystemExit("用法：preview | approve <sha>")
target = Path(D) / "control" / IN / out
target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
os.chmod(target, 0o600)
print(target, "attempt", attempt)
