# 变更集 4 验收报告

日期：2026-08-04

## 基线与范围

- 开发基线由 `base-receipt.json` 与 `base-files.sha256` 共同冻结，不以 `HEAD` 冒充脏工作区完整基线。
- `base-receipt.json` SHA-256 为 `95c11c24707242adca40456495c6be2113a56d37d5964696a00936c2973020bf`；该值由源码测试常量独立锚定，不由 receipt 自报。
- 基线文件清单共 536 项，清单 SHA-256 为 `701782a72cf23b032e9a11aed5d08de9978cc5041f52ed7aa731035c94e7369e`。
- 变更集 3 冻结源码的有意变化登记在 `changeset3-source-transition.json`，收据 SHA-256 为 `85cfb8e5324b5063b480f5e27cab1781500a5a3aba1957aae581f0ed0e62d478`；旧 final-wire manifest 未被改写。
- 本次仅处理变更集 4，没有提交、暂存或修改工作区中无关的既有差异。

## 实施结果

1. 新增 `ExecutableProfile` 启动期编译层；`SnapshotDoc/ProfileSpec` 继续作为完整证据层参与官方摘要、round-trip 和证据重建。
2. 未知枚举、未绑定可执行字段、非法生命周期组合和不完整 WS compression 组合均在启动期失败；Executor 只消费可执行画像。
3. HTTP turn-state 使用类型化 scope，绑定 group、local account、规范 authority、ReleaseDigest、session、turn 及两者来源；键使用 `sub2api.turn-state.v1\0` 域分隔和完整 SHA-256。
4. 纯内容推导和 request-local 身份禁止进入跨请求 store；WS→HTTP fallback 的 authority 仍只能来自同一冻结 Bundle 的静态 endpoint 闭集。
5. 生命周期编译为单一 `ResourceLifecyclePolicy`；跨调用能力由闭集 scope 推导，`Lifecycle + Scope + RetryReusesClient` 受严格真值表约束。
6. 连接身份绑定 release、executable profile、账号、authority、backend/transport/TLS、具体代理摘要、自定义 CA 内容摘要及生命周期 scope；旧 idle 资源具备关闭/淘汰验证。
7. WS offer、context takeover、RSV1、raw-deflate 四项作为整体进入签名计划并由 dialer 消费。
8. WHAM 列出 Codex CLI 0.145 OpenAPI 的 17 个计划值；已批准家族才使用顶层 5h/7d 窗口，其余已知值、未知值和证据不足身份均使用闭集 reason code fallback。Spark shadow 继续走 `codex_bengalfox`。
9. `backend_client_long_lived` 现在强制要求 `CrossCallConnectionReuse=true`；三个 WHAM 端点改绑独立长期 transport。旧快照保持不可变，新快照以新 digest 追加，运行时目录和 release graph 显式切换。
10. BaseReceipt 冻结测试同时硬编码 receipt/manifest 摘要及 schema、changeset、HEAD、HEAD tree、manifest 路径和 536 条计数；清单逐项校验完整小写 SHA-256、唯一且排序的规范相对路径，并拒绝目录穿越和未知 receipt 字段。

## 强制验收项

| 验收项 | 结果 |
|---|---|
| 未知枚举和非法生命周期组合导致启动失败 | 通过 |
| `backend_client_long_lived + CrossCallConnectionReuse=false` 启动失败 | 通过 |
| 证据专用字段改变证据摘要且不改变 executable digest | 通过 |
| group、账号、authority、release、session、turn 任一变化时 state 不复用 | 通过 |
| 内容兜底 session/turn 不跨请求复用 | 通过 |
| `RetryReusesClient=false` 时不同 attempt 使用不同资源 scope | 通过 |
| WS compression 四字段 mutation 改变行为或被拒绝 | 通过 |
| 17 个计划值的 289 个凭据/响应组合均得到 supported 或闭集 fallback | 通过 |
| active/previous 切换后旧 idle 资源可关闭或淘汰 | 通过 |
| BaseReceipt 与 536 项 manifest 具备独立源码摘要及结构锚点 | 通过 |

## 门禁结果

```text
go test ./internal/officialegress/... -count=1                         PASS
go test ./... -count=1                                                 PASS
go test -race ./internal/officialegress/... -count=1                   PASS
go test -race ./internal/service -run '<Changeset 4 聚焦测试族>'       PASS
go test -race ./internal/repository -count=1                           PASS
make check-egress-spec                                                 PASS
go build ./cmd/server                                                  PASS
git diff --check                                                       PASS
```

`make check-egress-spec` 另提示 `official_egress_codex_files.go` 扫描命中由 8 降至 7、`official_egress_transport.go` 由 5 降至 4，可在后续独立治理中收紧基线；该提示不影响本次门禁结果。
