# 变更集 1B：WHAM-first、探针迁移与 Chrome XHR 修复

## 一、范围结论

- 普通 OAuth 仅对白名单内的 Pro、非 Agent、非 FedRAMP 且账号／计划字段完整形态启用
  usage-only WHAM；Spark shadow 继续走 Bengalfox。
- WHAM 顶层窗口只接受完整的 18000 秒（5h）与 604800 秒（7d）组合；未知时长、缺字段、
  非法百分比或非法 reset 一律拒绝映射，不把 30d 猜成 7d。
- usage 与 reset-credit details 已拆开；周期刷新只发一次 usage 请求，WHAM staleness 不再
  依赖 WSv2。
- 已举证的 WHAM 失败条件可在同一次刷新内进入 Responses fallback；认证失败和瞬时故障
  不触发昂贵 fallback。非白名单计划、Agent Identity、FedRAMP 与老账号缺字段继续使用
  画像化 fallback 或已有快照。
- 管理端 OAuth Responses／compact、PAT alpha-search fallback 与 usage fallback 已进入
  Codex Executor；Executor 明确拒绝本变更集之外的端点，models、whoami、Agent register
  等路径没有顺带扩张。
- privacy 保持独立 browser persona，不经过 Codex Executor。

## 二、Codex Executor 与 fallback

- 生产运行时已把 ReleaseProvider、RequestCompiler、Guard、HTTPUpstream adapter 和
  Executor 接线到同一个依赖图。
- Executor 只接受已登记的 Codex persona SinkBinding、显式 BehaviorPolicy 与
  Responses／compact EndpointID；最终发送必须携带 FinalizationToken，并只通过
  `HTTPUpstream.DoWithTLS` 到达 terminal。
- PAT alpha fallback 使用服务端重建的 Responses body，不把 alpha/search 入站误认成
  完整官方 Responses 身份；缺失身份由画像生成。
- fallback 记录结构化 reason code、最小调用间隔与移除条件；周期刷新不会额外请求
  reset-credit details。

## 三、Chrome 133／XHR 独立发布线

- 新画像固定为 `HelloChrome_133`，UA 与 Client Hint 同步声明 Chrome 133；TLS 测试证明
  `supported_groups` 包含 `X25519MLKEM768`。
- 三个 privacy 生产端点统一为页面内 XHR 语义：`cors`、`same-origin`、`empty`；移除
  `sec-fetch-user`、`upgrade-insecure-requests`、导航 Accept、`cache-control` 与 `pragma`。
- `sec-ch-ua-platform` 与 `accept-language` 从配置／运行环境解析，不再写死 Windows 与英文。
- 新旧 persona 使用稳定账号分桶和隔离连接池；`enabled=false`、`canary_percent=0` 是默认
  审核状态。独立回滚只切回 `legacy_chrome_120`，不会改变 WHAM 或 Codex Executor。
- CF／失败结果写入闭集标签日志与进程内计数；失败账号持久化 retry-after，冷却期内不会
  在每轮 token refresh 重打。

## 四、真实 Chrome 脱敏对照

采集时间：2026-08-02；环境：用户本机已安装 Chrome 150、macOS、中文语言。采集器仅接收
公开浏览器 Header 白名单，没有读取或保存 Cookie、Authorization、localStorage、账号信息
或响应正文；原始临时采集服务已删除。

同一 Chrome 会话的页面导航包含：

- `sec-fetch-mode: navigate`
- `sec-fetch-dest: document`
- `sec-fetch-user: ?1`
- `upgrade-insecure-requests: 1`
- HTML 导航 Accept

由页面按钮触发的同源 XHR 包含：

- `accept: application/json`
- `sec-fetch-mode: cors`
- `sec-fetch-site: same-origin`
- `sec-fetch-dest: empty`
- `sec-ch-ua-platform: "macOS"`
- `accept-language: zh,en;q=0.9,zh-CN;q=0.8`
- `accept-encoding: gzip, deflate, br, zstd`

XHR 明确不含 `sec-fetch-user`、`upgrade-insecure-requests`、`cache-control`、`pragma`。公网
Chrome 同次访问 `https://chatgpt.com/` 正常到达 ChatGPT 页面，没有落入 Cloudflare challenge。
完整脱敏字段见 [chrome-live-capture.json](chrome-live-capture.json)。本机 Chrome 150 用于验证
真实 XHR／导航语义；生产画像的精确 133 版本与 TLS groups 由固定 wire 测试验证。

## 五、验证结果

- `go test ./... -count=1`：除 `cmd/egressscan` 的 scanner algorithm lock 人工复审断言外，
  其余包全部通过；该断言的摘要差异见下一节。
- `go test -tags=unit ./internal/service -count=1`：通过。
- Chrome wire、独立回滚、三端点覆盖、WHAM 严格映射、单请求与同调用 Executor fallback
  聚焦测试：通过。
- `internal/officialegress/...`、Chrome／privacy 与 WHAM／Executor 聚焦 race 测试：通过。
- `go build ./cmd/server`：通过。
- `go generate ./cmd/server`：通过，Wire 生成文件已同步。
- `make check-egress-spec` 的 bootstrap 回放、provisional seal、版本泄漏、§3.5 台账与源码
  引用均已分别验证通过；更新扫描器精确分类后，完整命令按设计停在 scanner algorithm
  lock 人工复审边界。扫描器 self-test 单独运行通过。

## 六、首次审核四项阻塞修复

### 1. OAuth privacy 冷却不再被 enrich 旁路

- `settings/account_user_setting` 的低层网络实现只能由统一 ensure 入口调用；该入口先读取
  账号现有 `privacy_mode`／`privacy_retry_after`，普通刷新不能使用 Force。
- `RefreshAccountToken` 的 enrich 阶段只补全 accounts/check 与 subscription 信息，不再提前
  调用 settings；凭据持久化后的后台或管理端 Ensure 才能尝试设置隐私。
- 首次 OAuth 授权把完整 `OpenAIPrivacyResult` 转为 CreateAccount 的 `Extra`，与凭据在账号
  首次插入时一次写入。Cloudflare 失败会同时保存 mode、persona、retry-after 和 rollout key。
- 生产客户端工厂即使创建客户端失败，也会把配置中的失败冷却返回 service，避免每轮刷新重试。

### 2. privacy 使用账号级稳定分桶

- access token 已完全退出 rollout key 计算。首次授权优先使用 ChatGPT account／organization／
  user 身份；这些字段缺失时使用 OAuth session 的随机稳定身份，并把派生后的
  `privacy_rollout_key` 随账号创建持久化。
- 老账号优先复用已保存的 rollout key，其次按远端账号身份派生，最后才使用本地数据库账号 ID。
- enrich 在调用开始时只解析一次 rollout key，settings、accounts/check、subscriptions 三个端点
  接收完全相同的值；token 轮换前后不会改变 persona。

### 3. Executor 遵守全局 previous 回滚

- 生产 Wire 在构造 `OfficialEgressTransitionRuntime` 时解析一次
  `gateway.official_client_profiles.mode`，并保存为合法 `ReleaseMode`。
- 1B Executor 把同一个 mode 同时传给旧 finalizer 上下文与 `CodexEgressPlan`；ReleaseProvider
  在一次发送中只解析一次 Release。
- previous 模式使用 previous 指针冻结的 UA／originator，不再被 active 运行态把
  `xterm-256color` 覆盖为 `unknown`。测试同时断言 previous Profile、Plan 与最终 Header 一致。

### 4. fallback 行为策略已实际执行

- Executor 新增运行期策略控制器，按 `账号稳定分区 + SinkID + PolicyID` 建立状态；
  `MinimumInterval` 在 transport 前原子预占，失败尝试同样进入冷却。
- `ConcurrencyLimit` 在 Executor 边界等待／释放，并作为实际参数传给 `HTTPUpstream.DoWithTLS`，
  不再使用账号通用并发值替代。
- usage 探针增加账号级 singleflight。手动 `force=true` 只绕过快照陈旧度缓存，不绕过
  singleflight 或 Executor lease；并发 force 合并为一次，顺序 force 在最短间隔内也不会
  再触发有计费副作用的 Responses fallback。

新增回归测试覆盖：OAuth enrich 冷却旁路、首次授权失败原子持久化、token 轮换、三个端点
rollout key 一致、previous 全链路、策略并发槽位、原子最短间隔以及并发／顺序 force。

## 七、复审剩余 OAuth UI 阻塞修复

- 普通 `/admin/openai/exchange-code` 现在只兑换 token 和补全账号信息，不再发送 privacy
  settings；因此不会在账号存在前产生无法原子持久化的冷却或分桶状态。
- 新建账号 UI 已切换到 `/admin/openai/create-from-oauth`。服务端在一个请求中完成授权码
  兑换、三个 browser 端点、Credentials 构建及完整 privacy Extra 创建；客户端只提交模型
  映射等本地配置，不能覆盖 token 或四个受管 privacy 字段。
- 重授权新增 `/admin/openai/accounts/:id/reauthorize`。入口先读取已有账号，冻结持久化的
  rollout key 与冷却状态，再兑换 token；即使新 token 补齐远端 account/user ID，本轮
  accounts/check、subscriptions、settings 仍使用同一个 key。
- 重授权的 Credentials 与 `privacy_mode`、`privacy_retry_after`、
  `privacy_browser_persona`、`privacy_rollout_key` 通过同一次账号 Update 写入。两个同名
  重授权组件均已切换到该入口，不再通过浏览器搬运 token 或 Extra。
- 通用 `UpdateAccount` 将上述四个字段视为服务端受管状态：部分 Extra、显式空 Extra 或
  客户端伪造的新值均不能删除或覆盖；只有内部 `ManagedPrivacyExtra` 通道可以更新。
- 服务端受管边界已扩展为所有通用写入口闭集：通用 `CreateAccount` 会剥离四个字段，只有
  内部 `CreateAccountInput.ManagedPrivacyExtra` 可以在专用 OAuth 创建时写入；bulk-update 与
  `UpdateAccountExtra` 在任何 repository 写入前显式拒绝这些字段。
- 遗留 `apply-oauth-credentials` 在更新 Credentials 前执行同一受管字段校验，OpenAI 调用方
  无法借该兼容入口伪造成功状态、retry-after、browser persona 或 rollout key。
- 新增生产等价回归覆盖专用创建 Cloudflare 单次请求、普通 exchange 无 settings 副作用、
  通用创建不双打及无法预置受管字段、重授权冷却、三个端点冻结 key、远端 ID 后补齐、
  原子写入、bulk／Extra patch／遗留凭据入口反例，以及前端创建／重授权组件只调用服务端
  受管入口。
- 所有普通创建后的自动隐私尝试已统一收敛到 `CreateAccount` 内的 Ensure：单账号 Handler、
  批量创建和数据导入不再追加 Force；显式管理端“重新设置隐私”仍保留 Force 语义。
- 异步 Ensure 使用独立账号快照，克隆 `Credentials` 与 `Extra` 后再进入后台任务，避免与
  Handler 构造响应并发读写同一个 map。Handler 级生产等价测试使用真实
  `adminServiceImpl.CreateAccount` 和 counting transport，验证 Cloudflare 失败只发送一次
  settings、持久化 retry-after，并通过聚焦 `-race`。

## 八、审核边界

- 1A 的 `legacy-baseline.json`／`legacy-ceiling.json` 继续保持 `provisional`。在受保护
  Git base 尚未建立前，四条已迁移 Sink 仍保留在与 ceiling 相同的 provisional
  baseline 中；只有 seal 后才能凭收据单调移除，本次没有伪造 seal。
- `migration-receipts.json` 已为管理端 Responses、compact、PAT alpha-search
  fallback 与 usage fallback 分别签发 `canary_enforce → enforced` 收据链。每份收据都绑定
  `codex.executor.changeset1b`、ReleaseBinding candidate、完整 route、Adapter/
  Transport，以及从生产 Executor 重放生成的脱敏最终请求和执行验证
  产物；enforced 收据额外引用 DMIT canary 观察和 acceptance 产物。测试重算并比对全部文件。
- `removal-receipts.json` 已冻结 PAT fallback 的旧 HTTPUpstream facade 候选和
  usage fallback 的两个旧 `net/http` 候选，并引用覆盖对应 candidate 的
  MigrationReceipt 摘要。
- 1B 新增精确 terminal delegation 分类，并移除已迁移 usage probe 的旧 net/http 自测事实，
  因而 scanner algorithm 摘要从
  `b17e4f79acf556f435a3f412062e5dffbac9defd347708049ef9ad43b8ba0db3` 变为
  `1ece3437e3a970565db243f8ac9b7aa723c53ae23ac68f2e960da32d6f97e635`。lock 已按
  用户验收的变更集 0／1A／1B 审核信息更新，后续任一扫描器变更仍会失败。
- Cloudflare 画像前后通过率由独立 privacy canary 产生。当前已具备关闭状态基线、稳定分桶、
  结构化指标、失败冷却和单独回滚；DMIT 实测与本地三端点生产链测试均已固化到 1C 主动演练清单。

上述 canary、acceptance 与 enforced 收据链已在变更集 1C 完成。privacy 继续使用独立灰度与
回滚线；若恶化，直接回滚到 legacy persona，不影响 WHAM／Executor。1C 的 UnknownRoutePolicy、
UnregisteredSinkPolicy 与单 Sink 同镜像回滚证据见 `docs/changeset1c/`。
