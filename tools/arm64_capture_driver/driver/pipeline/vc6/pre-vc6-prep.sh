#!/bin/bash
# VC-5 完成后、VC-6 晋升前（在 ARM64 上执行）：核对 VC-5 completion → 从完成收据现取验收 attempt 写 $W/vc5-att.txt → 清两类纯缓存
# （Go 编译缓存、golangci-lint 缓存，都可再生，不碰任何证据、记录、镜像、bundle）→ 复核根盘低于环境收据 before 阶段硬门槛 69%。
set -Eeuo pipefail
export PATH=/usr/local/go/bin:$PATH
D=${D:-/root/docker/capture-cli/data}
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
N=$NEWDIR
W=$V6
pgrep -f 'driver/[v]c5-all.sh' > /dev/null && { echo "vc5-all 仍在跑，不做"; exit 3; }
pgrep -x go > /dev/null && { echo "有 go 进程在跑，不清缓存"; exit 3; }
grep -q '^VC5_ALL_DONE ' "$RUNROOT/vc5-all.out" || { echo "vc5-all.out 没有 VC5_ALL_DONE"; exit 3; }
ls "$N/control/vc/" | tr '\n' ' '; echo
test -s "$N/control/vc/vc-5-checkpoint.json" || { echo "缺 vc-5-checkpoint.json"; exit 3; }
test -s "$N/control/vc/receipts/$CAND/vc5-completion.json" || { echo "缺 vc5-completion.json"; exit 3; }
# 验收 attempt 从 VC-5 完成收据的 subject.attempt_id 现取（该字段就是被验收的 attempt）
ATT=$(python3 -B -c "import json,sys; print(json.load(open(sys.argv[1]))['subject']['attempt_id'])" "$N/control/vc/receipts/$CAND/vc5-completion.json")
[[ "$ATT" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$ ]] || { echo "验收 attempt 形状不对：$ATT"; exit 3; }
mkdir -p "$W"; chmod 700 "$W"
echo "$ATT" > "$W/vc5-att.txt"; chmod 600 "$W/vc5-att.txt"; echo "vc5-att.txt=$(cat $W/vc5-att.txt)"
# 只读复核：当前有效 attempt 与完成收据一致（失败不拦，仅提示；显式 ATT 在 phase_context 里只作断言）
python3 -B "$VC6_DRV/phase_context.py" --env "$ARM64_VC_ENV" --mode attempt --attempt-id "$ATT" > "$W/vc5-att-check.out" 2>&1 \
  && echo "phase_context attempt 复核通过" || echo "（提示）phase_context attempt 复核未通过，见 $W/vc5-att-check.out"
echo "清理前：$(df -P / | tail -1 | awk '{print $3, $4, $5}')"
go clean -cache
rm -rf /root/.cache/golangci-lint
echo "清理后：$(df -P / | tail -1 | awk '{print $3, $4, $5}')"
python3 - <<'PY'
import os
st = os.statvfs("/")
bs = st.f_frsize or st.f_bsize
avail = st.f_bavail * bs; used = (st.f_blocks - st.f_bfree) * bs
den = used + avail
pct = (used * 100 + den - 1) // den  # 与环境收据 used_percent 同一公式（向上取整），>69 即 before 阶段硬失败
print(f"根盘 used_percent={pct}（门槛 ≤69），精确 {used * 100 / den:.2f}%；可用 {avail/1024**3:.1f} GiB（门槛 30 GiB／守卫 40 GiB）")
raise SystemExit(0 if pct <= 69 and avail >= 40 * 1024**3 else 5)
PY
echo PRE_VC6_PREP_DONE
