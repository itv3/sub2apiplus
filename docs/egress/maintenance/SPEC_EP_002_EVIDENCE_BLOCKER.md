# SPEC-EP-002 官方侧证据阻断（待外部审核）

> 状态：**未解决，等待外部工程师审核**。本文只陈述实证与备选方案，不预设结论。
> 日期：2026-08-10
> Campaign：`codex-0145-to-0147-20260810T000000Z-k36`
> campaign_id：`codex-0_147_0-20260809T221440Z`
> 官方 attempt：`20260809T221547Z-6f7ed96350016cc0`（19/19 job complete，22 个证据根）
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

四条 SNI check 的 record_type 均为 `tls_client_hello`，只能由 pcap 解析得到。

## 2. 实证：三个域名在官方侧一次都没出现

### 2.1 采集前的状态

k35（上一轮）全部 direct pcap 共 68 个 ClientHello，**SNI 全部为 `chatgpt.com`**；
A11／A14 绑定的 job 当时根本不抓包，A13 官方侧无任何 job。

### 2.2 本轮的补采动作

1. `run_official_relay_scenario.sh` 新增可选 `CAPTURE_CLIENT_HELLO=1`，在中继启动前
   于容器内起 `tcpdump -i lo -s 0 -U -w …/direct/traffic.pcap 'tcp port 443'`；
2. `official-relay-realtime-webrtc`（`RELAY_HOSTS="chatgpt.com api.openai.com"`）与
   `official-relay-file-upload`（`RELAY_HOSTS` 含 `auth.openai.com api.openai.com
   sdmntprwestus3.oaiusercontent.com`）开启该抓包；
3. 新增 `official-relay-oauth-refresh` job 与同名场景，`RELAY_HOSTS="chatgpt.com
   auth.openai.com"`，以 I 类触发干预把 `~/.codex/auth.json` 的 `last_refresh` 改为
   `2020-01-01T00:00:00Z`，期望官方 CLI 判定需要刷新；`access_token`／`refresh_token`／
   账号绑定一律不改，脚本退出时逐字还原。

**中继劫持不影响 SNI 证据**：中继按 `/etc/hosts` 把域名指向 `127.0.0.1`，但 SNI 由
客户端在 ClientHello 中填写、与实际目标 IP 无关，因此只要 CLI 发起过对某域名的连接，
pcap 中就应出现该 SNI。

### 2.3 结果

三个 job 均 `complete`，均产出 pcap 与 relay 字节：

| job | relay 连接 | pcap | pcap 中的 SNI | relay 记录的 SNI |
|---|---|---|---|---|
| `official-relay-oauth-refresh` | 3 | 1 | `chatgpt.com` ×3 | `chatgpt.com` ×3 |
| `official-relay-realtime-webrtc` | 3 | 1 | `chatgpt.com` ×3 | `chatgpt.com` ×3 |
| `official-relay-file-upload` | 4 | 1 | `chatgpt.com` ×4 | `chatgpt.com` ×4 |

**pcap 与中继两个独立观测点一致**：官方 CLI 在这三个场景下**没有向
`auth.openai.com`／`api.openai.com`／`*.oaiusercontent.com` 发起任何连接**。这不是
抓包遗漏或劫持配置问题。

### 2.4 复现命令

```bash
# 采集机 Vircs
cd /root/oauth-capture/candidate-source-k36
B=/root/oauth-capture/runs
for j in oauth-refresh realtime-webrtc file-upload; do
  d=$(ls -d $B/*-official-$j | tail -1)
  python3 tools/official_client_capture/pcap_clienthello.py --dir "$d/direct"
done
```

## 3. 三个假设为何失败

| 场景 | 假设 | 观测 | 推断 |
|---|---|---|---|
| A13 | 改 `auth.json` 的 `last_refresh` 会让 CLI 判定 token 过期并刷新 | 无 `auth.openai.com` 连接 | CLI 大概率以 `access_token`（JWT）内的 `exp` 判断过期，而非 `last_refresh` 字段 |
| A11 | `realtime-webrtc` 场景会建立 `api.openai.com` 的 sideband | 场景跑通但无该连接 | 未走到 sideband 那一跳；`SPEC-EP-012` 描述的两跳链可能需要真实 WebRTC 协商环境 |
| A14 | 文件上传会 PUT 到区域 `*.oaiusercontent.com` | 场景跑通但无该连接 | 未走到区域 URL；PUT 目标来自 create 响应，非客户端可强制 |

## 4. 已排除的手段及理由

| 手段 | 排除理由 |
|---|---|
| 伪造 `access_token` 的 JWT `exp` | 需重新签名，不可行 |
| 把 `access_token` 置为无效值以触发 401→刷新 | 篡改真实登录凭据；刷新若成功会写回新 token，风险不对称，且已超出「I 类触发干预」边界 |
| 更深地干预 realtime 协商 | 会替换官方出站形态，违反 §5.2／§6.2「官方取证不得替换出站形态」 |
| 构造假的区域文件 URL | 同上，且会伪造上游响应 |

## 5. 备选方案（待审核裁定）

### 方案一：classify 阶段把 SPEC-EP-002 标 `change`

把四条 SNI check 拆分处置：

- `chatgpt-sni` 有充分证据，**保留原判据**；
- `auth-sni`／`api-sni`／`regional-file-sni` 改为**条件判据**：当且仅当本轮证据中
  观测到对应出站时，才断言其 SNI 必须等于该域名；未观测到时不构成失败。

同时把 A11／A13／A14 的 `required_artifact_kinds` 相应调整。

与 `SPEC_TLS_003_JUDGMENT_DEFECT.md` 的处置同源：判据与真实可采集能力对齐，不降低
已有部分的强度。**风险**：条件判据无法证明「官方确实用这些域名」，只能证明「若用则
域名正确」；若某版本悄悄改了这些域名而本轮又未触发，规则不会报警。

### 方案二：保留缺口并标 `blocked`

符合 §6.4 对证据不足的处置，但 `profile_approved` 要求 `blocked=0`，升级流程会卡死
在 §6.4，无法推进。

### 方案三：找到安全的真实触发方式

需要外部输入。待确认的问题：

1. 官方 Codex CLI 判断 OAuth token 需要刷新的**确切条件**是什么？（`codex-rs` 源码已
   冻结在 `/root/oauth-capture/sources/formal-codex-cli-0.147.0/codex-rs`，可查证）
2. `realtime-webrtc` 走到 `api.openai.com` sideband 的前置条件是什么？
3. 文件上传返回区域 `*.oaiusercontent.com` URL 的触发条件是什么？

若这三条能在不替换官方出站形态的前提下满足，则应重采而非改判据。

## 6. 当前状态

- k36 官方证据完好：19/19 job complete、22 个证据根，未封存（`awaiting_receipts`）；
- 三个 job 的 relay 字节与 pcap 均已产出，只是缺目标域名；
- **`seal` 未执行**，等待本文裁定后决定：改判据后重建 Campaign，或按新的触发方式重采；
- Active 仍为 0.145，0.147 未替换。

## 7. 需要审核回答的问题

1. 方案一的条件判据是否可接受？若可接受，"未观测到即不失败"的边界如何界定才不会被
   滥用为「跑不过就跳过」？
2. 若倾向方案三，第 5 节的三个触发条件是否有已知答案？
3. `SPEC-EP-002` 的原始意图是否就是「必须在一轮采集中同时观测到四个域名」？若是，
   则本规则要求的是一个**跨场景的完整会话矩阵**，现有 job 设计从未满足过——这一点
   是否需要在场景定义层面重构？
