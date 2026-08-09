# Codex 官方出站采集操作要点

> 从升级计划 §10 抽出。这些是采集与封存时反复踩过的坑，属于操作手册，
> 不随单个 Campaign 变化。升级计划正文只引用本文，不再内联。

## 候选验收链路的正确顺序（此前一直做反）

`capture-candidate seal` 第一次调用输出的"使用上述不可变边界生成收据"，指的是**用这个
边界去校验已经发生的请求**，不是"之后再发请求"。`client_checkpoint_at_utc` 来自一次
真实的 Kilo 后环境探针，第三方入站请求必须落在它**之前**：

```
capture-candidate run（8 个任务）
  → 人工用 ZLF Code 发 Compatible 一条、Responses 一条
  → capture-candidate seal（采 client-after 探针，得到 checkpoint）
  → 生成 observed-profile 与两份 Kilo 收据
  → capture-candidate seal --approve-seal-sha256
```

顺序反了会同时踩两个坑：入站请求晚于 checkpoint 被判"不在客户端证据时间窗内"；为迁就
它去刷新 checkpoint，又会把服务运行画像观测挤出窗口。两者只能靠正确的先后顺序同时满足。

被归档的 `client-after` 探针目录可以重采以推后 checkpoint——但这只在**没有动过候选源码树**
时可行，见 §10.3。

## 候选源码树在 run 与 seal 之间不可触碰

`_directory_tree_digest` 扫描整个候选源码树，**不排除 `__pycache__`**，而 `.pyc` 内嵌源
文件的 mtime。因此在 `run` 之后向该目录写入任何文件——哪怕随后逐字还原——都会让摘要
永久漂移，`seal` 以"候选源码树在 run／seal 之间发生漂移"拒绝，只能重跑候选采集。

**收据生成器必须放在候选源码树之外运行**（例如 `runtime/build_kilo_receipts.py`），
产物写进 attempt 的 `evidence/receipts/`。

## 收据是两层结构

`build_*` 系列产出的是 **finalizer 的输入**，不是可直接提交给 seal 的收据：

| 环节 | 输入产出方 | 正式收据产出方 |
|---|---|---|
| 运行画像 | `build_observed_profile_runtime_audit.py` | `receipt_finalizer observed-profile` |
| Kilo 双协议 | `build_kilo_client_receipts.py` | `receipt_finalizer kilo-binding` |

直接把输入交给 seal 会得到"收据 schema_version 不受重放器支持"。另外 kilo-binding 要的
`runtime_audit` 与 observed-profile 那份**不是同一种**：前者描述单次调用的出站形态
（transport／upstream_endpoint／auth_mode／affected_branches），后者描述服务整体解析到
哪个画像。五份 Kilo 事实还要求时间非递减：安装 ≤ 入站 ≤ 出站 ≤ 响应 ≤ 用量。

## 采集期间的环境要求

- **激活事实必须在采集窗口内落盘**。部署 override 需注入
  `GATEWAY_EGRESS_ACTIVATION_FACT_PATH` 与全套身份变量，其中 `SOURCE_TREE_SHA256` 必须
  取**候选 attempt** 的 `identity.source_tree_sha256`（不是 official attempt 的）；服务会
  用自己解析的 digest 与声明逐字比对，不一致拒绝落盘。
- **合成 relay 的固定响应 ID 每轮必撞计费去重**。`resp_candidate_core_*` 被服务当作计费
  request_id，上一轮的记录会让本轮 fingerprint 冲突、用量行不落盘，Kilo 收据因此缺
  `usage_id`。已在 `kilo-byte-capture-r12.sh` 启动时自动清理。
- **Codex Desktop 会污染采集**。它同样使用账号 90 并周期性访问 `/v1/responses`，
  其请求会被 relay 判为"抓到了"。采集前必须退出，并确认一段时间内零新增。
- **ZLF Code 两条入口的 User-Agent 形态不同**：Compatible 走 ai-sdk（UA 自报
  `Kilo-Code/<版本>`），Responses 的 WebSocket 走 OpenAI 官方 JS SDK（UA 为 `OpenAI/JS`）。
  收据工具对后者回落到本机安装事实取版本。
- **WS 入站的 access log 在连接关闭时才写**，其 `completed_at` 是连接生命周期结束时刻，
  不是响应完成时刻；response witness 应取用量记录时刻，否则与 usage 的时间顺序倒挂。

## 本轮修复的缺陷

| 缺陷 | 提交 | 说明 |
|---|---|---|
| A13：`req.Client` 链 Guard 包装顺序 | `31768d0a6` | 既有生产缺陷，OAuth token 刷新被自身 Guard 拒绝，详见 §7.7 |
| A11：Linux Live attestation 接线 | — | `candidatecapture` 分支引用不存在的函数，详见 §7.8 |
| 环境恢复把容器重建判成污染 | `e5717d678` | 容器实例 ID 与 hosts 自引用行由 Docker 生成，不表达采集副作用 |
| A12 期望计数写错 | `7178edbf9` | `ResetQuota` 必然再查一次用量，实际是 2/2/1 |
| images-wire 不自备模型映射 | `7178edbf9` | 生图请求在入口即 404、根本不出站 |
| 候选镜像不可复现 | `e78a7556a`、`ee7170c32` | `BUILD_TAGS` 与 `FRONTEND_SOURCE` 参数化 |
| 同 attempt 内无法补跑失败任务 | `3fcc465a9` | 上游波动导致整轮 20 分钟作废 |
| resume 丢弃上一轮成功任务 | `9bb0ec767` | 承接 + 环境连续性证明 |
| comp-hash-changed 依赖 gpt-5.6-luna | `f94efb85b` | 改用受控模型目录 |
| 官方 CLI 偶发波动无重跑 | `9ffb836ce` | s4 的 hook 计数偶发变 2 |
| Kilo 两条入口 UA 形态不同 | `88937e40b` | 详见 §10.5 |
| 四类收据无产出方 | `65983ce15`、`0930a05aa` | capture-manifest、逐规则验收结果、Kilo 收据 |
| kilo-binding 缺单次调用 runtime_audit | `58cd4b670` | 与 observed-profile 的整体审计不同种，见 §10.4；内容承接服务端记录，工具不推断 |
| 编排器自造断言命令 | `5440acf96` | 与 accept 用 `build_assertion_command` 复算的期望命令永不相等，results 无法通过 accept |
| 编排器与 accept 契约权威不同源 | `5440acf96` | 编排器取仓库冻结画像、accept 取批准画像，规则集增删时两端各说各话 |
