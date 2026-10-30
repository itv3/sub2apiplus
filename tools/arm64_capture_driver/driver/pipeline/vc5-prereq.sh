#!/bin/bash
# VC-5 库表前置的临时改动、恢复与核对（show | apply | restore | verify）；逻辑与口径见 vc5_prereq.py 的模块说明。
# 用法：bash vc5-prereq.sh apply --account <采集账号 id> --group <采集分组 id> --key <采集 Key id> --backup-dir <绝对路径>
set -Eeuo pipefail; umask 077
exec python3 -B "$(dirname "${BASH_SOURCE[0]}")/vc5_prereq.py" "$@"
