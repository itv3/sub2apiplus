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
run_id=${RUN_ID:?必须提供 RUN_ID}
model=${MODEL:-gpt-5.6-luna}
# 直接监听 443：客户端打的就是 443，容器内该端口空闲，且 iptables 重定向在
# 无 NET_ADMIN 的容器里不可用。
relay_port=${RELAY_PORT:-443}
turns=${TURNS:-1}
scenario=${SCENARIO:?必须提供 SCENARIO: tool|image|compact}
codex_bin=${CODEX_BIN:-/root/.local/bin/codex}

if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

work_dir="$capture_root/runs/$run_id"
tls_dir="$work_dir/tls"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"

relay_started=0

cleanup() {
  local status=$?
  if [[ $relay_started == 1 ]]; then
    docker exec "$capture_container" pkill -f upstream_byte_relay.py >/dev/null 2>&1 || true
  fi
  # hosts 与临时 CA 一律还原，避免污染后续采集
  docker exec "$capture_container" sh -c \
    'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hr && cat /tmp/.hr > /etc/hosts && rm -f /tmp/.hr' \
    >/dev/null 2>&1 || true
  docker exec "$capture_container" rm -f /usr/local/share/ca-certificates/relay-ca.crt >/dev/null 2>&1 || true
  docker exec "$capture_container" update-ca-certificates --fresh >/dev/null 2>&1 || true
  echo "环境已恢复：中继已停止，hosts 与系统信任库中的临时 CA 均已还原。"
  exit $status
}
trap cleanup EXIT

install -d -m 0700 "$work_dir" "$tls_dir"

# 中继面向客户端的证书
openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/relay.key" \
  -out "$tls_dir/relay.csr" -subj "/CN=chatgpt.com" >/dev/null 2>&1
printf 'subjectAltName=DNS:chatgpt.com\n' > "$tls_dir/relay.ext"
openssl x509 -req -in "$tls_dir/relay.csr" -CA "$ca_full" -CAkey "$ca_full" \
  -CAcreateserial -out "$tls_dir/relay.crt" -days 1 -sha256 \
  -extfile "$tls_dir/relay.ext" >/dev/null 2>&1
chmod 600 "$tls_dir"/*

# 上游真实 IP：direct 模式必须绕开被劫持的 hosts，否则中继会连回自身
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
docker exec -d "$capture_container" python3 \
  /capture/tools/official_client_capture/upstream_byte_relay.py \
  --cert "/capture/runs/$run_id/tls/relay.crt" --key "/capture/runs/$run_id/tls/relay.key" \
  --mode direct --port "$relay_port" \
  --upstream-host chatgpt.com --upstream-ip "$upstream_ip" \
  --output "/capture/runs/$run_id/relay" --timeout 300
relay_started=1
sleep 2

# hosts 劫持须在中继起来之后
docker exec "$capture_container" sh -c \
  'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hp && cat /tmp/.hp > /etc/hosts && rm -f /tmp/.hp'
docker exec "$capture_container" sh -c "printf '127.0.0.1 chatgpt.com\n' >> /etc/hosts"

case "$scenario" in
  tool)
    # 用无副作用、参数固定的本地动作诱导工具调用（验证方案 §8.2）
    prompt='请用 shell 工具执行一条命令：echo probe-123。执行后只回复 TOOL-OK。' ;;
  image)
    prompt='请生成一张图片：一只红色的狐狸，简单画风。' ;;
  compact)
    # 灌满上下文触发 CompactionReason::ContextLimit——三条可行路径里唯一能拿到
    # **自然基线**的（另两条需交互式 TUI，或降 manifest 窗口属 I 类干预）。
    #
    # 注意 /compact 走不通：斜杠命令只在 TUI 输入框解析，codex exec 会把它当普通
    # 文本发给模型，模型"照字面理解"做段摘要，看着像压缩其实不是（SPEC-EP-024）。
    prompt='__COMPACT_FILL__' ;;
  *) echo "未知 SCENARIO: $scenario" >&2; exit 2 ;;
esac

echo "=== 场景 $scenario，$turns 轮 ==="
if [[ $prompt == "__COMPACT_FILL__" ]]; then
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
        $ctx_opt --model "$model" --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
        "$task" 2>&1 | tail -2 || true
    else
      docker exec -w "$work" "$capture_container" timeout 240 "$codex_bin" exec resume --last \
        $ctx_opt --model "$model" --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
        "$task" 2>&1 | tail -2 || true
    fi
  done
else
  for i in $(seq 1 "$turns"); do
    echo "--- 第 $i 轮 ---"
    docker exec "$capture_container" timeout 180 "$codex_bin" exec \
      --model "$model" --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
      "$prompt" 2>&1 | tail -3 || true
  done
fi

docker exec "$capture_container" pkill -f upstream_byte_relay.py >/dev/null 2>&1 || true
sleep 3
docker exec "$capture_container" cat "/capture/runs/$run_id/relay/relay.json" 2>/dev/null || echo "无产物"
printf 'run_id=%s\n' "$run_id"
