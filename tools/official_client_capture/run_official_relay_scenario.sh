#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 用真实上游字节中继采集**依赖模型自主决策的状态链**：工具调用、生图、压缩。
#
# 这是中继相对终结型探针的根本价值所在。探针自己应答，客户端拿不到真实响应就
# 不会有后续动作，这三类链路的请求根本发不出来（§2.12.3 那 9 条「可验未验」
# 的共同障碍）。中继转发真实上游，模型才会真的决定调用工具、生成图片、触发压缩。
#
# 提示词按场景注入，且刻意写得直白——降低模型随机性对请求结构的影响。
#
# 与既有探针的区别：探针自己应答、不转发上游，客户端拿不到真实响应就不会有后续
# 动作；本脚本走 upstream_byte_relay.py，两条 TLS 腿之间只复制明文字节，因此
# **多轮对话、工具调用、压缩触发这些依赖模型自主决策的链路才可能发生**。
#
# ALPN 必须为空
# -------------
# 官方在 native-tls 默认分支下**恒不 offer ALPN**——h1 探针三份基线与 nativetls
# 基线的 negotiated_alpn 全为 None，只有配了 CODEX_CA_CERTIFICATE 触发 rustls 时
# 才 offer h2（official-h2-20260727T131936Z）。因此本脚本：
#
#   - 不设 CODEX_CA_CERTIFICATE / SSL_CERT_FILE（否则切 rustls，污染 TLS 结论）
#   - 改把抓包 CA 装进容器系统信任库，native-tls 走 OpenSSL 系统根
#   - 中继的 --assume-alpn 留空，即不向上游 offer ALPN
#
# 会消耗真实配额：本脚本与真实上游完成真实往返。

capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}
scrub_tool="$capture_tool_root/scrub_raw_bytes.py"
run_id=${RUN_ID:?必须提供 RUN_ID}
model=${MODEL:-gpt-5.4}
codex_version=${CODEX_VERSION:-0.145.0}
# 直接监听 443：客户端打的就是 443，容器内该端口空闲，且 iptables 重定向在
# 无 NET_ADMIN 的容器里不可用。
relay_port=${RELAY_PORT:-443}
turns=${TURNS:-1}
scenario=${SCENARIO:-}
model_catalog_only=${MODEL_CATALOG_ONLY:-0}
extra_args=""
compaction_reason=""
compaction_first_model=""
compaction_second_model=""
compaction_catalog=""

configure_compaction_models() {
  # 首模型必须跟随 Campaign 冻结的主模型，不能再写死成某个可能已被账号禁用的
  # 历史模型。第二模型默认选当前 0.149.1 目录中的非 Lite mini 变体；调用方如需
  # 替换，仍必须给出合法且不同的模型 slug，目录生成阶段会再次核验它真实存在。
  local secondary=${COMPACTION_SECOND_MODEL:-gpt-5.4-mini}
  if [[ ! $model =~ ^[A-Za-z0-9._-]+$ || ! $secondary =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "压缩场景模型只能包含字母、数字、点、下划线和连字符。" >&2
    exit 2
  fi
  if [[ $model == "$secondary" ]]; then
    echo "压缩场景的首模型与第二模型必须不同。" >&2
    exit 2
  fi
  compaction_first_model=$model
  compaction_second_model=$secondary
}

# **必须关掉的非必要流量**（§1.5.4）。不关的后果是实测的：一次三轮对话 58 个请求里
# 49 个是 ps/plugins/* 与 ps/mcp，真业务请求只有 6 个——连接数、请求数这类统计
# 全被污染，而这些流量按 §1.5.4 本就不构成必须复刻的形态。
#
# `plugins` 是 default_enabled: true 的 feature（features/src/lib.rs:1121），
# 用 --disable 关掉。遥测已在 config.toml 里关（analytics/otel）。
# plugins → ps/plugins/*、plugins/featured
# apps    → ps/mcp（Codex Apps 的 MCP 连接，Stable 且默认开，features/src/lib.rs:1073）
DISABLE_FEATURES=${DISABLE_FEATURES:-"plugins apps"}
disable_args=""
for f in $DISABLE_FEATURES; do disable_args="$disable_args --disable $f"; done
echo "已关闭的 feature：${DISABLE_FEATURES:-（无）}"
codex_bin=${CODEX_BIN:-/root/.local/bin/codex}

if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi
if [[ ! $codex_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "CODEX_VERSION 必须是三段数字。" >&2
  exit 2
fi
if [[ $model_catalog_only != 0 && $model_catalog_only != 1 ]]; then
  echo "MODEL_CATALOG_ONLY 必须是 0 或 1。" >&2
  exit 2
fi
if [[ $model_catalog_only == 0 && -z $scenario ]]; then
  echo "必须提供 SCENARIO。" >&2
  exit 2
fi
# 与 capturelib/scenarios.py 的 UPSTREAM_CAPACITY_* 保持同一语义与量级；那边覆盖
# official-core 路径，这里覆盖中继路径。两处都只认这一条错误消息。
UPSTREAM_CAPACITY_MESSAGE="Selected model is at capacity"
UPSTREAM_CAPACITY_RETRY_LIMIT=${UPSTREAM_CAPACITY_RETRY_LIMIT:-4}
UPSTREAM_CAPACITY_RETRY_DELAY=${UPSTREAM_CAPACITY_RETRY_DELAY:-20}
if [[ ! $UPSTREAM_CAPACITY_RETRY_LIMIT =~ ^[0-9]+$ || ! $UPSTREAM_CAPACITY_RETRY_DELAY =~ ^[0-9]+$ ]]; then
  echo "UPSTREAM_CAPACITY_RETRY_LIMIT／DELAY 必须是非负整数。" >&2
  exit 2
fi

# legacy compact 靠上下文越过该门限自动触发。Lite 轨道的触发不如主线稳——k46
# 整轮一次都没触发，job 却因收据仍能从 WS 帧取到模型而判 complete，直到 seal 前
# 扫描才发现 EP-014/EP-020 不可达。调用方可据轨道下调阈值提高确定性。
compact_token_limit=${COMPACT_TOKEN_LIMIT:-4000}
if [[ ! $compact_token_limit =~ ^[0-9]+$ ]]; then
  echo "COMPACT_TOKEN_LIMIT 必须是非负整数。" >&2
  exit 2
fi

# 中继必须覆盖整条最长场景。原固定 300 秒会在 compact 第七轮左右先行退出，
# wrapper 继续运行却只留下半截字节。上限防止调用方误把拼写错误变成超长驻留进程。
relay_timeout=${RELAY_TIMEOUT:-1800}
if [[ ! $relay_timeout =~ ^[0-9]+$ ]] || (( relay_timeout < 60 || relay_timeout > 7200 )); then
  echo "RELAY_TIMEOUT 必须是 60..7200 秒的整数。" >&2
  exit 2
fi

require_model_receipt=${REQUIRE_MODEL_CONDITION_RECEIPT:-0}
model_track=${MODEL_TRACK:-}
expect_lite=${EXPECT_USE_RESPONSES_LITE:-}
if [[ $require_model_receipt == 1 ]]; then
  if [[ $model_track != main && $model_track != lite ]]; then
    echo "MODEL_TRACK 必须是 main 或 lite。" >&2
    exit 2
  fi
  if [[ $expect_lite != true && $expect_lite != false ]]; then
    echo "EXPECT_USE_RESPONSES_LITE 必须是 true 或 false。" >&2
    exit 2
  fi
fi
if [[ $model_catalog_only == 1 && $require_model_receipt != 1 ]]; then
  echo "MODEL_CATALOG_ONLY=1 必须同时启用 REQUIRE_MODEL_CONDITION_RECEIPT=1。" >&2
  exit 2
fi

work_dir="$capture_root/runs/$run_id"
tls_dir="$work_dir/tls"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"

# 同一 RUN_ID 重跑会留下本轮未覆盖的高编号 conn 文件，后置 glob 可能误把旧命中
# 当成本轮证据。采集目录必须一次性、不可复用；需要重采就换新 RUN_ID。
if [[ -e $work_dir ]]; then
  echo "RUN_ID 已存在，拒绝混写旧样本：$work_dir" >&2
  exit 2
fi

relay_started=0
# CAPTURE_CLIENT_HELLO=1 时额外抓 loopback，取 CLI 发往中继的 ClientHello。
# 中继按 hosts 劫持到 127.0.0.1，但 SNI 由客户端填写、不受劫持影响，因此
# 抓到的域名就是 CLI 真实意图连接的域名——SPEC-EP-002 正是验这一点。
capture_client_hello=${CAPTURE_CLIENT_HELLO:-0}
tcpdump_started=0
pcap_dir="$capture_root/runs/${RUN_ID:-unset}/direct"
requirements_changed=0
requirements_backup="/tmp/codex-requirements-$run_id.toml"
memgen_home=""
file_upload_home=""
model_catalog_home=""
auth_backup=""
auth_before_sha256=""
# SCN-REALITY-01：目标场景的原始观测落在这里，供 build_scenario_facts.py 解析。
observation_dir="$capture_root/runs/${RUN_ID:-unset}/scenario-observations"

write_observation() {
  # 只写事实，不判成败；判定由收据构建器按契约做。
  local name=$1 payload=$2
  install -d -m 0700 "$observation_dir" 2>/dev/null || return 0
  printf '%s\n' "$payload" > "$observation_dir/$name" || return 0
  chmod 600 "$observation_dir/$name" 2>/dev/null || true
}

stop_relay() {
  if [[ $relay_started != 1 ]]; then
    return
  fi
  docker exec "$capture_container" pkill -TERM -f '[u]pstream_byte_relay.py' \
    >/dev/null 2>&1 || true
  for _ in $(seq 1 40); do
    if ! docker exec "$capture_container" pgrep -f '[u]pstream_byte_relay.py' \
      >/dev/null 2>&1; then
      relay_started=0
      return
    fi
    sleep 0.25
  done
  echo "⚠ 中继 10 秒内未优雅退出，发送 SIGKILL。" >&2
  docker exec "$capture_container" pkill -KILL -f '[u]pstream_byte_relay.py' \
    >/dev/null 2>&1 || true
  relay_started=0
}

stop_tcpdump() {
  if [[ $tcpdump_started != 1 ]]; then
    return
  fi
  # 先给 tcpdump 一点时间把缓冲写盘，再停；否则末尾握手可能丢失。
  sleep 1
  docker exec "$capture_container" pkill -TERM -f '[t]cpdump -i any' >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    if ! docker exec "$capture_container" pgrep -f '[t]cpdump -i any' >/dev/null 2>&1; then
      tcpdump_started=0
      return
    fi
    sleep 0.25
  done
  docker exec "$capture_container" pkill -KILL -f '[t]cpdump -i any' >/dev/null 2>&1 || true
  tcpdump_started=0
}

verify_pcap() {
  # pcap 此前是「写了就算」：无非空检查、无可解析校验。24 字节是 pcap 全局头长度，
  # 只有头没有包说明抓包从未生效或写盘失败，据此产出的场景收据是假的。
  if [[ $capture_client_hello != 1 ]]; then
    return 0
  fi
  local pcap="/capture/runs/$run_id/direct/traffic.pcap"
  local size
  size=$(docker exec "$capture_container" sh -c "stat -c '%s' '$pcap' 2>/dev/null || printf '0'")
  if (( size <= 24 )); then
    echo "❌ pcap 只有全局头（$size 字节），没有捕获到任何数据包。" >&2
    return 1
  fi
  if ! docker exec "$capture_container" tcpdump -nn -r "$pcap" -c 1 >/dev/null 2>&1; then
    echo "❌ pcap 无法解析出首个数据包。" >&2
    return 1
  fi
  return 0
}

extract_a14_tool_call() {
  # 从 `codex exec --json` 的 JSONL 事件流里取出成功完成的 Apps 工具调用。
  # 只提取，不推断：没有 completed 的工具调用就不写观测，收据构建器据此失败关闭——
  # k36 的形态正是模型压根没调用工具，却仍被记成 job 完成。
  local log=$1
  local extracted
  extracted=$(python3 - "$log" "${A14_TOOL_NAME:-save_site_version}" <<'PY'
import json, sys

path, expected = sys.argv[1], sys.argv[2]
found = None
try:
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # 事件流里工具调用的字段是**平铺在 item 下**的，没有 details 这一层：
            #   {"type":"item.completed","item":{"id":...,"type":"mcp_tool_call",
            #    "server":"codex_apps","tool":"sites.save_site_version","status":"failed"}}
            # 照 Rust 侧 ThreadItemDetails::McpToolCall 的嵌套结构去取会一无所获。
            item = event.get("item") or {}
            if item.get("type") != "mcp_tool_call":
                continue
            tool = str(item.get("tool") or "")
            server = str(item.get("server") or "")
            qualified = f"{server}.{tool}" if server else tool
            if expected not in {tool, qualified} and not tool.endswith(f".{expected}"):
                continue
            # 不要求 status == completed：采集刻意用不存在的 project_id，让上传链走完后
            # 在业务校验阶段失败（避免真的发布站点），工具因此必然报 failed。要的是
            # 「这次调用真实发生过」，故只排除仍在进行中的。
            if item.get("status") not in {"completed", "failed"}:
                continue
            found = {"tool_name": tool, "tool_call_id": str(item.get("id") or "")}
            if server:
                found["tool_server"] = server
except OSError:
    found = None
print(json.dumps(found, ensure_ascii=False) if found and found["tool_call_id"] else "")
PY
)
  if [[ -z $extracted ]]; then
    echo "⚠ 事件流中没有已完成的 ${A14_TOOL_NAME:-save_site_version} 工具调用，A14 不会产出成功收据。" >&2
    return 0
  fi
  write_observation "A14-tool-call.json" "$extracted"
}

a13_probe_jwt() {
  # 在容器内解析 access token 的 exp，只输出非秘密结论。
  # token 本体绝不离开容器、绝不落盘——收据里只保留 exp 与 token 摘要。
  # 必须带 -i：不分配 stdin 时 heredoc 传不进容器，python3 读到空脚本后静默退出，
  # 探针输出为空、字段全取不到，失败原因会被误报成「仍未进入刷新窗口」。
  docker exec -i "$capture_container" python3 - "${A13_REFRESH_WINDOW_MINUTES:-5}" <<'PY'
import base64, datetime, hashlib, json, sys

window = int(sys.argv[1])
try:
    with open("/root/.codex/auth.json") as handle:
        document = json.load(handle)
except OSError as error:
    print(json.dumps({"error": f"无法读取 auth.json：{error}"}))
    raise SystemExit(0)
token = ((document.get("tokens") or {}).get("access_token")) or ""
if not token:
    print(json.dumps({"error": "auth.json 没有 access_token"}))
    raise SystemExit(0)
try:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    expires_at = datetime.datetime.fromtimestamp(
        int(json.loads(base64.urlsafe_b64decode(payload))["exp"]),
        datetime.timezone.utc,
    )
except Exception as error:  # noqa: BLE001 - 解不出 exp 就无法按 R3 自然触发
    print(json.dumps({"error": f"无法解析 JWT exp：{error}"}))
    raise SystemExit(0)
now = datetime.datetime.now(datetime.timezone.utc)
# 与 should_refresh_proactively 同一判据：exp <= now + 窗口。
seconds_until_window = (expires_at - now).total_seconds() - window * 60
print(json.dumps({
    "exp_at_utc": expires_at.isoformat().replace("+00:00", "Z"),
    "observed_at_utc": now.isoformat().replace("+00:00", "Z"),
    "within_refresh_window": seconds_until_window <= 0,
    "seconds_until_window": int(seconds_until_window),
    "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
}))
PY
}

a13_field() {
  printf '%s' "$1" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin).get(sys.argv[1], "")
except ValueError:
    value = ""
print("true" if value is True else "" if value is False else value)
' "$2"
}

a13_derive_observations() {
  # 把刷新驱动的原始事件拆成收据契约要求的两份观测。只搬运驱动记下的事实：
  # 触发方式固定为 app_server_refresh_request，凭据前后摘要取自驱动的 before/after。
  local events=$1
  if [[ ! -s $events ]]; then
    echo "⚠ 刷新驱动没有落下事件日志，A13 不会产出成功收据。" >&2
    return 0
  fi
  local derived
  derived=$(python3 - "$events" <<'PY'
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        document = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(0)
before, after = document.get("before") or {}, document.get("after") or {}
required = ("exp_at_utc", "token_sha256", "auth_file_sha256")
if not all(before.get(k) for k in required) or not all(after.get(k) for k in required):
    raise SystemExit(0)
print(json.dumps({
    "jwt": {
        # exp 记的是刷新前那枚 token 的到期时刻，与触发方式一起构成来源证明。
        "exp_at_utc": before["exp_at_utc"],
        "observed_at_utc": document["observed_at_utc"],
        "trigger": document.get("trigger", ""),
        "token_sha256": before["token_sha256"],
    },
    "credential": {
        "before_sha256": before["auth_file_sha256"],
        "after_sha256": after["auth_file_sha256"],
        "capture_side_wrote_auth": False,
    },
}, ensure_ascii=False))
PY
)
  if [[ -z $derived ]]; then
    echo "⚠ 刷新事件缺少必需字段，A13 不会产出成功收据。" >&2
    return 0
  fi
  write_observation "A13-jwt-exp.json" \
    "$(printf '%s' "$derived" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["jwt"], ensure_ascii=False))')"
  write_observation "A13-credential-restore.json" \
    "$(printf '%s' "$derived" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["credential"], ensure_ascii=False))')"
}

a13_observation() {
  # 只保留收据契约要求的四个字段，多余的等待信息不进观测记录。
  printf '%s' "$1" | python3 -c '
import json, sys
document = json.load(sys.stdin)
print(json.dumps({
    key: document[key]
    for key in ("exp_at_utc", "observed_at_utc", "within_refresh_window", "token_sha256")
}, ensure_ascii=False))
'
}

restore_auth_json() {
  # 幂等：记录一次后清空 auth_backup，cleanup 再调一次不会重复执行。
  #
  # ⚠ 这里**刻意不还原 auth.json**。R3 之前脚本会改写 last_refresh，所以必须逐字
  # 还原；现在触发改为等 JWT 自然进入刷新窗口，采集侧一个字节都不写。而 CLI 刷新
  # 成功后会用**轮换后的** refresh_token 改写 auth.json
  # （`login/src/auth/manager.rs:2848-2861` → `persist_tokens` `:1496-1498`）。
  # 此时把旧备份灌回去，会丢掉新 refresh_token——在轮换语义下旧值已作废，采集账号
  # 将再也刷新不了，必须重新登录。
  #
  # 备份仍然保留，只作离线对照与人工兜底；这里只如实记录前后摘要。
  if [[ -z $auth_backup ]]; then
    return 0
  fi
  local auth_after_sha256=""
  auth_after_sha256=$(docker exec "$capture_container" sh -c \
    "sha256sum /root/.codex/auth.json | cut -d' ' -f1" 2>/dev/null || printf '')
  if [[ -z $auth_after_sha256 ]]; then
    echo "⚠ 无法读取采集后的 auth.json 摘要，A13 不会产出成功收据。" >&2
  elif [[ $auth_after_sha256 == "$auth_before_sha256" ]]; then
    echo "⚠ auth.json 采集前后一致，CLI 没有真正刷新落盘。" >&2
  fi
  echo "auth.json 备份保留在容器 $auth_backup（不自动回灌，避免作废轮换后的 refresh_token）。"
  write_observation "A13-credential-restore.json" \
    "{\"before_sha256\":\"$auth_before_sha256\",\"after_sha256\":\"$auth_after_sha256\",\"capture_side_wrote_auth\":false}"
  auth_backup=""
}

prewarm_model_catalog() {
  # 需要模型条件收据的任务必须先取得一份完整在线目录响应。codex exec、
  # debug models 与单独 model/list 都可能只返回内置目录；这里用隔离 HOME 和
  # CODEX_HOME 启动官方 app-server，仅执行 initialize + thread/start 来触发
  # OnlineIfUncached，并保持进程存活到 relay 原文中的 /models HTTP 200 刷盘。
  model_catalog_home="/tmp/codex-model-catalog-$run_id"
  docker exec "$capture_container" sh -c \
    "rm -rf '$model_catalog_home' && install -d -m 0700 '$model_catalog_home'"
  local attempt home
  for attempt in 1 2 3; do
    home="$model_catalog_home/$attempt"
    docker exec "$capture_container" sh -c \
      "install -d -m 0700 '$home' && install -m 0600 /root/.codex/auth.json '$home/auth.json'"
    if docker exec -e HOME="$home" -e CODEX_HOME="$home" \
      "$capture_container" timeout 150 \
      python3 "$capture_tool_root/drive_codex_model_catalog.py" \
      --codex-bin "$codex_bin" \
      --codex-version "$codex_version" \
      --model "$model" \
      --expect-use-responses-lite "$expect_lite" \
      --relay-dir "/capture/runs/$run_id/relay" \
      --output "/capture/runs/$run_id/model-catalog-prewarm.json" \
      --timeout 120; then
      return 0
    fi
    echo "⚠ 在线模型目录预热第 $attempt 次未形成完整原始响应。" >&2
    sleep 2
  done
  echo "❌ 三次在线模型目录预热均未形成完整 /models HTTP 200 原始响应。" >&2
  return 1
}

scrub_relay_bytes() {
  # 中继原文含真实认证信息；即使只是模型目录诊断，也必须在离开采集机前等长脱敏。
  local scrubbed_relay="$work_dir/relay-scrubbed"
  rm -rf -- "$scrubbed_relay"
  python3 "$scrub_tool" \
    --src "$work_dir/relay" \
    --dst "$scrubbed_relay" \
    --verify
  rm -rf -- "$work_dir/relay"
  mv -- "$scrubbed_relay" "$work_dir/relay"
}

cleanup() {
  local status=$?
  # ⚠ 清理函数必须在 set +e 下跑。脚本顶部是 `set -Eeuo pipefail`，而 EXIT trap 里
  # 任何一条命令返回非 0 都会让 cleanup 当场中止，后面的恢复全部落空。
  #
  # 实证（k37）：`bash -x` 显示 cleanup 执行到第一个 `stop_tcpdump` 的 return 就停，
  # 连 stop_relay 都没跑到，「环境已恢复」这句从未出现在任何一轮 job 日志里；采集
  # 容器的 /etc/hosts 因此一直残留着劫持，中继停掉后所有出站都打向 127.0.0.1 被拒。
  # 这个残留会静默污染此后一切在该容器里的观测——A13 的首次手工复现就栽在这上面。
  set +e
  # hosts 与临时 CA 最先还原：它们是污染后续采集的唯一途径，必须排在最前面。
  for h in ${RELAY_HOSTS:-chatgpt.com}; do
    docker exec "$capture_container" sh -c \
      "grep -v \" $h\$\" /etc/hosts > /tmp/.hr && cat /tmp/.hr > /etc/hosts && rm -f /tmp/.hr" \
      >/dev/null 2>&1 || true
  done
  docker exec "$capture_container" rm -f /usr/local/share/ca-certificates/relay-ca.crt >/dev/null 2>&1 || true
  docker exec "$capture_container" update-ca-certificates --fresh >/dev/null 2>&1 || true
  # 环境已还原，再停进程与做其余清理。
  stop_tcpdump
  stop_relay
  if [[ $requirements_changed == 1 ]]; then
    if docker exec "$capture_container" test -f "$requirements_backup"; then
      docker exec "$capture_container" sh -c \
        "mkdir -p /etc/codex && mv '$requirements_backup' /etc/codex/requirements.toml" \
        >/dev/null 2>&1 || true
    else
      docker exec "$capture_container" rm -f /etc/codex/requirements.toml >/dev/null 2>&1 || true
    fi
  fi
  if [[ -n $memgen_home ]]; then
    docker exec "$capture_container" rm -rf -- "$memgen_home" >/dev/null 2>&1 || true
  fi
  if [[ -n $file_upload_home ]]; then
    docker exec "$capture_container" rm -rf -- "$file_upload_home" >/dev/null 2>&1 || true
  fi
  if [[ -n $model_catalog_home ]]; then
    docker exec "$capture_container" rm -rf -- "$model_catalog_home" >/dev/null 2>&1 || true
  fi
  restore_auth_json
  docker exec "$capture_container" rm -f /tmp/codex-guardian-probe.txt >/dev/null 2>&1 || true
  echo "环境已恢复：中继已停止，hosts 与系统信任库中的临时 CA 均已还原。"
  exit $status
}
# 编排器超时走 _terminate_process 发信号，只挂 EXIT 时还原不保证执行——
# A13 的 auth.json 会永久停留在被篡改的状态。
trap cleanup EXIT INT TERM

install -d -m 0700 "$work_dir" "$tls_dir"

# 中继面向客户端的证书——**多域名 SAN**。
# 验 SPEC-EP-002（域名全集）与 SPEC-EP-012（live 双出站：chatgpt.com 建会话 +
# api.openai.com 承载 sideband）都要求同时劫持多个域名，单域名证书握手会失败。
# 中继按 SNI 选上游（upstream_byte_relay.py 的 _on_sni）。
RELAY_HOSTS=${RELAY_HOSTS:-chatgpt.com}
san=$(echo "$RELAY_HOSTS" | tr ' ' '\n' | sed 's/^/DNS:/' | paste -sd, -)
first_host=$(echo "$RELAY_HOSTS" | awk '{print $1}')
echo "中继覆盖域名：$RELAY_HOSTS"
openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/relay.key" \
  -out "$tls_dir/relay.csr" -subj "/CN=$first_host" >/dev/null 2>&1
printf 'subjectAltName=%s\n' "$san" > "$tls_dir/relay.ext"
openssl x509 -req -in "$tls_dir/relay.csr" -CA "$ca_full" -CAkey "$ca_full" \
  -CAcreateserial -out "$tls_dir/relay.crt" -days 1 -sha256 \
  -extfile "$tls_dir/relay.ext" >/dev/null 2>&1
chmod 600 "$tls_dir"/*

# 上游真实 IP：direct 模式必须绕开被劫持的 hosts，否则中继会连回自身
# **必须在劫持 hosts 之前**逐域名解析——之后再解析只会拿到 127.0.0.1。
#
# ⚠ 先强制清一遍 hosts：上一轮若异常退出，cleanup 可能没跑全，
# 残留的劫持条目会让预解析读到 127.0.0.1，中继于是连回自身
# （表现为所有连接「只有上行、零下行」，实测踩过两次）。
for h in $RELAY_HOSTS; do
  docker exec "$capture_container" sh -c \
    "grep -v \" $h\$\" /etc/hosts > /tmp/.pre && cat /tmp/.pre > /etc/hosts && rm -f /tmp/.pre" \
    >/dev/null 2>&1 || true
done

upstream_map=""
for h in $RELAY_HOSTS; do
  hip=$(docker exec "$capture_container" getent ahostsv4 "$h" | head -1 | cut -d' ' -f1)
  # 解析成回环即说明 hosts 仍被污染，继续跑只会采到一堆空连接
  if [[ -z $hip || $hip == 127.* ]]; then
    echo "❌ $h 解析为 ${hip:-空}——hosts 仍被劫持，中止。" >&2
    exit 1
  fi
  upstream_map="${upstream_map:+$upstream_map,}$h=$hip"
  echo "  解析 $h → $hip"
done
upstream_ip=$(docker exec "$capture_container" getent ahostsv4 chatgpt.com | head -1 | cut -d' ' -f1)
if [[ -z $upstream_ip ]]; then
  echo "无法解析 chatgpt.com 的上游 IP。" >&2
  exit 1
fi
echo "上游真实 IP：$upstream_ip"

# CA 装系统信任库（不设环境变量，避免切到 rustls）
docker cp "$ca_cert" "$capture_container:/usr/local/share/ca-certificates/relay-ca.crt" >/dev/null
docker exec "$capture_container" update-ca-certificates >/dev/null 2>&1

# 起中继（--assume-alpn 留空 = 不 offer ALPN，与官方 native-tls 实测一致）
relay_intervention_args=()
if [[ $require_model_receipt == 1 ]]; then
  # Codex 0.149.1 把 /models 的建连与读取合计硬限制为 5 秒；DMIT 到 Cloudflare
  # 偶发一次 TLS 握手就接近 4.9 秒。中继先在该计时器启动前建立同一真实上游
  # TLS，首个官方模型目录请求只复用连接，不改变任何应用字节或网络选路。
  relay_intervention_args+=(--preconnect-upstream --preconnect-timeout 15)
fi
if [[ ${RELAY_FORCE_WS_FALLBACK_426:-0} == 1 ]]; then
  relay_intervention_args+=(--force-ws-fallback-426)
fi
if [[ -n ${RELAY_INJECT_TURN_STATE:-} ]]; then
  if [[ ! $RELAY_INJECT_TURN_STATE =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "RELAY_INJECT_TURN_STATE 只能包含字母、数字、点、下划线和连字符。" >&2
    exit 2
  fi
  relay_intervention_args+=(--inject-turn-state "$RELAY_INJECT_TURN_STATE")
fi
if [[ -n ${RELAY_INJECT_WS_TURN_STATE:-} ]]; then
  if [[ ! $RELAY_INJECT_WS_TURN_STATE =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "RELAY_INJECT_WS_TURN_STATE 只能包含字母、数字、点、下划线和连字符。" >&2
    exit 2
  fi
  relay_intervention_args+=(--inject-ws-turn-state "$RELAY_INJECT_WS_TURN_STATE")
fi
if [[ ${RELAY_SYNTHESIZE_REALTIME_CALL:-0} == 1 && -n ${RELAY_SYNTHESIZE_REALTIME_CALL_AFTER:-} ]]; then
  echo "realtime 立即合成与延迟合成开关不能同时启用。" >&2
  exit 2
fi
if [[ ${RELAY_SYNTHESIZE_REALTIME_CALL:-0} == 1 ]]; then
  relay_intervention_args+=(--synthesize-realtime-call)
fi
if [[ -n ${RELAY_SYNTHESIZE_REALTIME_CALL_AFTER:-} ]]; then
  if [[ ${RELAY_SYNTHESIZE_REALTIME_CALL_AFTER} != 1 ]]; then
    echo "RELAY_SYNTHESIZE_REALTIME_CALL_AFTER 当前只允许 1。" >&2
    exit 2
  fi
  relay_intervention_args+=(
    --synthesize-realtime-call-after "$RELAY_SYNTHESIZE_REALTIME_CALL_AFTER"
  )
fi
if [[ -n ${RELAY_RETRY_PROBE:-} ]]; then
  case "$RELAY_RETRY_PROBE" in
    keepalive-500|disconnect) ;;
    *) echo "RELAY_RETRY_PROBE 只能是 keepalive-500 或 disconnect。" >&2; exit 2 ;;
  esac
  relay_intervention_args+=(--retry-probe "$RELAY_RETRY_PROBE")
  if [[ -n ${RELAY_RETRY_PROBE_TARGET:-} ]]; then
    case "$RELAY_RETRY_PROBE_TARGET" in
      models|responses) ;;
      *) echo "RELAY_RETRY_PROBE_TARGET 只能是 models 或 responses。" >&2; exit 2 ;;
    esac
    relay_intervention_args+=(--retry-probe-target "$RELAY_RETRY_PROBE_TARGET")
  fi
fi
if [[ $capture_client_hello == 1 ]]; then
  # 容器缺 tcpdump 时 `docker exec -d` 静默返回 0，整轮跑完才发现没有 pcap。
  # 候选侧早有这道预检（run_candidate_aux_capture.sh），官方侧此前是缺的。
  if ! docker exec "$capture_container" sh -c 'command -v tcpdump' >/dev/null 2>&1; then
    echo "❌ capture 容器缺少 tcpdump，无法形成 A11／A13／A14 的 SNI pcap。" >&2
    exit 1
  fi
  install -d -m 0700 "$pcap_dir"
  # `-i any` 而不是 `-i lo`：响应返回的区域上传主机不在 RELAY_HOSTS 里就不被
  # hosts 劫持，流量走真实网卡，回环抓包完全看不到。按端口捕获所有主机，与候选侧
  # 及 SPEC-TLS-003 的既有先例一致。pcap linktype 变为 LINUX_SLL／SLL2，
  # pcap_clienthello.py 已支持，无需改解析器。
  docker exec -d "$capture_container" sh -c \
    "tcpdump -i any -s 0 -U -w /capture/runs/$run_id/direct/traffic.pcap 'tcp port $relay_port' \
     > /capture/runs/$run_id/direct/tcpdump.log 2>&1"
  tcpdump_started=1
  sleep 1
fi
docker exec -d "$capture_container" python3 \
  "$capture_tool_root/upstream_byte_relay.py" \
  --cert "/capture/runs/$run_id/tls/relay.crt" --key "/capture/runs/$run_id/tls/relay.key" \
  --mode direct --port "$relay_port" \
  --upstream-host chatgpt.com --upstream-ip "$upstream_ip" \
  --upstream-map "$upstream_map" \
  --output "/capture/runs/$run_id/relay" --timeout "$relay_timeout" \
  "${relay_intervention_args[@]}"
relay_started=1
if [[ $require_model_receipt == 1 ]]; then
  relay_ready=0
  for _ in $(seq 1 400); do
    if docker exec "$capture_container" test -s \
      "/capture/runs/$run_id/relay/preconnect-ready.json"; then
      relay_ready=1
      break
    fi
    if ! docker exec "$capture_container" pgrep -f '[u]pstream_byte_relay.py' \
      >/dev/null 2>&1; then
      break
    fi
    sleep 0.05
  done
  if [[ $relay_ready != 1 ]]; then
    echo "❌ 模型目录上游 TLS 预连接未在 20 秒内就绪。" >&2
    exit 1
  fi
else
  sleep 2
fi

# hosts 劫持须在中继起来之后
for h in $RELAY_HOSTS; do
  docker exec "$capture_container" sh -c \
    "grep -v \" $h\$\" /etc/hosts > /tmp/.hp && cat /tmp/.hp > /etc/hosts && rm -f /tmp/.hp"
  docker exec "$capture_container" sh -c "printf '127.0.0.1 $h\n' >> /etc/hosts"
done

if [[ $model_catalog_only == 1 ]]; then
  echo "=== 仅采集在线模型目录（$model_track / $model）==="
  prewarm_model_catalog
  # 目录命令已经关闭连接；先停止中继并刷盘 relay.json，再封存脱敏产物。
  stop_relay
  scrub_relay_bytes
  docker exec "$capture_container" jq \
    '{schema_version, mode, upstream_host, connection_count: (.connections | length),
      valid_count: ([.connections[] | select(.valid == true)] | length)}' \
    "/capture/runs/$run_id/relay/relay.json"
  printf 'run_id=%s\n' "$run_id"
  exit 0
fi

case "$scenario" in
  tool)
    # 用无副作用、参数固定的本地动作诱导工具调用（验证方案 §8.2）
    prompt='请用 shell 工具执行一条命令：echo probe-123。执行后只回复 TOOL-OK。' ;;
  conn-retry)
    # CONN-001：内置 OpenAI provider 先由受控 426 切到 HTTP，再只发一轮普通
    # Responses。RELAY_RETRY_PROBE 对该 POST 注入一次 500 或断连。
    prompt='请只回复 OK，不要调用任何工具。' ;;
  turnstate-compact)
    # 先由模型产生工具调用，再在同一 turn 内超过自动压缩阈值；关闭 V2 后会走
    # legacy /responses/compact。配合中继注入 turn-state，直接验证 compact 回送头。
    prompt='请用 shell 工具执行一条命令：echo turnstate-compact-probe。执行后只回复 TOOL-OK。'
    extra_args="--disable remote_compaction_v2 -c model_auto_compact_token_limit=$compact_token_limit" ;;
  legacy-compact-default)
    # SPEC-EP-014 legacy-default-headers／SPEC-EP-020 legacy-observed-subset 的
    # 默认样本：与 turnstate-compact 同样走 legacy /responses/compact，但**不**由
    # 中继注入 turn-state，也不打开任何 Stage::Experimental feature。
    # 于是 installation-id 之后的第三槽既不是 x-codex-turn-state 也不是
    # x-codex-beta-features，自然落到 x-codex-window-id——这正是判据要的默认线序。
    # 无任何干预，属自然基线。调用方必须不设 RELAY_INJECT_TURN_STATE。
    prompt='请用 shell 工具执行一条命令：echo legacy-compact-default-probe。执行后只回复 TOOL-OK。'
    extra_args="--disable remote_compaction_v2 -c model_auto_compact_token_limit=$compact_token_limit" ;;
  ws-optional-missing)
    # SPEC-WS-002 optional-missing-covered 要"至少保留一个缺少可选头后的独立扰动
    # 样本"（断言只是 count_at_least=1）。
    #
    # WS 握手头里天然可选的是 x-codex-beta-features：它由
    # build_model_client_beta_features_header 拼装，只收录 Stage::Experimental 的
    # feature 或 RemoteCompactionV2。而 remote_compaction_v2 是 Stable 且
    # default_enabled=true，所以默认握手**带**该头（值为 remote_compaction_v2），
    # 判据的 default 期望序里也确实有它。把它关掉，该头整条消失——这就是判据要的
    # "可选头缺失"扰动，且不改任何 WS 传输参数。
    #
    # 只关一个 feature，不注入头、不改 provider，因此仍是官方默认 WS 形态减去一项。
    prompt='请只回复 OK，不要做任何其他事。'
    extra_args='--disable remote_compaction_v2' ;;
  ws-default)
    # SPEC-WS-002 默认线序必须来自没有 runtime_metrics 等条件 feature 的独立样本。
    # 只执行普通 WS turn，不注入头、不切 provider、不改 feature。
    prompt='请只回复 OK，不要做任何其他事。' ;;
  legacy-compact-beta)
    # SPEC-EP-014 legacy-beta-slot 的 beta 样本：第三槽为 x-codex-beta-features。
    #
    # 该头由 `core/src/session/mod.rs` 的 build_model_client_beta_features_header
    # 拼装，只收录 `Stage::Experimental` 的 feature 或 RemoteCompactionV2。而 legacy
    # compact 本身要求关掉 RemoteCompactionV2，所以只能靠前者——0.147 全树
    # Stage::Experimental 仅剩 `network_proxy` 一个。
    #
    # ⚠ 属 I 类：靠打开 feature 让该头出现，采到的是"当此头存在时官方怎么写线序"。
    # 但**这不是本次新增的干预**：0.145 的同名函数逐字相同、Experimental 集合也同样
    # 只有 network_proxy，基线判据当初只可能这么采。不在同等条件下采 0.147，
    # "采不到"会被误读成行为变化。判据是 all_list_prefix，只断言槽位不断言头值。
    #
    # network_proxy 不污染出站：下游全在 core/src/sandboxing/，且还要
    # permission_profile.network_sandbox_policy().is_enabled() 才真正启用代理；
    # 而 beta 头只看 features.enabled()。调用方必须不设 RELAY_INJECT_TURN_STATE。
    prompt='请用 shell 工具执行一条命令：echo legacy-compact-beta-probe。执行后只回复 TOOL-OK。'
    extra_args="--disable remote_compaction_v2 --enable network_proxy -c model_auto_compact_token_limit=$compact_token_limit" ;;
  http-response-plain)
    # SPEC-BODY-002 要证明「Responses 尊重压缩开关」，因此需要一个**真正关闭压缩**
    # 的负样本：判据 responses-plain 断言选中的请求不带 content-encoding。
    #
    # 此前 A04 的 precondition 写着 enable_request_compression=false，但没有任何 job
    # 真的关过它——所有 relay 样本都是 zstd，标签也只有 zstd。R7 给该条 select 补
    # labels.compression=plain 之后就变成恒定选不到，seal 的 selector 可达性会失败
    # 关闭。补上本 job 才让声明与采集对齐。
    #
    # enable_request_compression 是 Stage::Stable、default_enabled=true 的 feature
    # （features/src/lib.rs:1085），关掉它只影响请求体是否 zstd，不改端点、头序或
    # 传输形态，与 ws-optional-missing 关 remote_compaction_v2 是同款做法。
    prompt='请只回复 OK，不要做任何其他事。'
    extra_args="--disable enable_request_compression -c model_provider=openai-http-probe -c model_providers.openai-http-probe.name=OpenAI -c model_providers.openai-http-probe.base_url=https://chatgpt.com/backend-api/codex -c model_providers.openai-http-probe.wire_api=responses -c model_providers.openai-http-probe.supports_websockets=false -c model_providers.openai-http-probe.requires_openai_auth=true -c model_providers.openai-http-probe.http_headers.version=$codex_version" ;;
  http-response)
    # 用官方 CLI + OpenAI OAuth 的干净 HTTP Responses 分支补原始 h1 字节。
    # 内置 provider 支持 WS，无法靠已移除的 feature 开关强制 HTTP；这里创建一个
    # 仍要求 OpenAI OAuth、但明确不支持 WS 的 provider。除 provider 能力位外不注入
    # 额外 header，避免 ws-special-headers 场景中的 origin 等人工头污染线序。
    prompt='请只回复 OK，不要做任何其他事。'
    extra_args="-c model_provider=openai-http-probe -c model_providers.openai-http-probe.name=OpenAI -c model_providers.openai-http-probe.base_url=https://chatgpt.com/backend-api/codex -c model_providers.openai-http-probe.wire_api=responses -c model_providers.openai-http-probe.supports_websockets=false -c model_providers.openai-http-probe.requires_openai_auth=true -c model_providers.openai-http-probe.http_headers.version=$codex_version" ;;
  residency-us)
    # residency 不是环境变量，也不是普通 config.toml 键；0.145.0 只从系统
    # /etc/codex/requirements.toml（或企业托管层）读取 enforce_residency。
    # 临时写入后由 cleanup 原样恢复，采的是官方受管配置分支。
    docker exec "$capture_container" sh -c "mkdir -p /etc/codex"
    if docker exec "$capture_container" test -f /etc/codex/requirements.toml; then
      docker exec "$capture_container" cp /etc/codex/requirements.toml "$requirements_backup"
    fi
    requirements_changed=1
    docker exec "$capture_container" sh -c \
      "printf 'enforce_residency = \"us\"\\n' > /etc/codex/requirements.toml"
    prompt='请只回复 OK，不要做任何其他事。' ;;
  image)
    prompt='请生成一张图片：一只红色的狐狸，简单画风。' ;;
  image-repeat-tui)
    prompt='__PROMPT_TUI_IMAGE__' ;;
  image-edit)
    # 采 SPEC-EP-001/BODY-006 缺的那一半：`images/edits`。
    #
    # 分派点在 `ext/image-generation/src/tool.rs:270`——按
    # (referenced_image_paths 是否为空, num_last_images_to_include) 二元组分派：
    #   (true , None)   → Generate → images/generations
    #   (false, None)   → Edit     → images/edits      ← 本场景要走的
    # 所以必须让模型带上 `referenced_image_paths`，工作目录里就得有**真实可读的
    # 图片文件**：`image_url()`（同文件 :427）会经沙箱读盘再转 data URL，
    # 路径不存在会直接 RespondToModel 报错，根本发不出请求。
    prompt='__IMAGE_EDIT__' ;;
  search)
    # 采 alpha-search（SPEC-EP-008／EP-015）。
    #
    # 早期只给搜索 prompt 采不到 `{base}/alpha/search`——上一轮实测确认它与模型侧
    # 内置 web_search 确是两条独立链路。调用点在 `ext/web-search/src/tool.rs:139`
    # （`SearchEndpoint::search()` → `POST alpha/search`），注册条件见
    # `core/src/tools/spec_plan.rs:854` 的 `standalone_web_search_enabled()`：
    #   namespace_tools ∧ provider.capabilities().web_search
    #     ∧ (model_info.use_responses_lite ∨ Feature::StandaloneWebSearch)
    # 前两项内置 openai provider 默认为真；第三项里 `standalone_web_search` 是
    # Stage::UnderDevelopment、default_enabled=false，必须显式打开——官方自己的
    # 集成测试（app-server/tests/suite/v2/web_search.rs:110）也是这么开的。
    # 不依赖任何交互，与 runtime-metrics 同款做法。
    prompt='请联网搜索一下 2026 年 Rust 1.9 版本有哪些新特性，简要总结三点。'
    extra_args='--enable standalone_web_search' ;;
  search-repeat-tui)
    prompt='__PROMPT_TUI_SEARCH__' ;;
  realtime-webrtc)
    # 采 SPEC-EP-012 的 live 双出站。第一跳打 chatgpt.com 的 realtime/calls，
    # 上游接受 SDP 后才会建第二跳（sideband WS → api.openai.com）。
    # 必须配 RELAY_HOSTS="chatgpt.com api.openai.com"，否则看不到第二跳。
    prompt='__REALTIME__' ;;
  runtime-metrics)
    # 采 SPEC-HDR-008 的第四项 `x-responsesapi-include-timing-metrics`。
    # 它由 `Feature::RuntimeMetrics` 控制（`features/src/lib.rs:925-929`，
    # default_enabled: false、Stage::UnderDevelopment），发送点在
    # `core/src/client.rs:1097-1101`。**不依赖任何交互**，`--enable` 打开即可，
    # 比走 TUI 的子代理路径可靠得多。
    prompt='请只回复 OK，不要做任何其他事。'
    extra_args='--enable runtime_metrics' ;;
  ws-special-headers)
    # 采 SPEC-WS-003：tungstenite 对 `origin` / `sec-websocket-protocol` 两个头
    # 做大小写改写（→ `Origin` / `Sec-WebSocket-Protocol`）。
    #
    # 官方默认配置不发这两个头，但 provider 的 `http_headers`
    # （`model-provider-info/src/lib.rs:116`，:218 处并入请求头）会被合并进
    # WS 握手。用 `-c` 注入即可——**不依赖任何交互**。
    # ⚠ 属 I 类：采到的是"当这两个头存在时官方怎么写"，不是官方默认形态。
    #
    # ⚠ **不能改内置 `openai` provider**——config 加载期就拒绝：
    #   "model_providers contains reserved built-in provider IDs: `openai`"
    # 必须另建一个 id（这里叫 `openai-probe`）并用 `model_provider` 指过去。
    # 实测这样仍走 OAuth，认证不受影响。
    # `version` 头要手工补上：它本是内置 provider 的 `http_headers` 自带的
    # （`model-provider-info/src/lib.rs:340-344`），换 provider 就没了。
    prompt='请只回复 OK，不要做任何其他事。'
    # ⚠ `supports_websockets` 与 `requires_openai_auth` **必须显式设 true**：
    # 二者在内置 openai provider 里是 true（`model-provider-info/src/lib.rs:362`），
    # 自建 provider 默认为 false，于是 `responses_websocket_enabled()`
    # （`core/src/client.rs:930`）返回假，整条链退回 HTTP——
    # 而 tungstenite 的大小写改写**只发生在 WS 握手**上，走 HTTP 就验不了本条。
    # 用 WS_MODE=0 可以刻意关掉，采 HTTP 路径的对照（SPEC-BODY-004 用的就是它）。
    _ws_opts='-c model_providers.openai-probe.supports_websockets=true -c model_providers.openai-probe.requires_openai_auth=true'
    [[ ${WS_MODE:-1} == 0 ]] && _ws_opts=''
    extra_args="-c model_provider=openai-probe -c model_providers.openai-probe.name=OpenAI -c model_providers.openai-probe.base_url=https://chatgpt.com/backend-api/codex -c model_providers.openai-probe.wire_api=responses $_ws_opts -c model_providers.openai-probe.http_headers.version=$codex_version -c model_providers.openai-probe.http_headers.origin=https://chatgpt.com -c model_providers.openai-probe.http_headers.sec-websocket-protocol=codex-probe" ;;
  review-tui)
    # 采 SPEC-HDR-008 的子代理 header（`x-openai-subagent` 等）。
    #
    # `/review` 的命令描述是 "review my current changes and find issues"
    # （`tui/src/slash_command.rs:89`）——**它 review 的是 git 改动**。
    # 工作目录不是 git 仓库、或没有未提交改动时，命令发出去也无事可做，
    # 一个模型请求都不会产生（实测 relay-review1/2 均如此）。
    # 故本场景必须先把 cwd 初始化成 git 仓库、提交一次基线、再制造改动。
    prompt='__REVIEW_TUI__' ;;
  guardian-tui)
    # guardian 只在非 bypass 的真实审批链里触发。让模型请求在工作区外创建一个
    # 无害探针文件，auto_review 会启动 guardian 子代理审核该动作。
    prompt='__GUARDIAN_TUI__' ;;
  memgen)
    # 同一 app-server 内先构造持久线程 A，再由线程 B 启动 memories pipeline。
    # 使用专用临时 CODEX_HOME，避免历史 session/memory 污染候选集合；只在容器内部
    # 复制 OAuth 与基础配置，采完由 cleanup 删除。
    prompt='__MEMGEN__' ;;
  compact-tui)
    # 走 TUI 触发 CompactionReason::UserRequested（SPEC-EP-023）。
    # 斜杠命令只在 TUI 输入框解析，exec 收到会当普通文本发给模型（SPEC-EP-024），
    # 所以这里用 pty 驱动真正的 TUI。判据不看屏幕，看中继抓到的端点与 body。
    prompt='__COMPACT_TUI__' ;;
  compact-exec-negative)
    # SPEC-EP-024 洁净负例：exec 表面不解析斜杠命令，必须把字面量
    # `/compact` 作为普通用户 message 发给 Responses。这里不添加前后缀，避免
    # “普通 message”结论被提示词改写污染；采后由 extract_capture_records.py
    # 同时拒绝 compaction_trigger 和 /responses/compact。
    prompt='/compact' ;;
  compact)
    # 灌满上下文触发 CompactionReason::ContextLimit——三条可行路径里唯一能拿到
    # **自然基线**的（另两条需交互式 TUI，或降 manifest 窗口属 I 类干预）。
    #
    # 注意 /compact 走不通：斜杠命令只在 TUI 输入框解析，codex exec 会把它当普通
    # 文本发给模型，模型"照字面理解"做段摘要，看着像压缩其实不是（SPEC-EP-024）。
    prompt='__COMPACT_FILL__' ;;
  comp-hash-changed)
    # CompHashChanged 只要求换模前后的 comp_hash 不同：第二轮开始前由
    # maybe_run_previous_model_inline_compact 触发。
    #
    # 原先直接借生产目录里 gpt-5.6-luna -> gpt-5.4 的自然跨组（3000 -> 2911），
    # 但那让本场景的成败绑死在某个特定模型的上游可用性上——实测旧账号不支持
    # gpt-5.4，第一轮直接失败。改为与 model-downshift 同样的受控目录：首模型
    # 跟随 Campaign 的 MODEL，第二模型使用目录中已核验的非 Lite mini 变体。
    # 这是明确记录的 I 类触发干预；官方 CLI、OAuth、V2 压缩实现与出站均不替换。
    prompt='__COMPACTION_REASON__'
    compaction_reason='comp_hash_changed'
    configure_compaction_models
    compaction_catalog="/capture/runs/$run_id/comp-hash-catalog.json"
    docker exec "$capture_container" jq \
      --arg first "$compaction_first_model" \
      --arg second "$compaction_second_model" '
      [.models[] | select(.slug == $first or .slug == $second)] as $selected
      | if (($selected | length) != 2)
          or any($selected[]; .use_responses_lite != false)
        then error("压缩场景模型目录缺少两个唯一的非 Lite 模型")
        else {models: [
          $selected[]
          | if .slug == $first
            then .comp_hash = "comp-hash-probe-first"
            else .comp_hash = "comp-hash-probe-second"
            end
        ]}
        end' /root/.codex/models_cache.json > "$work_dir/comp-hash-catalog.json"
    chmod 600 "$work_dir/comp-hash-catalog.json" ;;
  model-downshift)
    # ModelDownshift 需旧窗口 > 新窗口且当前 token 已超新模型阈值。默认阈值约
    # 115k，纯为触发灌入该体量会造成数十万 input token。这里把两个 hash 设成
    # 相同，并把受控目录的旧／新模型窗口冻结为 272000 -> 128000、自动压缩
    # 阈值冻结为 16000 -> 8000。两类字段缺一不可：官方实现先要求旧窗口严格大于
    # 新窗口，随后才用新模型阈值判断是否需要 ModelDownshift 压缩。
    # 先导样本中首轮约 9089 token、降档压缩后约 7249，故 8000 能触发降档且不会
    # 立刻再触发 ContextLimit；最终仍由提取器拒绝任何额外压缩原因。
    # 这是明确记录的 I 类触发干预；官方 CLI、OAuth、V2 压缩实现与出站均不替换。
    prompt='__COMPACTION_REASON__'
    compaction_reason='model_downshift'
    configure_compaction_models
    compaction_catalog="/capture/runs/$run_id/model-downshift-catalog.json"
    docker exec "$capture_container" jq \
      --arg first "$compaction_first_model" \
      --arg second "$compaction_second_model" '
      [.models[] | select(.slug == $first or .slug == $second)] as $selected
      | if (($selected | length) != 2)
          or any($selected[]; .use_responses_lite != false)
        then error("压缩场景模型目录缺少两个唯一的非 Lite 模型")
        else {models: [
          $selected[]
          | .comp_hash = "downshift-probe"
          | if .slug == $first
            then .context_window = 272000
              | .auto_compact_token_limit = 16000
            else .context_window = 128000
              | .auto_compact_token_limit = 8000
            end
        ]}
        end' /root/.codex/models_cache.json > "$work_dir/model-downshift-catalog.json"
    chmod 600 "$work_dir/model-downshift-catalog.json" ;;
  oauth-refresh)
    # 采 SPEC-EP-002 的 auth-sni：官方 CLI 的 OAuth token 刷新走 auth.openai.com。
    #
    # 触发方式按 R3 定案，**不再改写 last_refresh**。0.147 的
    # `should_refresh_proactively`（`login/src/auth/manager.rs:2762-2783`）先解 access
    # token JWT 的 exp，能解出就直接按 `exp <= now + 5min` 判定并返回；只有解不出
    # 有效期时才回退到 last_refresh。k36 改 last_refresh 之所以一次刷新都没触发，
    # 正是因为正常 JWT 走的是前一条路径。
    #
    # 现在改为等待 JWT 自然进入 5 分钟刷新窗口
    # （`CHATGPT_ACCESS_TOKEN_REFRESH_WINDOW_MINUTES = 5`），窗口内 CLI 的任何一次
    # 取认证（`auth()`，`:2238-2251`）都会自己发出真实 refresh。凭据一字不改。
    auth_backup="/tmp/codex-auth-$run_id.json"
    docker exec "$capture_container" sh -c \
      "cp /root/.codex/auth.json '$auth_backup' && chmod 600 '$auth_backup'"
    # 采集前后的原始字节摘要必须相等。本场景不写 auth.json，但 CLI 刷新成功后会自己
    # 改写它——还原链据此把账号恢复到采集前状态，两摘要不等即不产出 A13 成功收据。
    auth_before_sha256=$(docker exec "$capture_container" sh -c \
      "sha256sum '$auth_backup' | cut -d' ' -f1")
    jwt_probe=$(a13_probe_jwt)
    if [[ -n $(a13_field "$jwt_probe" error) ]]; then
      echo "❌ 无法取得 access token 的 exp：$(a13_field "$jwt_probe" error)" >&2
      exit 1
    fi
    if [[ -z $(a13_field "$jwt_probe" exp_at_utc) ]]; then
      # 探针没输出等于什么都没测到，绝不能当成可采继续。
      echo "❌ JWT 探针无输出，无法确认凭据状态（检查容器与 auth.json 可读性）。" >&2
      exit 1
    fi
    # 触发方式：走官方 app-server 的 `account/read {refreshToken:true}`，它落到
    # auth_manager.refresh_token() 且不检查 exp，因此不必等令牌自然到期（有效期
    # 10 天，等待完全不现实）。k37 实测：auth.json 被轮换改写、抓包出现
    # auth.openai.com 的 ClientHello。仍被排除的是伪造 last_refresh。
    prompt='__AUTH_REFRESH__' ;;
  file-upload)
    # 采 SPEC-EP-002 的生产文件上传三跳：
    #   POST /backend-api/files -> PUT <服务端预签名 URL>
    #   -> POST /backend-api/files/{file_id}/uploaded
    #
    # 入口必须是保留名 `codex_apps` 下、由真实 /ps/mcp 返回且声明
    # `_meta["openai/fileParams"]` 的工具。当前账号的内置 Sites
    # `save_site_version` 声明 archive 为 fileParams；自定义 MCP 不可替代。
    #
    # 使用确定不存在的一次性 project_id，使 fileParams 预上传完整结束后，真正的
    # Sites 保存调用在业务校验阶段失败。这样只创建本任务已获授权的 OpenAI 文件
    # 状态，绝不会发布、部署或覆盖真实站点。
    if [[ " $DISABLE_FEATURES " == *" apps "* ]]; then
      echo "❌ file-upload 必须启用内置 apps；请设置 DISABLE_FEATURES=plugins。" >&2
      exit 2
    fi
    file_upload_home="/tmp/ep002-file-upload-$run_id"
    docker exec "$capture_container" sh -c \
      "rm -rf '$file_upload_home' && mkdir -p '$file_upload_home/site/.openai'"
    docker exec "$capture_container" sh -c \
      "printf '{\"project_id\":\"ep002-probe-do-not-exist\"}\n' > '$file_upload_home/site/.openai/hosting.json' && \
       printf '<!doctype html><title>EP002 probe</title>\n' > '$file_upload_home/site/index.html' && \
       tar -C '$file_upload_home/site' -czf '$file_upload_home/ep002-probe.tar.gz' ."
    # k37 实测：内置 Sites 工具默认不直接暴露在会话里，模型需要先做一次工具检索
    # 才能取到它。原提示词写的是「不要调用任何其他工具」，反而把这条路堵死——模型
    # 明确回答「若要发现并调用它，需要先用别的工具做检索/加载，这会违反你的要求」，
    # 于是一个请求都没发。现在显式放行检索步骤，同时保留不创建/不发布的安全约束。
    prompt="这是经过授权的官方客户端出站采集。目标：调用一次内置 Sites 的 ${A14_TOOL_NAME:-save_site_version} 工具。如果该工具尚未在当前会话中直接暴露，请先执行必要的工具检索或加载步骤把它取出来——这些检索调用是允许且必要的。取到后只调用它一次，参数必须是：project_id=ep002-probe-do-not-exist，commit_sha=0000000000000000000000000000000000000000，archive=$file_upload_home/ep002-probe.tar.gz。即使工具报错也立即停止，不要重试、不要创建站点、不要发布或部署。" ;;
  *) echo "未知 SCENARIO: $scenario" >&2; exit 2 ;;
esac

if [[ $require_model_receipt == 1 ]]; then
  echo "=== 在线模型目录预热（$model_track / $model）==="
  prewarm_model_catalog
fi

echo "=== 场景 $scenario，$turns 轮 ==="
if [[ $prompt == "__REALTIME__" ]]; then
  # A11 先跑一次完整自然请求。若当前上游仍以 session.model 拒绝，再由同一个中继
  # 对第二次官方请求受控返回 200，让官方 CLI 自己派生 sideband。两次事件分别落盘，
  # build_scenario_facts.py 必须把自然 400 与受控 200 同时写入复合收据，绝不把后者
  # 冒充自然成功。若未来自然分支恢复，第一轮成功后不会再运行受控分支。
  realtime_status=0
  realtime_events="/capture/runs/$run_id/scenario-observations/A11-realtime-events.json"
  realtime_log="$work_dir/realtime-driver.log"
  if [[ -n ${RELAY_SYNTHESIZE_REALTIME_CALL_AFTER:-} ]]; then
    realtime_events="/capture/runs/$run_id/scenario-observations/A11-realtime-live-events.json"
    realtime_log="$work_dir/realtime-live-driver.log"
  fi
  # shellcheck disable=SC2086
  docker exec "$capture_container" timeout 120 python3 \
    "$capture_tool_root/drive_codex_realtime.py" \
    --codex-bin "$codex_bin" \
    --codex-version "$codex_version" \
    --model "$model" --transport webrtc --output-modality audio \
    --realtime-version "${REALTIME_VERSION:-v3}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --events-output "$realtime_events" \
    --hold "${REALTIME_HOLD:-20}" > "$realtime_log" 2>&1 || realtime_status=$?
  tail -10 "$realtime_log" || true
  if (( realtime_status != 0 )); then
    echo "⚠ realtime 自然驱动以 $realtime_status 退出，交由证据构建器判定。" >&2
  fi

  if [[ -n ${RELAY_SYNTHESIZE_REALTIME_CALL_AFTER:-} ]]; then
    live_events_host="$work_dir/scenario-observations/A11-realtime-live-events.json"
    live_success=0
    if jq -e '
      [.notifications[].method] as $methods
      | ([range(0; $methods | length)
          | select($methods[.] == "thread/realtime/started"
                   or $methods[.] == "thread/realtime/sdp")] | max) as $target
      | ($target != null)
        and ([range(0; $target + 1)
              | select($methods[.] == "thread/realtime/error")] | length == 0)
    ' "$live_events_host" >/dev/null 2>&1; then
      live_success=1
      echo "realtime 自然分支已成立，不运行受控分支。"
    fi
    if (( live_success == 0 )); then
      realtime_status=0
      # shellcheck disable=SC2086
      docker exec "$capture_container" timeout 120 python3 \
        "$capture_tool_root/drive_codex_realtime.py" \
        --codex-bin "$codex_bin" \
        --codex-version "$codex_version" \
        --model "$model" --transport webrtc --output-modality audio \
        --realtime-version "${REALTIME_VERSION:-v3}" \
        ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
        --events-output "/capture/runs/$run_id/scenario-observations/A11-realtime-events.json" \
        --hold "${REALTIME_HOLD:-20}" > "$work_dir/realtime-controlled-driver.log" 2>&1 || realtime_status=$?
      tail -10 "$work_dir/realtime-controlled-driver.log" || true
      if (( realtime_status != 0 )); then
        echo "⚠ realtime 受控分支驱动以 $realtime_status 退出，交由证据构建器判定。" >&2
      fi
    fi
  fi
elif [[ $prompt == "__AUTH_REFRESH__" ]]; then
  # A13 目标路径：由官方 CLI 自己发出 OAuth token 刷新，退出码不吞。
  auth_refresh_status=0
  docker exec "$capture_container" timeout 180 python3 \
    "$capture_tool_root/drive_codex_auth_refresh.py" \
    --codex-bin "$codex_bin" --codex-version "$codex_version" \
    --events-output "/capture/runs/$run_id/scenario-observations/A13-auth-events.json" \
    > "$work_dir/auth-refresh-driver.log" 2>&1 || auth_refresh_status=$?
  tail -8 "$work_dir/auth-refresh-driver.log" || true
  if (( auth_refresh_status != 0 )); then
    echo "⚠ 刷新驱动以 $auth_refresh_status 退出，A13 目标分支未成立。" >&2
  fi
  # 从驱动的原始事件派生收据契约要求的两份观测；只搬运事实，不补写。
  a13_derive_observations "$work_dir/scenario-observations/A13-auth-events.json"
elif [[ $prompt == "__COMPACT_TUI__" ]]; then
  ctx_opt=""
  [[ -n ${CONTEXT_WINDOW:-} ]] && ctx_opt="--context-window $CONTEXT_WINDOW"
  # shellcheck disable=SC2086
  docker exec "$capture_container" python3 \
    "$capture_tool_root/drive_codex_tui.py" \
    --codex-bin "$codex_bin" \
    --model "$model" --cwd /tmp/tui-probe $ctx_opt \
    --warmup "${TUI_WARMUP:-请用一段话介绍什么是 TCP 三次握手。}" \
    --slash "${TUI_SLASH:-/compact}" \
    --warmup-ready "${TUI_READY:-READY}" \
    --slash-hold "${TUI_HOLD:-75}" \
    ${TUI_ENABLE:+$(for f in $TUI_ENABLE; do printf -- '--enable %s ' "$f"; done)} \
    ${TUI_DISABLE:+--disable $TUI_DISABLE} \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --log "/capture/runs/$run_id/tui.log" 2>&1 | tail -12 || true
elif [[ $prompt == "__PROMPT_TUI_IMAGE__" ]]; then
  docker exec "$capture_container" python3 \
    "$capture_tool_root/drive_codex_tui.py" \
    --codex-bin "$codex_bin" \
    --model "$model" --cwd /tmp/tui-probe \
    --prompt '请调用图片生成工具生成一张红色狐狸的简单插画；必须实际调用工具。' \
    --prompt '请再次调用图片生成工具生成一张蓝色鲸鱼的简单插画；必须实际调用工具。' \
    --prompt-hold "${TUI_HOLD:-180}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --log "/capture/runs/$run_id/tui.log" 2>&1 | tail -16 || true
elif [[ $prompt == "__PROMPT_TUI_SEARCH__" ]]; then
  docker exec "$capture_container" python3 \
    "$capture_tool_root/drive_codex_tui.py" \
    --codex-bin "$codex_bin" \
    --model "$model" --cwd /tmp/tui-probe \
    --prompt '请联网搜索 Rust 1.90 的发布日期；必须实际调用联网搜索工具，只回复日期。' \
    --prompt '请再次联网搜索 Python 3.14 的发布日期；必须实际调用联网搜索工具，只回复日期。' \
    --prompt-hold "${TUI_HOLD:-150}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --log "/capture/runs/$run_id/tui.log" 2>&1 | tail -16 || true
elif [[ $prompt == "__GUARDIAN_TUI__" ]]; then
  docker exec "$capture_container" python3 \
    "$capture_tool_root/drive_codex_tui.py" \
    --codex-bin "$codex_bin" \
    --model "$model" --cwd /work --no-bypass \
    --approval-policy on-request --sandbox-mode workspace-write \
    --config 'approvals_reviewer="auto_review"' \
    --prompt '请用 shell 在工作区外执行：printf guardian-probe > /tmp/codex-guardian-probe.txt。必须实际执行命令。' \
    --prompt-hold "${TUI_HOLD:-240}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --log "/capture/runs/$run_id/tui.log" 2>&1 | tail -16 || true
elif [[ $prompt == "__MEMGEN__" ]]; then
  memgen_home="/tmp/codex-memgen-$run_id"
  docker exec "$capture_container" sh -c \
    "rm -rf '$memgen_home' && mkdir -m 700 '$memgen_home' && \
     cp /root/.codex/auth.json '$memgen_home/auth.json' && \
     cp /root/.codex/config.toml '$memgen_home/config.toml'"
  docker exec -e CODEX_HOME="$memgen_home" "$capture_container" timeout 540 python3 \
    "$capture_tool_root/drive_codex_memgen.py" \
    --codex-bin "$codex_bin" \
    --codex-version "$codex_version" \
    --model "$model" --cwd /tmp/memgen-probe \
    --relay-dir "/capture/runs/$run_id/relay" --hold "${MEMGEN_HOLD:-360}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    2>&1 | tail -30 || true
elif [[ $prompt == "__COMPACTION_REASON__" ]]; then
  catalog_opt=()
  [[ -n $compaction_catalog ]] && catalog_opt=(--model-catalog-json "$compaction_catalog")
  docker exec "$capture_container" timeout 420 python3 \
    "$capture_tool_root/drive_codex_compaction_reason.py" \
    --codex-bin "$codex_bin" \
    --codex-version "$codex_version" \
    --first-model "$compaction_first_model" --second-model "$compaction_second_model" \
    --cwd "/tmp/$scenario-probe" \
    "${catalog_opt[@]}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    2>&1 | tail -30
elif [[ $prompt == "__COMPACT_FILL__" ]]; then
  # 用**真实编码工作流**灌上下文，而非让模型写长文。
  #
  # 理由不只是"像不像"：Codex 是编码助手，真实使用中 input 数组是
  # message + 工具调用 + 工具输出**混合**的。用写作文的方式灌，采到的 input 全是
  # message（实测 relay-compact2 的分布为 message×46 / reasoning×7 / 工具×0），
  # 形态本身就偏离了真实使用——拿这种样本去验压缩链路，结论不可靠。
  #
  # 改为让它读文件、跑命令、改代码：上下文增长来自文件内容与命令输出，这才是
  # Codex 真实的 token 消耗来源。
  fill_rounds=${FILL_ROUNDS:-10}
  # model_context_window 是官方标准配置项（core/src/config/mod.rs:633），压缩链路
  # 四处都读它。调小它逼出 CompactionReason::ContextLimit，比继续灌 token 省得多。
  # 这不属 I 类污染：改的是「多大算超限」，压缩本身走的仍是真实代码路径；但该值
  # 非默认，证据中须标注。
  ctx_opt=""
  [[ -n ${CONTEXT_WINDOW:-} ]] && ctx_opt="-c model_context_window=$CONTEXT_WINDOW"
  echo "上下文窗口设定：${CONTEXT_WINDOW:-默认}"

  # 准备一个有真实体量的代码库供其探索——用官方源码树本身
  work="/tmp/compact-probe"
  docker exec "$capture_container" sh -c "rm -rf $work && mkdir -p $work" >/dev/null 2>&1

  tasks=(
    "看一下当前目录有哪些文件，简要说明这个项目是做什么的。"
    "读一下最主要的那个源文件，讲讲它的核心逻辑。"
    "这个项目有哪些依赖？列出来并说明各自的用途。"
    "找出代码里所有的错误处理逻辑，评估是否完备。"
    "帮我在项目里新建一个 utils.py，写几个常用的字符串处理函数。"
    "给刚才写的函数补上单元测试，并运行测试看是否通过。"
    "测试跑完后，检查一下代码风格是否统一，需要的话调整。"
    "把项目里所有 Python 文件的行数统计出来，按大小排序。"
    "回顾一下我们刚才做的所有修改，逐个说明改动理由。"
    "基于目前的代码，提出三个可以继续优化的方向，并说明优先级。"
  )

  echo "--- 准备工作目录 ---"
  docker exec "$capture_container" sh -c \
    "cd $work && printf 'import sys\n\ndef main():\n    print(\"probe\")\n\nif __name__ == \"__main__\":\n    main()\n' > app.py && printf 'requests>=2.0\npytest>=7.0\n' > requirements.txt && printf '# Compact Probe\n\n一个用于测试的最小项目。\n' > README.md" >/dev/null 2>&1

  for i in $(seq 1 "$fill_rounds"); do
    task="${tasks[$((i-1))]}"
    [[ -z $task ]] && break
    echo "--- 第 $i 轮：$task ---"
    if [[ $i == 1 ]]; then
      docker exec -w "$work" "$capture_container" timeout 240 "$codex_bin" exec \
        $ctx_opt $disable_args --model "$model" --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
        "$task" 2>&1 | tail -2 || true
    else
      docker exec -w "$work" "$capture_container" timeout 240 "$codex_bin" exec resume --last \
        $ctx_opt $disable_args --model "$model" --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
        "$task" 2>&1 | tail -2 || true
    fi
  done
elif [[ $prompt == "__REVIEW_TUI__" ]]; then
  work="/tmp/review-probe"
  echo "--- 准备 git 工作区（/review 依赖 git diff）---"
  docker exec "$capture_container" sh -c "
    rm -rf $work && mkdir -p $work && cd $work &&
    git init -q 2>/dev/null &&
    git config user.email probe@local && git config user.name probe &&
    printf 'def divide(a, b):\n    if b == 0:\n        raise ValueError(\"b must not be zero\")\n    return a / b\n' > calc.py &&
    git add -A && git commit -qm baseline &&
    printf 'def divide(a, b):\n    return a / b\n\n\ndef load(path):\n    return open(path).read()\n' > calc.py &&
    git --no-pager diff --stat
  "
  # shellcheck disable=SC2086
  docker exec "$capture_container" python3 \
    "$capture_tool_root/drive_codex_tui.py" \
    --codex-bin "$codex_bin" \
    --model "$model" --cwd "$work" \
    --warmup "${TUI_WARMUP:-请只回复 READY，不要做任何其他事。}" \
    --warmup-ready "${TUI_READY:-READY}" \
    --slash "${TUI_SLASH:-/review}" \
    --slash-hold "${TUI_HOLD:-240}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --log "/capture/runs/$run_id/tui.log" 2>&1 | tail -12 || true

elif [[ $prompt == "__IMAGE_EDIT__" ]]; then
  work="/tmp/imgedit-probe"
  docker exec "$capture_container" sh -c "rm -rf $work && mkdir -p $work"

  # 预置一张**真图**。用 Python 手写最小 PNG，不引入 Pillow 依赖：
  # 64×64 纯色，带合法 IHDR/IDAT/IEND 与 CRC。`load_for_prompt_bytes`
  # （tool.rs:443）会真的解码它，构造不合法的字节会在那一步失败。
  docker exec "$capture_container" python3 -c "
import zlib, struct
w = h = 64
raw = b''.join(b'\x00' + bytes([200, 80, 40] * w) for _ in range(h))
def chunk(tag, data):
    c = tag + data
    return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c))
png = (b'\x89PNG\r\n\x1a\n'
       + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(raw))
       + chunk(b'IEND', b''))
open('$work/source.png','wb').write(png)
print('预置图片', len(png), '字节')
"

  for i in $(seq 1 "$turns"); do
    echo "--- 第 $i 轮（image-edit）---"
    # 提示词要同时满足两件事：明确指向那个**绝对路径**（参数类型是
    # AbsolutePathBuf，相对路径不合法），且是"改这张图"而非"画一张新的"——
    # 后者模型会走 Generate 分支，采回来的还是 images/generations。
    # shellcheck disable=SC2086
    docker exec -w "$work" "$capture_container" timeout 240 "$codex_bin" exec \
      --model "$model" --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
      $disable_args \
      "请编辑已有图片 $work/source.png：在这张纯色图上加一只白色的小猫。注意是**编辑这张已存在的图**，把它的路径作为参考图片传给图片工具，不要重新生成一张全新的图。" \
      2>&1 | tail -4 || true
  done
else
  for i in $(seq 1 "$turns"); do
    echo "--- 第 $i 轮 ---"
    exec_status=0
    # A14 需要从事件流里取真实的工具调用记录（`ExecThreadItem.details.mcp_tool_call`
    # 含 server／tool／status），人读格式取不到，故只对该场景加 --json。
    exec_json_args=""
    [[ $scenario == "file-upload" ]] && exec_json_args="--json"
    # 上游对高需求模型间歇返回「无容量」，与画像、账号额度和客户端行为都无关：
    # 同一模型几分钟内可以一次失败一次成功。`capturelib/scenarios.py` 早已对
    # official-core 路径做了这类有限重试，但中继路径一直没有，于是 Lite 专项
    # （固定 gpt-5.6-luna，恰好是容量最紧张的一档）会被一次波动整轮打死——k43 的
    # lite-legacy-compact-default 连续三个 attempt 都死在这里，一个 compact 请求
    # 都没发出。
    #
    # 与既有实现同款语义：**只认这一种错误**，其余失败一律原样上报，不放宽边界。
    capacity_remaining=$UPSTREAM_CAPACITY_RETRY_LIMIT
    while true; do
      exec_status=0
      # shellcheck disable=SC2086 —— extra_args 需按空格拆成多个参数
      docker exec "$capture_container" timeout 180 "$codex_bin" exec \
        --model "$model" --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
        $exec_json_args $disable_args $extra_args "$prompt" \
        > "$work_dir/exec-$i.log" 2>&1 || exec_status=$?
      if ! grep -qF "$UPSTREAM_CAPACITY_MESSAGE" "$work_dir/exec-$i.log"; then
        break
      fi
      if (( capacity_remaining <= 0 )); then
        echo "❌ 上游连续 $UPSTREAM_CAPACITY_RETRY_LIMIT 次报「$UPSTREAM_CAPACITY_MESSAGE」，放弃本轮。" >&2
        break
      fi
      capacity_remaining=$((capacity_remaining - 1))
      echo "⚠ 上游报无容量，${UPSTREAM_CAPACITY_RETRY_DELAY}s 后重试（剩余 $capacity_remaining 次）。" >&2
      sleep "$UPSTREAM_CAPACITY_RETRY_DELAY"
    done
    tail -3 "$work_dir/exec-$i.log" || true
    if [[ $scenario == "file-upload" ]]; then
      extract_a14_tool_call "$work_dir/exec-$i.log"
    fi
    # A13／A14 是 SCN-REALITY-01 的目标场景，驱动失败必须留痕；其余场景沿用
    # 既有的宽松语义，本轮不批量改动。
    if (( exec_status != 0 )) && [[ $scenario == "oauth-refresh" || $scenario == "file-upload" ]]; then
      echo "⚠ 第 $i 轮 codex exec 以 $exec_status 退出，$scenario 目标分支未必成立。" >&2
    fi
  done
fi

# 发 SIGTERM 后由中继的信号处理器取消并等待连接任务，再写 relay.json。
# 直接 -9 会丢掉尚未完成的连接元数据与最终清单。
stop_relay

if ! docker exec "$capture_container" test -s "/capture/runs/$run_id/relay/relay.json"; then
  echo "❌ 中继未写出 relay.json，样本不完整。" >&2
  exit 1
fi

# 目标请求必须真的发出，否则这一轮采的是"跑完了但没触发"的空样本。
#
# 此前这件事没有任何显式检查，只是**碰巧**被模型条件收据挡住——收据要从 HTTP
# POST /responses(/compact) 的请求体里取 model，目标请求没发出时它自然报错，job
# 因此判 failed（k43 的 lite compact 正是这样暴露的）。后来给收据补上 WS 帧路径，
# WS 会话里取得到模型，这个隐含门禁就消失了：k46 的 lite compact 一个 compact
# 请求都没发，job 却是 complete，直到 seal 前的 selector 扫描才发现
# EP-014/legacy-default-headers 与 EP-020 不可达。
#
# 收据的语义是"模型条件成立"，不是"目标分支已触发"。两件事必须各自显式表达。
if [[ -n ${REQUIRE_REQUEST_PATH:-} ]]; then
  echo "=== 目标请求校验（$REQUIRE_REQUEST_METHOD ${REQUIRE_REQUEST_PATH}）==="
  if ! docker exec "$capture_container" python3 - \
      "/capture/runs/$run_id/relay" \
      "${REQUIRE_REQUEST_METHOD:-POST}" \
      "$REQUIRE_REQUEST_PATH" <<'PY'
import sys
from pathlib import Path

relay, method, target = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
found = 0
for path in sorted(relay.glob("conn*.client_to_upstream.bin")):
    data = path.read_bytes()
    offset = 0
    while offset < len(data):
        end = data.find(b"\r\n\r\n", offset)
        if end < 0:
            break
        head = data[offset:end].decode("latin-1", "replace")
        lines = head.split("\r\n")
        if not lines or " HTTP/1." not in lines[0]:
            break
        parts = lines[0].split(" ")
        if len(parts) >= 2 and parts[0] == method and parts[1].split("?", 1)[0] == target:
            found += 1
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers.setdefault(name.strip().lower(), value.strip())
        if headers.get("transfer-encoding", "").lower() == "chunked":
            break
        try:
            body = int(headers.get("content-length", "0"))
        except ValueError:
            body = 0
        offset = end + 4 + body
print(f"命中 {found} 条 {method} {target}")
sys.exit(0 if found else 1)
PY
  then
    echo "❌ 本轮未发出目标请求 ${REQUIRE_REQUEST_METHOD:-POST} ${REQUIRE_REQUEST_PATH}，样本不成立。" >&2
    exit 1
  fi
fi

# tcpdump 此前只在 EXIT 陷阱里停，导致后置处理全都发生在它仍在运行时，pcap
# 从未在流程内被确认可用。这里显式收口，再校验。
stop_tcpdump
if ! verify_pcap; then
  exit 1
fi

# 中继原始字节包含真实 Authorization/Cookie；在证据离开采集机前必须等长脱敏。
# 先写入新目录并复扫，再替换原目录，避免留下未脱敏副本；字节长度和偏移保持不变。
scrub_relay_bytes

# 双轨模型条件收据：字段全部从脱敏后的最终 relay 原始字节提取。编排器只声明
# 预期坐标，不能根据 job 退出码补写 model、Lite 或 fallback 状态。
if [[ $require_model_receipt == 1 ]]; then
  echo "=== 模型条件收据（$model_track / $model）==="
  # 收据解析与抓包运行时属于同一个冻结环境。Responses 请求可能使用 zstd；若在
  # ARM64 宿主机直接运行，会把宿主 Python 的偶然依赖状态混入 Campaign，并在宿主
  # 未安装 zstandard 时让完整样本失败。受管镜像显式提供 python3-zstandard，且仓库
  # 与 evidence root 均以相同绝对路径挂载，因此在 capture-cli 内生成最终收据。
  docker exec "$capture_container" \
    python3 "$capture_tool_root/model_condition_receipts.py" \
    --run-root "$work_dir" \
    --output "$work_dir/model-condition-receipt.json" \
    --job-id "${SCENARIO_JOB_ID:?模型收据要求 SCENARIO_JOB_ID}" \
    --run-id "$run_id" \
    --track "$model_track" \
    --model "$model" \
    --model-catalog-prewarm "$work_dir/model-catalog-prewarm.json" \
    --expect-use-responses-lite "$expect_lite"
fi

# 还原必须早于场景事实构建：A13 的成功收据要求 auth.json 逐字还原的前后摘要相等，
# 而还原结果此前只在 EXIT 陷阱里产生，那时事实早已构建完。
restore_auth_json

# SCN-REALITY-01：从脱敏后的最终字节提取目标协议分支是否真实成立。
# 证据不足即退出非 0 且不产出成功事实，编排器据此判 job 失败——
# 「脚本退出 0 且证据目录非空」不再等于场景成立。
case "$scenario" in
  realtime-webrtc) target_scenario="A11" ;;
  oauth-refresh)   target_scenario="A13" ;;
  file-upload)     target_scenario="A14" ;;
  *)               target_scenario="" ;;
esac
if [[ -n $target_scenario ]]; then
  echo "=== 场景真实性事实（$target_scenario）==="
  if ! python3 "$capture_tool_root/build_scenario_facts.py" \
    --scenario "$target_scenario" \
    --job-id "${SCENARIO_JOB_ID:-official-relay-$scenario}" \
    --run-id "$run_id" \
    --run-root "$work_dir"; then
    echo "❌ $target_scenario 目标协议分支未成立，不产出场景收据。" >&2
    exit 1
  fi
fi

if [[ $prompt == "__COMPACTION_REASON__" ]]; then
  echo "=== 压缩原因最小脱敏证据 ==="
  docker exec "$capture_container" python3 \
    "$capture_tool_root/extract_compaction_reason.py" \
    --relay-dir "/capture/runs/$run_id/relay" \
    --expected-reason "$compaction_reason" \
    --output "/capture/runs/$run_id/compaction-reason.json"
fi

# ── 采集后立即自检样本完整性 ──
# response_only 或 idle 非 0 = 丢数据（HTTP/1.1 的响应必然与请求同连接，
# 有响应没请求只能是上行丢了）。不在这里拦，就要等到审核阶段才发现，
# 那时结论已经写进文档了。
echo "=== 样本完整性自检 ==="
docker exec "$capture_container" python3 \
  "$capture_tool_root/check_sample_integrity.py" \
  "/capture/runs/$run_id/relay" || echo "⚠ 样本不洁净，谨慎使用" >&2
docker exec "$capture_container" jq \
  '{schema_version, mode, upstream_host, connection_count: (.connections | length),
    valid_count: ([.connections[] | select(.valid == true)] | length)}' \
  "/capture/runs/$run_id/relay/relay.json" 2>/dev/null || echo "无产物"
printf 'run_id=%s\n' "$run_id"
