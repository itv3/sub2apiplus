#!/bin/bash
# 新工作树跑门禁的包装（Codex 升级工具优化第 2 项）：给一个提交和一个 make 目标，自建临时工作树、配好
# CAPTURE_TYPESCRIPT_MODULE／CODEX_SPEC_SOURCE_ROOT、走本机重负载锁跑目标、无论成败清理工作树，日志最后一行是 EXIT=<码>。
# 逻辑都在同目录的 worktree_gate.py（文件头有完整说明与环境变量 HEAVY_RUN／WORKTREE_GATE_PARENT），这里只负责带上 PYTHONDONTWRITEBYTECODE 并 exec。
#
# 用法：bash tools/ci/worktree_gate.sh <提交> <make目标> [--log <日志绝对路径>] [--no-lock] [--node-modules auto|link|none] [--keep] [-- <make 参数>]
#   例：bash tools/ci/worktree_gate.sh "$(git rev-parse HEAD)" check-egress-spec --log /abs/path/egress.log
# 退出码与日志末行 EXIT=<码> 一致：make 的退出码（配方失败时 make 自己退出 2）；准备阶段出错 64；包装自身异常 70；被信号终止 128+信号号。
#
# exec 而不是子进程：外部发来的终止信号直接到 python，它会转给 make 所在进程组、等其退出、清掉工作树再写 EXIT。
# 不用 nohup 起它（会让子进程继承“忽略 SIGHUP”，依赖挂断检测的用例必红）；后台跑用 setsid -f。
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec python3 -B "${HERE}/worktree_gate.py" "$@"
