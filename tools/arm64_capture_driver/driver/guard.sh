#!/bin/bash
# 派发前检查（老板 2026-09-22 拍板：写进脚本、失败自动停止，不靠人工记忆；同日审核修正三处 P1 与四点小修；
# 2026-09-22 驱动脚本入库后新增第 0 项：驱动安装复验）
#   driver    以安装目标内的 install.py verify 复验驱动脚本清单：逐文件 SHA／权限／属主与安装收据绑定的部署收据
#             必须是 control/ 下当前最新的 codex-0154-supervisor-enable 收据（工具重新部署后驱动必须重新安装确认）
#   disk      根盘：used ≤ 69% 且 available ≥ 30 GiB（候选就绪门禁策略 root-69-percent-and-30-gib/v1）且 available ≥ 操作员阈值
#             操作员阈值固定最低 40 GiB，参数文件 MIN_FREE_GIB 只能把它设得更高
#   project   项目总账：blocked=false、root_causes_at_limit=[]；remaining_live_requests 为 null（本项目无固定请求上限）合法，非空且 ≤0 拒绝
#   campaign  （pre-vc4／pre-vc5）以受管工具从 Campaign 清单解析并验证精确绑定的 UpgradeTimingLedger，要求 status=active
#             （pre-vc5）再以受管工具 _current_candidate_revision_record + _replay_vc_checkpoint 完整重放当前 revision 的 VC-4 checkpoint
#   pre-vc4／pre-vc5 先清可再生的 Go build cache 再做磁盘检查
# 用法：bash guard.sh pre-plan | pre-vc4 <campaign_dir> | pre-vc5 <campaign_dir>
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
MODE="${1:-}"; CAMPAIGN="${2:-}"
case "$MODE" in pre-plan|pre-vc4|pre-vc5) ;; *) echo "guard: MODE 只能是 pre-plan|pre-vc4|pre-vc5（收到 '$MODE'）"; exit 2;; esac
if [ "$MODE" != pre-plan ] && [ -z "$CAMPAIGN" ]; then echo "guard: $MODE 需要 <campaign_dir>"; exit 2; fi
python3 "$DRV/../install.py" verify --target "$(dirname "$DRV")" --data-root "$D" || { echo "guard/driver: FAIL — 驱动安装复验未通过，停止"; exit 1; }
MIN_FREE_GIB=$(python3 -c "
import sys
raw = sys.argv[1]
try:
    value = float(raw)
except ValueError:
    sys.exit('guard: MIN_FREE_GIB 必须是数字')
print(max(40.0, value))" "${MIN_FREE_GIB:-40}")
if [ "$MODE" = pre-vc4 ] || [ "$MODE" = pre-vc5 ]; then
  # 老板 2026-09-22 口径：候选构建后、VC-4 与 VC-5 检查前先清可再生的 Go build cache，再做磁盘检查
  BEFORE=0; if [ -d /root/.cache/go-build ]; then BEFORE=$(du -sm /root/.cache/go-build | cut -f1); fi
  go clean -cache; echo "guard/go-clean-cache: released ${BEFORE} MiB"
fi
python3 - "$MIN_FREE_GIB" <<'PY'
import shutil, sys
t, u, f = shutil.disk_usage("/"); used = u * 100 / t; free_gib = f / 2**30; need = float(sys.argv[1])
print(f"guard/disk: used={used:.1f}% free={free_gib:.1f}GiB (policy ≤69% & ≥30GiB; operator ≥{need:g}GiB, floor 40)")
if used > 69 or free_gib < 30 or free_gib < need:
    sys.exit("guard/disk: FAIL — 根盘不满足派发条件，停止")
PY
python3 -m tools.official_client_capture.codex_upgrade_project_ledger status --ledger-dir "$D/evidence/campaigns/upgrade-project-ledger" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read()); h = d.get('head', d)
remaining = h.get('remaining_live_requests')
print('guard/project:', {k: h.get(k) for k in ('sequence', 'blocked', 'root_causes_at_limit', 'remaining_live_requests')})
if h.get('blocked'):
    sys.exit('guard/project: FAIL — 总账 blocked，停止')
if h.get('root_causes_at_limit'):
    sys.exit('guard/project: FAIL — 有根因达同根因重试上限，停止')
if remaining is not None and (not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or remaining <= 0):
    sys.exit('guard/project: FAIL — 剩余请求预算非空且 ≤0（或非数值），停止')
"
if [ "$MODE" = pre-vc4 ] || [ "$MODE" = pre-vc5 ]; then
  python3 - "$CAMPAIGN" "$MODE" <<'PY'
import sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
campaign_dir = Path(sys.argv[1]).resolve(strict=True); mode = sys.argv[2]
manifest = cu._require_formal_campaign(campaign_dir)
ledger_dir = cu._campaign_timing_ledger_dir(campaign_dir, manifest)   # 从清单解析并校验 ledger_plan_sha256 绑定
summary = timing_ledger.inspect_ledger(ledger_dir)
print("guard/campaign:", {"campaign_id": manifest.get("campaign_id"), "ledger_dir": ledger_dir.name, **{k: summary.get(k) for k in ("status", "active_phase", "head_sequence", "current_revision")}})
if summary.get("status") != "active":
    sys.exit("guard/campaign: FAIL — Campaign 账本非 active，停止")
if mode == "pre-vc5":
    plan = cu._vc_campaign_plan(campaign_dir, manifest)
    revision, record = cu._current_candidate_revision_record(campaign_dir, manifest)
    if revision is None:
        sys.exit("guard/campaign: FAIL — 没有 active 候选 revision，停止")
    path, checkpoint = cu._replay_vc_checkpoint(campaign_dir, plan, "VC-4", revision=revision)
    print("guard/vc4-checkpoint:", {"revision": revision, "candidate_id": record.get("candidate_id") if record else None, "path": str(path.relative_to(campaign_dir)), "status": checkpoint.get("status"), "checkpoint_sha256": str(checkpoint.get("checkpoint_sha256"))[:16]})
PY
fi
echo "GUARD_PASS $MODE $(utc_now)"
