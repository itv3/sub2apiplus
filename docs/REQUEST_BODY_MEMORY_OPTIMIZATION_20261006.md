# 请求体内存优化测试记录（2026-10-06）

**状态：本轮实现及验收已收尾，整体 2.5 倍目标未完成，未发布。** HTTP 大正文单次转发已明显下降，但默认 GC 设置下的持续请求、部分压缩入口及完整 WS 会话仍未达标。建议维持现有线上容量，不放宽准入预算或请求上限，不调整线上 GC 设置。隔离工作树的本次改动验证已完成；主仓库仍有五项用户既有文档摘要冻结失败，不能宣称主仓库全部测试通过。

## 1. 测量口径

- 目标是大请求的**请求相关堆峰值／原始正文大小 ≤2.5**，包含原文及本次请求的固定开销；这不是 zstd 压缩率，也不是容器总内存／正文大小。小请求单独观察固定开销。
- `forward`：正文已在内存，先预热并回收，再测 `Forward`。请求峰值为 `峰值 HeapInuse－基线 HeapInuse＋在途原文字节`。
- `read_forward`：从文件流读取已准入正文，覆盖读取、解压、归一化、编译、压缩和转发；基线不含原文，请求峰值直接取 `HeapInuse` 增量。不包含鉴权、账号调度及真实入站网络开销。`chunked` 样本模拟未知 `Content-Length`，不包含 HTTP 分块协议解析。
- `ws_read_service_session`：覆盖真实读帧、官方服务会话、实际原生 WS 或自动 HTTP bridge、同连接多轮保留及本地两端 WS 编解码；不含公网、TLS 和完整鉴权／调度 handler。分母为各并发连接最大单帧字节数之和，不按多轮累计流量稀释；`budget_enforced=false` 表示计量但不以线上共享预算拒绝样本，不能当作准入验收。
- 每 2ms 采样并补入请求结束时的 `HeapInuse`。累计分配、RSS、cgroup 峰值另记；采样峰值可能漏过更短暂的分配尖峰。持续场景 `2×16.8MiB×4` 表示两个并发槽各连续四次，倍率分母为两个在途正文合计约 33.6MiB，不是八次累计流量。
- 测量窗口中没有强制 GC；窗口前用于建立基线，窗口结束后的强制回收仅用于检查资源能否释放。因此“回收后下降”不等于生产请求结束后立即归还同等内存。
- HTTP 容器记录：Linux ARM64、Go 1.27.1，768MiB 内存且不额外放开 swap，2 CPU、`GOMAXPROCS=2`、`GOMEMLIMIT=512MiB`、默认 `GOGC=100`；各条记录使用全新容器、禁用网络，本地上游桩按流丢弃正文。底图为 `0.2.13-2`，入口执行的是本次候选测试二进制；`v8/v9` 是测量标签，不能当成线上发布版本。
- 本机记录来自 macOS ARM64、Go 1.27.1、`GOMEMLIMIT=512MiB`；缺失的 RSS/cgroup 字段为不可用，不能按零解释，也不与容器 CPU、耗时直接横比。下表均为已取得的单轮记录，尚未形成每项多轮统计区间。

## 2. 已实现的主要改动

| 环节 | 改动与边界 |
| --- | --- |
| 正式 JSON 编译与压缩 | 原始 JSON 字节区间和修改内容按画像字段顺序直接写入 zstd，取消完整未压缩出站副本；每份定型正文只压缩一次，结束后取得准确长度。 |
| 压缩输出 | 64KiB 不可变分段存储；发送、摘要和 `GetBody` 重放直接读取分段，正常路径不重新拼接。Reader 读完或关闭后释放自身引用。 |
| 编码器 | 等级仍由画像决定，窗口改为 512KiB，启用低内存选项；等级 3 最多保留一个空闲弱引用，并发借用独占。归还前断开旧输出 Writer，不恢复原有常驻池。 |
| JSON 局部修改 | 显式 `instructions`、工具和 `input` 转换尽量引用原文区间；减少索引与对象树残留。完整 JSON token 可跨分段组织；无法证明安全的形态仍走原兼容解析。短字符串复用仅限单次解码且有容量上限。 |
| HTTP 读取 | 已知长度正文按实际长度分配；未知长度大正文在符合条件的非内存文件系统暂存，获得长度后一次分配最终正文。保留 wire／解压后限额，限制 zstd 解码窗口，取消和失败清理临时文件；tmpfs 或无法确认文件系统时回退有界读取。 |
| 分配前准入 | 宽松 JSON 规范化先扫描输出长度、检查上限并补足预留，之后一次分配；无须转义时复用原切片。BOM 空正文保持原错误语义，压缩／未知长度预留不下调。 |
| WS 与 bridge | 加入 HTTP／WS 共享额度的帧读取、处理中正文及跨轮保留计量，按底层数组去重并处理路径交接；bridge 转换减少完整历史复制。重放序列化前将 HTML／Unicode 转义增长纳入上界并补预留，扫描估算不提前生成副本。最终完整 WS 测量仍未达 2.5 倍。 |

## 3. HTTP 单次与持续请求

原始容器记录见 [http-container-v8.jsonl](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/http-container-v8.jsonl)。单次 `forward` 的大正文结果如下，均为 `GOGC=100`：

| 标称正文 | 原生倍率 | 显式 `instructions` 倍率 | 难压缩倍率 | 三类样本 cgroup 总峰值范围 |
| --- | ---: | ---: | ---: | ---: |
| 16.8MiB | 2.04 | 2.00 | 2.16 | 49.8～52.4MiB |
| 33.6MiB | 2.02 | 1.98 | 未测 | 81.8～83.3MiB |
| 50MiB | 1.97 | 1.97 | 2.06 | 113.8～120.2MiB |
| 80MiB | 1.96 | 1.96 | 2.04 | 172.4～182.2MiB |

难压缩正文的生成按完整历史项增长，标称 16.8／80MiB 的实际大小约为 17.1／80.2MiB；倍率按真实字节计算。上述通过仅表示已列样本的单次堆峰值满足目标，不能据此放开线上 80MiB 请求。

50MiB 的 `read_forward` 覆盖三种入口方式：

| 入口 | 原生倍率／请求峰值 | 显式指令倍率／请求峰值 | cgroup 总峰值范围 |
| --- | ---: | ---: | ---: |
| identity，已知长度 | 1.98／99.0MiB | 1.94／97.1MiB | 113.9～114.4MiB |
| chunked，未知长度 | 1.98／99.3MiB | 1.90／95.0MiB | 117.6～118.8MiB |
| zstd 入口 | 2.35／117.3MiB | 2.34／117.2MiB | 132.8～133.2MiB |

持续与较小入口场景必须单列，不能用上述单次结果覆盖；补充容器记录见 [http-container-v8-extra.jsonl](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/http-container-v8-extra.jsonl)：

| 场景 | 环境 | 原生 | 显式指令 | 当前结论 |
| --- | --- | ---: | ---: | --- |
| `forward`，2×16.8MiB×4 | 容器，GOGC=100 | 2.79／93.8MiB | 2.77／93.1MiB | 未达标；cgroup 总峰值分别为 116.4／110.7MiB |
| `read_forward`，2×16.8MiB×4 | 容器，GOGC=100 | 3.26／109.5MiB | 3.28／110.3MiB | 未达标；cgroup 总峰值分别为 135.3／137.5MiB |
| identity 入口，16.8MiB 单次 | 容器，GOGC=100 | 2.01／33.8MiB | 2.07／34.8MiB | 该样本达标；cgroup 总峰值分别为 48.9／50.0MiB |
| zstd 入口，16.8MiB 单次 | 容器，GOGC=100 | 3.09／51.9MiB | 3.19／53.7MiB | 未达标；cgroup 总峰值分别为 66.4／68.6MiB |

小请求 `forward` 的原生／显式指令请求峰值：0.3MiB 约 3.6／3.6MiB，1MiB 约 5.4／5.3MiB，4MiB 约 11.3／11.6MiB。固定开销仍明显，不能给所有尺寸统一承诺 2.5 倍。已归档的 33 个 HTTP 容器均退出码 0、`OOMKilled=false`；窗口后回收的堆在默认 GOGC=100 下为 9.2～9.8MiB，GC25 实验为 8.6～8.7MiB。这只证明这些受测场景，不代表完整服务或更高并发不会 OOM。

最终代码另复核两场 HTTP：显式指令 50MiB 单次 `forward` 为 **1.96 倍／98.1MiB**，cgroup 峰值 114.0MiB；原生 `read_forward`、2×16.8MiB×4 为 **3.26 倍／109.6MiB**，cgroup 峰值 135.8MiB，后者仍未达标。原始数据见 [container-final-results.jsonl](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/container-final-results.jsonl)。前述 v8 矩阵保留原测量标签，不能全部冒充最终代码重测。

## 4. GC 调参实验与 WS 边界

容器与本机分别对 `read_forward`、2×16.8MiB×4 做了 GC 对照。容器原始数据见上文 `v8-extra`；本机数据见 [local-results.jsonl](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/local-results.jsonl)，按 `measurement_label` 定位 `v8-continuous-*`；该文件也保留较早迭代记录，不混入当前容器统计。

| 环境 | GOGC | 正文 | 倍率／请求峰值 | GC 次数／累计暂停 | CPU／总耗时 |
| --- | ---: | --- | ---: | ---: | ---: |
| 容器 | 100 | 原生 | 3.26／109.5MiB | 6／3.401ms | 11160／5842ms |
| 容器 | 100 | 显式指令 | 3.28／110.3MiB | 8／1.515ms | 11750／6375ms |
| 容器 | 25 | 原生 | 2.42／81.2MiB | 22／3.639ms | 11450／6083ms |
| 容器 | 25 | 显式指令 | 2.42／81.5MiB | 23／2.958ms | 11780／6163ms |
| 本机 | 100 | 原生 | 3.39／113.9MiB | 7／0.306ms | 4835／2496ms |
| 本机 | 50 | 原生 | 2.70／90.6MiB | 12／0.638ms | 5001／2570ms |
| 本机 | 25 | 原生 | 2.42／81.2MiB | 21／4.587ms | 4972／2572ms |
| 本机 | 25 | 显式指令 | 2.42／81.5MiB | 22／0.907ms | 4859／2479ms |

GC25 的原生／显式指令容器总峰值分别为 110.0／98.8MiB。`GOGC=25` 只是受控实验，未改线上设置；GC 次数明显增加，单轮结果不能证明稳定吞吐、尾延迟或所有路径达标，也不能替换默认参数下的未达标记录。

| WS 测量 | 环境与范围 | 已取得结果 | 边界 |
| --- | --- | --- | --- |
| 单轮 bridge | 本机，帧已在内存，原生／显式指令各 16.8／50MiB | 原生 2.11／2.00；显式指令 2.19／2.01 | 只测单轮转换与 HTTP 转发，不含首次读帧、完整会话和跨轮保留 |
| 完整冷会话 v9 中间记录 | 本机，原生 16.8MiB，1 路×2 轮，实际走 HTTP bridge | 6.34 倍，请求峰值 106.5MiB，累计分配 261.2MiB | 未达标；该次 `budget_enforced=false`，不能当作准入验证或最终 WS 结果 |
| 最终本机会话 | 本机，原生 16.8MiB，1 路×2 轮，外部预制同一组帧，实际走 HTTP bridge | **4.66 倍／78.3MiB**，累计分配 176.1MiB | 比 v9 降低，但仍未达标；`budget_enforced=false` |

本机原始记录：[v9 中间版本](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/ws-session-v9-intermediate.log)、[最终版本](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-ws-session-native16.log)。两份记录的两帧 SHA-256 相同。大历史触发现有限额并关闭对应重放能力，不能据此推断重放路径也已通过。

最终 ARM64 容器的五场真实 WS 会话均为冷会话、两轮、GOGC=100、外部预制帧，保持生产 15MiB 自动 bridge 阈值。原始 JSON 与另两场 HTTP 合并在上文最终结果文件；逐场日志归档于 `REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/container-final-logs/`。

| 正文与并发 | 实际传输 | 请求峰值 | 倍率 | 累计分配 | cgroup 总峰值 |
| --- | --- | ---: | ---: | ---: | ---: |
| 原生形态 4MiB，1 路 | 原生 WebSocket | **76.4MiB** | **19.06** | 442.5MiB | 110.7MiB |
| 原生形态 16.8MiB，1 路 | HTTP bridge | 76.0MiB | 4.52 | 180.0MiB | 107.6MiB |
| 显式指令 16.8MiB，1 路 | HTTP bridge | 71.4MiB | 4.25 | 180.6MiB | 93.0MiB |
| 原生形态 50MiB，1 路 | HTTP bridge | 191.1MiB | 3.82 | 515.6MiB | 243.7MiB |
| 原生形态 16.8MiB，2 路 | HTTP bridge | 130.2MiB | 3.87 | 349.6MiB | 157.4MiB |

**小正文原生 WS 的固定与历史处理开销仍高**：4MiB 样本虽不套用统一倍数目标，76.4MiB 请求峰值与 442.5MiB 累计分配仍须如实保留。其余大正文完整 WS 会话均超过 2.5 倍。五场均 `budget_enforced=false`，且不含完整 handler；50MiB 等受测样本不代表线上单请求上限已放开。最终七场均退出码 0、无 OOM，最大 cgroup 峰值为 243.7MiB；**无 OOM 不代表整体内存目标完成。**

## 5. 压缩保真与测试证据

最终 512KiB 窗口用独立 Zstandard CLI／libzstd 1.5.7 解码固定原生／显式指令的 16.8／50MiB 四帧，全部满足：恰好单帧、准确 FCS、DictID=0、512KiB 窗口、内容校验和通过，解码 SHA-256 与原文一致。zstd 内容校验和为 XXH64 低 32 位，不是 CRC。

| 样本 | 512KiB 窗口压缩字节 | 相比 2MiB 窗口 | 相比 8MiB 窗口 |
| --- | ---: | ---: | ---: |
| 原生 16.8MiB | 11,272,381 | +0.16663% | +0.25851% |
| 原生 50MiB | 33,549,014 | +0.18736% | +0.22100% |
| 显式指令 16.8MiB | 11,272,429 | +0.16656% | +0.25843% |
| 显式指令 50MiB | 33,549,070 | +0.18739% | +0.22103% |

数据及源码摘要见 [zstd-final512-independent.json](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/zstd-final512-independent.json)，取证说明见 [zstd-final512-report.md](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/zstd-final512-report.md)。这是相同固定 JSON 的压缩阶段对照；完整转发还会定型字段，因此不能要求表中压缩长度与 HTTP 转发记录相同。

已取得的定向日志包括：[编码器弱引用缓存功能](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/compiler-weak-cache-tests.log)、[并发 race](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/compiler-weak-cache-race.log)、[分段 JSON token 保真及回退](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/json-token-segments-tests.log)、[正文读取](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/httputil-tests.log)、[服务 JSON 路径](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/service-json-tests.log)。`handler` 与 `httputil` 曾在此前版本全包通过，分别耗时 40.180／0.946 秒，见 [回归日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-handler-httputil.log)；随后分配前规范化修改又通过 [`httputil` 全包](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-normalization-httputil.log)及 [handler 最终定向 race](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/http-normalized-body-before-allocation-final-race.log)，不把旧全包结果冒充所有后续修改均已复测。

准入与会话功能已有以下验证，具体命令和记录性质见 [ws-admission-checks.md](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/ws-admission-checks.md)，3／10 路 HTTP 定向 race 见 [原始日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/http-normalized-body-admission-race.log)：

| 已验证项目 | 结果与适用范围 |
| --- | --- |
| 18MiB、3／10 路 HTTP 请求与取消释放 | 按 256MiB 预算、6.11 倍放大、25MiB 固定量、37MiB 单请求上限，18MiB 权重为 134.98MiB，同时放行 1 路，其余 2／9 路返回 503＋`Retry-After: 2` 且不读取正文。取消后预算归零，后续请求可再次读取完整正文；race 通过。这里验证准入限制，不表示 3／10 路全部放行时的峰值。 |
| 规范化分配前补预留 | 先计算转义后的确切长度，在最终输出分配前补足额度；不足时不生成输出副本。真实 `Responses` handler 覆盖竞争时 503 与原预留释放、BOM 空正文错误语义、压缩／未知长度预留不降低，最终 race 通过。 |
| WS 重放上界 | HTML 字符、Unicode 分隔符、已转义内容、缺少 `input`、空白、多元素及空数组均核对序列化长度上界；扫描不分配副本，超预算提前拒绝。验证记录见 [ws-replay-budget-race.log](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/ws-replay-budget-race.log)。 |
| WS 共享首帧与多轮兼容 | 四类既有会话回归通过 race，并核对服务处理前后原始首帧 SHA-256 不变；原生 WS 0.15MiB、2 连接×2 轮 race 冒烟及外部样本准备／复用检查通过。此处不代表完整大正文 WS 已达内存目标。 |
| WS 关闭、取消及基础共享预算 | 本次 service 全包已执行稳定关闭码与取消读帧测试：单帧超限 1009、预算不足 1013、取消后部分帧额度清零。handler 全包也覆盖 `TestRequestMemoryReservationResizeSharesHTTPBudgetAndReleasesOnce`，验证共享 HTTP 预算的预留调整及只释放一次；这与尚未执行的完整 handler 混合大流量压力测试范围不同。 |

最终影响范围 `go vet` 与后端 `go build ./cmd/server` 均退出码 0，命令和结果见 [final-verification.json](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-verification.json)、[vet 日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-vet.log)、[构建日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-build.log)。最终回归按执行目录与先后状态区分如下：

| 验证 | 最终记录 |
| --- | --- |
| 隔离树 `officialegress` 及全部子包 | 全包通过；主包 8.462 秒，见 [原始日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-officialegress-verified.log)。 |
| 隔离树 `service` 首次全包 | 229.522 秒，仅报告 16 项冻结检查失败，未报告其他功能失败。该次在 successor 生成前启动，缓存旧冻结证据；[归档摘要](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-service-tests.summary.json)明确保留失败状态、原始日志位置及 SHA-256。 |
| 定稿后隔离树复测上述 16 项 | 16 项全部通过，5.834 秒；[测试名单及正则](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/service-freeze-rerun.json)、[原始日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-service-freeze-worktree-rerun.log)。没有把定向复测表述成重新运行了整个 service 包。 |
| 主仓库相同 16 项复测 | 11 项通过，5 项仍失败，全部指向既有 `docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md` 摘要，见 [原始日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-service-freeze-rerun.log)。**主仓库未全绿。** |
| 主仓库额外 `officialegress` 冻结检查 | 运行超过四分钟后中止，已在验证 JSON 登记；[原始输出](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/main-merged-freeze-tests.log)不作为通过证据。 |
| 候选事实与 trace | 六项 Go 候选测试全部通过，4.873 秒；13 项 Python trace 单测通过，见 [Go 日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/candidate-go-final-tests.log)与 [Python 日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/candidate-trace-final-tests.log)。 |

本次按既有流程生成 [request-memory successor](egress/maintenance/upstream-request-memory-20261006-freeze-successor.json)，共 24 条承接、无删除；当前 fact map 只重绑三份源码的七处摘要，保留 15 项测试、32 项事实与规则语义。生成器原始 `manual_actions_required` 状态保留，三处显式 fact map 重绑已完成，历史收据未改写；说明与生成记录见 [准入及冻结检查记录](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/ws-admission-checks.md)、[生成日志](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/freeze-successor-generate.log)。本轮 55 个 Go 文件的最终摘要见 [final-source-sha256.json](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final-source-sha256.json)。

主仓库既有框架文档当前 SHA-256 为 `3af48edd980c974e3c3956b57b0df58f509b410e1c065d8955876e9f3295492e`；主任务已核对回写前后相同，本次未修改该文档。已有 guide-part4 历史承接没有覆盖这个当前摘要，本次也没有为无关文档变更新增承接。完整 service 原始日志保留在 `/tmp/sub2api-memory-optimization-20261006/final-service-tests.log`（3,526,589 字节）；仓库中的摘要不是原始日志。

## 6. 保留问题与容量建议

1. 整体目标未完成：HTTP 持续请求、16.8MiB zstd 入口和完整大正文 WS 会话均仍有超过 2.5 倍的记录；4MiB 原生 WS 的 76.4MiB 请求峰值也不能忽略。
2. 本轮已完成列明的内存采样、准入／取消与兼容回归；未执行真实线上混合负载和长时间压力验收，不能由这些样本推断所有并发、请求形态和上游响应时长的容量。
3. 后续若尝试更主动的正文释放、可重放输出文件或分段复用，需要保持重试、Guard、异步读取／关闭安全；落盘收益还需核对 cgroup 页缓存。主仓库五项既有文档冻结差异应独立处理，不与本次内存变更混同。
4. **本次不放宽线上容量。** 后续先消除上述未达标路径，再按实测重新拟合“固定预留＋正文大小×倍数”并保留 25% 余量；单次达标、GC25 实验或容器无 OOM 均不足以直接下调系数或提高请求上限。

归档材料不含大正文或二进制。容器原始 JSON 的 `command` 保存实际命令，`body_sha256` 保存样本摘要，`fixture_path` 为当时挂载路径；需要原样本才能逐字节复测。本文及资产用于追踪本次验证过程，不是发布收据。

## 7. 简短复现入口

测量默认跳过。先在验收容器外用 `SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_FIXTURE=1` 运行 `TestOfficialEgressMemoryPrepareFixture`；WS 再用 `SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_WS_SESSION_FIXTURE=1` 运行 `TestOfficialEgressWSMemoryPrepareFixture`，保存包含帧长度和 SHA-256 的 `manifest.json`。测量时设置 `SUB2API_OFFICIAL_EGRESS_MEMORY_PROFILE=1`，WS 通过 `SUB2API_OFFICIAL_EGRESS_MEMORY_WS_SESSION_FIXTURE_DIR` 复用预制目录，避免样本构造污染容器峰值。

逐步命令见 [复现说明](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/reproduce.md)。[容器 runner](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/run_container_matrix.py)与 [HTTP 矩阵](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/container-cases.json)、[入口／GC 补充矩阵](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/container-extra-cases.json)、[WS 矩阵](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/container-ws-cases.json)、[最终七场矩阵](REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/container-final-cases.json)已归档；历史 WS manifest 保存在同目录 `ws-fixture-manifests/`，未附正文。每次重测使用新标签和新容器，重新生成的随机样本必须核对摘要，不能伪称与本报告逐字节相同。
