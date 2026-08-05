# 官方出站 persona 与 route 目录

本文件只记录当前分类规则和审计边界。运行时闭集由
`backend/internal/officialegress` 中的 SinkCatalog、PhysicalRouteCatalog、ReleaseCatalog
及其嵌入式数据共同决定；本文件不在服务请求路径中动态加载。

## 解析维度

受控路由使用 `method + host + path + purpose + protocol` 五个维度解析：

- `method` 和 `protocol` 区分同一路径上的 HTTP 与 WebSocket；
- `purpose` 区分主请求、管理测试、用量探针和 fallback；
- 动态上传目标按 `PUT *.oaiusercontent.com/{server_returned_path}` 模板校验；
- persona 不能根据 HTTP 库、host 或 path 单独推断。

## persona 边界

| persona | 当前边界 |
|---|---|
| `codex-cli` | 使用 OpenAI OAuth 且已经绑定 ReleaseBundle、EndpointID 和受控 Runtime Sink 的请求 |
| `chatgpt-web` | 账号设置、账号信息和订阅信息等浏览器路径 |
| `codex-api-key-mimic` | 明确启用 Codex API Key mimic 的兼容请求；与 OAuth 共用画像引擎，但使用独立身份模式 |
| `unclassified` | 已登记但证据不足的低频路由，只能按受限策略处理 |
| `out-of-scope` | 其他供应商、通用 API Key、自定义 provider 和非官方目标 |

官方客户端与第三方客户端的 OpenAI OAuth 请求统一进入 `codex-cli` 画像；入站客户端类型不拥有
最终 wire 身份。API Key mimic 只有在明确的身份模式和 purpose 下才能使用 Codex 画像，不能由
host 或模型名称猜测。

## 当前 Codex OAuth 路由族

| 路由族 | 端点 |
|---|---|
| Responses | `/backend-api/codex/responses`、`/backend-api/codex/responses/compact` |
| WebSocket | Responses WS、`/v1/realtime` sideband |
| Models 与 Search | `/backend-api/codex/models`、`/backend-api/codex/alpha/search` |
| Images | `/backend-api/codex/images/generations`、`/backend-api/codex/images/edits` |
| Files | `/backend-api/files`、动态 blob 上传、上传完成确认 |
| WHAM | usage、reset credits 查询与消费 |
| OAuth | `POST auth.openai.com/oauth/token`；refresh 使用完整端点画像，authorization-code exchange 保持 `transport_only` |
| Realtime calls | `/backend-api/codex/realtime/calls` HTTP 请求 |

精确的 method、host、purpose、protocol、SinkID、EndpointID 和 enforcement 状态以
`sink-baseline.json`、运行时嵌入式 Catalog 及 `make check-egress-spec` 的复算结果为准。

## Guard 规则

- 未登记的官方 host 路由失败关闭；
- 已登记但未完成画像绑定的路由不得获得 ReleaseSelection；
- 动态 blob host 必须匹配 Bundle 中的后缀和路径策略；
- 最终 Header、Body、TLS、连接与 WebSocket 画像只能由 Executor 和 terminal adapter 定型；
- browser persona、通用 API Key 和其他供应商不得被 Codex OAuth 画像覆盖。

完整校验入口：

```bash
make check-egress-spec
```
