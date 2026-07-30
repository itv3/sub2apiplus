# 候选核心场景受控抓包

`run_candidate_core_capture.sh` 通过生产 `sub2api` 入口采集 A03、A04、A05、A06、
A07、A08、A10、A15。候选服务仍真实执行账号调度、版本画像、HTTP/WS Finalizer、
TLS、连接池、重试预算和 HTTP fallback；`upstream_byte_relay.py` 只在最后的受控 TLS
终点返回冻结响应。

## 安全边界

- wrapper 必须显式设置
  `ENABLE_CANDIDATE_CORE_SYNTHETIC=YES_I_ACCEPT_SYNTHETIC_ONLY`；relay 同时要求
  `--synthetic-profile candidate-core-v1 --allow-synthetic-responses` 和精确场景 ID。
- 合成画像拒绝 `--upstream-ip`、`--upstream-map`，也不执行 DNS。每个场景只接受
  `chatgpt.com` 上冻结的 models、Responses HTTP 或 Responses WS 组合；未知请求返回
  421，不回退生产。
- A07 默认对同一入口调用的前 6 次 WS 握手返回 502；只有恰好耗尽该次数后，才允许
  HTTP POST 获得 SSE 成功。次数可通过 `A07_WS_FAILURE_COUNT` 调整，但 relay 和最终
  计数门禁必须一致。
- WS 成功路径必须先从候选收到真实、可解压的 `response.create`，之后才发送
  `response.created`、可选 `response.metadata` 和 `response.completed`。脚本不会生成
  客户端帧或结构化事实。
- A06 使用 `drive_candidate_gateway_ws.py` 连接生产 `GET /v1/responses` 入口。API Key
  只从匿名 fd 读取；驱动器严格校验 HTTP 101、`Sec-WebSocket-Accept`、服务端未掩码
  帧与未协商 RSV，支持分片及 PING/PONG，并在同一入站连接内等首轮完成后才发送续轮。
  上游必须恰好形成 1 次握手和 3 个 `response.create`：`generate=false` 预热、首轮
  业务以及携带首轮真实 `response.id` 的增量帧。
- API Key 分组必须只有 `ACCOUNT_ID` 一个可调度 OpenAI OAuth 账号。访问凭据从匿名
  curl 配置 fd 读取，relay 原始字节完成等长脱敏后才进入最终目录。

## 证据边界

本脚本只声明它真实观察到的网络事实：

- A03/A04：HTTP 请求、Lite/非 Lite、zstd/明文和账号管理态 residency；
- A05/A06：WS 101、`permessage-deflate`、真实 `response.create` 与服务端完成帧；A06
  还证明两轮业务共用同一条客户端 WebSocket，且预热 ID 不会冒充首轮业务 ID；
- A07：一次入口调用内的多次 WS 失败和最终 HTTP POST；
- A08：三个真实上层调用形成的原始连接；
- A10：四个带 `compaction_trigger` 的真实 V2 出站；
- A15：三组身份 header 经生产入口形成的真实 models 出站。

A08 的 keepalive/断连 retry 关系、A10 的 TokenBudget 零出站、A15 的真实 exec/TUI
进程来源无法仅凭该 relay 无歧义证明。它们必须由
`candidate_test_trace.py` 从精确 `go test -json` 日志生成的、绑定源码哈希的
`process_trace`/`websocket_trace` 补证。脚本不会为这些事实写 JSON 观察记录。

## 正式运行

先把下列文件同步到 Vircs 的 capture tools 根目录：

- `upstream_byte_relay.py`
- `run_candidate_core_capture.sh`
- `drive_candidate_gateway_ws.py`
- `scrub_raw_bytes.py`

然后执行：

```bash
ENABLE_CANDIDATE_CORE_SYNTHETIC=YES_I_ACCEPT_SYNTHETIC_ONLY \
RUN_ID=candidate-core-20260730T150000Z \
ACCOUNT_ID=90 \
API_KEY_ID=1 \
/root/oauth-capture/tools/official_client_capture/run_candidate_core_capture.sh
```

运行前还必须满足：

1. `capture-cli` 与候选服务位于同一 Docker 网络，且有 `tcpdump` 和 Python
   `zstandard`；
2. `$CAPTURE_ROOT/state/mitm` 中已有验收 CA；
3. 目标 OAuth access token 在整轮运行期间有效，避免触发白名单外 refresh；
4. `RUN_ID` 对应目录不存在，脚本拒绝覆盖旧证据。

## 产物与恢复

正式产物位于：

```text
/root/oauth-capture/runs/$RUN_ID/
├── run-summary.json
└── scenarios/{A03,A04,A05,A06,A07,A08,A10,A15}/
    ├── egress.pcap
    ├── relay/
    │   ├── relay.json
    │   ├── intervention.jsonl
    │   └── conn*.{client_to_upstream,upstream_to_client}.bin
    └── trigger/
```

`relay/*.bin` 已等长脱敏并重算 SHA-256；`relay.json` 固定声明
`production_forwarding_enabled=false`。原始 `relay-private/` 只在脱敏成功后删除。

脚本退出时恢复并逐项复核：

- `accounts.proxy_id` 与 `proxy_fallback_origin_id`；
- 完整 `accounts.extra` JSONB；
- 服务容器 `/etc/hosts` 与 CA bundle 的 SHA-256；
- keeper 的运行前状态。

任一恢复项不一致固定退出 97，并保留
`/root/oauth-capture/runtime/candidate-core-$RUN_ID` 供人工恢复。`complete` 只表示本脚本
八个场景闭合；最终仍须由 42 条 assertion、证据 guard 和 bundle finalize 得到
`accepted=true`。
