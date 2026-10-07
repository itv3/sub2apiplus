# Claude Code 客户端仿真历史审计

> **用途**：承接 [Claude 指南](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md)原 §3.4、§3.6 与 §3.6.1 的
> FW-A～FW-H 迁移事实、2.1.226 Store 事实和 2026-08-22 续作记录；迁移来源为文档提交 `b714bb1f0`。
> 本文只供审计、历史重放和回归夹具引用，不是当前升级操作入口。
>
> **效力边界**：失效的是旧机器坐标、旧容器／Compose／账号运行态的续跑资格；2.1.226 官方证据、Store
> 对象、事实与收据仍按形成时的身份只读有效。不能据此判定当前生产激活，也不能直接恢复旧在线矩阵。
> 新任务从当前指南 VC-0 冻结输入，当前工具与环境边界见其 §3.6、第四部分和第六部分。
>
> 下文保留原记录中的“当前”“本任务”、DMIT／Vircs 与原节号，它们仅指当时的历史上下文；
> 原 §4.6.4 finalizer 交接已由当前指南 §4.6.2 的受监督交付入口取代，不能照抄历史命令执行。
> 历史收据原字节与证据引用保持不变，旧授权不延伸到新的运行对象。

---

## 1 历史实现与 FW-A～FW-H 迁移

FW-G 的历史 Sonnet Candidate 与三模型后继均已完成各自范围的隔离验收。当前后继变更收窄入口
准入和本轮交付边界；历史 FW-H 收据保持不可变，但不再作为当前目标状态：

| 层 | 当前事实 |
|---|---|
| 画像与发布图 | 2.1.226 Profile、Wire、Snapshot、ReleaseArtifact／Bundle 均已内容寻址并登记到 Claude Release Catalog；2.1.220 只保留同一 Schema／Compiler 下的 baseline fixture |
| 统一执行链 | Claude production active 已接入共享 Persona Release Selector；PersonaPlanner、IdentityFacts、Compiler、authority、issuer 与状态仍属 Claude 方言，Codex facade 和 final wire 保持零差异 |
| strict 入口 | 正向只有 `official-messages-oauth` 与 `official-count-tokens-oauth`；共享物理路由先执行 OfficialIngressCatalog 门禁。第三方 Messages／count_tokens、Chat Completions 的 bare／v1 别名、Responses 的 bare／v1 HTTP／subpath／WS 全部是凭据前拒绝负例，不进入 RequiredRules 正向分母 |
| strict／managed 出站 | 八类 `persona_strict` 进入 Compiler／Executor／Guard；`non_persona_managed` 进入独立策略，未知 OAuth 出站保持 `denied` |
| 语义与兼容 | 只对 Catalog 已登记官方来源生成规范化语义；Persona system／identity 由批准事实派生。第三方兼容和工具桥不属于当前实现范围 |
| 候选验收 | 历史三模型验收继续证明 2.1.226 出站画像；当前 official-client-only 后继必须在 DMIT 重跑三类官方客户端正例、全部第三方／未知形态负例、回退／恢复和 Codex 隔离 |
| 交付边界 | DMIT 只承担候选验收，最高状态为 `ready_for_operator_release`；Vircs 为用户管理的生产机，状态固定为 `operator_managed／unverified／not_touched`，本任务不得签发 Vircs DeploymentFact |

| 阶段 | 变更 | 完成判据 |
|---|---|---|
| `FW-A` | 只读冻结 Codex、Claude 2.1.220 证据、生产运行态和遗留发送面基线；不新增 Claude 代码或绑定 | 两类基线可复算；遗留输出只标 diagnostic-only；没有生产写入 |
| `FW-B` | 按 Framework 抽取 Codex 已证明的暂定共享合同并保留 Codex facade | 共享内核无 Codex 专用策略字段；Codex active／rollback final wire 零差异；不宣称多 Persona 已冻结 |
| `FW-C` | 验证并发布 Codex-only 正式制品，完成回滚、恢复和稳定观察 | 本轮没有新增 Claude Persona／画像／strict 注册；Codex 发布与激活收据闭环 |
| `FW-D` | 建设 Campaign、正交事实、两段式批准、Snapshot／Release Store、candidate／PAIR、晋升与激活工具链 | 只用 Codex／合成数据自测；越权、摘要变化、范围缺口和收据不匹配均由机器阻断 |
| `FW-E` | 第一步冻结最新 stable；从目标 bundle 原生发现发送点，加载已冻结的 2.1.88／HitCC 候选台账和 2.1.220／前一批准 stable，与目标发现组成并集，分开建立 DiscoveryInventory、SemanticRuleCandidate 和 AtomicAssertionLedger，完成差分、P／R／J／M 和 Evidence 封存，再建立两个 Inventory 与 observation-only Sink | 目标版本、完整 sink／discovery inventory、语义候选、只含 SPEC 的原子断言台账和 EvidencePackage 可复算；没有截断或未分类项；停在 `evidence_recorded`，尚不定义目标 Schema／Snapshot 或签发 Evidence 批准 |
| `FW-F` | 先把 FW-E 全部发现和语义候选逐项收敛到可审计终态并清零未决项，再由最新 stable 证据生成 Schema、目标 Snapshot、Persona 和不可部署样例；随后用同一 Schema／Compiler 表达 2.1.220 rollback fixture，批准 Profile、范围和多 Persona 合同 | 全量 DiscoveryDispositionLedger 无缺失、重复或未决项；target-first 样例与跨 Persona 负例通过；ApprovalFact 完整；Codex 生产收据对应最终合同；selector 未改变 |
| `FW-G` | 实现本次已批准的 40 条最新 stable RequiredRules，完成受管语义层、辅助出站三态、全部 strict 入口原子断言、独立复测、DMIT candidate 和 rollback 验收 | 40 条规则、106 条画像原子断言和 4 条客户端本地场景断言通过；三模型正例、范围外拒绝和回退闭环；签发 AcceptanceFact |
| `FW-H` | 构建固定正式候选镜像，仅在 DMIT 完成 official-client-only 正负矩阵、回退／恢复和稳定观察后交付 | DMIT 签发 `ready_for_operator_release`；Vircs 不连接、不部署、不验证，生产替换由用户自行执行 |

历史 Sonnet FW-G 完成事实以 [FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)为准；
[三模型验收尝试收据](egress/maintenance/claude-fw-g-three-model-acceptance-attempt.json)只保留首次账号权益
阻断的历史事实，当前结论以
[三模型 FW-G 验收收据](egress/maintenance/claude-fw-g-three-model-acceptance.json)为准。FW-G 已完成，
历史 FW-H、request-id、Catalog DeploymentFact 和生产验收收据保持不可变，只说明各自签发时的运行
事实，不证明当前 Vircs 或当前 official-client-only 候选已经部署。当前结论必须以后继
OfficialIngressCatalog／Approval、源码 transition 和 DMIT `ready_for_operator_release` 收据共同判定。

最新 stable 是目标 Schema、Snapshot 和实现的唯一设计权威。FW-E 只冻结目标规则证据，不预先用
2.1.220 建画像；FW-F 完成发现项语义清零后，必须先生成目标 stable 画像和样例，随后才把 2.1.220 表达为差分基线、
conformance fixture 或受范围约束的 rollback。遗留 final wire 只用于盘点和诊断，不能决定任何画像
字段或提升证据等级。FW-F 两套 fixture 永久保留，但不得注册到 Codex-only 运行时镜像。

若 FW-G 的目标 stable 机制暴露真正的共享控制合同缺口，当前 candidate 作废，返回 FW-B 建立后继
合同，并重新执行 Codex 零差异、FW-C Codex-only 发布闭环及 FW-F 的 target-first／2.1.220 fixture；
不得在 Claude 方言中绕过或原位扩张接口。`constants.go` 若仍服务 API Key 产品语义，只退休
`claude-code` OAuth Persona 的版本面。

## 2 2.1.226 历史 Store 与准入状态

源码存在不能证明 production active。FW-A～FW-G 已完成：目标 stable 2.1.226 的 FW-E Campaign 为
`claude-code-2_1_226-fw-e-semantic-20260818-e577e144a`，其 Store 停在 `evidence_recorded`。旧 Campaign
`claude-code-2_1_226-fw-e-final-20260818-93f2edbc9` 及其“7,425 条规则”收据只保留为历史错误事实，由
[语义规则纠正收据](egress/maintenance/fw-e-semantic-rule-correction/receipt.json)替代，禁止继续作为
FW-F 输入。FW-F 后继 Campaign 为 `claude-code-2_1_226-fw-f-required-rules-v5-20260819`，Store 已到
`profile_approved`：7,368 个发现、593 个正交候选和 32 个语义候选族未决数均为 0，2.1.226 目标画像／
Release 与 2.1.220 fixture 已按 target-first 顺序生成，EvidenceApprovalFact 和 `validation_only`
ProfileApprovalFact 已签发。40 条 RequiredRules 由 Vircs 上 2.1.226 官方客户端的 106 条画像
P／R／M 原子断言支撑，另有 4 条客户端本地场景断言；110 条均通过。事实见
[清零收据](egress/maintenance/fw-f-discovery-clearance/receipt.json)和
[FW-F RequiredRules 规范化收据](egress/maintenance/fw-f-required-rules-normalization/receipt.json)。

历史 FW-G Campaign、三模型后继和 FW-H／Catalog 收据完整保留，继续证明 2.1.226 出站画像与当时的
运行事实，但不再授予第三方入口或当前生产状态。当前源码追加 official-client-only Approval 和
OfficialIngressCatalog：只有 Claude Code 2.1.226、Claude Desktop 2.1.237、Claude Code for VS Code
2.1.239 的已登记 wire 可进入 Messages／count_tokens 正向链；第三方与未知形态统一在 OAuth 凭据读取前
拒绝。该后继在 DMIT 验收前状态为 `candidate_sealed／not_ready_for_operator_release`；DMIT 通过后最高
状态为 `ready_for_operator_release`。Vircs 始终保持 `operator_managed／unverified／not_touched`。

## 3 FW-H 旧续作检查点（2026-08-22；续跑资格已失效）

本节只记录当前 official-client-only 后继的可恢复执行点，不改写历史 FW-G／FW-H 收据。当前状态为
`candidate_deployed／not_ready_for_operator_release`，尚未签发本轮 `ready_for_operator_release`：

| 项目 | 当前事实 |
|---|---|
| 源码 | 提交 `8a33e1c902f5b5bf911b4625dea6f35b70321183`（`feat: Claude OAuth 仅接受官方客户端`）；提交前 `go test ./... -count=1`、`make check-egress-spec`、`go vet ./...` 与 `git diff --check` 均通过 |
| 构建链 | Mac 原生构建前端并交叉编译 `linux/amd64` 后端，ARM64 只封装 amd64 镜像；后端二进制 SHA-256 为 `5b93521312a1625dd661860b074aae6aaa0e79ccc0bb4e1964a2337bb21b9bc2` |
| DMIT 镜像 | 标签 `sub2apiplus:official-client-only-8a33e1c90`，OCI image／运行摘要均为 `sha256:1de00f4b89a0aa16184186a923856ea13ee88cd504681c0233bd03d77b7f9ad4` |
| DMIT 容器 | 容器 ID `184fce0d30d164e6e39d7949d37abe21c4d05d57aa15910aace8579746c3c35b`，`running／healthy`，重启数 `0`；依赖容器未替换 |
| Compose | `/root/Docker/sub2apiplus/app/docker-compose.claude-official-client-only.yml`，SHA-256 `46ec692020f01b3769f2dc8c34c91ebd27ca88aae2313ff13f564f3f5b2e6279`；旧 override、正式镜像和回退镜像均保留 |
| 在线矩阵 | 脚本 `/root/Docker/sub2apiplus/app/data/deployment-evidence/claude-official-client-only-8a33e1c90-live-matrix.sh`，SHA-256 `6f7a5239ab6eaf1ab58b9fb2ec689f78c8a4efd19081d65461417bf7c6ec933d`；wire 文件 `claude-wire-a7d2c91f.json`，SHA-256 `a7d2c91fc5c4b43bd49f93b60d0d681e487db0e1cdb25d3096e703cb85587c4d` |
| 当前阻断 | 矩阵脚本已补齐 `x-stainless-timeout`；`attempt-002` 已通过 official-client-only 准入并到达 Anthropic，但上游返回 `401`。通过 DMIT 后台对账号 `#100` 执行正常“刷新令牌”又得到 `invalid_grant: Refresh token not found or invalid`，证明 access token 与 refresh token 均需重新授权 |
| 账号边界 | 只处理账号 `#100`；账号 `#101` 保持暂停／不可调度，不得改变。DMIT 后台已生成 `#100` 的重新授权流程，授权 URL、PKCE verifier、授权码和 OAuth Token 不写入本文或仓库 |
| Vircs | 未连接、未部署、未验证、未修改，保持 `operator_managed／unverified／not_touched` |

后台页面的版本角标仍显示历史构建字符串 `v0.1.177-4-fw-h-final-e2c80213a`，不能据此判定当前候选身份；
本轮续作必须以源码提交、OCI digest、容器 ID、Compose 摘要和收据联合判定。最终收据签发前应明确处置
该显示差异：若它影响已冻结 Candidate 身份，返回 VC-4 建立新 Candidate；只有证据证明它不参与身份、
路由或行为判定时，才可在收据中登记为非身份显示字段。VC-5／VC-6 不得为修正角标重新构建候选镜像。

恢复执行时按以下顺序继续，中间不得把账号故障误判为画像通过或失败：

1. 由用户在已生成的 Claude OAuth 页面完成账号 `#100` 登录授权，把授权码通过 DMIT 后台正常流程
   持久化；禁止直接修改数据库，并复核 `#101` 未变化；
2. 运行在线矩阵 `attempt-003`，验证 Claude Code 2.1.226、Claude Desktop 2.1.237、Claude Code for
   VS Code 2.1.239 的 Messages／流式响应／工具往返／标题／count_tokens 正例，以及 Sonnet、Opus、
   Fable 的登记模型能力；
3. 验证 KiloCode、zlfcode、curl、第三方 Messages／count_tokens、Chat Completions、Responses、未知
   版本、Header、metadata、System、工具目录和跨 Persona 请求均在读取 OAuth 凭据前拒绝，并复核
   Codex final wire 隔离；
4. 使用本机真实 Claude Code、Claude Desktop、Claude Code for VS Code 完成正例；测试前只读确认
   客户端版本与 OfficialIngressCatalog 一致；
5. 在 DMIT 切回已冻结回退镜像验证，再恢复上述 OCI digest；随后只执行 VC-6 交接清单预先冻结的交付
   smoke，完成稳定观察、容器／依赖身份、日志、磁盘、网络和 3x-ui 核对，不重跑完整 VC-5 规则矩阵；
6. 清理本轮临时传输包和构建临时目录，但不执行 Docker prune、不删除回退镜像或验收日志；随后追加
   `candidate_delivery_recorded` 四阶段事实，封存交付包与私有归档清单，再用 §4.6.4 的 finalizer 签发并
   独立重放 `ready_for_operator_release` 收据；缺少任一现场事实时保持当前未就绪状态，不得手写收据。


## 4 FW-F 历史画像生成入口

以下承接原指南 §4.3.1 的 2.1.226 聚合入口与限制，只供历史重放；当前 VC-3 使用原 Campaign 的
`prepare-profile`／`stage-profile`，不能运行本入口建立新的正式 Store。

当前 2.1.226 曾使用下列历史聚合入口：

```bash
python3 tools/official_client_capture/claude_fw_f_profile.py \
  --fw-e-store /绝对路径/VC-1-control-store \
  --fw-e-campaign <VC-1-campaign-id> \
  --clearance-dir /绝对路径/VC-2-clearance \
  --clearance-receipt /绝对路径/VC-2-clearance-receipt.json \
  --baseline-rules /绝对路径/baseline-rules.json \
  --rule-manifest /绝对路径/claude_required_rules_<version>.json \
  --policy /绝对路径/claude_fw_f_profile_policy_<version>.json \
  --output-dir /仓库根目录绝对路径/新的-VC-3-output \
  --source-commit <完整提交> \
  --issued-at <冻结时间>
```

`--output-dir` 必须指向仓库内尚不存在的目录；工具禁止向仓库外写入，也禁止覆盖已有目录。

该工具会初始化新的 Store 和 Campaign，不能承接 VC-1 已存在的 Campaign，因此只作为 2.1.226 已完成实例
和历史重放入口，不能作为后继版本的正式 VC-3 producer。正式流程必须在原 Store 中依次追加
`rule_classification_recorded`、`evidence_approved` 和 `profile_approved`；如果新的阶段 producer 尚不能绑定
RuleMigrationLedger、AtomicAssertionLedger、`affected_rules／inherited_rules` 和模型能力差异，则属于
工具阻断。当前
`claude_fw_g_generation_policy_2_1_226_v2.json` 同时包含 `official_finalize` 与 `acceptance` 的后续阶段身份，
只作为不可变历史聚合制品读取，禁止复制成新 Campaign 的 VC-3 policy。若现有 Schema 强制要求未来字段，
必须先独立拆分工具或策略 Schema，再重新执行 P0；不得用占位 Candidate／收据绕过。
