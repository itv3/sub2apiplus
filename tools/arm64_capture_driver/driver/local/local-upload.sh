#!/bin/bash
# 把本机门禁产物（local-gates 六件套 + impl-logs）上传到采集主机本轮 ${RUNROOT}（只解到子目录，绝不 -C /root/；
# 2026-09-21 教训：macOS tar 的 . 条目会把 /root chown 成 501 锁死 ssh）。
# 用法：bash local-upload.sh <输出根> <ARM64 RUNROOT>（输出根含 local-gates/ 与 impl-logs/）
set -Eeuo pipefail
OUTROOT="$1"; RUNROOT="$2"
test -d "$OUTROOT/local-gates"; test -d "$OUTROOT/impl-logs"
COPYFILE_DISABLE=1 tar -cf - --no-xattrs -C "$OUTROOT" local-gates impl-logs | ssh -o ConnectTimeout=20 ARM64 "mkdir -p '$RUNROOT' && chmod 700 '$RUNROOT' && tar -xf - --no-same-owner --no-same-permissions -C '$RUNROOT' && chown -R root:root '$RUNROOT/local-gates' '$RUNROOT/impl-logs' && chmod -R u=rwX,go= '$RUNROOT/local-gates' '$RUNROOT/impl-logs' && touch '$RUNROOT/impl-logs/READY' && stat -c '%U %a %n' /root '$RUNROOT' && find '$RUNROOT/local-gates' '$RUNROOT/impl-logs' -type f | wc -l"
echo "LOCAL_UPLOAD_DONE"
