# SPEC-EP-002 官方侧证据阻断（审核结论与后继方案）

> 状态：**根因已确认，阻断未解除**。A11／A13／A14 的 job 编排完成，但目标协议分支
> 均未成立；不能据此削弱 `SPEC-EP-002`。下一步先补场景真实性失败关闭，再修正安全触发。
> 日期：2026-08-10
> Campaign：`codex-0145-to-0147-20260810T000000Z-k36`
> campaign_id：`codex-0_147_0-20260809T221440Z`
> 官方 attempt：`20260809T221547Z-6f7ed96350016cc0`（19/19 job 编排完成，22 个证据根）
> attempt 状态：`awaiting_receipts`，`seal` 未执行
> 工具身份：91 项，`files_sha256 = c8cf3c6529e84d6f8d4beba3ebab2dbccd95664cbe1f98e65aa1fd788811ba82`
> 采集机：Vircs，源码树 `/root/oauth-capture/candidate-source-k36`

## 1. 规则与判据

`SPEC-EP-002`「业务、刷新、realtime 与文件上传使用各自域名来源」，冻结画像
`candidate_rule_expectations_0_145_0.json` 中的定义：

| check | 限定场景 | 断言 |
|---|---|---|
| `chatgpt-sni` | A01 | `any_equal(data.sni, "chatgpt.com")` |
| `auth-sni` | A13 | `any_equal(data.sni, "auth.openai.com")` |
| `api-sni` | A11 | `any_equal(data.sni, "api.openai.com")` |
| `regional-file-sni` | A14 | `where data.sni ~ ^[a-z0-9.-]+\.oaiusercontent\.com$` + `count_at_least 1` |
| `file-url-chain` | A14 | `file_upload_chain` 内部记录（候选侧产出，不在本文范围） |

四条 SNI check 的 `record_type` 均为 `tls_client_hello`，只能由 pcap 解析得到。

## 2. k36 的可确认事实

### 2.1 补采动作

1. `run_official_relay_scenario.sh` 新增可选 `CAPTURE_CLIENT_HELLO=1`，在中继启动前
   于容器内启动 `tcpdump -i lo -s 0 -U -w …/direct/traffic.pcap 'tcp port 443'`；
2. `official-relay-realtime-webrtc` 与 `official-relay-file-upload` 开启该抓包；
3. 新增 `official-relay-oauth-refresh`，把 `auth.json.last_refresh` 临时改为
   `2020-01-01T00:00:00Z`，退出时逐字还原；
4. 三个 job 均产出 pcap 与 relay 字节，并被编排器记为 `complete`。

中继按 `/etc/hosts` 把预列域名指向 loopback，不会改写客户端填写的 SNI。因此对于已经
进入中继的连接，pcap 与 relay 可以互相核对。

### 2.2 观测结果

| job | relay 连接 | pcap 中的 SNI | relay 记录的 SNI | 目标分支结果 |
|---|---:|---|---|---|
| `official-relay-oauth-refresh` | 3 | `chatgpt.com` ×3 | `chatgpt.com` ×3 | 没有 OAuth refresh 请求 |
| `official-relay-realtime-webrtc` | 3 | `chatgpt.com` ×3 | `chatgpt.com` ×3 | call-create 返回 400，无 sideband |
| `official-relay-file-upload` | 4 | `chatgpt.com` ×4 | `chatgpt.com` ×4 | 模型未调用上传工具，无 file create/PUT |

pcap 与 relay 一致，证明这次执行中三个目标连接没有发生。它们不能证明：

- 目标域名在正确场景下不可观测；
- `SPEC-EP-002` 判据错误；
- A14 当前抓包覆盖了任意响应返回的区域主机。

A14 当前只抓 loopback，`RELAY_HOSTS` 还预列了固定的
`sdmntprwestus3.oaiusercontent.com`。而上传目标来自服务端响应，现有实现不满足规格中
“按端口捕获所有主机”的覆盖要求；即使触发修正，也必须同时消除这一盲区。

## 3. 根因审核

### 3.1 共性根因：脚本成功不等于场景成功

三个 job 的完成判据只约束脚本是否结束和基础产物是否存在，没有验证目标协议分支。
其中 realtime 与通用 `codex exec` 路径会吞掉目标命令失败；realtime 驱动在 RPC 请求被
异步接受后直接返回成功，没有等待后续 `thread/realtime/started`、SDP 或
`thread/realtime/error`。因此 `19/19` 只能称为 **job 编排完成**。

### 3.2 A11：quicksilver 版本不匹配，未进入 sideband

实际请求：

```text
POST /backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas
HTTP/1.1 400 Bad Request
error.code = invalid_quicksilver_alpha_header
error.message = AVAS requires OpenAI-Alpha: quicksilver=v2.
```

随后日志只有 `thread/realtime/error`，没有 `call_id` 和成功 SDP。冻结的 0.147 源码显示：

- 默认 WebRTC V1 发送 `OpenAI-Alpha: quicksilver=v1`；
- V3／Frameless Bidi 发送 `quicksilver=v2`；
- call-create 成功后，sideband 默认 API base 才是 `https://api.openai.com/v1`。

源码坐标（正式冻结树 commit `be6e8eac…`）：`core/src/realtime_conversation.rs:1297-1335,1647-1661`；
`codex-api/src/endpoint/realtime_websocket/methods.rs:709-791,976-992`。

结论：不是“可能需要真实 WebRTC 环境”，而是本轮在第一跳已确定失败。

### 3.3 A13：`last_refresh` 不是正常 JWT 的过期权威

冻结的 0.147 源码先解析 access token JWT 的 `exp`；只有无法从 token 取得有效期时，
才回退使用 `last_refresh`。正式冻结树源码坐标：`login/src/auth/manager.rs:2762-2783`。

结论：对于正常 JWT，单改 `last_refresh` 必然不会触发刷新。这不是概率推测，也不能把
这次普通业务请求认作 A13。

### 3.4 A14：模型没有调用文件上传工具

CLI 明确返回：

```text
无法执行：当前接口里没有可直接调用的 save_site_version 工具。
```

relay 中出现 `/backend-api/ps/mcp`，其响应包含 `save_site_version` 与
`openai/fileParams` 元数据，但后续没有工具调用、`/backend-api/files` create 或区域 PUT。
冻结源码显示文件链为 create 后读取响应的 `upload_url`，再向该 URL 执行 PUT，成功后再
请求 `/uploaded`：`codex-api/src/files.rs:126-275`。

结论：本轮只证明 Apps 工具元数据被发现，未证明模型可见并调用了该工具。

## 4. 审核决定

### 4.1 当前不采用条件判据

拒绝把 `auth-sni`／`api-sni`／`regional-file-sni` 改成“观测到才断言，未观测即通过”。
当前缺口由场景未成立导致，不是规则已被证明不可观测；此时条件化会把触发失败永久掩盖。

### 4.2 k36 不具备封存资格

k36 的原始诊断产物保持不变，便于复现根因；但 A11／A13／A14 不能作为目标场景证据，
attempt 保持 `awaiting_receipts`，不得执行 `seal`，也不得在后续 Campaign 中迁移或复用。

### 4.3 仍然排除的手段

| 手段 | 排除理由 |
|---|---|
| 伪造 JWT `exp` 或签名 | 替换认证事实，且无法用真实签名安全构造 |
| 清空／破坏 access token 以诱发 401 | 会改写活跃凭据状态，刷新成功还可能写回新 token，风险不对称 |
| 人工构造 sideband 请求 | 绕开官方 call-create 状态机，替换被测出站形态 |
| 注入假的区域上传 URL | 伪造服务端响应，不能证明真实 create → PUT 链 |

## 5. 后继方案

### 5.1 先实现 `SCN-REALITY-01` 场景真实性门禁

job 只有同时满足基础产物和以下目标收据才允许 `complete`：

| 场景 | 必须机器验证的成功收据 |
|---|---|
| A11 | call-create 为 2xx；取得 `call_id` 与 SDP／started 通知；出现 `api.openai.com` sideband SNI；任何异步 realtime error 均失败 |
| A13 | 官方 CLI 实际发送 `POST auth.openai.com/oauth/token`；同一证据根存在 `auth.openai.com` SNI；普通 models／Responses 请求不能代替 |
| A14 | 模型实际调用目标 Apps 工具；出现 file create；`upload_url` 可追溯到真实响应；CLI 向该 URL PUT 并完成 uploaded；同一链存在响应派生区域主机的 SNI |

驱动程序必须等待最终通知，脚本不得以 `|| true` 吞掉目标分支失败。负例至少覆盖：
call-create 400、只改 `last_refresh`、工具未调用、只有 pcap 文件但缺目标记录。

### 5.2 再修正三个安全触发

1. **A11**：显式选择官方 0.147 已实现的 V3／`quicksilver=v2` 路径，仍由官方 CLI
   创建 call 和 sideband；不手工构造任何 sideband 请求。
2. **A13**：使用隔离采集账号和隔离 `CODEX_HOME`，预先冻结非秘密账号身份与凭据摘要，
   等待真实 JWT 自然进入 CLI 的 5 分钟刷新窗口，由官方 CLI 自行刷新；采集后逐字复核
   隔离目录和账号状态，不触碰日常登录目录。
3. **A14**：先冻结 0.147 下模型可见的 Apps 工具暴露与调用契约，使用能真实触发
   `save_site_version` 的官方交互；上传目标必须来自真实 create 响应。抓包改为按端口覆盖
   所有目标主机，不能依赖预列某个区域域名。

### 5.3 新建 k37 重采

`SCN-REALITY-01`、驱动、抓包范围或场景定义任一变化都会改变受管工具身份。完成独立审核
和测试后，应冻结新工具摘要并新建 k37，从官方 `run` 开始完整重采；不得在 k36 上续跑。

## 6. 执行顺序与退出条件

1. 文档审核结论合并，保持 Active=0.145；
2. 单独提交 `SCN-REALITY-01` 方案并审核；
3. 实现场景真实性门禁及正反测试；
4. 单独提交 A11／A13／A14 安全触发方案并审核；
5. 实现并做可丢弃预检，冻结工具身份；
6. 新建 k37，完成官方重采、收据、secret scan 与 `seal`；
7. 只有三条必现判据都有真实场景证据，才进入 classify。

如果正确触发、全主机抓包和真实性门禁都完成后仍无法观测，才按 §6.4 把该规则标为
`blocked` 并重新讨论规则语义；在此之前不降低判据。

## 7. 当前结论

- 根因：**场景真实性未验证**，不是 `SPEC-EP-002` 已被证明不可执行；
- k36：19/19 job 编排完成，3 个目标场景失败，诊断产物保留，`seal` 不执行；
- 规则：保持 `auth-sni`／`api-sni`／`regional-file-sni` 必现语义；
- `SCN-REALITY-01` R0 已完成，已冻结收据生命周期、双侧 trigger、批准画像交叉校验和 A14 可证明字段；
- 下一步：从 R1 真实性门禁开始，再修正安全触发，最后新建 k37 完整重采；
- Active/Previous 仍均为 0.145，0.147 未替换。

具体变更集、交付物和退出条件以升级计划 §11 为准。
