#!/bin/bash
# 采集登录态访问令牌的“剩余有效期检查 + 直连刷新”（采集主机侧，随驱动安装在 <驱动目录>/pipeline/preflight-refresh.sh）。
#
# 背景：VC-1 官方采集用的是 capture-cli 的 ChatGPT 登录态（$D/state/codex/auth.json，容器内 /root/.codex/auth.json）。访问令牌过期前约 5 分钟，
# 官方客户端会自己尝试刷新；官方 official-oauth-refresh 场景走抓包中继，中继把 POST /oauth/token 转给上游得到 403（Cloudflare），刷新不会落盘，
# 且场景前后 auth.json 摘要相同。所以令牌剩余有效期不足以覆盖整个 VC-1 时，必须在 VC-1 派发之前先“直连”刷新一次：
# 用官方客户端自带 app-server 的 account/read {refreshToken:true}，在 capture-cli 容器里无代理直连，不经 Sub2API、不走抓包中继，不是模型请求。
# 只打印非秘密观测（到期时间、last_refresh、文件摘要前缀），令牌形态在输出里一律截断。
#
# 用法：
#   bash preflight-refresh.sh --remaining           只读：打印访问令牌剩余分钟数（已过期为负数）；读不出退出 4
#   bash preflight-refresh.sh --ensure [--min N]    剩余 ≥ N 分钟就什么都不做（退出 0）；不足就直连刷新一次，刷新后再核对仍 ≥ N，否则退出 5
#                                                   N 缺省取 pipeline.env 的 PIPE_TOKEN_MIN_MINUTES，再缺省 150（VC-1 预计时长 + 1 小时）
#   bash preflight-refresh.sh                       无条件直连刷新一次（必须在开工空跑结束之后、entry.sh 之前运行）
# 退出码：0 成功；2 驱动自身失败；3 请求发出但 auth.json 没变（多半被服务端拒绝）；4 读不出剩余有效期；5 刷新后仍不足；6 前置缺失。
# 取值：数据根、客户端路径、版本取本轮参数文件；容器名、auth.json 路径可在 pipeline.env 里用 PIPE_CAPTURE_CONTAINER、PIPE_CAPTURE_AUTH 覆盖。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 6; }
pl_defaults
CONTAINER=${PIPE_CAPTURE_CONTAINER:-capture-cli}
AUTH=${PIPE_CAPTURE_AUTH:-${D}/state/codex/auth.json}
SCRIPT=${D}/tools/official_client_capture/drive_codex_auth_refresh.py
CLIENT_PATH=${CLIENT_BIN:-${CODEX_BIN:-}}
MIN_DEFAULT=${PIPE_TOKEN_MIN_MINUTES:-150}

# 只读观测：remaining 模式打印剩余分钟；full 模式打印一行非秘密摘要（刷新前后各一次）
observe() { # $1 remaining|full
  python3 -B - "${AUTH}" "$1" <<'PY'
import base64, hashlib, json, sys, time
path, mode = sys.argv[1:3]
try:
    raw = open(path, "rb").read()
    data = json.loads(raw)
except (OSError, ValueError):
    raise SystemExit(4)
tokens = data.get("tokens") or {}
def exp_of(token):
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg)).get("exp")
    except Exception:
        return None
exp = exp_of(tokens.get("access_token", ""))
if mode == "remaining":
    if not isinstance(exp, (int, float)):
        raise SystemExit(4)
    print(int((exp - time.time()) // 60))
else:
    print("last_refresh=%s access_exp=%s(%s) refresh_len=%s file_sha=%s" % (
        data.get("last_refresh"), time.strftime("%m-%d %H:%MZ", time.gmtime(exp)) if exp else None,
        "有效" if exp and exp > time.time() else "已过期", len(tokens.get("refresh_token") or ""), hashlib.sha256(raw).hexdigest()[:12]))
PY
}
remaining() { observe remaining; }

refresh_once() {
  [ -f "${SCRIPT}" ] && [ -f "${AUTH}" ] && [ -n "${CLIENT_PATH}" ] || { echo "前置缺失：驱动 ${SCRIPT}、登录态 ${AUTH} 或本轮参数文件的客户端路径"; return 6; }
  local ts out rc
  ts=$(date -u +%Y%m%dt%H%M%Sz)
  out=${RR}/preflight-refresh-${ts}
  install -d -m 700 "${out}"
  echo "== 刷新前"; observe full | tee "${out}/before.txt" || true
  echo "== 直连刷新（${TARGET_VERSION} 客户端，无代理，关闭 plugins／apps）"
  set +e
  docker exec -i "${CONTAINER}" python3 - --codex-bin "${CLIENT_PATH}" --codex-version "${TARGET_VERSION}" --disable plugins --disable apps \
    --events-output "/tmp/preflight-refresh-${ts}.json" < "${SCRIPT}" > "${out}/driver.out" 2> "${out}/driver.err"
  rc=$?
  set -e
  echo "驱动退出码=${rc}（0 刷新成功并落盘；3 请求发出但 auth.json 未变，多半被服务端拒绝；2 驱动自身失败）"
  sed -E 's/(eyJ[A-Za-z0-9_-]{10})[A-Za-z0-9_.-]+/\1…/g' "${out}/driver.out" | tail -12
  echo "-- stderr（令牌形态已截断）"
  sed -E 's/(eyJ[A-Za-z0-9_-]{10})[A-Za-z0-9_.-]+/\1…/g' "${out}/driver.err" | tail -8
  echo "== 刷新后"; observe full | tee "${out}/after.txt" || true
  docker exec "${CONTAINER}" sh -c "test -f /tmp/preflight-refresh-${ts}.json && cp /tmp/preflight-refresh-${ts}.json /dev/stdout" > "${out}/events.json" 2>/dev/null || true
  echo "RC=${rc}"
  echo "PREFLIGHT_REFRESH_DONE ${out}"
  return "${rc}"
}

MODE=${1:-refresh}
case "${MODE}" in
  --remaining) remaining || { echo "读不出访问令牌剩余有效期：${AUTH}" >&2; exit 4; } ;;
  --ensure)
    MIN=${MIN_DEFAULT}
    if [ "${2:-}" = "--min" ]; then MIN=${3:?"--min 需要分钟数"}; fi
    [[ ${MIN} =~ ^[0-9]+$ ]] || { echo "--min 必须是非负整数"; exit 2; }
    LEFT=$(remaining) || { echo "读不出访问令牌剩余有效期：${AUTH}；不猜，按不足处理需人工确认" >&2; exit 4; }
    if [ "${LEFT}" -ge "${MIN}" ]; then echo "TOKEN_OK 访问令牌剩余 ${LEFT} 分钟 ≥ 阈值 ${MIN} 分钟，无需刷新"; exit 0; fi
    echo "访问令牌剩余 ${LEFT} 分钟 < 阈值 ${MIN} 分钟，先直连刷新一次（VC-1 官方作业不能在抓包中继上碰刷新）"
    refresh_once || { rc=$?; echo "直连刷新失败 rc=${rc}，不继续。处置：核对 ${AUTH} 的登录态与网络；仍不行由老板重新登录官方账号" >&2; exit "${rc}"; }
    LEFT=$(remaining) || { echo "刷新后读不出剩余有效期" >&2; exit 4; }
    [ "${LEFT}" -ge "${MIN}" ] || { echo "刷新后访问令牌仍只剩 ${LEFT} 分钟 < ${MIN} 分钟，不继续" >&2; exit 5; }
    echo "TOKEN_OK 刷新后访问令牌剩余 ${LEFT} 分钟 ≥ 阈值 ${MIN} 分钟" ;;
  refresh) refresh_once ;;
  *) echo "用法：preflight-refresh.sh [--remaining | --ensure [--min N]]"; exit 2 ;;
esac
