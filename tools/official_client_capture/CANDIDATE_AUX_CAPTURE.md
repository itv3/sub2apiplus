# 候选辅助端点受控抓包

`run_candidate_aux_capture.sh` 用候选服务的真实生产调用路径采集 A09、A11、A12、
A13、A14。服务仍执行自己的 Go HTTP/TLS/WS、版本画像、账号解析和 Redis 生命周期；
`upstream_byte_relay.py` 只在 TLS 终点按冻结白名单返回确定性响应。

## 安全边界

- 必须显式设置 `ENABLE_CANDIDATE_AUX_SYNTHETIC=YES_I_ACCEPT_SYNTHETIC_ONLY`；relay
  同时要求 `--synthetic-profile candidate-aux-v1 --allow-synthetic-responses`。
- 合成模式拒绝 `--upstream-ip` 和 `--upstream-map`，且代码在任何
  `asyncio.open_connection` 之前完成本地响应。未知 host、路径或 query 返回 421，
  不回退到生产。
- API Key 所属分组必须只有 `ACCOUNT_ID` 一个可调度 OpenAI OAuth 账号。脚本不通过
  临时禁用其他账号来制造隔离，避免影响生产调度。
- A12 的 consume 由候选生成真实 `redeem_request_id`，但隧道只到受控 relay；A14
  的区域 PUT URL 只能来自 create 响应；A13 使用 raw refresh 入口，不更新账号凭据。
- A13 dummy token 通过匿名管道提交，并在 `ByteRecorder.write` 之前等长替换为
  `<secret>...`。最终还会以仅存在于进程环境的原值扫描整个 run 目录。

## 运行

```bash
ENABLE_CANDIDATE_AUX_SYNTHETIC=YES_I_ACCEPT_SYNTHETIC_ONLY \
RUN_ID=candidate-aux-20260730T120000Z \
ACCOUNT_ID=90 \
API_KEY_ID=1 \
ADMIN_BEARER_TOKEN_FILE=/run/secrets/sub2api-admin-token \
tools/official_client_capture/run_candidate_aux_capture.sh
```

运行前还必须满足：

1. `capture-cli` 与候选服务在同一 Docker 网络，且 capture 容器有 `tcpdump`；
2. `$CAPTURE_ROOT/state/mitm` 中有现有验收 CA；
3. 最新 tools 已挂载到 capture 容器的 `/capture/tools`；
4. API Key 分组已启用 Live，目标账号的 access token 在本轮有效。

脚本不接受已有 `RUN_ID` 目录，避免覆盖旧证据。

## 产物与恢复

每个场景目录同时包含：

- `relay/*.client_to_upstream.bin`：等长脱敏后的 H1/WS 应用原始字节；
- `relay/relay.json` 与 `intervention.jsonl`：连接哈希、动作及
  `production_forwarded=false`；
- `egress.pcap`：CONNECT 与 TLS ClientHello/SNI；
- `trigger/`：不含调用凭据的入口状态或响应。

根目录 `run-summary.json` 固定使用 `candidate-aux-capture/v1`，列出每个场景动作计数、
pcap 哈希以及 hosts/CA 恢复哈希。账号 `proxy_id`、`proxy_fallback_origin_id`、hosts、
CA bundle 和 keeper 运行状态均按运行前值恢复。环境恢复任一项不一致时固定退出 97，
并保留 `$CAPTURE_ROOT/runtime/candidate-aux-$RUN_ID` 供人工恢复。

正式验收前仍须把这些原始产物交给候选 42 条 assertion/manifest 工具绑定；本脚本的
`complete` 只表示五个辅助场景采集闭合且环境恢复成功，不替代 42/42 总门禁。
