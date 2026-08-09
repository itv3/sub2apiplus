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
model=${MODEL:-gpt-5.6-luna}
codex_version=${CODEX_VERSION:-0.145.0}
# 直接监听 443：客户端打的就是 443，容器内该端口空闲，且 iptables 重定向在
# 无 NET_ADMIN 的容器里不可用。
relay_port=${RELAY_PORT:-443}
turns=${TURNS:-1}
scenario=${SCENARIO:?必须提供 SCENARIO: tool|conn-retry|turnstate-compact|http-response|residency-us|image|image-edit|image-repeat-tui|search|search-repeat-tui|compact|compact-exec-negative|compact-tui|comp-hash-changed|model-downshift|review-tui|guardian-tui|memgen|realtime-webrtc|runtime-metrics|ws-special-headers|oauth-refresh|file-upload}
extra_args=""
compaction_reason=""
compaction_first_model=""
compaction_second_model=""
compaction_catalog=""

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
auth_backup=""

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
  docker exec "$capture_container" pkill -TERM -f '[t]cpdump -i lo' >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    if ! docker exec "$capture_container" pgrep -f '[t]cpdump -i lo' >/dev/null 2>&1; then
      tcpdump_started=0
      return
    fi
    sleep 0.25
  done
  docker exec "$capture_container" pkill -KILL -f '[t]cpdump -i lo' >/dev/null 2>&1 || true
  tcpdump_started=0
}

cleanup() {
  local status=$?
  stop_tcpdump
  stop_relay
  # hosts 与临时 CA 一律还原，避免污染后续采集
  for h in ${RELAY_HOSTS:-chatgpt.com}; do
    docker exec "$capture_container" sh -c \
      "grep -v \" $h\$\" /etc/hosts > /tmp/.hr && cat /tmp/.hr > /etc/hosts && rm -f /tmp/.hr" \
      >/dev/null 2>&1 || true
  done
  docker exec "$capture_container" rm -f /usr/local/share/ca-certificates/relay-ca.crt >/dev/null 2>&1 || true
  docker exec "$capture_container" update-ca-certificates --fresh >/dev/null 2>&1 || true
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
  if [[ -n $auth_backup ]]; then
    # 登录态必须原样还原：本场景只改 last_refresh 触发一次刷新，不改变账号绑定。
    docker exec "$capture_container" sh -c \
      "test -f '$auth_backup' && cat '$auth_backup' > /root/.codex/auth.json && \
       chmod 600 /root/.codex/auth.json && rm -f '$auth_backup'" >/dev/null 2>&1 || true
  fi
  docker exec "$capture_container" rm -f /tmp/codex-guardian-probe.txt >/dev/null 2>&1 || true
  echo "环境已恢复：中继已停止，hosts 与系统信任库中的临时 CA 均已还原。"
  exit $status
}
trap cleanup EXIT

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
if [[ ${RELAY_SYNTHESIZE_REALTIME_CALL:-0} == 1 ]]; then
  relay_intervention_args+=(--synthesize-realtime-call)
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
  install -d -m 0700 "$pcap_dir"
  docker exec -d "$capture_container" sh -c \
    "tcpdump -i lo -s 0 -U -w /capture/runs/$run_id/direct/traffic.pcap 'tcp port $relay_port' \
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
  --output "/capture/runs/$run_id/relay" --timeout 300 \
  "${relay_intervention_args[@]}"
relay_started=1
sleep 2

# hosts 劫持须在中继起来之后
for h in $RELAY_HOSTS; do
  docker exec "$capture_container" sh -c \
    "grep -v \" $h\$\" /etc/hosts > /tmp/.hp && cat /tmp/.hp > /etc/hosts && rm -f /tmp/.hp"
  docker exec "$capture_container" sh -c "printf '127.0.0.1 $h\n' >> /etc/hosts"
done

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
    extra_args='--disable remote_compaction_v2 -c model_auto_compact_token_limit=4000' ;;
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
    # 试探 alpha-search（SPEC-EP-015）。`--search` 的 help 写的是
    # "the native Responses `web_search` tool is available"——**未必**等于打
    # `{base}/alpha/search`：那条路来自 ext/web-search 扩展，与模型侧内置
    # web_search 工具可能是两条独立链路。本次采集正是要分辨这一点：
    #   - 若出现 POST /backend-api/codex/alpha/search → EP-015 拿到基线
    #   - 若只在 /responses 的 tools 里声明 web_search → 说明 alpha-search
    #     另有触发方式，须回到源码找调用点
    prompt='请联网搜索一下 2026 年 Rust 1.9 版本有哪些新特性，简要总结三点。'
    extra_args='' ;;
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
    # 但那让本场景的成败绑死在某个特定模型的上游可用性上——实测 gpt-5.6-luna
    # 间歇性连第一轮 turn 都跑不完（turn/completed 状态 failed、压缩事件为 0），
    # 整轮 official 采集因此反复作废。改为与 model-downshift 同样的受控目录：
    # 只把两个 hash 设成不同，模型固定用采集主模型及其 mini 变体。
    # 这是明确记录的 I 类触发干预；官方 CLI、OAuth、V2 压缩实现与出站均不替换。
    prompt='__COMPACTION_REASON__'
    compaction_reason='comp_hash_changed'
    compaction_first_model='gpt-5.4'
    compaction_second_model='gpt-5.4-mini'
    compaction_catalog="/capture/runs/$run_id/comp-hash-catalog.json"
    docker exec "$capture_container" jq '
      {models: [
        .models[]
        | select(.slug == "gpt-5.4" or .slug == "gpt-5.4-mini")
        | if .slug == "gpt-5.4"
          then .comp_hash = "comp-hash-probe-first"
          else .comp_hash = "comp-hash-probe-second"
          end
      ]}' /root/.codex/models_cache.json > "$work_dir/comp-hash-catalog.json"
    chmod 600 "$work_dir/comp-hash-catalog.json" ;;
  model-downshift)
    # ModelDownshift 需旧窗口 > 新窗口且当前 token 已超新模型阈值。默认阈值约
    # 115k，纯为触发灌入该体量会造成数十万 input token。这里保留生产模型的
    # 272k -> 128k 窗口关系，只把两个 hash 设成相同、把新模型阈值降为 8000。
    # 先导样本中首轮约 9089 token、降档压缩后约 7249，故 8000 能触发降档且不会
    # 立刻再触发 ContextLimit；最终仍由提取器拒绝任何额外压缩原因。
    # 这是明确记录的 I 类触发干预；官方 CLI、OAuth、V2 压缩实现与出站均不替换。
    prompt='__COMPACTION_REASON__'
    compaction_reason='model_downshift'
    compaction_first_model='gpt-5.4'
    compaction_second_model='gpt-5.3-codex-spark'
    compaction_catalog="/capture/runs/$run_id/model-downshift-catalog.json"
    docker exec "$capture_container" jq '
      {models: [
        .models[]
        | select(.slug == "gpt-5.4" or .slug == "gpt-5.3-codex-spark")
        | .comp_hash = "downshift-probe"
        | if .slug == "gpt-5.3-codex-spark"
          then .auto_compact_token_limit = 8000
          else .
          end
      ]}' /root/.codex/models_cache.json > "$work_dir/model-downshift-catalog.json"
    chmod 600 "$work_dir/model-downshift-catalog.json" ;;
  oauth-refresh)
    # 采 SPEC-EP-002 的 auth-sni：官方 CLI 的 OAuth token 刷新走 auth.openai.com。
    # ⚠ 属 I 类触发干预：只把 auth.json 的 last_refresh 提前，让 CLI 在本次调用前
    # 判定需要刷新；access_token／refresh_token／账号绑定一律不改，退出时逐字还原。
    # 刷新请求本身仍由官方 CLI 自己构造并发出，出站形态未被替换。
    auth_backup="/tmp/codex-auth-$run_id.json"
    docker exec "$capture_container" sh -c \
      "cp /root/.codex/auth.json '$auth_backup' && chmod 600 '$auth_backup'"
    docker exec "$capture_container" python3 - <<'PY'
import json, datetime
path = "/root/.codex/auth.json"
with open(path) as handle:
    doc = json.load(handle)
stale = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
doc["last_refresh"] = stale.isoformat().replace("+00:00", "Z")
with open(path, "w") as handle:
    json.dump(doc, handle)
PY
    docker exec "$capture_container" chmod 600 /root/.codex/auth.json
    prompt='只回复 OAUTH_REFRESH_OK，不要调用任何工具。' ;;
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
    prompt="这是经过授权的官方客户端出站采集。请严格只调用一次内置 Sites 的 save_site_version 工具，不要调用任何其他工具。参数必须是：project_id=ep002-probe-do-not-exist，commit_sha=0000000000000000000000000000000000000000，archive=$file_upload_home/ep002-probe.tar.gz。即使工具报错也立即停止，不要创建站点、不要发布或部署。" ;;
  *) echo "未知 SCENARIO: $scenario" >&2; exit 2 ;;
esac

echo "=== 场景 $scenario，$turns 轮 ==="
if [[ $prompt == "__REALTIME__" ]]; then
  # shellcheck disable=SC2086
  docker exec "$capture_container" timeout 120 python3 \
    "$capture_tool_root/drive_codex_realtime.py" \
    --codex-version "$codex_version" \
    --model "$model" --transport webrtc --output-modality audio \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --hold "${REALTIME_HOLD:-20}" 2>&1 | tail -10 || true
elif [[ $prompt == "__COMPACT_TUI__" ]]; then
  ctx_opt=""
  [[ -n ${CONTEXT_WINDOW:-} ]] && ctx_opt="--context-window $CONTEXT_WINDOW"
  # shellcheck disable=SC2086
  docker exec "$capture_container" python3 \
    "$capture_tool_root/drive_codex_tui.py" \
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
    --model "$model" --cwd /tmp/tui-probe \
    --prompt '请调用图片生成工具生成一张红色狐狸的简单插画；必须实际调用工具。' \
    --prompt '请再次调用图片生成工具生成一张蓝色鲸鱼的简单插画；必须实际调用工具。' \
    --prompt-hold "${TUI_HOLD:-180}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --log "/capture/runs/$run_id/tui.log" 2>&1 | tail -16 || true
elif [[ $prompt == "__PROMPT_TUI_SEARCH__" ]]; then
  docker exec "$capture_container" python3 \
    "$capture_tool_root/drive_codex_tui.py" \
    --model "$model" --cwd /tmp/tui-probe \
    --prompt '请联网搜索 Rust 1.90 的发布日期；必须实际调用联网搜索工具，只回复日期。' \
    --prompt '请再次联网搜索 Python 3.14 的发布日期；必须实际调用联网搜索工具，只回复日期。' \
    --prompt-hold "${TUI_HOLD:-150}" \
    ${DISABLE_FEATURES:+$(for f in $DISABLE_FEATURES; do printf -- '--disable %s ' "$f"; done)} \
    --log "/capture/runs/$run_id/tui.log" 2>&1 | tail -16 || true
elif [[ $prompt == "__GUARDIAN_TUI__" ]]; then
  docker exec "$capture_container" python3 \
    "$capture_tool_root/drive_codex_tui.py" \
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
    # shellcheck disable=SC2086 —— extra_args 需按空格拆成多个参数
    # shellcheck disable=SC2086
    docker exec "$capture_container" timeout 180 "$codex_bin" exec \
      --model "$model" --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
      $disable_args $extra_args "$prompt" 2>&1 | tail -3 || true
  done
fi

# 发 SIGTERM 后由中继的信号处理器取消并等待连接任务，再写 relay.json。
# 直接 -9 会丢掉尚未完成的连接元数据与最终清单。
stop_relay

if ! docker exec "$capture_container" test -s "/capture/runs/$run_id/relay/relay.json"; then
  echo "❌ 中继未写出 relay.json，样本不完整。" >&2
  exit 1
fi

# 中继原始字节包含真实 Authorization/Cookie；在证据离开采集机前必须等长脱敏。
# 先写入新目录并复扫，再替换原目录，避免留下未脱敏副本；字节长度和偏移保持不变。
scrubbed_relay="$work_dir/relay-scrubbed"
rm -rf -- "$scrubbed_relay"
python3 "$scrub_tool" \
  --src "$work_dir/relay" \
  --dst "$scrubbed_relay" \
  --verify
rm -rf -- "$work_dir/relay"
mv -- "$scrubbed_relay" "$work_dir/relay"

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
