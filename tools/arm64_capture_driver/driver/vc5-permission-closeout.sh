#!/bin/bash
# 候选 attempt 客户端证据（evidence/client/**）的权限收口——幂等、且以 EvidenceManifest 为硬边界。
#   * evidence-manifest.json 尚不存在（seal 预览之前）：只对模式／属主不符的条目执行 chmod 700（目录）／600（文件）／
#     chown root:root；已经合规的条目一律不碰（重复执行时 chmod/chown 调用为 0，ctime 不变）。
#   * evidence-manifest.json 已存在（seal 预览之后）：manifest 绑定了 evidence/** 的完整 stat 边界（含 ctime_ns），
#     任何 chmod/chown 即使模式不变也会让 ctime 漂移、读侧判 evidence-integrity 永久停线（2026-09-22 v14r4 批次 15）。
#     此时本脚本只核对、绝不修改；发现不合规条目以退出码 3 报告给操作员（也不能在这里修）。
# 用法：bash vc5-permission-closeout.sh <attempt_root>；输出一行 JSON 摘要。属主固定 root（离线测试可用 CLOSEOUT_OWNER 覆盖）。
set -Eeuo pipefail; umask 077
A="${1:?attempt_root}"; EV="$A/evidence"; CLIENT="$EV/client"; MANIFEST="$A/evidence-manifest.json"; OWNER="${CLOSEOUT_OWNER:-root}"
test -d "$A" || { echo "attempt 根不存在：$A"; exit 2; }
if [ -e "$MANIFEST" ] || [ -L "$MANIFEST" ]; then
  # 只读核对：不 mkdir、不 chmod、不 chown
  BAD_DIRS=0; BAD_FILES=0; BAD_OWNERS=0
  if [ -d "$CLIENT" ]; then
    BAD_DIRS=$(find "$CLIENT" -type d ! -perm 700 | wc -l | tr -d ' ')
    BAD_FILES=$(find "$CLIENT" -type f ! -perm 600 | wc -l | tr -d ' ')
    BAD_OWNERS=$(find "$CLIENT" ! -user "$OWNER" | wc -l | tr -d ' ')
  fi
  printf '{"mode":"verify-only","manifest_present":true,"client_root":"%s","noncompliant_dirs":%s,"noncompliant_files":%s,"noncompliant_owners":%s}\n' "$CLIENT" "$BAD_DIRS" "$BAD_FILES" "$BAD_OWNERS"
  if [ "$BAD_DIRS$BAD_FILES$BAD_OWNERS" != "000" ]; then echo "PERMISSION_CLOSEOUT_VERIFY_FAILED: manifest 已存在，不合规条目不得再修改（需人工裁定）"; exit 3; fi
  echo "PERMISSION_CLOSEOUT_VERIFIED"; exit 0
fi
mkdir -p "$CLIENT/raw" "$CLIENT/generated" "$CLIENT/receipts"
CHANGED_DIRS=$(find "$CLIENT" -type d ! -perm 700 -print -exec chmod 700 {} + | wc -l | tr -d ' ')
CHANGED_FILES=$(find "$CLIENT" -type f ! -perm 600 -print -exec chmod 600 {} + | wc -l | tr -d ' ')
CHANGED_OWNERS=$(find "$CLIENT" ! -user "$OWNER" -print -exec chown "$OWNER:$OWNER" {} + | wc -l | tr -d ' ')
printf '{"mode":"closeout","manifest_present":false,"client_root":"%s","changed_dirs":%s,"changed_files":%s,"changed_owners":%s}\n' "$CLIENT" "$CHANGED_DIRS" "$CHANGED_FILES" "$CHANGED_OWNERS"
echo "PERMISSION_CLOSEOUT_DONE"
