# 变更集 0：身份、包边界与发送面基线

> 对应 [主方案](../1.md) 的变更集 0。本阶段只产出证据、契约、测试输入和门禁，
> 不修改生产发送行为。

## 状态：已通过复审，任务关闭

变更集 0 已形成一套可复算的发送面基线，以及 0A–0D 四层可编译证据契约。旧
Executor 草案和旧 API 提案已移出仓库，当前不存在两套并行类型定义。

## 一、交付物

| 交付物 | 内容 |
|---|---|
| [persona-catalog.md](persona-catalog.md) | `method + host + path + purpose → persona` 分类规则 |
| [sink-inventory.md](sink-inventory.md) | 发送点人工判定、旁路与 facade 关系 |
| `sink-baseline.json` | 扫描候选的机器基线，含 bootstrap commit；数量由 `sink-stats.md` 生成 |
| [header-ownership.md](header-ownership.md) | Header 所有权及 Override 优先级 |
| [probe-behavior.md](probe-behavior.md) | 后台探针行为证据与 WHAM-first 决策 |
| [package-api.md](package-api.md) | 0A–0D 唯一包契约及后续生产边界 |
| [guard-state-machine.md](guard-state-machine.md) | per-sink observe/canary/enforce、灰度和回滚 |
| [sink-stats.md](sink-stats.md) | 从基线单源生成的候选、persona、迁移与证据统计 |
| `backend/cmd/egressscan` | go/packages + go/types + AST 发送面扫描器 |
| `profilecontract` | 0A：无损 ProfileSpec 与不可变快照目录 |
| `releasecontract` | 0B：purpose + mode ReleaseGraph |
| `bindingcontract` | 0C：sink 基线到 ReleaseBinding 的确定性投影 |
| `compositioncontract` | 0D：三层证据的一致性连接，不生成可执行请求 |

## 二、可复算统计

当前扫描数字全部由 `sink-baseline.json` 单源生成，见 [sink-stats.md](sink-stats.md)。
README 与 inventory 不再复制候选、persona、迁移归属等数字；`make check-egress-spec`
会重新生成统计并逐字节比较，避免基线更新后文档继续显示旧值。

`bootstrap_commit` 同时固定在扫描器锚点中；正常重算不会把它推进到当前 HEAD。若需重建
legacy 边界，必须同时修改锚点与基线并单独审核，不能通过重跑 bootstrap 悄悄扩大存量。

无法静态绑定的请求事实保持 dynamic/unknown，不使用“函数体里出现过某个 host”补造
证据；共享 OAuth 客户端工厂单列为 facade，不再任意归给 refresh。

0A–0D 契约：

- ProfileSpec 原始快照 canonical 往返相等；
- 2451/2451 个快照叶子做合法变异后 digest 均改变；
- 反射修改 getter 返回对象的 10477 个叶子，不污染 ProfileSpec；
- ReleaseGraph 为 HTTP/WS × active/previous 四节点，按 purpose + mode 定位；
- observed enum 必须是 engine-supported enum 的子集；
- ReleaseBinding 精确承载全部 in-scope Sink 与候选，不含虚构 RetryPolicy；数量见
  [sink-stats.md](sink-stats.md)；
- 全部具备 `codex_profile` 端点证据的 Codex 业务 Sink 均能唯一连接到画像 endpoint；
  OAuth authorization-code exchange 明确标记为 `transport_only`，不会因与 refresh 共用
  `/oauth/token` 就冒充 `oauth_refresh` 端点。

## 三、关键事实与修正

### 3.1 确认三条普通 Go TLS 旁路

- `probeOpenAICodexSnapshot`；
- Agent Identity task register；
- PAT whoami。

其中 PAT whoami 的 Header 声称 Codex CLI、TLS 却是 Go 标准库，身份在层间自相矛盾。

### 3.2 privacy 不是 Codex 旁路

settings、accounts check、subscriptions 属 `chatgpt-web` persona，应保持独立 Chrome
执行入口，不能因为 host 是 chatgpt.com 就迁入 Codex Executor。

现状本身仍有已固化的 wire 缺陷：Chrome 120 指纹过时、导航 Header 混入 XHR、
Accept-Encoding 只有 gzip、两个 GET 没有设置 Fetch Metadata。变更集 0 只记录现状，
修复属于 1B。

测试覆盖已分成两层：

1. `req_client_chrome_wire_test.go` 捕获 req/v3 Chrome 工厂产生的 h2/H1 wire；
2. `openai_privacy_production_chain_test.go` 通过 `PrivacyClientFactory` 直接执行三个生产
   函数，验证真实 method、URL、query 和请求级 Header，不再手工复刻生产调用链。

真实公网 Chrome 的 HTTP/2 SETTINGS、pseudo-header/HPACK 顺序仍需外部抓包；这是一项
1B 修复的对照证据，不阻塞 1A 的 observe 骨架，也不能据此宣称当前 browser wire 正确。

### 3.3 修正 API Key capability probe 的假证据

`ProbeOpenAIAPIKeyResponsesSupport` 对 mimic 账号会提前返回。可达发送分支仅处理非
mimic API Key，目标是账号 baseURL 的 `/v1/responses`，不是
`chatgpt.com/backend-api/codex/responses`。旧分类和 Header 文档把函数内部不可达分支
当成调用证据，现已改为 out-of-scope。

### 3.4 facade 不能生成业务 SinkID

主 `Forward` 不直接调用 terminal transport，多个语义不同的业务出口共用
`doOpenAIHTTPUpstreamWithProfile`。因此 SinkID 必须在业务调用点生成并穿过 facade；
在 facade 处统一生成会让 per-sink enforce 和回滚失效。0D 明确拒绝为共享 facade
组合业务 EvidenceBundle。

### 3.5 行为身份不等于 wire 身份

合成 `/responses` 用量探针即使迁入 Executor，也仍可能表现为官方 CLI 不会周期发送的
行为。后台请求还必须声明官方等价证据、最短间隔、jitter、并发、副作用和失败冷却。
普通 OAuth 用量刷新目标已定为 WHAM-first，见 [probe-behavior.md](probe-behavior.md)。

### 3.6 同 route 不等于同端点契约

OAuth authorization-code exchange 与 token refresh 都请求 `POST auth.openai.com/oauth/token`，
但当前 0.145 画像只承载 refresh 的 Header/Body 契约。基线因此分别记录
`transport_only` 与 `codex_profile`；0D 会拒绝把 exchange 组合成 `oauth_refresh`。
共享 ReqProfile 客户端工厂是 facade，不属于任一业务 Sink。

## 四、门禁

```bash
make check-egress-spec
```

该目标会执行：

1. 版本泄漏、§3.5 台账、源码引用门禁；
2. egressscan 的 10 条合成自测、method/path/host 反向变异、类型失败兜底、双平台
   构建矩阵、10 类发送栈与 4 条真实请求事实断言，以及完整基线比对；
3. 0A–0D 的 gofmt、go vet、契约测试；
4. Chrome 生产工厂 wire 测试、privacy 三函数生产链测试，以及普通 `httpclient`
   禁用 HTTP/2 的协议特征测试；
5. 重新导出发送面统计、snapshot、enum、ReleaseGraph、ReleaseBinding 后逐字节比较；
6. 不依赖专用 build tag 的生产构建。

变更集 1A 已把四个 contract 包和两个导出桥提升为生产依赖，台账门禁不再提供
`egresscontract` 专用排除。仅不会进入生产二进制、但会被源码扫描命中的命令工具，
允许在 `SCOPE_EXCLUSIONS` 逐项说明理由。

## 五、边界与下一步

变更集 0 的 `EvidenceBundle` 不是可执行 Release。以下内容没有被偷偷塞进契约：

- Executor/Guard 的生产实现；
- RetryPolicy 和行为调度；
- 代理/CA/平台支持域与 CONNECT 过渡画像；
- Credential、连接 key 和 adapter；
- 任何生产 sink 迁移。

审核通过后才能进入变更集 1A。1A 首次部署仍为 observe，不得把尚未迁移的主链提前
切到 enforce。
