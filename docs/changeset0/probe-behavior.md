# 变更集 0：内部探针行为决策表

> 对应方案 §6 BehaviorPolicy。
>
> 核心判据不是"长得像 Codex"，而是**官方客户端会不会在这个时机、以这个频率发这个请求**。
> 形态对齐只能骗过单请求检测，行为特征骗不过会话级观测。
>
> 标注：【已验证】= 已在本轮按生产调用链直接读码或用特征测试确认；【待补证】=
> 需要后续抓包或官方行为证据，不能作为当前实现正确的依据。

## 一、本次新发现的 P0 旁路（此前评审全部遗漏）

三份架构评审都只发现了 `probeOpenAICodexSnapshot` 一条 TLS 旁路。实际有 **3 条**。

### 1.1 Agent Identity task 注册 —— 最裸的一条【已验证】

[openai_agent_identity.go](../../backend/internal/service/openai_agent_identity.go#L188)

```
POST auth.openai.com/api/accounts/v1/agent/{runtime_id}/task/register
客户端：httpclient.GetClient          → 无 uTLS，Go 标准库 ClientHello
Header：仅 Content-Type + Accept      → 连 Codex UA/originator/version 都没有
```

比用量探针更严重：用量探针至少带 Codex 身份头（形态一半对），这条**两层都不对**。

触发是懒加载 + 失效恢复：`task_id` 为空时注册；上游返回 401 且体含
`invalid_task_id`/`task_not_found`/`task_expired` 时重新注册【已验证】。用量探针与 WHAM
查询在构造鉴权头时都会经过它，**后台探针可间接触发本请求**。

### 1.2 PAT whoami —— 身份自相矛盾【已验证】

[openai_codex_pat_service.go](../../backend/internal/service/openai_codex_pat_service.go#L46)

```
GET auth.openai.com/api/accounts/v1/user-auth-credential/whoami
客户端：httpclient.GetClient          → 无 uTLS
Header：applyOpenAICodexAuxiliaryHeaders → 有完整 Codex 身份头
```

**Header 声称是 Codex CLI，TLS 握手是 Go 标准库**。这种组合比"两层都不对"更容易被识别——
它主动声明了一个与传输层矛盾的身份。

三个生产调用者分别是 OAuth/PAT 导入、管理端 PAT 校验和用户触发的 alpha-search，
没有后台周期调用者【已验证】，因此暴露面小于前两条，但形态问题同样明确。

### 1.3 Codex 用量探针（已知）【已验证】

[account_usage_service.go](../../backend/internal/service/account_usage_service.go#L777)。详见 §三。

## 二、行为层面的可观测特征

形态之外，以下几条是**时序特征**，迁入 Executor 完全不能解决。

### 2.1 定时账号测试：每分钟第 10 秒准时【已验证】

[scheduled_test_runner_service.go](../../backend/internal/service/scheduled_test_runner_service.go#L87)

```go
func (s *ScheduledTestRunnerService) runScheduled() {
	// Delay 10s so execution lands at ~:10 of each minute instead of :00.
	time.Sleep(10 * time.Second)
```

**准确语义**：runner 自身的 cron 是 `* * * * *`——它每分钟唤醒一次、固定在第 10 秒，
调用 `planRepo.ListDue(now)` 扫描**到期的计划**并执行。单个账号的实际发送频率由该账号
计划的 cron 表达式决定，最小可配到每分钟一次。

因此"每账号每分钟发一次推理"是错的；准确的说法是：**所有到期计划的执行都被对齐到
每分钟的第 10 秒**，且 runner 无抖动、并发上限 10。

这个对齐本身就是特征：无论管理员把计划配成 5 分钟还是 1 小时，请求都精确落在
`:10`。真实用户的请求不会有秒级固定相位。多账号部署下，同一时刻到期的计划会并发
发出——形成周期性脉冲。执行时会完整读完 SSE 流（真实计费）。

这个特征与 TLS/Header 无关，Executor 迁移不改善分毫。

### 2.2 用量探针：发起推理后立即断连【已验证】

payload 为 `{"input":[{"text":"hi"}],"stream":true,"store":false}`，**不带
`max_output_tokens`**；代码只读 `resp.Header` 随后 `defer Body.Close()`
[account_usage_service.go](../../backend/internal/service/account_usage_service.go#L789)。

即：向上游发起一次无输出上限的流式推理，拿到响应头就断开。这是一个上游侧极易观测的
独特模式——真实客户端要么读完，要么因用户中断而断开，不会稳定地"只取 header"。

### 2.3 WS 连接池后台 ping：固定 30 秒无抖动【已验证】

[openai_ws_pool.go](../../backend/internal/service/openai_ws_pool.go#L26)，
`openAIWSBackgroundPingInterval = 30 * time.Second`，固定 Ticker，并发上限 10，无抖动。

### 2.4 隐私 PATCH 的失败重发环路【已验证】

一旦被 Cloudflare 拦截、标记为 `training_set_cf_blocked`，**每一次该账号的后台 token
刷新都会重打一次**（默认 5 分钟一轮），无冷却、无退避
[token_refresh_service.go](../../backend/internal/service/token_refresh_service.go#L1446)。

已被 CF 拦截的账号持续以 5 分钟周期重试同一被拒请求——这是最容易触发风控升级的模式。

### 2.5 `QueryUsage` 的隐藏第二发【已验证】

[openai_quota_service.go](../../backend/internal/service/openai_quota_service.go#L181)
在 `/wham/usage` 成功后**无条件**再发
`GET /backend-api/wham/rate-limit-reset-credits`，无独立节流。

**每次 `QueryUsage` = 2 次官方请求**。方案 §6 第 5 条要求周期刷新改用 usage-only 方法，
依据即此。

## 三、行为决策表

| ID | 请求 | 触发 | 频控 | 抖动 | 计费副作用 | 官方等价行为 | 决策 |
|---|---|---|---|---|---|---|---|
| R1 | 用量探针 `POST /responses` | 管理端查看用量 | TTL 10min，`force=true` 可绕过 | 无 | **有**（真实推理） | **无证据** | **改 WHAM-first** |
| R2 | Spark 影子 `GET /wham/usage` | 后台刷新 | 复用 R1 的 TTL | 无 | 无 | 有（画像已登记） | 保留，扩展到普通 OAuth |
| R3 | reset-credit details | R2 成功后无条件 | 无 | 无 | 无 | 待举证 | **拆出，周期刷新不带** |
| R4 | 定时账号测试 | cron 每分钟 | 管理员配置 | **无，固定 :10** | **有** | **无证据** | **加抖动 + 降频** |
| R5 | compact 探针 | 管理端手动 | 无 | 无 | 有 | 待举证 | 迁 Executor |
| R7 | OAuth token 刷新 | 后台 5min 轮询 | QPS 2 / 并发 4 | **有（75~125%）** | 无 | **有** | 保留 |
| R8 | 隐私 PATCH | 每次 token 刷新后评估 | 仅幂等标志 | 无 | 无 | 有（浏览器行为） | **加失败冷却** |
| R9 | Agent Identity 注册 | 懒触发 + 401 恢复 | 无 | 无 | 无 | 待举证 | **P0：补画像** |
| R10 | PAT whoami | 用户操作 | 无 | 无 | 无 | 待举证 | **P0：补画像** |
| R11 | WS 后台 ping | 30s Ticker | — | 无 | 无 | 有（WS 保活） | 加抖动 |

**R7 的抖动需要精确表述**：它的 75%~125% 抖动只作用于**失败重试的退避**，调度本身仍是
默认 5 分钟、可配置的固定 Ticker【已验证】。因此准确说法是“本表所列 OpenAI official-host 请求中，
唯一具备重试退避抖动的请求”，而不是“唯一具备调度抖动”——**本表路径的调度层均无
抖动**，包括 R7。其他平台和通用后台任务不在本结论范围内。

这反而加强了结论：多账号部署下，所有账号的 token 刷新仍会在同一 tick 对上游发起请求，
只有失败重试才会散开。调度抖动是 BehaviorPolicy 必须补齐的能力，没有现成参照实现。

## 四、WHAM-first 的落地约束

方案 §6 的 10 条规则中，与本调查直接相关的事实依据：

1. **窗口归类阈值确实过宽**【已验证】：
   [openai_gateway_service.go](../../backend/internal/service/openai_gateway_service.go#L186)
   `primaryMins <= 360`（6 小时）归 5h，其余全归 7d。30 天窗口（43200 分钟）会被吞成 7d。
   [同文件](../../backend/internal/service/openai_gateway_service.go#L201) 更宽：无窗口信息时按位置假设 primary=7d。

2. **WSv2 门控确实存在**【已验证】：
   [account_usage_service.go](../../backend/internal/service/account_usage_service.go#L685) 注释明确
   "普通账号的 codex 刷新走 probe(/responses 头)，要求 WSv2；但 spark 影子走 QueryUsage
   (/wham/usage body 的 codex_bengalfox)，与 WSv2 无关"。

3. **reset-credit 必须拆开**【已验证】：见 §2.5。

## 五、变更集 1B 的行为策略要求

每个保留的后台请求必须声明：

| 字段 | 要求 |
|---|---|
| `BehaviorKind` | user_request / admin_test / quota_query / oauth_refresh / background_probe |
| `MinInterval` | 最短间隔，per-account |
| `Jitter` | **必填**。当前除 R7 外全部为 0，这是系统性缺陷而非个案 |
| `MaxConcurrency` | 并发上限 |
| `BillingSideEffect` | 是否产生模型调用与计费 |
| `OfficialEvidence` | 官方等价行为的证据来源；**无证据者不得保留为主动请求** |
| `FailureCooldown` | 失败后的冷却，防止 R8 那样的重发环路 |

**本表所列 OpenAI official-host 调度均无抖动**：用量探针、定时测试、WS ping、隐私
PATCH、Agent 注册、token 刷新的**调度**都是固定间隔；唯一的抖动出现在 token 刷新的
失败重试退避上。该结论不扩张到仓库里的其他平台或通用调度器。

多账号部署下，所有账号会在同一 tick 对上游发起同类请求，形成明显的同步脉冲。这个
特征与单请求形态完全无关，必须在 BehaviorPolicy 层解决，且**没有现成实现可参照**。

## 六、已登记的 1B 后续证据（不阻塞变更集 0）

以下项目不影响变更集 0 的发送面枚举、身份状态登记和 observe 设计，但在 1B 决定
“保留、迁移还是删除”相应主动请求前必须补齐。它们不能被当作当前 wire 正确的证据：

- R1 使用的 `httpclient` 明确设置自定义 `DialContext`，且 `ForceAttemptHTTP2=false`；按
  Go `net/http.Transport` 契约，这会禁用 HTTP/2 尝试，实际走 HTTP/1.1【已验证】。
  `TestBuildTransportWithCustomDialKeepsHTTP2Disabled` 将这项配置事实固化为门禁；它与普通
  Go ClientHello 一起构成相对 Codex 画像的双重差异。
- `testOpenAIOfficialFilesProbe` 已确认调用正式 `UploadOfficialCodexFile`，依次执行 create、
  服务端返回绝对 URL 的 blob PUT、uploaded 三段式流程，并使用固定内存 payload【已验证】。
  后续只需在 1B 确认触发频率和官方等价行为，不再把请求序列本身列为未知。
- Agent Identity 与 PAT whoami 的官方等价行为证据——决定它们是补画像保留，还是应当取消。
