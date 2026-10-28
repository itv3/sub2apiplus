#!/bin/bash
# 晋升（vc6-promote.sh）里的 go run 会让 Go 编译缓存回涨，而 post-promotion 门禁前后的 ARM64
# 环境收据对根盘用量有硬门槛（gate_before 阶段 used_percent ≤69%）。所以晋升之后、门禁之前再清一次 Go 编译缓存并复核根盘。
# 只清可再生的 Go 编译缓存，不碰任何证据、记录、镜像、bundle。
set -Eeuo pipefail
export PATH=/usr/local/go/bin:$PATH PYTHONDONTWRITEBYTECODE=1
pgrep -x go > /dev/null && { echo "有 go 进程在跑，不清缓存"; exit 3; }
echo "清理前：$(df -P / | tail -1 | awk '{print $3, $4, $5}')"
go clean -cache
echo "清理后：$(df -P / | tail -1 | awk '{print $3, $4, $5}')"
python3 -B - <<'PY'
import os
st = os.statvfs("/")
bs = st.f_frsize or st.f_bsize
avail = st.f_bavail * bs; used = (st.f_blocks - st.f_bfree) * bs
den = used + avail
pct = (used * 100 + den - 1) // den  # 与环境收据 used_percent 同一公式（向上取整），>69 即 before 阶段硬失败
print(f"根盘 used_percent={pct}（门槛 ≤69），精确 {used * 100 / den:.2f}%；可用 {avail/1024**3:.1f} GiB（门槛 30 GiB／守卫 40 GiB）")
raise SystemExit(0 if pct <= 69 and avail >= 40 * 1024**3 else 5)
PY
echo "VC6_CLEAN_CACHE_DONE $(date -u +%H:%M:%SZ)"
