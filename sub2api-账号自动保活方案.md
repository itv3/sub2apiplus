# sub2apiplus-keeper 自动保活方案草稿

更新时间：2026-07-01

## 1. 目标

`sub2apiplus-keeper` 是一个独立保活组件，不和 `sub2apiplus` 一起打包。

它的目标是：

- 对指定 OpenAI / Claude 账号做低频自动保活。
- 系统主动模拟一次轻量开发对话，不发送固定 `hi`。
- 每次收到完整上游回复后开始计时，到点后再发下一条模拟消息。
- 下一条模拟消息可以基于上一轮回复摘要继续深入。
- 只在设定工作时间内触发，比如 `04:00-24:00`。
- 在 `sub2apiplus-keeper` 网页上展示每个账号的保活状态、token 用量和费用统计。
- 默认关闭，只对配置中的目标账号生效。

这个功能的定位是“账号 / 链路预热”，不是模拟真人持续聊天。

## 2. 总体架构

推荐架构：

```text
sub2apiplus-keeper
  -> 调用 sub2apiplus 内部 keeper 接口
    -> sub2apiplus 按 account_id 指定账号
      -> 复用现有 OpenAI / Claude mimic / TLS / proxy / base_url / 计费逻辑
        -> 上游
```

职责划分：

- `sub2apiplus-keeper`：定时、工作时间窗口、上一轮摘要、失败退避、任务模板、网页展示、状态存储。
- `sub2apiplus`：根据指定 `account_id` 发送保活请求，复用现有请求形态、mimic、TLS、proxy、账号凭据和计费口径。

这样 `sub2apiplus-keeper` 不需要知道上游账号密钥，也不需要复制 OpenAI / Claude mimic 逻辑。

## 3. sub2apiplus 最小改动

在 `sub2apiplus` 内新增一个内部接口：

```text
POST /api/v1/internal/keeper/accounts/:id/keepalive
```

请求体示例：

```json
{
  "model": "gpt-5.5",
  "prompt": "请继续分析上一轮提到的边界值测试。",
  "max_output_tokens": 300
}
```

规则：

- `:id` 是 sub2apiplus 内部 `account_id`。
- 平台从账号自身读取，不由 keeper 传入，避免配置错。
- 只允许 active / enabled / schedulable 的账号执行。
- OpenAI 账号走现有 `/v1/responses` 或 Codex mimic 语义。
- Claude / Anthropic 账号走现有 `/v1/messages` 或 Claude mimic 语义。
- 不走普通 group 选号，直接指定账号。
- 不暴露给普通用户 API key。

鉴权：

```bash
SUB2APIPLUS_KEEPER_INTERNAL_TOKEN=xxx
```

`sub2apiplus-keeper` 请求时带：

```text
Authorization: Bearer <keeper_internal_token>
```

也支持 `X-Keeper-Token: <keeper_internal_token>`。如果主服务没有设置 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` 或兼容变量 `KEEPER_INTERNAL_TOKEN`，内部接口返回 404，相当于关闭。

这个 token 只用于 keeper 内部接口，不复用 admin 登录态，也不复用普通用户 API key。

## 4. 内部接口返回

接口返回需要直接带上本次保活的用量和费用，费用口径以 `sub2apiplus` 现有计费标准为准。

示例：

```json
{
  "success": true,
  "account_id": 50,
  "account_name": "openai-main",
  "platform": "openai",
  "account_type": "oauth",
  "model": "gpt-5.5",
  "prompt": "请继续分析上一轮提到的边界值测试。",
  "reply_text": "可以补充边界值测试，例如小于最小值、等于最大值、超过最大值。",
  "usage": {
    "input_tokens": 120,
    "output_tokens": 88,
    "cache_read_tokens": 0,
    "total_tokens": 208
  },
  "billing": {
    "available": true,
    "rate_multiplier": 1,
    "input_cost": 0.00012,
    "output_cost": 0.0003,
    "total_cost": 0.00042,
    "actual_cost": 0.00042,
    "billing_mode": "token",
    "pricing_source": "sub2apiplus"
  },
  "started_at": "2026-07-01T15:40:00+08:00",
  "completed_at": "2026-07-01T15:40:02+08:00",
  "latency_ms": 1820,
  "upstream_request_id": "req_xxx"
}
```

关键点：

- token 用量由 `sub2apiplus` 按现有解析逻辑返回。
- 费用由 `sub2apiplus` 按现有计费标准计算后返回。
- `sub2apiplus-keeper` 只保存和展示结果，不自己维护模型价格表。
- 如果计费服务或模型价格不可用，`billing.available=false`，keeper 仍保存本次会话和 token 用量。

## 5. sub2apiplus-keeper 配置

配置文件建议叫 `keeper.yaml`。

```yaml
enabled: true
timezone: Asia/Shanghai
scan_interval_seconds: 120
max_workers: 2

sub2apiplus:
  base_url: https://sg.3ab.in
  internal_token: ${KEEPER_INTERNAL_TOKEN}

web:
  enabled: true
  listen: 0.0.0.0:38090
  username: admin
  password: ${KEEPER_WEB_PASSWORD}

targets:
  - name: openai-main
    enabled: true
    account_id: 50
    model: gpt-5.5
    interval_minutes: 90
    work_start: "04:00"
    work_end: "24:00"
    max_daily_runs: 8
    max_output_tokens: 300
    prompt_profile: safe_code_review

  - name: claude-main
    enabled: true
    account_id: 51
    model: claude-opus-4-8
    interval_minutes: 90
    work_start: "04:00"
    work_end: "24:00"
    max_daily_runs: 8
    max_output_tokens: 300
    prompt_profile: safe_code_review
```

配置里只需要 `account_id`，不需要为每个账号创建单独 group，也不需要保活专用 API key。

## 6. 保活规则

每个 target 记录状态：

- `last_keepalive_received_at`：最近一次自动保活收到完整上游回复的时间。
- `last_keepalive_prompt`：最近一次自动保活发出的模拟消息摘要。
- `last_keepalive_reply_summary`：最近一次上游回复的简短摘要，用于下一轮继续深入。
- `last_keepalive_at`：最近一次自动保活发送时间。
- `daily_keepalive_count`：当天保活次数。
- `consecutive_failures`：连续失败次数。
- `next_run_at`：下一次预计触发时间。

触发条件：

1. keeper 全局开关开启。
2. target 配置开启。
3. 当前时间在工作窗口内。
4. 距离上一次自动保活收到完整回复超过配置间隔，比如 90 分钟。
5. 如果还没有保活记录，则按启动后的首次扫描触发第一轮。
6. 当天次数没超过上限。
7. 当前 target 不在失败退避期。

关键点：

- 计时从“收到完整上游回复”开始，不是从发送请求开始。
- 普通业务请求不作为这个功能的计时来源。
- 自动保活要有每日上限和失败退避，避免变成无限自聊。

## 7. 保活内容

不要发固定 `hi`。第一版用内置安全任务池，不读取真实项目、不带用户原文。

可以准备几类模拟对话模板：

- 小函数代码审查：指出 2-3 个可读性或边界问题。
- 测试用例设计：为一个小配置函数设计 3 个测试用例。
- 重构方案比较：比较两种简单实现的优缺点。
- 文档摘要：总结一个很短的技术片段。

示例：

```text
请审查下面这个小型 Go 函数，指出 2-3 个可读性或边界处理优化点。
要求：不要重写完整文件，只给出简短建议。

func clampMinutes(v int) int {
    if v < 1 { return 1 }
    if v > 1440 { return 1440 }
    return v
}
```

下一轮可以基于上一轮回复继续深入，例如：

```text
你刚才提到边界值需要补测试。请继续给出 3 个最小测试用例，只写 case 名称、输入和期望输出。
```

第一版保存完整 prompt 和完整回复，方便网页上回看每一次保活会话。下一轮生成 prompt 时只截取上一轮回复的一小段作为继续深入的上下文。

## 8. keeper 网页

`sub2apiplus-keeper` 自带一个轻量网页，用来查看每个账号的保活情况和所有保活会话。

首页表格建议展示：

- target 名称。
- `account_id`。
- 平台和模型。
- 当前状态：正常、等待下次触发、失败退避、今日已达上限、窗口外。
- 上次保活时间。
- 上次收到完整回复时间。
- 下次预计触发时间。
- 今日保活次数 / 每日上限。
- 连续失败次数。
- 最近一次错误。
- 今日 token 用量。
- 今日费用。
- 累计 token 用量。
- 累计费用。

单账号详情页展示：

- 该账号的所有保活会话，按时间倒序展示。
- 支持按今日、7 天、30 天、自定义时间范围过滤。
- 每次的 prompt 摘要。
- 每次的回复摘要。
- 每次的完整 prompt 内容。
- 每次的完整回复内容。
- 每次发送时间。
- 每次收到完整回复时间。
- 每次从发送到完成的耗时。
- 输入 token、输出 token、cache token、总 token。
- 单次费用。
- 延迟。
- 成功 / 失败状态。
- 上游状态码和脱敏错误信息。

每条会话至少记录这些时间字段：

- `scheduled_at`：本次保活原计划触发时间。
- `started_at`：实际开始发送请求时间。
- `completed_at`：收到完整上游回复时间。
- `next_run_at`：基于本次回复计算出的下一次预计触发时间。

对话内容展示：

- 默认列表只展示 prompt 摘要和回复摘要。
- 点击某条会话后，可以展开查看完整 prompt 和完整回复。
- 内容来自 keeper 自己生成的保活对话，不保存普通业务请求内容。
- 如果后续允许自定义 prompt 模板，需要在网页上明确标注来源，避免和普通业务对话混淆。

统计口径：

- token 和费用以 `sub2apiplus` 内部接口返回为准。
- keeper 只做汇总展示，不自己计算模型价格。
- 支持按今日、7 天、30 天、全部时间统计。

## 9. 当前落地约定

第一版按最小入侵实现：

- `sub2apiplus` 只新增内部接口 `POST /api/v1/internal/keeper/accounts/:id/keepalive`。
- 主服务通过环境变量 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` 启用接口；未配置时接口返回 404。
- `sub2apiplus-keeper` 是独立 Docker 服务，不打进主服务镜像。
- keeper 默认监听 `38090`，配置文件为 `/app/keeper.yaml`。
- keeper 状态文件默认为 `/app/data/state.json`，建议 Docker 挂载卷持久化。
- keeper 网页支持 Basic Auth，账号密码由 `KEEPER_WEB_USERNAME` / `KEEPER_WEB_PASSWORD` 配置。
- keeper 不保存任何上游账号密钥，只保存保活会话、prompt、reply、usage、billing、状态和时间。

## 10. 状态存储

第一版建议 `sub2apiplus-keeper` 自己保存状态，不写 sub2apiplus 数据库。

当前实现使用本地 JSON 状态文件，记录：

- targets 当前状态。
- 每次保活运行记录。
- 每次 usage 和 billing。
- 每次完整 prompt 和完整回复。
- 每日汇总缓存。

`sub2apiplus` 只负责执行请求并返回结果，不保存 keeper 的调度状态。

## 11. 第一版范围

先做：

- 独立程序 / 独立 Docker 镜像：`sub2apiplus-keeper`。
- 支持 OpenAI 和 Claude / Anthropic。
- 通过 `account_id` 调用 sub2apiplus 内部 keeper 接口。
- sub2apiplus 最小新增内部接口。
- 工作时间窗口。
- 收到上一次完整保活回复后，间隔 90 分钟触发下一轮。
- 每个 target 每天最多 8 次。
- 内置安全任务池。
- 基于上一轮回复摘要继续深入。
- 失败退避。
- JSON 状态文件存储。
- keeper 网页展示状态、token 用量和费用统计。

先不做：

- 把 keeper 打包进 sub2apiplus。
- 给每个账号创建单账号 group。
- 给每个账号创建保活专用 API key。
- 读取 sub2apiplus 数据库。
- 自动读取真实项目。
- 无限制多轮自动对话。

## 12. 需要拍板的问题

1. 内部接口路径是否用 `/api/v1/internal/keeper/accounts/:id/keepalive`？
2. keeper 网页是否先用简单 Basic Auth？
3. 间隔默认用 90 分钟是否合适？
4. 每个 target 每天最多 8 次是否合适？
5. 内部接口返回费用时，是否需要同时返回原始 usage 和最终 billing 两块？

我的建议：第一版做独立 `sub2apiplus-keeper`，用 JSON 状态文件存状态；`sub2apiplus` 只最小新增一个按 `account_id` 发送保活请求的内部接口，并由这个接口返回按 sub2apiplus 计费标准计算后的 token 用量和费用。
