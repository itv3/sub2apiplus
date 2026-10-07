#!/bin/bash
# 前阶段 1 收尾（R19）：Job 演练收据 → 客户端启动探测 → atomic-double 收据 → 写 stage1.env。
# 按客户端（B2-10）：Job 演练模块与追加给共享模块的 --client、启动探测的 --client 取客户端表（lib.sh 加载）；Codex 的命令行
# 与改造前相同。atomic-double 的 Claude 一侧尚未接入（客户端表 pending），Claude 跑到那一步即停。
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
# 产物存在不等于可用（E2-06）：有作业失败时工具照样写收据并退出 0，沿用前必须核对状态是通过，不是就换新目录重做。
receipt_status() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("status"))' "$1/receipt.json" 2>/dev/null || true; }
if [ -n "$JR" ] && [ "$(receipt_status "$JR")" = passed ]; then
  echo "Job 演练收据已存在，沿用：$JR"
else
  if [ -n "$JR" ]; then echo "Job 演练收据不是通过（status=$(receipt_status "$JR")），不沿用，换新目录重做：$JR"; fi
  JR="$JR_BASE"
  if [ -e "$JR" ]; then JR="$JR_BASE-r$(date -u +%H%M%S)"; fi
  mkdir -m 0700 "$JR"
  # 收据模块与 --client 取客户端表（B2-10；Codex 即原模块、参数为空）。
  python3 -m "$CT_JOB_REHEARSAL_MODULE" collect --campaign-dir "$PRE" --evidence-root "$JR" --output facts.json ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} | cut -c1-160
  python3 -m "$CT_JOB_REHEARSAL_MODULE" finalize --evidence-root "$JR" --facts facts.json --output receipt.json ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} | cut -c1-160
  if [ "$(receipt_status "$JR")" != passed ]; then echo "Job 演练收据不是通过（status=$(receipt_status "$JR")）：修好后重跑本脚本（会换新目录）"; exit 1; fi
fi

# R19：取证前用目标客户端按各 TUI 作业的真实参数启动一次（私有命名空间、本地替身、零真实请求）。
PROBE="$D/control/${CAMPAIGN_PREFIX}-client-launch-probe-$ROUND-$STAMP"
python3 -B "$DRV/client_launch_probe.py" run --campaign-dir "$PRE" --output-dir "$PROBE" --data-root "$D" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} | cut -c1-400
python3 -B "$DRV/client_launch_probe.py" verify --output-dir "$PROBE" --campaign-dir "$PRE" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} | cut -c1-200

# atomic-double 按客户端表：本轮客户端尚未接入时停在这里（不拿 Codex 的实现顶替，也不写 stage1.env）。
if [ -n "$CT_PENDING_STEP_ATOMIC_DOUBLE" ]; then echo "本轮客户端（$CT_CLIENT）的 atomic-double 尚未接入：$CT_PENDING_STEP_ATOMIC_DOUBLE"; exit 1; fi

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
  # 容器里的 /capture 就是生产数据根：禁写字节码，受管树里不留 __pycache__（E2-02）。
  docker exec --env PYTHONPATH=/capture --env PYTHONDONTWRITEBYTECODE=1 --workdir /capture capture-cli python3 -m tools.official_client_capture.codex_upgrade_campaign_run_rehearsal_receipt atomic-double-collect --evidence-root "/capture/staging/$AT" --output receipt.json | cut -c1-160
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
