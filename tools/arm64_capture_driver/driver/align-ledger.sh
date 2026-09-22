#!/bin/bash
# 把只读导入后停在 active VC-0 的恢复账本对齐到 VC-1 completed：bash align-ledger.sh <ledger_dir>
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
LEDGER="$1"; T="python3 -m tools.official_client_capture.codex_upgrade_timing_ledger"
$T append --ledger-dir "$LEDGER" --event-id recovery-import-vc0-completed --phase VC-0 --event-type stage_completed --next-action "只读导入已封存 VC-0/VC-1 checkpoint；对齐账本至阶段之间" >/dev/null
$T append --ledger-dir "$LEDGER" --event-id recovery-import-vc1-started --phase VC-1 --event-type stage_started --next-action "VC-1 由 reuse-official-evidence 零请求导入" >/dev/null
$T append --ledger-dir "$LEDGER" --event-id recovery-import-vc1-completed --phase VC-1 --event-type stage_completed --next-action "等待 VC-2 批次" >/dev/null
$T status --ledger-dir "$LEDGER" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('ledger:', {k:d.get(k) for k in ('status','active_phase','head_sequence','total_deadline_at_utc')})"
