#!/bin/bash
# 派发一个 VC 批次：bash vc-batch.sh <campaign_id> <inputs 目录名> <phase> <sequence> <predecessor_phase> <action-plan 文件名>
# 监督器状态目录缺省为 control/<campaign_id>-supervisor；VC-0 收口另建了状态目录的轮次（后继批次必须接在首批同一
# 状态目录上，父批次 stop receipt 在其中）以环境变量 VC_STATE_DIR 指定。
# 修复轮规则（E4-01，指南「修好接着跑」）：派发前先读当前部署的后台验证结论（background-validate.sh check-boundary），
# 已失败或中止就拒绝派发、退出 3；还在跑或没有结论照常派发。然后向统一调度执行器申请整机（unit_executor.py acquire）：
# 后台验证停派、等在跑单元结束才批准，批次结束（含失败退出）归还，后台接着跑。等批准的上限 VC_ACQUIRE_TIMEOUT 秒
# （默认 3600），超时不派发、退出 3。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
CID="$1"; INPUTS="$2"; PHASE="$3"; SEQ="$4"; PRED="$5"; PLAN="$6"
CDIR="$D/evidence/campaigns/$CID"; WDIR="$D/control/$INPUTS"; STATE="${VC_STATE_DIR:-$D/control/$CID-supervisor}"
if [ -n "${VC_STATE_DIR:-}" ]; then test -d "$STATE" || { echo "VC_STATE_DIR 不存在：${STATE}"; exit 2; }; else [ -d "$STATE" ] || mkdir -m 0700 "$STATE"; fi
PRED_LOWER=$(echo "$PRED" | tr "A-Z" "a-z")
# 改造 2：候选级阶段（VC-4～VC-6）r≥2 的 checkpoint 落在 control/vc/revisions/r<N>/；有则取最大 revision 的，否则回落 Campaign 级路径
PRED_CKPT="$CDIR/control/vc/$PRED_LOWER-checkpoint.json"
case "$PRED" in VC-4|VC-5|VC-6)
  # 没有任何 revision 目录时 ls 失败，pipefail 下整条赋值失败会让 set -e 直接中止，回落分支走不到：|| true 兜住。
  LATEST=$(ls -d "$CDIR"/control/vc/revisions/r*/ 2>/dev/null | sed "s#.*/r\([0-9]*\)/#\1#" | sort -n | tail -n 1 || true)
  if [ -n "$LATEST" ] && [ "$LATEST" -ge 2 ] && [ -f "$CDIR/control/vc/revisions/r$LATEST/$PRED_LOWER-checkpoint.json" ]; then PRED_CKPT="$CDIR/control/vc/revisions/r$LATEST/$PRED_LOWER-checkpoint.json"; fi;;
esac
echo "predecessor checkpoint: $PRED_CKPT"
if ! python3 -B "$DRV/background_validation.py" check-boundary --runroot "$RUNROOT" --data-root "$D"; then
  echo "批次边界：后台验证不放行，不派发批次 ${SEQ}（修好、定向回归、部署后重新起后台验证）"; exit 3
fi
OWNER="vc-batch-$CID-$SEQ"
if ! python3 -B "$DRV/unit_executor.py" acquire --owner "$OWNER" --owner-pid "$$" --timeout "${VC_ACQUIRE_TIMEOUT:-3600}" > "$WDIR/batch-$SEQ.acquire.json"; then
  echo "向调度器申请整机失败（见上方输出），不派发批次 ${SEQ}"; exit 3
fi
trap 'python3 -B "$DRV/unit_executor.py" release --owner "$OWNER" > /dev/null 2>&1 || true' EXIT
echo "整机已批准：$(cut -c1-200 "$WDIR/batch-$SEQ.acquire.json")"
set +e
python3 -m tools.official_client_capture.codex_upgrade compile-and-run-vc-batch --campaign-dir "$CDIR" --state-dir "$STATE" --phase "$PHASE" --sequence "$SEQ" --predecessor-checkpoint "$PRED_CKPT" --action-plan "$WDIR/$PLAN" > "$WDIR/batch-$SEQ.out" 2> "$WDIR/batch-$SEQ.err"
RC=$?
set -e
echo "rc=$RC"
python3 - "$WDIR/batch-$SEQ.out" <<'PY'
import json, sys
t = open(sys.argv[1]).read()
try:
    d = json.loads(t); run = d.get("campaign_run") or {}
    print({k: d.get(k) for k in ("status", "phase", "batch_sequence")}, "run:", {k: run.get(k) for k in ("status", "reason")})
    print("actions:", [(a.get("action_id"), a.get("returncode"), a.get("status")) for a in run.get("actions", [])])
    tl = d.get("timing_ledger") or {}
    print("timing events:", [(e.get("event_type"), e.get("phase")) for e in tl.get("events", [])], "completion:", tl.get("completion"))
    print("admission head:", (d.get("project_ledger") or {}).get("head_sequence"))
except Exception:
    print(t[-1200:])
PY
tail -c 800 "$WDIR/batch-$SEQ.err"
# 日志解析成功不代表批次成功；上层只能在正式 CLI 成功时写完成标记。
exit "$RC"
