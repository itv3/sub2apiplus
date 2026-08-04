# 变更集 6 正式复审修复报告

## 当前结论

正式复审提出的两个 P1 与三个 P2 均已完成实现和机器证据补强；生产实现、正确性门禁、
final-wire、三组 benchmark、前后端及工具链回归均通过。正式复审已经确认通过，当前状态为
`accepted`。

## P1-1：Benchmark 同负载证据链

- pre/post Body、Catalog、Profile benchmark driver 已分别保存为冻结证据；pre driver 仅作
  开发前计时语义证明，不要求在 post API 上编译；
- Body pre driver 摘要为
  `c2902c023bfddb35d6b746a103069774d7d14c4b2a300efa1efb25632c320cab`，post driver 摘要为
  `bce3b5c5aace49adee353ce45d893a048b3c577d4244242fa34a911f1f847b4f`；
- 独立 validator 将唯一允许差异锁定为 `NewReplayableRequestBody(semantic.Body)` 到
  `semantic.Body` 的新旧 API 适配，并逐项验证 fixture、循环、attempt 数、`SetBytes`、
  `ResetTimer`、Prepare/Compile、错误检查和 sink 消费语义；Catalog/Profile driver 逐字节一致；
- validator 直接解析每项 10 个 pre/post 原始样本，重新计算中位数、`B/op`、
  `allocs/op` 和阈值，不读取元数据中的 `acceptance.result` 作为结论；原始输出、benchstat、
  driver 与计算结果均已校验摘要；
- mutation 覆盖 attempt 数、计时边界、sink 消费、fixture、下降不足和耗时回退。

Body 复算结果：

| Case | ns/op 变化 | B/op 降幅 | allocs/op 降幅 |
|---|---:|---:|---:|
| LargeUnchanged | -62.46% | 31.86% | 46.06% |
| LargeDirty | -62.45% | 31.86% | 46.27% |
| Retry | -60.92% | 31.86% | 46.34% |
| OpenWS | -10.01% | 56.75% | 33.87% |

Catalog/Profile post 复算结果：

| Case | post 中位数 | post B/op | post allocs/op |
|---|---:|---:|---:|
| ReleaseCatalogResolve | 50.80 ns | 0 | 0 |
| VersionProfileResolve | 339.8 ns | 0 | 0 |
| EndpointResolve | 818.6 ns | 3968 | 2 |

Endpoint 的两次分配只来自端点深拷贝；运行时路径不再创建 Catalog map，不再进行 JSON
编解码或摘要计算。

## P1-2：Execution Policy 错误边界

- `Executor.Execute` 不再把 `BeginInvocation` 的初始化、Bundle／参数校验或 InvocationID
  错误误标为执行策略拒绝；
- 真实 `executionPolicies.acquire` 失败出口统一包装
  `official_egress.execution_policy.rejected`、`op=executor.acquire_policy`；
- 生产链测试覆盖 minimum interval、并发槽位等待 `context.DeadlineExceeded`、未初始化
  Executor 与非法 Bundle，并同时验证 `RuntimeErrorCodeOf`、`errors.Is`、`errors.As` 及
  deadline 错误链。

复审修复导致的 `executor.go` 冻结源码变化通过独立
`review-source-transition.json` 承接；变更集 4、5 与变更集 6 原 transition 均未覆盖。

## P2 修复

### Catalog 稳定错误码

- 新增 `official_egress.catalog.load_failed` 与
  `official_egress.catalog.resolve_failed`；
- load 包装位于可注入 `fs.FS` 的真实加载入口，测试覆盖 JSON 语法错误与缺失文件并保留
  `*json.SyntaxError`、`*fs.PathError` 错误链；
- resolve 包装只位于非法 mode 和未初始化 Catalog 的失败出口；成功热路径没有新增接口调用、
  `defer` 或动态类型判断，现场复跑仍为 `0 B/op、0 allocs/op`。

### 冲突 transition 独立门禁

- 独立 validator 从冻结的变更集 5 baseline、post inventory 与 receipt 复算摘要、单元 ID
  增删集合、changed unit 数量和 changed path closure；
- 复算结果为 full `247 → 247`、governable `90 → 90`，新增与删除均为 0，变化单元分别为
  20 与 7，变化路径仅 `backend/internal/service/openai_ws_pool.go`；
- mutation 已证明错误 baseline、等量替换 ID、扩展变化路径、伪造计数和错误 receipt 均会失败。

### 前端与工具链

- `make test-frontend` 通过：ESLint、`vue-tsc` 与 Vitest 全部通过，Vitest 为 6 个测试文件、
  102 项测试；
- `make test-capture-tools` 通过：326 项通过、3 项跳过；
- workspace transition 没有前端路径变化，因此无需申请未执行豁免。

## 完整回归与冻结事实

- `go test ./... -count=1` 通过；
- Body、Catalog、RuntimeError、Execution Policy 与 service 聚焦 `go test -race` 通过；
- `make check-egress-spec` 通过，包含 benchmark 证据 mutation、冲突 transition mutation、
  workspace transition、生成物确定性复算、源码冻结与 final-wire 门禁；
- 变更集 6 的 56 份 post final-wire 与变更集 5 post 使用空允许列表比较，差异严格为零；
- runtime Catalog 完全脱离 `testdata`，内容寻址导出可确定性重建；历史 `testdata` 与变更集 5
  三件套均未改写；
- workspace transition 从开发前 498 个非 clean 路径出发，完整枚举当前 git 状态与被
  `docs/*` 忽略的变更集 6 证据，并按存在状态、类型、权限、大小及 SHA-256 确定性复算。

## 环境说明

本机 Go launcher 为 1.26.4；`backend/go.mod` 在同一冻结 HEAD 中声明 `go 1.26.5`，因此
pre/post 命令均由 `GOTOOLCHAIN=auto` 选择 Go 1.26.5 执行。两侧 GOOS/GOARCH、CPU、
GOMAXPROCS、HEAD、HEAD tree、命令和 fixture SHA-256 均一致。冻结 driver、原始结果、
benchstat 输出与机器计算结果分别位于 `baseline/benchmark-drivers/`、
`post/benchmark-drivers/`、`post/benchmarks/` 与 `post/benchmark-calculation.json`。
