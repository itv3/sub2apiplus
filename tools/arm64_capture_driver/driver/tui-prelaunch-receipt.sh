#!/bin/bash
# 新版二进制 TUI 零请求启动预探收据的生成与核对接口（Codex 升级工具优化第 5 项，ARM64 侧）。
#
# 为什么要有：新版客户端的 TUI 启动行为会变（上一轮取消了 daemon 功能白名单，带 --disable plugins／apps 的 TUI 报 “Cannot use the shared background server …
# --no-daemon” 退出，入口第 13 步客户端启动探测 5 个组合 3 个 drive_exited，且排在 2 小时门禁之后，修复与重部署 0.5 小时）。在开工准备阶段（与登记并行）用新版二进制对各 TUI
# 场景做一次零请求启动预探，就能提前发现。预探本身由 ARM64 上的预探流程做（对每个场景按驱动的真实参数启动一次、不发任何真实请求，观察是否一直停在首帧后的交互等待
# 而不是启动即退出）；本脚本只负责把预探结果登记成收据，并给起跑前置检查（entry-preflight.sh 的 tui-prelaunch-receipt、后台验证的 tui-prelaunch-receipt）核对用。
#
# 必需场景：compact-tui、ws-idle-close-tui、daemon-tui、guardian-tui，以及会话内步骤场景 effort-update-ws、effort-update-http、model-switch-ws、
# compact-continue-ws（bash tui-prelaunch-receipt.sh scenarios 列出）；image-repeat-tui、search-repeat-tui、review-tui、effort-unsupported-ws
# 可选，登记了就必须通过。收据绑定本轮目标版本与登记的客户端二进制摘要（CODEX_BIN_SHA256），并要求真实请求数为 0。
#
# 用法（参数全部来自 $ARM64_VC_ENV）：
#   bash tui-prelaunch-receipt.sh scenarios
#   bash tui-prelaunch-receipt.sh write --scenario compact-tui=passed --scenario ws-idle-close-tui=passed --scenario daemon-tui=passed \
#        --scenario guardian-tui=passed --scenario 其余必需场景=passed … [--scenario 名称=failed:退出码:屏幕尾部文字 …] --started <UTC> --finished <UTC> [--note 文字]
#      写之前核对宿主机上 $CODEX_BIN 的摘要等于登记的 CODEX_BIN_SHA256、--version 含目标版本；失败的场景也照实登记（收据会让起跑检查拒绝，并把退出码与屏幕尾部带进提示）。
#   bash tui-prelaunch-receipt.sh verify      # 只读：按起跑前置检查同一规则核对当前收据，通过 0、不通过 1
# 收据位置：$RUNROOT/local-receipts/tui-prelaunch.json（0600）。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
CHECKS="$DRV/opening_checks.py"
DIR="$RUNROOT/local-receipts"
BINARY=${!CT_BINARY_KEY}
EXPECTED_SHA=${!CT_BINARY_SHA256_KEY}
command="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
case "$command" in
  scenarios)
    python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import opening_checks as o; print("\n".join(o.REQUIRED_TUI_SCENARIOS))' "$DRV" ;;
  write)
    actual=$(sha256sum "$BINARY" | cut -c1-64)
    if [ "$actual" != "$EXPECTED_SHA" ]; then echo "宿主机上 ${BINARY} 的摘要 ${actual} 不等于登记的 ${EXPECTED_SHA}：预探的不是本轮登记的二进制，拒绝写收据" >&2; exit 1; fi
    version=$("$BINARY" --version 2>&1 | head -n 1)
    case "$version" in *"$TARGET_VERSION"*) ;; *) echo "客户端 --version 输出 ${version} 不含目标版本 ${TARGET_VERSION}，拒绝写收据" >&2; exit 1 ;; esac
    exec python3 -B "$CHECKS" tui-receipt-write --dir "$DIR" --version "$TARGET_VERSION" --binary "$BINARY" --binary-sha256 "$actual" --host "$(hostname -s)" "$@" ;;
  verify)
    exec python3 -B "$CHECKS" tui-receipt-check --dir "$DIR" --version "$TARGET_VERSION" --binary-sha256 "$EXPECTED_SHA" ;;
  *) echo "用法：bash tui-prelaunch-receipt.sh scenarios|write …|verify" >&2; exit 2 ;;
esac
