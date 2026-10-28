#!/bin/bash
# 改 env.sh 里已有键的值（采集主机侧，<驱动目录>/pipeline/set-env-keys.sh）。只改白名单键、只改已存在的单行赋值，
# 改前备份成 env.sh.bak-<UTC>，改后立刻用驱动的 parse_env.py 解析，失败则自动还原备份。
# 用法：bash set-env-keys.sh <本轮参数文件> KEY=VALUE [KEY=VALUE …]
# 例：  bash set-env-keys.sh $ENV ENTRY_BUNDLE=$RUNROOT/<轮次标签>-deploy-<提交前9位>.bundle ENTRY_COMMIT=<40 位> ENTRY_BRANCH=<集成分支>
# VALUE 里可以写 $D、$RUNROOT 这类对前面已定义键的引用（原样写进文件，由 parse_env 展开），所以传参时在本机 shell 里要用单引号。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
ENVF=${1:?"用法：set-env-keys.sh <env.sh> KEY=VALUE …"}; shift
[ "$#" -ge 1 ]
[ -f "${ENVF}" ] && [ ! -L "${ENVF}" ]
BAK="${ENVF}.bak-$(date -u +%Y%m%dt%H%M%Sz)"
cp -p "${ENVF}" "${BAK}"
if ! python3 -B - "${ENVF}" "$@" <<'PY'
import re, sys
path, pairs = sys.argv[1], sys.argv[2:]
ALLOWED = {"ENTRY_BUNDLE", "ENTRY_BRANCH", "ENTRY_COMMIT", "C", "DC", "RECEIPT", "BUNDLE_BRANCH", "PROFILE_DIGEST", "TARGET_PROFILE_ID",
           "PROJECT_DEADLINE_UTC", "PRODUCTION_IMAGE", "STAMP", "RETIRE_VERSION", "HISTORICAL_SOURCE_ROOT", "COMPOSE_BACKUP",
           "CODEX_ACCOUNT_ID", "API_KEY_ID"}   # 采集账号与采集专用 Key 的 ID
VALUE = re.compile(r"^[A-Za-z0-9_./:@,=+${}-]+$")
text = open(path, encoding="utf-8").read().split("\n")
for pair in pairs:
    key, sep, value = pair.partition("=")
    if not sep or key not in ALLOWED or not VALUE.match(value):
        raise SystemExit(f"拒绝：{pair!r}（键不在白名单或值含不允许的字符）")
    hits = [i for i, line in enumerate(text) if line.startswith(key + "=")]
    if len(hits) != 1:
        raise SystemExit(f"{key} 在文件里不是恰好一行赋值（{len(hits)} 行），拒绝")
    text[hits[0]] = f"{key}={value}"
open(path, "w", encoding="utf-8").write("\n".join(text))
PY
then cp -p "${BAK}" "${ENVF}"; echo "修改失败，已还原"; exit 2; fi
if ! python3 -B "${PIPE_DRIVER_DIR}/parse_env.py" "${ENVF}" > /dev/null; then cp -p "${BAK}" "${ENVF}"; echo "parse_env 拒绝新内容，已还原"; exit 2; fi
chmod 600 "${ENVF}"
echo "已改并通过 parse_env；备份 ${BAK}"
