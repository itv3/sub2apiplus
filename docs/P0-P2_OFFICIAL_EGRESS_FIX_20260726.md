# P0–P2 官方客户端伪装修复与验收记录

> **历史记录**：本文冻结 `0.1.165-2` 当轮结果，已被
> [0.1.165-3 最终复核记录](P0-P2_OFFICIAL_EGRESS_REVIEW_FIX_20260727.md)取代。
> 当前生产镜像、抓包结论和 #50 上游账号阻断状态以新文档为准。

## 1. 最终结论

2026-07-26 确认的 P0、P1、P2 缺口已完成修复、完整测试、Vircs 直接编译、生产镜像切换和三组抓包复验。

| 项目 | 最终结果 |
|---|---|
| 源码版本 | `0.1.165-2` |
| 生产镜像 | `sub2apiplus:p0-p1-p2-ws-max-output-fix-0.1.165-2-20260726T161107Z` |
| 镜像 ID | `sha256:7d48c1269bb49de78d7c81f1cd0f6116128705ba03fad18860bed226c94d7260` |
| 二进制标识 | `0.1.165-2 / p0-p1-p2-ws-max-output-fix` |
| 生产状态 | `healthy`，重启次数 0，根路径、`/health`、真实 `/v1/models` 探测通过 |
| 应用层验收 | AH、OH、OW 均为 `contract_equal=true`、语义守恒、`undeclared_differences=0` |
| direct TLS 验收 | S1/S2/S4 共 9 个业务场景，9/9 `equal=true` |
| Kilo 六组合验收 | 6 个协议转换与路由组合分别执行 S1/S2/S4，共 18/18 通过；6 个 S4 均只调用一次读取工具 |

本项目的验收口径是“应用层契约近似 + 已实现传输层画像对齐”，不是对官方客户端进行原始字节复制。`raw_equal=false` 保留独立运行正文、动态身份、Header 顺序等真实差异，不代表契约失败。

## 2. 修复清单

### 2.1 P0：功能、安全与 CI

| 编号 | 修复结果 | 状态 | 主要证据 |
|---|---|---|---|
| P0-01 | 删除 handler 遗留的 3 个未使用函数 | 已验收 | golangci-lint v2.9.0：`0 issues` |
| P0-02 | tool search 同时识别 Bedrock 信号与 `custom.defer_loading` | 已验收 | Anthropic beta 正反单测；最终 S4 抓包通过 |
| P0-03 | Official Egress 动态 beta 统一经过 BetaPolicy，并增加模型门控 | 已验收 | allow/drop/check 与模型门控专项测试通过 |
| P0-04 | OpenAI OAuth HTTP/compact 补 `version: 0.145.0` | 已验收 | 官方与候选 MITM 均实证该 Header |
| P0-05 | OpenAI HTTP 最终请求体使用 zstd level 3，重试可重放 | 已验收 | 压缩/解压/重试测试；候选 MITM 实证 `content-encoding: zstd` |
| P0-06 | Responses Lite 首次请求自动加载模型 manifest；单飞、超时、缓存复用、失败保持完整语义 | 已验收 | 模型能力专项测试与 gpt-5.6 候选抓包通过 |
| P0-07 | Cookie jar 只接受 HTTPS ChatGPT 域名的 Cloudflare allowlist Cookie，按账号与代理隔离；WS 禁用 jar | 已验收 | Cookie 接受/拒绝/隔离/代理切换测试；冷 jar 生命周期比较通过 |
| P0-08 | 裸 `/chat/completions` 及子路径进入规范化官方画像 | 已验收 | endpoint alias 单测通过 |
| P0-09 | `/v1/messages/count_tokens` 使用独立规范化端点 | 已验收 | endpoint 与 official egress 单测通过 |
| P0-10 | OpenAI OAuth Responses Lite 的 HTTP/WS 终态统一删除上游不支持的顶层 `max_output_tokens` | 已验收 | WS 终态单测；真实 Kilo OpenAI Responses→OpenAI S1/S2/S4 通过 |

### 2.2 P1：画像与状态生命周期

| 编号 | 修复结果 | 状态 | 主要证据 |
|---|---|---|---|
| P1-01 | `cc_entrypoint=sdk-cli` 使用 Agent SDK system prefix，不再伪装 DEFAULT Claude CLI prefix | 已验收 | Anthropic profile 单测与最终 AH 对比通过 |
| P1-02 | 删除固定 `[System Instructions]` 包装和虚假 assistant 确认语，保留第三方 system 语义 | 已验收 | system prompt 回归测试与 ingress→egress 语义守恒通过 |
| P1-03 | compact 保留 `service_tier`，补 `prompt_cache_key` 和 installation-id，不强制 `Accept` | 已验收 | compact 专项测试与比较器 compact 覆盖通过 |
| P1-04 | 拒绝客户端 turn-state；HTTP 仅同请求重试回放；WS 仅同连接将握手响应状态注入后续 `client_metadata` | 已验收 | HTTP/WS 生命周期和反向测试；OW 逐项 metadata 检查通过 |
| P1-05 | Codex token exchange/refresh 改为官方 JSON 体，身份为 `codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64)` + `originator` | 已验收 | OAuth service 单测通过 |
| P1-06 | 模型、compact、配额、PAT、alpha search 等辅助出站统一当前端点身份，删除旧 beta/旧系统组合 | 已验收 | 身份一致性和辅助端点专项测试通过 |
| P1-07 | 1M/structured outputs/tool search 按模型和请求能力补齐，不再静态覆盖 | 已验收 | Anthropic beta 测试与最终 AH S1/S2/S4 通过 |

### 2.3 P2：画像硬化、比较器与文档

| 编号 | 修复结果 | 状态 | 主要证据 |
|---|---|---|---|
| P2-01 | installation-id 保持 v4；session/turn 等字段按官方字段规则生成 | 已验收 | UUID 版本专项测试通过 |
| P2-02 | 已知 OpenAI/Anthropic 顶层字段使用稳定有序编码，不改写嵌套用户数据 | 已验收 | ordered JSON 与重试缓存测试通过 |
| P2-03 | 比较器只在 thinking 场景允许删除 sampling 字段；非 thinking 逐字段比较 | 已验收 | 比较器正反测试通过 |
| P2-04 | 比较器覆盖 compact、全部已知顶层参数、Cookie 生命周期、turn-state、beta 和 direct TLS 契约 | 已验收 | 抓包工具 80/80 测试；三条最终报告通过 |
| P2-05 | README、实证归档、本文档和本地抓包清单更新为最终版本 | 已验收 | 当前文档与抓包 `MANIFEST.md` |
| P2-06 | 明确披露 `cch`、Header 顺序和伴随流量边界 | 已验收 | 本文第 3 节和 README §1.1.2 |

## 3. 明确边界与有意取舍

1. Anthropic `cch` 由官方 Bun HTTP 栈按最终请求体生成。本项目不生成固定、随机或复用的虚假 `cch`。
2. Go HTTP 栈不能保证与 Bun/reqwest 完全相同的 Header 顺序。原始差异保留，契约比较不把顺序当作语义失败。
3. 不伪造官方遥测、插件枚举、内部 system prompt 或内部工具内容。
4. 非 thinking 时保留第三方显式 sampling 语义；thinking 时按上游约束清理 `temperature/top_p/top_k`，避免 400。
5. 官方 WS pcap 中 30-cipher 的 `/models` 属于辅助 HTTP 连接；WS 业务画像只使用 10-cipher ClientHello。
6. OpenAI HTTP/WS 官方实测 offered ALPN 为空，因此保持为空；不凭推测添加 ALPN 或强制 session resumption。

## 4. 完整测试结果

### 4.1 后端与静态检查

| 命令/范围 | 结果 |
|---|---|
| `go test ./... -count=1` | 通过 |
| `go test -tags=unit ./... -count=1` | 通过 |
| `go test -tags=integration ./... -count=1` | 通过 |
| `go vet ./...` | 通过 |
| golangci-lint v2.9.0 | `0 issues` |
| 后端生产构建（`VERSION=0.1.165-2`） | 通过 |

### 4.2 前端、keeper、容器与抓包工具

| 范围 | 结果 |
|---|---|
| 前端 lint / typecheck / 全量测试 | 通过；191 个文件、1318 个测试 |
| keeper test / build | 通过 |
| Apple container lifecycle 测试 | 通过 |
| 抓包工具测试 | 本地通过；Vircs 80/80 通过 |
| Vircs Docker 多阶段构建 | 通过 |

### 4.3 `max_output_tokens` 回归修复复验

2026-07-27 的真实 Kilo 复验发现，OpenAI Responses→OpenAI OAuth WebSocket
路径重新向上游发送了 `max_output_tokens`。原因不是上游契约变化，而是 P0–P2
收尾改动误删了共享终态归一化函数中的删除逻辑，并把 WS 测试从“字段不存在”
改成了“保留 32000”；HTTP 路径因更早的转换层仍会删除该字段，所以没有同时失败。

修复后结果：

| 范围 | 结果 |
|---|---|
| Official Egress HTTP/WS Lite 定向测试 | 通过；两条路径均不向上游发送 `max_output_tokens` |
| `go test ./internal/... -count=1` | 53 个包通过，退出码 0 |
| `go vet ./internal/...`、`git diff --check` | 通过 |
| Kilo OpenAI Responses→OpenAI OAuth | S1、S2 同会话双轮、S4 单次读取工具全部通过 |
| Kilo 完整六组合 | S1/S2/S4 共 18/18 通过；Vircs 新增 30 条对应业务记录 |

## 5. Vircs 构建、部署与回滚

| 项目 | 值 |
|---|---|
| 最终源码目录 | `/root/sub2apiplus-build/p0-p1-p2-ws-max-output-fix-20260726T161107Z` |
| 运行镜像 | `sub2apiplus:p0-p1-p2-ws-max-output-fix-0.1.165-2-20260726T161107Z` |
| 镜像 ID | `sha256:7d48c1269bb49de78d7c81f1cd0f6116128705ba03fad18860bed226c94d7260` |
| 上一镜像 | `sub2apiplus:p0-p1-p2-final-0.1.165-2-20260726T140102Z` |
| 上一镜像 ID | `sha256:6c0e54f4e28476604aef489fe90ce3bdf50e9ea8d7b196da2ed93681499e4713` |
| 编排备份 | `/root/Docker/sub2apiplus/app/docker-compose.yml.bak-20260726T161107Z` |

切换只替换主服务镜像，PostgreSQL、Redis、keeper、`.env` 和数据卷均未修改。最终主服务 healthy、重启次数 0；keeper running、重启次数 0；测试账号 #50/#90 的代理和调度状态已恢复，临时代理与抓包端口均已清理。

## 6. 抓包验收

### 6.1 三组应用层

官方基准 `oauth-oauth-p0p2-zstd-final-20260726T1420Z` 包含 3 条路径 × direct/MITM × S1/S2/S4 共 18 个完整 case。候选运行时间戳为 `20260726T142438Z`。

| 分组 | 候选业务样本 | `contract_equal` | 语义守恒 | 未声明差异 |
|---|---:|---:|---:|---:|
| AH：Anthropic HTTP | 5 | true | true | 0 |
| OH：OpenAI HTTP | 5 | true | true | 0 |
| OW：OpenAI WebSocket | 9 条 `response.create` | true | true | 0 |

### 6.2 direct TLS

候选 direct 首轮保留连接池真实行为；因 S2/S4 复用连接没有新 ClientHello，又按单元清空连接池补抓 4 个场景。最终映射覆盖 9/9：

| 路径 | 业务 TLS 契约 | 结果 |
|---|---|---|
| Anthropic HTTP | 17-cipher + ALPN `http/1.1` | 3/3 |
| OpenAI HTTP | 30-cipher + 空 ALPN | 3/3 |
| OpenAI WebSocket | 10-cipher + 空 ALPN | 3/3 |

## 7. 本地证据目录

完整官方基准和 Sub2API 出站原始证据已拉回：

```text
/Users/czs/Developer/sub2apiplus/local-analysis/captures/official-egress-20260726/
├── official-client/       # AH-1、OH-1、OW-1；18 个完整 case
├── sub2api-egress/
│   ├── mitm/              # AH-2、OH-2、OW-2 与 ingress
│   └── direct/            # 候选业务 ClientHello 原始 pcap
├── comparisons/           # 应用契约、语义守恒和 direct TLS 报告
├── MANIFEST.md            # 版本、路径、结论、敏感性说明
└── SHA256SUMS             # 本目录除自身外的文件哈希
```

完整性检查结果：官方 manifest 所列 152 个 artifact 的大小与 SHA-256 全部匹配；候选 direct summary 所列 pcap 全部匹配；候选文本使用实际测试 API Key 精确值扫描，0 命中。OAuth 凭据未进入编排器内存，无法进行精确值扫描，该限制已记录。原始 Body、CLI 事件、日志和 pcap 属于私有敏感证据，目录已被 Git 忽略且权限已收紧。

最终状态：**P0、P1、P2 全部已验收。**
