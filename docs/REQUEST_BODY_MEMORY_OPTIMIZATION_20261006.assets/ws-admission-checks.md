# WS 与请求准入定向验证

工作树：`/Users/czs/.codex/worktrees/request-memory-optimization/sub2apiplus`。

以下两次测试的结果来自工具原始回执；当时未重定向文件，不将本记录冒充原始测试日志。

1. 18 MiB、3/10 路 HTTP 真实准入与取消释放：

   ```sh
   cd /Users/czs/.codex/worktrees/request-memory-optimization/sub2apiplus/backend
   go test -race ./internal/handler -run '^TestResponsesRequestMemoryAdmission18MiBConcurrentCancellation$' -count=1
   ```

   结果：通过，`ok github.com/Wei-Shaw/sub2api/internal/handler 3.886s`。
   当前部署参数为 256 MiB 预算、6.11 倍放大、25 MiB 固定量、37 MiB 单请求上限。
   18 MiB 正文权重为 134.98 MiB，因此同时只放行 1 路，其余 2/9 路返回 503 与 Retry-After: 2。
   被拒请求零读取正文；取消后预算归零，随后同一准入器再次成功读取完整 18 MiB 正文。

2. 去掉 handler 的首包与审计副本后，四类多轮回归：

   ```sh
   go test -race ./internal/service -run '^TestOpenAIGatewayService_ProxyResponsesWebSocketFromClient_(KeepLeaseAcrossTurns|HTTPBridgeModeRelaysHTTPStream|StoreDisabledPrevResponseStrictDropToFullCreate|PassthroughModeRelaysByCaddyAdapter)$' -count=1
   ```

   结果：通过，`ok github.com/Wei-Shaw/sub2api/internal/service 3.017s`。
   测试辅助入口新增原始首帧 SHA256 不变检查，验证服务规范化/重试/跨轮处理没有原地改写 handler 共享正文。

3. 新增真实 WS 读帧到官方服务会话测量：

   - `ws-session-smoke.log`：0.15 MiB、1 连接、2 轮 native WS 小冒烟。
   - `ws-session-concurrent-race-smoke.log`：0.15 MiB、2 连接、2 轮，并启用 race。
   - `ws-fixture-prepare-smoke.log`：独立生成会话帧，准备阶段不进入容器测量。
   - `ws-fixture-reuse-smoke.log`：流式加载既有会话帧；刻意把 BODY_MIB 改成 0.01，仍复用已生成的两帧 159485/159589 字节，并逐帧核对原 SHA256。通过。

   外部准备与复用入口：

   ```sh
   SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_WS_SESSION_FIXTURE=1 \
   SUB2API_OFFICIAL_EGRESS_MEMORY_BODY_MIB=16.8 \
   SUB2API_OFFICIAL_EGRESS_MEMORY_REQUESTS_PER_SLOT=2 \
   SUB2API_OFFICIAL_EGRESS_MEMORY_WS_SESSION_FIXTURE_DIR=/绝对路径/新样本目录 \
   go test ./internal/service -run '^TestOfficialEgressWSMemoryPrepareFixture$' -count=1 -v

   SUB2API_OFFICIAL_EGRESS_MEMORY_PROFILE=1 \
   SUB2API_OFFICIAL_EGRESS_MEMORY_REQUESTS_PER_SLOT=2 \
   SUB2API_OFFICIAL_EGRESS_MEMORY_WS_SESSION_FIXTURE_DIR=/绝对路径/已生成样本目录 \
   go test ./internal/service -run '^TestOfficialEgressWSSessionReadMemoryProfile$' -count=1 -v
   ```

   原生 2 连接×2 轮 race 小冒烟通过，用时 3.522 秒；fixture 准备与加载复用冒烟均通过。

   测量默认跳过；它包含读帧、转换、服务会话、首包保活与本地 WS 传输，不包含完整鉴权/调度 handler。
   本地 WS 上游使用 Reader 与 io.Discard，不保存正文；客户端从文件流发送。
   默认按 15 MiB 阈值自动 bridge，单帧上限保持 64 MiB，不更改线上配置。
   容器测量必须复用容器外准备的 WS_SESSION_FIXTURE_DIR，否则生命周期 memory.peak 会被样本构造污染。

所有大正文峰值测量由主代理统一串行执行。

4. 复核后补充 HTTP 规范化正文上调预留：

   ```sh
   go test -race ./internal/handler -run '^TestResponsesRequestMemory(RejectsNormalizedBodyGrowthAndReleases|GrowsOriginalReservationWithoutReducingMaxPreallocation|Admission18MiBConcurrentCancellation)$' -count=1 -v
   ```

   通过，4.587 秒；原始日志为 `http-normalized-body-admission-race.log`。
   包含真正进入 Responses handler 的 lenient 扩展后与另一请求竞争、503/Retry-After、原预留释放，以及压缩/未知长度不降额。

## 最终准入边界修复

- WS replay 估算改为无分配扫描上界，计入 RawMessage 的 HTML 与 U+2028/U+2029 转义扩展、缺少 input 时新增字段的固定字节；不改变重放序列化语义。
- maxBytes 的既有语义保持为解压后的入站单帧上限。replay 缓存另受 16 MiB / 4096 项限制，重建正文按共享预算准入，不另套入站单帧上限。
- HTTP 使用 ReadAdmittedLenientJSONRequestBodyWithReservation，规范化计长后、分配前补足同一预留；准入回调写入响应时返回 sentinel，读取入口直接退出，保留原始 413/503。
- 定向 race：TestOpenAIWS(ReplayWorkingBytes|RequestMemory) 全部通过，3.009 秒；日志 ws-replay-budget-race.log。
- 定向 race：HTTP 存储失败映射、规范化补额拒绝/释放、分配前成功补额、压缩/未知长度不降额，以及 18 MiB 3/10 路取消压力全部通过，4.473 秒；日志 http-normalized-body-before-allocation-race.log。

- BOM 空正文边界：规范化长度为 0 时沿用原预留，后续返回 400 空正文；补额相关定向 race 再测通过，2.862 秒，日志 http-normalized-body-before-allocation-final-race.log。

## 最终源码冻结承接

- 扫描当前 fact map 全部源码引用后，确认只需重绑 compiler.go、official_egress_openai_http.go、openai_gateway_forward.go 三份源码的七处引用；15 项测试、32 项事实及规则语义不变。新映射 SHA256：ba9f684ea7b4d9d2ade2acd4e17b139488c8b7e1a2ca7b7c4a1051cb5ae2b615。
- 受影响六项 Go 候选测试全部通过（本次没有画像 skip），4.873 秒，candidate-go-final-tests.log；Python trace 单测 13 项通过，candidate-trace-final-tests.log。
- 生成 docs/egress/maintenance/upstream-request-memory-20261006-freeze-successor.json，工作树模式，24 条 transition，无删除。三项 manual_actions_required 是静态命中的显式重绑要求；映射已处理，不改生成器结果、不修改历史收据。
- 生成器提示未在仓库内写 timing-ledger；successor 正常生成，未产生仓库 timing 日志。后续最终门禁由主代理统一执行。
