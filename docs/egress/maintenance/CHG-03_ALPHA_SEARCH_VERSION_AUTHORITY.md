# CHG-03：alpha-search 版本单一权威 — 撤销记录与遗留判定项

> 状态：已撤销，不进入编码（2026-08-06 三方审核 `changes_requested`，复核逐条属实）
> 基线：`58d0827c3`
> 清单：[OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md](OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md) CHG-03 撤销条目

## 1. 撤销结论

原方案（alpha-search 出站 target 与 Executor 编译统一到同一冻结 `ReleaseBundle`）不再作为
必要变更集推进。原方案未获确认、未进入编码，其正文由本撤销记录替换；alpha-search 同源性
验证降级为换版门禁 §3.2 的实证检查项，只有实证失败才重新立项最小修复。

## 2. 撤销依据（已逐条复核的代码事实）

| # | 事实 | 位置 |
|---|---|---|
| 1 | `DefaultReleaseCatalog()` 为 `sync.OnceValues` 进程级不可变单例 | `backend/internal/officialegress/release_catalog.go:377-384` |
| 2 | 生产 runtime 构造唯一入口内部硬编码该单例，无第二 catalog 注入口 | `backend/internal/service/official_egress_1b_executor.go:116,146-169` |
| 3 | alpha-search 的 target 解析（按 mode 查全局 catalog）与 Executor 的 bundle 解析同源同 mode，mode 在请求内冻结不变 | `backend/internal/service/openai_alpha_search.go`、`official_egress_codex_release_projection.go:14-45` |
| 4 | changeset3 final-wire 生成器当前基线不可复现：首个 capture 即被统一比较器拒绝，六个 digest 与旧 approved delta 不符 | `TestGenerateChangeset3ProductionFinalWire` 实跑失败 |
| 5 | changeset3 的 alpha-search capture 由测试自建 target 与语义 request 直进 `invocation.Execute`，不经过 `ForwardAlphaSearch`/`buildOpenAIAlphaSearchRequest`，56 面零差异不能证明 service 层构造路径的回归安全 | `backend/internal/service/official_egress_changeset3_production_final_wire_test.go:272-507` |
| 6 | `officialegress` 包内已有合成异画像 catalog 的测试模式（改 snapshot 重建 catalog），原方案 §4.4「包外无法合成、须新增公共导出」不成立 | `backend/internal/officialegress/changeset2_synthetic_rollback_test.go` |
| 7 | 删除传参与常量属旧路径删除，须走规格 §5.2 七步退休流程或先立正式豁免，原方案两者皆未安排 | `docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md` §5.2 |

综合：「URL 来自全局 catalog、bundle 来自 runtime catalog 且可能分叉」无生产可达失败链；
换版在现有架构下是往同一内置 catalog 装两个版本、Active/Previous 两个 mode 各查各自槽位，
不形成 URL/version 混搭。原方案需先新增公共导出与 runtime catalog 参数化，才能在测试中
制造生产不存在的分叉前提，再以此证明修复必要——论证倒置，且与清单原则 3/4
（不以减少解析次数为目标；wire 无错不因结构重复修改生产路径）冲突。

## 3. 遗留判定项：固定 `ProfileVersion` 残留

唯一从原方案析出、值得单独跟踪的事项。定性为疑似无效兼容残留的清理调查，
不是正确性修复；不排期、不阻塞任何当前工作，可与换版门禁合并执行。

**现状：**`buildOpenAIAlphaSearchRequest` 向 `OfficialEgressContext` 提交固定常量
`officialCodexProfileVersion`（= `0.145.0`，定义于
`backend/internal/service/official_egress_codex_0145_profile.go:19`，生产引用仅
`openai_alpha_search.go:456` 一处）。alpha-search direct 已走 1B Executor，最终
header/body/TLS/release 由 bundle 编译，该值当前恰与 Active/Previous 版本一致，无生产错误。

`OfficialEgressContext.ProfileVersion()` accessor 在其他路径有四个真实消费者，判定不得
用 grep 空扫替代：

- `official_egress_transport.go:105`、`:128`（HTTP/WS transport provider 的 TLS 画像解析）
- `official_egress_openai_http.go:424`（派生工具呈现归一化）
- `official_egress_openai_ws.go:533`（派生工具呈现归一化，WS 侧）

**判定方法（须全部满足才可认定「该赋值无行为消费者」）：**

1. 调用图／AST：证明上述四个消费者在 alpha-search direct（1B Executor）路径不可达，
   或可达但读取其他来源；
2. mutation 差分：将该传参改为明显错误版本（如 `9.9.9-mutation`）与置空各执行一次完整
   `ForwardAlphaSearch` 直连链路，与正确值对照，三者最终 wire（有序 headers、User-Agent）
   与 TLS 摘要完全一致；仅「置空可执行」不足以判定（空值可能触发跳过分支而掩盖消费）；
3. 确认该值不进入 digest、Guard 判定、transport selection 与错误分类。

结论表述限定为「alpha-search Executor direct 路径中的该次赋值无消费者」，不得外推为
「`ProfileVersion` 无消费者」。

**判定后处置：**

- 无消费者：按规格 §5.2 完整退休（含机器退休收据），或先在清单为「单处赋值 + 私有常量」
  的简化流程立正式豁免并取得明确批准后执行。`officialCodexVersion0145` 常量保留
  （十余处测试消费者 + 已登记为版本化快照证据常量）。
- 有消费者：记录完整消费链，另行提交最小方案改为从执行期 bundle 派生，单独确认后实施。

## 4. 衍生现存问题（不随本撤销关闭）

- **changeset3 历史生成入口与旧 approved-delta 契约漂移**（撤销依据 #4）：不可复现的是
  旧生成入口（`TestGenerateChangeset3ProductionFinalWire`、
  `TestGenerateChangeset3ExactApprovedDeltas`）及其与旧 approved delta 的比较契约，
  不是共享的 `changeset3BuildProductionFinalWireCaptures`——changeset6 生成器仍复用该
  builder 且可复现，是当前通用 56 面 final-wire authority，但不替代换版清单
  （[OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md](OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md)）
  §3.2 降级检查项要求的 alpha-search 真实 service 链证据。
  当前可复现的对照组：`CHANGESET6_POST_FINAL_WIRE_OUTPUT=<临时目录> go test ./internal/service
  -run TestGenerateChangeset6PostFinalWire -count=1`。
- 重新引用旧生成入口前必须先修复；若决定删除旧入口或旧 approved-delta 契约，使用专用
  gate/evidence retirement receipt（先例：`docs/egress/consolidation/pairing-gate-retirement.json`），
  并证明 changeset6 仍依赖的共享 capture builder 保持完整。规格 §5.2 面向生产兼容层，
  不适用于纯历史测试生成器。在此之前不为让历史工具变绿而修改代码或重建历史证据。
- 任何后续变更集引用 final-wire 资产做门禁前，必须先验证：生成器在当前基线可复现，且
  捕获链真实经过被改的生产函数。两条有一条不成立，该门禁即无效。

## 5. 跟踪

- 撤销日期：2026-08-06
- 审核：三方 `changes_requested`（P1-1 必要性依据与代码事实不符、P1-2 门禁不可执行且不覆盖
  修改路径、P1-3 未走 §5.2、P2-1 导出面缺必要性、P2-2 body 同源表述矛盾、P2-3 状态字段错误、
  P2-4 置空测试不足）；复核确认全部属实
- 遗留判定项（§3）：待启动，不排期
- 衍生问题（§4）：排查中
