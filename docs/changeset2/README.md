# 变更集 2：ReleaseBundle、compiler 与真实回滚

状态：`accepted`

任务状态：

- 2.0：`completed_baseline_frozen`
- 2.1：`exchange_evidence_deferred`
- 2.2—2.7：`completed`
- 2.8：`accepted`

## 实施结果

1. 正式 `ReleaseCatalog`、`ResolvedCodexRelease`、`ReleaseBundle`、
   `EndpointBindingCatalog`、中立 compiler 和不可拆分 `CompiledExecution` 已下沉到
   `internal/officialegress`。
2. 一次账号尝试或独立调用只解析一个 Bundle；retry、WS 重拨、WS → HTTP fallback
   和同调用辅助端点只读取冻结 Bundle，`Executor.Prepare` 不再持有 Provider。
3. `POST /oauth/token` 的物理 route 与业务 endpoint 已分层。refresh 通过权威
   SinkID/purpose/route 解析并继续使用 req-profile；exchange 仍为
   `transport_only`，不能获得 EndpointID 或 ReleaseSelection。
4. 动态上传 URL 只在 compiler 收到 `EndpointDynamicInputs` 后验证；最终 target、
   ConnectionIdentity 与 PoolDigest 冻结在同一个 `CompiledExecution` 中。
5. 旧 receipt 原文和 `binding_digest` 保持不变；canary/enforced sink 仍只走
   Executor/FinalizationToken，Legacy Dispatcher 只接受 `legacy_observe`。
6. 临时 service adapter、ad-hoc active fallback、service active registry/profile
   查询桥、active wrapper、包级 active 字段序及 repository 反向 active builder 的
   生产引用已经归零。
7. scanner 通过精简 `legacy_delegated` 凭证记录旧业务 terminal 到中心 Dispatcher
   的改线，不伪造 MigrationReceipt，也不扩大 legacy baseline/ceiling。
8. Legacy 编译能力不再暴露请求、`TransportSpec` 或摘要 getter；受信资源只能原子消费
   capability 并立即发送。`ResolvedCodexRelease.Node()` 对 Build/Wire 内的 slice 做深拷贝，
   调用方不能污染冻结发布事实。
9. 当前源码门禁直接扫描辅助 HTTP、WS、OAuth 与 legacy 发送链，禁止重新引入 active
   同义包装或拆解 capability；隔离合成 Catalog 的回滚矩阵验证 HTTP、WS、辅助端点和
   OAuth refresh 只消费同一 mode，且画像、TLS、压缩、字段序、生命周期与池摘要不混搭。
10. 主 Responses 的 remote compaction 分派从进程运行时取得冻结 mode，出站 metadata
    从同一调用的 `OfficialEgressContext` 取得 mode；feature 查询不再默认 active。三个
    生产文件均已纳入当前源码门禁，并由 active/previous feature 相反的聚焦测试锁定。

## OAuth 证据状态

Vircs 的官方 `codex-cli 0.145.0` 脱敏应用层 wire fixture、二进制/环境信息、
SHA-256 和 secret-scan receipt 已落入 `oauth-evidence/`，足以复算 refresh 的 Header、
JSON Body 和字段顺序。

证据尚未覆盖 `alpn-capability` 与完整连接生命周期矩阵，因此 exchange 不在本变更集
升级画像；它继续保持 `transport_only`。该安全降级不阻塞 CS2 主体完成。

## 可复算产物

- `compatibility-lock.json`：变更前 receipt、binding、发布图和旧快照基线。
- `active-source-inventory.json`：五类旧 active 事实源的变更前生产引用清单。
- `scanner-candidates.json`：变更前业务发送候选身份。
- `bootstrap-inventory-lock.json`、`removal-receipts.json`：扫描器改线锁与最小凭证。
- `oauth-evidence/`：OAuth 脱敏 wire 证据及摘要链。
- `verification.json`：最终门禁、冻结边界和关键产物摘要。

`backend/internal/officialegress/changeset2_baseline_test.go` 持续验证 receipt 原文、旧版
五元组审计投影及旧快照不可变；`changeset2_acceptance_test.go` 验证单次解析、真实
active/previous、fallback Token 清除、动态 target、Dispatcher 授权边界和 capability
不可拆分；`changeset2_current_source_gate_test.go` 与
`changeset2_synthetic_rollback_test.go` 分别验证当前生产源码和隔离回滚矩阵。
