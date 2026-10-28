#!/bin/bash
# 流水线 ORDER 不能给脚本带参数，这里包一层：等价于 bash vc6-switch.sh rollback
exec bash "$(dirname "${BASH_SOURCE[0]}")/vc6-switch.sh" rollback
