#!/bin/bash
# 前阶段 1 收尾（R19）：Job 演练收据 → 客户端启动探测 → atomic-double 收据 → 写 stage1.env。
# stage1.sh 建好预检 Campaign 后写出 $RUNROOT/stage1.partial.env 并调用本脚本。本段任一步失败时，
# 修复环境后单独重跑本脚本即可续跑：不重做 stage1 前面各步、不新建账本；已有收据的 Job 演练与
# atomic-double 直接沿用，半途中断留下的目录原样保留、换带时间后缀的新目录重做；
# 客户端启动探测每次都重跑（反映修复后的最新环境）。
# 探测未通过即以非零退出，不写 stage1.env，后续 stage2／vc0_closeout 都进不去。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
PARTIAL="$RUNROOT/stage1.partial.env"
test -f "$PARTIAL" || { echo "缺少 $PARTIAL（先跑 stage1.sh）"; exit 1; }
# 上一次的 stage1.env 先改名留档：本段失败时不能让旧坐标被当成本轮结果。
if [ -f "$RUNROOT/stage1.env" ]; then mv "$RUNROOT/stage1.env" "$RUNROOT/stage1.env.superseded-$(date -u +%Y%m%dt%H%M%Sz)"; fi
# shellcheck disable=SC1090
source "$PARTIAL"
: "${DEPLOY:?}" "${ENV:?}" "${PRECID:?}" "${PRE:?}"
test -d "$PRE" || { echo "预检 Campaign 不存在：$PRE"; exit 1; }

JR_BASE="$D/control/${CAMPAIGN_PREFIX}-job-rehearsal-vc5-$ROUND-$STAMP"
JR=""
for candidate in "$JR_BASE" "$JR_BASE"-r*; do
  if [ -f "$candidate/receipt.json" ]; then JR="$candidate"; fi
done
if [ -n "$JR" ]; then
  echo "Job 演练收据已存在，沿用：$JR"
else
  JR="$JR_BASE"
  if [ -e "$JR" ]; then JR="$JR_BASE-r$(date -u +%H%M%S)"; fi
  mkdir -m 0700 "$JR"
  python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt collect --campaign-dir "$PRE" --evidence-root "$JR" --output facts.json | cut -c1-160
  python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt finalize --evidence-root "$JR" --facts facts.json --output receipt.json | cut -c1-160
fi

# R19：取证前用目标客户端按各 TUI 作业的真实参数启动一次（私有命名空间、本地替身、零真实请求）。
PROBE="$D/control/${CAMPAIGN_PREFIX}-client-launch-probe-$ROUND-$STAMP"
python3 -B "$DRV/client_launch_probe.py" run --campaign-dir "$PRE" --output-dir "$PROBE" --data-root "$D" | cut -c1-400
python3 -B "$DRV/client_launch_probe.py" verify --output-dir "$PROBE" --campaign-dir "$PRE" | cut -c1-200

AT_BASE="codex-atomic-vc0-vc1-$ROUND-$STAMP"
AT=""
for candidate in "$D/staging/$AT_BASE" "$D/staging/$AT_BASE"-r*; do
  if [ -f "$candidate/receipt.json" ]; then AT=$(basename "$candidate"); fi
done
if [ -n "$AT" ]; then
  echo "atomic-double 收据已存在，沿用：$D/staging/$AT"
else
  AT="$AT_BASE"
  if [ -e "$D/staging/$AT" ]; then AT="$AT_BASE-r$(date -u +%H%M%S)"; fi
  mkdir -m 0700 "$D/staging/$AT"
  docker exec --env PYTHONPATH=/capture --workdir /capture capture-cli python3 -m tools.official_client_capture.codex_upgrade_campaign_run_rehearsal_receipt atomic-double-collect --evidence-root "/capture/staging/$AT" --output receipt.json | cut -c1-160
fi
cat > "$RUNROOT/stage1.env" <<STAGE1ENV
DEPLOY=$DEPLOY
ENV=$ENV
PRECID=$PRECID
PRE=$PRE
JR=$JR
AT=$AT
PROBE=$PROBE
STAGE1ENV
chmod 600 "$RUNROOT/stage1.env"
echo "STAGE1_DONE $RUNROOT/stage1.env"
