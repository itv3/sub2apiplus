# CHG-03：alpha-search 版本单一权威 — 实施方案

> 状态：方案已确认，待编码
> 基线：`58d0827c3`
> 清单：[OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md](OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md) CHG-03

## 1. 目标

alpha-search 出站 target（endpoint、URL、method、Host）与 Executor 编译的 header、body、TLS、
release 全部来自同一个冻结 `ReleaseBundle` 实例。不改业务语义与出站 wire。

不在范围内：ingress 绑定与 body 预投影（[openai_alpha_search.go:46-62](../../../backend/internal/service/openai_alpha_search.go:46)、
[:585](../../../backend/internal/service/openai_alpha_search.go:585)）仍按冻结 mode 查全局 catalog。

## 2. 实施顺序

分两步提交，**第一步结论确认后再编码第二步**。

- **第一步**：§3 前置判定。若证明 `ProfileVersion` 无消费者，§4.3 退化为「删一处传参 + 删一个
  常量」，整个变更集比现方案小一圈。
- **第二步**：§4 改动 + §5 测试 + §6 门禁。

## 3. 前置判定

测试：alpha-search OAuth 请求的 `OfficialEgressContext` 置空 `ProfileVersion`，完整执行
`invocation.Execute`，断言 header／body／TLS／Guard／adapter 正常，且最终 TLS 等于 bundle 编译出的
`TransportSpec`。

通过走 §4.3-A，失败走 §4.3-B。该测试保留为防回归资产。

## 4. 改动

### 4.1 `ForwardAlphaSearch` 调整顺序

[openai_alpha_search.go:93-121](../../../backend/internal/service/openai_alpha_search.go:93)：
invocation 创建提到构造 request 之前，把 `invocation.Bundle()` 传下去。

```go
ctx, err = bindOfficialEgressSink(ctx, officialEgressSinkAlphaSearchDirect)
if err != nil { … }

var invocation *officialCodexHTTPInvocation
var alphaBundle *officialegress.ReleaseBundle
if account.IsOpenAIOAuth() {
    runtimeState, runtimeErr := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
    if runtimeErr != nil { return nil, runtimeErr }
    attemptIdentity, _ := officialegress.AttemptIdentityFromContext(ctx)
    invocation, err = newOfficialCodexHTTPInvocation(ctx, officialCodexHTTPInvocationInput{
        Runtime: runtimeState, Account: account,
        SinkID:       officialEgressSinkAlphaSearchDirect,
        InvocationID: attemptIdentity.InvocationID, ProxyURL: proxyURL,
        PolicyID:     "changeset3.alpha_search.direct",
        PolicySource: "service.ForwardAlphaSearch", AttemptBudget: 1,
    })
    if err != nil { return nil, err }
    bundle := invocation.Bundle()
    alphaBundle = &bundle
}

req, err := s.buildOpenAIAlphaSearchRequest(ctx, c, account, body, token, alphaBundle)
```

`invocation.Execute(req.Context(), …)` 不变，不再重复创建。

不使用 `officialCodexHTTPInvocationInput.Bundle` 输入字段——其复用分支要求逐字段复制
`execution` 策略构造（[official_egress_http_invocation.go:133-142](../../../backend/internal/service/official_egress_http_invocation.go:133)）。

> 编码验证：`attemptIdentity` 由 `req.Context()` 改取 `ctx`，以断言确认一致；不成立则构造
> request 前显式绑定。

### 4.2 `buildOpenAIAlphaSearchRequest` 从 bundle 派生

[openai_alpha_search.go:397-462](../../../backend/internal/service/openai_alpha_search.go:397)：
签名增加 `bundle *officialegress.ReleaseBundle`，OAuth 分支三处查 catalog 改为一次派生。

```go
if account.IsOpenAIOAuth() {
    if bundle == nil {
        return nil, errors.New("alpha search OAuth 路径缺少已冻结 ReleaseBundle")
    }
    profile, profileErr := resolveOfficialCodexReleaseProfile(bundle.Release())
    if profileErr != nil {
        return nil, fmt.Errorf("resolve alpha search release profile: %w", profileErr)
    }
    mode = string(bundle.Mode())
    endpointProfile, err = profile.ResolveEndpoint(string(officialCodexEndpointAlphaSearch))
    if err != nil {
        return nil, fmt.Errorf("resolve alpha search endpoint profile: %w", err)
    }
    profileURL, urlErr := buildOfficialCodexEndpointURL(
        profile, string(officialCodexEndpointAlphaSearch), officialCodexEndpointURLInput{},
    )
    if urlErr != nil {
        return nil, fmt.Errorf("resolve alpha search endpoint URL: %w", urlErr)
    }
    targetURL = profileURL.String()
    method = endpointProfile.Method
    ctx = s.bindOpenAICookieJar(ctx, account)
}
```

`OfficialEgressContext` 的 `ProfileMode` 改为 `string(bundle.Mode())`；**该字段有消费者
（写入 `runtimeState.ProfileMode` 后进 identity facts），不可删**。原 404-408 行的
`resolveOfficialEgressRuntime` 与 `runtimeState.CodexReleaseMode` 删除。

### 4.3 `ProfileVersion` 与常量

**A（§3 通过）**：删除 [openai_alpha_search.go:456](../../../backend/internal/service/openai_alpha_search.go:456)
的 `ProfileVersion` 传参 → 删除 [official_egress_codex_0145_profile.go:19](../../../backend/internal/service/official_egress_codex_0145_profile.go:19)
`officialCodexProfileVersion`。`officialCodexVersion0145` 保留（十余处测试消费者 + 已登记为
版本化快照证据常量）。

**B（§3 失败）**：`ProfileVersion` 改为 `bundle.Version()`，常量同样删除，消费者位置记入 §8。

不动 `OfficialEgressContext` 结构体与 accessor，不动其他端点传参。

### 4.4 导出 catalog 加载入口

[release_catalog.go](../../../backend/internal/officialegress/release_catalog.go) 新增：

```go
// LoadReleaseCatalogFromFS 从给定文件系统加载正式 Catalog，供合成夹具与离线校验使用。
func LoadReleaseCatalogFromFS(catalogFS fs.FS) (ReleaseCatalog, error) {
    return loadReleaseCatalogFromFS(catalogFS)
}
```

生产代码不引用。异版本夹具没有替代路径：`ReleaseBundle` 字段私有、只能由 `BundleResolver`
产出，resolver 必须先有 `ReleaseCatalog`，而包外无法合成。

### 4.5 runtime 组装 catalog 参数化

[official_egress_1b_executor.go:146-169](../../../backend/internal/service/official_egress_1b_executor.go:146)
的 `newOfficialEgressTransitionRuntimeWithExecutor` 内部硬编码 `DefaultReleaseCatalog()`，无法
承载异版本。把函数体整体提到新的内层私有函数，多收一个 catalog 参数；外层签名不变：

```go
func newOfficialEgressTransitionRuntimeWithExecutor(
    guard *officialegress.Guard,
    httpUpstream HTTPUpstream,
    executorID officialegress.ExecutorID,
    releaseMode officialegress.ReleaseMode,
    reqProfileResources ...OfficialCodexReqProfileTransportResource,
) (*OfficialEgressTransitionRuntime, error) {
    return newOfficialEgressTransitionRuntimeWithCatalog(
        officialegress.DefaultReleaseCatalog(),
        guard, httpUpstream, executorID, releaseMode, reqProfileResources...,
    )
}

func newOfficialEgressTransitionRuntimeWithCatalog(
    catalog officialegress.ReleaseCatalog,
    guard *officialegress.Guard,
    httpUpstream HTTPUpstream,
    executorID officialegress.ExecutorID,
    releaseMode officialegress.ReleaseMode,
    reqProfileResources ...OfficialCodexReqProfileTransportResource,
) (*OfficialEgressTransitionRuntime, error) { /* 原函数体，catalog 替换 DefaultReleaseCatalog() */ }
```

生产调用点零改动，组装逻辑单份，测试直接调内层。不新增导出面。

## 5. 测试

### 5.1 异版本合成夹具

`fstest.MapFS` 承载合成 manifest 与 blob，经 §4.4 入口装载。Active 沿用内置 `0.145.0`；
Previous 用 **`0.0.0-synthetic`**——该坐标不可能对应真实发布，避免将来真发某版本时全仓搜索
同时命中夹具与真坐标。

**构造方式：对 active snapshot 原文做一次全局字符串替换 `0.145.0` → `0.0.0-synthetic`。**
正式画像共 34 处 `0.145.0`，全局替换一次全改，`Endpoints[].TransportID` 与 `Transports[].ID`
天然保持引用一致：

| 字段路径 | 处数 | 上 wire |
|---|---:|---|
| `Endpoints[].TransportID` | 16 | 间接 |
| `Endpoints[].Headers[].Value` | 9 | 是（`version` header） |
| `Transports[].ID` | 3 | 间接 |
| `Surfaces[].Version` / `SuffixVersion` | 4 | 是（User-Agent） |
| `Endpoints[].Query[].Value` | 1 | 是（`client_version`） |
| `.Version` | 1 | 否 |

**只改顶层 `Version` 会假绿**：`profilecontract` 不做跨字段一致性校验
（[executable_profile.go:250-253](../../../backend/internal/officialegress/profilecontract/executable_profile.go:250)、
[:517-533](../../../backend/internal/officialegress/profilecontract/executable_profile.go:517)），
夹具能装载、断言能通过，但 wire 上的 `version` header 与 User-Agent 仍是 `0.145.0`。

替换后置 `Digest=""` 重算 profile digest，更新 snapshot catalog 条目、release graph previous
节点与 manifest／blob SHA-256。

**夹具自检**：装载后断言合成画像行为事实中不残留 `0.145.0`，失败即视为夹具无效。

> 编码验证：`profilecontract` 未对 `Version` 施加格式校验（已确认），仍先以最小用例确认
> `0.0.0-synthetic` 能通过 snapshot 解析与 digest 校验。

### 5.2 覆盖矩阵

| 层 | 用例 | 断言 |
|---|---|---|
| service | `ProfileVersion` 置空（§3） | 全链路正常，TLS 来自 bundle。**强制** |
| officialegress | 异版本 catalog 解析 | Active／Previous 各得自己的 version、profile digest、release digest、endpoint plan |
| officialegress | 夹具自检 | 合成画像无 `0.145.0` 残留。**强制** |
| service | alpha-search @ Active | target 取自 Active bundle；`version` header 与 UA 为 `0.145.0` |
| service | alpha-search @ Previous | target 取自 Previous bundle；`version` header 与 UA 为 `0.0.0-synthetic`；TLS 等于 Previous bundle 的 `TransportSpec`。**强制** |
| service | 同源性 | 出站 target 与 `invocation.Bundle()` 的 endpoint plan 一致；编译产物 digest 等于该 bundle |
| service | 回归 | OAuth、PAT fallback、API Key 行为不变 |

断言绑定到 bundle 编译出的 wire 事实，不绑定 `bundle.Version()` 标量。

## 6. 门禁

```bash
go test ./internal/service/... ./internal/officialegress/... -count=1
```

```bash
make check-egress-spec
```

final-wire：复用 `catalogdata/migration-artifacts/changeset3/codex_alpha_search_direct/wire.json`
（已含 active/previous 双 capture）。改动前后各采集一次 direct alpha-search capture，空允许列表
比较 `ordered_headers`（含顺序与各字段）、`body`、`final_host`、`final_path`、`tls_profile_digest`、
`transport_id`，以及 `release_digest`／`profile_digest`／`bundle_digest`／`connection_pool_digest`
四个摘要，全部要求零差异。基线不可复现则停止本变更集单独排查。

## 7. 非目标

- 不改 alpha-search、PAT fallback、API Key 产品语义。
- 不删历史证据、画像与测试夹具中的 `0.145.0`。
- 不重构 ingress runtime 绑定与 body 预投影。
- 不把 `0.0.0-synthetic` 写入任何生产画像或发布资产。
- 不处理 direct alpha-search `connectionPoolID` 为空、连接池不按画像隔离（换版门禁 §3.2 范围）。

## 8. 风险与回滚

| 风险 | 处置 |
|---|---|
| §3 判定失败 | 走 §4.3-B，方案已预置 |
| 夹具内部不一致假绿 | §5.1 全局替换 + 自检断言；矩阵断言绑定 wire 事实 |
| 顺序重排影响 `AttemptIdentity` | §4.1 断言确认，不成立则显式绑定 |
| `0.0.0-synthetic` 被 `profilecontract` 拒绝 | 已确认无格式校验；仍先跑最小用例 |

上游侵入面：`openai_alpha_search.go` 在 ledger 中为 `source: upstream`／`strict_surface`，改动
落在既有 fork 接缝内，实施后按规格 §3.5 复算 ledger 确认标记与计数不变。

回滚：`openai_alpha_search.go` 单文件 + 一处常量删除 + 一处函数导出 + 一处私有函数提取，
`git revert` 可完全恢复；无数据、配置或机器资产变更。

## 9. 跟踪

- §3 判定结论：待补充
- 实现：待补充
- 测试：待补充
- final-wire 证据：待补充
- ledger 复算：待补充
- 完成日期：待补充
