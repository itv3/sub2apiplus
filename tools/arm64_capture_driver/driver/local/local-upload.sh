#!/bin/bash
# 把本机门禁产物（local-gates 六件套 + impl-logs）上传到采集主机本轮 ${RUNROOT}（只解到子目录，绝不 -C /root/；
# 2026-09-21 教训：macOS tar 的 . 条目会把 /root chown 成 501 锁死 ssh）。
# 用法：bash local-upload.sh <输出根> <ARM64 RUNROOT>（输出根含 local-gates/ 与 impl-logs/）
set -Eeuo pipefail
OUTROOT="$1"; RUNROOT="$2"
test -d "$OUTROOT/local-gates"; test -d "$OUTROOT/impl-logs"
# 远端命令只接受独立子目录；限制字符同时防止参数插入远端 shell。
[[ "$RUNROOT" =~ ^/[a-zA-Z0-9/_.-]+$ ]] && [[ "$RUNROOT" != / && "$RUNROOT" != /root && "$RUNROOT" != */../* ]] || exit 3
python3 - "$RUNROOT" <<'PY' || exit 3
from pathlib import Path
import sys
root = Path(sys.argv[1])
if root.resolve() != root or len(root.parts) < 3:
    raise SystemExit('上传目标必须是规范的独立子目录')
PY
python3 "$(dirname "${BASH_SOURCE[0]}")/../upload_manifest.py" create "$OUTROOT"
SSH=(ssh -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 ARM64)
"${SSH[@]}" "mkdir -p '$RUNROOT/impl-logs' && chmod 700 '$RUNROOT' '$RUNROOT/impl-logs' && rm -f '$RUNROOT/impl-logs/READY' && touch '$RUNROOT/impl-logs/HEARTBEAT'"
(
  while sleep 30; do
    "${SSH[@]}" "touch '$RUNROOT/impl-logs/HEARTBEAT'" || exit 3
  done
) &
HEARTBEAT_PID=$!
cleanup_upload() { kill "$HEARTBEAT_PID" 2>/dev/null || true; wait "$HEARTBEAT_PID" 2>/dev/null || true; }
trap cleanup_upload EXIT
trap 'exit 3' INT TERM HUP
COPYFILE_DISABLE=1 tar -cf - --no-xattrs --exclude='._*' --exclude='impl-logs/READY' --exclude='impl-logs/HEARTBEAT' -C "$OUTROOT" local-gates impl-logs | "${SSH[@]}" "tar -xf - --no-same-owner --no-same-permissions -C '$RUNROOT' && chown -R root:root '$RUNROOT/local-gates' '$RUNROOT/impl-logs' && chmod -R u=rwX,go= '$RUNROOT/local-gates' '$RUNROOT/impl-logs' && touch '$RUNROOT/impl-logs/READY' && stat -c '%U %a %n' /root '$RUNROOT' && find '$RUNROOT/local-gates' '$RUNROOT/impl-logs' -type f | wc -l"
echo "LOCAL_UPLOAD_DONE"
