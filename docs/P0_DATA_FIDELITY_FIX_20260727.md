# P0 数据保真与 Lite 判定修复记录（0.1.165-5）

日期：2026-07-27  
目标版本：`0.1.165-5`  
范围：闭合独立审核在 `0.1.165-3` / `0.1.165-4` 两轮中提出、且至 `0.1.165-4` 仍未被处理的问题。

## 1. 背景与口径

`0.1.165-4` 修复的是第三方工程师复核意见（FR-P0-01~FR-P2-01），与独立审核提出的问题清单基本不重叠。经逐条核验，独立审核列为"必须修"的四项 P0、一项高风险外推和两项能力缓存缺陷，在 `0.1.165-4` 中代码未作任何改动。本轮专门处理这批遗留项。

验收口径不变：只有实际触发并验证过的门禁才标记为通过；没有 wire 证据的结论必须标注为源码外推。

## 2. 修复项与验收

| 编号 | 问题 | 修复 | 验收证据 | 状态 |
|---|---|---|---|---|
| AF-P0-01 | Lite 工具归一化在 Finalizer 之前用裸 `json.Unmarshal` + `map` 重编，破坏大整数精度与嵌套键序。触发率 100%（任何缺 `reasoning.context` 的第三方请求），入口 4 个（HTTP 1 + WS 3） | `normalizeOpenAIResponsesLiteToolsPayload` 改用 `decodeOfficialJSONObjectUseNumber` + `marshalOfficialJSONObjectPreservingOrderAndRaw` | `TestOpenAIResponsesLiteNormalizationPreservesNestedRawData`；回退即红 | 已修复 |
| AF-P0-02 | 同类破坏位于 namespace 摊平、请求体 map 解码、API Key Codex 画像三处 | 三处统一改用保真编解码 | `TestFlattenOpenAIResponsesNamespacesPreservesNestedRawData` | 已修复 |
| AF-P0-03 | 保真编码器对重复顶层键计算出负数 cap，触发 `makeslice: cap out of range` | `decodeOrderedRawJSONObject` 按首次出现登记 key、值保留最后一次，从根因消除两个 panic 点 | `TestMarshalOfficialOrderedJSONObjectHandlesDuplicateTopLevelKeys`、`TestDecodeOrderedRawJSONObjectDeduplicatesKeys`；回退后实测复现 panic | 已修复 |
| AF-P0-04 | HTTP 路径 Lite 判定两次且输入不同：入站用原始 model、出站用别名映射后的 model，`gpt-5.6` 等别名必然分裂 | `openAIModelCapabilityKey` 增加已知别名解析，与出站落到同一 manifest slug；只接受确定映射，未知模型不得冒名命中 | `TestOpenAIModelCapabilityKeyMatchesUpstreamAliasResolution` | 已修复 |
| AF-P1-01 | turn 起始时间 check-then-act 竞态，`rememberOfficialOpenAIHTTPTurnStart` 无返回值，调用方返回自己那份 | 改为返回实际生效值，调用方采用返回值后再写入 context | `TestResolveOfficialOpenAIHTTPTurnStartReturnsEffectiveValue` | 已修复 |
| AF-P1-02 | 上游返回 200 但清单为空/不可解析时静默返回，不进入失败退避，导致每个未知模型请求都放大一次 `/models` 调用 | 两个静默返回点均调用 `markLoadFailure` | 既有负缓存用例覆盖 | 已修复 |
| AF-P2-01 | bundled 能力清单无权威来源，测试脚手架预置清单掩盖其正确性 | 固化 Codex 0.145.0 真实 `/models` 响应为 fixture，新增 bundled 与 fixture 的逐项对账，以及不依赖 manifest 的 bundled 解析用例 | `TestBundledOpenAIModelCapabilitiesMatchesCapturedManifest`、`TestOpenAIResponsesLiteCapabilityResolvesFromBundledWithoutManifest` | 已修复 |
| AF-P2-02 | 文档表述与代码不符 | 订正 README：exchange 实为 form-encoded（仅 refresh 用 JSON）、keeper 实际版本、token 端点无 wire 证据 | README §1.1.3 / §3 | 已修复 |
| AF-P2-03 | 生产代码与验收文档游离在版本控制外 | 6 个生产文件、2 个新包与本轮文档全部纳入 Git；`.gitignore` 补齐 docs 白名单 | `git status` | 已修复 |

## 3. 未修复项与理由

| 项 | 结论 |
|---|---|
| OAuth refresh scope 外推 | **未补证**。OpenAI 删 scope、Anthropic 增 scope 两处改动均无 wire 实证；本轮抓包同样不覆盖 token/usage 端点（`oauth/token`、`grant_type`、`refresh_token`、`axios/` 在归档中 0 命中）。这类端点只在 token 过期时触发，无法在常规业务抓包中捕获。已在 README 标注证据等级，代码维持现状，不做无依据改动。 |
| 未知模型冷加载 8s 同步阻塞 | **维持现状**。AF-P1-02 修复后，空清单场景进入退避，不再每请求阻塞，最坏情况已消除。缩短超时会降低慢网络下的加载成功率，收益不足以抵消风险。 |
| 生产 `x-client-request-id` 缺失导致 502 | **仅记录，未改行为**。10:10 有一次真实用户请求因缺该 Header 被 strict 校验拒绝返回 502。该校验是官方画像身份自洽性的一部分，放宽会削弱契约；但也说明存在官方之外的客户端形态会踩中。需要先定位该客户端来源再决定豁免边界，不在本轮盲目放宽。 |
| Anthropic 证据强度 | **结构性未改善**。仍为单场景单记录，且受 #50 组织禁用阻断，无法取得成功响应。非代码问题。 |

## 4. 补充修复：终态定型之前的三处整体重编码

首轮修复后抓包复验发现 `reasoning` 仍被重排为字典序：入站 `effort/summary/context`，出站 `context/effort/summary`。定位到 Lite 归一化**之后**还有三处整体重编码未走保真路径——首轮只改了对应的解码，没改编码：

| 位置 | 场景 |
|---|---|
| `openai_gateway_forward.go:556` | sjson patch 失效后的整体重编码 |
| `openai_gateway_forward.go:948` | `invalid_encrypted_content` 重试时重建正文 |
| `openai_gateway_request_body.go:1178` | 空 base64 图片清洗后重建正文 |

三处统一改为以当前正文为原始字节基准的保真编码。新增
`TestOpenAIResponsesLiteNormalizationKeepsModifiedObjectKeyOrder` 专门覆盖"被改写的嵌套对象"这一类——`reasoning` 因必须补 `context=all_turns` 而无法复用原始字节，是最容易退化的位置。

## 5. 本地工程门禁

| 范围 | 结果 |
|---|---|
| `go build ./...` / `go vet ./...` | 通过 |
| `go test ./... -count=1` | 通过，46 个包 |
| `go test -tags=unit ./... -count=1` | 通过 |
| `go test -tags=integration ./... -count=1` | 通过 |
| keeper build / vet / test | 通过 |
| 前端 lint / typecheck / build | 通过 |
| 前端 Vitest | 191 个文件、1318 个测试全部通过 |
| 抓包工具 | 100 个测试通过，1 个按环境跳过 |
| `golangci-lint` | **未执行**：本地与 Vircs 均未安装该工具，本轮不冒充通过 |

破坏性反例验证：临时回退 `decodeOrderedRawJSONObject` 的去重后，
`TestMarshalOfficialOrderedJSONObjectHandlesDuplicateTopLevelKeys` 与
`TestDecodeOrderedRawJSONObjectDeduplicatesKeys` 双双变红，并精确捕获
`runtime error: makeslice: cap out of range`，随后已还原。

## 6. 正式发布与生产部署（最终状态）

本轮修复已作为正式版发布，生产运行的是已发布镜像而非本地编译镜像：

| 项目 | 值 |
|---|---|
| GitHub Release | `v0.1.165-5`（annotated tag，CI 构建成功，用时 9m4s） |
| 提交 | `0922523a6`，含合并进来的 API Key 画像 TLS 收口（`0f2c914f9`） |
| 生产镜像 | `ghcr.io/itv3/sub2apiplus:0.1.165-5` |
| 镜像 ID | `sha256:11ea1280ce56e33043767a8419e8f72eb2f23a6554d2b3624eae7bf78c2a6c06` |
| 容器内二进制 | `Sub2API 0.1.165-5 (commit: 0922523a6dd097dca243f4eb9deba40f00e37810, built: 2026-07-27T04:21:59Z)` |
| 状态 | `healthy`、`RestartCount=0`、`/health` 返回 `{"status":"ok"}`、启动后 0 条 ERROR |
| compose 备份 | `/root/Docker/sub2apiplus/backups/docker-compose.pre-ghcr-0.1.165-5.yml` |

容器内二进制自报的 commit 与 `origin/main` HEAD 完全一致，可确认运行的就是本轮提交的代码。PostgreSQL 与 Redis 保持 7 天连续运行未受影响，数据卷未动。

清理结果：删除 7 个历史构建目录、8 个临时镜像，`docker builder prune` 回收 10.5 GB。根分区占用由 29 G 降至 18 G；数据卷 1 个 0 B，未触碰。保留 `p0-data-fidelity-fix-0.1.165-5-20260727T034217Z` 本地镜像与最近两份 compose 备份作为回滚点。

## 7. 阶段性 Vircs 构建与切换（过程记录）

- 源码目录：`/root/sub2apiplus-build/p0-data-fidelity-fix-20260727T032232Z`
- 运行镜像：`sub2apiplus:p0-data-fidelity-fix-0.1.165-5-20260727T034217Z`
- 镜像 ID：`sha256:f46214b7a1fcd892ee9673771b72e2282b7ad0ce76ee473db2872358747a5ca0`
- 容器内二进制：`Sub2API 0.1.165-5 (commit: docker, built: 2026-07-27T03:42:46Z)`
- compose 备份：`/root/Docker/sub2apiplus/backups/docker-compose.pre-0.1.165-5-20260727T032232Z.yml`
- 变更范围：compose 仅主服务 `image` 一行（已用 diff 核对，除标签外无其他差异）；PostgreSQL、Redis、keeper 与数据卷未动
- 最终状态：主服务 `healthy`、`RestartCount=0`、`/health` 返回 `{"status":"ok"}`，启动后无 panic/FATAL/ERROR

## 7. 抓包验收

归档：`local-analysis/captures/data-fidelity-fix-0.1.165-5-20260727-115500/`，53 个抓包文件 + MANIFEST，共 54 项纳入 `SHA256SUMS`，实测 54/54 通过；目录 0700、文件 0600；凭据模式全部 0 命中。

核心 wire 证据（同一次 ingress→egress 对照）：

```
入站  "reasoning":{"effort":"low","summary":"auto","context":"none"}
出站  "reasoning":{"effort":"low","summary":"auto","context":"all_turns"}
```

键序守恒，仅 `context` 的值按 Lite 契约改写。Lite 终态定型同时确认正确：顶层字段为官方契约序，`tool_choice=auto`、`max_output_tokens` 已删除、`store=false`、`stream=true`、`parallel_tool_calls=false`、`include=["reasoning.encrypted_content"]`、顶层 `tools` 已迁入 `input[].additional_tools`。

采集组别：官方 Codex 基准（direct pcap 3.1 MB + MITM）、Sub2API OpenAI HTTP（2×200）、OpenAI WS（2×101，17 事件，38 帧）、Anthropic HTTP（1 条出站，业务 502）、direct TLS（pcap 285 KB）。

**未覆盖项**，不得当作通过：

1. 未生成跨运行 `summary.json` 比较报告。`verify_final_review_captures.py` 需要非 Lite、compaction 等全套配对运行，本轮只采集了验证本次修复所需的组别。第 7 节结论来自对原始记录的逐字段直接比对，不是比较器输出。
2. `gpt-5.6` 别名不在入口 `/v1/models` 列表中，入口直接 404，AF-P0-04 无法用抓包复现，仅由单元测试锁定。
3. Anthropic 无成功业务响应，#50 组织仍被官方禁用。
4. `golangci-lint` 未执行。

## 8. 归档路径统一

本轮抓包最初被拉回到仓库顶层 `captures/`——那是 0.1.165-3 时期的旧位置，其 ignore 规则已随 0.1.165-4 把证据迁往 `local-analysis/captures/` 而废弃，因此该目录当时未被忽略。已将归档移入 `local-analysis/captures/`，与前几轮统一，由 `/local-analysis/` 一条目录级规则覆盖，并删除顶层 `captures/` 目录及其冗余规则。移动后复验 `SHA256SUMS` 仍 54/54 通过，权限 0700/0600 保持。

核对确认全程没有任何抓包文件进入暂存区。

另需注意：`/local-analysis/` 与当时的 `captures/` 规则**至今都未提交**（`git show HEAD:.gitignore` 中均为 0 条），一直只存在于工作区。本轮 `git add` 后才进入暂存区，落地提交前的全新 clone 不带这条规则。
