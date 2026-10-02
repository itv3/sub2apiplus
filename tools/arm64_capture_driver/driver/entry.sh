#!/bin/bash
# 入口编排器（E2-06）：一条命令走完入口。
#   建账本之前（一次报全）：便宜检查、策略兼容与激活认证、入口门禁与 pre-A3、零请求 smoke、atomic-double；
#   建账本之后（失败即停）：建账本、环境收据、checkpoint、预检 plan、Job 演练、启动探测、发布认证（P0 收据与收口由 E2-07 接上）。
# 每步写步骤记录（$RUNROOT/entry-steps），重新执行同一条命令按记录续跑：判定为沿用的跳过，从失败的那一步接着做，账本不重建。
# 判定规则与输入清单见 entry_steps.py（E2-05），编排见 entry_orchestrator.py；每次运行的日志与汇总在 $RUNROOT/entry-runs/<UTC>/。
# 参数文件要有入口门禁的源码坐标 ENTRY_BUNDLE、ENTRY_BRANCH、ENTRY_COMMIT（部署到数据根的那一份工具提交）；产物根 ENTRY_ROOT
# 缺省是数据根，验收演练放在数据根 staging 下的独立目录（配演练总账，生产项目总账与正式坐标不写）。
# 用法（采集主机 root；入口门禁里有挂断检测用例，必须 setsid -f 启动，不能 nohup）：
#   ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash entry.sh [--plan] [--from <步骤>] [--to <步骤>] > $RUNROOT/entry.out 2>&1 < /dev/null
#   --plan 只判定不执行；验收专用 --inject-mask <步骤>=<目录>（执行这一步时用只读空 tmpfs 遮住目录，制造一次真实失败）。
# 退出码：0 全部完成或沿用；1 有步骤失败；2 用法或配置错误；3 被阻塞（账本输入不一致等）或已有编排在运行。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
exec python3 -B "$DRV/entry_orchestrator.py" "$@"
