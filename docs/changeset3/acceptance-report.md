# 变更集 3 开发自审报告

状态：`ready_for_audit`

自审日期：2026-08-03（Asia/Shanghai）

本报告记录正式审核提出的 3 个 P1 阻塞及补充强制约束的修复结果。当前仅表示开发与自审完成、可以重新审核，不表示已经通过正式验收。

## 最终运行边界

- `codex_profile` Runtime Sink：21 个 `enforced`、28 条 route，0 个 `legacy_observe`。
- 本变更集新增迁移：17 个真实 Runtime Sink、24 条 route，均具有独立的 `canary_enforce → enforced` 收据链。
- 不可回归锚点：变更集 1B/1C 已验收的 4 个 enforced sink、4 条 route；旧收据及 fixture 逐字节不变，新增 CS3 非回归证明。
- scope exclusion：`codex.admin_test.chat_completions`、`codex.admin_test.keeper`。二者是 API Key 专用历史入口，不属于 Codex OAuth Runtime 迁移面。
- transport-only 例外：`codex.oauth.exchange` 保持 `legacy_observe`，无 EndpointID、ReleaseSelection、MigrationReceipt 或 FinalizationToken。
- facade：3 个真实物理资源继续作为 transport infrastructure，未签发虚假 RemovalReceipt。

## P1 修复结论

1. Executor 已成为严格 Codex 身份与 wire 定型权威。`CodexIdentityFacts` 只包含不可变身份投影；Bearer、Agent Identity、attestation、cookie、refresh token 均为 attempt-local 单次认证能力。Plan 不包含 `Account`、service/repository 类型、可变 map 或可跨重试认证 Header。
2. `CodexOAuthStrict` 对普通 Header 与 override 中的保护头执行闭集拒绝；compiler 按 ProfileSpec 和结构化事实生成身份头、Body 线序、画像字段、压缩与长度。Token 签名投影绑定 IdentityMode、身份/Header/Body policy digest 及 attempt ordinal/reason；其他 IdentityMode fail-close。
3. 21 个 Runtime Sink 的生产入口均为 semantic request/frame → Executor。Forward、passthrough、alpha-search、WHAM 与 4 个既有锚点不再调用旧 attach/header/body finalizer；AST 门禁覆盖全部 21 个 Sink。825 行 pairing 门禁继续保留，当前生产调用计数为 legacy attach `0/0`、body finalizer `0`、仅冻结测试兼容 helper 的 header finalizer `1`。
4. WebSocket 握手和出站帧均进入 Executor 权威边界。正式路径只能使用 `PrepareFrame → WritePreparedFrame` 单次能力，不能取得任意 `WriteMessage([]byte)` 或底层连接；帧绑定 Bundle/Endpoint、Invocation、ordinal、event type、shape、session/turn identity 与一次消费状态。
5. 原始冻结矩阵 `19/23` 保持不变。不可变 acceptance amendment 绑定三份原始资产摘要以及两个 scope exclusion 各自的 AST/source blob 证据，并推导正式有效目标 `17/21`；缺失或漂移均 fail-close。

## Final-wire 与收据证据

- 身份权威重构前参考：21 Sink、28 route、`active/previous` 共 56 份 route-mode capture；只证明该双定型参考中已记录的 adapter-terminal 字段，不声称完整历史生产业务链等价。
- 重构后 final-wire：同样 56 份 capture，均从真实生产 semantic 构造/转换函数进入 production invocation 或 Forward plan，再通过正式 compiler、Executor、三类 adapter 与本地 terminal Guard；外部流量为零，凭据与 Body 全部为合成、脱敏材料。
- HTTP/req-profile 证据冻结最终 Header 线序、身份摘要、Body 字段顺序/shape/digest、normalization、TLS/Profile/Pool/Connection 与压缩事实；动态上传同时证明最终 host/path、目标校验和 raw stream 单次消费。
- WebSocket 证据冻结 coder 自动握手形态、compression/context takeover、pool/acquire token/ordinal 以及 8 个实际出站事件 capture。唯一正式比较器同时用于 pre→post、mutation、冻结 manifest 和 MigrationReceipt，具有 UA、originator、version、Header/Body 顺序与摘要、compression、TLS/Profile/Pool、WS compression/event shape/frame digest、上传 host/path 等 15 类 mutation 反例。
- 17 组 canary→enforced 收据已由最终证据原子重建；15 个 RemovalReceipt 只更新新 enforced 收据摘要，原 candidate、source blob、replacement SinkID 等冻结字段不变。

冻结摘要：

```text
post final-wire: c824ffb0ab6e2429c09f9ac517cf3e6f96860c7c6ef77c229757fd690bdbcf0f
approved deltas: 6e7e13a94d90431a1c49bf22b72efb746e8ac63b446a3e7176b63bc8c13f1ce0
migration receipts: 0b79f524328a583064bab889b03a3519c27774354fbccec3798fd845b5deadd2
removal receipts: 85d81ea6018c776f9c7c15b23c9868fe447a59e7bed4ec00b582225d90fa7dfb
```

## 验证结果

以下命令均在最终工作区执行并通过：

```text
make check-egress-spec
PASS

cd backend
go test -race ./internal/officialegress/...
PASS

go test -race ./cmd/egressscan -run '^TestChangeset3'
PASS

go test -race ./internal/service -run '<CS3 identity/forward/WS/HTTP invocation/pairing 精确测试族>'
PASS

go test ./internal/service -count=1
PASS

go test ./... -count=1
PASS

git diff --check
PASS
```

`make check-egress-spec` 的关键结论：

```text
bootstrap 回放：基线=180，受审补录=0，受审移除=22
当前发送面：bootstrap=180，受审补录=0，受审移除=22，1A 基础设施=10
§3.5 台账：52 个出站定型文件全部登记，2 个工具自引用排除
Codex 源码引用：127/127 可定位
```

## 审核入口

- `migration-receipts.json`：17 个新增迁移 sink 的收据链。
- `acceptance-amendment.json`：`19/23 → 17/21` 的不可变历史修正链。
- `pre_identity_authority_refactor_reference/`：重构前 56 份参考 capture。
- `post_identity_authority_refactor_final_wire/`：重构后 56 份 final-wire、比较与 secret scan。
- `removal-receipts.json`：真实消失 candidate 的移除证明。
- `facade-audit.json`：三个保留 facade 的物理存在性和 transport infrastructure 分类。
- `pairing-gate-evaluation.json`：pairing 门禁保留依据。
- `bootstrap-inventory-lock.json`：变更集 3 scanner 算法锁。
- `migration-artifacts/changeset3/`：脱敏 wire、执行验证和 canary acceptance 工件。

开发自审结论：变更集 3 已满足当前确认的修复约束，状态为 `ready_for_audit`，可以重新进入正式审核。
